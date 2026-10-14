"""Grant state and request migration through real framework connections."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from istota import db
from istota.location import sharing
from istota.relay import requests

NOW = datetime(2026, 3, 8, 12, tzinfo=timezone.utc)


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "state.db"
    db.init_db(path)
    with db.get_db(path) as conn:
        room = db.create_web_chat_room(conn, "alice", "Hiking").token
        db.add_web_room_member(conn, room, "bob")
    return path, room


def held(conn, room, owner="alice"):
    task = db.create_task(conn, user_id=owner, source_type="web", prompt="Share", conversation_token=room)
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task,))
    options = dict(owner_user_id=owner, origin_task_id=task, request_key="share",
                   room_token=room, precision="exact", duration_seconds=3600,
                   audience=["alice", "bob"], binding_fingerprint="binding", room_epoch=0, now=NOW)
    return sharing._hold_location_grant(conn, **options), options


def activate(conn, grant, now=NOW):
    return sharing._activate_location_grant(conn, grant_id=grant["id"], now=now)


def attach_request(conn, grant, options):
    req = requests._store_request(conn, actor_user_id="alice", task_id=options["origin_task_id"],
        request_key="share", kind="location_share", recipient_user_id="alice", text="Share terms",
        service_body="Share terms", template_body=None, provider="room", binding_fingerprint="binding", preview="Share terms")
    conn.execute("UPDATE location_share_grants SET request_id=? WHERE id=?", (req["id"], grant["id"]))
    return req


def test_hold_replay_conflicts_and_caller_rollback(state):
    path, room = state
    with db.get_db(path) as conn:
        grant, options = held(conn, room)
        assert grant["state"] == "held" and grant["expires_at"] is None
        assert sharing._hold_location_grant(conn, **options) == grant
        for change in ({"duration_seconds": 300}, {"precision": "area"}, {"room_token": "other"}):
            with pytest.raises(requests.RequestError, match="request_conflict"):
                sharing._hold_location_grant(conn, **(options | change))
        conn.rollback()
    with db.get_db(path) as conn:
        assert conn.execute("SELECT count(*) FROM location_share_grants").fetchone()[0] == 0


def test_activation_replacement_replay_and_exact_expiry(state):
    path, room = state
    with db.get_db(path) as conn:
        first, _ = held(conn, room)
        active = activate(conn, first)
        second, _ = held(conn, room)
        shares = sharing.list_location_shares(conn, actor_user_id="alice", now=NOW)["shares"]
        assert {s["state"] for s in shares} == {"active", "held"}
        assert activate(conn, first, NOW + timedelta(minutes=2)) == active
        replacement = activate(conn, second, NOW + timedelta(minutes=3))
        assert conn.execute("SELECT close_reason FROM location_share_grants WHERE id=?", (first["id"],)).fetchone()[0] == "replaced"
        assert activate(conn, second, NOW + timedelta(minutes=4)) == replacement
        assert sharing.list_location_shares(conn, actor_user_id="alice", now=NOW + timedelta(minutes=63)) == {"shares": []}
        with pytest.raises(requests.RequestError, match="grant_unavailable"):
            activate(conn, second, NOW + timedelta(minutes=64))


def test_stop_is_owner_scoped_cancels_pending_and_cannot_reactivate(state):
    path, room = state
    with db.get_db(path) as conn:
        first, _ = held(conn, room)
        activate(conn, first)
        pending, options = held(conn, room)
        req = attach_request(conn, pending, options)
        bob, _ = held(conn, room, "bob")
        activate(conn, bob)
        assert sharing.revoke_location_shares(conn, actor_user_id="alice", room_token=room, now=NOW) == {"stopped": 2}
        assert sharing.revoke_location_shares(conn, actor_user_id="alice", all_rooms=True, now=NOW) == {"stopped": 0}
        assert conn.execute("SELECT state FROM whatsapp_skill_requests WHERE id=?", (req["id"],)).fetchone()[0] == "cancelled"
        assert len(sharing.list_location_shares(conn, actor_user_id="bob", now=NOW)["shares"]) == 1
        with pytest.raises(requests.RequestError, match="grant_unavailable"):
            activate(conn, pending)


@pytest.mark.parametrize("foreign_keys", [False, True])
@pytest.mark.parametrize("action", ["task", "request", "expired", "room"])
def test_cleanup_preserves_active_metadata_and_settles_holds(state, foreign_keys, action):
    path, room = state
    with db.get_db(path) as conn:
        conn.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
        grant, options = held(conn, room)
        req = attach_request(conn, grant, options)
        active, active_options = held(conn, room)
        active_req = attach_request(conn, active, active_options)
        activate(conn, active)
        conn.execute("UPDATE whatsapp_skill_requests SET state='approved' WHERE id=?", (active_req["id"],))
        if action == "task":
            conn.execute("DELETE FROM tasks WHERE id IN (?,?)", (options["origin_task_id"], active_options["origin_task_id"]))
        elif action == "request":
            conn.execute("DELETE FROM whatsapp_skill_requests WHERE id IN (?,?)", (req["id"], active_req["id"]))
        elif action == "expired":
            conn.execute("UPDATE whatsapp_skill_requests SET state='expired' WHERE id=?", (req["id"],))
        else:
            db._delete_room_rows(conn, room)
            assert conn.execute("SELECT count(*) FROM location_share_grants").fetchone()[0] == 0
            assert conn.execute("SELECT state FROM whatsapp_skill_requests WHERE id=?", (req["id"],)).fetchone()[0] == "cancelled"
            return
        row = conn.execute("SELECT * FROM location_share_grants WHERE id=?", (grant["id"],)).fetchone()
        assert row["state"] == ("expired" if action == "expired" else "cancelled")
        row = conn.execute("SELECT * FROM location_share_grants WHERE id=?", (active["id"],)).fetchone()
        assert row["state"] == "active"
        if action == "task":
            assert row["origin_task_id"] is None
        if action == "request":
            assert row["request_id"] is None
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_upgrade_preserves_all_request_kinds_references_indexes_and_triggers(tmp_path):
    path = tmp_path / "old.db"
    schema = (Path(__file__).parents[1] / "schema.sql").read_text().replace(", 'location_share'", "")
    start = schema.index("-- Consent metadata only;")
    end = schema.index("-- A recipient's block on one asker.", start)
    schema = schema[:start] + schema[end:]
    with sqlite3.connect(path) as conn:
        conn.executescript(schema)
        for number, kind in enumerate(["self_send", "relay_question", "side_whisper", "room_post", "purchase", "recovery_fill"]):
            conn.execute("INSERT INTO whatsapp_skill_requests (id,requester_user_id,request_key,kind,recipient_user_id,content_hash,service_hash,provider,binding_fingerprint,state) VALUES (?,?,?,?,?,'h','s','room','fp','sent')", (str(number), "alice", str(number), kind, "alice"))
        conn.execute("UPDATE whatsapp_skill_requests SET destination=? WHERE kind='side_whisper'",
                     ('{"kind":"private_reply"}',))
        conn.execute("CREATE INDEX custom_request_index ON whatsapp_skill_requests(kind)")
        conn.execute("CREATE TABLE audit(request_id TEXT)")
        conn.execute("CREATE TRIGGER custom_request_trigger AFTER UPDATE ON whatsapp_skill_requests BEGIN INSERT INTO audit VALUES (NEW.id); END")
        conn.execute("CREATE TABLE child(request_id TEXT REFERENCES whatsapp_skill_requests(id))")
        conn.execute("INSERT INTO child VALUES ('0')")
    db.init_db(path)
    db.init_db(path)
    with db.get_db(path) as conn:
        assert conn.execute("SELECT count(*) FROM whatsapp_skill_requests").fetchone()[0] == 6
        conn.execute("UPDATE whatsapp_skill_requests SET kind='location_share' WHERE id='0'")
        assert conn.execute("SELECT request_id FROM audit").fetchone()[0] == "0"
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='custom_request_index'").fetchone()
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert len(conn.execute("PRAGMA index_list(location_share_grants)").fetchall()) == 6


@pytest.mark.parametrize("change", [{"precision": "street"}, {"duration_seconds": 299},
                                    {"duration_seconds": 86401}, {"duration_seconds": 300.5}])
def test_schema_rejects_invalid_grant_terms(state, change):
    path, room = state
    with db.get_db(path) as conn:
        grant, _ = held(conn, room)
        field, value = next(iter(change.items()))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"UPDATE location_share_grants SET {field}=? WHERE id=?", (value, grant["id"]))


def test_expired_active_grant_is_settled_before_new_activation(state):
    path, room = state
    with db.get_db(path) as conn:
        first, _ = held(conn, room)
        activate(conn, first)
        second, _ = held(conn, room)
        activate(conn, second, NOW + timedelta(hours=1))
        row = conn.execute("SELECT state,close_reason FROM location_share_grants WHERE id=?", (first["id"],)).fetchone()
        assert tuple(row) == ("expired", "expired")


def test_grant_shape_matches_fresh_after_repeated_upgrade(state, tmp_path):
    path, _ = state
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as conn:
        schema = (Path(__file__).parents[1] / "schema.sql").read_text()
        start = schema.index("-- Consent metadata only;")
        end = schema.index("-- A recipient's block on one asker.", start)
        conn.executescript((schema[:start] + schema[end:]).replace(", 'location_share'", ""))
    db.init_db(old)
    db.init_db(old)
    with db.get_db(path) as fresh, db.get_db(old) as upgraded:
        for pragma in ("table_info", "foreign_key_list", "index_list"):
            assert [tuple(r) for r in fresh.execute(f"PRAGMA {pragma}(location_share_grants)")] == [
                tuple(r) for r in upgraded.execute(f"PRAGMA {pragma}(location_share_grants)")]


@pytest.mark.parametrize("change", [
    "member_add", "web_add", "member_remove", "web_remove", "departure",
    "guest", "unknown", "agent", "identity", "kind", "binding_add",
    "binding_replace", "archive", "orphan_archive", "veto", "bot_removed", "relabel",
])
def test_room_authority_change_settles_all_grants_and_never_revives(state, change):
    from istota.config import Config, UserConfig
    from istota.rooms import veto
    from istota.transport import ParticipantRef
    from istota.transport.whatsapp.groups import _relabel

    path, room = state
    config = Config(users={user: UserConfig() for user in ("alice", "bob", "carol")})
    with db.get_db(path) as conn:
        db.add_room_binding(conn, room, "talk", "talk-old")
        db.upsert_room_participant(conn, room_token=room, surface="talk", surface_ref="bob",
                                   user_id="bob", kind="principal")
        db.upsert_room_participant(conn, room_token=room, surface="whatsapp", surface_ref="opaque@lid",
                                   user_id="bob", kind="principal")
        if change == "orphan_archive":
            conn.execute("UPDATE rooms SET origin='talk' WHERE token=?", (room,))
        active, _ = held(conn, room)
        activate(conn, active)
        pending, options = held(conn, room)
        req = attach_request(conn, pending, options)
        other = db.create_web_chat_room(conn, "alice", "Other").token
        other_grant, _ = held(conn, other)
        activate(conn, other_grant)
        epoch = conn.execute("SELECT count(*) FROM room_epochs WHERE room_token=?", (room,)).fetchone()[0]

        if change == "member_add":
            db.add_room_member(conn, room, "carol", acknowledged=True)
        elif change == "web_add":
            db.add_web_room_member(conn, room, "carol")
            assert conn.execute("SELECT count(*) FROM room_epochs WHERE room_token=?", (room,)).fetchone()[0] == epoch
        elif change in {"member_remove", "web_remove"}:
            remove = db.remove_room_member if change == "member_remove" else db.drop_web_room_member
            remove(conn, room, "bob")
            db.add_web_room_member(conn, room, "bob")
        elif change == "departure":
            db.sync_room_roster(conn, room_token=room, surface="talk", present=[])
            db.upsert_room_participant(conn, room_token=room, surface="talk", surface_ref="bob",
                                       user_id="bob", kind="principal")
        elif change in {"guest", "unknown", "agent", "identity", "kind"}:
            db.upsert_room_participant(conn, room_token=room, surface="talk",
                surface_ref="bob" if change in {"identity", "kind"} else "new-reader",
                user_id="carol" if change == "identity" else None,
                kind={"unknown": "principal", "identity": "principal", "kind": "guest"}.get(change, change))
        elif change == "binding_add":
            db.add_room_binding(conn, room, "whatsapp", "group@g.us")
        elif change == "binding_replace":
            assert db.replace_room_binding(conn, room, "talk", "talk-new", expected_ref="talk-old")
            assert db.replace_room_binding(conn, room, "talk", "talk-old", expected_ref="talk-new")
        elif change in {"archive", "orphan_archive"}:
            if change == "archive":
                db.set_room_archived(conn, room, True)
            else:
                assert db.archive_orphaned_talk_rooms(conn, set()) == 1
            db.set_room_archived(conn, room, False)
        elif change in {"veto", "bot_removed"}:
            author = ParticipantRef(surface="web", surface_ref="bob", user_id="bob")
            if change == "veto":
                assert veto.apply(conn, config, room_token=room, author=author, verb="off")
            else:
                veto.switch_off_by_removal(conn, room)
            assert veto.apply(conn, config, room_token=room, author=author, verb="on")
            assert not veto.is_vetoed(conn, room)
        else:
            _relabel(conn, room, "opaque@lid", "15555550100@s.whatsapp.net", "bob")

        assert conn.execute("SELECT state FROM location_share_grants WHERE id=?", (active["id"],)).fetchone()[0] == "revoked"
        assert conn.execute("SELECT state FROM location_share_grants WHERE id=?", (pending["id"],)).fetchone()[0] == "cancelled"
        assert conn.execute("SELECT state FROM whatsapp_skill_requests WHERE id=?", (req["id"],)).fetchone()[0] == "cancelled"
        assert conn.execute("SELECT state FROM location_share_grants WHERE id=?", (other_grant["id"],)).fetchone()[0] == "active"
        with pytest.raises(requests.RequestError, match="grant_unavailable"):
            activate(conn, pending)


def test_identical_refresh_and_presentation_changes_preserve_grants(state):
    path, room = state
    with db.get_db(path) as conn:
        db.add_room_binding(conn, room, "talk", "talk-old")
        active, _ = held(conn, room)
        activate(conn, active)
        pending, options = held(conn, room)
        req = attach_request(conn, pending, options)
        db.add_room_member(conn, room, "bob")
        db.add_web_room_member(conn, room, "bob", display_name="Robert")
        db.upsert_room_participant(conn, room_token=room, surface="web", surface_ref="bob",
                                   kind="principal", display_name="Bob")
        assert db.sync_room_roster(conn, room_token=room, surface="web", present=["alice", "bob"]) == 0
        db.remove_room_member(conn, room, "absent")
        assert db.add_room_binding(conn, room, "talk", "talk-old")
        assert not db.add_room_binding(conn, room, "talk", "different")
        assert db.replace_room_binding(conn, room, "talk", "talk-old", expected_ref="talk-old")
        assert not db.replace_room_binding(conn, room, "talk", "new", expected_ref="stale")
        db.set_room_archived(conn, room, False)
        db.rename_room(conn, room, "New title")
        db.record_external_room_name(conn, room, "talk", "New external title")
        assert conn.execute("SELECT state FROM location_share_grants WHERE id=?", (active["id"],)).fetchone()[0] == "active"
        assert conn.execute("SELECT state FROM location_share_grants WHERE id=?", (pending["id"],)).fetchone()[0] == "held"
        assert conn.execute("SELECT state FROM whatsapp_skill_requests WHERE id=?", (req["id"],)).fetchone()[0] == "held"


def test_room_invalidation_rolls_back_with_membership_change(state):
    path, room = state
    with db.get_db(path) as conn:
        active, _ = held(conn, room)
        activate(conn, active)
        pending, options = held(conn, room)
        req = attach_request(conn, pending, options)
    with db.get_db(path) as conn:
        db.drop_web_room_member(conn, room, "bob")
        assert conn.execute("SELECT state FROM location_share_grants WHERE id=?", (active["id"],)).fetchone()[0] == "revoked"
        conn.rollback()
    with db.get_db(path) as conn:
        assert db.is_room_member(conn, room, "bob")
        assert conn.execute("SELECT state FROM location_share_grants WHERE id=?", (active["id"],)).fetchone()[0] == "active"
        assert conn.execute("SELECT state FROM location_share_grants WHERE id=?", (pending["id"],)).fetchone()[0] == "held"
        assert conn.execute("SELECT state FROM whatsapp_skill_requests WHERE id=?", (req["id"],)).fetchone()[0] == "held"


@pytest.mark.parametrize("own_bot", [True, False])
def test_recorded_bot_identity_only_invalidates_for_another_bot(state, own_bot):
    from istota.config import Config, UserConfig
    from istota.transport import ParticipantRef
    from istota.transport.ingest import record_inbound

    path, room = state
    config = Config(db_path=path, users={u: UserConfig() for u in ("alice", "bob")})
    config.talk.bot_username = "istota"
    with db.get_db(path) as conn:
        db.add_room_binding(conn, room, "talk", "talk-group")
        active, _ = held(conn, room)
        activate(conn, active)
        result = record_inbound(conn, config, surface="talk", surface_ref="talk-group",
            user_id="", text="echo", author=ParticipantRef(surface="talk",
                surface_ref="istota" if own_bot else "bots/other", is_bot=True))
        assert result.outcome == "recorded" and result.gate_reason == "agent_author"
        assert conn.execute("SELECT state FROM location_share_grants WHERE id=?", (active["id"],)).fetchone()[0] == (
            "active" if own_bot else "revoked")


def test_image_location_witness_rejects_old_schema_and_accepts_upgrade(tmp_path):
    import runpy

    path = tmp_path / "pre-sharing.db"
    repo = Path(__file__).parents[1]
    schema = (repo / "schema.sql").read_text().replace(", 'location_share'", "")
    start = schema.index("-- Consent metadata only;")
    end = schema.index("-- A recipient's block on one asker.", start)
    with sqlite3.connect(path) as conn:
        conn.executescript(schema[:start] + schema[end:])
    witness = runpy.run_path(str(repo / "tests/image/seed/location_state.py"))
    witness["seed"](path)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        witness["witness"](path)
    db.init_db(path)
    expected = {"retained_kinds": sorted(witness["KINDS"]), "constraints": True, "cleanup": True}
    assert witness["witness"](path) == expected
    db.init_db(path)
    assert witness["witness"](path) == expected
