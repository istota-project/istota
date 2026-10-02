"""Configured public origin, shared by web redirects and operator links."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from istota.config import Config


def external_origin(config: Config | None) -> tuple[str, str]:
    """Return hostname and scheme without trusting request headers."""
    if not config or not config.site.hostname:
        raise ValueError("site.hostname must be configured when web app is enabled")
    host = config.site.hostname
    bare = host.split(":")[0]
    scheme = "http" if bare in ("localhost", "127.0.0.1", "::1") else "https"
    return host, scheme
