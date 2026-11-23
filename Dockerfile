FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg nodejs && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY Main.py ./Main.py

CMD ["sh", "-c", "uvicorn Main:app --host 0.0.0.0 --port ${PORT:-8080}"]
