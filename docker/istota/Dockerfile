FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1
ENV LANG=C.UTF-8

# System deps (including weasyprint/tesseract requirements for all extras)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git sqlite3 \
    # Bubblewrap sandbox (per-invocation filesystem isolation)
    bubblewrap \
    # WeasyPrint (accounting/invoicing)
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 libffi-dev libcairo2 \
    # Tesseract OCR (transcribe skill)
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 (for Claude Code)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Claude Code
RUN npm install -g @anthropic-ai/claude-code

# uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Project files
WORKDIR /app
COPY pyproject.toml uv.lock schema.sql README.md ./
COPY src/ src/
COPY config/ config/

# Install all deps (core + all optional extras)
RUN uv sync --frozen --no-dev --extra all

# Data directories (persistent via volume)
RUN mkdir -p /data/db /data/config /data/tmp

# Entrypoint
COPY docker/istota/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
