"""The activity half of the skill: what the stack is doing and whether it is well.

Stage 1 holds ``status``: each app's version, start time and health checks,
the first thing to run when something seems wrong. ``queue``, ``why``,
``wanted``, ``search-now`` and ``indexers`` follow in later stages.
"""

from __future__ import annotations

from .apps import APP_ORDER
from .client import ArrClient, as_rows, fence, selector
from .run import Run, walk_apps

_HEALTH_TYPES = frozenset({"ok", "notice", "warning", "error"})


def _health_item(item: dict) -> dict:
    kind = item.get("type")
    return {
        "type": kind if kind in _HEALTH_TYPES else selector(kind),
        "source": selector(item.get("source")),
        "message": fence(item.get("message")),
    }


def _status(client: ArrClient) -> dict:
    data = client.system_status()
    health = as_rows(client.request("GET", "health"), client.app, "health")
    return {
        "version": selector(data.get("version")),
        "start_time": selector(data.get("startTime")),
        "health": [_health_item(item) for item in health],
    }


def cmd_status(run: Run, args) -> dict:
    """Every configured app, prowlarr included, or the one ``--app`` names."""
    if args.app:
        return walk_apps(run, [args.app], _status, named=True)
    names = [name for name in APP_ORDER if name in run.config.servarr.apps]
    return walk_apps(run, names, _status, named=False)
