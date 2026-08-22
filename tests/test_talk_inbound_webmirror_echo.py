"""Tests for echo prevention of post-as-user mirrored Talk messages (Stage 3
of the user-scoped Nextcloud OAuth spec): the referenceId fast-path in the
poller and the external-ids ledger backstop in record_inbound."""

import pytest
from unittest.mock import AsyncMock, patch

from istota import db
from istota.config import Config, NextcloudConfig, SchedulerConfig, TalkConfig, UserConfig
from istota.transport.talk import inbound as _talk_poller_mod
from istota.transport.talk.inbound import poll_talk_conversations
from istota.transport._types import WEBMIRROR_REF_PREFIX


@pytest.fixture(autouse=True)
def _reset_poller_caches():
    _talk_poller_mod._participant_cache.clear()
    _talk_poller_mod._conversation_cache = None
    _talk_poller_mod._dm_token_cache.clear()
    yield
    _talk_poller_mod._participant_cache.clear()
    _talk_poller_mod._conversation_cache = None
    _talk_poller_mod._dm_token_cache.clear()


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def config(db_path, tmp_path):
    config = Config()
    config.db_path = db_path
    config.temp_dir = tmp_path / "temp"
    config.temp_dir.mkdir(exist_ok=True)
    config.talk = TalkConfig(enabled=True, bot_username="istota")
    config.nextcloud = NextcloudConfig(
        url="https://nc.test", username="istota", app_password="pass",
    )
    config.users = {"alice": UserConfig()}
    config.scheduler = SchedulerConfig()
    return config


def _msg(id=100, actor_id="alice", message="hello", reference_id=None,
         actor_type="users", message_type="comment"):
    msg = {
        "id": id,
        "actorId": actor_id,
        "actorType": actor_type,
        "message": message,
        "messageType": message_type,
        "messageParameters": {},
        "timestamp": 1700000000,
    }
    if reference_id is not None:
        msg["referenceId"] = reference_id
    return msg


async def _poll(config, messages, token="dmroom"):
    conversations = [{"token": token, "type": 1, "name": "alice"}]
    with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
        instance = MockClient.return_value
        instance.list_conversations = AsyncMock(return_value=conversations)
        instance.poll_messages = AsyncMock(return_value=messages)
        instance.send_message = AsyncMock()
        return await poll_talk_conversations(config)


class TestReferenceIdFastPath:
    @pytest.mark.asyncio
    async def test_webmirror_message_skipped(self, config):
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "dmroom", 50)

        created = await _poll(config, [
            _msg(id=100, reference_id=f"{WEBMIRROR_REF_PREFIX}42"),
        ])

        assert created == []
        with db.get_db(config.db_path) as conn:
            # No task created…
            assert db.list_tasks(conn) == []
            # …no canonical message row…
            n = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
            assert n == 0
            # …but the poll cursor advanced past the echo…
            assert db.get_talk_poll_state(conn, "dmroom") == 100
            # …and the context cache still holds the turn (it's legitimately
            # part of the conversation).
            assert db.has_cached_talk_messages(conn, "dmroom")

    @pytest.mark.asyncio
    async def test_other_reference_ids_not_skipped(self, config):
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "dmroom", 50)

        created = await _poll(config, [
            _msg(id=101, reference_id="istota:task:7:result"),
        ])

        # A non-webmirror referenceId is a normal message → task created.
        assert len(created) == 1

    @pytest.mark.asyncio
    async def test_plain_message_still_creates_task(self, config):
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "dmroom", 50)

        created = await _poll(config, [_msg(id=102)])

        assert len(created) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, created[0])
            assert task.prompt == "hello"


class TestExternalIdLedgerBackstop:
    @pytest.mark.asyncio
    async def test_stamped_message_dropped_without_reference_id(self, config):
        """The same mirrored message with its referenceId stripped (federation
        edge) is caught by record_inbound's external-ids echo check."""
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "dmroom", 50)
            db.register_room(conn, "dmroom", "alice", origin="talk")
            db.add_room_binding(conn, "dmroom", "talk", "dmroom")
            # The canonical user row the web POST stored, stamped with the
            # Talk id the mirror post received.
            db.add_message(
                conn, "dmroom", role="user", body="hello",
                origin_surface="web", task_id=999,
                external_ids={"talk": "100"},
            )

        created = await _poll(config, [_msg(id=100)])  # no referenceId

        assert created == []
        with db.get_db(config.db_path) as conn:
            assert db.list_tasks(conn) == []
            # No duplicate user row landed.
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE room_token='dmroom'"
            ).fetchone()["n"]
            assert n == 1

    @pytest.mark.asyncio
    async def test_inbound_talk_id_stamped_on_canonical_row(self, config):
        """record_inbound stamps the Talk message id on the stored user turn
        (feeds the Talk→web read-sync cursor cap)."""
        import json
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "dmroom", 50)

        created = await _poll(config, [_msg(id=123)])

        assert len(created) == 1
        with db.get_db(config.db_path) as conn:
            row = conn.execute(
                "SELECT external_ids FROM messages WHERE role='user'"
            ).fetchone()
            assert json.loads(row["external_ids"]) == {"talk": "123"}


class TestTimedOutMirrorReconciledFromEcho:
    """ISSUE-287: `_post_as_user` posts the turn, Nextcloud stores it, and the
    caller's 5 s timeout fires before the response lands — so nothing stamps
    the canonical row and the scheduler's legacy attributed repost puts the
    question in the room a second time. The echo is Nextcloud's own
    confirmation that the message exists, so the poller stamps from it."""

    def _seed(self, config, *, role="user", origin_surface="web",
              room_token="dmroom", external_ids=None, author="alice"):
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "dmroom", 50)
            db.register_room(conn, "dmroom", "alice", origin="talk")
            db.add_room_binding(conn, "dmroom", "talk", "dmroom")
            db.add_message(
                conn, room_token, role=role, body="hello",
                origin_surface=origin_surface, task_id=999,
                author_user_id=author, external_ids=external_ids,
            )
            return conn.execute(
                "SELECT id FROM messages ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]

    @pytest.mark.asyncio
    async def test_unstamped_user_turn_is_stamped_from_the_echo(self, config):
        msg_id = self._seed(config)

        created = await _poll(config, [
            _msg(id=600123, reference_id=f"{WEBMIRROR_REF_PREFIX}{msg_id}"),
        ])

        assert created == []
        with db.get_db(config.db_path) as conn:
            assert db.get_message_external_id(conn, msg_id, "talk") == "600123"
            # …which is exactly the read the scheduler's repost suppression
            # makes, so the attributed repost no longer fires.
            assert db.user_turn_has_external_id(conn, 999, "talk") is True

    @pytest.mark.asyncio
    async def test_an_existing_stamp_is_not_overwritten(self, config):
        msg_id = self._seed(config, external_ids={"talk": "100"})

        await _poll(config, [
            _msg(id=999999, reference_id=f"{WEBMIRROR_REF_PREFIX}{msg_id}"),
        ])

        with db.get_db(config.db_path) as conn:
            assert db.get_message_external_id(conn, msg_id, "talk") == "100"

    @pytest.mark.asyncio
    async def test_a_reference_naming_another_room_stamps_nothing(self, config):
        """`referenceId` is caller-supplied and any Talk participant can set
        it, so the row id in it is untrusted: the stamp is scoped to the room
        the echo actually arrived in."""
        self._seed(config)
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "other", "alice", origin="talk")
            db.add_message(
                conn, "other", role="user", body="elsewhere",
                origin_surface="web", task_id=777,
            )
            victim = conn.execute(
                "SELECT id FROM messages ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]

        await _poll(config, [
            _msg(id=500, reference_id=f"{WEBMIRROR_REF_PREFIX}{victim}"),
        ])

        with db.get_db(config.db_path) as conn:
            assert db.get_message_external_id(conn, victim, "talk") is None

    @pytest.mark.asyncio
    async def test_an_assistant_row_is_not_stamped(self, config):
        msg_id = self._seed(config, role="assistant")

        await _poll(config, [
            _msg(id=501, reference_id=f"{WEBMIRROR_REF_PREFIX}{msg_id}"),
        ])

        with db.get_db(config.db_path) as conn:
            assert db.get_message_external_id(conn, msg_id, "talk") is None

    @pytest.mark.asyncio
    async def test_a_talk_origin_row_is_not_stamped(self, config):
        """Only a web-origin turn is ever mirrored as the user, so only a
        web-origin row is a candidate for the reconciliation."""
        msg_id = self._seed(config, origin_surface="talk")

        await _poll(config, [
            _msg(id=502, reference_id=f"{WEBMIRROR_REF_PREFIX}{msg_id}"),
        ])

        with db.get_db(config.db_path) as conn:
            assert db.get_message_external_id(conn, msg_id, "talk") is None

    @pytest.mark.asyncio
    async def test_a_reference_from_another_author_stamps_nothing(self, config):
        """The echo's author is the evidence. A room co-member spraying
        `istota:webmirror:<n>` over an id range would otherwise stamp every
        unstamped web turn in the room with Talk ids of their choosing — and
        the stamp *suppresses* the repost, so the victim's question would
        vanish from Talk entirely rather than merely doubling."""
        msg_id = self._seed(config, author="alice")
        config.users["mallory"] = UserConfig()

        await _poll(config, [
            _msg(id=505, actor_id="mallory",
                 reference_id=f"{WEBMIRROR_REF_PREFIX}{msg_id}"),
        ])

        with db.get_db(config.db_path) as conn:
            assert db.get_message_external_id(conn, msg_id, "talk") is None

    @pytest.mark.asyncio
    async def test_a_guest_echo_stamps_nothing(self, config):
        """The echo skip runs ahead of the actor-type and known-user filters,
        so an unconfigured actor reaches this code and must be refused here."""
        msg_id = self._seed(config)

        await _poll(config, [
            _msg(id=506, actor_id="alice",
                 reference_id=f"{WEBMIRROR_REF_PREFIX}{msg_id}",
                 actor_type="guests"),
        ])

        with db.get_db(config.db_path) as conn:
            assert db.get_message_external_id(conn, msg_id, "talk") is None

    @pytest.mark.asyncio
    async def test_a_deleted_echo_stamps_nothing(self, config):
        """A deleted mirror keeps its referenceId; stamping a dead Talk id
        would suppress the repost and drop the question from the room."""
        msg_id = self._seed(config)

        await _poll(config, [
            _msg(id=507, reference_id=f"{WEBMIRROR_REF_PREFIX}{msg_id}",
                 message_type="comment_deleted"),
        ])

        with db.get_db(config.db_path) as conn:
            assert db.get_message_external_id(conn, msg_id, "talk") is None

    @pytest.mark.asyncio
    async def test_a_malformed_reference_is_skipped_without_raising(self, config):
        """Every one of these is a reference any room participant can compose,
        and the poll batch is a single transaction whose cursor advance rolls
        back with it — so a raise here re-polls the same message forever and
        stops all Talk inbound. `str.isdigit()` is not a safe gate: `'²'`
        passes it and `int()` refuses it, `'١٢٣'` passes both and folds onto an
        ASCII row id the producer could never have written, and a long enough
        run of ASCII digits overflows SQLite's INTEGER on binding."""
        msg_id = self._seed(config)

        created = await _poll(config, [
            _msg(id=503, reference_id=f"{WEBMIRROR_REF_PREFIX}not-a-number"),
            _msg(id=504, reference_id=WEBMIRROR_REF_PREFIX),
            _msg(id=508, reference_id=f"{WEBMIRROR_REF_PREFIX}²"),
            _msg(id=509, reference_id=f"{WEBMIRROR_REF_PREFIX}{'9' * 40}"),
            _msg(id=510, reference_id=f"{WEBMIRROR_REF_PREFIX}١٢٣"),
        ])

        # Still echoes: skipped, no task, nothing stamped, and — the part that
        # matters — the batch committed, so the poll cursor advanced past them.
        assert created == []
        with db.get_db(config.db_path) as conn:
            assert db.get_talk_poll_state(conn, "dmroom") == 510
            assert db.get_message_external_id(conn, msg_id, "talk") is None

    @pytest.mark.asyncio
    async def test_a_poisoned_reference_does_not_strand_a_real_message(
        self, config,
    ):
        """The batch-rollback consequence, stated as its own test: a message
        behind a poisoned echo must still become a task, and the cursor must
        still advance past both."""
        self._seed(config)

        created = await _poll(config, [
            _msg(id=520, reference_id=f"{WEBMIRROR_REF_PREFIX}²"),
            _msg(id=521, message="a real question"),
        ])

        assert len(created) == 1
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, created[0]).prompt == "a real question"
            assert db.get_talk_poll_state(conn, "dmroom") == 521
