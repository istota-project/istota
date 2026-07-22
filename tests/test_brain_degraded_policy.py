"""ISSUE-181 — backoff/cooldown + pause-when-degraded for non-essential
automatic brain callers (sleep cycle, shared-block generation).

These callers invoke the primary brain *directly* (not through the executor's
fallback-wrapped path), so they must (a) consult the shared availability
breaker before each call and (b) feed their own failures back into it, so a
degraded primary doesn't grind through every channel/block and re-attempt every
cycle.
"""

from unittest.mock import patch

import pytest

from istota.brain import BrainResult
from istota.brain import (
    primary_brain_unavailable,
    report_brain_result,
    reset_availability_breaker,
)
from istota.brain._fallback import get_availability_breaker
from istota.config import BrainConfig, UserConfig


@pytest.fixture(autouse=True)
def _reset_breaker():
    reset_availability_breaker()
    yield
    reset_availability_breaker()


# ---------------------------------------------------------------------------
# Direct-caller helpers (brain._fallback)
# ---------------------------------------------------------------------------


class TestPrimaryBrainUnavailable:
    def test_available_when_breaker_closed(self):
        cfg = BrainConfig(kind="claude_code", fallback_cooldown_seconds=900)
        assert primary_brain_unavailable(cfg) == (True, None)

    def test_unavailable_when_breaker_open(self):
        cfg = BrainConfig(kind="claude_code", fallback_cooldown_seconds=900)
        get_availability_breaker().open("claude_code", 900)
        available, reason = primary_brain_unavailable(cfg)
        assert available is False
        assert reason == "unavailable"

    def test_cooldown_zero_disables_stickiness(self):
        """fallback_cooldown_seconds=0 means every caller probes the primary."""
        cfg = BrainConfig(kind="claude_code", fallback_cooldown_seconds=0)
        get_availability_breaker().open("claude_code", 0)
        # cooldown 0 → never skip (matches the executor contract).
        assert primary_brain_unavailable(cfg) == (True, None)

    def test_keyed_by_primary_kind(self):
        cfg = BrainConfig(kind="native", fallback_cooldown_seconds=900)
        get_availability_breaker().open("claude_code", 900)
        # A different primary kind is unaffected.
        assert primary_brain_unavailable(cfg) == (True, None)


class TestReportBrainResult:
    def test_success_closes_breaker(self):
        cfg = BrainConfig(kind="claude_code", fallback_cooldown_seconds=900)
        get_availability_breaker().open("claude_code", 900)
        assert primary_brain_unavailable(cfg)[0] is False
        # A successful result closes it.
        reason = report_brain_result(
            BrainResult(True, "ok", stop_reason="completed"), cfg
        )
        assert reason is None
        assert primary_brain_unavailable(cfg)[0] is True

    def test_usage_limit_opens_breaker_once(self):
        cfg = BrainConfig(kind="claude_code", fallback_cooldown_seconds=900)
        r1 = report_brain_result(
            BrainResult(False, "usage limit", stop_reason="usage_limit"), cfg
        )
        r2 = report_brain_result(
            BrainResult(False, "usage limit", stop_reason="usage_limit"), cfg
        )
        assert r1 == "usage_limit"  # closed→open transition → alert
        assert r2 is None  # already open → no second alert
        assert primary_brain_unavailable(cfg)[0] is False

    def test_not_found_opens_breaker(self):
        cfg = BrainConfig(kind="claude_code", fallback_cooldown_seconds=900)
        assert report_brain_result(
            BrainResult(False, "missing", stop_reason="not_found"), cfg
        ) == "not_found"
        assert primary_brain_unavailable(cfg)[0] is False

    @pytest.mark.parametrize("reason", ["oom", "timeout", "error", "cancelled", "fallback"])
    def test_non_cooldown_reasons_do_not_open(self, reason):
        cfg = BrainConfig(kind="claude_code", fallback_cooldown_seconds=900)
        assert report_brain_result(
            BrainResult(False, "boom", stop_reason=reason), cfg
        ) is None
        assert primary_brain_unavailable(cfg)[0] is True

    def test_cooldown_zero_no_op(self):
        cfg = BrainConfig(kind="claude_code", fallback_cooldown_seconds=0)
        assert report_brain_result(
            BrainResult(False, "usage limit", stop_reason="usage_limit"), cfg
        ) is None
        # breaker never opened → still available.
        assert primary_brain_unavailable(cfg)[0] is True


# ---------------------------------------------------------------------------
# Sleep cycle: short-circuits the pass + per-call skip
# ---------------------------------------------------------------------------


def _sleep_config(tmp_path, *, cooldown=900, cron="* * * * *"):
    """A sleep-cycle config whose cron is always due (every minute)."""
    from istota.config import Config, SleepCycleConfig

    mount = tmp_path / "mount"
    mount.mkdir()
    cfg = Config(
        db_path=tmp_path / "test.db",
        temp_dir=tmp_path / "temp",
        nextcloud_mount_path=mount,
        sleep_cycle=SleepCycleConfig(enabled=True, cron=cron, lookback_hours=24),
        users={"alice": UserConfig(timezone="UTC")},
    )
    cfg.brain = BrainConfig(kind="claude_code", fallback_cooldown_seconds=cooldown)
    return cfg


class TestSleepCycleSkipsWhenDegraded:
    def test_check_sleep_cycles_skips_whole_pass_when_breaker_open(self, tmp_path):
        """The per-user pass short-circuits at the top when the breaker is open —
        no per-user iteration, no brain call."""
        from istota import db
        from istota.memory.sleep_cycle import check_sleep_cycles

        cfg = _sleep_config(tmp_path)
        db.init_db(cfg.db_path)
        get_availability_breaker().open("claude_code", 900)

        with patch(
            "istota.memory.sleep_cycle._run_sleep_cycle_brain"
        ) as mock_run, patch(
            "istota.memory.sleep_cycle.process_user_sleep_cycle"
        ) as mock_proc:
            with db.get_db(cfg.db_path) as conn:
                processed = check_sleep_cycles(conn, cfg)

        assert processed == []
        mock_run.assert_not_called()
        mock_proc.assert_not_called()

    def test_check_channel_sleep_cycles_skips_when_breaker_open(self, tmp_path):
        from istota import db
        from istota.memory.sleep_cycle import check_channel_sleep_cycles

        cfg = _sleep_config(tmp_path)
        db.init_db(cfg.db_path)
        # An active channel (recent completed task) so the loop would run.
        with db.get_db(cfg.db_path) as conn:
            t = db.create_task(
                conn, prompt="hi", user_id="alice", conversation_token="room1"
            )
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="yo")

        get_availability_breaker().open("claude_code", 900)

        with patch(
            "istota.memory.sleep_cycle.process_channel_sleep_cycle"
        ) as mock_proc:
            with db.get_db(cfg.db_path) as conn:
                processed = check_channel_sleep_cycles(conn, cfg)

        assert processed == []
        mock_proc.assert_not_called()

    def test_run_sleep_cycle_brain_skips_when_breaker_open(self, tmp_path):
        """The per-call helper short-circuits without paying for a brain call."""
        from istota.memory.sleep_cycle import _run_sleep_cycle_brain

        cfg = _sleep_config(tmp_path)
        get_availability_breaker().open("claude_code", 900)

        with patch("istota.memory.sleep_cycle.make_brain") as mock_make:
            ok, out = _run_sleep_cycle_brain(cfg, "prompt", "general", "test")
        assert ok is False
        assert out == ""
        mock_make.assert_not_called()

    def test_usage_limit_opens_breaker_and_alerts_once(self, tmp_path):
        """A usage_limit from the sleep cycle opens the breaker + fires one alert."""
        from istota.memory.sleep_cycle import _run_sleep_cycle_brain

        cfg = _sleep_config(tmp_path)
        alerts = []

        with patch("istota.memory.sleep_cycle.make_brain") as mock_make, patch(
            "istota.notifications.send_operator_alert",
            side_effect=lambda c, m, **k: alerts.append(m),
        ):
            mock_make.return_value.resolve_model_name.return_value = "m"
            mock_make.return_value.execute.return_value = BrainResult(
                False, "org spend limit", stop_reason="usage_limit"
            )
            ok1, _ = _run_sleep_cycle_brain(cfg, "p", "general", "ch1")
            ok2, _ = _run_sleep_cycle_brain(cfg, "p", "general", "ch2")

        assert ok1 is False and ok2 is False
        # First call opened the breaker + alerted; second call short-circuited
        # (breaker now open) so the brain was hit exactly once.
        assert primary_brain_unavailable(cfg.brain)[0] is False
        assert len(alerts) == 1
        assert "Sleep cycle paused" in alerts[0]
        assert mock_make.return_value.execute.call_count == 1

    def test_success_does_not_open_breaker(self, tmp_path):
        """A successful sleep-cycle call leaves the breaker closed."""
        from istota.memory.sleep_cycle import _run_sleep_cycle_brain

        cfg = _sleep_config(tmp_path)

        with patch("istota.memory.sleep_cycle.make_brain") as mock_make:
            mock_make.return_value.resolve_model_name.return_value = "m"
            mock_make.return_value.execute.return_value = BrainResult(
                True, "- a memory", stop_reason="completed"
            )
            ok, _ = _run_sleep_cycle_brain(cfg, "p", "general", "ch1")

        assert ok is True
        assert primary_brain_unavailable(cfg.brain)[0] is True

    def test_channel_loop_short_circuits_after_first_failure(self, tmp_path):
        """First channel's usage_limit opens the breaker; remaining channels skip."""
        from istota import db
        from istota.memory.sleep_cycle import check_channel_sleep_cycles

        cfg = _sleep_config(tmp_path)
        db.init_db(cfg.db_path)
        # Two active channels, both due (never-run + per-minute cron).
        with db.get_db(cfg.db_path) as conn:
            for token in ("room1", "room2"):
                t = db.create_task(
                    conn, prompt="hi", user_id="alice", conversation_token=token
                )
                db.update_task_status(conn, t, "running")
                db.update_task_status(conn, t, "completed", result="yo")

        call_count = {"n": 0}

        def fake_process(config, conn, token):
            call_count["n"] += 1
            # First channel hits the brain → usage_limit → opens breaker.
            report_brain_result(
                BrainResult(False, "spend limit", stop_reason="usage_limit"),
                config.brain,
            )
            return False

        with patch(
            "istota.memory.sleep_cycle.process_channel_sleep_cycle",
            side_effect=fake_process,
        ):
            with db.get_db(cfg.db_path) as conn:
                check_channel_sleep_cycles(conn, cfg)

        # Only the first channel was processed; the second was skipped in-pass
        # (the per-iteration breaker re-check broke the loop).
        assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# Shared blocks: synthesis skips when degraded; structured still generates
# ---------------------------------------------------------------------------


def _shared_block_config(tmp_path, *, cooldown=900):
    from istota.config import Config

    cfg = Config(
        db_path=tmp_path / "istota.db",
        nextcloud_mount_path=tmp_path / "mount",
        users={"stefan": UserConfig(timezone="UTC")},
    )
    cfg.brain = BrainConfig(kind="claude_code", fallback_cooldown_seconds=cooldown)
    return cfg


class TestSharedBlockSkipsWhenDegraded:
    def test_synthesis_run_skips_brain_when_breaker_open(self, tmp_path, monkeypatch):
        """run_shared_block's synthesis path skips the brain call when degraded."""
        from istota.briefings import shared_blocks
        from istota.briefings.sources import GatheredSource
        from istota.config import BriefingSharedBlock

        cfg = _shared_block_config(tmp_path)
        block = BriefingSharedBlock(
            name="hl", cron="0 6 * * *",
            sources=[{"kind": "markets", "config": {}}],
        )
        monkeypatch.setattr(
            shared_blocks, "_gather_shared",
            lambda config, sources, now: [
                GatheredSource(kind="markets", title="M", text="data"),
            ],
        )
        get_availability_breaker().open("claude_code", 900)

        # The real _run_section_brain should short-circuit (breaker open) and
        # never reach make_brain. Patch make_brain to assert it's not called.
        with patch("istota.brain.make_brain") as mock_make:
            result = shared_blocks.run_shared_block(block, cfg)

        # Skipped → None → caller keeps prior content.
        assert result is None
        mock_make.assert_not_called()

    def test_structured_block_still_generates_when_degraded(self, tmp_path, monkeypatch):
        """Structured blocks have no brain call, so they generate even when degraded."""
        from istota.briefings import shared_blocks
        from istota.briefings.sources import GatheredSource
        from istota.config import BriefingSharedBlock

        cfg = _shared_block_config(tmp_path)
        block = BriefingSharedBlock(
            name="mk", cron="0 6 * * *", render_mode="structured", trusted=True,
            sources=[{"kind": "markets", "config": {}}],
        )
        monkeypatch.setattr(
            shared_blocks, "_gather_shared",
            lambda config, sources, now: [
                GatheredSource(kind="markets", title="M", text="📈 DOW +1%"),
            ],
        )
        get_availability_breaker().open("claude_code", 900)

        result = shared_blocks.run_shared_block(block, cfg)
        assert result == {"text": "📈 DOW +1%", "trusted": True}

    def test_section_brain_usage_limit_opens_breaker_and_alerts(self, tmp_path):
        from istota.briefings.shared_blocks import _run_section_brain

        cfg = _shared_block_config(tmp_path)
        alerts = []

        with patch("istota.brain.make_brain") as mock_make, patch(
            "istota.notifications.send_operator_alert",
            side_effect=lambda c, m, **k: alerts.append(m),
        ):
            mock_make.return_value.resolve_model_name.return_value = "m"
            mock_make.return_value.execute.return_value = BrainResult(
                False, "spend limit", stop_reason="usage_limit"
            )
            ok, _ = _run_section_brain(cfg, "prompt", "world-headlines")

        assert ok is False
        assert primary_brain_unavailable(cfg.brain)[0] is False
        assert len(alerts) == 1
        assert "Shared block" in alerts[0]

    def test_generate_shared_block_skips_gather_when_degraded(self, tmp_path, monkeypatch):
        """The scheduler worker skips the expensive gather for synthesis blocks."""
        from istota import scheduler
        from istota.config import BriefingSharedBlock

        cfg = _shared_block_config(tmp_path)
        block = BriefingSharedBlock(
            name="hl", cron="0 6 * * *", render_mode="synthesis",
            sources=[{"kind": "browse", "config": {"url": "https://x"}}],
        )
        get_availability_breaker().open("claude_code", 900)

        run_calls = []
        monkeypatch.setattr(
            "istota.briefings.shared_blocks.run_shared_block",
            lambda b, config, now=None: run_calls.append(b) or None,
        )
        scheduler._generate_shared_block(cfg, block)
        # The early skip means run_shared_block is never reached (gather avoided).
        assert run_calls == []

    def test_generate_shared_block_structured_runs_when_degraded(self, tmp_path, monkeypatch):
        from istota import scheduler
        from istota.config import BriefingSharedBlock

        cfg = _shared_block_config(tmp_path)
        block = BriefingSharedBlock(
            name="mk", cron="0 6 * * *", render_mode="structured", trusted=True,
            sources=[{"kind": "markets", "config": {}}],
        )
        get_availability_breaker().open("claude_code", 900)

        run_calls = []
        monkeypatch.setattr(
            "istota.briefings.shared_blocks.run_shared_block",
            lambda b, config, now=None: run_calls.append(b) or {"text": "v", "trusted": True},
        )
        scheduler._generate_shared_block(cfg, block)
        assert len(run_calls) == 1


# ---------------------------------------------------------------------------
# Posture registry (Problem 3)
# ---------------------------------------------------------------------------


class TestPostureRegistry:
    def test_registry_has_skip_pin_fail_clean_postures(self):
        from istota.brain import POSTURE_FAIL_CLEAN, POSTURE_PIN, POSTURE_SKIP, TASK_POSTURES

        postures = {p.posture for p in TASK_POSTURES}
        assert {POSTURE_SKIP, POSTURE_PIN, POSTURE_FAIL_CLEAN} <= postures

    def test_sleep_cycle_and_shared_block_are_skip(self):
        from istota.brain import POSTURE_SKIP, task_postures_by_name

        assert task_postures_by_name()["sleep_cycle_user"].posture == POSTURE_SKIP
        assert task_postures_by_name()["sleep_cycle_channel"].posture == POSTURE_SKIP
        assert task_postures_by_name()["shared_block_synthesis"].posture == POSTURE_SKIP

    def test_briefing_is_pin(self):
        from istota.brain import POSTURE_PIN, task_postures_by_name

        assert task_postures_by_name()["briefing"].posture == POSTURE_PIN

    def test_every_entry_has_name_posture_callsite_notes(self):
        from istota.brain import TASK_POSTURES

        for p in TASK_POSTURES:
            assert p.name
            assert p.posture in ("skip", "pin", "fail_clean")
            assert p.call_site
            assert p.notes
