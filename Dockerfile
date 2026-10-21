# syntax=docker/dockerfile:1.7

FROM node:22-slim AS web
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/codepilot

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 codepilot \
    && useradd --uid 10001 --gid codepilot --create-home codepilot

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . && rm -rf /src build

COPY configs ./configs
COPY demo ./demo
COPY docs ./docs
COPY --from=web /build/web/dist ./web/dist

RUN mkdir -p /data && chown -R codepilot:codepilot /app /data
USER codepilot
WORKDIR /app

ENV CODEPILOT_CONFIG_DIR=/app/configs \
    CODEPILOT_API_DEMO_ROOT=/app/demo \
    CODEPILOT_WEB_DIST_DIR=/app/web/dist \
    CODEPILOT_DOCS_DIR=/app/docs \
    CODEPILOT_README_PATH=/app/README.md \
    CODEPILOT_ARTIFACTS_DIR=/data/artifacts \
    CODEPILOT_CACHE_DIR=/data/cache \
    CODEPILOT_API_QUEUE_PATH=/data/artifacts/api/api.sqlite3 \
    CODEPILOT_WORKER_HEALTH_PATH=/data/worker.heartbeat.json \
    CODEPILOT_API_HOST=0.0.0.0 \
    CODEPILOT_API_PORT=8000

EXPOSE 8000
CMD ["codepilot", "api"]
