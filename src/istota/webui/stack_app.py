"""The stack's web process: the web app with the webhook receiver's routes.

The receiver used to run as its own `webhooks` container from the same image
with the same privileges, so it bought one more Python process and nothing a
separate process isolates. Its routes are included the way `istota serve`
includes them (`serve._maybe_mount_webhooks`): only for the features the config
enables, under their own paths, with the receiver's config load run inside the
web app's lifespan. Each route still refuses on its own when its feature is off.
"""

from __future__ import annotations

from istota.config import load_config
from istota.serve import _maybe_mount_webhooks
from istota.webui.app import app

_maybe_mount_webhooks(app, load_config())

__all__ = ["app"]
