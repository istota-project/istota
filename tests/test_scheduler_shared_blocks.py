"""Tests for check_shared_blocks cron scheduling (UTC, off-dispatch worker)."""

import json
from datetime import datetime, timezone

from istota import db, scheduler
from istota.config import BriefingSharedBlock, Config, UserConfig


def _config(tmp_path, blocks) -> Config:
    cfg = Config(
        db_path=tmp_path / "istota.db",
        nextcloud_mount_path=tmp_path / "mount",
        users={"stefan": UserConfig(timezone="UTC")},
    )
    cfg.briefing_shared_blocks = blocks
    return cfg


def _at(monkeypatch, dt):
    monkeypatch.setattr(scheduler, "_now", lambda tz=None: dt.astimezone(tz) if tz else dt)


def _stub_generation(monkeypatch, value):
    monkeypatch.setattr(
        "istota.briefings.shared_blocks.run_shared_block",
        lambda b, config, now=None: value,
    )


NOW_0605 = datetime(2026, 7, 21, 6, 5, tzinfo=timezone.utc)


class TestCheckSharedBlocks:
    def test_due_block_fires(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path, [
            BriefingSharedBlock(name="hl", cron="0 6 * * *", sources=[]),
        ])
        db.init_db(cfg.db_path)
        _at(monkeypatch, NOW_0605)
        _stub_generation(monkeypatch, {"text": "news"})

        names = scheduler.check_shared_blocks(cfg, run_inline=True)
        assert names == ["hl"]
        with db.get_db(cfg.db_path) as conn:
            row = db.shared_kv_get(conn, "briefing_shared_blocks", "hl")
            assert json.loads(row["value"]) == {"text": "news"}
            assert db.get_briefing_shared_block_last_run(conn, "hl") is not None

    def test_not_due_block_skipped(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path, [
            BriefingSharedBlock(name="hl", cron="0 8 * * *", sources=[]),
        ])
        db.init_db(cfg.db_path)
        _at(monkeypatch, NOW_0605)
        _stub_generation(monkeypatch, {"text": "news"})

        assert scheduler.check_shared_blocks(cfg, run_inline=True) == []
        with db.get_db(cfg.db_path) as conn:
            assert db.shared_kv_get(conn, "briefing_shared_blocks", "hl") is None

    def test_disabled_block_skipped(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path, [
            BriefingSharedBlock(name="hl", cron="0 6 * * *", enabled=False, sources=[]),
        ])
        db.init_db(cfg.db_path)
        _at(monkeypatch, NOW_0605)
        _stub_generation(monkeypatch, {"text": "news"})
        assert scheduler.check_shared_blocks(cfg, run_inline=True) == []

    def test_last_run_prevents_refire_same_window(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path, [
            BriefingSharedBlock(name="hl", cron="0 6 * * *", sources=[]),
        ])
        db.init_db(cfg.db_path)
        _at(monkeypatch, NOW_0605)
        _stub_generation(monkeypatch, {"text": "news"})

        assert scheduler.check_shared_blocks(cfg, run_inline=True) == ["hl"]
        # Same tick again → last_run just stamped → not due.
        assert scheduler.check_shared_blocks(cfg, run_inline=True) == []

    def test_no_blocks_noop(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path, [])
        db.init_db(cfg.db_path)
        _at(monkeypatch, NOW_0605)
        assert scheduler.check_shared_blocks(cfg, run_inline=True) == []

    def test_stale_fire_suppressed(self, tmp_path, monkeypatch):
        # A cron whose next fire is far in the past (long outage) → suppressed,
        # last_run bumped so it resumes from the next future fire.
        cfg = _config(tmp_path, [
            BriefingSharedBlock(name="hl", cron="0 6 * * *", sources=[]),
        ])
        cfg.scheduler.cron_max_staleness_minutes = 60
        db.init_db(cfg.db_path)
        # now = 09:00, next fire from today-start = 06:00 → 180 min stale > 60.
        _at(monkeypatch, datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc))
        _stub_generation(monkeypatch, {"text": "news"})

        assert scheduler.check_shared_blocks(cfg, run_inline=True) == []
        with db.get_db(cfg.db_path) as conn:
            assert db.get_briefing_shared_block_last_run(conn, "hl") is not None
            assert db.shared_kv_get(conn, "briefing_shared_blocks", "hl") is None

    def test_background_thread_path(self, tmp_path, monkeypatch):
        # Default (run_inline=False) hands generation to a worker thread.
        cfg = _config(tmp_path, [
            BriefingSharedBlock(name="hl", cron="0 6 * * *", sources=[]),
        ])
        db.init_db(cfg.db_path)
        _at(monkeypatch, NOW_0605)

        started = {}

        class _FakeThread:
            def __init__(self, target, args=(), name=None, daemon=None):
                started["target"] = target
                started["args"] = args

            def start(self):
                started["started"] = True

        monkeypatch.setattr(scheduler.threading, "Thread", _FakeThread)
        names = scheduler.check_shared_blocks(cfg)
        assert names == ["hl"]
        assert started["started"] is True
