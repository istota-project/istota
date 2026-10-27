"""Tests for check_shared_blocks cron scheduling (configurable TZ, off-dispatch worker).

Definitions are read from ``shared_block_configs`` on each pass, so the tests
store their blocks there rather than on the config.
"""

import json
from datetime import datetime, timezone

from istota import db, scheduler
from istota.config import BriefingSharedBlock, Config, UserConfig


def _store(cfg: Config, block: BriefingSharedBlock) -> None:
    with db.get_db(cfg.db_path) as conn:
        db.upsert_shared_block_config(
            conn,
            name=block.name,
            cron=block.cron,
            title=block.title,
            directive=block.directive,
            render_mode=block.render_mode,
            enabled=block.enabled,
            trusted=block.trusted,
            sources=block.sources,
        )


def _config(tmp_path, blocks) -> Config:
    cfg = Config(
        db_path=tmp_path / "istota.db",
        workspace_path=tmp_path / "mount",
        users={"alice": UserConfig(timezone="UTC")},
    )
    db.init_db(cfg.db_path)
    for block in blocks:
        _store(cfg, block)
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
        _at(monkeypatch, NOW_0605)
        _stub_generation(monkeypatch, {"text": "news"})

        assert scheduler.check_shared_blocks(cfg, run_inline=True) == []
        with db.get_db(cfg.db_path) as conn:
            assert db.shared_kv_get(conn, "briefing_shared_blocks", "hl") is None

    def test_disabled_block_skipped(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path, [
            BriefingSharedBlock(name="hl", cron="0 6 * * *", enabled=False, sources=[]),
        ])
        _at(monkeypatch, NOW_0605)
        _stub_generation(monkeypatch, {"text": "news"})
        assert scheduler.check_shared_blocks(cfg, run_inline=True) == []

    def test_last_run_prevents_refire_same_window(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path, [
            BriefingSharedBlock(name="hl", cron="0 6 * * *", sources=[]),
        ])
        _at(monkeypatch, NOW_0605)
        _stub_generation(monkeypatch, {"text": "news"})

        assert scheduler.check_shared_blocks(cfg, run_inline=True) == ["hl"]
        # Same tick again → last_run just stamped → not due.
        assert scheduler.check_shared_blocks(cfg, run_inline=True) == []

    def test_no_blocks_noop(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path, [])
        _at(monkeypatch, NOW_0605)
        assert scheduler.check_shared_blocks(cfg, run_inline=True) == []

    def test_stale_fire_suppressed(self, tmp_path, monkeypatch):
        # A cron whose next fire is far in the past (long outage) → suppressed,
        # last_run bumped so it resumes from the next future fire.
        cfg = _config(tmp_path, [
            BriefingSharedBlock(name="hl", cron="0 6 * * *", sources=[]),
        ])
        cfg.scheduler.cron_max_staleness_minutes = 60
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

    def test_configured_timezone_drives_fire_time(self, tmp_path, monkeypatch):
        # Cron is evaluated in the configured shared-block timezone, not UTC.
        # A 06:00 America/Los_Angeles cron (PDT = UTC-7 in July) is 13:00 UTC.
        # At 06:05 UTC the LA-local 06:00 fire hasn't happened yet → not due.
        cfg = _config(tmp_path, [
            BriefingSharedBlock(name="hl", cron="0 6 * * *", sources=[]),
        ])
        cfg.briefings.shared_block_timezone = "America/Los_Angeles"
        _at(monkeypatch, datetime(2026, 7, 21, 6, 5, tzinfo=timezone.utc))
        _stub_generation(monkeypatch, {"text": "news"})

        assert scheduler.check_shared_blocks(cfg, run_inline=True) == []

        # At 13:05 UTC (= 06:05 LA) the 06:00 LA fire is due.
        _at(monkeypatch, datetime(2026, 7, 21, 13, 5, tzinfo=timezone.utc))
        assert scheduler.check_shared_blocks(cfg, run_inline=True) == ["hl"]

    def test_invalid_timezone_falls_back_to_utc(self, tmp_path, monkeypatch, caplog):
        # An invalid zone name logs a warning and falls back to UTC (historical
        # behaviour), so a misconfiguration never breaks shared-block generation.
        cfg = _config(tmp_path, [
            BriefingSharedBlock(name="hl", cron="0 6 * * *", sources=[]),
        ])
        cfg.briefings.shared_block_timezone = "Not/A/Zone"
        _at(monkeypatch, NOW_0605)
        _stub_generation(monkeypatch, {"text": "news"})

        with caplog.at_level("WARNING"):
            assert scheduler.check_shared_blocks(cfg, run_inline=True) == ["hl"]
        assert any("Not/A/Zone" in r.message for r in caplog.records)


class TestDefinitionsReadPerPass:
    def test_block_created_after_load_fires_on_its_cron(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path, [])
        _at(monkeypatch, NOW_0605)
        _stub_generation(monkeypatch, {"text": "news"})
        assert scheduler.check_shared_blocks(cfg, run_inline=True) == []

        _store(cfg, BriefingSharedBlock(name="late", cron="0 6 * * *", sources=[]))

        assert cfg.briefings.shared_blocks == []
        assert scheduler.check_shared_blocks(cfg, run_inline=True) == ["late"]
        with db.get_db(cfg.db_path) as conn:
            row = db.shared_kv_get(conn, "briefing_shared_blocks", "late")
            assert json.loads(row["value"]) == {"text": "news"}

    def test_edited_directive_is_used_on_the_next_pass(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path, [
            BriefingSharedBlock(
                name="hl", cron="0 6 * * *", directive="old prompt", sources=[],
            ),
        ])
        # The boot-time overlay still holds the old definition.
        cfg.briefings.shared_blocks = [
            BriefingSharedBlock(
                name="hl", cron="0 6 * * *", directive="old prompt", sources=[],
            ),
        ]
        _store(cfg, BriefingSharedBlock(
            name="hl", cron="0 6 * * *", directive="new prompt", sources=[],
        ))
        _at(monkeypatch, NOW_0605)
        seen = []

        def _generate(block, config, now=None):
            seen.append(block.directive)
            return {"text": "news"}

        monkeypatch.setattr(
            "istota.briefings.shared_blocks.run_shared_block", _generate,
        )

        assert scheduler.check_shared_blocks(cfg, run_inline=True) == ["hl"]
        assert seen == ["new prompt"]

    def test_disabling_the_row_stops_a_block_the_config_still_lists(
        self, tmp_path, monkeypatch,
    ):
        block = BriefingSharedBlock(name="hl", cron="0 6 * * *", sources=[])
        cfg = _config(tmp_path, [block])
        cfg.briefings.shared_blocks = [block]
        _store(cfg, BriefingSharedBlock(
            name="hl", cron="0 6 * * *", enabled=False, sources=[],
        ))
        _at(monkeypatch, NOW_0605)
        _stub_generation(monkeypatch, {"text": "news"})

        assert scheduler.check_shared_blocks(cfg, run_inline=True) == []
