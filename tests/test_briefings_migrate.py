"""Tests for the briefings module components → blocks/sources migration."""

from pathlib import Path

import pytest

from istota.briefings import blocks_from_components, normalize_block_specs
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
# normalize_block_specs — pure config-authored block coercion
# ---------------------------------------------------------------------------


class TestNormalizeBlockSpecs:
    def test_full_rich_block(self):
        specs = normalize_block_specs([
            {
                "title": "World News",
                "directive": "3-5 stories, neutral.",
                "render_mode": "synthesis",
                "options": {"story_count": 5},
                "sources": [
                    {"kind": "rss", "config": {"feed_ref": {"kind": "category", "value": 4}}},
                    {"kind": "browse", "config": {"preset": "ap"}},
                ],
            },
        ])
        assert specs == [
            {
                "title": "World News",
                "directive": "3-5 stories, neutral.",
                "render_mode": "synthesis",
                "options": {"story_count": 5},
                "sources": [
                    {"kind": "rss", "config": {"feed_ref": {"kind": "category", "value": 4}}},
                    {"kind": "browse", "config": {"preset": "ap"}},
                ],
            }
        ]

    def test_missing_title_dropped(self):
        specs = normalize_block_specs([
            {"sources": [{"kind": "markets", "config": {}}]},
            {"title": "  ", "sources": [{"kind": "markets", "config": {}}]},
            {"title": "Keep", "sources": [{"kind": "markets", "config": {}}]},
        ])
        assert [s["title"] for s in specs] == ["Keep"]

    def test_unknown_source_kind_dropped(self):
        specs = normalize_block_specs([
            {
                "title": "Mixed",
                "sources": [
                    {"kind": "bogus", "config": {}},
                    {"kind": "markets", "config": {}},
                ],
            },
        ])
        assert len(specs) == 1
        assert [s["kind"] for s in specs[0]["sources"]] == ["markets"]

    def test_block_with_only_unknown_sources_dropped(self):
        specs = normalize_block_specs([
            {"title": "AllBad", "sources": [{"kind": "bogus", "config": {}}]},
        ])
        assert specs == []

    def test_block_with_no_sources_dropped(self):
        specs = normalize_block_specs([{"title": "Empty", "sources": []}])
        assert specs == []

    def test_unknown_render_mode_defaults_synthesis(self):
        specs = normalize_block_specs([
            {"title": "T", "render_mode": "wild",
             "sources": [{"kind": "rss", "config": {}}]},
        ])
        assert specs[0]["render_mode"] == "synthesis"

    def test_omitted_render_mode_structured_for_markets(self):
        specs = normalize_block_specs([
            {"title": "M", "sources": [{"kind": "markets", "config": {}}]},
        ])
        assert specs[0]["render_mode"] == "structured"

    def test_omitted_render_mode_synthesis_for_rss(self):
        specs = normalize_block_specs([
            {"title": "N", "sources": [{"kind": "rss", "config": {}}]},
        ])
        assert specs[0]["render_mode"] == "synthesis"

    def test_non_list_input(self):
        assert normalize_block_specs(None) == []
        assert normalize_block_specs({"title": "x"}) == []
        assert normalize_block_specs("nope") == []

    def test_non_dict_block_skipped(self):
        specs = normalize_block_specs([
            "not a table",
            {"title": "Good", "sources": [{"kind": "notes", "config": {}}]},
        ])
        assert [s["title"] for s in specs] == ["Good"]

    def test_options_missing_defaults_empty(self):
        specs = normalize_block_specs([
            {"title": "T", "sources": [{"kind": "todos", "config": {}}]},
        ])
        assert specs[0]["options"] == {}

    def test_source_config_missing_defaults_empty(self):
        specs = normalize_block_specs([
            {"title": "T", "sources": [{"kind": "todos"}]},
        ])
        assert specs[0]["sources"][0]["config"] == {}

    def test_non_dict_source_skipped(self):
        specs = normalize_block_specs([
            {"title": "T", "sources": ["not a table", {"kind": "notes", "config": {}}]},
        ])
        assert [s["kind"] for s in specs[0]["sources"]] == ["notes"]

    def test_directive_non_str_coerced_none(self):
        specs = normalize_block_specs([
            {"title": "T", "directive": 123,
             "sources": [{"kind": "notes", "config": {}}]},
        ])
        assert specs[0]["directive"] is None


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

    def test_empty_first_touch_then_briefing_added_later_migrates(self, tmp_path):
        # Regression: an empty first touch (e.g. opening the on-by-default
        # Briefings tab before configuring anything) must not permanently
        # disable migration. A briefing added afterward still migrates.
        empty_cfg = _config(tmp_path, [])
        ctx = synthesize_briefings_context(
            "stefan", tmp_path / "mount",
            db_path=empty_cfg.module_db_path("stefan", "briefings"),
        )
        ensure_initialised(ctx, app_config=empty_cfg)  # nothing to migrate yet

        later_cfg = _config(
            tmp_path,
            [BriefingConfig(name="Morning", cron="0 7 * * *",
                            components={"news": True, "markets": True})],
        )
        ensure_initialised(ctx, app_config=later_cfg)  # same DB, briefing now present

        with bdb.connect(ctx.db_path) as conn:
            blocks = bdb.list_blocks(conn, "Morning")
        assert [b.title for b in blocks] == ["News", "Markets"]

    def test_second_briefing_added_later_also_migrates(self, tmp_path):
        # A DB-wide sentinel would migrate the first briefing then lock out any
        # later one; the per-briefing sentinel migrates each independently.
        cfg_a = _config(
            tmp_path,
            [BriefingConfig(name="A", cron="0 7 * * *", components={"news": True})],
        )
        ctx = synthesize_briefings_context(
            "stefan", tmp_path / "mount",
            db_path=cfg_a.module_db_path("stefan", "briefings"),
        )
        ensure_initialised(ctx, app_config=cfg_a)

        cfg_ab = _config(
            tmp_path,
            [
                BriefingConfig(name="A", cron="0 7 * * *", components={"news": True}),
                BriefingConfig(name="B", cron="0 8 * * *", components={"markets": True}),
            ],
        )
        ensure_initialised(ctx, app_config=cfg_ab)

        with bdb.connect(ctx.db_path) as conn:
            a_blocks = bdb.list_blocks(conn, "A")
            b_blocks = bdb.list_blocks(conn, "B")
        assert [b.title for b in a_blocks] == ["News"]      # migrated once, not duplicated
        assert [b.title for b in b_blocks] == ["Markets"]   # later briefing migrated too

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


class TestConfigAuthoredBlocksSeeding:
    def test_seeds_rich_blocks_in_order(self, tmp_path):
        cfg = _config(
            tmp_path,
            [BriefingConfig(
                name="Morning", cron="0 7 * * *",
                blocks=[
                    {
                        "title": "World News",
                        "directive": "neutral tone",
                        "render_mode": "synthesis",
                        "options": {"story_count": 5},
                        "sources": [
                            {"kind": "rss", "config": {"feed_ref": {"kind": "category", "value": 4}}},
                            {"kind": "browse", "config": {"preset": "ap"}},
                        ],
                    },
                    {
                        "title": "Markets",
                        "render_mode": "structured",
                        "sources": [{"kind": "markets", "config": {}}],
                    },
                ],
            )],
        )
        ctx = synthesize_briefings_context(
            "stefan", tmp_path / "mount", db_path=cfg.module_db_path("stefan", "briefings"),
        )
        ensure_initialised(ctx, app_config=cfg)

        with bdb.connect(ctx.db_path) as conn:
            blocks = bdb.list_blocks(conn, "Morning")
        assert [b.title for b in blocks] == ["World News", "Markets"]
        assert blocks[0].directive == "neutral tone"
        assert blocks[0].options == {"story_count": 5}
        assert [s.kind for s in blocks[0].sources] == ["rss", "browse"]
        assert blocks[0].sources[0].config == {"feed_ref": {"kind": "category", "value": 4}}
        assert blocks[1].render_mode == "structured"

    def test_blocks_win_over_components(self, tmp_path):
        cfg = _config(
            tmp_path,
            [BriefingConfig(
                name="Morning", cron="0 7 * * *",
                components={"news": True, "markets": True},
                blocks=[{
                    "title": "Only Block",
                    "sources": [{"kind": "notes", "config": {}}],
                }],
            )],
        )
        ctx = synthesize_briefings_context(
            "stefan", tmp_path / "mount", db_path=cfg.module_db_path("stefan", "briefings"),
        )
        ensure_initialised(ctx, app_config=cfg)

        with bdb.connect(ctx.db_path) as conn:
            blocks = bdb.list_blocks(conn, "Morning")
        # Components content ("News", "Markets") absent — blocks won.
        assert [b.title for b in blocks] == ["Only Block"]

    def test_empty_blocks_falls_through_to_components(self, tmp_path):
        cfg = _config(
            tmp_path,
            [BriefingConfig(
                name="Morning", cron="0 7 * * *",
                components={"news": True}, blocks=[],
            )],
        )
        ctx = synthesize_briefings_context(
            "stefan", tmp_path / "mount", db_path=cfg.module_db_path("stefan", "briefings"),
        )
        ensure_initialised(ctx, app_config=cfg)

        with bdb.connect(ctx.db_path) as conn:
            blocks = bdb.list_blocks(conn, "Morning")
        assert [b.title for b in blocks] == ["News"]

    def test_end_to_end_from_loaded_config(self, tmp_path, monkeypatch):
        # Load a real config.toml carrying a rich-block briefing, then seed via
        # the module resolver, exactly as first-touch does in production.
        from istota.briefings import resolve_for_user
        from istota.config import load_config

        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(
            f'db_path = "{tmp_path / "istota.db"}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            f'nextcloud_mount_path = "{tmp_path / "mount"}"\n'
            "\n[users.dana]\n"
            'display_name = "Dana"\n'
            "\n[[users.dana.briefings]]\n"
            'name = "world"\n'
            'cron = "0 7 * * *"\n'
            'output = "email"\n'
            "\n[[users.dana.briefings.blocks]]\n"
            'title = "World News"\n'
            'render_mode = "synthesis"\n'
            "options = { story_count = 5 }\n"
            "\n[[users.dana.briefings.blocks.sources]]\n"
            'kind = "rss"\n'
            "config = { feed_ref = { kind = \"category\", value = 4 } }\n"
            "\n[[users.dana.briefings.blocks.sources]]\n"
            'kind = "browse"\n'
            'config = { preset = "ap" }\n'
        )
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        cfg = load_config(cfg_path)

        ctx = resolve_for_user("dana", cfg)
        ensure_initialised(ctx, app_config=cfg)

        with bdb.connect(ctx.db_path) as conn:
            blocks = bdb.list_blocks(conn, "world")
        assert [b.title for b in blocks] == ["World News"]
        assert blocks[0].options == {"story_count": 5}
        assert [s.kind for s in blocks[0].sources] == ["rss", "browse"]

    def test_idempotent_and_resurrection_protection(self, tmp_path):
        cfg = _config(
            tmp_path,
            [BriefingConfig(
                name="Morning", cron="0 7 * * *",
                blocks=[
                    {"title": "A", "sources": [{"kind": "notes", "config": {}}]},
                    {"title": "B", "sources": [{"kind": "todos", "config": {}}]},
                ],
            )],
        )
        ctx = synthesize_briefings_context(
            "stefan", tmp_path / "mount", db_path=cfg.module_db_path("stefan", "briefings"),
        )
        ensure_initialised(ctx, app_config=cfg)

        # User deletes a block, then a re-init runs.
        with bdb.connect(ctx.db_path) as conn:
            blocks = bdb.list_blocks(conn, "Morning", with_sources=False)
            bdb.delete_block(conn, blocks[0].id)
            conn.commit()
        ensure_initialised(ctx, app_config=cfg)

        with bdb.connect(ctx.db_path) as conn:
            titles = [b.title for b in bdb.list_blocks(conn, "Morning")]
        # Sentinel already set → deleted block not resurrected, no dupes.
        assert titles == ["B"]
