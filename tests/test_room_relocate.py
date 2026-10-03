"""Database identity migration, seeded with pre-mint rooms."""
import json
import sqlite3

import pytest

from istota import db
from istota.maintenance import room_relocate
from istota.relay.destinations import destination_fingerprint


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "state.db"
    db.init_db(path)
    return path


def legacy(conn, token="old-talk", origin="talk"):
    db.register_room(conn, token, "alice", origin=origin, name="Example")
    db.add_room_binding(conn, token, origin, token)
    db.add_room_member(conn, token, "alice")
    return token


def snapshot(path):
    with sqlite3.connect(path) as conn:
        return list(conn.iterdump())


def request(conn, ident, destination, *, origin=None, fingerprint=None, kind="room_post"):
    fingerprint = fingerprint or destination_fingerprint(destination)
    conn.execute("""INSERT INTO whatsapp_skill_requests
        (id,requester_user_id,request_key,kind,recipient_user_id,text,content_hash,
         service_hash,preview,preview_digest,approved_digest,provider,binding_fingerprint,
         state,origin,destination) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ident, "alice", ident, kind, "alice", "Exact body", "body-digest",
         "service-digest", "Exact preview", "preview-digest", "preview-digest", "room",
         fingerprint, "queued", json.dumps(origin) if origin else None, json.dumps(destination)))


def test_migrate_preserves_multiplayer_state_and_native_refs(database):
    with db.get_db(database) as conn:
        old = legacy(conn)
        side = legacy(conn, "side-old", "web")
        conn.execute("UPDATE rooms SET side_of=?,side_for_user='alice' WHERE token=?", (old, side))
        task = db.create_task(conn, user_id="alice", source_type="talk", prompt="hi",
                              conversation_token=old, output_target=f"room:{old}, talk:{old}, web:{side}")
        conn.execute("UPDATE tasks SET talk_delivery_token=? WHERE id=?", (old, task))
        db.add_message(conn, old, role="user", body="hi", origin_surface="talk")
        conn.execute("INSERT INTO room_participants(room_token,surface,surface_ref,kind) VALUES (?, 'talk','guest','guest')", (old,))
        conn.execute("INSERT INTO credential_grant_rooms(user_id,name,conversation_token) VALUES ('alice','service',?)", (old,))
        conn.execute("INSERT INTO talk_poll_state(conversation_token,last_known_message_id) VALUES (?,42)", (old,))
        conn.execute("INSERT INTO talk_messages(conversation_token,message_id) VALUES (?,42)", (old,))
        conn.execute("INSERT INTO istota_kv(user_id,namespace,key,value) VALUES ('alice','_provisioned_rooms','general',?)", (old,))
        conn.execute("INSERT INTO room_policy(room_token,host_user_id,vetoed_at) VALUES (?,'alice','2026-01-01')", (old,))
        conn.execute("INSERT INTO room_vetoes(room_token,person) VALUES (?,'talk:guest')", (old,))
        conn.execute("INSERT INTO room_notices(room_token,body,reference) VALUES (?,'off','notice-1')", (old,))
        conn.execute("INSERT INTO room_epochs(room_token,epoch,reason,after_task_id) VALUES (?,1,'join',?)", (old, task))
        conn.execute("INSERT INTO speech_gate_decisions(room_token,surface,user_id,spoke,rung) VALUES (?,'talk','alice',0,'mode_mention')", (old,))
        conn.execute("INSERT INTO web_chat_rooms(user_id,token,name) VALUES ('alice',?,'Example')", (old,))
        conn.execute("INSERT INTO web_chat_messages(user_id,token,text) VALUES ('alice',?,'notice')", (old,))
        conn.execute("INSERT INTO user_profiles(user_id,default_room,routing) VALUES ('alice',?,?)", (old, json.dumps({"alerts": f"room:{old},talk:{old}"})))
        conn.execute("INSERT INTO sent_emails(user_id,message_id,to_addr,conversation_token,origin_target) VALUES ('alice','<one@example.com>','bob@example.com',?,?)", (old, f"room:{old}"))
    assert room_relocate.migrate_database(database) == 0
    with db.get_db(database) as conn:
        mappings = dict(conn.execute("SELECT old_token,new_token FROM room_token_migration"))
        new, new_side = mappings[old], mappings[side]
        assert db.is_canonical_room_token(new)
        assert db.get_room(conn, old) is None
        assert db.get_room(conn, new_side).side_of == new
        assert db.get_task(conn, task).output_target == f"room:{new}, talk:{old}, web:{new_side}"
        assert db.get_task(conn, task).talk_delivery_token == old
        for table in ("room_members", "room_participants", "room_policy",
                      "room_vetoes", "room_notices", "room_epochs", "speech_gate_decisions", "messages"):
            assert conn.execute(f"SELECT count(*) FROM {table} WHERE room_token=?", (new,)).fetchone()[0] == 1
        assert conn.execute("SELECT conversation_token FROM credential_grant_rooms").fetchone()[0] == new
        assert conn.execute("SELECT conversation_token FROM talk_poll_state").fetchone()[0] == old
        assert conn.execute("SELECT conversation_token FROM talk_messages").fetchone()[0] == old
        assert conn.execute("SELECT value FROM istota_kv WHERE namespace='_provisioned_rooms'").fetchone()[0] == old
        assert db.get_room_binding(conn, new, "talk").surface_ref == old
        assert conn.execute("SELECT conversation_token FROM sent_emails").fetchone()[0] == old
        assert conn.execute("SELECT origin_target FROM sent_emails").fetchone()[0] == f"room:{new}"
        assert conn.execute("SELECT token FROM web_chat_messages").fetchone()[0] == new
        profile = conn.execute("SELECT default_room,routing FROM user_profiles").fetchone()
        assert profile[0] == new
        assert json.loads(profile[1]) == {"alerts": f"room:{new},talk:{old}"}
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    after = snapshot(database)
    assert room_relocate.migrate_database(database) == 0
    assert snapshot(database) == after


def test_migrate_rewrites_private_reply_tags(database):
    """`messages.about_room_token` and `tasks.about_room_token` name a shared
    room canonically, so a relocation carries them to the minted token."""
    with db.get_db(database) as conn:
        old = legacy(conn)
        private = db.create_web_chat_room(conn, "alice", "general").token
        tagged = db.add_message(conn, private, role="system", body="note",
                                origin_surface="web", about_room_token=old)
        untagged = db.add_message(conn, private, role="system", body="other",
                                  origin_surface="web", about_room_token="unrelated")
        task = db.create_task(conn, user_id="alice", source_type="web", prompt="hi",
                              conversation_token=private)
        conn.execute("UPDATE tasks SET about_room_token=?, status='completed' WHERE id=?",
                     (old, task))
    assert room_relocate.migrate_database(database) == 0
    with db.get_db(database) as conn:
        new = dict(conn.execute("SELECT old_token,new_token FROM room_token_migration"))[old]
        tags = dict(conn.execute("SELECT id, about_room_token FROM messages WHERE id IN (?, ?)",
                                 (tagged, untagged)))
        assert tags == {tagged: new, untagged: "unrelated"}
        assert conn.execute("SELECT about_room_token FROM tasks WHERE id=?",
                            (task,)).fetchone()[0] == new


@pytest.mark.parametrize("status", ["locked", "running", "pending_confirmation"])
def test_refuses_active_tasks_without_any_write(database, status, capsys):
    with db.get_db(database) as conn:
        legacy(conn)
        ident = db.create_task(conn, user_id="alice", prompt="hi", source_type="web")
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, ident))
    before = snapshot(database)
    assert room_relocate.migrate_database(database) == 1
    assert "refusal:" in capsys.readouterr().err
    assert snapshot(database) == before


@pytest.mark.parametrize("ddl", [
    "ALTER TABLE rooms ADD COLUMN future_token TEXT",
    "ALTER TABLE rooms ADD COLUMN future_token VARCHAR(255)",
    "ALTER TABLE rooms ADD COLUMN future_token",
    "CREATE TABLE future_holder (parent TEXT REFERENCES rooms(token))",
    "DROP TABLE tasks",
])
def test_refuses_unknown_schema_or_unreadable_tasks(database, ddl):
    with db.get_db(database) as conn:
        legacy(conn)
        conn.execute(ddl)
    before = snapshot(database)
    assert room_relocate.migrate_database(database) == 1
    assert snapshot(database) == before


@pytest.mark.parametrize("mode", ["dry_run", "list_only"])
def test_inspection_does_not_write(database, mode):
    with db.get_db(database) as conn:
        legacy(conn)
    before = snapshot(database)
    assert room_relocate.migrate_database(database, **{mode: True}) == 0
    assert snapshot(database) == before


def test_failed_room_rolls_back_and_other_rooms_resume(database):
    with db.get_db(database) as conn:
        legacy(conn, "a-room")
        legacy(conn, "b-room")
        conn.execute("""CREATE TRIGGER reject_move BEFORE UPDATE OF token ON rooms
            WHEN OLD.token='a-room' BEGIN SELECT RAISE(ABORT,'injected failure'); END""")
    assert room_relocate.migrate_database(database) == 2
    with db.get_db(database) as conn:
        assert db.get_room(conn, "a-room") is not None
        assert db.get_room_binding(conn, "a-room", "talk") is not None
        assert conn.execute("SELECT old_token FROM room_token_migration").fetchall()[0][0] == "b-room"
        conn.execute("DROP TRIGGER reject_move")
    assert room_relocate.migrate_database(database) == 0
    with db.get_db(database) as conn:
        assert conn.execute("SELECT count(*) FROM room_token_migration").fetchone()[0] == 2
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_email_and_memory_handlers_preserve_noncanonical_content(database):
    with db.get_db(database) as conn:
        old = legacy(conn, "email-thread-old", "email")
        conn.execute("INSERT INTO processed_emails(email_id,sender_email,thread_id) VALUES ('1','bob@example.com',?)", (old,))
        conn.execute("INSERT INTO processed_emails(email_id,sender_email,thread_id) VALUES ('2','bob@example.com','private-hash')")
        conn.execute("INSERT INTO sent_emails(user_id,message_id,to_addr,conversation_token,thread_id) VALUES ('alice','<one@example.com>','bob@example.com',?,'private-hash')", (old,))
        source = f"/srv/data/Channels/{old}/memories/note.md"
        conn.execute("""INSERT INTO memory_chunks(user_id,source_type,source_id,chunk_index,content,content_hash,metadata_json)
            VALUES (?,'channel_memory',?,0,?,'hash',?)""", (f"channel:{old}", source, old,
            json.dumps({"file_path": source, "label": old})))
    assert room_relocate.migrate_database(database) == 0
    with db.get_db(database) as conn:
        new = conn.execute("SELECT new_token FROM room_token_migration").fetchone()[0]
        assert [r[0] for r in conn.execute("SELECT thread_id FROM processed_emails ORDER BY id")] == [new, "private-hash"]
        assert tuple(conn.execute("SELECT conversation_token,thread_id FROM sent_emails").fetchone()) == (new, "private-hash")
        row = conn.execute("SELECT * FROM memory_chunks").fetchone()
        assert row["user_id"] == f"channel:{new}"
        assert row["source_id"] == source.replace(old, new)
        assert json.loads(row["metadata_json"]) == {"file_path": source.replace(old, new), "label": old}
        assert row["content"] == old


def test_requests_rebind_same_destination_and_preserve_approval(database):
    with db.get_db(database) as conn:
        old = legacy(conn)
        side = legacy(conn, "side-old", "web")
        destination = {"kind": "room", "room_token": old, "talk_ref": old, "label": old}
        request(conn, "post", destination, origin={"surface": "talk", "room_token": old, "channel": old, "talk_ref": old})
        from istota.rooms.side_rooms import _fingerprint
        request(conn, "whisper", {"kind": "side_room", "room_token": side, "parent": old},
                fingerprint=_fingerprint(side, old), kind="side_whisper",
                origin={"surface": "web", "channel": side, "room_token": side})
    assert room_relocate.migrate_database(database) == 0
    with db.get_db(database) as conn:
        mapping = dict(conn.execute("SELECT old_token,new_token FROM room_token_migration"))
        post = conn.execute("SELECT * FROM whatsapp_skill_requests WHERE id='post'").fetchone()
        dest = json.loads(post["destination"])
        assert dest == {**destination, "room_token": mapping[old]}
        assert post["binding_fingerprint"] == destination_fingerprint(dest)
        assert json.loads(post["origin"]) == {"surface": "talk", "room_token": mapping[old], "channel": old, "talk_ref": old}
        assert post["preview_digest"] == post["approved_digest"] == "preview-digest"
        assert post["content_hash"] == "body-digest"
        whisper = conn.execute("SELECT * FROM whatsapp_skill_requests WHERE id='whisper'").fetchone()
        assert whisper["binding_fingerprint"] == _fingerprint(mapping[side], mapping[old])
        assert json.loads(whisper["origin"])["channel"] == mapping[side]


def test_ambiguous_binding_refuses_without_writes(database):
    with db.get_db(database) as conn:
        conn.execute("DROP INDEX idx_room_bindings_unique_ref")
        legacy(conn, "one")
        legacy(conn, "two")
        conn.execute("UPDATE room_bindings SET surface_ref='same'")
    before = snapshot(database)
    assert room_relocate.migrate_database(database) == 1
    assert snapshot(database) == before


@pytest.mark.parametrize("mode", [{}, {"dry_run": True}, {"list_only": True}])
def test_missing_database_is_not_created(tmp_path, mode):
    path = tmp_path / "absent.db"
    assert room_relocate.migrate_database(path, **mode) == 1
    assert not path.exists()


@pytest.mark.parametrize("surface", ["email", "whatsapp"])
def test_omitted_group_ref_is_recovered_only_with_matching_hash(database, surface):
    with db.get_db(database) as conn:
        old = legacy(conn)
        db.add_room_binding(conn, old, surface, "native-group-ref")
        stored = {"kind": "room", "room_token": old, "talk_ref": old, "label": "Group"}
        complete = {**stored, surface + "_ref": "native-group-ref"}
        request(conn, "post", stored, fingerprint=destination_fingerprint(complete))
    assert room_relocate.migrate_database(database) == 0
    with db.get_db(database) as conn:
        new = conn.execute("SELECT new_token FROM room_token_migration").fetchone()[0]
        row = conn.execute("SELECT * FROM whatsapp_skill_requests").fetchone()
        assert row["binding_fingerprint"] == destination_fingerprint({**complete, "room_token": new})
        assert json.loads(row["destination"]) == {**stored, "room_token": new}


def test_changed_group_binding_cannot_be_reapproved_by_migration(database):
    with db.get_db(database) as conn:
        old = legacy(conn)
        db.add_room_binding(conn, old, "email", "new-thread")
        stored = {"kind": "room", "room_token": old, "talk_ref": old, "label": "Group"}
        request(conn, "post", stored, fingerprint=destination_fingerprint({**stored, "email_ref": "old-thread"}))
    before = snapshot(database)
    assert room_relocate.migrate_database(database) == 2
    assert snapshot(database) == before


def test_linked_relay_updates_both_hashes_but_phone_hashes_stay(database):
    with db.get_db(database) as conn:
        old = legacy(conn)
        dest = {"kind": "room", "room_token": old, "talk_ref": old, "label": "Example"}
        fingerprint = destination_fingerprint(dest)
        dest["fingerprint"] = fingerprint
        request(conn, "relay-request", dest, kind="relay_question")
        # Relay requests store their destination on message_relays only.
        conn.execute("UPDATE whatsapp_skill_requests SET destination=NULL WHERE id='relay-request'")
        conn.execute("""INSERT INTO message_relays(id,asker_user_id,recipient_user_id,surface,request_id,
            origin,provider,binding_fingerprint,state,destination) VALUES
            ('relay','alice','bob','room','relay-request',?,'room',?,'queued',?)""",
            (json.dumps({"surface": "web", "channel": old, "room_token": old}), fingerprint, json.dumps(dest)))
        request(conn, "phone", {"kind": "sms", "label": old}, fingerprint="phone-hash", kind="self_send")
    assert room_relocate.migrate_database(database) == 0
    with db.get_db(database) as conn:
        new = conn.execute("SELECT new_token FROM room_token_migration").fetchone()[0]
        relay = conn.execute("SELECT * FROM message_relays").fetchone()
        expected = destination_fingerprint({**dest, "room_token": new})
        assert relay["binding_fingerprint"] == expected
        assert json.loads(relay["destination"])["fingerprint"] == expected
        assert json.loads(relay["origin"])["channel"] == new
        assert conn.execute("SELECT binding_fingerprint FROM whatsapp_skill_requests WHERE id='relay-request'").fetchone()[0] == expected
        assert conn.execute("SELECT binding_fingerprint FROM whatsapp_skill_requests WHERE id='phone'").fetchone()[0] == "phone-hash"


def test_migration_bootstraps_mapping_only_after_guards(database):
    with db.get_db(database) as conn:
        legacy(conn)
        conn.execute("DROP TABLE room_token_migration")
    before = snapshot(database)
    assert room_relocate.migrate_database(database, dry_run=True) == 0
    assert snapshot(database) == before
    assert room_relocate.migrate_database(database) == 0
    with db.get_db(database) as conn:
        new = conn.execute("SELECT new_token FROM room_token_migration").fetchone()[0]
        assert db.resolve_room_token(conn, "talk", "old-talk") == new
        from istota.transport.routing import _canonical_room_token
        assert _canonical_room_token(conn, "email", "old-talk", cross_surface=False) == new
        conn.execute("DELETE FROM rooms WHERE token=?", (new,))
        assert _canonical_room_token(conn, "email", "old-talk", cross_surface=False) is None
        assert conn.execute("SELECT new_token FROM room_token_migration").fetchone()[0] == new


def test_permanent_mapping_never_consolidates_two_existing_rooms(database):
    with db.get_db(database) as conn:
        legacy(conn)
        canonical = db.mint_room_token()
        legacy(conn, canonical)
        conn.execute("INSERT INTO room_token_migration VALUES ('old-talk',?,'2026-01-01')", (canonical,))
    before = snapshot(database)
    assert room_relocate.migrate_database(database) == 2
    assert snapshot(database) == before


def test_interrupted_run_keeps_only_completed_room_transactions(database, monkeypatch):
    with db.get_db(database) as conn:
        legacy(conn, "a-room")
        legacy(conn, "b-room")
    original = room_relocate._migrate_room

    def interrupted(conn, old):
        new = original(conn, old)
        if old == "b-room":
            raise KeyboardInterrupt
        return new

    with monkeypatch.context() as patcher:
        patcher.setattr(room_relocate, "_migrate_room", interrupted)
        with pytest.raises(KeyboardInterrupt):
            room_relocate.migrate_database(database)
    with db.get_db(database) as conn:
        assert [r[0] for r in conn.execute("SELECT old_token FROM room_token_migration")] == ["a-room"]
        assert db.get_room(conn, "b-room") is not None
        assert db.get_room_binding(conn, "b-room", "talk") is not None
    assert room_relocate.migrate_database(database) == 0


def test_main_config_failure_is_a_refusal(monkeypatch, capsys):
    def fail():
        raise OSError("unavailable")
    monkeypatch.setattr("istota.config.load_config", fail)
    assert room_relocate.main([]) == 1
    assert "refusal: config_unreadable" in capsys.readouterr().err


def test_cli_dry_run_and_run(database):
    import subprocess
    import sys

    with db.get_db(database) as conn:
        legacy(conn)
    before = snapshot(database)
    command = [sys.executable, "-m", "istota.maintenance.room_relocate", "--db-path", str(database)]
    dry = subprocess.run([*command, "--dry-run"], text=True, capture_output=True)
    assert dry.returncode == 0, dry.stderr
    assert "pending: old-talk" in dry.stdout
    assert snapshot(database) == before
    run = subprocess.run(command, text=True, capture_output=True)
    assert run.returncode == 0, run.stderr
    assert "migrated: old-talk -> rm_" in run.stdout
    listed = subprocess.run([*command, "--list"], text=True, capture_output=True)
    assert listed.returncode == 0, listed.stderr
    assert "already-migrated: rm_" in listed.stdout


def test_migration_preserves_retention_orphans(database):
    with db.get_db(database) as conn:
        old = legacy(conn, "email-thread-old", "email")
        ident = db.create_task(conn, user_id="alice", source_type="email", prompt="mail", conversation_token=old)
        conn.execute("UPDATE tasks SET status='completed',completed_at=datetime('now','-30 days') WHERE id=?", (ident,))
        conn.execute("INSERT INTO processed_emails(email_id,sender_email,thread_id,task_id) VALUES ('1','bob@example.com',?,?)", (old, ident))
        assert db.cleanup_old_tasks(conn, 7) == 1
        before = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
        assert before
    assert room_relocate.migrate_database(database) == 0
    with db.get_db(database) as conn:
        new = conn.execute("SELECT new_token FROM room_token_migration").fetchone()[0]
        row = conn.execute("SELECT thread_id,task_id FROM processed_emails").fetchone()
        assert tuple(row) == (new, ident)
        assert [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")] == before


def test_existing_vector_index_survives_migration(database, monkeypatch):
    import struct
    sqlite_vec = pytest.importorskip("sqlite_vec")
    # Other tests may have probed a connection with extension loading disabled.
    monkeypatch.setattr("istota.memory.search._vec_available", None)
    with db.get_db(database) as conn:
        old = legacy(conn)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.execute("CREATE VIRTUAL TABLE memory_chunks_vec USING vec0(chunk_id INTEGER PRIMARY KEY, embedding FLOAT[384])")
        embedding = struct.pack("384f", *([0.25] * 384))
        conn.execute("INSERT INTO memory_chunks_vec(chunk_id,embedding) VALUES (1,?)", (embedding,))
    assert room_relocate.migrate_database(database) == 0
    with db.get_db(database) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        assert bytes(conn.execute("SELECT embedding FROM memory_chunks_vec WHERE chunk_id=1").fetchone()[0]) == embedding
        assert db.get_room(conn, old) is None


def test_new_foreign_key_violation_rolls_back_room(database):
    with db.get_db(database) as conn:
        legacy(conn)
        conn.execute("""CREATE TRIGGER bad_reference AFTER UPDATE OF token ON rooms
            BEGIN INSERT INTO room_members(room_token,user_id) VALUES ('absent-room','bob'); END""")
    before = snapshot(database)
    assert room_relocate.migrate_database(database) == 2
    assert snapshot(database) == before


def test_unavailable_vector_extension_refuses_without_changes(database, monkeypatch, capsys):
    with db.get_db(database) as conn:
        legacy(conn)
        # The detection is by persisted index name; the real vec0 happy path
        # is exercised above with the optional extension installed.
        conn.execute("CREATE TABLE memory_chunks_vec(chunk_id INTEGER PRIMARY KEY, embedding BLOB)")
    before = snapshot(database)
    monkeypatch.setattr("istota.memory.search.enable_vec_extension", lambda conn: False)
    assert room_relocate.migrate_database(database) == 1
    assert "refusal: vector_extension_unavailable" in capsys.readouterr().err
    assert snapshot(database) == before
