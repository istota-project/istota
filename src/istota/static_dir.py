"""Where the built SvelteKit frontend is on disk.

A stdlib-only leaf for the same reason as :mod:`istota.forge_bin`: two callers
with very different budgets need one answer. ``web_app`` resolves this at import
and serves from it; ``doctor``'s ``web.static`` check needs the same path to say
whether the build exists.

Reaching it through ``web_app`` is not free. That module is ~8200 lines with
FastAPI, authlib, starlette and httpx imported at module level, and its
module-level ``_resolve_session_secret()`` calls ``load_config()`` — so a
diagnostic that imported it added ~56 MB RSS to the scheduler process,
permanently, and triggered a second full config load (including the DB overlay
reads) inside a check. On a deployment where host memory pressure is an active
concern, a check is not the place to spend that.

Named ``static_dir`` rather than ``web_static`` on purpose: the packaged build
tree is ``istota/web_static/``, and a module of that name beside it would
shadow the package data directory.
"""

from __future__ import annotations

import os
from pathlib import Path


def pick_static_dir(env_dir: str, repo_build: Path, packaged: Path) -> Path:
    """Pick the static dir from candidates (pure — unit-testable).

    Precedence: env override > repo-relative build > packaged. Falls back to
    the repo-relative path when neither build exists, preserving the existing
    "missing build" behaviour (the ``StaticFiles`` mount is guarded on
    ``.is_dir()``).
    """
    if env_dir.strip():
        return Path(env_dir.strip())
    if repo_build.is_dir():
        return repo_build
    if packaged.is_dir():
        return packaged
    return repo_build


def resolve_static_dir() -> Path:
    """The static dir for this install.

    Precedence:
      1. ``ISTOTA_WEB_STATIC_DIR`` env override (Docker runtime / explicit).
      2. Repo-relative ``web/build`` (editable installs from the repo root).
      3. Packaged static tree at ``istota/web_static`` (non-editable wheel
         installs — the release build copies ``web/build`` there; see the
         pyproject packaging config).
    """
    here = Path(__file__).resolve()
    return pick_static_dir(
        os.environ.get("ISTOTA_WEB_STATIC_DIR", ""),
        here.parent.parent.parent / "web" / "build",
        here.parent / "web_static",
    )
