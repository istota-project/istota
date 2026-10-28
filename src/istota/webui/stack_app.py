"""The stack's web process: the web app with the webhook receiver's routes.

The receiver used to run as its own `webhooks` container from the same image
with the same privileges, so it bought one more Python process and nothing a
separate process isolates. Its routes are included the way `istota serve`
includes them (`serve._maybe_mount_webhooks`): only for the features the config
enables, under their own paths, with the receiver's config load run inside the
web app's lifespan. Each route still refuses on its own when its feature is off.

uvicorn configures only its own loggers, so this module configures `istota.*`
the way the daemon does, on stderr only (ISSUE-872). `istota serve` imports
`webui.app` directly and configures logging in the CLI.
"""

from __future__ import annotations

from istota.config import load_config
from istota.logging_setup import setup_logging
from istota.serve import _maybe_mount_webhooks
from istota.webui.app import app

_config = load_config()
setup_logging(_config, daemon_mode=True, console_only=True)
_maybe_mount_webhooks(app, _config)

__all__ = ["app"]
