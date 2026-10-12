"""Where the built SvelteKit frontend is on disk.

A stdlib-only leaf for the same reason as :mod:`istota.sandbox.forge_bin`: two callers
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

The cache policy and the ``StaticFiles`` mount live here too, because two apps
serve the bundle: ``web_app`` and the first-run setup server, which must run
with no config and so cannot import ``web_app``. Starlette is imported inside
:func:`cache_header_statics` only, so ``doctor`` still pays for none of it.

Named ``static_dir`` rather than ``web_static`` on purpose: the packaged build
tree is ``istota/web_static/``, and a module of that name beside it would
shadow the package data directory.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


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
        here.parent.parent.parent.parent / "web" / "build",
        here.parent.parent / "web_static",
    )


# SvelteKit emits two classes of asset and they need opposite caching. Bare
# `StaticFiles` sends neither header, leaving both to heuristic freshness — and
# an iOS home-screen PWA takes that as licence to pin the app shell more or less
# forever. That is how a device ends up running a months-old bundle against a
# current API: the shell it cached still names hashed chunks the server has
# since deleted, so nothing 404s loudly, it just never picks up new code.
def static_cache_control(path: str) -> str:
    """The `Cache-Control` for a built asset, by SvelteKit's own two classes."""
    # `_app/immutable/*` is content-addressed — the hash changes when the bytes
    # do, so it can be cached indefinitely and never revalidated.
    if "/_app/immutable/" in f"/{path.lstrip('/')}":
        return "public, max-age=31536000, immutable"
    # Everything else is a stable name with changing content: the HTML shell
    # (which names the hashed chunks), version.json, the manifest, icons.
    # `no-cache` still allows caching — it requires revalidation, which the
    # ETag answers with a 304 in the common case.
    return "no-cache"


def relative_location(location: str) -> str | None:
    """A path-relative rewrite of an absolute `Location`, or None to leave it.

    `StaticFiles(html=True)` answers a directory URL with a redirect that adds
    the trailing slash, and it builds that Location as
    `{scope['scheme']}://{Host header}{path}/` — an absolute URL whose authority
    is entirely whatever the proxy in front of us chose to forward. Both halves
    have been wrong: nginx forwarding `$host` drops the port (a stack published
    on :8282 redirects to `http://localhost/istota/chat/`, which is nothing),
    and `scope['scheme']` is always `http` because no deployment starts uvicorn
    with `--proxy-headers`, so a TLS deployment answers with a plaintext URL and
    recovers only via the port-80 redirect back to https.

    A relative reference has no authority to get wrong: the client resolves it
    against the URL it actually asked for. RFC 7231 §7.1.2 permits it and has
    since 2014. The nginx configs forward `$http_host` as well — this is the
    half that does not depend on the proxy being configured correctly.

    Returns None when there is nothing to do (already relative) or when
    rewriting would change meaning: a path starting with `//` is a
    protocol-relative reference, so its first segment would be read as a
    hostname.
    """
    parsed = urlsplit(location)
    if not parsed.scheme and not parsed.netloc:
        return None
    if parsed.path.startswith("//"):
        return None
    return urlunsplit(("", "", parsed.path or "/", parsed.query, parsed.fragment))


@functools.cache
def _cache_header_statics_class():
    from starlette.staticfiles import StaticFiles

    class CacheHeaderStatics(StaticFiles):
        """`StaticFiles` that stamps the cache policy above onto every response."""

        def file_response(self, full_path, stat_result, scope, status_code=200):
            response = super().file_response(full_path, stat_result, scope, status_code)
            response.headers["Cache-Control"] = static_cache_control(scope.get("path", ""))
            return response

        async def get_response(self, path, scope):
            response = await super().get_response(path, scope)
            location = response.headers.get("location")
            if location:
                relative = relative_location(location)
                if relative is not None:
                    response.headers["location"] = relative
            return response

    return CacheHeaderStatics


def cache_header_statics(directory: Path):
    """The ASGI app that serves the built bundle, for a mount at ``/istota``."""
    return _cache_header_statics_class()(directory=str(directory), html=True)
