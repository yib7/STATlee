# Minimal, network-less execution image for SANDBOX_MODE=docker (roadmap 0.3).
# Build once:  docker build -f runner.Dockerfile -t statlee-runner .
# The app launches throwaway sibling containers from this image per run:
#   docker run --rm --network none --read-only --user 1000:1000 --memory 2g \
#     --cpus 1 --pids-limit 128 --cap-drop ALL --security-opt no-new-privileges \
#     -v <run_dir>:/work:rw -w /work statlee-runner python script.py
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    r-base \
    r-cran-dplyr \
    r-cran-ggplot2 \
    r-cran-mass \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    pandas==3.0.1 \
    statsmodels==0.14.6 \
    matplotlib==3.10.8 \
    seaborn==0.13.2 \
    openpyxl==3.1.5

# Non-root by default; matches the --user 1000:1000 launch flag.
RUN useradd --uid 1000 --create-home runner
USER 1000:1000
WORKDIR /work
