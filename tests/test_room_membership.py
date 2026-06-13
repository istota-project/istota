"""ISSUE-134 — per-user room membership.

The unified room registry used to key a room to a single `user_id` (`rooms`
PK = token, `web_chat_rooms` UNIQUE token), so a group Talk room (bot + 2+
humans) surfaced in the web room list for exactly one arbitrary participant and
was invisible to the rest. These tests pin the membership model: a room is
shared (one token, one transcript) but *membership* is many-to-many, so every
participant sees it and each gets their own web handle.
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


class TestMembershipPrimitives:
    def test_register_room_makes_registering_user_a_member(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tok", "alice", origin="talk", name="#x")
            assert db.is_room_member(conn, "tok", "alice")
            assert db.list_room_members(conn, "tok") == ["alice"]

    def test_add_remove_member_idempotent(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tok", "alice", origin="talk")
            db.add_room_member(conn, "tok", "bob")
            db.add_room_member(conn, "tok", "bob")  # idempotent
            assert sorted(db.list_room_members(conn, "tok")) == ["alice", "bob"]
            db.remove_room_member(conn, "tok", "bob")
            assert db.list_room_members(conn, "tok") == ["alice"]
            assert not db.is_room_member(conn, "tok", "bob")

    def test_list_member_rooms_joins_membership_and_filters_archived(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "live", "alice", origin="talk", name="#live")
            db.register_room(conn, "gone", "alice", origin="talk", name="#gone")
            db.add_room_member(conn, "live", "bob")
            db.set_room_archived(conn, "gone", True)
            # alice sees both registry rows but gone is archived
            assert {r.token for r in db.list_member_rooms(conn, "alice")} == {"live"}
            # bob is only a member of live
            assert {r.token for r in db.list_member_rooms(conn, "bob")} == {"live"}
            # a non-member sees nothing
            assert db.list_member_rooms(conn, "carol") == []


class TestSharedTalkRoomVisibleToAllMembers:
    def test_warsaw_case_second_human_sees_room(self, web_config, db_path):
        """The #warsaw bug: monika registered the room first; stefan's later
        turns must still make him a member and surface the room in his web list.
        """
        from istota import web_app
        from istota.transport.ingest import record_inbound

        with db.get_db(db_path) as conn:
            # monika is first into the group Talk room — she registers it.
            db.register_room(conn, "r77", "monika", origin="talk", name="#warsaw")
            db.add_room_binding(conn, "r77", "talk", "r77")
            # stefan later sends into the already-registered room.
            room_token, task_id = record_inbound(
                conn,
                web_config,
                surface="talk",
                surface_ref="r77",
                user_id="stefan",
                text="hi",
                source_type="talk",
                channel_name="#warsaw",
            )
            assert room_token == "r77"
            assert task_id is not None

        # Both humans now see #warsaw in their own web room list.
        monika_rooms = {r["token"] for r in web_app._chat_list_rooms("monika")}
        stefan_rooms = {r["token"] for r in web_app._chat_list_rooms("stefan")}
        assert "r77" in monika_rooms
        assert "r77" in stefan_rooms

    def test_each_member_gets_their_own_web_handle(self, web_config, db_path):
        from istota import web_app

        with db.get_db(db_path) as conn:
            db.register_room(conn, "r77", "monika", origin="talk", name="#warsaw")
            db.add_room_member(conn, "r77", "stefan")

        web_app._chat_list_rooms("monika")
        web_app._chat_list_rooms("stefan")
        with db.get_db(db_path) as conn:
            rows = conn.execute(
                "SELECT user_id FROM web_chat_rooms WHERE token = 'r77' ORDER BY user_id"
            ).fetchall()
        owners = [r["user_id"] for r in rows]
        assert owners == ["monika", "stefan"]

    def test_ensure_web_chat_handle_is_user_scoped(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "r77", "monika", origin="talk", name="#warsaw")
            h1 = db.ensure_web_chat_handle(conn, "monika", "r77", "#warsaw")
            h2 = db.ensure_web_chat_handle(conn, "stefan", "r77", "#warsaw")
            assert h1.user_id == "monika"
            assert h2.user_id == "stefan"
            assert h1.id != h2.id
            assert h1.token == h2.token == "r77"


class TestPerUserHideDoesNotAffectOthers:
    def test_talk_room_delete_only_removes_requesters_membership(
        self, web_config, db_path
    ):
        from istota import web_app

        with db.get_db(db_path) as conn:
            db.register_room(conn, "r77", "monika", origin="talk", name="#warsaw")
            db.add_room_binding(conn, "r77", "talk", "r77")
            db.add_room_member(conn, "r77", "stefan")
        # stefan opens it in web (mints his handle), then deletes it from web.
        rooms = web_app._chat_list_rooms("stefan")
        rid = next(r["id"] for r in rooms if r["token"] == "r77")
        assert web_app._chat_delete_room("stefan", rid) == "ok"

        # stefan no longer sees it; monika still does; the room/transcript live.
        assert "r77" not in {r["token"] for r in web_app._chat_list_rooms("stefan")}
        assert "r77" in {r["token"] for r in web_app._chat_list_rooms("monika")}
        with db.get_db(db_path) as conn:
            assert db.get_room(conn, "r77") is not None
            assert db.get_room(conn, "r77").archived is False

    def test_web_hide_writes_tombstone_surviving_poll_re_add(
        self, web_config, db_path
    ):
        """Hiding an imported room writes a dismissal tombstone, so the poll's
        membership re-seed can't resurface it (only re-engagement does)."""
        from istota import web_app

        with db.get_db(db_path) as conn:
            db.register_room(conn, "r77", "stefan", origin="talk", name="#warsaw")
            db.add_room_binding(conn, "r77", "talk", "r77")
        rooms = web_app._chat_list_rooms("stefan")
        rid = next(r["id"] for r in rooms if r["token"] == "r77")
        assert web_app._chat_delete_room("stefan", rid) == "ok"
        with db.get_db(db_path) as conn:
            assert db.is_room_dismissed(conn, "r77", "stefan")
            db.add_room_member(conn, "r77", "stefan")  # poll re-seed
        assert "r77" not in {r["token"] for r in web_app._chat_list_rooms("stefan")}


class TestHideTombstone:
    """The per-user dismissal tombstone. The poll-time Talk-room registration
    backfill re-adds membership for every participant, so a hide can't rely on
    the dropped `room_members` row alone — `list_member_rooms` must also honor a
    tombstone, cleared only by the user's own re-engagement."""

    def test_dismiss_excludes_from_member_rooms_even_while_member(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tok", "alice", origin="talk", name="#x")
            assert {r.token for r in db.list_member_rooms(conn, "alice")} == {"tok"}
            db.dismiss_room(conn, "tok", "alice")
            # Still a member, but hidden by the tombstone.
            assert db.is_room_member(conn, "tok", "alice")
            assert db.is_room_dismissed(conn, "tok", "alice")
            assert db.list_member_rooms(conn, "alice") == []

    def test_undismiss_restores(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tok", "alice", origin="talk", name="#x")
            db.dismiss_room(conn, "tok", "alice")
            db.undismiss_room(conn, "tok", "alice")
            assert not db.is_room_dismissed(conn, "tok", "alice")
            assert {r.token for r in db.list_member_rooms(conn, "alice")} == {"tok"}

    def test_dismiss_is_per_user(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tok", "alice", origin="talk", name="#x")
            db.add_room_member(conn, "tok", "bob")
            db.dismiss_room(conn, "tok", "alice")
            # alice hidden, bob unaffected.
            assert db.list_member_rooms(conn, "alice") == []
            assert {r.token for r in db.list_member_rooms(conn, "bob")} == {"tok"}

    def test_tombstone_survives_membership_re_add(self, db_path):
        """The core guarantee: re-adding membership (what the poll backfill does
        every cycle) does NOT resurrect a hidden room — only undismiss does."""
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tok", "alice", origin="talk", name="#x")
            db.dismiss_room(conn, "tok", "alice")
            db.add_room_member(conn, "tok", "alice")  # poll re-seed
            assert db.list_member_rooms(conn, "alice") == []


class TestReviewFixes:
    """Edge cases surfaced by the Mulder/Scully review of the membership change."""

    def test_inbound_unarchives_a_rejoined_talk_room(self, web_config, db_path):
        from istota.transport.ingest import record_inbound

        with db.get_db(db_path) as conn:
            db.register_room(conn, "r77", "monika", origin="talk", name="#warsaw")
            db.set_room_archived(conn, "r77", True)  # bot was removed from NC
            # bot is re-added; monika messages again
            record_inbound(
                conn, web_config, surface="talk", surface_ref="r77",
                user_id="monika", text="back?", channel_name="#warsaw",
            )
            assert db.get_room(conn, "r77").archived is False

    def test_rehidden_then_readded_room_not_reported_archived(
        self, web_config, db_path
    ):
        from istota import web_app
        from istota.transport.ingest import record_inbound

        with db.get_db(db_path) as conn:
            db.register_room(conn, "r77", "monika", origin="talk", name="#warsaw")
            db.add_room_binding(conn, "r77", "talk", "r77")
            db.add_room_member(conn, "r77", "stefan")
        # stefan opens it, then hides (deletes from web).
        rooms = web_app._chat_list_rooms("stefan")
        rid = next(r["id"] for r in rooms if r["token"] == "r77")
        web_app._chat_delete_room("stefan", rid)
        assert "r77" not in {r["token"] for r in web_app._chat_list_rooms("stefan")}
        # stefan messages the room again → re-added as a member.
        with db.get_db(db_path) as conn:
            record_inbound(
                conn, web_config, surface="talk", surface_ref="r77",
                user_id="stefan", text="hi again", channel_name="#warsaw",
            )
        listed = {r["token"]: r for r in web_app._chat_list_rooms("stefan")}
        assert "r77" in listed
        assert listed["r77"]["archived"] is False  # stale flag cleared

    def test_hard_delete_removes_all_participant_handles_no_empty_list(
        self, web_config, db_path
    ):
        from istota import web_app

        with db.get_db(db_path) as conn:
            # A web-origin room carol created, that bob also became a member of
            # (as happens after an "Also open in Talk" promote + a second poster).
            room = db.create_web_chat_room(conn, "carol", "Shared")
            token = room.token
            db.add_room_member(conn, token, "bob")
        # bob lists it → mints his own handle.
        web_app._chat_list_rooms("bob")
        with db.get_db(db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) c FROM web_chat_rooms WHERE token = ?", (token,)
            ).fetchone()["c"] == 2
            # carol hard-deletes the room.
            owner_handle = db.get_web_chat_room_by_token(conn, token)
        carol_room_id = next(
            r["id"] for r in web_app._chat_list_rooms("carol") if r["token"] == token
        )
        assert web_app._chat_delete_room("carol", carol_room_id) == "ok"
        with db.get_db(db_path) as conn:
            # no orphan handle left for bob pointing at the dead token
            assert conn.execute(
                "SELECT COUNT(*) c FROM web_chat_rooms WHERE token = ?", (token,)
            ).fetchone()["c"] == 0
        # bob still gets a usable (non-empty) room list with a default room.
        bob_rooms = web_app._chat_list_rooms("bob")
        assert bob_rooms
        assert token not in {r["token"] for r in bob_rooms}


class TestMembershipBackfillMigration:
    def test_backfill_makes_every_talk_sender_a_member(self, db_path):
        with db.get_db(db_path) as conn:
            # A group Talk room registered (arbitrarily) under monika, no members.
            conn.execute(
                "INSERT INTO rooms (token, user_id, name, origin) "
                "VALUES ('r77', 'monika', '#warsaw', 'talk')"
            )
            # Two distinct talk senders in the room's task history.
            for uid in ("monika", "stefan", "stefan"):
                conn.execute(
                    "INSERT INTO tasks (status, source_type, user_id, prompt, "
                    "conversation_token) VALUES ('completed', 'talk', ?, 'x', 'r77')",
                    (uid,),
                )
            # A web room for carol.
            conn.execute(
                "INSERT INTO web_chat_rooms (user_id, token, name) "
                "VALUES ('carol', 'web-carol-1', 'Ideas')"
            )
            conn.execute(
                "INSERT INTO rooms (token, user_id, name, origin) "
                "VALUES ('web-carol-1', 'carol', 'Ideas', 'web')"
            )
            # Reset the membership state so the backfill re-runs from scratch.
            conn.execute("DELETE FROM room_members")
            conn.execute("DELETE FROM _migration_state WHERE name = 'room_members_v1'")

            db._migrate_room_members(conn)

            assert sorted(db.list_room_members(conn, "r77")) == ["monika", "stefan"]
            assert db.list_room_members(conn, "web-carol-1") == ["carol"]


class TestLegacySchemaMigration:
    """The risky half: existing prod DBs carry the legacy single-owner shape
    (web_chat_rooms UNIQUE(token), read_state PK without user_id). These exercise
    the in-place rebuilds against that shape, not a fresh schema."""

    def test_web_chat_rooms_rebuilt_to_per_user_unique_preserving_ids(self, tmp_path):
        import sqlite3

        conn = sqlite3.connect(tmp_path / "legacy.db")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "CREATE TABLE web_chat_rooms ("
            " id INTEGER PRIMARY KEY, user_id TEXT NOT NULL,"
            " token TEXT NOT NULL UNIQUE, name TEXT NOT NULL,"
            " archived INTEGER NOT NULL DEFAULT 0,"
            " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
            " updated_at TEXT NOT NULL DEFAULT (datetime('now')));"
            "INSERT INTO web_chat_rooms (id, user_id, token, name)"
            " VALUES (5, 'monika', 'r77', '#warsaw');"
        )
        db._migrate_web_chat_rooms_peruser(conn)
        # id preserved (a live frontend room id stays valid)
        assert conn.execute(
            "SELECT id FROM web_chat_rooms WHERE token='r77'"
        ).fetchone()["id"] == 5
        # a second participant can now hold a handle for the same Talk token
        conn.execute(
            "INSERT INTO web_chat_rooms (user_id, token, name) "
            "VALUES ('stefan', 'r77', '#warsaw')"
        )
        assert conn.execute(
            "SELECT COUNT(*) c FROM web_chat_rooms WHERE token='r77'"
        ).fetchone()["c"] == 2
        # idempotent: a second pass is a no-op (no exception, table intact)
        db._migrate_web_chat_rooms_peruser(conn)
        assert conn.execute(
            "SELECT COUNT(*) c FROM web_chat_rooms WHERE token='r77'"
        ).fetchone()["c"] == 2
        conn.close()

    def test_read_state_rebuilt_with_user_id(self, tmp_path):
        import sqlite3

        conn = sqlite3.connect(tmp_path / "legacy.db")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE room_read_state ("
            " room_token TEXT NOT NULL, surface TEXT NOT NULL,"
            " last_read_message_id INTEGER NOT NULL DEFAULT 0,"
            " PRIMARY KEY (room_token, surface))"
        )
        db._migrate_room_read_state_peruser(conn)
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='room_read_state'"
        ).fetchone()[0]
        assert "user_id" in sql
        # idempotent
        db._migrate_room_read_state_peruser(conn)
        conn.close()


class TestWebChatRoomsPerUserUnique:
    def test_two_users_one_token_both_handles_persist(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "r77", "monika", origin="talk")
            db.ensure_web_chat_handle(conn, "monika", "r77", "#warsaw")
            db.ensure_web_chat_handle(conn, "stefan", "r77", "#warsaw")
            count = conn.execute(
                "SELECT COUNT(*) c FROM web_chat_rooms WHERE token = 'r77'"
            ).fetchone()["c"]
            assert count == 2
