"""ISSUE-723: daemon and web writers into a user's tree that followed a link.

A task can write inside its user's tree, so a link it plants there redirects a
later daemon or web write. Each class plants one at the place the issue names
and requires the write to be refused, or to land in the tree, never through it.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from istota import db
from istota.config import Config
from istota.health._migrate import ensure_initialised
from istota.health.workspace import synthesize_health_context


def _swap_for_link(directory: Path, outside: Path) -> Path:
    """Move ``directory`` aside and leave a link to ``outside`` in its place."""
    aside = directory.with_name(directory.name + ".real")
    directory.rename(aside)
    directory.symlink_to(outside, target_is_directory=True)
    return aside


@pytest.fixture
def outside(tmp_path):
    d = tmp_path / "outside"
    d.mkdir()
    return d


# --- health ----------------------------------------------------------------


@pytest.fixture
def health_ctx(tmp_path):
    ctx = synthesize_health_context("alice", tmp_path / "workspace")
    ensure_initialised(ctx)
    return ctx


class TestHealthUploadsRoot:
    """`open_or_create_dirs` trusted `{workspace}/health/uploads` itself, so a
    link there sent a lab upload anywhere."""

    def test_the_panel_upload_does_not_follow_a_linked_uploads_dir(
        self, health_ctx, outside,
    ):
        from istota.health.routes import get_user_context, require_auth, router

        _swap_for_link(health_ctx.uploads_dir, outside)
        app = FastAPI()
        app.include_router(router, prefix="/istota/api/health")
        app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
        app.dependency_overrides[get_user_context] = lambda: health_ctx

        resp = TestClient(app).post(
            "/istota/api/health/panels/upload",
            files={"file": ("report.pdf", b"%PDF-1.4 fake", "application/pdf")},
            data={"drawn_at": "2026-05-08", "lab_name": "Quest"},
        )

        assert resp.status_code == 500
        assert list(outside.iterdir()) == []

    def test_an_ordinary_upload_still_lands(self, health_ctx):
        from istota.health.routes import get_user_context, require_auth, router

        app = FastAPI()
        app.include_router(router, prefix="/istota/api/health")
        app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
        app.dependency_overrides[get_user_context] = lambda: health_ctx

        resp = TestClient(app).post(
            "/istota/api/health/panels/upload",
            files={"file": ("report.pdf", b"%PDF-1.4 fake", "application/pdf")},
            data={"drawn_at": "2026-05-08", "lab_name": "Quest"},
        )

        assert resp.status_code == 200, resp.text
        pid = resp.json()["id"]
        assert (health_ctx.uploads_dir / str(pid) / "original.pdf").read_bytes() == b"%PDF-1.4 fake"


class TestHealthDocumentWrite:
    """`store_document` mkdir'd and published under `uploads/documents/<id>`
    following every link above the leaf."""

    def test_a_linked_documents_dir_gets_no_bytes(self, health_ctx, outside):
        from istota.health import db as health_db
        from istota.health import documents

        docs = health_ctx.uploads_dir / "documents"
        docs.mkdir(parents=True, exist_ok=True)
        _swap_for_link(docs, outside)

        with health_db.connect(health_ctx.db_path) as conn:
            with pytest.raises(OSError):
                documents.store_document(
                    conn, health_ctx, raw=b"%PDF-1.4 doc", filename="a.pdf",
                    mime="application/pdf",
                )
        assert list(outside.iterdir()) == []

    def test_an_ordinary_document_is_stored_and_deleted(self, health_ctx):
        from istota.health import db as health_db
        from istota.health import documents

        with health_db.connect(health_ctx.db_path) as conn:
            doc, created = documents.store_document(
                conn, health_ctx, raw=b"%PDF-1.4 doc", filename="a.pdf",
                mime="application/pdf",
            )
            assert created
            path = health_ctx.uploads_dir / doc.stored_path
            assert path.read_bytes() == b"%PDF-1.4 doc"

            assert documents.delete_document_fully(conn, health_ctx, doc.id)
        assert not path.exists()
        assert not path.parent.exists()


# --- the template_file env source -------------------------------------------


class TestTemplateFileEnvSource:
    def _resolve(self, config, user_path):
        from istota import storage
        from istota.skills._env import EnvContext, _resolve_env_spec
        from istota.skills._types import EnvSpec

        storage._TEST_ISSUE_723_PATH = lambda uid, bot: user_path
        storage._TEST_ISSUE_723_TEMPLATE = "seed for {user_id}\n"
        try:
            spec = EnvSpec(
                var="X", source="template_file",
                user_path_fn="_TEST_ISSUE_723_PATH", template="_TEST_ISSUE_723_TEMPLATE",
            )
            ctx = EnvContext(
                config=config, task=MagicMock(id=1, user_id="alice"),
                user_resources=[], user_config=None, user_temp_dir=None,
                is_admin=True,
            )
            return _resolve_env_spec(spec, ctx)
        finally:
            del storage._TEST_ISSUE_723_PATH
            del storage._TEST_ISSUE_723_TEMPLATE

    def test_a_dangling_link_at_the_template_path_is_not_written_through(
        self, tmp_path, outside,
    ):
        mount = tmp_path / "mount"
        config_dir = mount / "Users" / "alice" / "istota" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "X.md").symlink_to(outside / "planted.md")

        self._resolve(Config(workspace_path=mount), "/Users/alice/istota/config/X.md")

        assert list(outside.iterdir()) == []

    def test_a_linked_directory_above_it_is_refused(self, tmp_path, outside):
        mount = tmp_path / "mount"
        bot = mount / "Users" / "alice" / "istota"
        bot.mkdir(parents=True)
        (bot / "config").symlink_to(outside, target_is_directory=True)

        value = self._resolve(Config(workspace_path=mount), "/Users/alice/istota/config/X.md")

        assert value is None
        assert list(outside.iterdir()) == []

    def test_the_seed_is_created_when_absent(self, tmp_path):
        mount = tmp_path / "mount"
        (mount / "Users" / "alice").mkdir(parents=True)

        value = self._resolve(Config(workspace_path=mount), "/Users/alice/istota/config/X.md")

        assert Path(value).read_text() == "seed for alice\n"


# --- the legacy user migrations -----------------------------------------------


class TestLegacyMigrations:
    @pytest.fixture
    def base(self, tmp_path):
        b = tmp_path / "mount" / "Users" / "alice"
        b.mkdir(parents=True)
        return b

    def test_a_linked_context_memory_is_not_copied_into_user_md(self, base, outside):
        from istota.storage import _migrate_old_layout

        secret = outside / "secret.md"
        secret.write_text("not alice's")
        (base / "context").mkdir()
        (base / "context" / "memory.md").symlink_to(secret)

        _migrate_old_layout(base)

        assert not (base / "USER.md").exists()

    def test_a_linked_context_dir_is_not_read(self, base, outside):
        from istota.storage import _migrate_old_layout

        (outside / "2026-01-28.md").write_text("not alice's")
        (base / "context").symlink_to(outside, target_is_directory=True)

        _migrate_old_layout(base)

        assert not (base / "memories" / "2026-01-28.md").exists()

    def test_a_dangling_link_at_user_md_is_not_written_through(self, base, outside):
        from istota.storage import _migrate_old_layout

        (base / "context").mkdir()
        (base / "context" / "memory.md").write_text("mine")
        (base / "USER.md").symlink_to(outside / "planted.md")

        _migrate_old_layout(base)

        assert list(outside.iterdir()) == []

    def test_a_linked_workspace_dir_receives_no_moved_file(self, base, outside):
        from istota.storage import _migrate_workspace_files

        (base / "workspace").symlink_to(outside, target_is_directory=True)
        (base / "USER.md").write_text("mine")

        _migrate_workspace_files(base)

        assert list(outside.iterdir()) == []
        assert (base / "USER.md").read_text() == "mine"

    def test_a_linked_notes_dir_does_not_become_the_bot_dir(self, tmp_path, base, outside):
        from istota.storage import ensure_user_directories_v2

        (base / "notes").symlink_to(outside, target_is_directory=True)

        ensure_user_directories_v2(Config(workspace_path=tmp_path / "mount"), "alice")

        assert list(outside.iterdir()) == []
        assert not (base / "istota").is_symlink()

    def test_a_linked_bot_scripts_dir_receives_no_moved_script(self, tmp_path, base, outside):
        from istota.storage import ensure_user_directories_v2

        (base / "scripts").mkdir()
        (base / "scripts" / "job.sh").write_text("echo hi")
        (base / "istota").mkdir()
        (base / "istota" / "scripts").symlink_to(outside, target_is_directory=True)

        ensure_user_directories_v2(Config(workspace_path=tmp_path / "mount"), "alice")

        assert list(outside.iterdir()) == []
        assert (base / "scripts" / "job.sh").read_text() == "echo hi"


# --- the money module ---------------------------------------------------------


class TestMoneyLedgerWriters:
    @pytest.fixture
    def ledger(self, tmp_path):
        d = tmp_path / "money" / "ledgers"
        d.mkdir(parents=True)
        p = d / "main.beancount"
        p.write_text("; ledger\n")
        return p

    def test_a_linked_ledger_is_not_appended_through(self, tmp_path, outside):
        from istota.money.core.transactions import append_to_ledger

        victim = outside / "victim.beancount"
        victim.write_text("; untouched\n")
        d = tmp_path / "money" / "ledgers"
        d.mkdir(parents=True)
        (d / "main.beancount").symlink_to(victim)

        with pytest.raises(OSError):
            append_to_ledger(d / "main.beancount", ["2026-01-01 * \"x\""])
        assert victim.read_text() == "; untouched\n"
        assert not (d / "backups").exists() or list((d / "backups").iterdir()) == []

    def test_a_linked_backups_dir_gets_no_backup(self, ledger, outside):
        from istota.money.core.transactions import append_to_ledger

        (ledger.parent / "backups").symlink_to(outside, target_is_directory=True)

        with pytest.raises(OSError):
            append_to_ledger(ledger, ["2026-01-01 * \"x\""])
        assert list(outside.iterdir()) == []
        assert ledger.read_text() == "; ledger\n"

    def test_a_linked_imports_dir_gets_no_staging_file(self, ledger, outside):
        from istota.money.core.transactions import write_import_file

        (ledger.parent / "imports").symlink_to(outside, target_is_directory=True)

        with pytest.raises(OSError):
            write_import_file(ledger, "x_import.beancount", "; staged\n")
        assert list(outside.iterdir()) == []

    def test_an_ordinary_append_backs_up_and_appends(self, ledger):
        from istota.money.core.transactions import append_to_ledger

        append_to_ledger(ledger, ["2026-01-01 * \"x\""])

        assert ledger.read_text().endswith('2026-01-01 * "x"\n')
        (backup,) = (ledger.parent / "backups").iterdir()
        assert backup.read_text() == "; ledger\n"

    def test_the_work_dir_refuses_a_link(self, tmp_path, outside):
        from istota.money.work import _work_dir

        data = tmp_path / "money"
        (data / "invoices").mkdir(parents=True)
        (data / "invoices" / "work").symlink_to(outside, target_is_directory=True)

        with pytest.raises(OSError):
            _work_dir(data)

    def test_the_invoice_counter_is_not_written_through_a_link(self, tmp_path, outside):
        from istota.money.core.invoicing import update_invoice_number

        victim = outside / "victim.toml"
        victim.write_text("next_invoice_number = 1\n")
        link = tmp_path / "INVOICING.md"
        link.symlink_to(victim)

        with pytest.raises(OSError):
            update_invoice_number(link, 7)
        assert victim.read_text() == "next_invoice_number = 1\n"


# --- module contexts ------------------------------------------------------------


class TestModuleWorkspaceContainment:
    def test_a_bot_dir_linked_out_of_the_tree_is_refused(self, tmp_path, outside):
        from istota import module_loader

        mount = tmp_path / "mount"
        (mount / "Users" / "alice").mkdir(parents=True)
        (mount / "Users" / "alice" / "istota").symlink_to(outside, target_is_directory=True)
        config = MagicMock()
        config.workspace_path = mount
        config.bot_dir_name = "istota"
        config.users = {"alice": object()}
        config.is_module_enabled.return_value = True

        with pytest.raises(LookupError):
            module_loader.resolve_module_workspace(
                config, "alice", module="health", error=LookupError,
            )


# --- the sleep cycle --------------------------------------------------------------


@pytest.fixture
def mount_config(tmp_path):
    mount = tmp_path / "mount"
    (mount / "Users" / "alice").mkdir(parents=True)
    db.init_db(tmp_path / "test.db")
    return Config(db_path=tmp_path / "test.db", workspace_path=mount)


class TestSleepCycleWrites:
    """The directory was checked and then written by path: a component swapped
    for a link in between was followed."""

    def test_a_memories_dir_swapped_after_the_check_is_not_followed(
        self, mount_config, outside, monkeypatch,
    ):
        from istota.memory import sleep_cycle
        from istota.workspace import writes

        memories = mount_config.workspace_path / "Users" / "alice" / "memories"
        memories.mkdir()
        real_open = writes.open_or_create_dir_below
        aside = {}

        def open_then_swap(root, path):
            fd = real_open(root, path)
            aside["dir"] = _swap_for_link(Path(path), outside)
            return fd

        monkeypatch.setattr(writes, "open_or_create_dir_below", open_then_swap)

        fd = sleep_cycle._user_dir_for_write(mount_config, "alice", memories)
        try:
            assert sleep_cycle._append_dated_memory(fd, "2026-01-28.md", "- fact") is not None
        finally:
            os.close(fd)

        assert list(outside.iterdir()) == []
        assert (aside["dir"] / "2026-01-28.md").read_text() == "- fact\n"

    def test_a_linked_memories_dir_is_refused(self, mount_config, outside):
        from istota.memory import sleep_cycle

        memories = mount_config.workspace_path / "Users" / "alice" / "memories"
        memories.symlink_to(outside, target_is_directory=True)

        assert sleep_cycle._user_dir_for_write(mount_config, "alice", memories) is None

    def test_a_pinned_playbook_link_is_not_indexed(self, mount_config, outside, monkeypatch):
        from istota.config import MemorySearchConfig, PlaybooksConfig
        from istota.memory import search, sleep_cycle

        mount_config.playbooks = PlaybooksConfig(enabled=True)
        mount_config.memory_search = MemorySearchConfig(
            enabled=True, auto_index_memory_files=True,
        )
        secret = outside / "secret.md"
        secret.write_text("---\npinned: true\n---\nnot alice's\n")
        pb_dir = mount_config.workspace_path / "Users" / "alice" / "istota" / "playbooks"
        pb_dir.mkdir(parents=True)
        slug = sleep_cycle._playbook_slug("Deploy the site")
        (pb_dir / f"{slug}.md").symlink_to(secret)
        indexed = []
        monkeypatch.setattr(
            search, "index_file", lambda conn, uid, path, body, kind: indexed.append(body),
        )

        with db.get_db(mount_config.db_path) as conn:
            sleep_cycle._process_extracted_playbooks(
                mount_config, conn, "alice",
                [{"title": "Deploy the site", "steps": "1. a\n2. b"}], "2026-01-28", None,
            )

        assert not any("not alice's" in body for body in indexed)
        assert secret.read_text().endswith("not alice's\n")


# --- channel memory -----------------------------------------------------------------


class TestChannelMemoryWrite:
    def test_a_token_dir_swapped_after_it_is_opened_is_not_followed(
        self, tmp_path, outside, monkeypatch,
    ):
        from istota import storage
        from istota.workspace import writes

        mount = tmp_path / "mount"
        (mount / "Channels" / "room1").mkdir(parents=True)
        config = Config(workspace_path=mount)
        real_open = writes.open_or_create_dirs
        aside = {}

        def open_then_swap(root, parts):
            fd = real_open(root, parts)
            if list(parts) == ["room1"]:
                aside["dir"] = _swap_for_link(Path(root) / "room1", outside)
            return fd

        monkeypatch.setattr(writes, "open_or_create_dirs", open_then_swap)

        assert storage.write_channel_memory(config, "room1", "notes") is True
        assert list(outside.iterdir()) == []
        assert (aside["dir"] / "CHANNEL.md").read_text() == "notes"


# --- !export --------------------------------------------------------------------------


class TestExportWrite:
    async def test_an_exports_dir_swapped_after_the_check_is_not_followed(
        self, make_config, outside, monkeypatch,
    ):
        from istota import commands, storage
        from istota.commands import CommandContext, cmd_export

        config = make_config()
        db.init_db(config.db_path)
        export_dir = (
            config.workspace_path / "Users" / "alice" / "istota" / "exports" / "conversations"
        )
        real_read = storage.read_regular_file

        def read_then_swap(path, **kw):
            result = real_read(path, **kw)
            if not export_dir.is_symlink():
                _swap_for_link(export_dir, outside)
            return result

        monkeypatch.setattr(storage, "read_regular_file", read_then_swap)
        with db.get_db(config.db_path) as conn:
            tid = db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", source_type="talk",
            )
            db.update_task_status(conn, tid, "completed", result="hi")
            ctx = CommandContext(
                config=config, conn=conn, user_id="alice",
                conversation_token="room1", args="",
            )
            monkeypatch.setattr(commands, "resolve_room_name", _room_name)
            await cmd_export(ctx)

        assert list(outside.iterdir()) == []


async def _room_name(ctx, token):
    return "Room"


class TestInvoicePdfWalk:
    def test_a_linked_invoice_folder_gets_no_pdf(self, tmp_path, outside, monkeypatch):
        import sys
        import types

        from istota.money.core.invoicing import generate_invoice_pdf

        fake = types.ModuleType("weasyprint")
        fake.HTML = lambda string: types.SimpleNamespace(write_pdf=lambda: b"%PDF-1.4 x")
        monkeypatch.setitem(sys.modules, "weasyprint", fake)
        data = tmp_path / "money"
        (data / "invoices").mkdir(parents=True)
        (data / "invoices" / "generated").symlink_to(outside, target_is_directory=True)

        with pytest.raises(OSError):
            generate_invoice_pdf(
                "<p>x</p>", data / "invoices" / "generated" / "2026" / "INV-1.pdf", root=data,
            )
        assert list(outside.iterdir()) == []

    def test_an_ordinary_invoice_is_written(self, tmp_path, monkeypatch):
        import sys
        import types

        from istota.money.core.invoicing import generate_invoice_pdf

        fake = types.ModuleType("weasyprint")
        fake.HTML = lambda string: types.SimpleNamespace(write_pdf=lambda: b"%PDF-1.4 x")
        monkeypatch.setitem(sys.modules, "weasyprint", fake)
        data = tmp_path / "money"
        pdf = data / "invoices" / "generated" / "2026" / "INV-1.pdf"

        generate_invoice_pdf("<p>x</p>", pdf, root=data)

        assert pdf.read_bytes() == b"%PDF-1.4 x"


class TestHealthBotDirSwap:
    def test_a_bot_dir_swapped_after_the_context_is_resolved_gets_no_document(
        self, tmp_path, outside,
    ):
        from istota.health import db as health_db
        from istota.health import documents

        bot = tmp_path / "Users" / "alice" / "istota"
        bot.mkdir(parents=True)
        ctx = synthesize_health_context(
            "alice", bot, db_path=tmp_path / "local" / "health.db",
        )
        ensure_initialised(ctx)
        _swap_for_link(bot, outside)

        with health_db.connect(ctx.db_path) as conn:
            with pytest.raises(OSError):
                documents.store_document(
                    conn, ctx, raw=b"%PDF-1.4 doc", filename="a.pdf",
                    mime="application/pdf",
                )
        assert list(outside.iterdir()) == []
