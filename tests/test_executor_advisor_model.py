"""Executor-level advisor resolution (advisor-model spec, Stage 3)."""

from unittest.mock import patch

from istota.config import BrainConfig, Config
from istota.db import Task
from istota.executor import _resolve_advisor, execute_task

from tests.test_executor_streaming import (
    _make_config,
    _make_task,
    _patch_executor,
    contextmanager_chain,
)


def _task(**kw) -> Task:
    return _make_task(**kw)


class TestResolveAdvisorUnit:
    """`_resolve_advisor` in isolation — no brain, no config-loader involved."""

    def test_no_config_advisor_returns_empty(self):
        config = Config()
        assert _resolve_advisor(_task(model=""), config) == ""

    def test_configured_advisor_flows_through_when_unpinned(self):
        config = Config(advisor_model="opus")
        assert _resolve_advisor(_task(model=""), config) == "opus"

    def test_task_model_pin_drops_the_advisor(self):
        # A per-task model pin (!model, !room model, a [[jobs]] model, an API
        # caller) drops the configured advisor: a rejected executor/advisor
        # pairing is a hard CLI error, not a downgrade.
        config = Config(advisor_model="opus")
        assert _resolve_advisor(_task(model="haiku"), config) == ""

    def test_whitespace_only_task_model_does_not_count_as_a_pin(self):
        config = Config(advisor_model="opus")
        assert _resolve_advisor(_task(model="   "), config) == "opus"

    def test_none_task_model_does_not_count_as_a_pin(self):
        config = Config(advisor_model="opus")
        assert _resolve_advisor(_task(model=None), config) == "opus"


class _FakeAnthropicBrain:
    """Minimal Brain stand-in for the executor's `brain = make_brain(...)` call,
    with the anthropic namespace so the `advisor` field actually resolves."""

    model_namespace = "anthropic"

    def __init__(self):
        self.received_reqs = []

    def resolve_model_name(self, name):
        return (name or "").strip()

    def resolve_alias(self, a):
        return None

    def execute(self, req):
        from istota.brain._types import BrainResult

        self.received_reqs.append(req)
        return BrainResult(True, "ok", stop_reason="completed")

    def list_aliases(self):
        return []

    def validate_alias_override(self, r, t):
        return []


class _FakeNativeBrain(_FakeAnthropicBrain):
    model_namespace = "openai_compat"


def _run_with_fake_brain(tmp_path, fake_brain, *, advisor_model="", task_model=""):
    config = _make_config(tmp_path)
    config.advisor_model = advisor_model
    config.brain = BrainConfig(kind="claude_code")
    config.security.sandbox_enabled = False

    patches = _patch_executor() + [
        patch("istota.executor.make_brain", return_value=fake_brain),
    ]
    with contextmanager_chain(patches):
        task = _task(source_type="cli", model=task_model)
        execute_task(task, config, [])
    return fake_brain.received_reqs[0]


class TestBrainRequestWiring:
    def test_unpinned_task_carries_configured_advisor(self, tmp_path):
        fb = _FakeAnthropicBrain()
        req = _run_with_fake_brain(tmp_path, fb, advisor_model="opus")
        assert req.advisor == "opus"

    def test_pinned_task_drops_advisor(self, tmp_path):
        fb = _FakeAnthropicBrain()
        req = _run_with_fake_brain(
            tmp_path, fb, advisor_model="opus", task_model="haiku"
        )
        assert req.advisor == ""

    def test_no_configured_advisor_stays_empty(self, tmp_path):
        fb = _FakeAnthropicBrain()
        req = _run_with_fake_brain(tmp_path, fb, advisor_model="")
        assert req.advisor == ""

    def test_native_brain_never_carries_advisor(self, tmp_path):
        # model_namespace != "anthropic" — the executor never sets `advisor`
        # regardless of config, since NativeBrain has no wire for it.
        fb = _FakeNativeBrain()
        req = _run_with_fake_brain(tmp_path, fb, advisor_model="opus")
        assert req.advisor == ""


class TestRealBrainResolvesAdvisorAlias:
    """The fakes above stub resolve_model_name as identity, which can't fail
    on the spec's own design decision (Stage 2/3: the advisor resolves through
    the real alias table, and a :effort modifier is silently stripped since the
    CLI flag takes none). This runs the unmocked ClaudeCodeBrain end to end."""

    def test_smart_high_reaches_argv_as_opus_no_effort_modifier(self, tmp_path):
        from unittest.mock import MagicMock

        from istota.brain.claude_code import OPUS

        config = _make_config(tmp_path)
        config.advisor_model = "smart:high"
        config.brain = BrainConfig(kind="claude_code")
        config.security.sandbox_enabled = False

        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            mock = MagicMock()
            mock.stdout = "ok"
            mock.stderr = ""
            mock.returncode = 0
            return mock

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.run", side_effect=fake_run),
        ]
        with contextmanager_chain(patches):
            task = _task(source_type="cli", model="")
            execute_task(task, config, [])

        assert "--advisor" in captured_cmd
        idx = captured_cmd.index("--advisor")
        # "smart" -> OPUS (DEFAULT_ALIASES); the ":high" effort modifier is
        # stripped — --advisor takes no effort. Imports the live constant
        # rather than pinning a version string, so a future OPUS bump doesn't
        # break this test the way it broke on the OPUS 4.8 -> 5 bump.
        assert captured_cmd[idx + 1] == OPUS


class TestAdvisorLogLine:
    def test_logs_once_when_advisor_resolved(self, tmp_path, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="istota.executor"):
            _run_with_fake_brain(tmp_path, _FakeAnthropicBrain(), advisor_model="opus")
        matches = [r for r in caplog.records if "advisor=opus" in r.message]
        assert len(matches) == 1

    def test_no_log_line_when_no_advisor(self, tmp_path, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="istota.executor"):
            _run_with_fake_brain(tmp_path, _FakeAnthropicBrain(), advisor_model="")
        assert not any("advisor=" in r.message for r in caplog.records)


class TestFallbackDropsAdvisor:
    """`_run_fallback` strips `advisor` whenever the fallback brain's
    model_namespace isn't anthropic; an anthropic->anthropic fallback keeps it."""

    def test_anthropic_to_native_fallback_drops_advisor(self, tmp_path):
        from istota.brain._fallback import reset_availability_breaker
        from istota.brain._types import BrainResult

        reset_availability_breaker()
        primary = _FakeAnthropicBrain()
        primary.execute = lambda req: BrainResult(
            False, "usage limit", stop_reason="usage_limit"
        )
        fallback = _FakeNativeBrain()

        config = _make_config(tmp_path)
        config.advisor_model = "opus"
        config.brain = BrainConfig(kind="claude_code", fallback="native")
        config.security.sandbox_enabled = False

        def fake_make_brain(bc):
            return primary if getattr(bc, "kind", "") == "claude_code" else fallback

        patches = _patch_executor() + [
            patch("istota.executor.make_brain", side_effect=fake_make_brain),
            patch("istota.executor._native_with_user_key", side_effect=lambda nc, *a, **k: nc),
            patch("istota.notifications.send_notification"),
        ]
        with contextmanager_chain(patches):
            task = _task(source_type="cli", model="")
            execute_task(task, config, [])

        assert fallback.received_reqs
        assert fallback.received_reqs[0].advisor == ""

        reset_availability_breaker()

    def test_anthropic_to_anthropic_fallback_keeps_advisor(self, tmp_path):
        from istota.brain._fallback import reset_availability_breaker
        from istota.brain._types import BrainResult

        reset_availability_breaker()
        primary = _FakeAnthropicBrain()
        primary.execute = lambda req: BrainResult(
            False, "usage limit", stop_reason="usage_limit"
        )
        fallback = _FakeAnthropicBrain()

        config = _make_config(tmp_path)
        config.advisor_model = "opus"
        config.brain = BrainConfig(kind="claude_code", fallback="tmux_claude")
        config.security.sandbox_enabled = False

        def fake_make_brain(bc):
            return primary if getattr(bc, "kind", "") == "claude_code" else fallback

        patches = _patch_executor() + [
            patch("istota.executor.make_brain", side_effect=fake_make_brain),
            patch("istota.executor._native_with_user_key", side_effect=lambda nc, *a, **k: nc),
            patch("istota.notifications.send_notification"),
        ]
        with contextmanager_chain(patches):
            task = _task(source_type="cli", model="")
            execute_task(task, config, [])

        assert fallback.received_reqs
        assert fallback.received_reqs[0].advisor == "opus"

        reset_availability_breaker()

    def _run_fallback_with(self, tmp_path, *, task_model, fallback_kind="tmux_claude"):
        """Primary hits a usage limit; return the request the fallback got.

        `fallback_kind` decides whether the reroute crosses a namespace, which
        since ISSUE-417 is what decides whether the pin carries: `tmux_claude`
        is the same `anthropic` namespace as the primary, `native` is not.
        """
        from istota.brain._fallback import reset_availability_breaker
        from istota.brain._types import BrainResult

        reset_availability_breaker()
        primary = _FakeAnthropicBrain()
        primary.execute = lambda req: BrainResult(
            False, "usage limit", stop_reason="usage_limit"
        )
        fallback = (
            _FakeNativeBrain() if fallback_kind == "native" else _FakeAnthropicBrain()
        )

        config = _make_config(tmp_path)
        config.advisor_model = "sonnet"
        config.brain = BrainConfig(kind="claude_code", fallback=fallback_kind)
        config.security.sandbox_enabled = False

        def fake_make_brain(bc):
            return primary if getattr(bc, "kind", "") == "claude_code" else fallback

        patches = _patch_executor() + [
            patch("istota.executor.make_brain", side_effect=fake_make_brain),
            patch("istota.executor._native_with_user_key", side_effect=lambda nc, *a, **k: nc),
            patch("istota.notifications.send_notification"),
        ]
        with contextmanager_chain(patches):
            task = _task(source_type="cli", model=task_model)
            execute_task(task, config, [])

        assert fallback.received_reqs
        reset_availability_breaker()
        return fallback.received_reqs[0]

    def test_a_pin_dropped_across_a_namespace_also_drops_the_advisor(
        self, tmp_path,
    ):
        # "opus" is a non-portable pin (a provider shortcut, not a portable
        # tier), so it cannot cross into another namespace: the fallback runs
        # on its own default model, and the advisor was never evaluated against
        # that model, so it goes too.
        #
        # The pin is the *task's*. It used to be `config.model`, which the
        # executor substituted into every request; since ISSUE-418 a request
        # carries a genuine per-task pin or nothing, which is the only case
        # this rule was ever written for.
        req = self._run_fallback_with(
            tmp_path, task_model="opus", fallback_kind="native",
        )
        assert req.model == ""  # dropped_pin: fb default
        assert req.advisor == ""

    def test_a_same_namespace_fallback_keeps_the_pin(self, tmp_path):
        """ISSUE-417: `claude_code -> tmux_claude` crosses nothing.

        The same `claude` binary and the same `anthropic` namespace, so `opus`
        is valid on the target and carries. This used to drop it and tell the
        user their pin had been dropped, for no reason — which is what the old
        version of the test above asserted on this pairing.

        Nothing is claimed about the advisor here, and that is not an omission:
        `_resolve_advisor` drops it at the *request build* for any task carrying
        a model pin, so it is already empty on the primary request and there is
        none for the fallback to keep. The pairing rule and the crossing rule
        cannot both be exercised by one task.
        """
        req = self._run_fallback_with(
            tmp_path, task_model="opus", fallback_kind="tmux_claude",
        )
        assert req.model == "opus"
        assert req.advisor == ""

    def test_an_unpinned_task_keeps_its_advisor_across_the_fallback(self, tmp_path):
        """The ISSUE-418 half: no pin means nothing to drop.

        The deployment default used to stand in as a pin here, so an unpinned
        task reached the fallback carrying a non-portable id, had it dropped,
        lost its advisor, and showed the user a "your pin was dropped" note
        about a pin they never set. With no pin the fallback applies its own
        default and the advisor survives — the same condition that keeps it on
        the primary, which is also unpinned.
        """
        req = self._run_fallback_with(tmp_path, task_model="")
        assert req.model == ""
        assert req.advisor == "sonnet"
