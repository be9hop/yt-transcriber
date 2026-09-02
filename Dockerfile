# yt-transcriber — Python 3.12-slim, non-root, /data volume for the cache.

FROM python:3.12-slim

# Non-root user + persistent cache dir
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

USER appuser

ENV PORT=8000 \
    CACHE_DB_PATH=/data/cache.sqlite3

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT','8000') + '/health', timeout=4).getcode() == 200 else 1)"

# sh -c so the PORT env var is expanded; exec so signals reach uvicorn
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
