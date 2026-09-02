"""The room builders, and the pin that keeps them honest.

`tests/support/rooms.py` is test infrastructure, so it needs tests of its own: a
builder that writes a room the product would never write produces green tests
about a system that does not exist. The claim that matters is in
`TestPinnedAgainstTheProducers` — the builders write what
`transport.ingest.record_inbound` and `web_app._chat_promote_to_talk` write, run
side by side and diffed, rather than compared against a hand list that would
drift the moment either producer gained a write.
"""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from istota import db
from istota.config import Config, NextcloudConfig
from istota.transport.ingest import record_inbound

from .support.rooms import RoomShape, plain_talk_room, promoted_room

# What `db._new_web_chat_token` produces. Asserted against a freshly minted real
# token below, so a change to that format turns this red instead of leaving the
# builder quietly generating a shape the product no longer mints.
WEB_TOKEN_RE = re.compile(r"^web-[^-]+-[0-9a-f]{12}$")


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


@pytest.fixture
def conn(db_path):
    with db.get_db(db_path) as c:
        yield c


class TestPlainTalkRoom:
    def test_canonical_token_is_the_talk_ref(self, conn):
        room = plain_talk_room(conn, "alice")
        assert room.canonical == room.talk_ref
        assert room.diverges is False
        assert room.origin == "talk"

    def test_writes_registry_binding_and_membership(self, conn):
        room = plain_talk_room(conn, "alice")
        registered = db.get_room(conn, room.canonical)
        assert registered is not None
        assert registered.origin == "talk"
        assert registered.name == room.name
        assert db.resolve_room_token(conn, "talk", room.talk_ref) == room.canonical
        assert db.is_room_member(conn, room.canonical, "alice")
        # The membership assertion above is what this one exists for: a room
        # with no member row is invisible to the web listing.
        assert [r.token for r in db.list_member_rooms(conn, "alice")] == [
            room.canonical,
        ]

    def test_explicit_token_and_name_are_honoured(self, conn):
        room = plain_talk_room(conn, "alice", token="cpzpcfx2", name="#istota")
        assert room == RoomShape(
            canonical="cpzpcfx2", talk_ref="cpzpcfx2", origin="talk", name="#istota",
        )
        assert db.get_room(conn, "cpzpcfx2").name == "#istota"


class TestPromotedRoom:
    def test_canonical_token_diverges_from_the_talk_ref(self, conn):
        room = promoted_room(conn, "alice")
        assert room.canonical != room.talk_ref
        assert room.diverges is True
        assert room.origin == "web"

    def test_the_canonical_token_names_no_talk_conversation(self, conn):
        # The whole point of the shape. Anything resolving the binding gets the
        # Talk ref; anything handing the canonical token to the Talk API is
        # ISSUE-400, and here it resolves to nothing rather than working by
        # accident.
        room = promoted_room(conn, "alice")
        assert db.resolve_room_token(conn, "talk", room.talk_ref) == room.canonical
        assert db.resolve_room_token(conn, "talk", room.canonical) is None

    def test_writes_both_bindings_membership_and_a_web_handle(self, conn):
        room = promoted_room(conn, "alice")
        assert db.get_room(conn, room.canonical).origin == "web"
        assert {(b.surface, b.surface_ref) for b in db.list_room_bindings(
            conn, room.canonical,
        )} == {("web", room.canonical), ("talk", room.talk_ref)}
        assert db.is_room_member(conn, room.canonical, "alice")
        assert [r.token for r in db.list_member_rooms(conn, "alice")] == [
            room.canonical,
        ]
        handle = db.get_web_chat_room_by_token(conn, room.canonical)
        assert handle is not None
        assert (handle.user_id, handle.name) == ("alice", room.name)

    def test_explicit_tokens_and_name_are_honoured(self, conn):
        room = promoted_room(
            conn, "alice",
            canonical="web-alice-aaaabbbbcccc", talk_ref="RealTalkRoom", name="Ideas",
        )
        assert room == RoomShape(
            canonical="web-alice-aaaabbbbcccc", talk_ref="RealTalkRoom",
            origin="web", name="Ideas",
        )

    def test_refuses_to_build_a_room_that_does_not_diverge(self, conn):
        with pytest.raises(ValueError, match="two different tokens"):
            promoted_room(conn, "alice", canonical="same", talk_ref="same")


class TestGeneratedDefaults:
    def test_many_rooms_in_one_connection_do_not_collide(self, conn):
        shapes = [plain_talk_room(conn, "alice") for _ in range(10)]
        shapes += [promoted_room(conn, "alice") for _ in range(10)]
        canonicals = [s.canonical for s in shapes]
        talk_refs = [s.talk_ref for s in shapes]
        assert len(set(canonicals)) == len(canonicals)
        assert len(set(talk_refs)) == len(talk_refs)
        # A plain room's two tokens are the same string by design, so 10 plain
        # rooms contribute 10 and 10 promoted ones contribute 20.
        assert len({t for s in shapes for t in (s.canonical, s.talk_ref)}) == 30
        assert len(db.list_member_rooms(conn, "alice")) == 20

    def test_generated_canonical_has_the_shape_a_real_web_token_has(self, conn):
        real = db.create_web_chat_room(conn, "alice", "Ideas").token
        built = promoted_room(conn, "alice").canonical
        assert WEB_TOKEN_RE.match(real), real
        assert WEB_TOKEN_RE.match(built), built

    def test_a_repeated_canonical_token_is_refused(self, conn):
        room = plain_talk_room(conn, "alice", token="cpzpcfx2")
        with pytest.raises(ValueError, match="already registered"):
            plain_talk_room(conn, "alice", token=room.canonical)
        with pytest.raises(ValueError, match="already registered"):
            promoted_room(conn, "alice", canonical=room.canonical)

    def test_a_talk_ref_bound_to_another_room_is_refused(self, conn):
        room = plain_talk_room(conn, "alice", token="cpzpcfx2")
        with pytest.raises(ValueError, match="already bound"):
            promoted_room(conn, "alice", talk_ref=room.talk_ref)


def _room_model(conn) -> dict[str, list[tuple]]:
    """Every room-model row, with the columns nothing chooses left out.

    `id`, `created_at` and `updated_at` differ between two runs by construction.
    `messages` and `tasks` are out of scope on purpose: a builder builds a room,
    not a turn, and `record_inbound` writes both.
    """
    def rows(sql: str) -> list[tuple]:
        return [tuple(r) for r in conn.execute(sql).fetchall()]

    return {
        "rooms": rows(
            "SELECT token, user_id, name, origin, archived, model, effort "
            "FROM rooms ORDER BY token"
        ),
        "room_bindings": rows(
            "SELECT room_token, surface, surface_ref FROM room_bindings "
            "ORDER BY room_token, surface"
        ),
        "room_members": rows(
            "SELECT room_token, user_id FROM room_members "
            "ORDER BY room_token, user_id"
        ),
        "room_dismissals": rows(
            "SELECT room_token, user_id FROM room_dismissals "
            "ORDER BY room_token, user_id"
        ),
        "web_chat_rooms": rows(
            "SELECT user_id, token, name, archived FROM web_chat_rooms "
            "ORDER BY user_id, token"
        ),
    }


class TestPinnedAgainstTheProducers:
    """Run the real producer and the builder against two fresh databases and
    diff the room model. A producer that grows a write turns this red."""

    @pytest.fixture
    def two_dbs(self, tmp_path):
        produced, built = tmp_path / "produced.db", tmp_path / "built.db"
        db.init_db(produced)
        db.init_db(built)
        return produced, built

    def test_plain_talk_room_matches_record_inbound(self, two_dbs):
        produced, built = two_dbs
        config = Config()
        config.db_path = produced
        with db.get_db(produced) as conn:
            token, task_id = record_inbound(
                conn, config, surface="talk", surface_ref="cpzpcfx2",
                user_id="alice", text="hi", channel_name="#istota",
            )
        assert token == "cpzpcfx2" and task_id is not None  # the room branch ran

        with db.get_db(built) as conn:
            plain_talk_room(conn, "alice", token="cpzpcfx2", name="#istota")

        with db.get_db(produced) as a, db.get_db(built) as b:
            assert _room_model(b) == _room_model(a)

    @pytest.mark.asyncio
    async def test_promoted_room_matches_create_plus_promote(self, two_dbs):
        from istota import web_app

        produced, built = two_dbs
        config = Config()
        config.db_path = produced
        config.nextcloud = NextcloudConfig(
            url="https://nc.example", username="bot", app_password="pw",
        )
        with db.get_db(produced) as conn:
            handle = db.create_web_chat_room(conn, "alice", "Ideas")

        fake = MagicMock()
        fake.create_conversation = AsyncMock(return_value={"token": "promoted-tok"})
        fake.add_participant = AsyncMock(return_value={})
        fake.send_message = AsyncMock(return_value={})
        fake.aclose = AsyncMock()
        previous_config = web_app._config
        web_app._config = config
        try:
            with patch("istota.talk.TalkClient", return_value=fake):
                status, _ = await web_app._chat_promote_to_talk("alice", handle.id)
        finally:
            web_app._config = previous_config
        assert status == "ok"  # the promote ran; a refusal writes nothing

        with db.get_db(built) as conn:
            room = promoted_room(
                conn, "alice",
                canonical=handle.token, talk_ref="promoted-tok", name="Ideas",
            )
        assert room.diverges

        with db.get_db(produced) as a, db.get_db(built) as b:
            assert _room_model(b) == _room_model(a)
