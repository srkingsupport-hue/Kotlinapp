# Production Railway Dockerfile — Python 3.11 + FFmpeg + Node.js (for YouTube nsig)
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

# Install FFmpeg (video merging, MP3 encoding, 16D spatial DSP) and Node.js (yt-dlp YouTube JS runtime)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app ./app
COPY static ./static

# Run as unprivileged user for security
RUN useradd --create-home --shell /bin/bash srking \
    && mkdir -p /tmp/srking_web_data/cache /tmp/srking_web_data/jobs \
    && chown -R srking:srking /app /tmp/srking_web_data

USER srking

EXPOSE 8080

# Single uvicorn worker keeps in-memory job SSE streams & single-flight locks consistent
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
