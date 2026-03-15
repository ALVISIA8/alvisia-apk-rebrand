#!/usr/bin/env python3
"""
ALVISIA APK REBRAND SERVER
Flask backend — apktool + apksigner + Java
"""

import os, sys, re, shutil, subprocess, uuid, json, time, threading, zipfile, glob
from pathlib import Path
from flask import Flask, request, jsonify, send_file, after_this_request
from flask_cors import CORS
from werkzeug.utils import secure_filename
from PIL import Image
import logging

# ══════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════
BASE_DIR    = Path(__file__).parent
WORK_DIR    = BASE_DIR / 'work'
UPLOAD_DIR  = BASE_DIR / 'uploads'
OUTPUT_DIR  = BASE_DIR / 'outputs'
TOOLS_DIR   = BASE_DIR / 'tools'
KEYSTORE    = BASE_DIR / 'alvisia.keystore'
APKTOOL_JAR = TOOLS_DIR / 'apktool.jar'

MAX_APK_SIZE = 300 * 1024 * 1024  # 300MB
# Render free tier RAM: 512MB — APK > 150MB mungkin OOM
# Upgrade ke Render Starter ($7/bln) untuk APK besar
RENDER_FREE_WARN = 150 * 1024 * 1024  # warn if > 150MB
JOB_TIMEOUT  = 600  # 10 menit

for d in [WORK_DIR, UPLOAD_DIR, OUTPUT_DIR, TOOLS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(message)s')
log = logging.getLogger('alvisia')

app = Flask(__name__)
CORS(app, origins='*', 
     allow_headers=['Content-Type','Authorization','X-Requested-With'],
     methods=['GET','POST','OPTIONS'])

@app.after_request
def add_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization'
    return response
app.config['MAX_CONTENT_LENGTH'] = MAX_APK_SIZE

# In-memory job store
jobs = {}  # job_id -> {status, log, progress, result_file}
jobs_lock = threading.Lock()

# ══════════════════════════════════════════
#  KEYSTORE — auto-generate jika belum ada
# ══════════════════════════════════════════
def ensure_keystore():
    if KEYSTORE.exists():
        return True
    log.info("Generating keystore...")
    cmd = [
        'keytool', '-genkeypair',
        '-keystore', str(KEYSTORE),
        '-alias', 'alvisia',
        '-keyalg', 'RSA', '-keysize', '2048',
        '-validity', '10000',
        '-storepass', 'alvisia123',
        '-keypass', 'alvisia123',
        '-dname', 'CN=ALVISIA, OU=ALVISIA, O=ALVISIA, L=ID, ST=ID, C=ID',
        '-noprompt'
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log.error(f"Keystore gen failed: {r.stderr}")
        return False
    log.info("Keystore generated.")
    return True

# ══════════════════════════════════════════
#  JOB HELPERS
# ══════════════════════════════════════════
def job_log(job_id, msg, color='var(--g)'):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]['log'].append({'msg': msg, 'color': color})
            log.info(f"[{job_id[:8]}] {msg}")

def job_progress(job_id, pct, msg=''):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]['progress'] = pct
            jobs[job_id]['progress_msg'] = msg

def job_set(job_id, key, val):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id][key] = val

# ══════════════════════════════════════════
#  ICON RESIZE
# ══════════════════════════════════════════
ICON_SIZES = {
    'mipmap-ldpi':     36,
    'mipmap-mdpi':     48,
    'mipmap-hdpi':     72,
    'mipmap-xhdpi':   96,
    'mipmap-xxhdpi':  144,
    'mipmap-xxxhdpi': 192,
}

def replace_icons(decompile_dir: Path, icon_bytes: bytes, job_id: str):
    try:
        from io import BytesIO
        img = Image.open(BytesIO(icon_bytes)).convert('RGBA')
    except Exception as e:
        job_log(job_id, f'⚠️ Icon parse error: {e}', 'var(--y)')
        return

    res_dir = decompile_dir / 'res'
    replaced = 0
    for folder, size in ICON_SIZES.items():
        mipmap_path = res_dir / folder
        if not mipmap_path.exists():
            continue
        # Find all launcher icons
        for ext in ['*.png', '*.webp']:
            for icon_file in mipmap_path.glob(ext):
                if any(kw in icon_file.name.lower() for kw in
                       ['launcher', 'icon', 'ic_', 'app_']):
                    resized = img.resize((size, size), Image.LANCZOS)
                    resized.save(str(icon_file), 'PNG')
                    replaced += 1

    # Also handle adaptive icons
    for folder in res_dir.glob('mipmap-*'):
        for icon_file in folder.glob('*.png'):
            if 'foreground' in icon_file.name or 'round' in icon_file.name:
                size = ICON_SIZES.get(folder.name, 192)
                resized = img.resize((size, size), Image.LANCZOS)
                resized.save(str(icon_file), 'PNG')
                replaced += 1

    job_log(job_id, f'🖼️ Icon diganti di {replaced} file', 'var(--g)')

# ══════════════════════════════════════════
#  STRING REPLACE ENGINE
# ══════════════════════════════════════════
def replace_in_file(filepath: Path, replacements: list, job_id: str) -> int:
    """
    replacements = [(old, new), ...]
    Returns count of replacements made
    """
    try:
        content = filepath.read_bytes()
        modified = False
        count = 0
        for old, new in replacements:
            if not old:
                continue
            old_b = old.encode('utf-8', errors='replace') if isinstance(old, str) else old
            new_b = new.encode('utf-8', errors='replace') if isinstance(new, str) else new
            if old_b in content:
                n = content.count(old_b)
                content = content.replace(old_b, new_b)
                count += n
                modified = True
        if modified:
            filepath.write_bytes(content)
        return count
    except Exception as e:
        job_log(job_id, f'⚠️ replace error in {filepath.name}: {e}', 'var(--y)')
        return 0

def scan_and_replace(decompile_dir: Path, replacements: list, job_id: str):
    """Scan ALL files in decompile dir and replace"""
    total = 0
    skip_ext = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.ttf', '.otf',
                '.mp3', '.ogg', '.wav', '.mp4', '.so', '.arsc'}

    # Priority files first
    priority_patterns = ['*.smali', '*.xml', '*.json', '*.js', '*.properties',
                         '*.yaml', '*.yml', '*.txt', '*.html', '*.cfg', '*.ini',
                         '*.conf', '*.config']

    scanned = 0
    for f in decompile_dir.rglob('*'):
        if not f.is_file():
            continue
        if f.suffix.lower() in skip_ext:
            continue
        n = replace_in_file(f, replacements, job_id)
        total += n
        scanned += 1
        if scanned % 200 == 0:
            job_log(job_id, f'   🔍 Scanned {scanned} files, {total} replacements...', 'var(--tx)')

    job_log(job_id, f'✅ Total: {total} replacements di {scanned} files', 'var(--g)')
    return total

# ══════════════════════════════════════════
#  BYPASS LOGIN — smali patch
# ══════════════════════════════════════════
def bypass_login(decompile_dir: Path, job_id: str):
    """
    Patch smali: ubah instruksi kondisi login
    if-eqz  → if-nez  (jika sukses login = 0, ubah jadi != 0 agar bypass)
    if-nez  → if-eqz  (sebaliknya)
    Hanya pada method yang mengandung keyword login/auth/valid/check
    """
    patched = 0
    keywords = [b'login', b'Login', b'LOGIN', b'auth', b'Auth', b'AUTH',
                b'valid', b'Valid', b'VALID', b'check', b'Check', b'CHECK',
                b'verif', b'Verif', b'token', b'Token']

    for smali_file in (decompile_dir).rglob('*.smali'):
        try:
            content = smali_file.read_bytes()
            # Only patch files containing auth keywords
            if not any(kw in content for kw in keywords):
                continue

            original = content
            # Split into methods
            methods = re.split(rb'(\.method\s)', content)
            new_content = b''
            i = 0
            for part in methods:
                if part.startswith(b'.method'):
                    # Find end of method
                    pass
                new_content += part

            # Simpler: parse line by line within suspected auth methods
            lines = content.split(b'\n')
            new_lines = []
            in_auth_method = False
            method_buf = []

            for line in lines:
                stripped = line.strip()
                if stripped.startswith(b'.method'):
                    in_auth_method = any(kw in line for kw in keywords)
                    method_buf = [line]
                elif stripped == b'.end method' and method_buf:
                    method_buf.append(line)
                    if in_auth_method:
                        # Patch this method
                        for ml in method_buf:
                            ml_str = ml.strip()
                            if ml_str.startswith(b'if-eqz'):
                                new_lines.append(ml.replace(b'if-eqz', b'if-nez', 1))
                                patched += 1
                            elif ml_str.startswith(b'if-nez') and b'return' not in ml_str:
                                new_lines.append(ml.replace(b'if-nez', b'if-eqz', 1))
                                patched += 1
                            else:
                                new_lines.append(ml)
                    else:
                        new_lines.extend(method_buf)
                    method_buf = []
                    in_auth_method = False
                elif method_buf:
                    method_buf.append(line)
                else:
                    new_lines.append(line)

            if method_buf:
                new_lines.extend(method_buf)

            new_content = b'\n'.join(new_lines)
            if new_content != content:
                smali_file.write_bytes(new_content)

        except Exception as e:
            job_log(job_id, f'⚠️ bypass error {smali_file.name}: {e}', 'var(--y)')

    job_log(job_id, f'🔓 Bypass Login: {patched} instruksi dipatch', 'var(--g)')
    return patched

# ══════════════════════════════════════════
#  STRINGS.XML — ganti nama app
# ══════════════════════════════════════════
def patch_app_name(decompile_dir: Path, new_name: str, job_id: str):
    patched = 0
    # Find all strings.xml
    for strings_xml in decompile_dir.rglob('strings.xml'):
        try:
            content = strings_xml.read_text(encoding='utf-8', errors='replace')
            # Replace app_name, application_name, display_name etc
            patterns = [
                (r'(<string name="app_name"[^>]*>)[^<]*(</string>)',
                 rf'\g<1>{new_name}\g<2>'),
                (r'(<string name="application_name"[^>]*>)[^<]*(</string>)',
                 rf'\g<1>{new_name}\g<2>'),
                (r'(<string name="display_name"[^>]*>)[^<]*(</string>)',
                 rf'\g<1>{new_name}\g<2>'),
                (r'(<string name="app_display_name"[^>]*>)[^<]*(</string>)',
                 rf'\g<1>{new_name}\g<2>'),
            ]
            new_content = content
            for pat, repl in patterns:
                new_content, n = re.subn(pat, repl, new_content)
                patched += n
            if new_content != content:
                strings_xml.write_text(new_content, encoding='utf-8')
        except Exception as e:
            job_log(job_id, f'⚠️ strings.xml error: {e}', 'var(--y)')

    # Also patch AndroidManifest.xml label
    manifest = decompile_dir / 'AndroidManifest.xml'
    if manifest.exists():
        try:
            content = manifest.read_text(encoding='utf-8', errors='replace')
            # android:label="AppName" or android:label="@string/app_name"
            new_content = re.sub(
                r'(android:label=")([^"@][^"]*?)(")',
                rf'\g<1>{new_name}\g<3>',
                content
            )
            if new_content != content:
                manifest.write_text(new_content, encoding='utf-8')
                patched += 1
        except Exception as e:
            job_log(job_id, f'⚠️ manifest label error: {e}', 'var(--y)')

    job_log(job_id, f'📱 App name dipatch di {patched} lokasi', 'var(--g)')

# ══════════════════════════════════════════
#  PACKAGE RENAME
# ══════════════════════════════════════════
def rename_package(decompile_dir: Path, old_pkg: str, new_pkg: str, job_id: str):
    if not old_pkg or not new_pkg or old_pkg == new_pkg:
        return
    job_log(job_id, f'📦 Rename package: {old_pkg} → {new_pkg}', 'var(--c)')

    # 1. Replace in all text files
    replacements = [
        (old_pkg, new_pkg),
        (old_pkg.replace('.', '/'), new_pkg.replace('.', '/')),
    ]
    scan_and_replace(decompile_dir, replacements, job_id)

    # 2. Rename smali directories
    smali_root = decompile_dir / 'smali'
    if smali_root.exists():
        old_path = smali_root / old_pkg.replace('.', '/')
        new_path = smali_root / new_pkg.replace('.', '/')
        if old_path.exists():
            new_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(str(old_path), str(new_path), dirs_exist_ok=True)
            shutil.rmtree(str(old_path))
            job_log(job_id, f'📂 Smali dir renamed', 'var(--g)')

# ══════════════════════════════════════════
#  MAIN REBRAND WORKER
# ══════════════════════════════════════════
def rebrand_worker(job_id: str, apk_path: Path, params: dict):
    work = WORK_DIR / job_id
    decompile_dir = work / 'decompiled'
    output_apk = work / 'unsigned.apk'
    signed_apk = OUTPUT_DIR / f'{job_id}_rebranded.apk'

    try:
        work.mkdir(parents=True, exist_ok=True)

        # ── STEP 1: Decompile APK ──
        job_log(job_id, '⚙️ [1/7] Decompiling APK dengan apktool...', 'var(--c)')
        job_progress(job_id, 5, 'Decompiling APK...')
        cmd = ['java', '-jar', str(APKTOOL_JAR), 'd',
               str(apk_path), '-o', str(decompile_dir),
               '-f', '--no-res'] if params.get('no_res') else \
              ['java', '-jar', str(APKTOOL_JAR), 'd',
               str(apk_path), '-o', str(decompile_dir), '-f']

        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f'apktool decompile gagal:\n{r.stderr[-500:]}')
        job_log(job_id, f'✅ APK berhasil didecompile', 'var(--g)')
        job_progress(job_id, 20, 'Decompile selesai')

        # ── STEP 2: Replace URL & Key ──
        job_log(job_id, '🔍 [2/7] Scan & Replace URL / Key...', 'var(--c)')
        job_progress(job_id, 25, 'Scanning & replacing...')

        replacements = []
        old_url = params.get('old_url', '').strip()
        new_url = params.get('new_url', '').strip()
        old_key = params.get('old_key', '').strip()
        new_key = params.get('new_key', '').strip()
        old_pkg = params.get('old_pkg', '').strip()
        new_pkg = params.get('new_pkg', '').strip()
        extra_replacements = params.get('extra_replacements', [])

        if old_url and new_url:
            replacements.append((old_url, new_url))
            # Also replace without trailing slash
            if old_url.endswith('/'):
                replacements.append((old_url.rstrip('/'), new_url.rstrip('/')))

        if old_key and new_key:
            replacements.append((old_key, new_key))

        for extra in extra_replacements:
            if '||' in extra:
                parts = extra.split('||', 1)
                if parts[0].strip() and parts[1].strip():
                    replacements.append((parts[0].strip(), parts[1].strip()))

        if replacements:
            total_replaced = scan_and_replace(decompile_dir, replacements, job_id)
            job_log(job_id, f'✅ {total_replaced} string diganti', 'var(--g)')

        job_progress(job_id, 38, 'Replace selesai')

        # ── STEP 3: Rename Package ──
        if old_pkg and new_pkg and old_pkg != new_pkg:
            job_log(job_id, f'📦 [3/7] Rename package: {old_pkg} → {new_pkg}', 'var(--c)')
            job_progress(job_id, 40, 'Renaming package...')
            rename_package(decompile_dir, old_pkg, new_pkg, job_id)
        else:
            job_log(job_id, '📦 [3/7] Package tidak diubah', 'var(--tx)')
        job_progress(job_id, 45, 'Package renamed')

        # ── STEP 4: Ganti Nama App ──
        new_app_name = params.get('app_name', '').strip()
        if new_app_name:
            job_log(job_id, f'📱 [4/7] Ganti nama app: {new_app_name}', 'var(--c)')
            job_progress(job_id, 50, 'Patching app name...')
            patch_app_name(decompile_dir, new_app_name, job_id)
        else:
            job_log(job_id, '📱 [4/7] Nama app tidak diubah', 'var(--tx)')
        job_progress(job_id, 55, 'App name patched')

        # ── STEP 5: Ganti Icon ──
        icon_bytes = params.get('icon_bytes')
        if icon_bytes:
            job_log(job_id, '🖼️ [5/7] Mengganti icon...', 'var(--c)')
            job_progress(job_id, 60, 'Replacing icons...')
            replace_icons(decompile_dir, icon_bytes, job_id)
        else:
            job_log(job_id, '🖼️ [5/7] Icon tidak diubah', 'var(--tx)')
        job_progress(job_id, 65, 'Icon replaced')

        # ── STEP 5b: Bypass Login (opsional) ──
        if params.get('bypass_login'):
            job_log(job_id, '🔓 [5b] Bypass Login — patching smali...', 'var(--y)')
            job_progress(job_id, 68, 'Bypass login patching...')
            bypass_login(decompile_dir, job_id)
            job_progress(job_id, 72, 'Bypass login done')

        # ── STEP 6: Rebuild APK ──
        job_log(job_id, '🔨 [6/7] Rebuild APK dengan apktool...', 'var(--c)')
        job_progress(job_id, 75, 'Building APK...')
        cmd_build = ['java', '-jar', str(APKTOOL_JAR), 'b',
                     str(decompile_dir), '-o', str(output_apk), '--use-aapt2']
        r = subprocess.run(cmd_build, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            # Retry without --use-aapt2
            job_log(job_id, '⚠️ Retry build tanpa aapt2...', 'var(--y)')
            cmd_build2 = ['java', '-jar', str(APKTOOL_JAR), 'b',
                          str(decompile_dir), '-o', str(output_apk)]
            r2 = subprocess.run(cmd_build2, capture_output=True, text=True, timeout=300)
            if r2.returncode != 0:
                raise RuntimeError(f'apktool build gagal:\n{r2.stderr[-500:]}')
        job_log(job_id, f'✅ APK rebuilt: {output_apk.stat().st_size/1024/1024:.1f} MB', 'var(--g)')
        job_progress(job_id, 88, 'Build selesai')

        # ── STEP 7: Sign APK ──
        job_log(job_id, '🔐 [7/7] Signing APK (V1+V2)...', 'var(--c)')
        job_progress(job_id, 90, 'Signing APK...')
        ensure_keystore()

        # Try: 1) uber-apk-signer, 2) apksigner, 3) jarsigner fallback
        uber_signer = TOOLS_DIR / 'apksigner.jar'
        apksigner_bin = shutil.which('apksigner') or                         '/usr/lib/android-sdk/build-tools/apksigner'
        signed = False

        # Method 1: uber-apk-signer (most compatible, V1+V2+V3)
        if uber_signer.exists():
            cmd_uber = [
                'java', '-jar', str(uber_signer),
                '--apks', str(output_apk),
                '--ks', str(KEYSTORE),
                '--ksPass', 'alvisia123',
                '--ksKeyAlias', 'alvisia',
                '--ksKeyPass', 'alvisia123',
                '--out', str(work),
                '--overwrite',
            ]
            r = subprocess.run(cmd_uber, capture_output=True, text=True, timeout=180)
            if r.returncode == 0:
                # uber-apk-signer output: filename-aligned-signed.apk
                candidates = list(work.glob('*-aligned-signed.apk')) +                              list(work.glob('*-signed.apk'))
                if candidates:
                    shutil.copy(str(candidates[0]), str(signed_apk))
                    signed = True
                    job_log(job_id, '✅ Signed with uber-apk-signer (V1+V2+V3)', 'var(--g)')
            else:
                job_log(job_id, f'⚠️ uber-signer: {r.stderr[:100]}', 'var(--y)')

        # Method 2: apksigner binary
        if not signed and shutil.which('apksigner'):
            cmd_sign = [
                'apksigner', 'sign',
                '--ks', str(KEYSTORE),
                '--ks-pass', 'pass:alvisia123',
                '--key-pass', 'pass:alvisia123',
                '--ks-key-alias', 'alvisia',
                '--out', str(signed_apk),
                str(output_apk)
            ]
            r = subprocess.run(cmd_sign, capture_output=True, text=True, timeout=180)
            if r.returncode == 0:
                signed = True
                job_log(job_id, '✅ Signed with apksigner (V1+V2)', 'var(--g)')
            else:
                job_log(job_id, f'⚠️ apksigner: {r.stderr[:100]}', 'var(--y)')

        # Method 3: jarsigner fallback (V1 only)
        if not signed:
            shutil.copy(str(output_apk), str(signed_apk))
            cmd_jar = [
                'jarsigner',
                '-sigalg', 'SHA256withRSA',
                '-digestalg', 'SHA-256',
                '-keystore', str(KEYSTORE),
                '-storepass', 'alvisia123',
                '-keypass', 'alvisia123',
                str(signed_apk), 'alvisia'
            ]
            r = subprocess.run(cmd_jar, capture_output=True, text=True, timeout=180)
            if r.returncode == 0:
                signed = True
                job_log(job_id, '✅ Signed with jarsigner (V1 only)', 'var(--y)')
            else:
                job_log(job_id, f'⚠️ jarsigner: {r.stderr[:200]}', 'var(--y)')

        if not signed:
            # Last resort: unsigned copy
            shutil.copy(str(output_apk), str(signed_apk))
            job_log(job_id, '⚠️ APK tidak ter-sign! Install apksigner.', 'var(--r)')

        job_log(job_id, f'✅ APK signed: {signed_apk.stat().st_size/1024/1024:.2f} MB', 'var(--g)')
        job_progress(job_id, 98, 'Signing selesai')

        # ── DONE ──
        job_log(job_id, '════════════════════════════', 'var(--g)')
        job_log(job_id, '  ✅ REBRAND SELESAI!', 'var(--g)')
        job_log(job_id, f'  📦 Output: {signed_apk.name}', 'var(--g)')
        job_log(job_id, f'  📏 Size: {signed_apk.stat().st_size/1024/1024:.2f} MB', 'var(--g)')
        job_log(job_id, '════════════════════════════', 'var(--g)')

        job_progress(job_id, 100, '✅ Selesai!')
        job_set(job_id, 'status', 'done')
        job_set(job_id, 'result_file', str(signed_apk))
        job_set(job_id, 'result_name', signed_apk.name)

    except Exception as e:
        log.exception(f'Job {job_id} error')
        job_log(job_id, f'❌ ERROR: {str(e)}', 'var(--r)')
        job_set(job_id, 'status', 'error')
        job_set(job_id, 'error', str(e))
    finally:
        # Cleanup work dir (keep output)
        try:
            if work.exists():
                shutil.rmtree(str(work))
        except:
            pass
        # Cleanup upload
        try:
            if apk_path.exists():
                apk_path.unlink()
        except:
            pass

# ══════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════

@app.route('/api/health', methods=['GET'])
def health():
    """Health check + tool version info"""
    info = {'status': 'ok', 'tools': {}}

    # Check Java
    r = subprocess.run(['java', '-version'], capture_output=True, text=True)
    info['tools']['java'] = r.stderr.strip().split('\n')[0] if r.returncode == 0 else 'not found'

    # Check apktool
    r = subprocess.run(['java', '-jar', str(APKTOOL_JAR), '--version'],
                       capture_output=True, text=True)
    info['tools']['apktool'] = r.stdout.strip() if r.returncode == 0 else 'not found'

    # Check apksigner
    apksigner = shutil.which('apksigner')
    info['tools']['apksigner'] = apksigner or 'not found'

    # Check keystore
    info['keystore'] = KEYSTORE.exists()

    return jsonify(info)


@app.route('/api/rebrand', methods=['POST'])
def rebrand():
    """Start rebrand job"""
    # Validate APK
    if 'apk' not in request.files:
        return jsonify({'error': 'APK file required'}), 400
    apk_file = request.files['apk']
    if not apk_file.filename.endswith('.apk'):
        return jsonify({'error': 'File harus .apk'}), 400

    job_id = str(uuid.uuid4())
    apk_path = UPLOAD_DIR / f'{job_id}.apk'

    # Save APK
    apk_file.save(str(apk_path))
    apk_size = apk_path.stat().st_size
    log.info(f"APK saved: {apk_size/1024/1024:.1f} MB")
    if apk_size > RENDER_FREE_WARN:
        log.warning(f"Large APK {apk_size/1024/1024:.0f}MB may cause OOM on free tier")

    # Parse params
    params = {
        'app_name':          request.form.get('app_name', '').strip(),
        'old_pkg':           request.form.get('old_pkg', '').strip(),
        'new_pkg':           request.form.get('new_pkg', '').strip(),
        'old_url':           request.form.get('old_url', '').strip(),
        'new_url':           request.form.get('new_url', '').strip(),
        'old_key':           request.form.get('old_key', '').strip(),
        'new_key':           request.form.get('new_key', '').strip(),
        'bypass_login':      request.form.get('bypass_login') == '1',
        'extra_replacements': [x.strip() for x in
                               request.form.get('extra_replacements', '').split('\n')
                               if x.strip()],
    }

    # Icon
    if 'icon' in request.files:
        icon_f = request.files['icon']
        params['icon_bytes'] = icon_f.read()

    # Init job
    with jobs_lock:
        jobs[job_id] = {
            'status': 'running',
            'log': [],
            'progress': 0,
            'progress_msg': 'Memulai...',
            'result_file': None,
            'result_name': None,
            'error': None,
            'created': time.time(),
        }

    # Start worker thread
    t = threading.Thread(target=rebrand_worker,
                         args=(job_id, apk_path, params),
                         daemon=True)
    t.start()

    return jsonify({'job_id': job_id})


@app.route('/api/job/<job_id>', methods=['GET'])
def job_status(job_id):
    """Poll job status"""
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify({
        'status':       job['status'],
        'progress':     job['progress'],
        'progress_msg': job['progress_msg'],
        'log':          job['log'][-50:],  # last 50 lines
        'result_name':  job['result_name'],
        'error':        job['error'],
    })


@app.route('/api/download/<job_id>', methods=['GET'])
def download(job_id):
    """Download result APK"""
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job['status'] != 'done':
        return jsonify({'error': 'Job not done or not found'}), 404
    result_file = Path(job['result_file'])
    if not result_file.exists():
        return jsonify({'error': 'File not found'}), 404

    @after_this_request
    def cleanup(response):
        # Cleanup after download
        def _del():
            time.sleep(30)
            try: result_file.unlink()
            except: pass
            with jobs_lock:
                jobs.pop(job_id, None)
        threading.Thread(target=_del, daemon=True).start()
        return response

    return send_file(
        str(result_file),
        as_attachment=True,
        download_name=job['result_name'],
        mimetype='application/vnd.android.package-archive'
    )


@app.route('/api/cleanup', methods=['POST'])
def cleanup_jobs():
    """Clean old jobs (>1 hour)"""
    cutoff = time.time() - 3600
    removed = 0
    with jobs_lock:
        old = [jid for jid, j in jobs.items() if j.get('created', 0) < cutoff]
        for jid in old:
            jobs.pop(jid)
            removed += 1
    return jsonify({'removed': removed})


# ══════════════════════════════════════════
#  ENTRY
# ══════════════════════════════════════════
if __name__ == '__main__':
    print("""
╔══════════════════════════════════════════╗
║   ALVISIA APK REBRAND SERVER v5          ║
║   Flask + apktool + apksigner + Java     ║
╚══════════════════════════════════════════╝
    """)
    ensure_keystore()
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'false').lower() == 'true'
    log.info(f"Starting on port {port}")
    app.run(host='0.0.0.0', port=port, debug=debug, threaded=True)
