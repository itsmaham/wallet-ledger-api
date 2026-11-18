FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY db/migrations ./migrations
ENV MIGRATIONS_DIR=/srv/migrations

RUN useradd --system --no-create-home app
USER app

EXPOSE 8080
# exec so uvicorn is PID 1 and receives SIGTERM directly (graceful shutdown).
# Access logs are off: they cost latency and are not needed here.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers ${WEB_CONCURRENCY:-4} --loop uvloop --http httptools --no-access-log --timeout-graceful-shutdown 10"]
