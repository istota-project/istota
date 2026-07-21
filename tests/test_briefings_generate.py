"""Tests for briefings generation assembly + archive + executor routing."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from istota import db
from istota.briefings import db as bdb
from istota.briefings import ensure_initialised, resolve_for_user
from istota.briefings.generate import archive_briefing, assemble_briefing_input
from istota.config import Config, UserConfig


def _config(tmp_path) -> Config:
    return Config(
        db_path=tmp_path / "istota.db",
        nextcloud_mount_path=tmp_path / "mount",
        users={"stefan": UserConfig(timezone="UTC")},
    )


def _write_workspace_file(cfg: Config, filename: str, content: str) -> str:
    from istota.storage import get_user_bot_path
    rel = f"{get_user_bot_path('stefan', cfg.bot_dir_name)}/{filename}".lstrip("/")
    p = cfg.nextcloud_mount_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return rel


def _ctx_with_blocks(cfg: Config, blocks: list[dict]):
    """Init the module DB and seed blocks. Each block: {title, render_mode?,
    sources: [{kind, config}]}."""
    ctx = resolve_for_user("stefan", cfg)
    ensure_initialised(ctx, app_config=cfg)
    with bdb.connect(ctx.db_path) as conn:
        for spec in blocks:
            bid = bdb.add_block(
                conn, briefing_name="M", title=spec["title"],
                render_mode=spec.get("render_mode", "synthesis"),
                directive=spec.get("directive"),
                options=spec.get("options", {}),
            )
            for s in spec.get("sources", []):
                bdb.add_source(conn, block_id=bid, kind=s["kind"],
                              config=s.get("config", {}))
        conn.commit()
    return ctx


class TestAssembleBriefingInput:
    def test_none_when_no_blocks(self, tmp_path):
        cfg = _config(tmp_path)
        ctx = resolve_for_user("stefan", cfg)
        ensure_initialised(ctx, app_config=cfg)
        db.init_db(cfg.db_path)
        with db.get_db(cfg.db_path) as conn:
            result = assemble_briefing_input(ctx, "M", cfg, conn=conn)
        assert result is None

    def test_grouped_prompt_with_notes_block(self, tmp_path):
        cfg = _config(tmp_path)
        rel = _write_workspace_file(cfg, "NOTES.md", "Buy a gift for mom")
        ctx = _ctx_with_blocks(cfg, [
            {"title": "Notes", "sources": [{"kind": "notes", "config": {"path": rel}}]},
        ])
        db.init_db(cfg.db_path)
        with db.get_db(cfg.db_path) as conn:
            result = assemble_briefing_input(ctx, "M", cfg, conn=conn)
        assert result is not None
        assert result.rendered_blocks == 1
        assert "### Block: Notes" in result.prompt
        assert "Buy a gift for mom" in result.prompt
        assert '"subject"' in result.prompt and '"body"' in result.prompt
        assert "Notes" in result.block_meta

    def test_empty_block_omitted(self, tmp_path):
        cfg = _config(tmp_path)
        # A todos block with no TODO.md file → empty → omitted.
        ctx = _ctx_with_blocks(cfg, [
            {"title": "Todos", "sources": [{"kind": "todos"}]},
        ])
        db.init_db(cfg.db_path)
        with db.get_db(cfg.db_path) as conn:
            result = assemble_briefing_input(ctx, "M", cfg, conn=conn)
        assert result is not None
        assert result.rendered_blocks == 0
        assert "### Block: Todos" not in result.prompt
        # But provenance is still recorded.
        assert result.block_meta["Todos"]["gathered"] == 0

    def test_block_order_preserved(self, tmp_path):
        cfg = _config(tmp_path)
        notes_rel = _write_workspace_file(cfg, "NOTES.md", "note one")
        todos_rel = _write_workspace_file(cfg, "TODO.md", "- [ ] task one")
        ctx = _ctx_with_blocks(cfg, [
            {"title": "Notes", "sources": [{"kind": "notes", "config": {"path": notes_rel}}]},
            {"title": "Todos", "sources": [{"kind": "todos", "config": {"path": todos_rel}}]},
        ])
        db.init_db(cfg.db_path)
        with db.get_db(cfg.db_path) as conn:
            result = assemble_briefing_input(ctx, "M", cfg, conn=conn)
        notes_pos = result.prompt.index("### Block: Notes")
        todos_pos = result.prompt.index("### Block: Todos")
        assert notes_pos < todos_pos


class TestStructuredGoldenParity:
    def test_markets_text_is_verbatim(self, tmp_path, monkeypatch):
        """A structured markets block reproduces the legacy fetcher output
        byte-for-byte — no model-side reformatting of the pre-rendered text."""
        import istota.briefings.sources.builtins as bi
        import istota.skills.briefing as briefing_mod

        golden = "🟢 **S&P 500**: 6,104.75 (+30.25, +0.50%)"
        monkeypatch.setattr(briefing_mod, "_fetch_market_data", lambda mc, mode, tz_str=None: golden)

        cfg = _config(tmp_path)
        from istota.briefings.sources import SourceContext
        monday = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
        ctx = SourceContext(app_config=cfg, user_id="stefan", now=monday)
        gs = bi.resolve_markets({}, ctx)
        # The gathered text is the fetcher output verbatim.
        assert gs.text == golden


class TestEmailNewsletterDiscrimination:
    def test_email_source_carries_discrimination_note(self):
        """A shared-pool email source tells the model to drop non-newsletter
        mail (receipts / transactional / spam) while keeping ambiguous items."""
        from istota.briefings.generate import _render_source
        from istota.briefings.sources import GatheredSource

        gs = GatheredSource(
            kind="email", title="Newsletters",
            items=[{"sender": "news@semafor.com", "subject": "Flagship",
                    "body": "world news today"}],
            provenance="1 newsletters (past 12h)",
        )
        rendered = _render_source(gs)
        assert "world news today" in rendered
        low = rendered.lower()
        assert "newsletter" in low
        assert "receipt" in low
        # Fail-open: keep an item when unsure.
        assert "keep it" in low

    def test_non_email_source_has_no_discrimination_note(self):
        from istota.briefings.generate import _render_source
        from istota.briefings.sources import GatheredSource

        gs = GatheredSource(kind="todos", title="Todos",
                            items=[{"text": "- [ ] task one"}])
        rendered = _render_source(gs)
        assert "receipt" not in rendered.lower()


class TestArchive:
    def test_archive_and_prune(self, tmp_path):
        cfg = _config(tmp_path)
        ctx = resolve_for_user("stefan", cfg)
        ensure_initialised(ctx, app_config=cfg)
        rid = archive_briefing(
            ctx, briefing_name="M", subject="Morning", body_md="📰 NEWS",
            task_id=7, block_meta={"News": {"gathered": 1}},
            delivered_to=["talk"], retention_days=90,
        )
        assert rid is not None
        with bdb.connect(ctx.db_path) as conn:
            rows = bdb.list_archive(conn, briefing_name="M")
        assert len(rows) == 1
        assert rows[0].subject == "Morning"
        assert rows[0].task_id == 7
        assert rows[0].delivered_to == ["talk"]


class TestSchedulerArchive:
    def _task(self, **kw):
        from istota.db import Task
        defaults = dict(
            id=5, status="completed", source_type="briefing", user_id="stefan",
            prompt="p", conversation_token="", briefing_name="M",
            output_target="talk,email",
        )
        defaults.update(kw)
        return Task(**defaults)

    def test_archives_module_path_briefing(self, tmp_path):
        from istota.scheduler import _maybe_archive_briefing

        cfg = _config(tmp_path)
        cfg.temp_dir = tmp_path / "temp"
        _ctx_with_blocks(cfg, [{"title": "Notes", "sources": [{"kind": "notes"}]}])
        db.init_db(cfg.db_path)

        parsed = {"subject": "Morning Briefing", "body": "📰 the news"}
        _maybe_archive_briefing(cfg, self._task(), "raw result", parsed)

        ctx = resolve_for_user("stefan", cfg)
        with bdb.connect(ctx.db_path) as conn:
            rows = bdb.list_archive(conn, briefing_name="M")
        assert len(rows) == 1
        assert rows[0].subject == "Morning Briefing"
        assert rows[0].body_md == "📰 the news"
        assert set(rows[0].delivered_to) == {"talk", "email"}

    def test_skips_legacy_no_blocks(self, tmp_path):
        from istota.scheduler import _maybe_archive_briefing

        cfg = _config(tmp_path)
        cfg.temp_dir = tmp_path / "temp"
        ctx = resolve_for_user("stefan", cfg)
        ensure_initialised(ctx, app_config=cfg)  # module DB but no blocks
        db.init_db(cfg.db_path)

        _maybe_archive_briefing(
            cfg, self._task(), "raw", {"subject": "x", "body": "y"},
        )
        with bdb.connect(ctx.db_path) as conn:
            assert bdb.count_archive(conn) == 0

    def test_skips_when_module_disabled(self, tmp_path):
        from istota.scheduler import _maybe_archive_briefing

        cfg = _config(tmp_path)
        cfg.temp_dir = tmp_path / "temp"
        cfg.users["stefan"].disabled_modules = ["briefings"]
        db.init_db(cfg.db_path)
        # Must not raise even though no module DB / ctx.
        _maybe_archive_briefing(
            cfg, self._task(), "raw", {"subject": "x", "body": "y"},
        )


class TestExecutorRouting:
    def _task(self, **kw):
        from istota.db import Task
        defaults = dict(
            id=1, status="running", source_type="briefing", user_id="stefan",
            prompt="placeholder", conversation_token="", briefing_name="M",
            output_target="talk",
        )
        defaults.update(kw)
        return Task(**defaults)

    def test_module_path_when_blocks(self, tmp_path):
        from istota.executor import build_deferred_briefing_prompt

        cfg = _config(tmp_path)
        cfg.temp_dir = tmp_path / "temp"
        rel = _write_workspace_file(cfg, "NOTES.md", "an important note")
        _ctx_with_blocks(cfg, [{"title": "Notes", "sources": [{"kind": "notes", "config": {"path": rel}}]}])
        db.init_db(cfg.db_path)

        task = self._task()
        prompt = build_deferred_briefing_prompt(task, cfg)
        assert prompt is not None
        assert "### Block: Notes" in prompt
        assert "an important note" in prompt

    def test_no_blocks_returns_none(self, tmp_path):
        # Blocks are the sole content model: module enabled but no blocks →
        # None (task fails with quiet retry), never a legacy render.
        from istota.executor import build_deferred_briefing_prompt

        cfg = _config(tmp_path)
        cfg.temp_dir = tmp_path / "temp"
        ctx = resolve_for_user("stefan", cfg)
        ensure_initialised(ctx, app_config=cfg)
        db.init_db(cfg.db_path)

        task = self._task()
        assert build_deferred_briefing_prompt(task, cfg) is None

    def test_module_disabled_returns_none(self, tmp_path):
        # Module disabled for the user → None (no legacy fallback).
        from istota.executor import build_deferred_briefing_prompt

        cfg = _config(tmp_path)
        cfg.temp_dir = tmp_path / "temp"
        cfg.users["stefan"].disabled_modules = ["briefings"]
        task = self._task()
        assert build_deferred_briefing_prompt(task, cfg) is None
