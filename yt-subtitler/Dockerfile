FROM python:3.12-slim

# System deps:
#  - ffmpeg: clip/burn (Debian's build includes libass)
#  - deno: yt-dlp uses it to solve YouTube's JS n-challenge
#  - curl, ca-certificates, unzip: required by deno installer
#  - tini: clean signal handling for the Flask process
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates unzip tini \
    && rm -rf /var/lib/apt/lists/*

# deno (single binary, ~40MB)
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y \
    && /usr/local/bin/deno --version

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY templates ./templates

# /data: outputs (clips, srts, settings.json) — mount a volume here.
# /models: faster-whisper weights cache — mount a volume here too so the
#          ~3GB large-v3 download survives container rebuilds.
ENV IN_DOCKER=1 \
    LISTEN_PORT=8077 \
    DATA_DIR=/data \
    MODEL_CACHE=/models \
    COOKIES_FROM_BROWSER=""
RUN mkdir -p /data /models

EXPOSE 8077
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "app.py"]
