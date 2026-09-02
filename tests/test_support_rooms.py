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

def web_token_re(user_id: str) -> re.Pattern:
    """What `db._new_web_chat_token` produces for this user.

    Built per user rather than written once with `[^-]+`, because a Nextcloud
    account name may contain a hyphen and that pattern would reject a perfectly
    real token. Asserted against a freshly minted one below, so a change to the
    product's format turns this red instead of leaving the builder quietly
    generating a shape nothing mints any more.
    """
    return re.compile(rf"^web-{re.escape(user_id)}-[0-9a-f]{{12}}$")


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

    @pytest.mark.parametrize("user_id", ["alice", "alice-smith"])
    def test_generated_canonical_has_the_shape_a_real_web_token_has(
        self, conn, user_id,
    ):
        real = db.create_web_chat_room(conn, user_id, "Ideas").token
        built = promoted_room(conn, user_id).canonical
        pattern = web_token_re(user_id)
        assert pattern.match(real), real
        assert pattern.match(built), built

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

    def test_a_talk_ref_naming_another_room_is_refused(self, conn):
        # A web room binds `web` only, so the ref is free by the check above —
        # but binding `talk -> <another room's canonical token>` would make an
        # inbound naming that token resolve into this room. That is ISSUE-400
        # planted in the fixture rather than exposed by it.
        other = db.create_web_chat_room(conn, "alice", "Ideas")
        assert db.resolve_room_token(conn, "talk", other.token) is None
        with pytest.raises(ValueError, match="another room's canonical token"):
            promoted_room(conn, "alice", talk_ref=other.token)


# Written by a producer and by no builder, because a builder builds a room and
# not a turn: `record_inbound` also stores the user's message and creates the
# task that answers it.
NOT_THE_ROOM_MODEL = frozenset({"messages", "tasks"})

# Nothing chooses these, so two runs differ by construction. `applied_at` is
# `_migration_state`'s, stamped by `db.init_db` on each database in turn — the
# two calls land a second apart often enough to matter.
VOLATILE_COLUMNS = frozenset({"id", "created_at", "updated_at", "applied_at"})


def _room_model(conn) -> dict[str, list[tuple]]:
    """Every non-empty table in the database, minus the two above.

    Enumerated from `sqlite_master` rather than from a list of the room tables.
    A list would make the pin's guarantee smaller than it reads: a producer that
    grew a write into a table nobody had thought of would leave it green while
    the builders diverged, which is the failure this whole file exists to catch.
    """
    out: dict[str, list[tuple]] = {}
    tables = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    for table in tables:
        if table in NOT_THE_ROOM_MODEL:
            continue
        columns = [
            r[1] for r in conn.execute(f"PRAGMA table_info({table})")
            if r[1] not in VOLATILE_COLUMNS
        ]
        if not columns:
            continue
        # Quoted, because at least one column in this schema is a reserved
        # word (`sent_emails.references`).
        selected = ", ".join(f'"{c}"' for c in columns)
        rows = conn.execute(
            f'SELECT {selected} FROM "{table}" ORDER BY {selected}'
        ).fetchall()
        if rows:
            out[table] = [tuple(r) for r in rows]
    return out


class TestPinnedAgainstTheProducers:
    """Run the real producer and the builder against two fresh databases and
    diff every table. A producer that grows a write turns this red.

    Both are parametrized over a name needing normalization, because the two
    producers disagree about whether to normalize one and a fixed already-clean
    name makes the pin hold for a reason unrelated to the builders agreeing.
    """

    @pytest.fixture
    def two_dbs(self, tmp_path):
        produced, built = tmp_path / "produced.db", tmp_path / "built.db"
        db.init_db(produced)
        db.init_db(built)
        return produced, built

    @pytest.mark.parametrize("name", ["#istota", "  #istota  "])
    def test_plain_talk_room_matches_record_inbound(self, two_dbs, name):
        produced, built = two_dbs
        config = Config()
        config.db_path = produced
        with db.get_db(produced) as conn:
            token, task_id = record_inbound(
                conn, config, surface="talk", surface_ref="cpzpcfx2",
                user_id="alice", text="hi", channel_name=name,
            )
        assert token == "cpzpcfx2" and task_id is not None  # the room branch ran

        with db.get_db(built) as conn:
            plain_talk_room(conn, "alice", token="cpzpcfx2", name=name)

        with db.get_db(produced) as a, db.get_db(built) as b:
            assert _room_model(b) == _room_model(a)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["Ideas", "  Ideas  ", ""])
    async def test_promoted_room_matches_create_plus_promote(self, two_dbs, name):
        from istota import web_app

        produced, built = two_dbs
        config = Config()
        config.db_path = produced
        config.nextcloud = NextcloudConfig(
            url="https://nc.example", username="bot", app_password="pw",
        )
        with db.get_db(produced) as conn:
            handle = db.create_web_chat_room(conn, "alice", name)

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
                canonical=handle.token, talk_ref="promoted-tok", name=name,
            )
        assert room.diverges

        with db.get_db(produced) as a, db.get_db(built) as b:
            assert _room_model(b) == _room_model(a)
