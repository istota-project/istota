"""Session signing key resolution shared by the web process and doctor."""

import logging
import secrets
from collections.abc import Mapping
from typing import TYPE_CHECKING

from istota.config import normalize_auth_methods

if TYPE_CHECKING:
    from istota.config import Config

logger = logging.getLogger("istota.webui.session_secret")
ALLOW_INSECURE_SESSION_ENV = "ISTOTA_WEB_ALLOW_INSECURE_SESSION"


def resolve(config: "Config | None", env: Mapping[str, str]) -> str | None:
    """Return a signing key, or None when authenticated startup must refuse."""
    env_secret = env.get("ISTOTA_WEB_SESSION_SECRET_KEY", "").strip()
    if env_secret:
        return env_secret
    if config is not None:
        config_secret = (config.web.session_secret_key or "").strip()
        if config_secret:
            return config_secret
        if "none" in normalize_auth_methods(getattr(config.web, "auth", "nextcloud")):
            return secrets.token_hex(32)
    if env.get(ALLOW_INSECURE_SESSION_ENV, "").strip().lower() in ("1", "true", "yes"):
        logger.warning(
            "No web session secret configured; signing with a random per-process "
            "key because %s is set. Sessions will not survive a restart. Do not "
            "use this in production.", ALLOW_INSECURE_SESSION_ENV,
        )
        return secrets.token_hex(32)
    return None
