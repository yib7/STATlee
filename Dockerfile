# Web application image. Untrusted analysis code should run in the separate
# runner image (runner.Dockerfile) via SANDBOX_MODE=docker; the R/Python
# stack here covers SANDBOX_MODE=subprocess deployments.
FROM python:3.12-slim

# R toolchain for R-language analyses (subprocess sandbox mode)
RUN apt-get update && apt-get install -y --no-install-recommends \
    r-base \
    r-cran-dplyr \
    r-cran-ggplot2 \
    r-cran-mass \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV APP_ENV=production
EXPOSE 5000

# Hit /health on whatever port the host injected (1.2)
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','5000')+'/health')"]

# Production WSGI server honoring $PORT, threaded workers for SSE (1.2).
# WEB_CONCURRENCY controls the worker count and defaults to 1 so the in-memory
# rate-limit buckets and the in-process monthly billing ceiling hold at their
# configured numbers out of the box (a single process owns both counters). An
# operator raising WEB_CONCURRENCY above 1 MUST set a shared RATELIMIT_STORAGE_URI
# (e.g. redis://) — otherwise each worker keeps its own copy of the rate-limit
# buckets and the configured limits no longer hold across the fleet. And if
# billing is enabled, note the monthly priority ceiling is ALSO per-process, so
# it multiplies by the worker count without a shared backing store.
# --preload imports wsgi:app (and runs the boot schema upgrade in _init_schema)
# ONCE in the arbiter before forking workers, so multiple workers never run
# Alembic concurrently against the same DB (which would race on DDL and crash a
# worker). _init_schema calls db.engine.dispose() so no pooled connection is
# shared across the fork. --threads 8 keeps workers threaded (SSE needs it).
CMD ["sh", "-c", "gunicorn --preload --bind 0.0.0.0:${PORT:-5000} --workers ${WEB_CONCURRENCY:-1} --threads 8 --timeout 120 --graceful-timeout 30 wsgi:app"]
