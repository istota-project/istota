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
        # A room nobody has spoken in falls back to its creation time, so
        # nothing in the payload ever sorts as undefined.
        assert all(r.get("last_activity") for r in rooms)


class TestListingCarriesTheTalkRef:
    """ISSUE-342 — a promoted room has to say it is on Talk.

    `RoomSettings.svelte` decides with `origin === 'talk' || !!talk_token`. A
    promoted room keeps `origin='web'` by design, so the whole answer rests on
    `talk_token` — and the listing never sent it. Worse, the room-list refresh
    merges `talk_token: fresh.talk_token` unconditionally, so a poll *erased*
    the value the promote response had just put in the store: the room reverted
    to reading as istota-only and re-offered "Also open in Talk", which the
    backend then refuses.
    """

    def test_promoted_room_carries_its_talk_ref(self, web_config, db_path):
        from istota import web_app
        with db.get_db(db_path) as conn:
            db.create_web_chat_room(conn, "alice", "general")
            token = db.list_web_chat_rooms(conn, "alice")[0].token
            db.add_room_binding(conn, token, "talk", "tk4ab9cd")
        by_token = {r["token"]: r for r in web_app._chat_list_rooms("alice")}
        assert by_token[token]["origin"] == "web"
        assert by_token[token]["talk_token"] == "tk4ab9cd"

    def test_talk_origin_room_carries_its_own_token(self, web_config, db_path):
        from istota import web_app
        with db.get_db(db_path) as conn:
            db.register_room(conn, "cpz", "alice", origin="talk", name="#istota")
            db.add_room_binding(conn, "cpz", "talk", "cpz")
        by_token = {r["token"]: r for r in web_app._chat_list_rooms("alice")}
        assert by_token["cpz"]["talk_token"] == "cpz"

    def test_unpromoted_web_room_carries_none(self, web_config, db_path):
        from istota import web_app
        with db.get_db(db_path) as conn:
            db.create_web_chat_room(conn, "alice", "Ideas")
            token = db.list_web_chat_rooms(conn, "alice")[0].token
        by_token = {r["token"]: r for r in web_app._chat_list_rooms("alice")}
        assert by_token[token]["talk_token"] is None

    def test_one_users_binding_does_not_leak_into_anothers_listing(
        self, web_config, db_path,
    ):
        from istota import web_app
        with db.get_db(db_path) as conn:
            db.register_room(conn, "shared", "alice", origin="talk", name="#shared")
            db.add_room_binding(conn, "shared", "talk", "shared")
            db.create_web_chat_room(conn, "bob", "Ideas")
            bob_token = db.list_web_chat_rooms(conn, "bob")[0].token
        by_token = {r["token"]: r for r in web_app._chat_list_rooms("bob")}
        assert bob_token in by_token
        assert by_token[bob_token]["talk_token"] is None
        assert "shared" not in by_token

    def test_patch_response_matches_the_listing_shape(self, web_config, db_path):
        # The PATCH response is merged into the client's room record, so a key
        # the listing carries and the PATCH omits reads as absent to any
        # consumer that replaces rather than spreads.
        from istota import web_app
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "general")
            db.add_room_binding(conn, room.token, "talk", "tk4ab9cd")
        listed = {r["token"]: r for r in web_app._chat_list_rooms("alice")}[room.token]
        patched = web_app._chat_update_room("alice", room.id, "#general", None)
        assert patched["name"] == "#general"
        assert patched["origin"] == listed["origin"] == "web"
        assert patched["talk_token"] == listed["talk_token"] == "tk4ab9cd"


class TestDefaultRoomAsksTheRegistry:
    """ISSUE-342 — the first web visit must not mint a second `general`.

    `_chat_list_rooms` calls `ensure_default_web_chat_room` before it reads the
    registry, and that helper counted `web_chat_rooms` handles — which the
    listing itself creates a few lines later. So a user whose rooms all came
    from Talk looked room-less on their first visit and got a web-origin
    `general` beside the Talk one provisioning had already made. ISSUE-134
    named this helper in its audit list and the pass was never taken.
    """

    def test_a_talk_member_gets_no_second_general(self, web_config, db_path):
        from istota import web_app
        with db.get_db(db_path) as conn:
            db.register_room(conn, "rm1a2b3c", "alice", origin="talk", name="general")
            db.add_room_binding(conn, "rm1a2b3c", "talk", "rm1a2b3c")
        rooms = web_app._chat_list_rooms("alice")
        assert [r["token"] for r in rooms] == ["rm1a2b3c"]

    def test_the_default_helper_returns_a_handle_on_the_member_room(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "rm1a2b3c", "alice", origin="talk", name="general")
            handle = db.ensure_default_web_chat_room(conn, "alice")
        assert handle.token == "rm1a2b3c"
        assert handle.user_id == "alice"

    def test_a_user_with_no_rooms_at_all_still_gets_one(self, db_path):
        with db.get_db(db_path) as conn:
            room = db.ensure_default_web_chat_room(conn, "alice")
        assert room.name == "general"
        assert room.token.startswith("web-alice-")

    def test_a_dismissed_room_does_not_count_as_having_one(self, db_path):
        # A hidden room is not a room the user can post into, so it must not
        # suppress the default the way a live membership does.
        with db.get_db(db_path) as conn:
            db.register_room(conn, "rm1a2b3c", "alice", origin="talk", name="general")
            db.dismiss_room(conn, "rm1a2b3c", "alice")
            room = db.ensure_default_web_chat_room(conn, "alice")
        assert room.token.startswith("web-alice-")

    def test_a_shared_room_is_never_the_default_delivery_target(self, db_path):
        # `default_web_room_token` resolves a bare `web` route — an alert, the
        # execution log — and a room with other members in it is one they read.
        with db.get_db(db_path) as conn:
            db.register_room(conn, "shared", "alice", origin="talk", name="general")
            db.add_room_member(conn, "shared", "bob")
            room = db.ensure_default_web_chat_room(conn, "alice")
        assert room.token.startswith("web-alice-")

    def test_a_channel_room_is_never_the_default_delivery_target(self, db_path):
        # `logs` and `alerts` are machine-owned, and the boot sequence posts
        # into `alerts` — so activity order alone would hand the user's default
        # to whichever the daemon last wrote to, permanently.
        from istota import user_profiles

        user_profiles.update_profile_with_status(
            db_path, "alice", log_channel="lg4d5e6f", alerts_channel="al7g8h9i",
        )
        with db.get_db(db_path) as conn:
            db.register_room(conn, "lg4d5e6f", "alice", origin="talk", name="logs")
            db.register_room(conn, "al7g8h9i", "alice", origin="talk", name="alerts")
            room = db.ensure_default_web_chat_room(conn, "alice")
        assert room.token.startswith("web-alice-")

    def test_the_talkable_room_wins_over_the_channel_rooms(self, db_path):
        from istota import user_profiles

        user_profiles.update_profile_with_status(
            db_path, "alice", log_channel="lg4d5e6f", alerts_channel="al7g8h9i",
        )
        with db.get_db(db_path) as conn:
            db.register_room(conn, "rm1a2b3c", "alice", origin="talk", name="general")
            db.register_room(conn, "lg4d5e6f", "alice", origin="talk", name="logs")
            db.register_room(conn, "al7g8h9i", "alice", origin="talk", name="alerts")
            # The daemon posted into `alerts` at boot, so it is the most
            # recently active room. Activity order alone would pick it.
            db.add_message(
                conn, "al7g8h9i", role="system", body="up", origin_surface="web",
            )
            room = db.ensure_default_web_chat_room(conn, "alice")
        assert room.token == "rm1a2b3c"

    def test_the_listing_still_shows_every_room_it_did_not_invent(
        self, web_config, db_path,
    ):
        # The listing does not need a default invented for it — it mints handles
        # in its own loop — so a user whose only room is shared still sees that
        # room and nothing else.
        from istota import web_app
        with db.get_db(db_path) as conn:
            db.register_room(conn, "shared", "alice", origin="talk", name="general")
            db.add_room_member(conn, "shared", "bob")
        assert [r["token"] for r in web_app._chat_list_rooms("alice")] == ["shared"]

    def test_an_archived_handle_is_cleared_by_the_fallback(self, db_path):
        # The ISSUE-134 "hid it, then was re-added" state: the handle carries
        # the per-user archived flag while the registry room does not.
        with db.get_db(db_path) as conn:
            db.register_room(conn, "rm1a2b3c", "alice", origin="talk", name="general")
            handle = db.ensure_web_chat_handle(conn, "alice", "rm1a2b3c", "general")
            db.update_web_chat_room(conn, handle.id, archived=True)
            room = db.ensure_default_web_chat_room(conn, "alice")
        assert room.token == "rm1a2b3c"
        assert room.archived is False

    def test_web_transport_default_room_follows(self, db_path):
        from istota.transport.web import default_web_room_token
        cfg = Config()
        cfg.db_path = db_path
        with db.get_db(db_path) as conn:
            db.register_room(conn, "rm1a2b3c", "alice", origin="talk", name="general")
        assert default_web_room_token(cfg, "alice") == "rm1a2b3c"
