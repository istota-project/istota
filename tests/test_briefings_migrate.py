"""Tests for the briefings module components → blocks/sources migration."""

from pathlib import Path

import pytest

from istota.briefings import blocks_from_components
from istota.briefings import db as bdb
from istota.briefings._migrate import ensure_initialised
from istota.briefings.workspace import synthesize_briefings_context
from istota.config import BriefingConfig, Config, UserConfig


# ---------------------------------------------------------------------------
# blocks_from_components — pure translation
# ---------------------------------------------------------------------------


class TestBlocksFromComponents:
    def test_empty(self):
        assert blocks_from_components({}) == []

    def test_news_shared_when_no_sources(self):
        specs = blocks_from_components({"news": True})
        assert len(specs) == 1
        assert specs[0]["title"] == "News"
        src = specs[0]["sources"][0]
        assert src["kind"] == "email"
        assert src["config"]["mode"] == "shared"

    def test_news_senders_from_sources(self):
        specs = blocks_from_components(
            {"news": {"sources": ["news@semafor.com", "axios.com"],
                     "lookback_hours": 24}}
        )
        src = specs[0]["sources"][0]
        assert src["kind"] == "email"
        assert src["config"]["mode"] == "senders"
        # email → literal; bare domain → *@domain
        assert src["config"]["senders"] == ["news@semafor.com", "*@axios.com"]
        assert src["config"]["lookback_hours"] == 24

    def test_headlines_to_browse_presets(self):
        specs = blocks_from_components({"headlines": {"sources": ["ap", "reuters"]}})
        assert len(specs) == 1
        assert specs[0]["title"] == "Headlines"
        kinds = [s["kind"] for s in specs[0]["sources"]]
        presets = [s["config"]["preset"] for s in specs[0]["sources"]]
        assert kinds == ["browse", "browse"]
        assert presets == ["ap", "reuters"]

    def test_headlines_empty_sources_skipped(self):
        assert blocks_from_components({"headlines": {"sources": []}}) == []

    def test_markets_structured_with_overrides(self):
        specs = blocks_from_components(
            {"markets": {"enabled": True, "futures": ["ES=F"], "indices": ["^GSPC"]}}
        )
        assert specs[0]["title"] == "Markets"
        assert specs[0]["render_mode"] == "structured"
        src = specs[0]["sources"][0]
        assert src["kind"] == "markets"
        assert src["config"] == {"futures": ["ES=F"], "indices": ["^GSPC"]}

    def test_calendar_todos_notes_reminders(self):
        specs = blocks_from_components(
            {"calendar": True, "todos": True, "notes": True, "reminders": True}
        )
        titles = [s["title"] for s in specs]
        assert titles == ["Calendar", "Todos", "Notes", "Reminder"]
        assert specs[0]["render_mode"] == "structured"  # calendar
        kinds = [s["sources"][0]["kind"] for s in specs]
        assert kinds == ["calendar", "todos", "notes", "reminders"]

    def test_dead_email_component_dropped(self):
        specs = blocks_from_components({"email": True})
        assert specs == []

    def test_fixed_order(self):
        specs = blocks_from_components(
            {
                "reminders": True,
                "markets": True,
                "news": True,
                "calendar": True,
                "headlines": {"sources": ["ap"]},
                "todos": True,
                "notes": True,
            }
        )
        titles = [s["title"] for s in specs]
        assert titles == [
            "News", "Headlines", "Markets", "Calendar", "Todos", "Notes", "Reminder",
        ]

    def test_disabled_dict_skipped(self):
        assert blocks_from_components({"markets": {"enabled": False}}) == []

    def test_non_dict_input(self):
        assert blocks_from_components(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ensure_initialised — DB seeding + idempotency
# ---------------------------------------------------------------------------


def _config(tmp_path: Path, briefings) -> Config:
    return Config(
        db_path=tmp_path / "istota.db",
        nextcloud_mount_path=tmp_path / "mount",
        users={"stefan": UserConfig(briefings=briefings)},
    )


class TestEnsureInitialised:
    def test_migrates_components_into_blocks(self, tmp_path, monkeypatch):
        cfg = _config(
            tmp_path,
            [
                BriefingConfig(
                    name="Morning", cron="0 7 * * *",
                    components={"news": True, "markets": True, "calendar": True},
                )
            ],
        )
        ctx = synthesize_briefings_context(
            "stefan", tmp_path / "mount", db_path=cfg.module_db_path("stefan", "briefings"),
        )
        ensure_initialised(ctx, app_config=cfg)

        with bdb.connect(ctx.db_path) as conn:
            blocks = bdb.list_blocks(conn, "Morning")
        assert [b.title for b in blocks] == ["News", "Markets", "Calendar"]
        assert blocks[0].sources[0].kind == "email"

    def test_idempotent_no_dup_blocks(self, tmp_path):
        cfg = _config(
            tmp_path,
            [BriefingConfig(name="M", cron="0 7 * * *", components={"news": True})],
        )
        ctx = synthesize_briefings_context(
            "stefan", tmp_path / "mount", db_path=cfg.module_db_path("stefan", "briefings"),
        )
        ensure_initialised(ctx, app_config=cfg)
        ensure_initialised(ctx, app_config=cfg)  # second run
        with bdb.connect(ctx.db_path) as conn:
            blocks = bdb.list_blocks(conn, "M")
        assert len(blocks) == 1

    def test_no_app_config_inits_but_skips_migration(self, tmp_path):
        ctx = synthesize_briefings_context("stefan", tmp_path / "mount")
        ensure_initialised(ctx)  # no app_config
        # DB exists and is empty (no migration ran).
        with bdb.connect(ctx.db_path) as conn:
            assert bdb.list_briefing_names(conn) == []

    def test_does_not_mutate_briefing_configs(self, tmp_path):
        original = {"news": True, "markets": True}
        briefing = BriefingConfig(name="M", cron="0 7 * * *", components=original)
        cfg = _config(tmp_path, [briefing])
        ctx = synthesize_briefings_context(
            "stefan", tmp_path / "mount", db_path=cfg.module_db_path("stefan", "briefings"),
        )
        ensure_initialised(ctx, app_config=cfg)
        # The framework-side components blob is untouched.
        assert briefing.components == original
