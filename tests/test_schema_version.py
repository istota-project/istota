"""`PRAGMA user_version`: an older image refuses a database a newer one migrated.

Migrations are idempotent `_migrate*` functions with no record of how far they
went, so before this nothing stopped a rollback's older `istota init` from
running against a schema it does not know. `init_db` now stamps the image's
schema level into the file header, and refuses, before any migration runs, a
database stamped higher than it knows. `istota-stack rollback` asks the older
image first with `istota init --check-schema`, which reads the stamp and writes
nothing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from istota import cli, db


def _user_version(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def _stamp(path: Path, version: int) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version = {int(version)}")


def _schema(path: Path) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


class TestInitStampsTheSchemaLevel:
    def test_a_fresh_database_carries_this_images_level(self, db_path):
        assert db.SCHEMA_VERSION >= 1
        assert _user_version(db_path) == db.SCHEMA_VERSION

    def test_an_older_stamp_is_raised_to_this_level(self, db_path):
        _stamp(db_path, 0)
        db.init_db(db_path)
        assert _user_version(db_path) == db.SCHEMA_VERSION


class TestANewerDatabaseIsRefused:
    def test_init_db_raises_before_touching_it(self, db_path):
        newer = db.SCHEMA_VERSION + 1
        _stamp(db_path, newer)
        # A table this image's migrations would never create, so a migration
        # that ran anyway would not be visible; the stamp and the schema are.
        with sqlite3.connect(db_path) as conn:
            conn.execute("DROP TABLE IF EXISTS task_events")
        before = _schema(db_path)

        with pytest.raises(db.SchemaTooNew) as excinfo:
            db.init_db(db_path)

        assert excinfo.value.found == newer
        assert excinfo.value.known == db.SCHEMA_VERSION
        assert _user_version(db_path) == newer
        assert _schema(db_path) == before, "init_db migrated a database it refused"

    def test_the_check_reads_without_writing(self, db_path):
        assert db.check_schema_version(db_path) is None
        _stamp(db_path, db.SCHEMA_VERSION + 3)
        refusal = db.check_schema_version(db_path)
        assert isinstance(refusal, db.SchemaTooNew)
        assert refusal.found == db.SCHEMA_VERSION + 3

    def test_the_check_answers_none_for_a_missing_database(self, tmp_path):
        missing = tmp_path / "absent.db"
        assert db.check_schema_version(missing) is None
        assert not missing.exists(), "the check created the database it was asked about"


def _invoke(monkeypatch, capsys, config_path: Path, *argv: str) -> tuple[int, str, str]:
    monkeypatch.setattr("sys.argv", ["istota", "-c", str(config_path), *argv])
    code = 0
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code or 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def config_path(tmp_path, db_path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(f'db_path = "{db_path}"\n')
    return path


class TestTheInitCommand:
    def test_init_refuses_a_newer_database_with_one_line(self, monkeypatch, capsys, config_path, db_path):
        _stamp(db_path, db.SCHEMA_VERSION + 1)
        code, out, err = _invoke(monkeypatch, capsys, config_path, "init")
        assert code == cli.EXIT_SCHEMA_TOO_NEW
        assert "Database initialized" not in out
        refusal = [line for line in err.splitlines() if "REFUSE" in line]
        assert len(refusal) == 1, err
        assert str(db.SCHEMA_VERSION + 1) in refusal[0]
        assert str(db.SCHEMA_VERSION) in refusal[0]
        assert _user_version(db_path) == db.SCHEMA_VERSION + 1

    def test_check_schema_passes_an_equal_database(self, monkeypatch, capsys, config_path):
        code, out, _ = _invoke(monkeypatch, capsys, config_path, "init", "--check-schema")
        assert code == 0
        assert "Database initialized" not in out

    def test_check_schema_refuses_a_newer_one(self, monkeypatch, capsys, config_path, db_path):
        _stamp(db_path, db.SCHEMA_VERSION + 1)
        code, _, err = _invoke(monkeypatch, capsys, config_path, "init", "--check-schema")
        assert code == cli.EXIT_SCHEMA_TOO_NEW
        assert "REFUSE" in err

    def test_check_schema_does_not_migrate_an_older_one(self, monkeypatch, capsys, config_path, db_path):
        _stamp(db_path, 0)
        code, _, _ = _invoke(monkeypatch, capsys, config_path, "init", "--check-schema")
        assert code == 0
        assert _user_version(db_path) == 0
