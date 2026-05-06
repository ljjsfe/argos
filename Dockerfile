# KDD Cup 2026 Data Agents — submission image.
#
# Single-stage slim build pinned to linux/amd64 (eval requirement).
# Image footprint goal: < 1.5 GB compressed (well under 10 GB cap).
#
# Build:
#   docker buildx build --platform=linux/amd64 -t <team_id>:v<N> --load .
# Save:
#   docker save <team_id>:v<N> | gzip > <team_id>_v<N>.tar.gz
#
# Runtime injected by evaluator:
#   /input  (ro)  — all task_<id>/ directories
#   /output (rw)  — write task_<id>/prediction.csv
#   /logs   (rw)  — runtime.log written via tee
#   env: MODEL_API_URL / MODEL_API_KEY / MODEL_NAME

FROM --platform=linux/amd64 python:3.11-slim

# System deps:
# - tesseract-ocr + libtesseract-dev: pytesseract OCR backend (Phase 2 ready)
# - poppler-utils: pdftotext / pdfimages (used by pdfplumber on some PDFs)
# - libgomp1: openblas runtime needed by pyarrow / pandas wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        libtesseract-dev \
        poppler-utils \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps in one layer (cache-friendly for code-only rebuilds)
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Application code
COPY config.yaml main.py submit_main.py ./
COPY dataline ./dataline

# Pre-create runtime mount points so the container starts cleanly
# even if the evaluator forgets one of them.
RUN mkdir -p /input /output /logs

# Defensive: prevent Python from buffering stdout/stderr (so /logs
# captures everything in real time).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Entry point: tee stdout+stderr into /logs/runtime.log per spec §3.7.
# Using bash -c so the pipe is owned by the container shell.
ENTRYPOINT ["/bin/bash", "-c", "python /app/submit_main.py 2>&1 | tee /logs/runtime.log"]
