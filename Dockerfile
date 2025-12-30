# =========================
# Builder: download TwitchDownloaderCLI once
# =========================
FROM python:3.12-slim AS td_builder

ARG TD_VERSION=1.56.2
ENV DEBIAN_FRONTEND=noninteractive

RUN set -eux; \
  apt-get update; \
  apt-get install -y --no-install-recommends ca-certificates curl unzip; \
  rm -rf /var/lib/apt/lists/*

RUN set -eux; \
  curl -fsSL -o /tmp/td.zip \
    "https://github.com/lay295/TwitchDownloader/releases/download/${TD_VERSION}/TwitchDownloaderCLI-${TD_VERSION}-Linux-x64.zip"; \
  unzip -j /tmp/td.zip "TwitchDownloaderCLI*" -d /out/; \
  chmod +x /out/TwitchDownloaderCLI; \
  rm -f /tmp/td.zip

# =========================
# Runtime
# =========================
FROM python:3.12-slim

ARG TD_VERSION=1.56.2
ENV DEBIAN_FRONTEND=noninteractive

# Better defaults for containers + Render Free temp workaround
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
# - ffmpeg for combine
# - fontconfig + noto fonts for chat rendering
# - ICU runtime fixes TwitchDownloaderCLI (.NET) crash
# - tini for clean shutdown
RUN set -eux; \
  apt-get update; \
  apt-get install -y --no-install-recommends \
    ffmpeg \
    fontconfig libfontconfig1 \
    fonts-noto-core fonts-noto-color-emoji fonts-noto-cjk \
    ca-certificates \
    tini; \
  # ICU package name varies by Debian base; try common runtime packages, fallback to -dev as last resort
  (apt-get install -y --no-install-recommends libicu72 \
    || apt-get install -y --no-install-recommends libicu71 \
    || apt-get install -y --no-install-recommends libicu70 \
    || apt-get install -y --no-install-recommends libicu-dev); \
  rm -rf /var/lib/apt/lists/*

# TwitchDownloaderCLI binary (from builder) - no curl/unzip in final image
COPY --from=td_builder /out/TwitchDownloaderCLI /usr/local/bin/TwitchDownloaderCLI

# Create non-root user + writable dirs (Render Free needs persistent writable paths)
RUN set -eux; \
  useradd -m -u 10001 appuser; \
  mkdir -p /app /app/storage /app/tdtmp; \
  chown -R appuser:appuser /app

WORKDIR /app

# Install python deps first (better layer caching)
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip setuptools wheel \
  && pip install -r /app/requirements.txt

# Copy app
COPY --chown=appuser:appuser app.py /app/app.py

USER appuser

EXPOSE 10000

# Optional healthcheck (Render has its own, but this helps debugging)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','10000')).read()" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]

# Keep 1 worker for Render Free (memory + CPU)
CMD ["bash","-lc","uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
