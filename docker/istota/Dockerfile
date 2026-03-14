FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1
ENV LANG=C.UTF-8

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git sqlite3 \
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

# Install deps (core + commonly used extras, no heavy ML deps)
RUN uv sync --frozen --no-dev \
    --extra calendar --extra email --extra markets --extra transcribe --extra location

# Data directories (persistent via volume)
RUN mkdir -p /data/db /data/config /data/tmp

# Entrypoint
COPY docker/istota/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
