# syntax=docker/dockerfile:1
# Pin to a specific Python patch release. Replace the tag with a digest for
# stronger reproducibility: FROM python@sha256:<DIGEST>
FROM python:3.11.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    APP_USER=cryptoagent \
    APP_UID=1500 \
    APP_GID=1500 \
    WEB_UI_ENABLED=false \
    WEB_UI_HOST=0.0.0.0 \
    TZ=Europe/Zurich

# Install only what is required for runtime and health checks.
# `curl` is used by scripts/healthcheck.sh to probe /health/live and /health/ready.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

# Create a dedicated non-root user/group.
RUN groupadd --gid ${APP_GID} ${APP_USER} \
    && useradd --uid ${APP_UID} --gid ${APP_GID} \
               --create-home --shell /bin/false ${APP_USER}

WORKDIR /app

# Install Python dependencies first for better layer caching.
COPY --chown=${APP_UID}:${APP_GID} requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code and runtime scripts.
# config.json is mounted at runtime from the host data directory;
# the copy below is a safe fallback default only.
COPY --chown=${APP_UID}:${APP_GID} main.py config.json ./
COPY --chown=${APP_UID}:${APP_GID} bot/ ./bot/
COPY --chown=${APP_UID}:${APP_GID} web/ ./web/
COPY --chown=${APP_UID}:${APP_GID} scripts/entrypoint.sh scripts/healthcheck.sh ./scripts/

RUN chmod +x /app/main.py /app/scripts/entrypoint.sh /app/scripts/healthcheck.sh

# Pre-create runtime directories with the app user's ownership. Fresh named
# volumes (compose.public.yaml) inherit this UID/GID; without it Docker creates
# the mountpoints root-owned and the non-root app cannot write its data.
RUN mkdir -p /app/data /app/backups /app/logs \
    && chown -R ${APP_UID}:${APP_GID} /app/data /app/backups /app/logs

USER ${APP_USER}

# Web dashboard port. The bot itself only needs outbound HTTPS unless the
# dashboard is enabled via WEB_UI_ENABLED.
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
  CMD ["/app/scripts/healthcheck.sh"]

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
