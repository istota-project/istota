# Layer 1 — the Linux test runner.
#
# The unit suite runs on the developer's macOS host, where `bwrap` does not
# exist and `tests/test_sandbox.py` therefore patches `_bwrap_available` and
# asserts argv. This image is where the sandbox code path actually executes: a
# real kernel, a real bubblewrap, real mount namespaces. `scripts/test-linux.sh`
# is the only supported way to run it — see that script for the capability
# grants it needs and why they must never appear in a deployment compose file.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1
ENV LANG=C.UTF-8

# bubblewrap is the point of the image. The rest mirrors what the runtime image
# installs for the same tests: git and sqlite3 for the developer and skill
# paths, tmux for the tmux brain, curl + ca-certificates for anything that
# resolves a URL.
# procps is not decoration: tests/test_qtest_semaphore.py and the process-group
# tests shell out to `ps` to prove a grandchild died with its group, and a
# slim image has no `ps` at all — the assertion then fails on exit 127 rather
# than on the property it is about.
RUN apt-get update && apt-get install -y --no-install-recommends \
    bubblewrap git sqlite3 curl tmux ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*

# Pinned, not `:latest`. The Python dependencies are pinned by `uv sync
# --frozen` a few lines down, so a floating toolchain is the one thing in this
# file that could make two builds of the same commit disagree.
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

# The venv lives OUTSIDE the source tree, because scripts/test-linux.sh binds
# the host checkout over the working directory read-only. A venv at ./.venv
# would be shadowed by that mount, and `uv run` would then try to rebuild an
# environment inside a read-only directory and fail before collecting a test.
ENV UV_PROJECT_ENVIRONMENT=/venv
ENV UV_CACHE_DIR=/uv-cache
ENV PATH="/venv/bin:${PATH}"
# ruff writes its cache beside the rootdir by default, and the rootdir is the
# read-only bind. Point it at the container's writable /tmp so a cache miss is
# a cache miss rather than a warning on every run.
ENV RUFF_CACHE_DIR=/tmp/ruff-cache

WORKDIR /build
COPY pyproject.toml uv.lock ./
# --no-install-project: `istota` is a hatchling package with no
# `[tool.uv] package = false`, so a plain sync would try to build the root
# project and fail — src/ is not in the build context at this point. The source
# arrives at run time via the bind mount, and pythonpath = ["src"] makes it
# importable.
#
# Extras: everything the developer host has (`uv sync --all-extras`) *except*
# `memory-search`, which pulls torch and sentence-transformers — a couple of GB
# of wheels for an image whose job is mount namespaces. Nothing imports them at
# module scope: `memory/search.py` imports `sqlite_vec` inside a function and
# the tests that reach it mock or skip, so the suite collects and runs clean
# without the extra, and the real library stays covered on the developer host.
#
# The list is spelled out rather than `--extra all` because two of the needed
# packages are not in `all`: `psutil` arrives via `whisper` and `jinja2` via
# `docs`, and six test modules import them at module scope. `all` alone leaves
# the runner reporting six collection errors that have nothing to do with the
# code under test. If a future test imports torch or sentence-transformers at
# module scope, collection fails loudly here and `memory-search` goes back in.
RUN uv sync --frozen --no-install-project --group dev \
    --extra calendar --extra email --extra markets --extra transcribe \
    --extra location --extra web --extra money --extra feeds --extra garmin \
    --extra whisper --extra docs

# ruff is not a project dependency: it appears in pyproject.toml only as
# [tool.ruff] configuration, and [dependency-groups] dev is pytest and its
# plugins. Installing it as a tool here is narrower than adding it to the dev
# group, which would change what every developer's `uv sync` pulls.
#
# Pinned for the same reason as uv above, and because the lint gate in
# scripts/test-linux.sh is what the whole suite run hangs on: an unpinned ruff
# turns a new rule in a new release into "the Linux runner is broken". This is
# the version the tree is known clean under; bump it deliberately.
RUN uv tool install ruff==0.16.4
ENV PATH="/root/.local/bin:${PATH}"
