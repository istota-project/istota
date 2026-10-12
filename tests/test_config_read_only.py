"""Prospective config loads leave database state and seed definitions alone."""

import io
import json

import pytest

from istota import apply, cli, db, user_briefings, user_profiles
from istota.config import load_config
from istota.credentials import store as secrets_store
from istota.lib.toml_write import dumps


def _files(root):
    return {
        path.relative_to(root): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*") if path.is_file()
    }


@pytest.fixture
def installed_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "x" * 64)
    monkeypatch.setenv("ISTOTA_WEB_AUTH", "email")
    monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "admins"))
    db_path = tmp_path / "data" / "istota.db"
    db_path.parent.mkdir()
    db.init_db(db_path)
    user_profiles.ensure_profile(db_path, "alice", display_name="Stored Alice")
    user_profiles.ensure_profile(db_path, "bob", display_name="Stored Bob")
    user_briefings.ensure_briefing(
        db_path, user_id="alice", name="morning", cron="0 8 * * *", output="email",
    )
    with db.get_db(db_path) as conn:
        db.add_user_resource(
            conn, user_id="alice", resource_type="folder",
            resource_path="/Docs", display_name="Stored docs",
        )
        db.add_user_resource(
            conn, user_id="alice", resource_type="overland",
            resource_path="overland", extras={"ingest_token": "example-ingest-value"},
        )
        db.upsert_shared_block_config(conn, name="news", cron="0 8 * * *", title="Stored news")
    document = {
        "db_path": str(db_path),
        "workspace_path": str(tmp_path / "workspace"),
        "default_briefings": [{"name": "daily", "cron": "0 6 * * *", "output": "email"}],
        "briefing_shared_blocks": [{"name": "news", "cron": "0 6 * * *", "title": "File news"}],
        "users": {"alice": {
            "display_name": "File Alice",
            "resources": [{"type": "folder", "path": "/Docs", "name": "File docs"}],
            "briefings": [{"name": "morning", "cron": "0 6 * * *", "output": "email"}],
        }},
    }
    path = tmp_path / "config.toml"
    path.write_text(dumps(document))
    return path, document


def test_read_only_load_preserves_paths_seeds_and_existing_state(installed_config, tmp_path):
    path, document = installed_config
    before = _files(tmp_path)

    config = load_config(path, apply_db_overlays=False)

    assert _files(tmp_path) == before
    assert str(config.db_path) == document["db_path"]
    assert str(config.workspace_path) == document["workspace_path"]
    assert config.config_path == path
    assert config.web.auth == ["email"]
    assert set(config.users) == {"alice"}
    alice = config.users["alice"]
    assert alice.display_name == "File Alice"
    assert [(r.type, r.name) for r in alice.resources] == [("folder", "File docs")]
    assert [(b.name, b.cron) for b in alice.briefings] == [("morning", "0 6 * * *")]
    assert [b.name for b in config.default_briefings] == ["daily"]
    assert [(b.name, b.title) for b in config.briefing_shared_blocks] == [("news", "File news")]


@pytest.mark.parametrize("options", [{}, {"apply_db_overlays": True}], ids=["default", "explicit"])
def test_normal_load_still_overlays_imports_cleans_and_seeds(installed_config, options):
    path, _ = installed_config

    config = load_config(path, **options)

    assert set(config.users) == {"alice", "bob"}
    alice = config.users["alice"]
    assert alice.display_name == "Stored Alice"
    assert [(r.type, r.name) for r in alice.resources] == [("folder", "Stored docs")]
    assert {b.name: b.cron for b in alice.briefings} == {"morning": "0 8 * * *", "daily": "0 6 * * *"}
    assert config.briefing_shared_blocks[0].title == "Stored news"
    with db.get_db(config.db_path) as conn:
        assert [r.resource_type for r in db.get_user_resources(conn, "alice")] == ["folder"]
    assert secrets_store.get_secret(config.db_path, "alice", "overland", "ingest_token") == "example-ingest-value"


@pytest.mark.parametrize("explicit_db_path", [False, True], ids=["default-path", "explicit-path"])
def test_collect_does_not_import_or_cleanup_existing_database(installed_config, tmp_path, explicit_db_path):
    _, document = installed_config
    if not explicit_db_path:
        del document["db_path"]
    before = _files(tmp_path)

    result = apply.collect(document)

    assert not result.refused, result.as_json()
    assert _files(tmp_path) == before


@pytest.mark.parametrize("existing", [False, True], ids=["absent", "corrupt"])
def test_read_only_load_does_not_open_database(tmp_path, existing):
    db_path = tmp_path / "state" / "istota.db"
    if existing:
        db_path.parent.mkdir()
        db_path.write_bytes(b"not a database")
    path = tmp_path / "config.toml"
    path.write_text(dumps({"db_path": str(db_path)}))
    before = _files(tmp_path)

    config = load_config(path, apply_db_overlays=False)

    assert config.db_path == db_path
    assert _files(tmp_path) == before
    assert db_path.parent.exists() is existing


@pytest.mark.parametrize("mode, expected", [("--check", 0), ("--dry-run", 2)])
def test_cli_inspection_leaves_existing_state_untouched(installed_config, tmp_path, monkeypatch, capsys, mode, expected):
    path, document = installed_config
    del document["db_path"]
    document["bot_name"] = "Prospective"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"config": document})))
    monkeypatch.setattr("sys.argv", ["istota", "-c", str(path), "apply", "-f", "-", mode])
    before = _files(tmp_path)

    code = 0
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code or 0

    assert code == expected, capsys.readouterr()
    assert _files(tmp_path) == before
