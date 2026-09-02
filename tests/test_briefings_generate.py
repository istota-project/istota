"""Tests for briefings generation assembly + archive + executor routing."""

from datetime import datetime, timezone


from istota import db
from istota.briefings import db as bdb
from istota.briefings import ensure_initialised, resolve_for_user
from istota.briefings.generate import archive_briefing, assemble_briefing_input
from istota.config import Config, UserConfig


def _config(tmp_path) -> Config:
    return Config(
        db_path=tmp_path / "istota.db",
        nextcloud_mount_path=tmp_path / "mount",
        users={"alice": UserConfig(timezone="UTC")},
    )


def _write_workspace_file(cfg: Config, filename: str, content: str) -> str:
    from istota.storage import get_user_bot_path
    rel = f"{get_user_bot_path('alice', cfg.bot_dir_name)}/{filename}".lstrip("/")
    p = cfg.nextcloud_mount_path / rel
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
        # The envelope is body-only — the title is computed at delivery, not
        # supplied by the model.
        assert '"body"' in result.prompt and '"subject"' not in result.prompt
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
        assert "Notes" in rows[0].block_meta
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

        monkeypatch.setattr("istota.email_support.get_email_config", lambda c: cfg.email)
        monkeypatch.setattr("istota.skills.email.list_emails", lambda **kw: [_Env()])
        monkeypatch.setattr(
            "istota.skills.email.fetch_emails_full", lambda **kw: [_Full()],
        )
        monkeypatch.setattr(
            "istota.email_ownership.resolve_email_owner", lambda config, conn, e: None,
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
