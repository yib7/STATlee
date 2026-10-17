# Web application image. docker compose builds it (docker-compose.yml); by hand,
# from the repo root (the build context):
#   docker build -f docker/app.Dockerfile -t statlee-web .
#
# Untrusted analysis code should run in the separate
# runner image (docker/runner.Dockerfile) via SANDBOX_MODE=docker; the R/Python
# stack here covers SANDBOX_MODE=subprocess deployments.
#
# NOTE: this image ships no `docker` CLI, so SANDBOX_MODE=docker does not work
# from inside it as-is (every run would fail with "Interpreter not available").
# Docker mode needs the app on a host (or a derived image) that has the docker
# CLI and access to the daemon; see docs/ARCHITECTURE.md "Sandbox".
#
# .dockerignore keeps .env, instance/ (local DBs) and .git out of the build
# context, so no secret or local data is baked into a layer.
FROM python:3.12-slim

# R toolchain for R-language analyses (subprocess sandbox mode)
RUN apt-get update && apt-get install -y --no-install-recommends \
    r-base \
    r-cran-dplyr \
    r-cran-ggplot2 \
    r-cran-mass \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# requirements/postgres.txt adds the psycopg driver for the documented
# production DATABASE_URL=postgresql+psycopg://... (D5-11).
COPY requirements/base.txt requirements/postgres.txt ./requirements/
RUN pip install --no-cache-dir -r requirements/base.txt -r requirements/postgres.txt

COPY . .

# Run as an unprivileged user (D5-17). In the default subprocess sandbox mode
# generated code runs as this user, so it cannot modify the app code (owned by
# root) and can only write its run dir, /tmp and the instance volume.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/instance/uploads \
    && chown -R app:app /app/instance

# UPLOAD_ROOT sits under /app/instance; mount a persistent volume there (the
# compose file does) so logged-in users' files and the SQLite DB survive a
# container replacement (D5-9).
ENV APP_ENV=production \
    UPLOAD_ROOT=/app/instance/uploads
EXPOSE 5000

USER app

# Hit /health on whatever port the host injected (1.2)
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','5000')+'/health')"]

# Production WSGI server honoring $PORT, threaded workers for SSE (1.2).
# WEB_CONCURRENCY controls the worker count and defaults to 1 so the in-memory
# rate-limit buckets and the in-process monthly priority ceiling hold at their
# configured numbers out of the box (a single process owns both counters). An
# operator raising WEB_CONCURRENCY above 1 MUST set a shared RATELIMIT_STORAGE_URI
# (e.g. redis://); otherwise each worker keeps its own copy of the rate-limit
# buckets and the configured limits no longer hold across the fleet. The monthly
# priority ceiling is ALSO per-process (and resets on restart), so it multiplies
# by the worker count without a shared backing store.
# --preload imports wsgi:app (and runs the boot schema upgrade in _init_schema)
# ONCE in the arbiter before forking workers, so multiple workers never run
# Alembic concurrently against the same DB (which would race on DDL and crash a
# worker). _init_schema calls db.engine.dispose() so no pooled connection is
# shared across the fork. --threads 8 keeps workers threaded (SSE needs it).
CMD ["sh", "-c", "gunicorn --preload --bind 0.0.0.0:${PORT:-5000} --workers ${WEB_CONCURRENCY:-1} --threads 8 --timeout 120 --graceful-timeout 30 wsgi:app"]
