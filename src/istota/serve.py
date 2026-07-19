"""Combined single-process launcher for the local install (``istota serve``).

Runs the task-processing scheduler loop and the uvicorn web server in one
process, blocking until Ctrl-C. The scheduler owns the worker pool + every
interval-gated poller (so web-chat ``source_type="web"`` tasks flow through the
normal pool); uvicorn serves the web UI + API on a loopback bind.

The scheduler runs on a worker thread with ``install_signal_handlers=False``
(signal handlers can only be installed on the main thread). uvicorn — running on
the main thread — installs its own SIGINT/SIGTERM handlers; on shutdown the
launcher drains the scheduler after uvicorn returns. A dead scheduler thread
stops the web server (fail-loud, no zombie web with no worker).
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from .config import Config

logger = logging.getLogger("istota.serve")


class ServeError(RuntimeError):
    """A launch-blocking configuration/bootstrap error, reported to the user."""


# ---------------------------------------------------------------------------
# Env file sourcing
# ---------------------------------------------------------------------------


def load_env_file(path: Path) -> int:
    """Read ``KEY=VALUE`` lines from ``path`` into ``os.environ``.

    Best-effort and non-clobbering: a variable already set in the environment
    wins (so an explicit shell export overrides the file). Blank lines and
    ``#`` comments are ignored; values may be quoted. Returns the number of
    variables set. A missing file is a no-op (returns 0).
    """
    if not path.is_file():
        return 0
    count = 0
    try:
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if not key or key in os.environ:
                continue
            os.environ[key] = value
            count += 1
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning("Could not read env file %s: %s", path, exc)
    return count


# ---------------------------------------------------------------------------
# Bootstrap checks
# ---------------------------------------------------------------------------


def bootstrap_checks(config: Config) -> None:
    """Verify the instance is set up, and seed workspace dirs.

    Raises ``ServeError`` with an actionable message when the DB is missing or
    no user row exists (i.e. ``istota setup`` was never run). Ensures the
    configured user's workspace directories exist (idempotent) so a fresh
    install is fully seeded before the pollers run.
    """
    from . import db

    db_path = Path(config.db_path)
    if not db_path.exists():
        raise ServeError(
            f"No database at {db_path}. Run `istota setup` first."
        )

    # A configured user is required — the no-auth web UI and every workspace
    # path key off it. `config.users` is populated from TOML + the
    # user_profiles table by load_config.
    if not config.users:
        raise ServeError(
            "No user is configured. Run `istota setup` first (or "
            "`istota user ensure -u <id>`)."
        )

    # Confirm the DB is readable (a present-but-corrupt file should fail here,
    # not deep inside a worker).
    try:
        with db.get_db(db_path) as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as exc:
        raise ServeError(
            f"Database at {db_path} is present but unreadable: {exc}. "
            "Run `istota setup` to repair, or restore a backup."
        ) from exc

    # Seed workspace directories for every configured user (idempotent).
    from .storage import ensure_user_directories_v2

    for user_id in config.users:
        try:
            ensure_user_directories_v2(config, user_id)
        except Exception as exc:  # noqa: BLE001
            raise ServeError(
                f"Could not create workspace directories for {user_id!r}: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# uvicorn server construction
# ---------------------------------------------------------------------------


def build_uvicorn_server(host: str, port: int):
    """Construct a programmatic uvicorn ``Server`` for the web app.

    Separated so tests can stub it. The app is imported lazily so `serve` never
    pulls in the (optional) web dependency stack until it actually runs.
    """
    import uvicorn

    from .web_app import app as _web_app

    _maybe_mount_webhooks(_web_app)

    uv_config = uvicorn.Config(
        _web_app,
        host=host,
        port=port,
        log_level="info",
        # Keep the launcher's stdout clean — the app logs through istota logging.
        access_log=False,
    )
    return uvicorn.Server(uv_config)


def _maybe_mount_webhooks(web_app) -> None:
    """Mount the GPS webhook receiver as a sub-app when location is enabled.

    Off by default in the lean footprint (``[location] enabled = false``). The
    receiver serves ``/webhooks/location``; mounting it on the same uvicorn
    server keeps the local install a single port. No-op (and never imports the
    webhook module) when location is disabled or already mounted.
    """
    from .web_app import _config as web_config

    if not web_config or not getattr(web_config, "location", None):
        return
    if not web_config.location.enabled:
        return
    # Avoid a double mount on a serve restart in the same process.
    if any(getattr(r, "path", "") == "/webhooks" for r in web_app.routes):
        return
    try:
        from .webhook_receiver import app as webhook_app

        web_app.mount("/webhooks", webhook_app)
        logger.info("Mounted GPS webhook receiver at /webhooks")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not mount webhook receiver: %s", exc)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

_READY_TIMEOUT_SECONDS = 60.0
_SCHEDULER_JOIN_TIMEOUT_SECONDS = 15.0


def run_serve(
    config: Config,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
) -> None:
    """Run the combined scheduler + web server until shutdown.

    Blocks until uvicorn stops (Ctrl-C / SIGTERM), then drains the scheduler.
    Raises ``ServeError`` on a launch-blocking condition (no-auth non-loopback
    bind, missing setup, another instance already running, uvicorn bind
    failure).
    """
    from . import scheduler
    from .web_app import assert_no_auth_bind_safe

    bind_port = port if port is not None else config.web.port

    # Structural guard: never serve no-auth on a non-loopback host.
    try:
        assert_no_auth_bind_safe(config.web.auth, host)
    except RuntimeError as exc:
        raise ServeError(str(exc)) from exc

    bootstrap_checks(config)

    ready_event = threading.Event()
    sched_state: dict = {"error": None}

    def _run_scheduler() -> None:
        try:
            scheduler.run_daemon(
                config, install_signal_handlers=False, ready_event=ready_event,
            )
        except BaseException as exc:  # noqa: BLE001 - propagate to main thread
            sched_state["error"] = exc
            ready_event.set()

    sched_thread = threading.Thread(
        target=_run_scheduler, name="scheduler", daemon=True,
    )
    sched_thread.start()

    if not ready_event.wait(timeout=_READY_TIMEOUT_SECONDS):
        scheduler.request_shutdown()
        sched_thread.join(timeout=_SCHEDULER_JOIN_TIMEOUT_SECONDS)
        raise ServeError(
            "Scheduler did not become ready within "
            f"{_READY_TIMEOUT_SECONDS:.0f}s; aborting."
        )

    # The scheduler thread signalled ready by failing (e.g. flock contention).
    if sched_state["error"] is not None:
        err = sched_state["error"]
        if isinstance(err, scheduler._DaemonAlreadyRunning):
            raise ServeError(str(err)) from err
        raise ServeError(f"Scheduler failed to start: {err}") from err

    server = build_uvicorn_server(host, bind_port)

    # Supervise the scheduler thread: if it dies while the web server runs,
    # stop uvicorn so we don't leave a web server with no worker behind it.
    stop_supervisor = threading.Event()

    def _supervise() -> None:
        while not stop_supervisor.wait(0.5):
            if not sched_thread.is_alive():
                logger.error(
                    "Scheduler thread exited unexpectedly — stopping web server.",
                )
                server.should_exit = True
                return

    supervisor = threading.Thread(
        target=_supervise, name="serve-supervisor", daemon=True,
    )
    supervisor.start()

    logger.info(
        "istota serve: web UI on http://%s:%d/istota (auth=%s)",
        host, bind_port, config.web.auth,
    )
    print(f"istota serve — open http://{host}:{bind_port}/istota  (Ctrl-C to stop)")

    try:
        # uvicorn installs its own SIGINT/SIGTERM handlers and blocks here.
        server.run()
    finally:
        stop_supervisor.set()
        scheduler.request_shutdown()
        sched_thread.join(timeout=_SCHEDULER_JOIN_TIMEOUT_SECONDS)
        logger.info("istota serve: shutdown complete.")

    # If the scheduler thread died mid-run, surface it as a non-zero exit.
    if sched_state["error"] is not None:
        err = sched_state["error"]
        raise ServeError(f"Scheduler thread crashed: {err}") from err
