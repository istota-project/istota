"""`runtime.bare_metal_import`: where an install was imported from, and whether
it is a rehearsal copy.

`istota migrate import-bare-metal` writes its record into the reserved
`_migration` shared-KV namespace last, in the rebase transaction. A rehearsal
copy holds production credentials and data, so the check warns on every run
rather than passing quietly. Every value here is synthetic.
"""

from __future__ import annotations

import json

import pytest

from istota import db, doctor
from istota.doctor import CHECK_SCOPES, CHECKS, DEPLOYMENT, OK, SKIP, WARN, run_checks
from istota.maintenance import bare_metal_import as bmi

NAME = "runtime.bare_metal_import"


def _record(**overrides) -> str:
    record = {
        "source_host": "old-host.example.test",
        "source_release": "v0.43.0",
        "exported_at": "2026-10-10T12:00:00Z",
        "export_mode": "stopped",
        "imported_at": "2026-10-10T13:00:00Z",
        "rehearsal": False,
    }
    record.update(overrides)
    return json.dumps(record)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


def _write(db_path, value: str) -> None:
    with db.get_db(db_path) as conn:
        db.shared_kv_set(conn, bmi.MIGRATION_NAMESPACE, bmi.RECORD_KEY, value, "test")
        conn.commit()


def _run(make_config, db_path):
    [result] = run_checks(make_config(db_path=db_path), only=(NAME,))
    return result


def test_it_is_registered_as_a_deployment_check():
    assert NAME in dict(CHECKS)
    assert CHECK_SCOPES[NAME] == DEPLOYMENT


def test_an_install_that_was_never_imported_skips(make_config, db_path):
    result = _run(make_config, db_path)
    assert result.status == SKIP
    assert result.scope == DEPLOYMENT


def test_no_database_skips_and_creates_none(make_config, tmp_path):
    absent = tmp_path / "absent.db"
    result = _run(make_config, absent)
    assert result.status == SKIP
    assert not absent.exists()


def test_a_live_import_says_where_from_and_when(make_config, db_path):
    _write(db_path, _record())
    result = _run(make_config, db_path)
    assert result.status == OK
    for part in ("old-host.example.test", "v0.43.0", "2026-10-10T13:00:00Z"):
        assert part in result.detail


def test_a_rehearsal_copy_warns_on_every_run(make_config, db_path):
    _write(db_path, _record(rehearsal=True, export_mode="online"))
    first = _run(make_config, db_path)
    second = _run(make_config, db_path)
    for result in (first, second):
        assert result.status == WARN
        assert "rehearsal" in result.detail
        assert "old-host.example.test" in result.detail
        assert result.remedy.strip()


def test_an_unreadable_record_warns_rather_than_raising(make_config, db_path):
    _write(db_path, "{not json")
    result = _run(make_config, db_path)
    assert result.status == WARN
    assert result.remedy.strip()


def test_the_check_never_raises_on_a_broken_database(make_config, tmp_path):
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database at all, just bytes" * 10)
    result = doctor.check_bare_metal_import(make_config(db_path=broken), False)
    assert result.status in (SKIP, WARN)
    if result.status == WARN:
        assert result.remedy.strip()
