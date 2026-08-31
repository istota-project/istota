"""ISSUE-374 — the availability breaker ends at the quota's reset, not an hour later.

A `usage_limit` used to open the breaker for a flat `fallback_cooldown_seconds`
measured from the failure, so a limit hit eleven minutes before the window reset
held every task on the fallback brain for the remaining forty-nine. The breaker
now carries a deadline, and the deadline comes from the reset time
`subscription_usage` already caches.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import istota.subscription_usage as su
import pytest

from istota.brain import BrainResult, report_brain_result, reset_availability_breaker
from istota.brain._fallback import get_availability_breaker
from istota.brain_availability import read_unavailable
from istota.config import BrainConfig, Config

NOW = 1_756_000_000.0


@pytest.fixture(autouse=True)
def _reset_breaker():
    reset_availability_breaker()
    yield
    reset_availability_breaker()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _config(tmp_path, *, kind="claude_code", cooldown=3600.0) -> Config:
    config = Config(db_path=tmp_path / "istota.db")
    config.brain = BrainConfig(kind=kind, fallback_cooldown_seconds=cooldown)
    return config


def _cache_windows(config: Config, *resets_in: float | None, base: float | None = None) -> None:
    """Write a usage cache whose windows reset ``resets_in`` seconds after ``base``.

    ``base`` defaults to the real clock because the breaker path reads
    ``time.time()`` itself: ``resets_at`` is absolute, and a cache written a year
    in the simulated past would be recomputed as already reset.
    """
    origin = time.time() if base is None else base
    windows = tuple(
        su.UsageWindow(
            key=f"w{i}",
            label=f"Window {i}",
            percent=100.0,
            resets_at=None if seconds is None else _iso(origin + seconds),
            resets_in_seconds=None if seconds is None else int(seconds),
        )
        for i, seconds in enumerate(resets_in)
    )
    su.write_cache(
        su.cache_path(config.db_path.parent),
        su.UsageSnapshot(fetched_at=origin, windows=windows, source="fetch"),
    )


class TestSoonestResetSeconds:
    def test_takes_the_soonest_future_window(self):
        snapshot = su.UsageSnapshot(
            fetched_at=NOW,
            windows=(
                su.UsageWindow(key="a", label="A", percent=100.0, resets_in_seconds=7200),
                su.UsageWindow(key="b", label="B", percent=40.0, resets_in_seconds=660),
            ),
        )
        assert su.soonest_reset_seconds(snapshot) == 660

    def test_ignores_windows_that_have_already_reset(self):
        snapshot = su.UsageSnapshot(
            fetched_at=NOW,
            windows=(
                su.UsageWindow(key="a", label="A", percent=100.0, resets_in_seconds=0),
                su.UsageWindow(key="b", label="B", percent=100.0, resets_in_seconds=900),
            ),
        )
        assert su.soonest_reset_seconds(snapshot) == 900

    def test_none_when_no_window_carries_a_reset(self):
        snapshot = su.UsageSnapshot(
            fetched_at=NOW,
            windows=(su.UsageWindow(key="a", label="A", percent=100.0),),
        )
        assert su.soonest_reset_seconds(snapshot) is None

    def test_none_for_no_snapshot_at_all(self):
        assert su.soonest_reset_seconds(None) is None
        assert su.soonest_reset_seconds(su.UsageSnapshot(fetched_at=0.0)) is None


class TestCachedResetSeconds:
    def test_reads_the_disk_cache_and_recomputes_the_countdown(self, tmp_path):
        config = _config(tmp_path)
        _cache_windows(config, 660, base=NOW)
        # A minute later the same cached window is a minute closer.
        assert su.cached_reset_seconds(config, now_ts=NOW) == 660
        assert su.cached_reset_seconds(config, now_ts=NOW + 60) == 600

    def test_a_window_that_reset_while_the_cache_aged_is_ignored(self, tmp_path):
        config = _config(tmp_path)
        _cache_windows(config, 660, base=NOW)
        assert su.cached_reset_seconds(config, now_ts=NOW + 700) is None

    def test_none_without_a_cache(self, tmp_path):
        assert su.cached_reset_seconds(_config(tmp_path), now_ts=NOW) is None

    def test_none_when_the_operator_disabled_the_lookup(self, tmp_path):
        config = _config(tmp_path)
        _cache_windows(config, 660, base=NOW)
        config.brain.claude_code.subscription_usage = False
        assert su.cached_reset_seconds(config, now_ts=NOW) is None

    def test_never_reaches_the_network(self, tmp_path, monkeypatch):
        """The read must not fetch, resolve a credential, or open a socket.

        The guards record rather than raise: `cached_reset_seconds` catches
        `Exception`, and an `AssertionError` is one — a raising guard would be
        swallowed and the test would pass no matter what the code did.
        """
        config = _config(tmp_path)
        _cache_windows(config, 660, base=NOW)
        called = []

        monkeypatch.setattr(su, "fetch_snapshot", lambda *a, **kw: called.append("fetch"))
        monkeypatch.setattr(su, "resolve_token", lambda *a, **kw: called.append("token"))
        monkeypatch.setattr(su, "_urllib_transport", lambda *a, **kw: called.append("http"))

        # The cached answer still comes back, so the read really ran.
        assert su.cached_reset_seconds(config, now_ts=NOW) == 660
        assert called == []


class TestBreakerEndsAtTheReset:
    def test_the_window_ends_at_the_reset_not_an_hour_later(self, tmp_path):
        """The reported failure: a limit hit eleven minutes before the reset."""
        config = _config(tmp_path, cooldown=3600.0)
        _cache_windows(config, 660)
        breaker = get_availability_breaker()

        assert report_brain_result(
            BrainResult(False, "session limit", stop_reason="usage_limit"),
            config.brain,
            config=config,
        ) == "usage_limit"

        remaining = breaker.remaining("claude_code")
        assert remaining is not None
        assert 600 < remaining <= 660

    def test_the_published_record_expires_with_the_breaker(self, tmp_path):
        config = _config(tmp_path, cooldown=3600.0)
        _cache_windows(config, 660)

        report_brain_result(
            BrainResult(False, "session limit", stop_reason="usage_limit"),
            config.brain,
            config=config,
            started_at=NOW,
        )

        # The sibling process must not report a window the scheduler isn't holding.
        remaining = get_availability_breaker().remaining("claude_code")
        now = time.time()
        assert read_unavailable(config, "claude_code", now=now + remaining - 5) is not None
        assert read_unavailable(config, "claude_code", now=now + remaining + 5) is None

    def test_a_reset_beyond_the_cooldown_is_capped_by_it(self, tmp_path):
        config = _config(tmp_path, cooldown=3600.0)
        _cache_windows(config, 3 * 24 * 3600)

        report_brain_result(
            BrainResult(False, "weekly limit", stop_reason="usage_limit"),
            config.brain,
            config=config,
        )

        remaining = get_availability_breaker().remaining("claude_code")
        assert 3590 < remaining <= 3600

    def test_a_reset_moments_away_still_buys_the_floor(self, tmp_path):
        config = _config(tmp_path, cooldown=3600.0)
        _cache_windows(config, 30)

        report_brain_result(
            BrainResult(False, "session limit", stop_reason="usage_limit"),
            config.brain,
            config=config,
        )

        remaining = get_availability_breaker().remaining("claude_code")
        assert 55 < remaining <= 60

    def test_no_cache_keeps_the_flat_cooldown(self, tmp_path):
        config = _config(tmp_path, cooldown=3600.0)

        report_brain_result(
            BrainResult(False, "session limit", stop_reason="usage_limit"),
            config.brain,
            config=config,
        )

        remaining = get_availability_breaker().remaining("claude_code")
        assert 3590 < remaining <= 3600

    def test_a_missing_binary_ignores_the_quota_reset(self, tmp_path):
        """`not_found` is a missing CLI. A quota reset says nothing about it."""
        config = _config(tmp_path, cooldown=3600.0)
        _cache_windows(config, 660)

        report_brain_result(
            BrainResult(False, "no such binary", stop_reason="not_found"),
            config.brain,
            config=config,
        )

        remaining = get_availability_breaker().remaining("claude_code")
        assert 3590 < remaining <= 3600

    def test_a_non_subscription_primary_ignores_the_quota_reset(self, tmp_path):
        """The endpoint describes the Claude plan; a native brain's limit is elsewhere."""
        config = _config(tmp_path, kind="native", cooldown=3600.0)
        _cache_windows(config, 660)

        report_brain_result(
            BrainResult(False, "usage limit", stop_reason="usage_limit"),
            config.brain,
            config=config,
        )

        remaining = get_availability_breaker().remaining("native")
        assert 3590 < remaining <= 3600

    def test_a_repeat_failure_inside_the_window_does_not_move_the_deadline(self, tmp_path):
        config = _config(tmp_path, cooldown=3600.0)
        _cache_windows(config, 660)

        assert report_brain_result(
            BrainResult(False, "session limit", stop_reason="usage_limit"),
            config.brain, config=config,
        ) == "usage_limit"
        first = get_availability_breaker().remaining("claude_code")

        # A second report with a cache that now names a much later reset.
        _cache_windows(config, 3 * 24 * 3600)
        assert report_brain_result(
            BrainResult(False, "session limit", stop_reason="usage_limit"),
            config.brain, config=config,
        ) is None
        assert get_availability_breaker().remaining("claude_code") <= first
