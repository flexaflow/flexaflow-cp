# Contest judge image.
# Build and run with:  docker compose up -d --build
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Compilers and runtimes for the supported languages, plus bubblewrap for the sandbox.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        bubblewrap gcc g++ libc6-dev default-jdk-headless pypy3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Live-update client, fetched at build time so the running container needs no internet.
ADD https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js /app/static/socket.io.min.js

# Non-root server user. The judged code runs one level deeper, inside bubblewrap.
RUN useradd --system --uid 10001 --no-create-home judge \
 && mkdir -p /data \
 && chown judge:judge /data

ENV JUDGE_DB=/data/judge.db \
    JUDGE_PORT=8000 \
    JUDGE_ASYNC=eventlet \
    JUDGE_WORKERS=3

USER judge
VOLUME ["/data"]
EXPOSE 8000

CMD ["python", "app.py"]
