FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV JAVA_HOME=/usr/lib/jvm/java-11-openjdk-amd64
ENV PATH=$PATH:$JAVA_HOME/bin

# ── Install dependencies ──
RUN apt-get update -qq && apt-get install -y -qq \
    openjdk-11-jdk \
    python3 python3-pip \
    wget curl unzip \
    zipalign \
    && rm -rf /var/lib/apt/lists/*

# ── Install Python packages ──
RUN pip3 install flask flask-cors pillow gunicorn --quiet

# ── Download apktool ──
RUN mkdir -p /app/tools && \
    wget -q https://github.com/iBotPeaches/Apktool/releases/download/v2.9.3/apktool_2.9.3.jar \
         -O /app/tools/apktool.jar

# ── Download apksigner (dari Android build-tools) ──
RUN wget -q https://github.com/patrickfav/uber-apk-signer/releases/download/v1.3.0/uber-apk-signer-1.3.0.jar \
         -O /app/tools/apksigner.jar || true

# ── Generate keystore ──
RUN keytool -genkeypair \
    -keystore /app/alvisia.keystore \
    -alias alvisia \
    -keyalg RSA -keysize 2048 \
    -validity 10000 \
    -storepass alvisia123 \
    -keypass alvisia123 \
    -dname "CN=ALVISIA, OU=ALVISIA, O=ALVISIA, L=ID, ST=ID, C=ID" \
    -noprompt

# ── Copy app ──
WORKDIR /app
COPY server.py .

# ── Create dirs ──
RUN mkdir -p work uploads outputs tools

# ── Expose port ──
EXPOSE 5000

# ── Run with gunicorn ──
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--timeout", "600", \
     "--workers", "2", "--threads", "4", "server:app"]
