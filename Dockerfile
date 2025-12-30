FROM python:3.12-slim

ARG TD_VERSION=1.56.2

# Better defaults for containers
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

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

# Create non-root user (safer)
RUN useradd -m -u 10001 appuser \
  && mkdir -p /app /tmp/downloads \
  && chown -R appuser:appuser /app /tmp/downloads

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

# tini helps prevent zombie processes + handles SIGTERM correctly
ENTRYPOINT ["/usr/bin/tini", "--"]

# uvicorn options:
# --proxy-headers helps behind Render proxy
# --forwarded-allow-ips="*" allows X-Forwarded-* headers
CMD ["bash","-lc","uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers --forwarded-allow-ips='*'"]
