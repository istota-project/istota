"""Tests for briefings generation assembly + archive + executor routing."""

from datetime import datetime, timezone
import json
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest


from istota import db
from istota.briefings import db as bdb
from istota.briefings import ensure_initialised, resolve_for_user
from istota.briefings.generate import archive_briefing, assemble_briefing_input
from istota.config import Config, UserConfig


def _config(tmp_path) -> Config:
    return Config(
        db_path=tmp_path / "istota.db",
        workspace_path=tmp_path / "mount",
        users={"alice": UserConfig(timezone="UTC")},
    )


def _write_workspace_file(cfg: Config, filename: str, content: str) -> str:
    from istota.storage import get_user_bot_path
    rel = f"{get_user_bot_path('alice', cfg.bot_dir_name)}/{filename}".lstrip("/")
    p = cfg.workspace_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return rel


def _ctx_with_blocks(cfg: Config, blocks: list[dict]):
    """Init the module DB and seed blocks. Each block: {title, render_mode?,
    sources: [{kind, config}]}."""
    ctx = resolve_for_user("alice", cfg)
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
        ctx = resolve_for_user("alice", cfg)
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
        assert '"body_md"' in result.prompt and '"subject"' not in result.prompt
        assert len(result.block_meta) == 1
        assert result.manifest["blocks"][0]["block_id"] in result.block_meta

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
        assert next(iter(result.block_meta.values()))["gathered"] == 0
        assert result.manifest["blocks"] == []

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
        ctx = SourceContext(app_config=cfg, user_id="alice", now=monday)
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


class TestTodoSectionRendering:
    """Todo items render grouped under their source heading (ISSUE-207).

    Without this the model saw a flat list and a block directive naming a
    section ("only show items under ### NOW") had nothing to act on.
    """

    def _gs(self, items):
        from istota.briefings.sources import GatheredSource

        return GatheredSource(kind="todos", title="Todos", items=items)

    def test_section_label_precedes_its_group(self):
        from istota.briefings.generate import _render_source

        rendered = _render_source(self._gs([
            {"text": "- [ ] ship it", "section": "NOW"},
            {"text": "- [ ] and this", "section": "NOW"},
            {"text": "- [ ] someday", "section": "BACKLOG"},
        ]))
        body = rendered.splitlines()[1:]  # drop the source header
        assert body == [
            body[0],  # the grouping note
            "NOW:",
            "- [ ] ship it",
            "- [ ] and this",
            "BACKLOG:",
            "- [ ] someday",
        ]

    def test_label_emitted_once_per_group(self):
        from istota.briefings.generate import _render_source

        rendered = _render_source(self._gs([
            {"text": "- a", "section": "NOW"},
            {"text": "- b", "section": "NOW"},
        ]))
        assert rendered.count("NOW:") == 1

    def test_labels_are_not_markdown_headings(self):
        """The block prompt forbids markdown headings in the output; a raw
        '### NOW' inside a source body invites one straight through."""
        from istota.briefings.generate import _render_source

        rendered = _render_source(self._gs([
            {"text": "- a", "section": "NOW"},
        ]))
        assert "#" not in rendered

    def test_sectionless_items_render_bare(self):
        from istota.briefings.generate import _render_source

        rendered = _render_source(self._gs([
            {"text": "- loose", "section": None},
            {"text": "- under now", "section": "NOW"},
        ]))
        lines = rendered.splitlines()
        assert "- loose" in lines
        assert lines.index("NOW:") == lines.index("- under now") - 1

    def test_grouping_note_only_when_sections_present(self):
        from istota.briefings.generate import _render_source

        with_sections = _render_source(self._gs([
            {"text": "- a", "section": "NOW"},
        ]))
        without = _render_source(self._gs([{"text": "- a", "section": None}]))
        assert "grouped by" in with_sections.lower()
        assert "grouped by" not in without.lower()

    def test_legacy_items_without_section_key_still_render(self):
        """An archived/mid-rollout item dict may predate the section key."""
        from istota.briefings.generate import _render_source

        rendered = _render_source(self._gs([{"text": "- [ ] task one"}]))
        assert "- [ ] task one" in rendered


class TestArchive:
    def test_archive_and_prune(self, tmp_path):
        cfg = _config(tmp_path)
        ctx = resolve_for_user("alice", cfg)
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
            id=5, status="completed", source_type="briefing", user_id="alice",
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

        parsed = {"body": "📰 the news"}
        _maybe_archive_briefing(
            cfg, self._task(created_at="2026-07-27 06:00:00"), "raw result", parsed,
        )

        ctx = resolve_for_user("alice", cfg)
        with bdb.connect(ctx.db_path) as conn:
            rows = bdb.list_archive(conn, briefing_name="M")
        assert len(rows) == 1
        # Deterministic — derived from the briefing name + run date, not from
        # anything the model wrote.
        assert rows[0].subject == "M Briefing — Monday, 27 July"
        assert rows[0].body_md == "📰 the news"
        assert set(rows[0].delivered_to) == {"talk", "email"}

    def test_ignores_a_model_supplied_subject(self, tmp_path):
        """A stale model still emitting `subject` must not reach the archive."""
        from istota.scheduler import _maybe_archive_briefing

        cfg = _config(tmp_path)
        cfg.temp_dir = tmp_path / "temp"
        _ctx_with_blocks(cfg, [{"title": "Notes", "sources": [{"kind": "notes"}]}])
        db.init_db(cfg.db_path)

        parsed = {"subject": "🌎 Morning Briefing — Iran, the Fed", "body": "news"}
        _maybe_archive_briefing(
            cfg, self._task(created_at="2026-07-27 06:00:00"), "raw result", parsed,
        )

        ctx = resolve_for_user("alice", cfg)
        with bdb.connect(ctx.db_path) as conn:
            rows = bdb.list_archive(conn, briefing_name="M")
        assert rows[0].subject == "M Briefing — Monday, 27 July"

    def test_the_archived_block_meta_comes_back_from_the_control_directory(
        self, tmp_path,
    ):
        """Both ends of the provenance handoff, in one run.

        The executor writes `briefing_meta.json` and the scheduler reads it
        back after `execute_task` has returned — two modules, and the reader
        sits inside a bare `except Exception: block_meta = {}`. So a wrong
        path on either end is green and silently lossy: the archive is written,
        every other assertion in this class still passes, and the per-block
        provenance is gone with nothing logged. Hence the assertion is on the
        archived `block_meta` being non-empty, never on the archive call
        having happened.
        """
        from istota.executor import (
            build_deferred_briefing_prompt,
            get_task_control_dir,
            get_user_temp_dir,
        )
        from istota.scheduler import _maybe_archive_briefing

        cfg = _config(tmp_path)
        cfg.temp_dir = tmp_path / "temp"
        rel = _write_workspace_file(cfg, "NOTES.md", "an important note")
        _ctx_with_blocks(cfg, [
            {"title": "Notes", "sources": [{"kind": "notes", "config": {"path": rel}}]},
        ])
        db.init_db(cfg.db_path)

        task = self._task(id=11, created_at="2026-07-27 06:00:00")
        assert build_deferred_briefing_prompt(task, cfg) is not None

        control_dir = get_task_control_dir(cfg, "alice", 11)
        meta_path = control_dir / "briefing_meta.json"
        assert meta_path.exists(), "the write end never reached the control directory"
        # And nowhere else: the old location is model-writable, which is the
        # whole reason the file moved.
        user_temp = get_user_temp_dir(cfg, "alice")
        assert not (user_temp / "task_11_briefing_meta.json").exists()

        _maybe_archive_briefing(cfg, task, "raw result", {"body": "the news"})

        ctx = resolve_for_user("alice", cfg)
        with bdb.connect(ctx.db_path) as conn:
            rows = bdb.list_archive(conn, briefing_name="M")
        assert len(rows) == 1
        assert rows[0].block_meta, "provenance was lost between the two ends"
        assert next(iter(rows[0].block_meta.values()))["gathered"] == 1
        # The reader owns the deletion; nothing else unlinks it.
        assert not meta_path.exists()

    def test_an_absent_meta_file_still_archives_with_empty_provenance(
        self, tmp_path,
    ):
        # The best-effort half, pinned so the assertion above cannot be
        # satisfied by making a missing file fatal.
        from istota.scheduler import _maybe_archive_briefing

        cfg = _config(tmp_path)
        cfg.temp_dir = tmp_path / "temp"
        _ctx_with_blocks(cfg, [{"title": "Notes", "sources": [{"kind": "notes"}]}])
        db.init_db(cfg.db_path)

        _maybe_archive_briefing(
            cfg, self._task(id=12, created_at="2026-07-27 06:00:00"),
            "raw result", {"body": "the news"},
        )

        ctx = resolve_for_user("alice", cfg)
        with bdb.connect(ctx.db_path) as conn:
            rows = bdb.list_archive(conn, briefing_name="M")
        assert len(rows) == 1
        assert rows[0].block_meta == {}

    def test_an_unresolvable_control_dir_archives_with_empty_provenance(
        self, tmp_path,
    ):
        # `get_task_control_dir` returns None when the control root is not a
        # directory the daemon owns — a symlink planted at `.control` is the
        # reachable case, since the resolver's containment equality resolves
        # through it and fails. The reader then has no path to try, and what
        # this pins is that the *rest* of the archive still happens: the row
        # is written, with empty provenance, and nothing escapes.
        #
        # What it deliberately does not claim to cover is the
        # `if control_dir else None` guard itself. Measured: removing that
        # guard leaves this case green, because the `None /` `TypeError` lands
        # in the same bare `except` that a missing file lands in. Two causes,
        # one indistinguishable outcome — the shape this whole stage exists
        # to work around, showing up one more time in its own test.
        import os

        from istota.executor import get_task_control_dir
        from istota.scheduler import _maybe_archive_briefing

        cfg = _config(tmp_path)
        cfg.temp_dir = tmp_path / "temp"
        cfg.temp_dir.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, cfg.temp_dir / ".control")
        _ctx_with_blocks(cfg, [{"title": "Notes", "sources": [{"kind": "notes"}]}])
        db.init_db(cfg.db_path)

        task = self._task(id=13, created_at="2026-07-27 06:00:00")
        assert get_task_control_dir(cfg, "alice", 13) is None, (
            "the resolver still resolved; the None branch is not under test"
        )

        _maybe_archive_briefing(cfg, task, "raw result", {"body": "the news"})

        ctx = resolve_for_user("alice", cfg)
        with bdb.connect(ctx.db_path) as conn:
            rows = bdb.list_archive(conn, briefing_name="M")
        assert len(rows) == 1
        assert rows[0].block_meta == {}

    def test_skips_legacy_no_blocks(self, tmp_path):
        from istota.scheduler import _maybe_archive_briefing

        cfg = _config(tmp_path)
        cfg.temp_dir = tmp_path / "temp"
        ctx = resolve_for_user("alice", cfg)
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
        cfg.users["alice"].disabled_modules = ["briefings"]
        db.init_db(cfg.db_path)
        # Must not raise even though no module DB / ctx.
        _maybe_archive_briefing(
            cfg, self._task(), "raw", {"subject": "x", "body": "y"},
        )


class TestExecutorRouting:
    def _task(self, **kw):
        from istota.db import Task
        defaults = dict(
            id=1, status="running", source_type="briefing", user_id="alice",
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
        ctx = resolve_for_user("alice", cfg)
        ensure_initialised(ctx, app_config=cfg)
        db.init_db(cfg.db_path)

        task = self._task()
        assert build_deferred_briefing_prompt(task, cfg) is None

    def test_module_disabled_returns_none(self, tmp_path):
        # Module disabled for the user → None (no legacy fallback).
        from istota.executor import build_deferred_briefing_prompt

        cfg = _config(tmp_path)
        cfg.temp_dir = tmp_path / "temp"
        cfg.users["alice"].disabled_modules = ["briefings"]
        task = self._task()
        assert build_deferred_briefing_prompt(task, cfg) is None


class TestNewsletterInlineLinks:
    """Inline `[anchor](url)` links in a newsletter body must reach the model."""

    def test_render_source_keeps_inline_links(self):
        from istota.briefings.generate import _render_source
        from istota.briefings.sources import GatheredSource

        gs = GatheredSource(
            kind="email", title="Newsletters",
            items=[{
                "sender": "news@semafor.com", "subject": "Flagship",
                "body": (
                    "[Iran tensions escalate](https://semafor.com/a/iran)\n"
                    "Tehran warned that forces have their fingers on the trigger."
                ),
            }],
            provenance="1 newsletters (past 12h)",
        )
        rendered = _render_source(gs)
        assert "[Iran tensions escalate](https://semafor.com/a/iran)" in rendered

    def test_default_directive_covers_both_link_shapes(self):
        """The directive must name the newsletter inline form, not only RSS's."""
        from istota.briefings.generate import _default_directive
        from istota.briefings.models import BriefingBlock

        block = BriefingBlock(
            id=1, briefing_name="M", position=0, title="World",
            render_mode="synthesis",
        )
        directive = _default_directive(block)
        assert "[article: <url>]" in directive
        assert "[text](url)" in directive

    def test_assembled_prompt_carries_a_newsletter_article_url(
        self, tmp_path, monkeypatch,
    ):
        """End-to-end: an HTML newsletter's article URL survives into the prompt."""
        import istota.briefings.sources.email as email_mod

        cfg = _config(tmp_path)
        cfg.users["alice"].email_addresses = ["alice@x.com"]
        cfg.email.enabled = True
        cfg.email.imap_host = "imap.x"
        cfg.email.bot_email = "bot@x.com"

        class _Env:
            id = "1"
            sender = "news@semafor.com"
            subject = "Flagship"
            date = None
            snippet = "snip"
            to = ()
            cc = ()
            references = None

        class _Full:
            id = "1"
            body = (
                "<html><body><div>"
                '<a href="https://link.semafor.com/click/abc?url='
                'https%3A%2F%2Fsemafor.com%2Fa%2Firan">Iran tensions</a>'
                "</div><p>Tehran warned.</p>"
                '<div><a href="https://link.semafor.com/unsubscribe/z">Unsubscribe</a>'
                "</div></body></html>"
            )

        monkeypatch.setattr("istota.mail.support.get_email_config", lambda c: cfg.email)
        monkeypatch.setattr("istota.skills.email.list_emails", lambda **kw: [_Env()])
        monkeypatch.setattr(
            "istota.skills.email.fetch_emails_full", lambda **kw: [_Full()],
        )
        monkeypatch.setattr(
            "istota.mail.ownership.resolve_email_owner", lambda config, conn, e: None,
        )
        # The dispatcher caches resolver modules; call through it so the block
        # wiring is exercised, with the module's own names patched above.
        monkeypatch.setattr(email_mod, "resolve", email_mod.resolve)

        ctx = _ctx_with_blocks(cfg, [{
            "title": "World",
            "sources": [{"kind": "email", "config": {"mode": "shared"}}],
        }])
        with db.get_db(cfg.db_path) as conn:
            result = assemble_briefing_input(ctx, "M", cfg, conn=conn)

        assert result is not None
        assert "[Iran tensions](https://semafor.com/a/iran)" in result.prompt
        assert "unsubscribe/z" not in result.prompt


class TestGenerationHandoff:
    def _setup(self, tmp_path, monkeypatch, *, output="email,ntfy"):
        from istota.briefings.sources import GatheredSource
        from istota.config import BriefingConfig

        cfg = _config(tmp_path)
        cfg.temp_dir = tmp_path / "temp"
        cfg.scheduler.worker_heartbeat_seconds = 0
        cfg.users["alice"].timezone = "America/Los_Angeles"
        cfg.users["alice"].briefings = [BriefingConfig(name="M", cron="0 6 * * *", title="Saved edition")]
        ctx = _ctx_with_blocks(cfg, [
            {"title": "Same title", "options": {"presentation": {"width": "wide"}},
             "sources": [{"kind": "notes"}, {"kind": "browse"}]},
            {"title": "Same title", "render_mode": "structured",
             "sources": [{"kind": "calendar"}]},
            {"title": "Empty", "sources": [{"kind": "todos"}]},
        ])
        db.init_db(cfg.db_path)

        def gather(kind, source_config, source_ctx):
            return GatheredSource(
                kind=kind, title=kind, text="input" if kind in ("notes", "calendar") else "",
                ok=kind != "browse", provenance="private failure" if kind == "browse" else "",
            )

        monkeypatch.setattr("istota.briefings.generate.resolve_source", gather)
        with db.get_db(cfg.db_path) as conn:
            tid = db.create_task(conn, prompt="placeholder", user_id="alice", source_type="briefing",
                                 briefing_name="M", output_target=output)
            conn.execute("UPDATE tasks SET created_at = '2026-10-15 01:00:00' WHERE id = ?", (tid,))
            conn.commit()
            task = db.get_task(conn, tid)
        return cfg, ctx, task

    def test_manifest_uses_ids_order_status_and_task_date(self, tmp_path, monkeypatch):
        from istota.executor import build_deferred_briefing_prompt, get_task_control_dir

        cfg, ctx, task = self._setup(tmp_path, monkeypatch)
        prompt = build_deferred_briefing_prompt(task, cfg)
        meta = json.loads((get_task_control_dir(cfg, "alice", task.id) / "briefing_meta.json").read_text())
        manifest = meta["manifest"]
        assert manifest["version"] == 1
        assert manifest["title"] == "Saved edition — Wednesday, 14 October"
        with bdb.connect(ctx.db_path) as conn:
            blocks = bdb.list_blocks(conn, "M")
        assert [b["block_id"] for b in manifest["blocks"]] == [b.id for b in blocks[:2]]
        assert [b["source_status"] for b in manifest["blocks"]] == ["partial", "complete"]
        assert manifest["blocks"][0]["presentation"] == {"width": "wide"}
        assert manifest["blocks"][0]["source_count"] == 2
        assert manifest["blocks"][1]["render_mode"] == "structured"
        assert set(meta["block_meta"]) == {str(b.id) for b in blocks}
        assert '"version": 2' in prompt and '"body_md"' in prompt
        assert f"Block ID: {blocks[0].id}" in prompt
        assert "private failure" not in json.dumps(manifest)

    @pytest.mark.parametrize("failure", ["write", "invalid"])
    def test_manifest_failure_stops_preparation(self, tmp_path, monkeypatch, failure):
        from istota.executor import execute_task

        cfg, ctx, task = self._setup(tmp_path, monkeypatch)
        if failure == "write":
            monkeypatch.setattr("istota.executor._write_control_file", Mock(side_effect=OSError("unavailable")))
        else:
            monkeypatch.setattr("istota.briefings.generate.presentation_for_options", lambda options: {"width": "bad"})
        # dry_run would return a composed prompt if preparation reached the brain seam.
        success, text, _, _ = execute_task(task, cfg, [], dry_run=True)
        assert success is False
        assert "briefing prompt build failed" in text

    @pytest.mark.parametrize("change", ["edit", "delete", "archive_failure"])
    def test_completion_keeps_snapshot_after_definition_changes(self, tmp_path, monkeypatch, change):
        from istota import scheduler
        from istota.briefings.document import document_to_markdown
        from istota.executor import build_deferred_briefing_prompt, get_task_control_dir

        cfg, ctx, task = self._setup(tmp_path, monkeypatch, output="email,ntfy,talk:briefing-room,web:web-room")
        cfg.talk.enabled = True
        email = AsyncMock(return_value=True)
        talk = AsyncMock(return_value=123)
        web = AsyncMock()
        monkeypatch.setattr(scheduler, "post_result_to_talk", talk)
        monkeypatch.setattr("istota.transport.web.WebTransport.deliver", web)
        if change == "archive_failure":
            monkeypatch.setattr(bdb, "insert_archive", Mock(side_effect=OSError("archive unavailable")))
        ntfy = AsyncMock()
        monkeypatch.setattr(scheduler, "post_result_to_email", email)
        monkeypatch.setattr("istota.transport.ntfy.NtfyTransport.deliver", ntfy)
        monkeypatch.setattr("istota.transport.ntfy.ntfy_settings", lambda *args: {"topic": "briefing"})
        indexed = Mock()
        monkeypatch.setattr("istota.memory.search.index_conversation", indexed)
        cfg.memory_search.enabled = True
        cfg.memory_search.auto_index_conversations = True

        def model(current, config, resources, **kwargs):
            assert build_deferred_briefing_prompt(current, config)
            meta_path = get_task_control_dir(config, "alice", current.id) / "briefing_meta.json"
            manifest = json.loads(meta_path.read_text())["manifest"]
            with bdb.connect(ctx.db_path) as conn:
                for block in bdb.list_blocks(conn, "M"):
                    if change == "delete":
                        bdb.delete_block(conn, block.id)
                    else:
                        bdb.update_block(conn, block.id, title="Changed block", options={"presentation": {"width": "full"}})
                conn.commit()
            cfg.users["alice"].briefings[0].title = "Changed while generating"
            entries = [{"block_id": b["block_id"], "body_md": f"[Story {i}](https://example.com/{i})",
                        "title": "model title", "presentation": {"width": "full"}}
                       for i, b in enumerate(manifest["blocks"])]
            return True, json.dumps({"version": 2, "blocks": entries[::-1]}), None, None

        monkeypatch.setattr(scheduler, "execute_task", model)
        assert scheduler.process_one_task(cfg) == (task.id, True)
        with db.get_db(cfg.db_path) as conn:
            stored = db.get_task(conn, task.id)
        canonical = json.loads(stored.result)
        document = canonical["briefing_document"]
        assert document["title"] == "Saved edition — Wednesday, 14 October"
        assert [b["body_md"] for b in document["blocks"]] == [
            "[Story 0](https://example.com/0)", "[Story 1](https://example.com/1)",
        ]
        assert [b["title"] for b in document["blocks"]] == ["Same title", "Same title"]
        assert document["blocks"][0]["presentation"] == {"width": "wide"}
        body = document_to_markdown(document)
        assert canonical["body"] == body
        assert indexed.call_args.args[4] == body
        with bdb.connect(ctx.db_path) as conn:
            archived = bdb.list_archive(conn, briefing_name="M")
        if change == "archive_failure":
            assert archived == []
        else:
            assert len(archived) == 1
            assert archived[0].document == document
            assert archived[0].body_md == body
            assert archived[0].subject == document["title"]
        assert not (get_task_control_dir(cfg, "alice", task.id) / "briefing_meta.json").exists()
        talk.assert_awaited_once()
        assert talk.call_args.args[2] == body
        web.assert_awaited_once()
        assert web.call_args.args[1] == body
        email.assert_awaited_once()
        assert email.call_args.args[2] == body
        assert email.call_args.kwargs["subject"] == document["title"]
        assert email.call_args.kwargs["briefing_document"] == document
        ntfy.assert_awaited_once()
        assert ntfy.call_args.args[1] == body
        assert ntfy.call_args.kwargs["options"].title == document["title"]

    def test_cross_surface_archive_preview_and_mime_acceptance(self, tmp_path, monkeypatch):
        from pathlib import Path
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from istota import scheduler
        from istota.briefings.routes import get_user_context, require_auth, router, verify_origin
        from istota.executor import build_deferred_briefing_prompt, get_task_control_dir

        fixture_path = Path(__file__).parents[1] / "web/src/lib/components/briefings/edition.acceptance.json"
        fixture = json.loads(fixture_path.read_text())
        expected = fixture["edition"]["document"]
        cfg, ctx, task = self._setup(tmp_path, monkeypatch, output="email")
        cfg.email.enabled = True
        cfg.email.bot_email = "bot@example.com"
        cfg.users["alice"].email_addresses = ["alice@example.com"]
        with bdb.connect(ctx.db_path) as conn:
            for block in expected["blocks"]:
                bdb.update_block(conn, block["block_id"], title=block["title"])
            conn.commit()
        monkeypatch.setattr(bdb, "_now_iso", lambda: "2026-10-15T07:00:00+00:00")

        def model(current, config, resources, **kwargs):
            prompt = build_deferred_briefing_prompt(current, config)
            assert prompt and '"version": 2' in prompt
            manifest_path = get_task_control_dir(config, "alice", current.id) / "briefing_meta.json"
            manifest = json.loads(manifest_path.read_text())["manifest"]
            assert [block["block_id"] for block in manifest["blocks"]] == [1, 2]
            with bdb.connect(ctx.db_path) as conn:
                for block in bdb.list_blocks(conn, "M"):
                    bdb.delete_block(conn, block.id)
                replacement = bdb.add_block(conn, briefing_name="M", title="Unrelated new block")
                assert replacement > 3
                conn.commit()
            cfg.users["alice"].briefings[0].title = "Changed while generating"
            output = {"version": 2, "blocks": [
                {"block_id": block["block_id"], "body_md": block["body_md"], "title": "Model title"}
                for block in reversed(expected["blocks"])
            ]}
            return True, json.dumps(output), None, None

        monkeypatch.setattr(scheduler, "execute_task", model)
        smtp = MagicMock()
        monkeypatch.setattr("smtplib.SMTP", lambda *args, **kwargs: smtp)
        monkeypatch.setattr("istota.skills.email._save_to_sent", Mock())
        assert scheduler.process_one_task(cfg) == (task.id, True)
        smtp.send_message.assert_called_once()
        message = smtp.send_message.call_args.args[0]
        assert str(message["Subject"]) == expected["title"]
        assert message.get_content_type() == "multipart/alternative"
        parts = list(message.iter_parts())
        assert [part.get_content_type() for part in parts] == ["text/plain", "text/html"]
        assert parts[0].get_content().rstrip("\n") == fixture["preview"]["plain"]
        assert parts[1].get_content().rstrip("\n") == fixture["preview"]["html"]
        app = FastAPI()
        app.state.istota_config = cfg
        app.include_router(router, prefix="/istota/api/briefings")
        app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
        app.dependency_overrides[get_user_context] = lambda: ctx
        app.dependency_overrides[verify_origin] = lambda: None
        with TestClient(app) as client:
            archived = client.get("/istota/api/briefings/archive/1")
            assert archived.status_code == 200
            preview = client.post("/istota/api/briefings/preview", json={"document": archived.json()["document"]})
            assert preview.status_code == 200
            assert {"edition": archived.json(), "preview": preview.json()} == fixture
        with db.get_db(cfg.db_path) as conn:
            canonical = json.loads(db.get_task(conn, task.id).result)
        assert canonical["briefing_document"] == expected
        assert canonical["body"] == fixture["edition"]["body_md"]

    @pytest.mark.parametrize("output,missing_manifest,reason", [
        ({"version": 2, "blocks": []}, False, "briefing_output_missing_block"),
        ({"version": 3, "blocks": []}, False, "briefing_output_version"),
        ({"body": "legacy response to new prompt"}, False, "briefing_output_version"),
        ("raw model text", False, "briefing_output_version"),
        ({"version": 2, "blocks": []}, True, "briefing_manifest_missing"),
        ("empty_blocks", False, "briefing_no_content"),
    ])
    def test_invalid_output_never_completes_or_delivers(self, tmp_path, monkeypatch, output, missing_manifest, reason):
        from istota import scheduler
        from istota.executor import build_deferred_briefing_prompt, get_task_control_dir

        cfg, ctx, task = self._setup(tmp_path, monkeypatch)
        email = AsyncMock(return_value=True)
        monkeypatch.setattr(scheduler, "post_result_to_email", email)
        digest = Mock()
        monkeypatch.setattr("istota.skills.briefing.save_briefing_digest", digest)

        def model(current, config, resources, **kwargs):
            assert build_deferred_briefing_prompt(current, config)
            path = get_task_control_dir(config, "alice", current.id) / "briefing_meta.json"
            response = output
            if output == "empty_blocks":
                manifest = json.loads(path.read_text())["manifest"]
                response = {"version": 2, "blocks": [{"block_id": b["block_id"], "body_md": ""}
                                                      for b in manifest["blocks"]]}
            if missing_manifest:
                path.unlink()
            return True, response if isinstance(response, str) else json.dumps(response), None, None

        monkeypatch.setattr(scheduler, "execute_task", model)
        assert scheduler.process_one_task(cfg) == (task.id, False)
        with db.get_db(cfg.db_path) as conn:
            stored = db.get_task(conn, task.id)
        assert stored.status in ("pending", "failed")
        assert reason in stored.error
        email.assert_not_called()
        digest.assert_not_called()
        with bdb.connect(ctx.db_path) as conn:
            assert bdb.count_archive(conn) == 0


    def test_legacy_body_cannot_supply_its_own_document(self, tmp_path, monkeypatch):
        from istota import scheduler

        cfg, ctx, task = self._setup(tmp_path, monkeypatch, output="email")
        email = AsyncMock(return_value=True)
        monkeypatch.setattr(scheduler, "post_result_to_email", email)
        response = json.dumps({"body": "legacy content", "briefing_document": {
            "version": 1, "title": "Model title", "blocks": [],
        }})
        monkeypatch.setattr(scheduler, "execute_task", lambda *args, **kwargs: (True, response, None, None))
        assert scheduler.process_one_task(cfg) == (task.id, True)
        with db.get_db(cfg.db_path) as conn:
            stored = db.get_task(conn, task.id)
        assert "briefing_document" not in json.loads(stored.result)
        with bdb.connect(ctx.db_path) as conn:
            archived = bdb.list_archive(conn, briefing_name="M")
        assert len(archived) == 1 and archived[0].document is None
        assert archived[0].body_md == "legacy content"
        assert email.call_args.args[2] == "legacy content"


    def test_retry_replaces_the_protected_manifest(self, tmp_path, monkeypatch):
        from istota.executor import build_deferred_briefing_prompt, get_task_control_dir

        cfg, ctx, task = self._setup(tmp_path, monkeypatch)
        assert build_deferred_briefing_prompt(task, cfg)
        path = get_task_control_dir(cfg, "alice", task.id) / "briefing_meta.json"
        first = json.loads(path.read_text())["manifest"]
        cfg.users["alice"].briefings[0].title = "Next attempt"
        with bdb.connect(ctx.db_path) as conn:
            block_id = first["blocks"][0]["block_id"]
            bdb.update_block(conn, block_id, title="New title", options={"presentation": {"width": "full"}})
            conn.commit()
        assert build_deferred_briefing_prompt(task, cfg)
        second = json.loads(path.read_text())["manifest"]
        assert second["title"] == "Next attempt — Wednesday, 14 October"
        assert second["blocks"][0]["title"] == "New title"
        assert second["blocks"][0]["presentation"] == {"width": "full"}
        assert second["blocks"][0]["block_id"] == block_id
