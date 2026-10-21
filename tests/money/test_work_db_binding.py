"""The work store follows the context's ``db_path``, not ``{data_dir}/data``.

On a deployment ``money.db`` is ``Config.module_db_path`` on local disk while
``data_dir`` is the workspace on the mount. Every other money test passes
``data_dir/data/money.db`` as the db_path, which is also the unbound fallback,
so a path that forgot the binding would pass them all.
"""

from __future__ import annotations

import sqlite3
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from istota.config import Config, UserConfig
from istota.money import config_store

CONFIG_TOML = """
accounting_path = "."
invoice_output = "invoices/generated"
next_invoice_number = 1

[company]
name = "My Co"

[clients.acme]
name = "Acme Corp"
terms = 30

[services.dev]
display_name = "Development"
rate = 150
type = "hours"
"""


@pytest.fixture(autouse=True)
def fake_weasyprint(monkeypatch):
    from tests.support import weasyprint_stub

    weasyprint_stub.install(monkeypatch, render=lambda string: b"%PDF-1.4")


def _seed_config(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    config_store.init_db(db_path)
    cfg = config_store.invoicing_config_from_toml_dict(tomllib.loads(CONFIG_TOML))
    config_store.save_invoicing(db_path, cfg, replace_collections=True)


def _counts(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("work_entries", "invoices", "invoice_lines")
        }
    finally:
        conn.close()


def test_core_and_cli_use_the_bound_database(tmp_path):
    from istota.money.cli import UserContext, cli
    from istota.money.core.invoicing import generate_invoices_for_period
    from istota.money.work import add_work_entry, db_path_for, list_invoices

    ws = tmp_path / "ws"
    ws.mkdir()
    local = tmp_path / "local" / "money.db"
    _seed_config(local)
    ctx = UserContext(data_dir=ws, db_path=local)
    assert db_path_for(ws) == local

    add_work_entry(ws, "2026-03-10", "acme", "dev", qty=2, description="core")
    result = CliRunner().invoke(
        cli, ["work", "add", "-d", "2026-03-11", "-c", "acme", "-s", "dev", "-q", "1"],
        obj=_cli_context(ctx), catch_exceptions=False,
    )
    assert '"status": "ok"' in result.output, result.output

    config = config_store.load_invoicing(local)
    (issued,) = generate_invoices_for_period(
        config=config, config_path=None, accounting_path=ws, data_dir=ws,
        invoice_output_dir=ws / config.invoice_output, db_path=local,
    )

    assert _counts(local) == {"work_entries": 2, "invoices": 1, "invoice_lines": 2}
    assert [r.number for r in list_invoices(ws)] == [issued["invoice_number"]]
    assert Path(issued["file"]).is_file()
    assert (local.parent / ".invoice-issue.lock").exists()
    assert not (ws / "data" / "money.db").exists()
    assert not (ws / "data" / ".invoice-issue.lock").exists()


def _cli_context(user_ctx):
    from istota.money.cli import Context

    ctx = Context()
    ctx.users = {"alice": user_ctx}
    ctx.activate_user("alice")
    return ctx


def test_the_routes_use_the_module_database(tmp_path):
    from istota.money import resolve_for_user
    from istota.money.routes import require_auth, router, verify_origin

    mount = tmp_path / "mount"
    mount.mkdir()
    cfg = Config(
        workspace_path=mount,
        workspace_mount_path=mount,
        module_data_dir=tmp_path / "local",
        users={"alice": UserConfig()},
        bot_name="Istota",
        db_path=tmp_path / "istota.db",
    )
    local = cfg.module_db_path("alice", "money")
    _seed_config(local)

    app = FastAPI()
    app.state.istota_config = cfg
    app.include_router(router, prefix="/api/money")
    app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
    app.dependency_overrides[verify_origin] = lambda: None
    client = TestClient(app)

    resp = client.post("/api/money/work", json={
        "date": "2026-03-10", "client": "acme", "service": "dev", "qty": 3,
    })
    assert resp.status_code == 200, resp.text
    resp = client.post("/api/money/invoices/generate", json={"dry_run": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["invoice_count"] == 1
    listed = client.get("/api/money/invoices").json()["invoices"]
    assert listed[0]["has_pdf"] is True

    data_dir = resolve_for_user("alice", cfg).data_dir
    assert _counts(local) == {"work_entries": 1, "invoices": 1, "invoice_lines": 1}
    assert not (data_dir / "data" / "money.db").exists()


def test_a_database_replaced_at_the_same_path_is_initialised_again(tmp_path):
    """A memo keyed on the path alone said "done" for the replacement and left
    it with no work tables (ISSUE-827: the Linux tier reuses a passed test's
    tmp path, so the next parametrization's fresh money.db hit it)."""
    from istota.money import work

    data_dir = tmp_path / "money"
    db_path = data_dir / "data" / "money.db"
    work.add_work_entry(data_dir, "2026-01-02", "acme", "dev", qty=1, description="first")

    held = tmp_path / "held.db"
    db_path.rename(held)  # keeps the old inode alive, so the new file cannot reuse it
    for sidecar in db_path.parent.glob("money.db-*"):
        sidecar.unlink()
    sqlite3.connect(db_path).close()

    work.add_work_entry(data_dir, "2026-01-03", "acme", "dev", qty=1, description="second")
    assert [e.description for e in work.load_work_entries(data_dir)] == ["second"]
