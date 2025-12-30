FROM python:3.12-slim

ARG TD_VERSION=1.56.2

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fontconfig libfontconfig1 \
    fonts-noto-core fonts-noto-color-emoji \
    ca-certificates curl unzip \
  && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /tmp/td.zip \
      https://github.com/lay295/TwitchDownloader/releases/download/${TD_VERSION}/TwitchDownloaderCLI-${TD_VERSION}-Linux-x64.zip \
  && unzip -j /tmp/td.zip "TwitchDownloaderCLI*" -d /usr/local/bin/ \
  && chmod +x /usr/local/bin/TwitchDownloaderCLI \
  && rm -f /tmp/td.zip

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

CMD ["bash","-lc","uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]
