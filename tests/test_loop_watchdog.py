"""Tests for the scheduler main-loop stall watchdog (ISSUE-143)."""

import time
from unittest.mock import patch

from istota.config import Config, SchedulerConfig, UserConfig
from istota.scheduler import LoopWatchdog, _operator_alert_user


class TestOperatorAlertUser:
    def test_prefers_first_admin(self):
        config = Config(
            users={"bob": UserConfig(), "alice": UserConfig()},
            admin_users={"carol", "alice"},
        )
        assert _operator_alert_user(config) == "alice"

    def test_falls_back_to_first_user(self):
        config = Config(users={"bob": UserConfig(), "alice": UserConfig()})
        assert _operator_alert_user(config) == "alice"

    def test_none_when_no_users(self):
        assert _operator_alert_user(Config(users={})) is None


class TestLoopWatchdog:
    def test_disabled_when_threshold_zero(self):
        config = Config(users={"alice": UserConfig()})
        wd = LoopWatchdog(config, stall_seconds=0)
        wd.start()
        assert wd._thread is None
        wd.stop()  # no-op, must not raise

    def test_alerts_when_loop_does_not_tick(self):
        config = Config(users={"alice": UserConfig()})
        wd = LoopWatchdog(config, stall_seconds=1)
        # Force an immediate stall: pretend the last tick was long ago.
        wd._last_tick = time.time() - 100

        with patch("istota.scheduler.send_notification") as mock_notify:
            # Drive one watchdog check directly rather than racing the thread.
            with patch.object(wd._stop, "wait", side_effect=[False, True]):
                wd._run()

        assert wd._alerted is True
        mock_notify.assert_called_once()
        args, kwargs = mock_notify.call_args
        assert args[1] == "alice"  # user_id
        assert kwargs.get("purpose") == "alert"
        assert "stalled" in args[2].lower()

    def test_does_not_alert_when_loop_is_ticking(self):
        config = Config(users={"alice": UserConfig()})
        wd = LoopWatchdog(config, stall_seconds=5)
        wd._last_tick = time.time()  # fresh

        with patch("istota.scheduler.send_notification") as mock_notify:
            with patch.object(wd._stop, "wait", side_effect=[False, True]):
                wd._run()

        assert wd._alerted is False
        mock_notify.assert_not_called()

    def test_alerts_once_then_rearms_after_recovery(self):
        config = Config(users={"alice": UserConfig()})
        wd = LoopWatchdog(config, stall_seconds=1)
        wd._last_tick = time.time() - 100

        with patch("istota.scheduler.send_notification") as mock_notify:
            # Two stalled checks back-to-back: only the first should alert.
            with patch.object(wd._stop, "wait", side_effect=[False, False, True]):
                wd._run()
            assert mock_notify.call_count == 1

        # Loop recovers and re-arms.
        wd.tick()
        assert wd._alerted is False

    def test_suspended_blocks_alert_during_known_long_check(self):
        config = Config(users={"alice": UserConfig()})
        wd = LoopWatchdog(config, stall_seconds=1)
        wd._last_tick = time.time() - 100  # would normally alert

        with patch("istota.scheduler.send_notification") as mock_notify:
            with wd.suspended():
                # Simulate the watchdog thread checking mid-suspend.
                with patch.object(wd._stop, "wait", side_effect=[False, True]):
                    wd._run()
            assert mock_notify.call_count == 0
        # Exiting suspended() resets the tick so the next check starts clean.
        assert not wd._suspended

    def test_fire_alert_does_not_block_on_slow_delivery(self):
        """The alert is delivered off the watchdog thread, so a wedged delivery
        path can't freeze the watchdog itself."""
        import threading as _t

        config = Config(users={"alice": UserConfig()})
        wd = LoopWatchdog(config, stall_seconds=1)

        release = _t.Event()
        delivered = _t.Event()

        def _slow_send(*args, **kwargs):
            delivered.set()
            release.wait(5)  # block until released

        with patch("istota.scheduler.send_notification", side_effect=_slow_send):
            start = time.time()
            wd._fire_alert(99.0)
            # Returns promptly despite the blocking send.
            assert time.time() - start < 1.0
            assert delivered.wait(2), "delivery thread did not run"
        release.set()

    def test_config_default_threshold(self):
        assert SchedulerConfig().loop_stall_alert_seconds == 180
