"""`config_editor.requests`: the table a web config change waits in."""

from __future__ import annotations

import sqlite3

import pytest

from istota import db
from istota.config_editor import requests
from istota.config_editor.edits import Preview
from istota.config_editor.secrets import SENTINEL
from istota.credentials import store

DIGEST = "a" * 64
FILE_VALUE = "imap-secret-value-7"
INLINE_VALUE = "header-secret-value-8"


@pytest.fixture(autouse=True)
def master_key(monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "k" * 64)


def _preview(**overrides) -> Preview:
    values = dict(
        document={
            "email": {"imap_host": "imap.example.org"},
            "brain": {"native": {"extra_headers": {"X-Key": SENTINEL}}},
        },
        file_secrets={"istota_email_imap_password": FILE_VALUE},
        inline_secrets={"brain.native.extra_headers.X-Key": INLINE_VALUE},
        admins=["alice", "bob"],
        diff=["--- a/config.toml", "+++ b/config.toml"],
        changed=True,
        changed_sections=["brain.native", "email"],
        secret_names=["brain.native.extra_headers.X-Key", "istota_email_imap_password"],
        admins_changed=True,
    )
    values.update(overrides)
    return Preview(**values)


def _create(conn, **overrides) -> int:
    return requests.create(conn, _preview(**overrides), admin_id="alice", base_digest=DIGEST)


def _columns(db_path, request_id) -> str:
    with db.get_db(db_path) as conn:
        row = conn.execute("SELECT * FROM config_change_requests WHERE id = ?", (request_id,)).fetchone()
    return " ".join(str(row[key]) for key in row.keys())


def _notifications(db_path) -> list[sqlite3.Row]:
    with db.get_db(db_path) as conn:
        return conn.execute(
            "SELECT user_id, title, body FROM notifications WHERE source = 'admin_config_change' ORDER BY user_id"
        ).fetchall()


class TestCreate:
    def test_a_request_is_pending_with_its_credentials_sealed(self, db_path):
        with db.get_db(db_path) as conn:
            request_id = _create(conn)
            row = requests.get(conn, request_id)
        assert row["status"] == "pending"
        assert row["admins"] == ["alice", "bob"] and row["admins_changed"] is True
        assert row["changed_sections"] == ["brain.native", "email"]
        assert "secrets_sealed" not in row
        stored = _columns(db_path, request_id)
        assert FILE_VALUE not in stored and INLINE_VALUE not in stored
        assert SENTINEL in stored

    def test_a_second_open_request_is_refused_with_the_first_id(self, db_path):
        with db.get_db(db_path) as conn:
            first = _create(conn)
        with pytest.raises(requests.RequestOpen) as raised:
            with db.get_db(db_path) as conn:
                _create(conn)
        assert raised.value.id == first

    def test_the_index_itself_refuses_a_second_open_row(self, db_path):
        with db.get_db(db_path) as conn:
            _create(conn)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO config_change_requests (status, admin_id, base_digest,
                       document_toml, changed_sections_json, secret_names_json, diff_text, created_at)
                       VALUES ('taken', 'bob', 'd', '', '[]', '[]', '', 'now')"""
                )

    def test_without_the_master_key_nothing_is_stored(self, db_path, monkeypatch):
        monkeypatch.delenv("ISTOTA_SECRET_KEY")
        with pytest.raises(store.SecretKeyMissingError):
            with db.get_db(db_path) as conn:
                _create(conn)
        with db.get_db(db_path) as conn:
            assert requests.list_recent(conn) == []


class TestTake:
    def test_take_claims_unseals_and_nulls_the_sealed_column(self, db_path):
        with db.get_db(db_path) as conn:
            request_id = _create(conn)
        with db.get_db(db_path) as conn:
            taken = requests.take(conn, DIGEST, current_admins=["alice", "carol"])
        assert taken.id == request_id
        assert taken.secrets == {
            "istota_email_imap_password": FILE_VALUE,
            "brain.native.extra_headers.X-Key": INLINE_VALUE,
        }
        assert taken.admins == ["alice", "bob"]
        with db.get_db(db_path) as conn:
            row = conn.execute("SELECT * FROM config_change_requests WHERE id = ?", (request_id,)).fetchone()
        assert row["status"] == "taken" and row["taken_at"]
        assert row["secrets_sealed"] is None
        stored = _columns(db_path, request_id)
        assert FILE_VALUE not in stored and INLINE_VALUE not in stored

    def test_nothing_pending_is_none(self, db_path):
        with db.get_db(db_path) as conn:
            assert requests.take(conn, DIGEST) is None

    def test_a_digest_mismatch_is_refused_and_nothing_is_taken(self, db_path):
        with db.get_db(db_path) as conn:
            request_id = _create(conn)
        with db.get_db(db_path) as conn:
            assert requests.take(conn, "b" * 64) is None
            row = requests.get(conn, request_id)
        assert row["status"] == "refused"
        assert row["output_text"] == "config.toml changed since this request was made"
        assert FILE_VALUE not in _columns(db_path, request_id)

    def test_a_stale_taken_row_is_failed_first(self, db_path):
        with db.get_db(db_path) as conn:
            stale = _create(conn)
        with db.get_db(db_path) as conn:
            requests.take(conn, DIGEST, current_admins=["alice", "carol"])
        # The open request blocks a new one until a run fails it.
        with pytest.raises(requests.RequestOpen):
            with db.get_db(db_path) as conn:
                _create(conn)
        with db.get_db(db_path) as conn:
            assert requests.take(conn, DIGEST, current_admins=["alice"]) is None
            row = requests.get(conn, stale)
        assert row["status"] == "failed" and row["output_text"] == "the host did not record an outcome"
        # bob is in the request's new list, carol in the one it was taken under; alice acted.
        assert [row["user_id"] for row in _notifications(db_path)] == ["bob", "carol"]
        with db.get_db(db_path) as conn:
            fresh = _create(conn)
        with db.get_db(db_path) as conn:
            assert requests.take(conn, DIGEST).id == fresh

    def test_a_building_row_is_failed_too(self, db_path):
        with db.get_db(db_path) as conn:
            stale = _create(conn)
        with db.get_db(db_path) as conn:
            requests.take(conn, DIGEST)
            requests.mark(conn, stale)
        with db.get_db(db_path) as conn:
            assert requests.take(conn, DIGEST) is None
            assert requests.get(conn, stale)["status"] == "failed"

    def test_a_token_another_key_sealed_fails_the_request(self, db_path, monkeypatch):
        with db.get_db(db_path) as conn:
            request_id = _create(conn)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "z" * 64)
        with db.get_db(db_path) as conn:
            assert requests.take(conn, DIGEST) is None
            assert requests.get(conn, request_id)["status"] == "failed"


class TestFinish:
    def _taken(self, db_path, **overrides) -> int:
        with db.get_db(db_path) as conn:
            request_id = _create(conn, **overrides)
        with db.get_db(db_path) as conn:
            requests.take(conn, DIGEST, current_admins=["alice", "carol"])
        return request_id

    def test_applied_records_the_output_and_notifies_every_other_admin(self, db_path):
        request_id = self._taken(db_path)
        with db.get_db(db_path) as conn:
            requests.finish(conn, request_id, "applied", "ok", current_admins=["alice", "bob", "dave"])
            row = requests.get(conn, request_id)
        assert row["status"] == "applied" and row["finished_at"] and row["output_text"] == "ok"
        found = _notifications(db_path)
        assert [item["user_id"] for item in found] == ["bob", "carol", "dave"]
        body = found[0]["body"]
        assert "alice changed" in body and "brain.native (inline)" in body
        assert "istota_email_imap_password" in body and "the admin list" in body
        assert "X-Key" not in body and INLINE_VALUE not in body

    def test_unchanged_and_refused_notify_nobody(self, db_path):
        request_id = self._taken(db_path)
        with db.get_db(db_path) as conn:
            requests.finish(conn, request_id, "unchanged", "", current_admins=["bob"])
        assert _notifications(db_path) == []

    def test_a_request_not_taken_cannot_be_finished(self, db_path):
        with db.get_db(db_path) as conn:
            request_id = _create(conn)
        with pytest.raises(requests.RequestStateError):
            with db.get_db(db_path) as conn:
                requests.finish(conn, request_id, "applied", "")

    def test_mark_moves_taken_to_building_only(self, db_path):
        request_id = self._taken(db_path)
        with db.get_db(db_path) as conn:
            requests.mark(conn, request_id)
            assert requests.get(conn, request_id)["status"] == "building"
        with pytest.raises(requests.RequestStateError):
            with db.get_db(db_path) as conn:
                requests.mark(conn, request_id)


class TestCancelAndPrune:
    def test_cancel_only_from_pending(self, db_path):
        with db.get_db(db_path) as conn:
            request_id = _create(conn)
            assert requests.cancel(conn, request_id) is True
            assert requests.get(conn, request_id)["status"] == "cancelled"
            assert requests.cancel(conn, request_id) is False
        assert FILE_VALUE not in _columns(db_path, request_id)
        with db.get_db(db_path) as conn:
            taken_id = _create(conn)
        with db.get_db(db_path) as conn:
            requests.take(conn, DIGEST)
            assert requests.cancel(conn, taken_id) is False

    def test_prune_removes_old_closed_rows_only(self, db_path):
        with db.get_db(db_path) as conn:
            old = _create(conn)
            requests.cancel(conn, old)
            conn.execute(
                "UPDATE config_change_requests SET created_at = '2020-01-01T00:00:00Z' WHERE id = ?", (old,),
            )
            recent = _create(conn)
            requests.cancel(conn, recent)
            open_old = _create(conn)
            conn.execute(
                "UPDATE config_change_requests SET created_at = '2020-01-01T00:00:00Z' WHERE id = ?",
                (open_old,),
            )
            assert requests.prune(conn) == 1
            assert [row["id"] for row in requests.list_recent(conn)] == [open_old, recent]
