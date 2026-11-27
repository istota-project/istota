"""The secret-link token store: digests only, a read-only peek, one spend."""

from __future__ import annotations

import hashlib
import sqlite3
import textwrap

import pytest

from istota import db
from istota.config import Config, load_config
from istota.config_mapper import collect_problems
from istota.webui import auth
from istota.webui import secret_links
from istota.webui.secret_links import SecretLinkError


def _issue(conn, *, user="alice", name="acme-login", intent="credential", params=None, ttl=900):
    if params is None:
        params = {"name": name, "url": "https://acme.com", "username": False}
    return secret_links.issue(
        conn, user_id=user, intent=intent, params=params,
        task_id=7, room_token="room-abc", ttl_seconds=ttl,
    )


def _issued(db_path, **kwargs):
    with db.get_db(db_path) as conn:
        return _issue(conn, **kwargs)


def _row(db_path, link_id):
    with db.get_db(db_path) as conn:
        return tuple(conn.execute("SELECT * FROM secret_links WHERE id=?", (link_id,)).fetchone())


def _age(db_path, link_id, *, column="expires_at", seconds):
    with db.get_db(db_path) as conn:
        conn.execute(
            f"UPDATE secret_links SET {column}=? WHERE id=?",
            (auth._timestamp(-seconds), link_id),
        )


class TestIssue:
    def test_only_the_digest_is_stored(self, db_path):
        token, link = _issued(db_path)
        with db.get_db(db_path) as conn:
            row = conn.execute("SELECT * FROM secret_links WHERE id=?", (link.id,)).fetchone()
        assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
        assert all(token not in str(value) for value in tuple(row))
        assert len(token) >= 40

    def test_the_link_describes_the_row(self, db_path):
        _token, link = _issued(db_path)
        assert (link.user_id, link.intent, link.task_id, link.room_token) == (
            "alice", "credential", 7, "room-abc")
        assert link.params == {"name": "acme-login", "url": "https://acme.com", "username": False}
        assert link.expires_at > auth._timestamp()

    def test_tokens_differ(self, db_path):
        first, _ = _issued(db_path, name="one")
        second, _ = _issued(db_path, name="two")
        assert first != second

    @pytest.mark.parametrize("kwargs", [
        {"intent": "settings"},
        {"params": ["not", "a", "dict"]},
        {"params": {"url": "https://acme.com"}},
        {"user": ""},
        {"ttl": 0},
    ])
    def test_a_bad_request_is_invalid_and_writes_nothing(self, db_path, kwargs):
        with pytest.raises(SecretLinkError) as raised:
            _issued(db_path, **kwargs)
        assert raised.value.reason == "invalid"
        with db.get_db(db_path) as conn:
            assert conn.execute("SELECT count(*) FROM secret_links").fetchone()[0] == 0

    def test_the_table_refuses_an_unknown_intent_itself(self, db_path):
        with db.get_db(db_path) as conn, pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO secret_links (token_hash,user_id,intent,task_id,room_token,expires_at) "
                "VALUES ('h','alice','settings',1,'r','2099-01-01')")

    def test_google_connect_takes_empty_params(self, db_path):
        token, link = _issued(db_path, intent="google_connect", params={})
        assert secret_links.peek(db_path, token) == link


class TestSupersede:
    def test_a_new_link_for_the_same_name_spends_the_old_one(self, db_path):
        old_token, old = _issued(db_path)
        new_token, new = _issued(db_path)
        assert secret_links.peek(db_path, old_token) is None
        assert secret_links.peek(db_path, new_token) == new
        with db.get_db(db_path) as conn:
            row = conn.execute("SELECT used_at, outcome FROM secret_links WHERE id=?", (old.id,)).fetchone()
        assert row["used_at"] is not None and row["outcome"] == "superseded"

    def test_another_name_or_user_is_left_alone(self, db_path):
        kept_token, _ = _issued(db_path, name="acme-login")
        bobs_token, _ = _issued(db_path, user="bob", name="acme-login")
        _issued(db_path, name="other-login")
        _issued(db_path, intent="google_connect", params={})
        assert secret_links.peek(db_path, kept_token) is not None
        assert secret_links.peek(db_path, bobs_token) is not None


class TestTheLiveCap:
    def test_a_sixth_live_link_is_refused(self, db_path):
        for n in range(secret_links.MAX_LIVE_PER_USER):
            _issued(db_path, name=f"name-{n}")
        with pytest.raises(SecretLinkError) as raised:
            _issued(db_path, name="one-too-many")
        assert raised.value.reason == "too_many_links"
        with db.get_db(db_path) as conn:
            assert conn.execute("SELECT count(*) FROM secret_links").fetchone()[0] == 5
        # Another user has a cap of their own.
        _issued(db_path, user="bob", name="one-too-many")

    def test_reissuing_a_live_name_at_the_cap_supersedes_rather_than_refuses(self, db_path):
        tokens = [_issued(db_path, name=f"name-{n}")[0] for n in range(5)]
        fresh, _ = _issued(db_path, name="name-0")
        assert secret_links.peek(db_path, tokens[0]) is None
        assert secret_links.peek(db_path, fresh) is not None

    def test_expired_and_spent_links_do_not_count(self, db_path):
        links = [_issued(db_path, name=f"name-{n}")[1] for n in range(5)]
        _age(db_path, links[0].id, seconds=1)
        with db.get_db(db_path) as conn:
            assert secret_links.spend(conn, links[1].id, "stored")
            _issue(conn, name="after-expiry")
            _issue(conn, name="after-spend")


class TestPeek:
    def test_peek_is_read_only(self, db_path):
        token, link = _issued(db_path)
        before = _row(db_path, link.id)
        with db.get_db(db_path) as conn:
            changes = conn.total_changes
        for _ in range(20):
            assert secret_links.peek(db_path, token) == link
        assert _row(db_path, link.id) == before
        with db.get_db(db_path) as conn:
            assert conn.total_changes == changes

    @pytest.mark.parametrize("token", ["", "nope", "x" * 300, "café", None, 5])
    def test_unknown_or_malformed_tokens_peek_as_none(self, db_path, token):
        _issued(db_path)
        assert secret_links.peek(db_path, token) is None

    def test_an_expired_link_does_not_peek(self, db_path):
        token, link = _issued(db_path)
        _age(db_path, link.id, seconds=1)
        assert secret_links.peek(db_path, token) is None
        with db.get_db(db_path) as conn:
            assert secret_links.live_for_update(conn, token) is None


class TestSpend:
    def test_spend_succeeds_once(self, db_path):
        token, link = _issued(db_path)
        with db.get_db(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            assert secret_links.live_for_update(conn, token) == link
            assert secret_links.spend(conn, link.id, "stored") is True
            assert secret_links.spend(conn, link.id, "cancelled") is False
        with db.get_db(db_path) as conn:
            row = conn.execute("SELECT used_at, outcome FROM secret_links WHERE id=?", (link.id,)).fetchone()
            assert row["used_at"] is not None and row["outcome"] == "stored"
            assert secret_links.live_for_update(conn, token) is None
        assert secret_links.peek(db_path, token) is None

    def test_an_unknown_outcome_is_refused(self, db_path):
        token, link = _issued(db_path)
        with db.get_db(db_path) as conn, pytest.raises(ValueError):
            secret_links.spend(conn, link.id, "whatever")
        assert secret_links.peek(db_path, token) == link

    def test_two_connections_racing_store_once(self, db_path):
        token, link = _issued(db_path)
        results = []
        for _ in range(2):
            with db.get_db(db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                found = secret_links.live_for_update(conn, token)
                results.append(found is not None and secret_links.spend(conn, found.id, "stored"))
        assert results == [True, False]


class TestRecordFailure:
    def test_failures_count_and_the_fifth_spends(self, db_path):
        token, link = _issued(db_path)
        with db.get_db(db_path) as conn:
            counts = [secret_links.record_failure(conn, link.id) for _ in range(4)]
        assert counts == [1, 2, 3, 4]
        assert secret_links.peek(db_path, token) is not None
        with db.get_db(db_path) as conn:
            assert secret_links.record_failure(conn, link.id) == secret_links.MAX_FAILURES
            row = conn.execute("SELECT used_at, outcome FROM secret_links WHERE id=?", (link.id,)).fetchone()
        assert row["used_at"] is not None and row["outcome"] == "failed"
        assert secret_links.peek(db_path, token) is None

    def test_a_spent_link_counts_nothing(self, db_path):
        _token, link = _issued(db_path)
        with db.get_db(db_path) as conn:
            secret_links.spend(conn, link.id, "cancelled")
            assert secret_links.record_failure(conn, link.id) == 0
            assert conn.execute("SELECT failures FROM secret_links WHERE id=?", (link.id,)).fetchone()[0] == 0


class TestPrune:
    def test_only_rows_past_the_window_go(self, db_path):
        _live_token, live = _issued(db_path, name="live")
        _a, expired_recently = _issued(db_path, name="recent")
        _b, expired_long_ago = _issued(db_path, name="old")
        _c, spent_long_ago = _issued(db_path, name="spent")
        _age(db_path, expired_recently.id, seconds=86400)
        _age(db_path, expired_long_ago.id, seconds=8 * 86400)
        with db.get_db(db_path) as conn:
            secret_links.spend(conn, spent_long_ago.id, "stored")
        _age(db_path, spent_long_ago.id, seconds=8 * 86400)
        assert secret_links.prune(db_path) == 2
        with db.get_db(db_path) as conn:
            left = {r[0] for r in conn.execute("SELECT id FROM secret_links")}
        assert left == {live.id, expired_recently.id}

    def test_the_window_is_a_parameter(self, db_path):
        _token, link = _issued(db_path)
        _age(db_path, link.id, seconds=2 * 86400)
        assert secret_links.prune(db_path, older_than_days=7) == 0
        assert secret_links.prune(db_path, older_than_days=1) == 1


class TestTheTtlSetting:
    def _load(self, tmp_path, body):
        path = tmp_path / "config.toml"
        path.write_text(textwrap.dedent(body))
        with collect_problems() as problems:
            config = load_config(path)
        return config, problems

    def test_the_default_is_fifteen_minutes(self):
        assert Config().secret_links.ttl_seconds == 900

    def test_a_value_in_range_loads(self, tmp_path):
        config, problems = self._load(tmp_path, "[secret_links]\nttl_seconds = 1800\n")
        assert config.secret_links.ttl_seconds == 1800
        assert not [p for p in problems if p["path"][:1] == ("secret_links",)]

    @pytest.mark.parametrize("raw, expected", [(30, 60), (7200, 3600)])
    def test_a_value_out_of_range_is_clamped_with_a_problem(self, tmp_path, raw, expected):
        config, problems = self._load(tmp_path, f"[secret_links]\nttl_seconds = {raw}\n")
        assert config.secret_links.ttl_seconds == expected
        assert [p["kind"] for p in problems if p["path"] == ("secret_links", "ttl_seconds")] == ["discarded"]
