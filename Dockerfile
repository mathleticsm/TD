FROM python:3.12-slim

ARG TD_VERSION=1.56.2

# Better defaults for containers
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    # IMPORTANT: avoid Render's /tmp 2GB cap by keeping large temp + downloads elsewhere
    DOWNLOAD_DIR=/app/storage \
    TD_TEMP_DIR=/app/tdtmp \
    TMPDIR=/app/tdtmp

# System deps:
# - ffmpeg for combining videos
# - fontconfig + noto fonts for chat rendering
# - libicu fixes TwitchDownloaderCLI (.NET) crash
# - tini for clean shutdown / signal handling
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fontconfig libfontconfig1 \
    fonts-noto-core fonts-noto-color-emoji fonts-noto-cjk \
    libicu-dev \
    ca-certificates curl unzip \
    tini \
  && rm -rf /var/lib/apt/lists/*

# Download TwitchDownloaderCLI
RUN curl -fsSL -o /tmp/td.zip \
      "https://github.com/lay295/TwitchDownloader/releases/download/${TD_VERSION}/TwitchDownloaderCLI-${TD_VERSION}-Linux-x64.zip" \
  && unzip -j /tmp/td.zip "TwitchDownloaderCLI*" -d /usr/local/bin/ \
  && chmod +x /usr/local/bin/TwitchDownloaderCLI \
  && rm -f /tmp/td.zip

# Create non-root user + writable dirs
RUN useradd -m -u 10001 appuser \
  && mkdir -p /app /app/storage /app/tdtmp \
  && chown -R appuser:appuser /app

WORKDIR /app

# Install python deps first for better caching
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
  && pip install -r /app/requirements.txt

# Copy app
COPY --chown=appuser:appuser app.py /app/app.py

USER appuser

# Render sets PORT
EXPOSE 10000

# Optional: container-level healthcheck (Render uses its own too, but this is helpful)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % (__import__('os').environ.get('PORT','10000'))).read()" || exit 1

# tini helps prevent zombie processes + handles SIGTERM correctly
ENTRYPOINT ["/usr/bin/tini", "--"]

# uvicorn options:
# --proxy-headers helps behind Render proxy
# --forwarded-allow-ips="*" allows X-Forwarded-* headers
CMD ["bash","-lc","uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers --forwarded-allow-ips='*'"]
