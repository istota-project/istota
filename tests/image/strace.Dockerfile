# The shipped istota image plus strace, for tests/image/test_cli_harness_init.py.
# Test-only: strace is never in the shipped image.
ARG BASE
FROM ${BASE}
RUN apt-get update \
    && apt-get install -y --no-install-recommends strace \
    && rm -rf /var/lib/apt/lists/*
