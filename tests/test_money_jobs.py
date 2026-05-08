"""Tests for money module job seeding into istota's scheduler."""

from pathlib import Path

import pytest

from istota import db
from istota.config import Config, UserConfig
from istota.cron_loader import (
    CronJob,
    _MODULE_JOB_PREFIX,
    sync_cron_jobs_to_db,
)
from istota.scheduler import _sync_money_module_jobs

pytest.importorskip("istota.money", reason="money extra not installed")
pytest.importorskip("beancount", reason="money requires beancount")

from istota.money.jobs import DEFAULT_JOBS, MODULE_PREFIX, jobs_for_user


def _conn(tmp_path: Path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _money_toml(data_dir: Path, *, with_invoicing: bool, with_monarch: bool) -> Path:
    """Write a legacy money config TOML for ``TestJobsForUser`` (pure-logic tests).

    The standalone ``money.cli.load_context`` still accepts this form;
    workspace-mode integration tests should use :func:`_make_workspace`.
    """
    cfg_path = data_dir / "money.toml"
    ledger = data_dir / "main.beancount"
    ledger.write_text("")
    parts = [
        f'data_dir = "{data_dir}"',
        f'db_path = "{data_dir / "money.db"}"',
        "",
        "[users.alice]",
        f'data_dir = "{data_dir}"',
        f'ledgers = [{{name = "main", path = "{ledger}"}}]',
    ]
    if with_invoicing:
        inv = data_dir / "invoicing.toml"
        inv.write_text(
            '[default_entity]\n'
            'name = "Acme"\n'
            'address = "1 Way"\n'
            'email = "x@y.z"\n'
            'tax_id = "0"\n'
        )
        parts.append(f'invoicing_config = "{inv}"')
    if with_monarch:
        mon = data_dir / "monarch.toml"
        mon.write_text('[monarch]\nemail = "x@y.z"\n')
        parts.append(f'monarch_config = "{mon}"')
    cfg_path.write_text("\n".join(parts) + "\n")
    return cfg_path


def _make_workspace(
    mount: Path, user_id: str, *, with_invoicing: bool, with_monarch: bool,
) -> Path:
    """Create a money workspace under ``{mount}/Users/{user_id}/istota``.

    Drops legacy-named ``invoicing.toml`` / ``monarch.toml`` into
    ``{workspace}/money/config/`` (resolved by ``synthesize_user_context``)
    so :func:`jobs_for_user` will see the relevant features as configured.
    The contents are minimal but rich enough to populate the per-user money
    DB collections that ``has_invoicing_data`` / ``has_monarch_data`` look
    at — without that, the post-migration DB-fallback check would still
    return empty after :func:`ensure_initialised` renames the legacy files.
    """
    workspace = mount / "Users" / user_id / "istota"
    cfg_dir = workspace / "money" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (workspace / "money" / "ledgers").mkdir(parents=True, exist_ok=True)
    if with_invoicing:
        (cfg_dir / "invoicing.toml").write_text(
            '[companies.default]\n'
            'name = "Acme"\n'
            'address = "1 Way"\n'
            'email = "x@y.z"\n'
            'tax_id = "0"\n'
        )
    if with_monarch:
        (cfg_dir / "monarch.toml").write_text(
            '[monarch]\nemail = "x@y.z"\n\n'
            '[monarch.profiles.default]\nledger = "main"\n'
        )
    return workspace


def _make_app_config(
    tmp_path: Path, users: list[str], *, mount: Path | None = None,
) -> Config:
    return Config(
        db_path=tmp_path / "istota.db",
        temp_dir=tmp_path / "tmp",
        nextcloud_mount_path=mount,
        users={uid: UserConfig() for uid in users},
    )


# ---------------------------------------------------------------------------
# jobs_for_user — pure logic
# ---------------------------------------------------------------------------


class TestJobsForUser:
    def test_seeds_run_scheduled_when_only_invoicing_configured(self, tmp_path):
        from istota.money.cli import load_context
        cfg = _money_toml(tmp_path, with_invoicing=True, with_monarch=False)
        ctx = load_context(str(cfg))
        jobs = jobs_for_user(ctx.users["alice"], "alice")
        names = [j["name"] for j in jobs]
        assert names == [f"{MODULE_PREFIX}run_scheduled"]

    def test_seeds_run_scheduled_when_only_monarch_configured(self, tmp_path):
        from istota.money.cli import load_context
        cfg = _money_toml(tmp_path, with_invoicing=False, with_monarch=True)
        ctx = load_context(str(cfg))
        jobs = jobs_for_user(ctx.users["alice"], "alice")
        names = [j["name"] for j in jobs]
        assert names == [f"{MODULE_PREFIX}run_scheduled"]

    def test_seeds_run_scheduled_when_fully_configured(self, tmp_path):
        from istota.money.cli import load_context
        cfg = _money_toml(tmp_path, with_invoicing=True, with_monarch=True)
        ctx = load_context(str(cfg))
        jobs = jobs_for_user(ctx.users["alice"], "alice")
        assert len(jobs) == 1
        assert jobs[0]["name"] == f"{MODULE_PREFIX}run_scheduled"

    def test_no_jobs_when_neither_feature_configured(self, tmp_path):
        from istota.money.cli import load_context
        cfg = _money_toml(tmp_path, with_invoicing=False, with_monarch=False)
        ctx = load_context(str(cfg))
        jobs = jobs_for_user(ctx.users["alice"], "alice")
        assert jobs == []

    def test_dispatch_shape_is_skill_task(self, tmp_path):
        """Phase 1.3: jobs are skill-tasks, not shell command-tasks."""
        import json
        from istota.money.cli import load_context
        cfg = _money_toml(tmp_path, with_invoicing=True, with_monarch=True)
        ctx = load_context(str(cfg))
        jobs = jobs_for_user(ctx.users["alice"], "alice")
        for j in jobs:
            assert "command" not in j
            assert j["skill"] == "money"
            assert json.loads(j["skill_args"]) == ["run-scheduled"]


# ---------------------------------------------------------------------------
# _sync_money_module_jobs — integration with DB
# ---------------------------------------------------------------------------


class TestSyncMoneyModuleJobs:
    def test_seeds_run_scheduled_for_configured_user(self, tmp_path):
        mount = tmp_path / "mount"
        _make_workspace(mount, "alice", with_invoicing=True, with_monarch=True)
        app_config = _make_app_config(tmp_path, ["alice"], mount=mount)
        conn = _conn(tmp_path)
        _sync_money_module_jobs(conn, app_config)
        rows = conn.execute(
            "SELECT name FROM scheduled_jobs WHERE user_id = ? ORDER BY name",
            ("alice",),
        ).fetchall()
        names = [r[0] for r in rows]
        assert names == [f"{MODULE_PREFIX}run_scheduled"]

    def test_user_with_module_disabled_has_no_module_jobs(self, tmp_path):
        mount = tmp_path / "mount"
        _make_workspace(mount, "bob", with_invoicing=True, with_monarch=True)
        app_config = _make_app_config(tmp_path, ["bob"], mount=mount)
        app_config.users["bob"].disabled_modules = ["money"]
        conn = _conn(tmp_path)
        _sync_money_module_jobs(conn, app_config)
        rows = conn.execute(
            "SELECT 1 FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
            ("bob", f"{MODULE_PREFIX}%"),
        ).fetchall()
        assert rows == []

    def test_idempotent_no_duplicate_inserts(self, tmp_path):
        mount = tmp_path / "mount"
        _make_workspace(mount, "alice", with_invoicing=True, with_monarch=True)
        app_config = _make_app_config(tmp_path, ["alice"], mount=mount)
        conn = _conn(tmp_path)
        _sync_money_module_jobs(conn, app_config)
        _sync_money_module_jobs(conn, app_config)
        count = conn.execute(
            "SELECT COUNT(*) FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchone()[0]
        assert count == 1

    def test_removes_module_jobs_when_module_disabled(self, tmp_path):
        mount = tmp_path / "mount"
        _make_workspace(mount, "alice", with_invoicing=True, with_monarch=True)
        app_config = _make_app_config(tmp_path, ["alice"], mount=mount)
        conn = _conn(tmp_path)
        _sync_money_module_jobs(conn, app_config)
        # Now disable the module for alice
        app_config.users["alice"].disabled_modules = ["money"]
        _sync_money_module_jobs(conn, app_config)
        rows = conn.execute(
            "SELECT 1 FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchall()
        assert rows == []

    def test_removes_run_scheduled_when_both_features_removed(self, tmp_path):
        # Start with both monarch + invoicing → run_scheduled seeded.
        from istota.money import resolve_for_user
        from istota.money.core.models import (
            CompanyConfig, InvoicingConfig, MonarchCredentials, MonarchConfig,
            MonarchSyncSettings, MonarchTagFilters,
        )
        from istota.money.config_store import save_invoicing, save_monarch

        mount = tmp_path / "mount"
        _make_workspace(mount, "alice", with_invoicing=True, with_monarch=True)
        app_config = _make_app_config(tmp_path, ["alice"], mount=mount)
        conn = _conn(tmp_path)
        _sync_money_module_jobs(conn, app_config)
        assert conn.execute(
            "SELECT COUNT(*) FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchone()[0] == 1

        # ensure_initialised renames the legacy TOMLs to *.imported on first
        # sync, so the file-based detection is already empty. Wipe the
        # DB-backed feature collections to simulate "both features removed."
        ctx = resolve_for_user("alice", app_config)
        empty_invoicing = InvoicingConfig(
            accounting_path="", invoice_output="", next_invoice_number=1,
            company=CompanyConfig(name=""), clients={}, services={},
        )
        empty_monarch = MonarchConfig(
            credentials=MonarchCredentials(),
            sync=MonarchSyncSettings(),
            accounts={}, categories={},
            tags=MonarchTagFilters(),
        )
        save_invoicing(ctx.db_path, empty_invoicing, replace_collections=True)
        save_monarch(ctx.db_path, empty_monarch, replace_collections=True)

        _sync_money_module_jobs(conn, app_config)
        names = [
            r[0] for r in conn.execute(
                "SELECT name FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
                ("alice", f"{MODULE_PREFIX}%"),
            ).fetchall()
        ]
        assert names == []

    def test_seeds_with_skip_log_channel_set(self, tmp_path):
        # Module jobs run on a cadence and emit structured JSON envelopes —
        # they must never post to the user's log channel.
        mount = tmp_path / "mount"
        _make_workspace(mount, "alice", with_invoicing=True, with_monarch=True)
        app_config = _make_app_config(tmp_path, ["alice"], mount=mount)
        conn = _conn(tmp_path)
        _sync_money_module_jobs(conn, app_config)
        row = conn.execute(
            "SELECT skip_log_channel FROM scheduled_jobs "
            "WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchone()
        assert row[0] == 1

    def test_backfills_skip_log_channel_on_existing_row(self, tmp_path):
        # Pre-fix rows have skip_log_channel=0 and should flip to 1 on the
        # next sync. Critically, backfilling must NOT bump last_run_at — for
        # a daily money job that would mean losing one whole day's run.
        # Phase 1.3 also migrates the legacy command shape to skill/skill_args.
        import json
        mount = tmp_path / "mount"
        _make_workspace(mount, "alice", with_invoicing=True, with_monarch=True)
        app_config = _make_app_config(tmp_path, ["alice"], mount=mount)
        conn = _conn(tmp_path)
        original_last_run = "2026-05-03 08:00:01"
        conn.execute(
            "INSERT INTO scheduled_jobs "
            "(user_id, name, cron_expression, prompt, command, "
            "skill, skill_args, enabled, skip_log_channel, last_run_at) "
            "VALUES (?, ?, ?, '', ?, NULL, NULL, 1, 0, ?)",
            ("alice", f"{MODULE_PREFIX}run_scheduled", "0 8 * * *",
             "MONEY_USER=alice istota-skill money run-scheduled",
             original_last_run),
        )
        conn.commit()
        _sync_money_module_jobs(conn, app_config)
        row = conn.execute(
            "SELECT skip_log_channel, last_run_at, command, skill, skill_args "
            "FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchone()
        assert row[0] == 1
        assert row[1] == original_last_run
        assert row[2] is None
        assert row[3] == "money"
        assert json.loads(row[4]) == ["run-scheduled"]


# ---------------------------------------------------------------------------
# CRON.md sync must not touch _module.* jobs
# ---------------------------------------------------------------------------


class TestCronMdLeavesModuleJobsAlone:
    def test_cron_md_orphan_pass_does_not_delete_module_jobs(self, tmp_path):
        conn = _conn(tmp_path)
        # Pre-seed a module job
        conn.execute(
            "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, command) "
            "VALUES (?, ?, ?, '', ?)",
            ("alice", f"{MODULE_PREFIX}monarch_sync", "0 6 * * *", "money sync"),
        )
        conn.commit()
        # Run cron-md sync with empty file_jobs (would normally orphan-delete everything)
        sync_cron_jobs_to_db(conn, "alice", [])
        rows = conn.execute(
            "SELECT 1 FROM scheduled_jobs WHERE user_id = ? AND name = ?",
            ("alice", f"{MODULE_PREFIX}monarch_sync"),
        ).fetchall()
        assert len(rows) == 1

    def test_cron_md_rejects_module_prefixed_user_jobs(self, tmp_path):
        conn = _conn(tmp_path)
        bad = CronJob(
            name=f"{_MODULE_JOB_PREFIX}money.something",
            cron="0 0 * * *",
            prompt="hi",
        )
        # Should be silently skipped (with a warning), not inserted
        sync_cron_jobs_to_db(conn, "alice", [bad])
        rows = conn.execute(
            "SELECT 1 FROM scheduled_jobs WHERE user_id = ?", ("alice",),
        ).fetchall()
        assert rows == []
