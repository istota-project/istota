"""Who starts the Baileys bridge, and who is told when the device is unlinked.

Two jobs the bridge deliberately does not do. It never opens a database and
never decides its own lifecycle, so the loop it binds to and the alert its
permanent fatal raises both belong to an owner — and both were named by
Stage 5's deferred list as things Stage 6 owed.

The loop is the one with teeth. Every WhatsApp send reaches `deliver_whatsapp`
through `run_coro`, which runs it on the process-global `AsyncRuntime` loop,
and the bridge's asyncio primitives bind to whichever loop first awaits them.
A bridge started anywhere else is awaited across loops from inside the ledger's
claim-to-settle region.
"""

from __future__ import annotations

import asyncio

import pytest

from istota import db
from istota.config import Config, UserConfig
from istota.transport.whatsapp import baileys_bridge
from istota.transport.whatsapp import baileys_runtime

from .support.whatsapp_config import build_whatsapp_config

USER = "alice"


def _config(tmp_path, *, enabled=True, provider="baileys", **baileys_fields) -> Config:
    path = tmp_path / "istota.db"
    db.init_db(path)
    return Config(
        db_path=path,
        temp_dir=tmp_path / "tmp",
        users={USER: UserConfig()},
        whatsapp=build_whatsapp_config(
            enabled=enabled,
            provider=provider,
            business_phone_number="+15551230000",
            **baileys_fields,
        ),
    )


@pytest.fixture(autouse=True)
def _no_leaked_bridge():
    yield
    baileys_bridge.clear_active_bridge()


class TestWhetherABridgeIsWanted:
    def test_it_is_wanted_when_the_surface_runs_on_a_paired_session(self, tmp_path):
        assert baileys_runtime.baileys_bridge_wanted(_config(tmp_path)) is True

    def test_a_cloud_deployment_wants_none(self, tmp_path):
        cfg = _config(tmp_path, provider="whatsapp_cloud")

        assert baileys_runtime.baileys_bridge_wanted(cfg) is False

    def test_a_disabled_surface_wants_none(self, tmp_path):
        cfg = _config(tmp_path, enabled=False)

        assert baileys_runtime.baileys_bridge_wanted(cfg) is False

    def test_an_unpaired_deployment_still_wants_one(self, tmp_path):
        """The listener is what a sidecar dials into, and pairing is what
        creates the credential — so gating the bridge on the session existing
        would mean a deployment could never reach the state that ungates it.
        The refusal for an unpaired session belongs at the send."""
        cfg = _config(tmp_path)
        assert not (cfg.db_path.parent / "whatsapp-baileys-session").exists()

        assert baileys_runtime.baileys_bridge_wanted(cfg) is True


class TestResolvingTheSidecarCommand:
    def test_a_configured_command_is_split_by_shell_rules_and_not_run_by_one(
        self, tmp_path,
    ):
        cfg = _config(
            tmp_path, sidecar_command='/usr/bin/node "/opt/my sidecar/index.js"',
        )

        assert baileys_bridge.resolve_sidecar_argv(cfg) == (
            "/usr/bin/node", "/opt/my sidecar/index.js",
        )

    def test_an_unparseable_command_spawns_nothing_rather_than_raising(
        self, tmp_path,
    ):
        """The caller is a boot path. A raise there stops the daemon over a
        typo in an optional setting."""
        cfg = _config(tmp_path, sidecar_command='node "unclosed')

        assert baileys_bridge.resolve_sidecar_argv(cfg) == ()

    def test_the_shipped_program_is_found_in_this_checkout(self, monkeypatch):
        monkeypatch.setattr(
            "shutil.which", lambda name: "/usr/bin/node" if name == "node" else None,
        )

        argv = baileys_bridge.in_tree_sidecar_argv()

        assert argv[0] == "/usr/bin/node"
        assert argv[1].endswith("docker/whatsapp-baileys/index.js")

    def test_the_daemon_never_takes_the_in_tree_program(self, tmp_path,
                                                        monkeypatch):
        """The fallback would fire on exactly the canonical deployment: the
        Ansible shape installs from a checkout, so the program is present and
        `node` is usually on PATH — and the daemon would then spawn a second
        sidecar beside the systemd unit's, against one session directory.

        Driven with the program genuinely present in this tree, which is what
        makes the assertion about the *decision* rather than about the file
        being absent.
        """
        monkeypatch.setattr(
            "shutil.which", lambda name: "/usr/bin/node" if name == "node" else None,
        )
        assert baileys_bridge.in_tree_sidecar_argv() != ()

        assert baileys_bridge.resolve_sidecar_argv(_config(tmp_path)) == ()

    def test_without_node_the_in_tree_fallback_resolves_nothing(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr("shutil.which", lambda name: None)

        assert baileys_bridge.in_tree_sidecar_argv() == ()


class TestStartingTheBridge:
    def test_a_deployment_that_wants_none_starts_none(self, tmp_path):
        cfg = _config(tmp_path, provider="whatsapp_cloud")

        assert baileys_runtime.start_baileys_bridge(cfg) is False
        assert baileys_bridge.active_bridge() is None

    def test_it_publishes_the_bridge_only_after_start_returned(
        self, tmp_path, monkeypatch,
    ):
        """The ordering the adapter's refusal rests on. A send resolved through
        a bridge whose listener is not up yet would be written to nothing,
        where the adapter's answer with no bridge published is a definite
        local failure and a clean ledger row."""
        seen = []

        class _Bridge:
            def __init__(self, config, **kwargs):
                self.kwargs = kwargs

            async def start(self):
                seen.append(("start", baileys_bridge.active_bridge()))

            async def stop(self):
                seen.append(("stop", None))

        monkeypatch.setattr(baileys_bridge, "BaileysBridge", _Bridge)
        monkeypatch.setattr(
            baileys_bridge, "resolve_sidecar_argv", lambda config: (),
        )
        monkeypatch.setattr(
            "istota.async_runtime.run_coro", lambda coro, **kw: asyncio.run(coro),
        )
        hooks = []
        monkeypatch.setattr(
            "istota.async_runtime.get_async_runtime",
            lambda: type("_RT", (), {"add_cleanup_hook": staticmethod(hooks.append)})(),
        )

        assert baileys_runtime.start_baileys_bridge(_config(tmp_path)) is True

        assert seen == [("start", None)]
        assert baileys_bridge.active_bridge() is not None
        assert hooks, "no cleanup hook was registered"

    def test_a_start_that_fails_publishes_nothing_and_does_not_raise(
        self, tmp_path, monkeypatch,
    ):
        """A scheduler whose WhatsApp bridge would not start still has to run,
        so the failure is loud and the boot carries on."""
        class _Bridge:
            def __init__(self, config, **kwargs):
                pass

            async def start(self):
                raise OSError("address already in use")

        monkeypatch.setattr(baileys_bridge, "BaileysBridge", _Bridge)
        monkeypatch.setattr(
            baileys_bridge, "resolve_sidecar_argv", lambda config: (),
        )
        monkeypatch.setattr(
            "istota.async_runtime.run_coro", lambda coro, **kw: asyncio.run(coro),
        )

        assert baileys_runtime.start_baileys_bridge(_config(tmp_path)) is False
        assert baileys_bridge.active_bridge() is None

    def test_the_cleanup_hook_unpublishes_before_it_stops(self, tmp_path):
        """In that order. Once the bridge is stopping, a send resolved through
        it sits on a socket being closed and times out into `unknown`; with
        nothing published the adapter answers a definite `failed`."""
        order = []

        class _Bridge:
            async def stop(self):
                order.append(("stop", baileys_bridge.active_bridge()))

        bridge = _Bridge()
        baileys_bridge.set_active_bridge(bridge)

        asyncio.run(baileys_runtime._stop(bridge))

        assert order == [("stop", None)]
        assert baileys_bridge.active_bridge() is None


class TestTheUnlinkAlert:
    def test_the_first_permanent_fatal_calls_the_owner_back(self, tmp_path):
        seen = []
        bridge = baileys_bridge.BaileysBridge(
            _config(tmp_path), on_fatal=seen.append,
        )

        bridge._handle_fatal({"reason": "logged_out"})

        assert seen == ["logged_out"]

    def test_a_second_fatal_in_the_same_outage_is_not_announced_again(
        self, tmp_path,
    ):
        """A logged-out sidecar reconnects and reports the same fatal, because
        the session on disk is still the dead one. One durable row and one push
        per outage, not one per attempt."""
        seen = []
        bridge = baileys_bridge.BaileysBridge(
            _config(tmp_path), on_fatal=seen.append,
        )

        bridge._handle_fatal({"reason": "logged_out"})
        bridge._handle_fatal({"reason": "logged_out"})
        bridge._handle_fatal({"reason": "unpaired"})

        assert seen == ["logged_out"]

    def test_a_successful_re_pair_re_arms_it(self, tmp_path):
        """`ready` clears the latch, so the next unlink after a repair is the
        transition an operator does want to hear about."""
        seen = []
        bridge = baileys_bridge.BaileysBridge(
            _config(tmp_path), on_fatal=seen.append,
        )

        bridge._handle_fatal({"reason": "logged_out"})
        bridge._dispatch("ready", {})
        bridge._handle_fatal({"reason": "logged_out"})

        assert seen == ["logged_out", "logged_out"]

    def test_a_transient_fatal_announces_nothing(self, tmp_path):
        """`_send`'s gate is `fatal_is_permanent` alone, so a transient fatal
        refuses no send — alerting on one would put an operator's real outage
        among the usual noise."""
        seen = []
        bridge = baileys_bridge.BaileysBridge(
            _config(tmp_path), on_fatal=seen.append,
        )

        bridge._handle_fatal({"reason": "stream_error"})

        assert seen == []

    def test_a_callback_that_raises_does_not_reach_the_read_loop(self, tmp_path):
        def _explode(reason):
            raise RuntimeError("the database was locked")

        bridge = baileys_bridge.BaileysBridge(_config(tmp_path), on_fatal=_explode)

        bridge._handle_fatal({"reason": "logged_out"})

        assert bridge.status.fatal_is_permanent is True

    def test_the_rows_go_to_the_admins_and_carry_no_sidecar_prose(self, tmp_path):
        config = _config(tmp_path)
        config.users["bob"] = UserConfig()

        asyncio.run(baileys_runtime._announce_unlink(
            config, "logged_out: stream errored out (conflict 401) for +15551234567",
        ))

        with db.get_db(config.db_path) as conn:
            rows = conn.execute(
                "SELECT user_id, title, body, dedup_key FROM notifications "
                "ORDER BY user_id",
            ).fetchall()

        assert [row["user_id"] for row in rows] == ["alice", "bob"]
        for row in rows:
            assert row["dedup_key"] == "whatsapp:baileys-unlinked"
            assert "istota whatsapp pair" in row["body"]
            # The reason is a bounded label, never the sidecar's own words: a
            # Baileys error string is one of the places a number turns up, and
            # this body renders on the notification panel.
            assert "15551234567" not in row["body"]
            assert "conflict" not in row["body"]

    def test_a_second_announcement_bumps_rather_than_duplicating(self, tmp_path):
        config = _config(tmp_path)

        asyncio.run(baileys_runtime._announce_unlink(config, "logged_out"))
        asyncio.run(baileys_runtime._announce_unlink(config, "logged_out"))

        with db.get_db(config.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM notifications",
            ).fetchone()["n"]

        assert count == 1

    def test_announcing_never_raises(self, tmp_path, monkeypatch):
        """It runs as a task off the bridge's read loop, where an escape is a
        `RuntimeWarning` nobody sees."""
        config = _config(tmp_path)
        config.db_path = tmp_path / "nonexistent" / "istota.db"

        asyncio.run(baileys_runtime._announce_unlink(config, "logged_out"))
