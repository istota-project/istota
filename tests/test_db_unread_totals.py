"""The aggregate agrees with the sidebar's visible-room unread semantics."""

from istota import db


def test_aggregate_agrees_with_visible_rooms_and_keeps_surfaces_separate(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    with db.get_db(path) as conn:
        rooms = {}
        for name in ("visible", "shared", "archived", "dismissed", "email", "other"):
            owner = "bob" if name == "other" else "alice"
            room = db.register_room(conn, None, owner, origin="web", name=name)
            rooms[name] = room
            for role in ("assistant", "user", "system", "assistant"):
                db.add_message(conn, room.token, role=role, body="Message", origin_surface="web")
        db.add_room_member(conn, rooms["shared"].token, "bob")
        db.set_room_archived(conn, rooms["archived"].token, True)
        db.dismiss_room(conn, rooms["dismissed"].token, "alice")
        db.add_room_binding(conn, rooms["email"].token, "email", "thread:one")
        db.set_room_read_state(conn, rooms["visible"].token, "web", db.room_max_message_id(conn, rooms["visible"].token), "alice")
        db.set_room_read_state(conn, rooms["shared"].token, "talk", db.room_max_message_id(conn, rooms["shared"].token), "alice")
        hidden = db.hidden_room_tokens_for_member(conn, "alice")
        assert rooms["email"].token in hidden
        expected = sum(db.count_unread_messages(conn, room.token, "web", "alice")
                       for room in db.list_member_rooms(conn, "alice") if room.token not in hidden)
        assert expected == 3
        assert db.total_unread_messages(conn, "alice", "web") == expected
        assert db.total_unread_messages(conn, "alice", "talk") == 3
        assert db.total_unread_messages(conn, "bob", "web") == 6
        assert db.total_unread_messages(conn, "unknown", "web") == 0
        # A read aggregate does not seed a cursor. The existing web counter
        # still seeds first-use rooms and then delegates to the same aggregate.
        assert db.get_room_read_state(conn, rooms["shared"].token, "web", "alice") == 0
        assert db.count_user_web_unread_messages(conn, "alice") == 0
        assert db.total_unread_messages(conn, "alice", "web") == 0
