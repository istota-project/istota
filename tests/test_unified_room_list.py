"""Stage 6 — room-list sync: Talk rooms surface in the web room list.

The web room list is driven by the unified `rooms` registry. A Talk room the
bot joined surfaces automatically (lazily registered on first inbound), is given
a web_chat_rooms handle (the frontend's integer id) + a web binding on first
listing, and is hidden (archived) rather than destroyed when deleted from web.
"""

import pytest

from istota import db
from istota.config import Config


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


@pytest.fixture
def web_config(db_path):
    from istota import web_app
    web_app._config = Config()
    web_app._config.db_path = db_path
    return web_app._config


class TestRoomListSurfacesTalk:
    def test_talk_room_appears_with_origin_and_handle(self, web_config, db_path):
        from istota import web_app
        with db.get_db(db_path) as conn:
            db.register_room(conn, "cpz", "alice", origin="talk", name="#istota")
            db.add_room_binding(conn, "cpz", "talk", "cpz")
        rooms = web_app._chat_list_rooms("alice")
        by_token = {r["token"]: r for r in rooms}
        assert "cpz" in by_token
        talk = by_token["cpz"]
        assert talk["origin"] == "talk"
        assert talk["name"] == "#istota"
        assert isinstance(talk["id"], int)  # frontend handle
        # web binding materialized on listing
        with db.get_db(db_path) as conn:
            assert db.resolve_room_token(conn, "web", "cpz") == "cpz"

    def test_web_rooms_still_listed(self, web_config, db_path):
        from istota import web_app
        with db.get_db(db_path) as conn:
            db.create_web_chat_room(conn, "alice", "Ideas")
        rooms = web_app._chat_list_rooms("alice")
        names = {r["name"] for r in rooms}
        assert "Ideas" in names
        assert all(r["origin"] in ("web", "talk") for r in rooms)


class TestDeleteGuard:
    def test_delete_talk_room_archives_not_destroys(self, web_config, db_path):
        from istota import web_app
        with db.get_db(db_path) as conn:
            db.register_room(conn, "cpz", "alice", origin="talk", name="#istota")
            db.add_room_binding(conn, "cpz", "talk", "cpz")
            db.add_message(conn, "cpz", role="user", body="hi", origin_surface="talk", task_id=1)
        # listing materializes the handle
        rooms = web_app._chat_list_rooms("alice")
        handle_id = next(r["id"] for r in rooms if r["token"] == "cpz")

        assert web_app._chat_delete_room("alice", handle_id) == "ok"
        with db.get_db(db_path) as conn:
            # registry room archived, not gone; messages preserved
            assert db.get_room(conn, "cpz").archived is True
            assert len(db.get_messages(conn, "cpz")) == 1
        # hidden from the list
        assert "cpz" not in {r["token"] for r in web_app._chat_list_rooms("alice")}

    def test_delete_web_room_hard_deletes(self, web_config, db_path):
        from istota import web_app
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "Scratch")
        assert web_app._chat_delete_room("alice", room.id) == "ok"
        with db.get_db(db_path) as conn:
            assert db.get_room(conn, room.token) is None  # gone


class TestTalkRenameFlowBack:
    def test_talk_rename_updates_registry(self, db_path):
        from istota.transport.ingest import record_inbound
        cfg = Config()
        cfg.db_path = db_path
        with db.get_db(db_path) as conn:
            record_inbound(conn, cfg, surface="talk", surface_ref="cpz",
                           user_id="alice", text="hi", channel_name="Old")
        with db.get_db(db_path) as conn:
            assert db.get_room(conn, "cpz").name == "Old"
        # A later inbound carrying a new Talk displayName renames the registry.
        with db.get_db(db_path) as conn:
            record_inbound(conn, cfg, surface="talk", surface_ref="cpz",
                           user_id="alice", text="hi again", channel_name="Renamed")
        with db.get_db(db_path) as conn:
            assert db.get_room(conn, "cpz").name == "Renamed"
