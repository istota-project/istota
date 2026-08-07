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
    def test_delete_talk_room_drops_membership_not_destroys(self, web_config, db_path):
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
            # Shared room is per-user-hidden, not globally archived or destroyed:
            # the registry row, its transcript, and the global flag all survive
            # so other participants keep seeing it (ISSUE-134).
            assert db.get_room(conn, "cpz").archived is False
            assert not db.is_room_member(conn, "cpz", "alice")
            assert len(db.get_messages(conn, "cpz")) == 1
        # hidden from the requester's list
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


class TestRoomListActivityOrder:
    """The sidebar renders the payload in the order it arrives, so the
    most-recently-active room has to come first — and each entry carries the
    `last_activity` stamp the client re-sorts on as messages stream in."""

    def _stamp(self, conn, table: str, key_col: str, key, ts: str) -> None:
        conn.execute(
            f"UPDATE {table} SET created_at = ? WHERE {key_col} = ?", (ts, key),
        )

    def test_payload_is_newest_activity_first(self, web_config, db_path):
        from istota import web_app
        with db.get_db(db_path) as conn:
            db.register_room(conn, "chatty", "alice", origin="web", name="Chatty")
            db.register_room(conn, "stale", "alice", origin="talk", name="Stale")
            # `chatty` is the *younger* room, so creation order alone would put
            # it second — only its newer message can lift it to the top.
            self._stamp(conn, "rooms", "token", "stale", "2026-01-01 00:00:00")
            self._stamp(conn, "rooms", "token", "chatty", "2026-01-02 00:00:00")
            mid = db.add_message(
                conn, "stale", role="user", body="ages ago", origin_surface="talk",
            )
            self._stamp(conn, "messages", "id", mid, "2026-01-03 09:00:00")
            mid = db.add_message(
                conn, "chatty", role="user", body="just now", origin_surface="web",
            )
            self._stamp(conn, "messages", "id", mid, "2026-05-05 09:00:00")
        rooms = web_app._chat_list_rooms("alice")
        tokens = [r["token"] for r in rooms if r["token"] in ("chatty", "stale")]
        assert tokens == ["chatty", "stale"]

    def test_every_entry_carries_an_iso_last_activity(self, web_config, db_path):
        from istota import web_app
        with db.get_db(db_path) as conn:
            db.register_room(conn, "cpz", "alice", origin="talk", name="#istota")
            self._stamp(conn, "rooms", "token", "cpz", "2026-01-01 00:00:00")
            mid = db.add_message(
                conn, "cpz", role="assistant", body="yo", origin_surface="talk",
            )
            self._stamp(conn, "messages", "id", mid, "2026-04-02 07:30:00")
        rooms = web_app._chat_list_rooms("alice")
        by_token = {r["token"]: r for r in rooms}
        # Same normalization the streamed message rows get, so the client can
        # compare a room's stamp against an arriving row's `created_at`.
        assert by_token["cpz"]["last_activity"] == "2026-04-02T07:30:00Z"
        # The auto-created default room has never been spoken in and still
        # carries a stamp (its creation time), so nothing sorts as undefined.
        assert all(r.get("last_activity") for r in rooms)
