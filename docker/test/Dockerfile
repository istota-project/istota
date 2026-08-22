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
# Extras: `all` minus the two heavy ones. `memory-search` pulls torch and
# sentence-transformers and `whisper` pulls faster-whisper, av and onnxruntime
# — together about 750 MB of wheels for an image whose job is mount namespaces.
# Nothing imports either at module scope; every heavy import in src/ is inside a
# function, deliberately, so the suite collects and runs clean without them.
#
# The one test that needs a heavy extra carries the `ml` marker and is
# deselected by both pyproject's addopts and scripts/test-linux.sh. If a future
# test imports torch or faster-whisper at module scope, collection fails loudly
# here rather than quietly passing on a developer host that happens to have
# them; the fix is the marker, not a bigger install.
#
# `test` is that set, defined once in pyproject.toml — it used to be nine
# `--extra` flags spelled out here, because there is no way to subtract an extra
# from `all`, and this file is where such a list drifts. jinja2 and psutil used
# to be in it by proxy (via `docs` and `whisper`); they are test dependencies
# and now live in the `dev` group, which `--group dev` below installs.
RUN uv sync --frozen --no-install-project --group dev --extra test

# ruff is in [dependency-groups] dev as of ISSUE-301 and so already arrives in
# /venv above. Keeping the tool install anyway is deliberate: /root/.local/bin
# is prepended below, so the lint gate in scripts/test-linux.sh resolves this
# copy whatever a future `uv sync` does with the group, and the runner's
# behaviour does not change with a flag somewhere else. It used to be the only
# place ruff was installed at all, which is what left a developer on a fresh
# clone unable to run the check AGENTS.md mandates.
#
# Pinned for the same reason as uv above, and because the lint gate in
# scripts/test-linux.sh is what the whole suite run hangs on: an unpinned ruff
# turns a new rule in a new release into "the Linux runner is broken". This is
# the version the tree is known clean under; bump it deliberately, here and in
# the dev group together — tests/test_lean_install.py pins them equal.
RUN uv tool install ruff==0.16.4
ENV PATH="/root/.local/bin:${PATH}"
