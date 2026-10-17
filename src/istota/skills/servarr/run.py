"""One CLI run: the caller's tier, the run's deadline, a client per app, and
the walk every multi-app verb shares.

A failure for one app is a per-app ``error`` in the result, not a whole-call
failure, so the user still gets the other apps' answers. Running out of time
stops the walk and names the apps not reached, since a CLI the proxy kills
returns nothing at all.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .access import Tier
from .apps import MEDIA_APPS
from .client import ArrClient, ServarrError

#: Kept back from the proxy's own timeout for this skill, so the CLI finishes
#: and prints before it is killed.
DEADLINE_MARGIN_SECONDS = 30
MIN_RUN_SECONDS = 10


def run_deadline(config, clock: Callable[[], float]) -> float:
    """The `clock()` value by which this run must have finished its requests."""
    from istota.sandbox.skill_proxy import resolve_skill_timeout  # heavy; one call a run

    security = config.security
    try:
        ceiling = resolve_skill_timeout(security.skill_proxy_timeout,
                                        security.skill_proxy_timeouts, "servarr",
                                        security.skill_client_wait_seconds)
    except Exception:  # noqa: BLE001 — a malformed setting falls back to the default
        ceiling = 300
    return clock() + max(MIN_RUN_SECONDS, ceiling - DEADLINE_MARGIN_SECONDS)


@dataclass
class Run:
    config: object
    tier: Tier
    user_id: str
    deadline: float
    clock: Callable[[], float]
    sleep: Callable[[float], None] = time.sleep
    #: Wall-clock time, for comparing against the dates the apps report.
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    transport: object = None
    resolve: Callable | None = None
    clients: dict[str, ArrClient] = field(default_factory=dict)

    def client(self, app: str) -> ArrClient:
        """The one client for `app` in this run, so guard 5 holds across verbs."""
        if app in self.clients:
            return self.clients[app]
        connection = self.config.servarr.apps.get(app)
        if connection is None:
            raise ServarrError(
                f"{app} is not configured: there is no [servarr.apps.{app}] table.",
                "app_not_configured", app=app,
            )
        servarr = self.config.servarr
        client = ArrClient(
            app=app, base_url=connection.url, api_key=connection.api_key,
            private_hosts=servarr.private_hosts, transport=self.transport,
            resolve=self.resolve, deadline=self.deadline, clock=self.clock,
            lookup_timeout=servarr.lookup_timeout_seconds,
            release_timeout=servarr.release_search_timeout_seconds,
        )
        self.clients[app] = client
        return client

    def close(self) -> None:
        for client in self.clients.values():
            client.close()


def media_apps(run: Run, app: str | None, verb: str) -> tuple[list[str], bool]:
    """The apps a media verb visits, and whether ``--app`` named one.

    With no ``--app``, every configured app that serves media; prowlarr named
    with ``--app`` is refused, since it holds no library.
    """
    if app:
        if app not in MEDIA_APPS:
            raise ServarrError(f"{app} serves no media, so it has nothing for {verb}; "
                               f"use status for it.", "not_a_media_app", app=app)
        return [app], True
    return [name for name in MEDIA_APPS if name in run.config.servarr.apps], False


def error_entry(app: str, exc: ServarrError) -> dict:
    """One app's failure inside a multi-app result."""
    entry = {"app": app, "error": exc.reason, "message": str(exc)}
    entry.update({k: v for k, v in exc.extra.items() if k != "app"})
    return entry


def walk_apps(run: Run, names: list[str], visit: Callable[[ArrClient], dict], *,
              named: bool) -> dict:
    """Visit each app in order; the result envelope.

    `named` is true when the caller asked for one app with ``--app``: then a
    failure is the call's own and raises, rather than becoming a per-app entry.
    """
    results: list[dict] = []
    not_reached: list[str] = []
    for index, name in enumerate(names):
        try:
            results.append({"app": name, **visit(run.client(name))})
        except ServarrError as exc:
            if named:
                raise
            if exc.reason == "time_budget":
                not_reached = list(names[index:])
                break
            results.append(error_entry(name, exc))
    envelope: dict = {"status": "ok", "apps": results}
    if not_reached:
        envelope["partial"] = True
        envelope["not_reached"] = not_reached
    return envelope
