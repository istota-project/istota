"""Cross-process availability state for the admin dashboard."""

from istota.brain_availability import (
    clear_all,
    clear_unavailable,
    read_unavailable,
    record_unavailable,
)
from istota.config import Config


def _config(tmp_path):
    return Config(db_path=tmp_path / "istota.db")


def test_published_unavailability_expires_at_the_breaker_cooldown(tmp_path):
    config = _config(tmp_path)

    assert record_unavailable(
        config,
        "claude_code",
        "usage_limit",
        cooldown_seconds=900,
        now=1000,
    )
    assert read_unavailable(config, "claude_code", now=1899) == {
        "reason": "usage_limit"
    }
    assert read_unavailable(config, "claude_code", now=1900) is None


def test_successful_probe_clears_published_unavailability(tmp_path):
    config = _config(tmp_path)
    record_unavailable(
        config,
        "claude_code",
        "usage_limit",
        cooldown_seconds=900,
        now=1000,
    )

    assert clear_unavailable(config, "claude_code")
    assert read_unavailable(config, "claude_code", now=1001) is None


def test_successful_probe_does_not_clear_a_newer_failure(tmp_path):
    config = _config(tmp_path)
    record_unavailable(
        config,
        "claude_code",
        "usage_limit",
        cooldown_seconds=900,
        now=1000,
    )
    record_unavailable(
        config,
        "claude_code",
        "usage_limit",
        cooldown_seconds=900,
        now=2000,
    )

    assert not clear_unavailable(config, "claude_code", started_at=1500)
    assert read_unavailable(config, "claude_code", now=2001) == {
        "reason": "usage_limit"
    }


def test_each_primary_has_its_own_published_state(tmp_path):
    config = _config(tmp_path)
    record_unavailable(
        config,
        "claude_code",
        "usage_limit",
        cooldown_seconds=900,
        now=1000,
    )
    record_unavailable(
        config,
        "native",
        "not_found",
        cooldown_seconds=900,
        now=1001,
    )

    assert read_unavailable(config, "claude_code", now=1002) == {
        "reason": "usage_limit"
    }
    assert read_unavailable(config, "native", now=1002) == {"reason": "not_found"}


def test_scheduler_startup_can_clear_all_published_state(tmp_path):
    config = _config(tmp_path)
    record_unavailable(
        config, "claude_code", "usage_limit", cooldown_seconds=900, now=1000
    )
    record_unavailable(
        config, "native", "not_found", cooldown_seconds=900, now=1000
    )

    assert clear_all(config) == 2
    assert read_unavailable(config, "claude_code", now=1001) is None
    assert read_unavailable(config, "native", now=1001) is None
