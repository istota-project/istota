"""Stage 2 tests for the local single-user install: the ``istota serve`` launcher.

Covers ``run_daemon(install_signal_handlers=…, ready_event=…)`` +
``request_shutdown()``, ``serve.run_serve`` orchestration (scheduler thread ↔
uvicorn), and the bootstrap / flock / loopback error paths.
"""

import signal
import threading
import time

import pytest

from istota import db
from istota.config import (
    Config,
    SchedulerConfig,
    SecurityConfig,
    TalkConfig,
    UserConfig,
    WebConfig,
)


def _standalone_config(tmp_path, *, init=True, with_user=True):
    workspace = tmp_path / "workspace"
    cfg = Config(
        db_path=tmp_path / "istota.db",
        nextcloud_mount_path=workspace,
        users={"alice": UserConfig(display_name="Alice")} if with_user else {},
        talk=TalkConfig(enabled=False),
        security=SecurityConfig(sandbox_enabled=False),
        web=WebConfig(enabled=True, port=8799, auth="none"),
        # These tests run a real daemon loop; keep the host-pressure breadcrumb
        # off so they never touch the real /proc (see test_background_checks).
        scheduler=SchedulerConfig(host_pressure_enabled=False),
        bot_name="Istota",
    )
    if init:
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        db.init_db(cfg.db_path)
    return cfg


@pytest.fixture(autouse=True)
def _reset_shutdown_flag():
    import istota.scheduler as sched
    sched._shutdown_requested = False
    yield
    sched._shutdown_requested = False


# ---------------------------------------------------------------------------
# request_shutdown + run_daemon params
# ---------------------------------------------------------------------------


class TestRequestShutdown:
    def test_request_shutdown_sets_flag(self):
        import istota.scheduler as sched
        assert sched._shutdown_requested is False
        sched.request_shutdown()
        assert sched._shutdown_requested is True

    def test_run_daemon_no_signal_handlers_sets_ready_event(self, tmp_path, monkeypatch):
        import istota.scheduler as sched

        cfg = _standalone_config(tmp_path)

        # Neutralize external subsystems that spawn real loops / write files.
        monkeypatch.setattr(sched, "DAEMON_LOCK_PATH", tmp_path / "daemon.lock")
        import istota.async_runtime as ar
        monkeypatch.setattr(ar, "get_async_runtime", lambda: None)
        monkeypatch.setattr(sched, "reset_async_runtime", lambda: None)
        import istota.status_writer as sw
        monkeypatch.setattr(sw, "init_status_writer", lambda *a, **k: None)
        monkeypatch.setattr(sw, "write_status", lambda *a, **k: None)

        # Record signal installs — must NOT happen when install_signal_handlers=False.
        installed = []
        monkeypatch.setattr(
            signal, "signal",
            lambda sig, handler: installed.append(sig),
        )

        # Break out of the loop on the first dispatch so we run exactly one pass.
        monkeypatch.setattr(
            sched.WorkerPool, "dispatch", lambda self: sched.request_shutdown(),
        )

        ready = threading.Event()
        t = threading.Thread(
            target=lambda: sched.run_daemon(
                cfg, install_signal_handlers=False, ready_event=ready,
            ),
            daemon=True,
        )
        t.start()
        assert ready.wait(timeout=10.0), "ready_event never set"
        t.join(timeout=10.0)
        assert not t.is_alive(), "daemon did not exit after shutdown"

        assert signal.SIGINT not in installed
        assert signal.SIGTERM not in installed

    def test_run_daemon_flock_contention_raises(self, tmp_path, monkeypatch):
        import fcntl
        import istota.scheduler as sched

        cfg = _standalone_config(tmp_path)
        lock_path = tmp_path / "daemon.lock"
        monkeypatch.setattr(sched, "DAEMON_LOCK_PATH", lock_path)

        # Hold the lock so run_daemon can't acquire it.
        holder = open(lock_path, "w")
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            ready = threading.Event()
            with pytest.raises(sched._DaemonAlreadyRunning):
                sched.run_daemon(cfg, install_signal_handlers=False, ready_event=ready)
            # ready_event still set so a waiter isn't blocked forever.
            assert ready.is_set()
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()


# ---------------------------------------------------------------------------
# serve.load_env_file / bootstrap_checks
# ---------------------------------------------------------------------------


class TestEnvFile:
    def test_load_env_file_sets_missing_vars(self, tmp_path, monkeypatch):
        from istota import serve

        env = tmp_path / "istota.env"
        env.write_text(
            "# comment\n"
            "ISTOTA_TEST_A=alpha\n"
            'export ISTOTA_TEST_B="beta"\n'
            "\n"
            "ISTOTA_TEST_C='gamma'\n"
        )
        monkeypatch.delenv("ISTOTA_TEST_A", raising=False)
        monkeypatch.delenv("ISTOTA_TEST_B", raising=False)
        monkeypatch.delenv("ISTOTA_TEST_C", raising=False)
        n = serve.load_env_file(env)
        assert n == 3
        import os
        assert os.environ["ISTOTA_TEST_A"] == "alpha"
        assert os.environ["ISTOTA_TEST_B"] == "beta"
        assert os.environ["ISTOTA_TEST_C"] == "gamma"

    def test_load_env_file_does_not_clobber(self, tmp_path, monkeypatch):
        from istota import serve
        env = tmp_path / "istota.env"
        env.write_text("ISTOTA_TEST_X=fromfile\n")
        monkeypatch.setenv("ISTOTA_TEST_X", "fromenv")
        serve.load_env_file(env)
        import os
        assert os.environ["ISTOTA_TEST_X"] == "fromenv"

    def test_load_env_file_missing_is_noop(self, tmp_path):
        from istota import serve
        assert serve.load_env_file(tmp_path / "nope.env") == 0


class TestBootstrapChecks:
    def test_missing_db_raises_setup_hint(self, tmp_path):
        from istota import serve
        cfg = _standalone_config(tmp_path, init=False)
        with pytest.raises(serve.ServeError, match="setup"):
            serve.bootstrap_checks(cfg)

    def test_no_user_raises(self, tmp_path):
        from istota import serve
        cfg = _standalone_config(tmp_path, init=True, with_user=False)
        with pytest.raises(serve.ServeError, match="user"):
            serve.bootstrap_checks(cfg)

    def test_ok_seeds_workspace_dirs(self, tmp_path):
        from istota import serve
        cfg = _standalone_config(tmp_path)
        serve.bootstrap_checks(cfg)
        base = cfg.nextcloud_mount_path / "Users" / "alice"
        assert base.is_dir()


# ---------------------------------------------------------------------------
# serve.run_serve orchestration
# ---------------------------------------------------------------------------


class _FakeTransport:
    def __init__(self):
        self.aborted = 0

    def abort(self):
        self.aborted += 1


class _FakeConnection:
    def __init__(self):
        self.transport = _FakeTransport()


class _FakeServerState:
    def __init__(self, connections=()):
        self.connections = set(connections)


class _FakeServer:
    """Stands in for ``uvicorn.Server``, including its exit-signal contract.

    ``handle_exit`` is modelled because ``serve.install_force_quit`` wraps it;
    a double that lacked it would let the wiring be removed silently.
    """

    def __init__(self, connections=()):
        self.should_exit = False
        self.force_exit = False
        self.ran = False
        self.server_state = _FakeServerState(connections)

    def handle_exit(self, sig, frame):
        if self.should_exit and sig == signal.SIGINT:
            self.force_exit = True
        else:
            self.should_exit = True

    def run(self):
        self.ran = True
        # Simulate serving briefly, then return (as uvicorn does on SIGINT).
        for _ in range(3):
            if self.should_exit:
                break
            time.sleep(0.01)


class TestRunServe:
    def test_loopback_guard_refuses_non_loopback(self, tmp_path):
        from istota import serve
        cfg = _standalone_config(tmp_path)
        with pytest.raises(serve.ServeError, match="loopback"):
            serve.run_serve(cfg, host="0.0.0.0", port=8799)

    def test_orchestration_starts_and_stops(self, tmp_path, monkeypatch):
        import istota.scheduler as sched
        from istota import serve

        cfg = _standalone_config(tmp_path)

        daemon_started = threading.Event()

        def fake_daemon(config, *, install_signal_handlers=True, ready_event=None):
            daemon_started.set()
            if ready_event is not None:
                ready_event.set()
            while not sched._shutdown_requested:
                time.sleep(0.01)

        monkeypatch.setattr(sched, "run_daemon", fake_daemon)
        fake_server = _FakeServer()
        monkeypatch.setattr(serve, "build_uvicorn_server", lambda host, port: fake_server)

        serve.run_serve(cfg, host="127.0.0.1", port=8799)

        assert fake_server.ran
        assert daemon_started.is_set()
        # run_serve requested shutdown on exit, so the fake daemon loop ended.
        assert sched._shutdown_requested is True

    def test_flock_already_running_surfaces_serve_error(self, tmp_path, monkeypatch):
        import fcntl
        import istota.scheduler as sched
        from istota import serve

        cfg = _standalone_config(tmp_path)
        lock_path = tmp_path / "daemon.lock"
        monkeypatch.setattr(sched, "DAEMON_LOCK_PATH", lock_path)

        # build_uvicorn_server must never be reached — fail loudly if it is.
        monkeypatch.setattr(
            serve, "build_uvicorn_server",
            lambda host, port: (_ for _ in ()).throw(AssertionError("should not build server")),
        )

        holder = open(lock_path, "w")
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with pytest.raises(serve.ServeError, match="already running"):
                serve.run_serve(cfg, host="127.0.0.1", port=8799)
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()

    def test_serve_before_setup_errors(self, tmp_path):
        from istota import serve
        cfg = _standalone_config(tmp_path, init=False)
        with pytest.raises(serve.ServeError, match="setup"):
            serve.run_serve(cfg, host="127.0.0.1", port=8799)


# ---------------------------------------------------------------------------
# Shutting down with a long-lived connection open (SSE)
# ---------------------------------------------------------------------------


class TestGracefulShutdownBound:
    def test_uvicorn_config_bounds_the_graceful_wait(self, monkeypatch):
        """uvicorn's default waits for every open connection forever, and the
        web app's SSE generators poll until the client goes away — so one open
        browser tab would wedge Ctrl-C."""
        pytest.importorskip("uvicorn")
        pytest.importorskip("fastapi")
        from istota import serve

        monkeypatch.setattr(serve, "_maybe_mount_webhooks", lambda app: None)
        server = serve.build_uvicorn_server("127.0.0.1", 8799)

        timeout = server.config.timeout_graceful_shutdown
        assert timeout is not None, "an unbounded graceful shutdown hangs on SSE"
        assert 0 < timeout <= 30


class TestForceQuit:
    def test_first_interrupt_leaves_connections_alone(self):
        from istota import serve

        conn = _FakeConnection()
        server = _FakeServer([conn])
        serve.install_force_quit(server)

        server.handle_exit(signal.SIGINT, None)

        assert server.should_exit is True
        assert server.force_exit is False
        # A graceful shutdown must still let an in-flight response finish.
        assert conn.transport.aborted == 0

    def test_repeat_interrupt_aborts_open_connections(self):
        """The second Ctrl-C sets uvicorn's ``force_exit``, which breaks its own
        wait loops — and then it blocks in ``asyncio.Server.wait_closed()``,
        which since Python 3.12 waits for the same connections. Aborting the
        transports is what lets that return."""
        from istota import serve

        conn = _FakeConnection()
        server = _FakeServer([conn])
        serve.install_force_quit(server)

        server.handle_exit(signal.SIGINT, None)
        server.handle_exit(signal.SIGINT, None)

        assert server.force_exit is True
        assert conn.transport.aborted == 1

    def test_abort_survives_a_dead_transport(self):
        from istota import serve

        good = _FakeConnection()
        broken = _FakeConnection()

        def boom():
            raise RuntimeError("transport already gone")

        broken.transport.abort = boom
        server = _FakeServer([broken, good])

        # One bad transport must not strand the rest, on the path whose whole
        # job is ending a process that is already refusing to end.
        assert serve.abort_open_connections(server) == 1
        assert good.transport.aborted == 1

    def test_abort_defers_onto_a_running_loop(self):
        """Inside a live server the handler runs on the loop's own thread; the
        abort is queued as ordinary loop work rather than run mid-callback."""
        import asyncio

        from istota import serve

        conn = _FakeConnection()
        server = _FakeServer([conn])
        serve.install_force_quit(server)

        async def _drive():
            server.handle_exit(signal.SIGINT, None)
            server.handle_exit(signal.SIGINT, None)
            # Not yet: the handler scheduled it rather than calling it.
            assert conn.transport.aborted == 0
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert conn.transport.aborted == 1

        asyncio.run(_drive())
