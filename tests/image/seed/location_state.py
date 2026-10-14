"""Seed pre-sharing requests and witness the upgraded SQLite contract."""

import json
import sqlite3
import sys

KINDS = ("self_send", "relay_question", "side_whisper", "room_post", "purchase", "recovery_fill")


def seed(path):
    with sqlite3.connect(path) as conn:
        for kind in KINDS:
            conn.execute(
                "INSERT INTO whatsapp_skill_requests "
                "(id,requester_user_id,request_key,kind,recipient_user_id,content_hash,"
                "service_hash,provider,binding_fingerprint,state) "
                "VALUES (?,?,?,?,?,'h','s','room','fp','sent')",
                ("upgrade-location-" + kind, "alice", kind, kind, "alice"))
        conn.execute("UPDATE whatsapp_skill_requests SET destination=? WHERE kind='side_whisper'",
                     ('{"kind":"private_reply"}',))
        conn.execute("CREATE TABLE location_upgrade_child(request_id TEXT REFERENCES whatsapp_skill_requests(id))")
        conn.execute("INSERT INTO location_upgrade_child VALUES ('upgrade-location-self_send')")
        conn.execute("CREATE INDEX location_upgrade_index ON whatsapp_skill_requests(kind)")
        conn.execute("CREATE TABLE location_upgrade_audit(request_id TEXT)")
        conn.execute("CREATE TRIGGER location_upgrade_trigger AFTER UPDATE ON whatsapp_skill_requests "
                     "BEGIN INSERT INTO location_upgrade_audit VALUES (NEW.id); END")


def witness(path):
    conn = sqlite3.connect(path)
    try:
        retained = [r[0] for r in conn.execute(
            "SELECT kind FROM whatsapp_skill_requests WHERE id LIKE 'upgrade-location-%' ORDER BY kind")]
        assert retained == sorted(KINDS), retained
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='location_upgrade_index'").fetchone()
        conn.execute("UPDATE whatsapp_skill_requests SET kind='location_share' WHERE id='upgrade-location-self_send'")
        assert conn.execute("SELECT 1 FROM location_upgrade_audit WHERE request_id='upgrade-location-self_send'").fetchone()
        conn.execute("INSERT INTO rooms(token,user_id,origin,name) VALUES ('location-upgrade-room','alice','web','Hiking')")
        tasks = [conn.execute("INSERT INTO tasks(user_id,source_type,prompt) VALUES ('alice','web','Share')").lastrowid
                 for _ in range(2)]
        insert = ("INSERT INTO location_share_grants "
                  "(id,owner_user_id,room_token,origin_task_id,request_id,request_key,precision,"
                  "duration_seconds,audience_json,binding_fingerprint,room_epoch,state) "
                  "VALUES (?,'alice','location-upgrade-room',?,?,?,'exact',3600,'[]','fp',0,?)")
        conn.execute(insert, ('upgrade-active', tasks[0], 'upgrade-location-self_send', 'active', 'active'))
        conn.execute(insert, ('upgrade-held', tasks[1], 'upgrade-location-room_post', 'held', 'held'))
        for sql in (
            "UPDATE location_share_grants SET state='active' WHERE id='upgrade-held'",
            "UPDATE location_share_grants SET precision='street' WHERE id='upgrade-active'",
            "UPDATE location_share_grants SET duration_seconds=299 WHERE id='upgrade-active'",
        ):
            try:
                conn.execute(sql)
            except sqlite3.IntegrityError:
                pass
            else:
                raise AssertionError("upgraded constraint accepted invalid grant")
        conn.execute("DELETE FROM tasks WHERE id=?", (tasks[0],))
        assert conn.execute("SELECT state,origin_task_id FROM location_share_grants WHERE id='upgrade-active'").fetchone() == ('active', None)
        conn.execute("UPDATE whatsapp_skill_requests SET state='cancelled' WHERE id='upgrade-location-room_post'")
        assert conn.execute("SELECT state FROM location_share_grants WHERE id='upgrade-held'").fetchone() == ('cancelled',)
        conn.execute("DELETE FROM whatsapp_skill_requests WHERE id='upgrade-location-room_post'")
        assert conn.execute("SELECT request_id FROM location_share_grants WHERE id='upgrade-held'").fetchone() == (None,)
        conn.execute("UPDATE whatsapp_skill_requests SET state='held' WHERE id='upgrade-location-purchase'")
        conn.execute(insert, ('upgrade-room-delete', None, 'upgrade-location-purchase', 'delete', 'held'))
        conn.execute("DELETE FROM rooms WHERE token='location-upgrade-room'")
        assert conn.execute("SELECT count(*) FROM location_share_grants WHERE id LIKE 'upgrade-%'").fetchone() == (0,)
        assert conn.execute("SELECT state FROM whatsapp_skill_requests WHERE id='upgrade-location-purchase'").fetchone() == ('cancelled',)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        return {"retained_kinds": retained, "constraints": True, "cleanup": True}
    finally:
        conn.rollback()
        conn.close()


if __name__ == "__main__":
    operation = seed if sys.argv[1] == "seed" else witness
    print(json.dumps(operation(sys.argv[2])))
