"""`run_daemon`'s side of signaling: the two refusals, and one driver.

Stage 2 built `require_websockets` and `require_hpb` and tested them in
isolation, wired to nothing — so a deployment with `[talk.signaling] enabled =
true` and no library booted normally, polled, and reported healthy, with only
`doctor` saying otherwise. These tests are what say they are now called.

The other half is that `_talk_poll_loop` is **not started** when signaling is
enabled. Two drivers at different cadences would double every fetch and race
`inbound.py`'s module globals across their awaits; the poller is the capability
floor for a deployment with no high-performance backend, not a parallel path.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from istota.config import (
    Config,
    NextcloudConfig,
    SchedulerConfig,
    TalkConfig,
    TalkSignalingConfig,
)
from istota.scheduler import _start_talk_signaling
from istota.transport.talk import signaling as sig


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "test.db"
    cfg.talk = TalkConfig(
        enabled=True, bot_username="istota",
        signaling=TalkSignalingConfig(enabled=True),
    )
    cfg.nextcloud = NextcloudConfig(
        url="https://nc.test", username="istota", app_password="pass",
    )
    cfg.scheduler = SchedulerConfig()
    return cfg


_EXTERNAL = {
    "server": "https://hpb.test/standalone-signaling",
    "signalingMode": "external",
    "helloAuthParams": {"2.0": {"token": "jwt"}},
}


def _patched(payload=None, *, raises=None):
    """Patch the three seams `_start_talk_signaling` reaches through."""
    client = MagicMock()
    client.get_signaling_settings = AsyncMock(
        side_effect=raises, return_value=payload,
    )
    return (
        patch("istota.async_runtime.get_talk_client", return_value=client),
        patch("istota.async_runtime.run_coro",
              side_effect=lambda coro, **kw: _drain(coro, raises, payload)),
        # Closes what it is handed: the real `spawn_task` schedules the
        # coroutine, and a mock that merely records it leaves an un-awaited
        # coroutine for the garbage collector to complain about.
        patch("istota.async_runtime.spawn_task", side_effect=_close),
    )


def _close(coro, **_kwargs):
    coro.close()
    return None


def _drain(coro, raises, payload):
    """Close the coroutine we are standing in for, then answer as Talk would."""
    coro.close()
    if raises is not None:
        raise raises
    return payload


class TestTheStartupRefusals:
    def test_a_missing_websockets_library_refuses_to_boot(self, config):
        # The Nextcloud seams are patched even though this refusal fires before
        # any of them: without that, a regression here would fall through to a
        # real settings call and fail on DNS rather than on the assertion, and
        # a test that can open a socket is one that lies about what it runs
        # against.
        nc, run, spawn = _patched(_EXTERNAL)
        with patch.object(
            sig, "require_websockets",
            side_effect=sig.SignalingUnavailable("the websockets library is not installed"),
        ), nc, run, spawn as spawned:
            with pytest.raises(sig.SignalingUnavailable, match="websockets"):
                _start_talk_signaling(config)
        assert spawned.call_count == 0

    def test_talk_in_internal_mode_refuses_to_boot(self, config):
        internal = {"server": "", "signalingMode": "internal", "helloAuthParams": {}}
        nc, run, spawn = _patched(internal)
        with patch.object(sig, "require_websockets", return_value=object()), \
                nc, run, spawn as spawned:
            with pytest.raises(sig.SignalingUnavailable, match="internal signaling mode"):
                _start_talk_signaling(config)
        assert spawned.call_count == 0

    def test_an_external_deployment_starts_the_supervisor(self, config):
        nc, run, spawn = _patched(_EXTERNAL)
        with patch.object(sig, "require_websockets", return_value=object()), \
                nc, run, spawn as spawned:
            assert _start_talk_signaling(config) is True
        assert spawned.call_count == 1
        assert spawned.call_args.kwargs["name"] == "talk-signaling-supervisor"

    def test_an_unreadable_settings_payload_starts_rather_than_refusing(
        self, config, caplog,
    ):
        """`require_hpb` refuses on three states; only two are settled facts.

        `parse_settings` is total, so a 200 carrying a proxy error page, an OCS
        envelope shaped differently, or an upstream field rename all yield an
        empty `signalingMode` — and refusing there is the same single point of
        failure as refusing on an unreachable Nextcloud, which this gate
        deliberately does not do.
        """
        nc, run, spawn = _patched({"nothing": "recognisable"})
        with patch.object(sig, "require_websockets", return_value=object()), \
                nc, run, spawn as spawned:
            with caplog.at_level("ERROR"):
                assert _start_talk_signaling(config) is True
        assert spawned.call_count == 1
        assert "nothing this client could read" in caplog.text

    def test_an_unreachable_nextcloud_starts_rather_than_refusing(
        self, config, caplog,
    ):
        """A blip is not a misconfiguration.

        Refusing here would make one slow service take down a daemon that also
        runs cron jobs, briefings, email and the web scheduler — a single point
        of failure the poll path never had. The watchers retry on their own
        backoff and the reconciliation pass carries inbound meanwhile.
        """
        nc, run, spawn = _patched(raises=RuntimeError("connection refused"))
        with patch.object(sig, "require_websockets", return_value=object()), \
                nc, run, spawn as spawned:
            with caplog.at_level("ERROR"):
                assert _start_talk_signaling(config) is True
        assert spawned.call_count == 1
        assert "Could not read Talk's signaling settings" in caplog.text


class TestOnlyOneDriverRuns:
    """The poll loop is not started when signaling is enabled."""

    def _wiring(self, config):
        """Run just the driver-selection branch `run_daemon` carries."""
        import istota.scheduler as sched

        started = []
        with patch.object(sched, "_start_talk_signaling",
                          side_effect=lambda c: started.append("signaling") or True), \
                patch.object(sched.threading, "Thread") as thread:
            thread.side_effect = lambda **kw: started.append(kw.get("name"))
            if config.talk.enabled and config.talk.signaling.enabled:
                sched._start_talk_signaling(config)
            elif config.talk.enabled:
                sched.threading.Thread(
                    target=sched._talk_poll_loop, args=(config,),
                    daemon=True, name="talk-poller",
                )
        return started

    def test_signaling_enabled_starts_no_poller(self, config):
        assert self._wiring(config) == ["signaling"]

    def test_signaling_disabled_starts_the_poller(self, config):
        config.talk.signaling.enabled = False
        assert self._wiring(config) == ["talk-poller"]

    def test_talk_disabled_starts_neither(self, config):
        config.talk.enabled = False
        assert self._wiring(config) == []

    def test_run_daemon_carries_exactly_that_branch(self):
        """The test above reproduces a branch; this pins that it is the one
        `run_daemon` has, so the reproduction cannot drift from the product.

        Read off the AST rather than the source text: the branch is documented
        in a comment that names both functions, so a substring search matches
        the prose and says nothing about the code.
        """
        import ast
        import inspect
        import textwrap

        import istota.scheduler as sched

        tree = ast.parse(textwrap.dedent(inspect.getsource(sched.run_daemon)))

        def mentions(node, name):
            return any(
                isinstance(n, ast.Name) and n.id == name
                for n in ast.walk(node)
            )

        gates = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.If) and mentions(node.test, "config")
            and "signaling" in ast.unparse(node.test)
            and "talk" in ast.unparse(node.test)
        ]
        assert len(gates) == 1, ast.unparse(tree)[:200]
        gate = gates[0]

        # The supervisor is what the signaling arm starts.
        assert any(
            mentions(stmt, "_start_talk_signaling") for stmt in gate.body
        )
        # And the poller appears in the *else* arm and nowhere else, so no
        # value of the config can start both.
        assert not any(mentions(stmt, "_talk_poll_loop") for stmt in gate.body)
        assert any(mentions(stmt, "_talk_poll_loop") for stmt in gate.orelse)
        assert sum(
            1 for n in ast.walk(tree)
            if isinstance(n, ast.Name) and n.id == "_talk_poll_loop"
        ) == 1
