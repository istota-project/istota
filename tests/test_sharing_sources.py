"""The `shared_sources` store, `sharing/sources.py`.

A shared source is something a user shared with Istota through an open
protocol: an ICS feed or an OCM folder share. Every function takes the owner's
id and re-checks it, a move into ``declined`` or ``revoked`` takes the secret
with it, and `sources_for_task` is the one listing a task reads, so a guest's
turn and a task nobody asked in a shared room see nothing (ISSUE-576).

Real database, real Fernet layer: the refusals are only meaningful where the
write they refuse could have happened.
"""

from __future__ import annotations

import json
import os
from unittest import mock

import pytest

from istota import db
from istota.credentials import local as local_credentials
from istota.credentials import store as secrets_store
from istota.credentials.local import LocalCredential
from istota.credentials.names import VAULT_ENTRY_SERVICE
from istota.sharing import sources
from istota.skills._types import SkillMeta

INDEX = {
    name: SkillMeta(name=name, description="x", shared_room=value)
    for name, value in {"calendar": "private", "shares": "private",
                        "sensitive_actions": "safe"}.items()
}


@pytest.fixture(autouse=True)
def secret_key_env():
    with mock.patch.dict(os.environ, {"ISTOTA_SECRET_KEY": "deadbeef" * 8}):
        yield


@pytest.fixture
def conn(db_path):
    with db.get_db(db_path) as c:
        yield c


def _feed(conn, user="alice", key="feed-key-1", host="cal.example.org"):
    return sources.create_source(
        conn, user_id=user, kind="ics_feed", state="active", remote_host=host,
        remote_key=key, access="read", extras=None,
    )


def _offer(conn, user="alice", key="cloud.example.org|41", extras=None):
    return sources.create_source(
        conn, user_id=user, kind="ocm_share", state="pending",
        remote_host="cloud.example.org", remote_key=key, access="read",
        extras=extras or {"name": "Taxes 2026"},
    )


def _secret(conn, user, key):
    return secrets_store.get_secret(None, user, sources.SECRET_SERVICE, key,
                                    connection=conn, touch=False)


class TestCreate:
    def test_a_feed_starts_active_and_reads_back(self, conn):
        source_id = _feed(conn)
        source = sources.get_source(conn, source_id, "alice")
        assert (source.kind, source.state, source.remote_host, source.access) == (
            "ics_feed", "active", "cal.example.org", "read")
        assert source.extras == {}
        assert source.error_count == 0

    def test_an_offer_starts_pending_with_its_extras(self, conn):
        source = sources.get_source(conn, _offer(conn), "alice")
        assert source.state == "pending"
        assert source.extras == {"name": "Taxes 2026"}

    @pytest.mark.parametrize("kind, state", [("ics_feed", "pending"), ("ocm_share", "active"),
                                             ("ics_feed", "revoked")])
    def test_a_source_cannot_start_in_another_state(self, conn, kind, state):
        with pytest.raises(ValueError):
            sources.create_source(conn, user_id="alice", kind=kind, state=state,
                                  remote_host="h.example", remote_key="k", access="read",
                                  extras=None)

    @pytest.mark.parametrize("field, value", [
        ("kind", "caldav"), ("access", "admin"), ("remote_host", ""),
        ("remote_host", "two words"), ("remote_key", ""), ("extras", ["not", "a", "dict"]),
    ])
    def test_bad_fields_are_refused_and_nothing_is_written(self, conn, field, value):
        fields = {"user_id": "alice", "kind": "ics_feed", "state": "active",
                  "remote_host": "h.example", "remote_key": "k", "access": "read",
                  "extras": None}
        fields[field] = value
        with pytest.raises(ValueError):
            sources.create_source(conn, **fields)
        assert conn.execute("SELECT COUNT(*) FROM shared_sources").fetchone()[0] == 0

    def test_the_same_remote_resource_twice_names_the_existing_row(self, conn):
        first = _feed(conn)
        with pytest.raises(sources.SourceExists) as err:
            _feed(conn)
        assert err.value.source_id == first

    def test_another_user_may_hold_the_same_remote_resource(self, conn):
        assert _feed(conn, user="alice") != _feed(conn, user="bob")


class TestStateTransitions:
    @pytest.mark.parametrize("path", [
        ["pending", "active", "error", "active", "revoked", "active"],
        ["pending", "pending", "declined"],
        ["pending", "revoked", "pending", "active"],
        ["pending", "active", "revoked", "pending"],
    ])
    def test_allowed_paths(self, conn, path):
        source_id = _offer(conn)
        for state in path[1:]:
            assert sources.set_state(conn, source_id, "alice", state) is True
        assert sources.get_source(conn, source_id, "alice").state == path[-1]

    @pytest.mark.parametrize("path", [
        ["pending", "error"],
        ["pending", "active", "pending"],
        ["pending", "active", "declined"],
        ["pending", "declined", "active"],
        ["pending", "declined", "pending"],
    ])
    def test_refused_paths_change_nothing(self, conn, path):
        source_id = _offer(conn)
        for state in path[1:-1]:
            sources.set_state(conn, source_id, "alice", state)
        before = sources.get_source(conn, source_id, "alice")
        with pytest.raises(ValueError):
            sources.set_state(conn, source_id, "alice", path[-1])
        assert sources.get_source(conn, source_id, "alice") == before

    def test_an_unknown_state_is_refused(self, conn):
        with pytest.raises(ValueError):
            sources.set_state(conn, _feed(conn), "alice", "paused")

    def test_errors_count_up_and_a_success_resets_them(self, conn):
        source_id = _feed(conn)
        sources.set_state(conn, source_id, "alice", "error", reason="timeout")
        sources.set_state(conn, source_id, "alice", "error", reason="http_500")
        source = sources.get_source(conn, source_id, "alice")
        assert (source.error_count, source.last_reason) == (2, "http_500")
        sources.set_state(conn, source_id, "alice", "active")
        source = sources.get_source(conn, source_id, "alice")
        assert (source.error_count, source.last_reason) == (0, None)

    def test_a_reason_must_be_a_code(self, conn):
        with pytest.raises(ValueError):
            sources.set_state(conn, _feed(conn), "alice", "error",
                              reason="<html>Server said something</html>")


class TestSecretsGoWithTheRow:
    def test_revoking_a_feed_deletes_its_address(self, conn):
        source_id = _feed(conn)
        secrets_store.set_secret(None, "alice", sources.SECRET_SERVICE, str(source_id),
                                 "https://cal.example.org/private-feed.ics", connection=conn)
        assert sources.revoke(conn, source_id, "alice", reason="removed") is True
        source = sources.get_source(conn, source_id, "alice")
        assert (source.state, source.last_reason) == ("revoked", "removed")
        assert _secret(conn, "alice", str(source_id)) is None
        assert source.remote_key == "feed-key-1"

    def test_declining_an_offer_deletes_the_held_secrets(self, conn):
        source_id = _offer(conn)
        for key in (str(source_id), f"{source_id}:reoffer"):
            secrets_store.set_secret(None, "alice", sources.SECRET_SERVICE, key,
                                     "held-share-secret", connection=conn)
        sources.set_state(conn, source_id, "alice", "declined")
        assert _secret(conn, "alice", str(source_id)) is None
        assert _secret(conn, "alice", f"{source_id}:reoffer") is None

    def test_revoking_an_active_share_deletes_its_local_credential(self, conn):
        source_id = _offer(conn)
        sources.set_state(conn, source_id, "alice", "active")
        local_credentials.create(
            conn, "alice",
            LocalCredential(name=sources.credential_name(source_id), value="share-secret",
                            url="https://cloud.example.org"),
            access={"scope_mode": "all", "allow_scheduled": False},
            system_prefix="ocm_share_",
        )
        conn.commit()
        sources.revoke(conn, source_id, "alice", reason="share_revoked")
        conn.commit()
        assert secrets_store.get_secret(None, "alice", VAULT_ENTRY_SERVICE,
                                        sources.credential_name(source_id),
                                        connection=conn) is None

    def test_an_error_keeps_the_secret(self, conn):
        source_id = _feed(conn)
        secrets_store.set_secret(None, "alice", sources.SECRET_SERVICE, str(source_id),
                                 "https://cal.example.org/private-feed.ics", connection=conn)
        sources.set_state(conn, source_id, "alice", "error", reason="timeout")
        assert _secret(conn, "alice", str(source_id)) is not None


class TestOwnership:
    def test_another_user_cannot_read_a_source_by_id(self, conn):
        source_id = _feed(conn, user="alice")
        assert sources.get_source(conn, source_id, "bob") is None

    def test_another_user_cannot_change_or_revoke_it(self, conn):
        source_id = _feed(conn, user="alice")
        secrets_store.set_secret(None, "alice", sources.SECRET_SERVICE, str(source_id),
                                 "https://cal.example.org/private-feed.ics", connection=conn)
        assert sources.set_state(conn, source_id, "bob", "error", reason="timeout") is False
        assert sources.revoke(conn, source_id, "bob", reason="removed") is False
        assert sources.get_source(conn, source_id, "alice").state == "active"
        assert _secret(conn, "alice", str(source_id)) is not None

    def test_the_listing_is_the_owners_and_reachable_states_only(self, conn):
        feed = _feed(conn, user="alice")
        _offer(conn, user="alice")
        _feed(conn, user="bob")
        broken = _feed(conn, user="alice", key="feed-key-2")
        sources.set_state(conn, broken, "alice", "error", reason="timeout")
        gone = _feed(conn, user="alice", key="feed-key-3")
        sources.revoke(conn, gone, "alice", reason="removed")
        assert [s.id for s in sources.sources_for_user(conn, "alice")] == [feed, broken]
        assert [s.id for s in sources.sources_for_user(
            conn, "alice", kinds=("ocm_share",), states=("pending",))] != []

    def test_delete_for_user_takes_rows_secrets_and_credentials(self, conn):
        mine = _offer(conn, user="alice")
        sources.set_state(conn, mine, "alice", "active")
        local_credentials.create(
            conn, "alice",
            LocalCredential(name=sources.credential_name(mine), value="share-secret",
                            url="https://cloud.example.org"),
            system_prefix="ocm_share_",
        )
        feed = _feed(conn, user="alice")
        secrets_store.set_secret(None, "alice", sources.SECRET_SERVICE, str(feed),
                                 "https://cal.example.org/a.ics", connection=conn)
        theirs = _feed(conn, user="bob")
        secrets_store.set_secret(None, "bob", sources.SECRET_SERVICE, str(theirs),
                                 "https://cal.example.org/b.ics", connection=conn)
        conn.commit()

        assert sources.delete_for_user(conn, "alice") == 2
        conn.commit()

        assert sources.sources_for_user(conn, "alice", states=sources.STATES) == []
        assert _secret(conn, "alice", str(feed)) is None
        assert secrets_store.get_secret(None, "alice", VAULT_ENTRY_SERVICE,
                                        sources.credential_name(mine), connection=conn) is None
        assert sources.get_source(conn, theirs, "bob") is not None
        assert _secret(conn, "bob", str(theirs)) is not None

    def test_deleting_the_profile_takes_every_source_secret_and_credential(self, conn, db_path):
        from istota import user_profiles

        user_profiles.ensure_profile(db_path, "alice")
        active = _offer(conn, user="alice", key="cloud.example.org|41")
        sources.set_state(conn, active, "alice", "active")
        local_credentials.create(
            conn, "alice",
            LocalCredential(name=sources.credential_name(active), value="share-secret",
                            url="https://cloud.example.org"),
            system_prefix="ocm_share_",
        )
        held = _offer(conn, user="alice", key="cloud.example.org|42")
        secrets_store.set_secret(None, "alice", sources.SECRET_SERVICE, str(held),
                                 "held-secret", connection=conn)
        feed = _feed(conn, user="alice")
        secrets_store.set_secret(None, "alice", sources.SECRET_SERVICE, str(feed),
                                 "https://cal.example.org/a.ics", connection=conn)
        conn.execute("INSERT INTO shared_feed_cache (source_id, body, fetched_at)"
                     " VALUES (?, ?, datetime('now'))", (feed, b"BEGIN:VCALENDAR"))
        theirs = _feed(conn, user="bob")
        secrets_store.set_secret(None, "bob", sources.SECRET_SERVICE, str(theirs),
                                 "https://cal.example.org/b.ics", connection=conn)
        conn.commit()

        assert user_profiles.delete_profile(db_path, "alice") is True

        assert conn.execute(
            "SELECT COUNT(*) FROM shared_sources WHERE user_id = 'alice'").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM secrets WHERE user_id = 'alice' AND service IN (?, ?)",
            (sources.SECRET_SERVICE, VAULT_ENTRY_SERVICE)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM shared_feed_cache WHERE source_id = ?", (feed,)).fetchone()[0] == 0
        assert sources.get_source(conn, theirs, "bob") is not None
        assert _secret(conn, "bob", str(theirs)) is not None


def _shared_room(conn) -> str:
    room = db.create_web_chat_room(conn, "alice", "Family")
    db.add_room_member(conn, room.token, "bob")
    return room.token


class TestSourcesForTask:
    """ISSUE-576: a turn runs with its sender's reach."""

    @pytest.fixture
    def alices(self, conn):
        feed = _feed(conn)
        share = _offer(conn)
        sources.set_state(conn, share, "alice", "active")
        conn.commit()
        return feed, share

    def test_a_private_room_turn_reaches_both_kinds(self, conn, make_task, alices):
        room = db.create_web_chat_room(conn, "alice", "Mine")
        task = make_task(user_id="alice", conversation_token=room.token, source_type="web")
        assert [s.id for s in sources.sources_for_task(
            conn, task, kind="ics_feed", skill_index=INDEX)] == [alices[0]]
        assert [s.id for s in sources.sources_for_task(
            conn, task, kind="ocm_share", skill_index=INDEX)] == [alices[1]]

    def test_a_members_own_turn_in_a_shared_room_reaches_them(self, conn, make_task, alices):
        task = make_task(user_id="alice", conversation_token=_shared_room(conn),
                         source_type="web")
        assert sources.sources_for_task(conn, task, kind="ics_feed", skill_index=INDEX)
        assert sources.sources_for_task(conn, task, kind="ocm_share", skill_index=INDEX)

    @pytest.mark.parametrize("kind", ["ics_feed", "ocm_share"])
    def test_a_guests_turn_reaches_nothing(self, conn, make_task, alices, kind):
        task = make_task(user_id="alice", conversation_token=_shared_room(conn),
                         source_type="web", guest_participant_id=7)
        assert sources.sources_for_task(conn, task, kind=kind, skill_index=INDEX) == []

    @pytest.mark.parametrize("source_type", ["subtask", "cli", "heartbeat"])
    def test_a_task_nobody_asked_in_a_shared_room_reaches_nothing(
        self, conn, make_task, alices, source_type,
    ):
        task = make_task(user_id="alice", conversation_token=_shared_room(conn),
                         source_type=source_type)
        assert sources.sources_for_task(conn, task, kind="ics_feed", skill_index=INDEX) == []
        assert sources.sources_for_task(conn, task, kind="ocm_share", skill_index=INDEX) == []

    def test_briefing_research_reaches_nothing(self, conn, make_task, alices):
        task = make_task(user_id="alice", source_type="group_briefing_research")
        assert sources.sources_for_task(conn, task, kind="ics_feed", skill_index=INDEX) == []

    def test_a_kind_whose_skill_is_not_installed_reaches_nothing(self, conn, make_task, alices):
        task = make_task(user_id="alice", source_type="cli")
        index = {"calendar": INDEX["calendar"]}
        assert sources.sources_for_task(conn, task, kind="ocm_share", skill_index=index) == []
        assert sources.sources_for_task(conn, task, kind="ics_feed", skill_index=index)

    def test_another_users_task_never_reaches_them(self, conn, make_task, alices):
        task = make_task(user_id="bob", source_type="cli")
        assert sources.sources_for_task(conn, task, kind="ics_feed", skill_index=INDEX) == []

    def test_an_unknown_kind_is_refused(self, conn, make_task):
        with pytest.raises(ValueError):
            sources.sources_for_task(conn, make_task(user_id="alice"), kind="caldav",
                                     skill_index=INDEX)


def test_extras_are_stored_as_json(conn):
    source_id = _offer(conn, extras={"name": "Taxes 2026", "owner": "carol"})
    raw = conn.execute("SELECT extras FROM shared_sources WHERE id = ?", (source_id,)).fetchone()[0]
    assert json.loads(raw) == {"name": "Taxes 2026", "owner": "carol"}


def test_the_migration_creates_the_table_on_an_older_database(tmp_path):
    path = tmp_path / "old.db"
    db.init_db(path)
    with db.get_db(path) as c:
        c.execute("DROP TABLE shared_sources")
    db.init_db(path)
    with db.get_db(path) as c:
        columns = {row[1] for row in c.execute("PRAGMA table_info(shared_sources)")}
        assert {"user_id", "kind", "state", "remote_key", "extras", "next_poll_at"} <= columns
        indexes = {row[1] for row in c.execute("PRAGMA index_list(shared_sources)")}
        assert "idx_shared_sources_due" in indexes
