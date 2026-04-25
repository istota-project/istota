"""Tests for money module job seeding into istota's scheduler."""

from pathlib import Path

import pytest

from istota import db
from istota.config import Config, ResourceConfig, UserConfig
from istota.cron_loader import (
    CronJob,
    _MODULE_JOB_PREFIX,
    sync_cron_jobs_to_db,
)
from istota.scheduler import _sync_money_module_jobs

pytest.importorskip("money", reason="money extra not installed")
pytest.importorskip("beancount", reason="money requires beancount")

from money.jobs import DEFAULT_JOBS, MODULE_PREFIX, jobs_for_user


def _conn(tmp_path: Path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _money_toml(data_dir: Path, *, with_invoicing: bool, with_monarch: bool) -> Path:
    """Write a minimal money config TOML and required referenced files."""
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


def _make_app_config(tmp_path: Path, users: dict[str, list[ResourceConfig]]) -> Config:
    return Config(
        db_path=tmp_path / "istota.db",
        temp_dir=tmp_path / "tmp",
        users={
            uid: UserConfig(resources=resources)
            for uid, resources in users.items()
        },
    )


# ---------------------------------------------------------------------------
# jobs_for_user — pure logic
# ---------------------------------------------------------------------------


class TestJobsForUser:
    def test_filters_monarch_when_no_monarch_config(self, tmp_path):
        from money.cli import load_context
        cfg = _money_toml(tmp_path, with_invoicing=True, with_monarch=False)
        ctx = load_context(str(cfg))
        jobs = jobs_for_user(ctx.users["alice"], str(cfg), "alice")
        names = [j["name"] for j in jobs]
        assert f"{MODULE_PREFIX}run_scheduled" in names
        assert f"{MODULE_PREFIX}monarch_sync" not in names

    def test_filters_invoicing_when_no_invoicing_config(self, tmp_path):
        from money.cli import load_context
        cfg = _money_toml(tmp_path, with_invoicing=False, with_monarch=True)
        ctx = load_context(str(cfg))
        jobs = jobs_for_user(ctx.users["alice"], str(cfg), "alice")
        names = [j["name"] for j in jobs]
        assert f"{MODULE_PREFIX}monarch_sync" in names
        assert f"{MODULE_PREFIX}run_scheduled" not in names

    def test_returns_both_when_fully_configured(self, tmp_path):
        from money.cli import load_context
        cfg = _money_toml(tmp_path, with_invoicing=True, with_monarch=True)
        ctx = load_context(str(cfg))
        jobs = jobs_for_user(ctx.users["alice"], str(cfg), "alice")
        assert len(jobs) == 2

    def test_command_includes_config_path_and_user_key(self, tmp_path):
        from money.cli import load_context
        cfg = _money_toml(tmp_path, with_invoicing=True, with_monarch=True)
        ctx = load_context(str(cfg))
        jobs = jobs_for_user(ctx.users["alice"], str(cfg), "alice")
        for j in jobs:
            assert str(cfg) in j["command"]
            assert "--user alice" in j["command"]

    def test_secrets_path_threaded_into_command(self, tmp_path):
        from money.cli import load_context
        cfg = _money_toml(tmp_path, with_invoicing=True, with_monarch=True)
        ctx = load_context(str(cfg))
        jobs = jobs_for_user(
            ctx.users["alice"], str(cfg), "alice",
            secrets_path="/etc/istota/secrets/alice/money.toml",
        )
        for j in jobs:
            assert "MONEY_SECRETS_FILE=/etc/istota/secrets/alice/money.toml" in j["command"]

    def test_no_secrets_env_when_path_omitted(self, tmp_path):
        from money.cli import load_context
        cfg = _money_toml(tmp_path, with_invoicing=True, with_monarch=True)
        ctx = load_context(str(cfg))
        jobs = jobs_for_user(ctx.users["alice"], str(cfg), "alice")
        for j in jobs:
            assert "MONEY_SECRETS_FILE" not in j["command"]


# ---------------------------------------------------------------------------
# _sync_money_module_jobs — integration with DB
# ---------------------------------------------------------------------------


class TestSyncMoneyModuleJobs:
    def test_seeds_jobs_for_user_with_money_resource(self, tmp_path):
        cfg = _money_toml(tmp_path, with_invoicing=True, with_monarch=True)
        app_config = _make_app_config(
            tmp_path,
            {"alice": [ResourceConfig(type="money", extra={"config_path": str(cfg)})]},
        )
        conn = _conn(tmp_path)
        _sync_money_module_jobs(conn, app_config)
        rows = conn.execute(
            "SELECT name FROM scheduled_jobs WHERE user_id = ? ORDER BY name",
            ("alice",),
        ).fetchall()
        names = [r[0] for r in rows]
        assert f"{MODULE_PREFIX}monarch_sync" in names
        assert f"{MODULE_PREFIX}run_scheduled" in names

    def test_user_without_money_resource_has_no_module_jobs(self, tmp_path):
        app_config = _make_app_config(tmp_path, {"bob": []})
        conn = _conn(tmp_path)
        _sync_money_module_jobs(conn, app_config)
        rows = conn.execute(
            "SELECT 1 FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
            ("bob", f"{MODULE_PREFIX}%"),
        ).fetchall()
        assert rows == []

    def test_idempotent_no_duplicate_inserts(self, tmp_path):
        cfg = _money_toml(tmp_path, with_invoicing=True, with_monarch=True)
        app_config = _make_app_config(
            tmp_path,
            {"alice": [ResourceConfig(type="money", extra={"config_path": str(cfg)})]},
        )
        conn = _conn(tmp_path)
        _sync_money_module_jobs(conn, app_config)
        _sync_money_module_jobs(conn, app_config)
        count = conn.execute(
            "SELECT COUNT(*) FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchone()[0]
        assert count == 2  # exactly the two default jobs

    def test_removes_module_jobs_when_resource_disappears(self, tmp_path):
        cfg = _money_toml(tmp_path, with_invoicing=True, with_monarch=True)
        app_config = _make_app_config(
            tmp_path,
            {"alice": [ResourceConfig(type="money", extra={"config_path": str(cfg)})]},
        )
        conn = _conn(tmp_path)
        _sync_money_module_jobs(conn, app_config)
        # Now drop the resource
        app_config2 = _make_app_config(tmp_path, {"alice": []})
        _sync_money_module_jobs(conn, app_config2)
        rows = conn.execute(
            "SELECT 1 FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchall()
        assert rows == []

    def test_removes_obsolete_job_when_feature_removed(self, tmp_path):
        # Start with both monarch + invoicing
        cfg_full = _money_toml(tmp_path, with_invoicing=True, with_monarch=True)
        app_config = _make_app_config(
            tmp_path,
            {"alice": [ResourceConfig(type="money", extra={"config_path": str(cfg_full)})]},
        )
        conn = _conn(tmp_path)
        _sync_money_module_jobs(conn, app_config)
        assert conn.execute(
            "SELECT COUNT(*) FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchone()[0] == 2

        # Now point at a config without monarch
        cfg_inv_only_dir = tmp_path / "alt"
        cfg_inv_only_dir.mkdir()
        cfg_inv_only = _money_toml(cfg_inv_only_dir, with_invoicing=True, with_monarch=False)
        app_config2 = _make_app_config(
            tmp_path,
            {"alice": [ResourceConfig(type="money", extra={"config_path": str(cfg_inv_only)})]},
        )
        _sync_money_module_jobs(conn, app_config2)
        names = [
            r[0] for r in conn.execute(
                "SELECT name FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
                ("alice", f"{MODULE_PREFIX}%"),
            ).fetchall()
        ]
        assert names == [f"{MODULE_PREFIX}run_scheduled"]

    def test_legacy_moneyman_resource_type_accepted(self, tmp_path):
        cfg = _money_toml(tmp_path, with_invoicing=True, with_monarch=True)
        app_config = _make_app_config(
            tmp_path,
            {"alice": [ResourceConfig(type="moneyman", extra={"config_path": str(cfg)})]},
        )
        conn = _conn(tmp_path)
        _sync_money_module_jobs(conn, app_config)
        count = conn.execute(
            "SELECT COUNT(*) FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchone()[0]
        assert count == 2


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
