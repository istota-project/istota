"""`sharing.ics_feeds`: the calendar feeds users shared, as counts by state.

A real database: the check reads it read-only and names no address, host or
calendar name.
"""

from __future__ import annotations

from istota import db, doctor
from istota.config import Config
from istota.sharing import sources


def _feed(conn, user, key, *, failing=False):
    source_id = sources.create_source(
        conn, user_id=user, kind="ics_feed", state="active",
        remote_host="secret-host.example", remote_key=key, extras={"name": "Private name"},
    )
    if failing:
        sources.set_state(conn, source_id, user, "error", reason="http_500")
    return source_id


def _check(db_path):
    return doctor.check_sharing_ics_feeds(Config(db_path=db_path), False)


def test_registered_with_a_deployment_scope():
    assert "sharing.ics_feeds" in dict(doctor.CHECKS)
    assert doctor.CHECK_SCOPES["sharing.ics_feeds"] == doctor.DEPLOYMENT


def test_no_database_skips(tmp_path):
    assert _check(tmp_path / "absent.db").status == doctor.SKIP


def test_no_feeds_is_ok(db_path):
    result = _check(db_path)
    assert (result.status, result.detail) == (doctor.OK, "no calendar feeds are subscribed")


def test_counts_by_state_and_names_nothing(db_path):
    with db.get_db(db_path) as conn:
        _feed(conn, "alice", "a")
        _feed(conn, "bob", "b", failing=True)
        revoked = _feed(conn, "bob", "c")
        sources.revoke(conn, revoked, "bob", reason="user_removed")
        conn.commit()
    result = _check(db_path)
    assert result.status == doctor.OK
    assert result.detail == "1 calendar feed(s) answering, 1 failing"
    assert "secret-host" not in result.detail and "Private" not in result.detail


def test_every_feed_failing_warns_with_a_remedy(db_path):
    with db.get_db(db_path) as conn:
        _feed(conn, "alice", "a", failing=True)
        conn.commit()
    result = _check(db_path)
    assert result.status == doctor.WARN and result.remedy


def test_an_unmigrated_database_warns_rather_than_raising(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    sqlite3.connect(path).close()
    assert _check(path).status == doctor.WARN
