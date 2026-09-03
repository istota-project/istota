"""The Talk double's negative controls, which are the point of this file.

A double that cannot refuse is worse than no double: it reports coverage that
does not exist, which is the failure `.claude/rules/testbed.md` collects
instances of — this double's own permissive predecessor being one of them. So
the tests here are mostly about what `FakeTalkClient` *rejects* — a canonical
room token, a string naming nothing, an unseeded attachment path — and about the
two properties everything built on it depends on:

- **the swallowing control**, that a refusal is still observable after the
  product's `except Exception` has eaten it, and
- **the seam control**, that a call made through `transport/talk/inbound.py`'s
  own `get_talk_client` binding reaches the double at all.

Verified by forcing `FakeTalkClient.__init__`'s `strict` default to False and
confirming the refusal cases go red; recorded in the stage log.
"""

import inspect
import re
from pathlib import Path

import httpx
import pytest

from istota import async_runtime, db, talk
from istota.config import NextcloudConfig, TalkConfig
from istota.scheduler import edit_talk_message
from istota.transport import talk as talk_pkg
from istota.transport.talk import inbound as talk_inbound

from .support.rooms import plain_talk_room, promoted_room
from .support.talk_double import (
    BrokenTalkDouble,
    FakeTalkClient,
    TalkCall,
    TalkConstruction,
    UnknownTalkAttachment,
    UnknownTalkRoom,
    talk_bot_client,
    talk_client_factory,
    talk_refs_in,
)


def _module_name_for(path: Path) -> str:
    """`src/istota/transport/talk/inbound.py` -> `istota.transport.talk.inbound`."""
    root = Path(async_runtime.__file__).parent.parent
    parts = path.relative_to(root).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


@pytest.fixture
def rooms(db_path):
    """One room of each shape, in the database the `fake_talk` fixture reads."""
    with db.get_db(db_path) as conn:
        return {
            "plain": plain_talk_room(conn, "alice"),
            "promoted": promoted_room(conn, "alice"),
        }


@pytest.fixture
def talk_config(make_config, db_path):
    return make_config(
        db_path=db_path,
        nextcloud=NextcloudConfig(
            url="https://nc.example.com", username="istota", app_password="s",
        ),
        talk=TalkConfig(enabled=True, bot_username="istota"),
    )


class TestTheRule:
    async def test_a_plain_rooms_token_is_accepted(self, fake_talk, rooms):
        room = rooms["plain"]
        await fake_talk.send_message(room.talk_ref, "hello")
        assert fake_talk.refusals == []

    async def test_a_promoted_rooms_talk_ref_is_accepted(self, fake_talk, rooms):
        room = rooms["promoted"]
        await fake_talk.send_message(room.talk_ref, "hello")
        assert fake_talk.refusals == []

    async def test_a_promoted_rooms_canonical_token_is_refused(
        self, fake_talk, rooms,
    ):
        """ISSUE-400 itself: the canonical token 404s at Nextcloud.

        It *is* in `room_bindings` — as the `web` binding's surface_ref — so
        this is also the check that the lookup is scoped to the `talk` surface
        rather than asking whether the string appears in the table at all.
        """
        room = rooms["promoted"]
        assert room.diverges
        with pytest.raises(UnknownTalkRoom):
            await fake_talk.send_message(room.canonical, "hello")

    async def test_a_refusal_is_recorded_in_calls(self, fake_talk, rooms):
        room = rooms["promoted"]
        with pytest.raises(UnknownTalkRoom):
            await fake_talk.send_message(room.canonical, "hello")
        assert fake_talk.calls == [
            TalkCall(
                "send_message", room.canonical,
                {"message": "hello", "reply_to": None, "reference_id": None},
                refused=True,
            )
        ]

    async def test_an_unregistered_token_is_refused(self, fake_talk, rooms):
        with pytest.raises(UnknownTalkRoom):
            await fake_talk.send_message("nosuchroom", "hello")

    async def test_an_unregistered_token_is_accepted_when_not_strict(
        self, db_path, rooms,
    ):
        client = FakeTalkClient(db_path, strict=False)
        await client.send_message("nosuchroom", "hello")
        assert client.refusals == []

    async def test_an_unregistered_token_is_accepted_via_known_channels(
        self, db_path, rooms,
    ):
        """The operator-configured channel case, expressed as data.

        `alerts_channel`, `log_channel`, the first briefing token,
        `default_destination`, an auto-detected DM and a provisioned room the
        poller has not seen are all raw Talk tokens nothing writes a binding
        for. They are ordinary product behaviour, so they must not have to go
        through `strict=False`.
        """
        client = FakeTalkClient(db_path, known_channels=["alertsroom"])
        await client.send_message("alertsroom", "hello")
        assert client.refusals == []
        with pytest.raises(UnknownTalkRoom):
            await client.send_message("someotherroom", "hello")

    async def test_known_channels_can_be_extended_mid_scenario(
        self, fake_talk, rooms,
    ):
        with pytest.raises(UnknownTalkRoom):
            await fake_talk.send_message("logsroom", "hello")
        fake_talk.known_channels.add("logsroom")
        await fake_talk.send_message("logsroom", "hello")
        assert len(fake_talk.refusals) == 1

    async def test_a_binding_added_after_construction_is_honoured(
        self, fake_talk, db_path,
    ):
        """The lookup is per call, so a room promoted mid-test works."""
        with db.get_db(db_path) as conn:
            room = promoted_room(conn, "alice", talk_ref="LaterRoom")
        # The client was constructed before the room existed.
        await fake_talk.send_message(room.talk_ref, "hello")
        assert fake_talk.refusals == []

    async def test_the_double_cannot_see_a_deleted_conversation(
        self, fake_talk, db_path,
    ):
        """ISSUE-401's shape, and explicitly out of scope.

        A binding whose Nextcloud conversation has been deleted looks exactly
        like a live one in `room_bindings` — there is no column for it — so the
        double accepts it and nothing here covers 401. This test exists so that
        is not mistaken for coverage.

        It archives the room to make a second, separate claim: `rooms.archived`
        is *our* state, not Nextcloud's, and the double deliberately does not
        consult it. The Talk conversation outlives our archive flag, so
        refusing here would be the double being stricter than the thing it
        stands in for.
        """
        with db.get_db(db_path) as conn:
            room = plain_talk_room(conn, "alice")
            db.set_room_archived(conn, room.canonical, True)
            assert db.get_room(conn, room.canonical).archived
        await fake_talk.send_message(room.talk_ref, "hello")
        assert fake_talk.refusals == []

    @pytest.mark.parametrize("token", ["", None, 12345, object()])
    async def test_junk_is_refused_rather_than_crashing(self, fake_talk, token):
        with pytest.raises(UnknownTalkRoom):
            await fake_talk.send_message(token, "hello")

    async def test_the_rule_applies_to_every_token_taking_method(
        self, fake_talk, rooms,
    ):
        """Not just `send_message` — a misroute on any of them is the same bug."""
        bad = rooms["promoted"].canonical
        for call in (
            fake_talk.send_message(bad, "x"),
            fake_talk.edit_message(bad, 1, "x"),
            fake_talk.get_conversation_info(bad),
            fake_talk.poll_messages(bad),
            fake_talk.fetch_chat_history(bad),
            fake_talk.get_latest_message_id(bad),
            fake_talk.get_participants(bad),
        ):
            with pytest.raises(UnknownTalkRoom):
                await call
        assert {c.method for c in fake_talk.refusals} == {
            "send_message", "edit_message", "get_conversation_info",
            "poll_messages", "fetch_chat_history", "get_latest_message_id",
            "get_participants",
        }


class TestTheErrorMessage:
    async def test_it_names_the_token_the_live_refs_and_the_known_channels(
        self, db_path, rooms,
    ):
        client = FakeTalkClient(db_path, known_channels=["alertsroom"])
        with pytest.raises(UnknownTalkRoom) as excinfo:
            await client.send_message(rooms["promoted"].canonical, "hello")
        message = str(excinfo.value)
        assert rooms["promoted"].canonical in message
        assert "alertsroom" in message
        refs = talk_refs_in(db_path)
        assert refs  # otherwise the next assertion is vacuous
        for ref in refs:
            assert ref in message
        assert excinfo.value.token == rooms["promoted"].canonical
        assert excinfo.value.method == "send_message"


class TestReturnShapes:
    async def test_send_message_returns_the_ocs_id_shape(self, fake_talk, rooms):
        response = await fake_talk.send_message(rooms["plain"].talk_ref, "hi")
        assert isinstance(response["ocs"]["data"]["id"], int)

    async def test_message_ids_increment(self, fake_talk, rooms):
        token = rooms["plain"].talk_ref
        first = await fake_talk.send_message(token, "one")
        second = await fake_talk.send_message(token, "two")
        assert (
            second["ocs"]["data"]["id"] > first["ocs"]["data"]["id"]
        )

    async def test_a_refused_send_mints_no_id(self, fake_talk, rooms):
        """A 404 posts nothing, so it must not burn an id either — otherwise a
        test asserting on a returned id can pass while a post was refused."""
        before = await fake_talk.send_message(rooms["plain"].talk_ref, "one")
        with pytest.raises(UnknownTalkRoom):
            await fake_talk.send_message(rooms["promoted"].canonical, "two")
        after = await fake_talk.send_message(rooms["plain"].talk_ref, "three")
        assert after["ocs"]["data"]["id"] == before["ocs"]["data"]["id"] + 1

    async def test_sent_ids_records_what_came_back(self, fake_talk, rooms):
        token = rooms["plain"].talk_ref
        first = await fake_talk.send_message(token, "one")
        second = await fake_talk.send_message(token, "two")
        assert fake_talk.sent_ids == [
            first["ocs"]["data"]["id"], second["ocs"]["data"]["id"],
        ]

    async def test_a_refused_send_records_no_id_either(self, fake_talk, rooms):
        """The reason `sent_id_for` walks `calls` instead of indexing.

        `sent_ids` is one entry per *accepted* send, so it is shorter than the
        send calls whenever one was refused — an index taken from `calls` would
        silently name the wrong message.
        """
        await fake_talk.send_message(rooms["plain"].talk_ref, "one")
        with pytest.raises(UnknownTalkRoom):
            await fake_talk.send_message(rooms["promoted"].canonical, "two")
        assert len(fake_talk.sent_ids) == 1
        assert len([c for c in fake_talk.calls if c.method == "send_message"]) == 2

    async def test_sent_id_for_names_the_post_by_its_reference_id(
        self, fake_talk, rooms,
    ):
        token = rooms["plain"].talk_ref
        await fake_talk.send_message(token, "ack", reference_id="istota:task:7:ack")
        result = await fake_talk.send_message(
            token, "answer", reference_id="istota:task:7:result",
        )
        assert fake_talk.sent_id_for("istota:task:7:result") == (
            result["ocs"]["data"]["id"]
        )
        assert fake_talk.sent_id_for("istota:task:7:ack") != (
            result["ocs"]["data"]["id"]
        )
        assert fake_talk.sent_id_for("istota:task:7:prompt") is None

    async def test_sent_id_for_takes_the_last_part_of_a_split_message(
        self, fake_talk, rooms,
    ):
        """`TalkTransport.deliver` gives every part of a split message the same
        `reference_id` and returns the *last* part's id, which is the one the
        caller stores. Matching the first would agree on every short message
        and disagree on exactly the long ones."""
        token = rooms["plain"].talk_ref
        first = await fake_talk.send_message(
            token, "Part 1", reference_id="istota:task:7:result",
        )
        last = await fake_talk.send_message(
            token, "Part 2", reference_id="istota:task:7:result",
        )
        assert first["ocs"]["data"]["id"] != last["ocs"]["data"]["id"]
        assert fake_talk.sent_id_for("istota:task:7:result") == (
            last["ocs"]["data"]["id"]
        )

    async def test_sent_id_for_refuses_a_falsy_reference_id(
        self, fake_talk, rooms,
    ):
        """An unlabelled send records `reference_id: None`, so a `None`
        argument would otherwise match it and return a real id."""
        await fake_talk.send_message(rooms["plain"].talk_ref, "unlabelled")
        assert fake_talk.sent_id_for(None) is None
        assert fake_talk.sent_id_for("") is None

    async def test_sent_id_for_skips_a_refused_send(self, fake_talk, rooms):
        """A refused send is not "the post did not happen for my reason"."""
        with pytest.raises(UnknownTalkRoom):
            await fake_talk.send_message(
                rooms["promoted"].canonical, "answer",
                reference_id="istota:task:7:result",
            )
        after = await fake_talk.send_message(
            rooms["plain"].talk_ref, "later", reference_id="istota:task:8:result",
        )
        assert fake_talk.sent_id_for("istota:task:7:result") is None
        assert fake_talk.sent_id_for("istota:task:8:result") == (
            after["ocs"]["data"]["id"]
        )

    async def test_get_conversation_info_returns_the_rooms_display_name(
        self, fake_talk, rooms,
    ):
        info = await fake_talk.get_conversation_info(rooms["plain"].talk_ref)
        assert info["displayName"] == rooms["plain"].name

    async def test_a_known_channel_falls_back_to_its_token(self, db_path, rooms):
        client = FakeTalkClient(db_path, known_channels=["alertsroom"])
        info = await client.get_conversation_info("alertsroom")
        assert info["displayName"] == "alertsroom"

    async def test_display_names_can_be_seeded(self, fake_talk, rooms):
        fake_talk.display_names[rooms["plain"].talk_ref] = "Standup"
        info = await fake_talk.get_conversation_info(rooms["plain"].talk_ref)
        assert info["displayName"] == "Standup"


class TestTheSwallowingControl:
    """The property every converted delivery test depends on.

    `scheduler.edit_talk_message` catches everything and returns False, so
    `UnknownTalkRoom` never propagates out of it — exactly as a real 404 would
    not. A converted test asserting only "nothing raised" therefore proves
    nothing; it has to read `calls` or the return value.
    """

    async def test_the_refusal_is_invisible_as_an_exception(
        self, fake_talk, rooms, talk_config, make_task,
    ):
        room = rooms["promoted"]
        task = make_task(conversation_token=room.canonical, source_type="talk")
        # No pytest.raises: this is the whole point.
        ok = await edit_talk_message(
            talk_config, task, 42, "edited", target_token=room.canonical,
        )
        assert ok is False

    async def test_but_it_is_visible_in_calls(
        self, fake_talk, rooms, talk_config, make_task,
    ):
        room = rooms["promoted"]
        task = make_task(conversation_token=room.canonical, source_type="talk")
        await edit_talk_message(
            talk_config, task, 42, "edited", target_token=room.canonical,
        )
        assert [c.refused for c in fake_talk.calls_to(room.canonical)] == [True]

    async def test_the_same_edit_against_the_talk_ref_succeeds(
        self, fake_talk, rooms, talk_config, make_task,
    ):
        """The positive half. Without it the test above passes on a double that
        refuses everything, which would be a different broken instrument."""
        room = rooms["promoted"]
        task = make_task(conversation_token=room.canonical, source_type="talk")
        ok = await edit_talk_message(
            talk_config, task, 42, "edited", target_token=room.talk_ref,
        )
        assert ok is True
        assert fake_talk.refusals == []


class TestTheDeliverPath:
    """`TalkTransport.deliver` is the path most of the eleven files to be
    converted will drive, and the second of the three swallowing handlers.

    Also the only test that exercises the OCS envelope through the code that
    unwraps it. Asserting the literal `{"ocs": {"data": {"id": n}}}` in
    `TestReturnShapes` cannot catch a wrong envelope: `deliver` would return
    None, its documented failure value, with every test here green.
    """

    async def test_it_returns_the_minted_id_for_the_talk_ref(
        self, fake_talk, rooms, talk_config,
    ):
        msg_id = await talk_pkg.TalkTransport(talk_config).deliver(
            rooms["promoted"].talk_ref, "the answer",
        )
        assert isinstance(msg_id, int)
        assert fake_talk.refusals == []

    async def test_it_returns_none_for_the_canonical_token(
        self, fake_talk, rooms, talk_config,
    ):
        """ISSUE-400 through the real delivery path. `deliver` catches and
        returns None, so the refusal is only visible in `calls`."""
        room = rooms["promoted"]
        msg_id = await talk_pkg.TalkTransport(talk_config).deliver(
            room.canonical, "the answer",
        )
        assert msg_id is None
        assert [c.refused for c in fake_talk.calls_to(room.canonical)] == [True]

    async def test_resolve_channel_name_reads_the_display_name(
        self, fake_talk, rooms, talk_config,
    ):
        """The `get_conversation_info` consumer, through its own seam."""
        name = await talk_pkg.TalkTransport(talk_config).resolve_channel_name(
            rooms["plain"].talk_ref,
        )
        assert name == rooms["plain"].name


class TestABrokenDoubleIsNotARefusal:
    """A database with no schema must abort the test, not look like a 404.

    `db_path` is the only fixture that runs `db.init_db`, so the fixture's own
    documented `fake_talk.db_path = ...` escape hatch lands here easily. A
    `sqlite3.OperationalError` escaping as-is is caught by
    `TalkTransport.deliver` and reported as a Talk failure, with no call
    recorded — the one failure mode the "assert on `calls`" doctrine cannot
    see.
    """

    async def test_an_uninitialised_database_raises_past_the_product(
        self, fake_talk, tmp_path, talk_config, rooms,
    ):
        fake_talk.db_path = tmp_path / "never-initialised.db"
        with pytest.raises(BrokenTalkDouble):
            await talk_pkg.TalkTransport(talk_config).deliver("anything", "hi")

    async def test_and_the_attempt_is_still_recorded(
        self, fake_talk, tmp_path, rooms,
    ):
        fake_talk.db_path = tmp_path / "never-initialised.db"
        with pytest.raises(BrokenTalkDouble):
            await fake_talk.send_message("anything", "hi")
        assert [c.refused for c in fake_talk.calls] == [True]

    def test_it_is_not_catchable_as_an_exception(self):
        """The property that makes it work: every product handler is
        `except Exception`, so this must not be one."""
        assert not issubclass(BrokenTalkDouble, Exception)
        assert issubclass(BrokenTalkDouble, BaseException)


class TestAttachments:
    async def test_an_unseeded_path_is_refused(self, fake_talk, talk_config, tmp_path):
        """The real client GETs and calls `raise_for_status`, so a path naming
        nothing must fail rather than leave a zero-byte file a test can assert
        exists."""
        local = tmp_path / "out.bin"
        with pytest.raises(UnknownTalkAttachment):
            await talk_pkg.TalkTransport(talk_config).download_attachment(
                "Talk/nothing.png", str(local),
            )
        assert not local.exists()
        assert [c.refused for c in fake_talk.calls] == [True]

    async def test_a_seeded_path_is_written(self, fake_talk, talk_config, tmp_path):
        fake_talk.attachments["Talk/photo.png"] = b"\x89PNG"
        local = tmp_path / "nested" / "out.png"
        await talk_pkg.TalkTransport(talk_config).download_attachment(
            "Talk/photo.png", str(local),
        )
        assert local.read_bytes() == b"\x89PNG"
        assert fake_talk.refusals == []

    async def test_an_empty_body_is_asked_for_explicitly(self, fake_talk, tmp_path):
        fake_talk.attachments["Talk/empty.txt"] = b""
        local = tmp_path / "empty.txt"
        await fake_talk.download_attachment("Talk/empty.txt", str(local))
        assert local.read_bytes() == b""


class TestPollMessages:
    """`last_known_message_id` is the difference between a poller that makes
    progress and one that re-ingests the same turns for ever."""

    async def test_no_id_returns_the_seeded_history(self, fake_talk, rooms):
        token = rooms["plain"].talk_ref
        fake_talk.messages[token] = [{"id": 1}, {"id": 2}, {"id": 3}]
        assert await fake_talk.poll_messages(token) == [
            {"id": 1}, {"id": 2}, {"id": 3},
        ]

    async def test_an_id_returns_only_what_is_newer(self, fake_talk, rooms):
        token = rooms["plain"].talk_ref
        fake_talk.messages[token] = [{"id": 1}, {"id": 2}, {"id": 3}]
        assert await fake_talk.poll_messages(token, last_known_message_id=2) == [
            {"id": 3},
        ]

    async def test_nothing_newer_is_the_real_clients_304(self, fake_talk, rooms):
        token = rooms["plain"].talk_ref
        fake_talk.messages[token] = [{"id": 1}, {"id": 2}]
        assert await fake_talk.poll_messages(token, last_known_message_id=2) == []


class TestTheConstructionSitePatch:
    """`talk_client_factory`, which is what reaches `web_app`.

    Asserted directly here rather than only through the web paths, because a
    factory returning a fresh instance per construction would leave every
    web-side assertion looking at an empty `calls` — green, and proving nothing.
    """

    def test_every_construction_returns_the_one_instance(self, fake_talk):
        factory = talk_client_factory(fake_talk)
        first = factory(None, bearer_token="a", timeout=5)
        second = factory(None, bearer_token="b", timeout=5)
        assert first is fake_talk and second is fake_talk

    def test_it_records_what_the_product_asked_for(self, fake_talk):
        factory = talk_client_factory(fake_talk)
        factory(None)
        factory(None, bearer_token="live-at", timeout=5)
        assert fake_talk.constructions == [
            TalkConstruction(bearer_token=None, timeout=None),
            TalkConstruction(bearer_token="live-at", timeout=5),
        ]

    async def test_a_call_carries_the_current_construction_s_bearer(
        self, fake_talk, rooms,
    ):
        factory = talk_client_factory(fake_talk)
        factory(None, bearer_token="stale-at", timeout=5)
        await fake_talk.send_message(rooms["plain"].talk_ref, "one")
        factory(None, bearer_token="fresh-at", timeout=5)
        await fake_talk.send_message(rooms["plain"].talk_ref, "two")
        assert [c.bearer_token for c in fake_talk.calls] == [
            "stale-at", "fresh-at",
        ]

    async def test_the_bot_client_carries_no_bearer(self, fake_talk, rooms):
        """`_delete_from_talk` tries the user, then the bot, on one instance.

        Without the reset in `talk_bot_client` the bot inherits the credential
        the user attempt just failed with, and the fallback the product has
        cannot be told from a second failure of the same kind.
        """
        talk_client_factory(fake_talk)(None, bearer_token="user-at", timeout=5)
        bot = talk_bot_client(fake_talk)(None)
        await bot.send_message(rooms["plain"].talk_ref, "as the bot")
        assert fake_talk.calls[-1].bearer_token is None

    async def test_aclose_is_counted_not_honoured(self, fake_talk, rooms):
        await fake_talk.aclose()
        await fake_talk.send_message(rooms["plain"].talk_ref, "still open")
        assert fake_talk.closes == 1
        assert fake_talk.refusals == []


class TestABearerTokenTheServerRejects:
    """The 401 both retrying callers exist for, expressible as data.

    The trap this closes: a double whose only unhappy answer is
    `UnknownTalkRoom` makes a stale credential and a misroute the same event, so
    `_post_as_user`'s retry can neither be driven nor distinguished — and a test
    written against such a double would report the retry as covered.
    """

    async def test_it_raises_the_status_the_product_branches_on(
        self, fake_talk, rooms,
    ):
        fake_talk.bearer_rejections["stale-at"] = 401
        talk_client_factory(fake_talk)(None, bearer_token="stale-at", timeout=5)
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await fake_talk.send_message(rooms["plain"].talk_ref, "hi")
        assert excinfo.value.response.status_code == 401

    async def test_it_is_recorded_as_a_status_and_not_as_a_refusal(
        self, fake_talk, rooms,
    ):
        """`refusals == []` means "nothing was misrouted" in thirty-odd files."""
        fake_talk.bearer_rejections["stale-at"] = 401
        talk_client_factory(fake_talk)(None, bearer_token="stale-at", timeout=5)
        with pytest.raises(httpx.HTTPStatusError):
            await fake_talk.send_message(rooms["plain"].talk_ref, "hi")
        assert fake_talk.refusals == []
        assert [(c.method, c.status) for c in fake_talk.auth_failures] == [
            ("send_message", 401),
        ]

    async def test_the_credential_answers_before_the_room_does(
        self, fake_talk, rooms,
    ):
        """Nextcloud authenticates before it routes, so a rejected token
        answers 401 even for a token naming nothing — and the misroute only
        surfaces on the retry, once the credential is good."""
        fake_talk.bearer_rejections["stale-at"] = 401
        factory = talk_client_factory(fake_talk)
        factory(None, bearer_token="stale-at", timeout=5)
        with pytest.raises(httpx.HTTPStatusError):
            await fake_talk.send_message(rooms["promoted"].canonical, "hi")
        factory(None, bearer_token="fresh-at", timeout=5)
        with pytest.raises(UnknownTalkRoom):
            await fake_talk.send_message(rooms["promoted"].canonical, "hi")
        assert [(c.status, c.refused) for c in fake_talk.calls] == [
            (401, False), (None, True),
        ]

    async def test_a_fresh_token_is_not_rejected(self, fake_talk, rooms):
        fake_talk.bearer_rejections["stale-at"] = 401
        talk_client_factory(fake_talk)(None, bearer_token="fresh-at", timeout=5)
        resp = await fake_talk.send_message(rooms["plain"].talk_ref, "hi")
        assert resp["ocs"]["data"]["id"] == fake_talk.sent_ids[-1]

    async def test_the_bot_is_never_rejected_by_a_bearer_rule(
        self, fake_talk, rooms,
    ):
        """`bearer_token` is None in basic-auth mode, and `None` must not match
        a `bearer_rejections` entry by accident."""
        fake_talk.bearer_rejections[""] = 401
        await fake_talk.send_message(rooms["plain"].talk_ref, "hi")
        assert fake_talk.auth_failures == []


class TestTheWebSeamMethods:
    """The rule applies to the methods only `web_app` calls, too."""

    @pytest.mark.parametrize("method,args", [
        ("add_participant", ("alice",)),
        ("rename_conversation", ("new name",)),
        ("delete_conversation", ()),
        ("delete_message", (12,)),
    ])
    async def test_a_canonical_token_is_refused(
        self, fake_talk, rooms, method, args,
    ):
        with pytest.raises(UnknownTalkRoom):
            await getattr(fake_talk, method)(rooms["promoted"].canonical, *args)
        assert [c.refused for c in fake_talk.calls] == [True]

    @pytest.mark.parametrize("method,args", [
        ("add_participant", ("alice",)),
        ("rename_conversation", ("new name",)),
        ("delete_conversation", ()),
        ("delete_message", (12,)),
    ])
    async def test_the_talk_ref_is_accepted(self, fake_talk, rooms, method, args):
        await getattr(fake_talk, method)(rooms["promoted"].talk_ref, *args)
        assert fake_talk.refusals == []

    async def test_create_conversation_mints_a_token_bound_to_nothing(
        self, fake_talk, db_path,
    ):
        room = await fake_talk.create_conversation("a new room")
        assert room["token"] == fake_talk.created_tokens[-1]
        assert room["token"] not in talk_refs_in(db_path)
        # Which is exactly why the next call against it is refused until the
        # product writes the binding — the promote path's real assertion.
        with pytest.raises(UnknownTalkRoom):
            await fake_talk.add_participant(room["token"], "alice")

    async def test_two_creates_do_not_collide(self, fake_talk):
        first = await fake_talk.create_conversation("one")
        second = await fake_talk.create_conversation("two")
        assert first["token"] != second["token"]

    async def test_mark_read_swallows_by_default(self, fake_talk, rooms):
        """The real client's own contract, and the swallowing control for it."""
        assert await fake_talk.mark_conversation_read(
            rooms["promoted"].canonical,
        ) is False
        assert [c.refused for c in fake_talk.calls] == [True]

    async def test_mark_read_raises_when_asked_to(self, fake_talk, rooms):
        with pytest.raises(UnknownTalkRoom):
            await fake_talk.mark_conversation_read(
                rooms["promoted"].canonical, raise_on_error=True,
            )

    async def test_mark_read_returns_true_for_a_bound_ref(self, fake_talk, rooms):
        assert await fake_talk.mark_conversation_read(
            rooms["promoted"].talk_ref,
        ) is True

    async def test_a_401_reaches_a_caller_that_asked_for_it(
        self, fake_talk, rooms,
    ):
        """`_mark_read_as_user` passes `raise_on_error=True` precisely so it can
        see the 401 and force a refresh. Swallowing it here would take that
        away without failing anything."""
        fake_talk.bearer_rejections["stale-at"] = 401
        talk_client_factory(fake_talk)(None, bearer_token="stale-at", timeout=5)
        with pytest.raises(httpx.HTTPStatusError):
            await fake_talk.mark_conversation_read(
                rooms["plain"].talk_ref, raise_on_error=True,
            )
        assert await fake_talk.mark_conversation_read(
            rooms["plain"].talk_ref,
        ) is False


class TestTheSeamControl:
    """`get_talk_client` is imported at module level in two places.

    A fixture patching only `istota.transport.talk.get_talk_client` leaves the
    poller and `_post_ack` talking to the real factory. These fail in that case.
    """

    async def test_a_call_through_the_inbound_binding_reaches_the_double(
        self, fake_talk, rooms, talk_config,
    ):
        await talk_inbound._post_ack(talk_config, rooms["plain"].talk_ref, "ok")
        assert [c.method for c in fake_talk.calls] == ["send_message"]

    async def test_the_inbound_binding_refuses_a_canonical_token(
        self, fake_talk, rooms, talk_config,
    ):
        """`_post_ack` swallows too, so the evidence is again in `calls`."""
        await talk_inbound._post_ack(talk_config, rooms["promoted"].canonical, "ok")
        assert [c.refused for c in fake_talk.calls] == [True]

    def test_the_fixture_patches_every_module_level_importer(self):
        """The set is walked, not asserted `is not None`.

        A third module importing `get_talk_client` at module level would leave
        `fake_talk` patching a proper subset, and a test built on it would
        reach the real singleton — which constructs a real client against the
        configured Nextcloud URL and then has its failure swallowed, so the
        escape reads as a refusal *and* attempts a socket. Identity against
        `async_runtime` is checked too, since a name rebound to something else
        would satisfy a `is not None`.
        """
        patched = {"istota.transport.talk", "istota.transport.talk.inbound"}
        assert self._importers(r"^from") == patched
        assert talk_pkg.get_talk_client is async_runtime.get_talk_client
        assert talk_inbound.get_talk_client is async_runtime.get_talk_client

    def test_the_function_local_importers_are_the_two_written_down(self):
        """These are covered by the third patch, on `async_runtime` itself.

        A function-local import resolves the name at call time, so patching the
        definition site reaches every one of them — which is why this pins the
        set rather than the patching. A third one appearing is covered
        automatically; it is listed here so somebody confirms that rather than
        assuming it.
        """
        # `[ \t]`, not `\s`: with `re.MULTILINE` a `\s+` after `^` matches the
        # preceding newline, so every module-level import reads as indented and
        # the two sets collapse into one.
        assert self._importers(r"^[ \t]+from") == {
            "istota.web_app", "istota.commands",
        }

    def test_the_definition_site_is_patched(self, fake_talk, talk_config):
        """Which is what actually reaches those two.

        Pinned directly rather than by driving either caller, because neither
        would prove it. `commands`' one calls `search_messages`, which is on no
        seam and would have to be added to the double for one test; and
        `web_app._delete_from_talk`'s bot fallback is reached by the
        *construction* patch anyway — `get_talk_client` builds its singleton
        with a lazy `from .talk import TalkClient` — so under `fake_talk_web` it
        stays green with this patch removed. Measured, not assumed.
        """
        assert async_runtime.get_talk_client(talk_config) is fake_talk

    @staticmethod
    def _importers(anchor: str) -> set[str]:
        found = set()
        for path in Path(async_runtime.__file__).parent.rglob("*.py"):
            source = path.read_text()
            if re.search(anchor + r" [.\w]*async_runtime import .*get_talk_client",
                         source, re.MULTILINE):
                found.add(_module_name_for(path))
        return found


# The methods the two patched seams call on their client, written down so a
# *shrink* is red as well as a growth. The walk below catches a method added to
# a seam; this catches one that moved out of reach of the walk's regex — which
# only sees a receiver literally named `client`, so `talk = get_talk_client(...)`
# or `get_talk_client(cfg).mark_conversation_read(...)` would be invisible and
# leave a non-empty `called` behind.
SEAM_METHODS = {
    "download_attachment",
    "edit_message",
    "fetch_chat_history",
    "get_conversation_info",
    "get_latest_message_id",
    "get_participants",
    "list_conversations",
    "poll_messages",
    "send_message",
}

# The same, for `web_app`'s own `TalkClient(...)` constructions. A separate set
# because the two seams are patched separately and a method can belong to one
# and not the other — `aclose` is called by every web site and by neither
# `get_talk_client` seam, since those hand back a singleton nobody closes.
WEB_SEAM_METHODS = {
    "aclose",
    "add_participant",
    "create_conversation",
    "delete_conversation",
    "delete_message",
    "get_conversation_info",
    "list_conversations",
    "mark_conversation_read",
    "rename_conversation",
    "send_message",
}

# `web_app` has one other receiver literally named `client`: the
# `httpx.AsyncClient` in `_fetch_userinfo`. Subtracted by name rather than by
# narrowing the regex, so a *second* method appearing on it fails the walk and
# somebody looks, instead of being silently absorbed.
NOT_A_TALK_CLIENT = {"get"}

# Public on the double and on no real client — helpers for the tests, not part
# of the shadowed surface.
DOUBLE_ONLY = {"auth_failures", "calls_to", "refusals", "sent_id_for"}


class TestPinnedAgainstTheSeams:
    """A method a seam calls and the double lacks is an `AttributeError` raised
    inside the same `except Exception` that swallows a 404 — a false pass, of
    exactly the kind this whole spec is about. So the list is walked *and*
    written down: the walk catches a growth, the literal catches a shrink, and
    neither on its own catches both."""

    @staticmethod
    def _methods_called_on_the_client(module) -> set[str]:
        source = Path(module.__file__).read_text()
        return set(re.findall(r"\bclient\.([a-z_][a-z_0-9]*)\(", source))

    def test_the_walked_set_is_the_written_down_set(self):
        called = (
            self._methods_called_on_the_client(talk_pkg)
            | self._methods_called_on_the_client(talk_inbound)
        )
        assert called == SEAM_METHODS

    def test_the_walked_web_set_is_the_written_down_set(self):
        """`web_app`'s receivers are `client` and `user_client`.

        `_talk_conversation_verdict` takes its client as a parameter also named
        `client`, so the same walk covers it; `_delete_from_talk`'s inner
        `_attempt` does too, which is what puts `delete_message` in the set for
        both the user and the bot credential.
        """
        # Read rather than imported: `istota.web_app` needs fastapi and authlib,
        # which are an optional extra, and this pin has to hold in the default
        # suite whether or not they are installed.
        source = (Path(async_runtime.__file__).parent / "web_app.py").read_text()
        called = set(re.findall(
            r"\b(?:user_)?client\.([a-z_][a-z_0-9]*)\(", source,
        )) - NOT_A_TALK_CLIENT
        assert called == WEB_SEAM_METHODS

    def test_every_method_a_seam_calls_exists_on_the_double(self):
        missing = {
            m for m in SEAM_METHODS | WEB_SEAM_METHODS
            if not hasattr(FakeTalkClient, m)
        }
        assert missing == set()

    def test_every_shadowing_method_still_exists_on_the_real_client(self):
        """The direction the parametrization below cannot see.

        Filtering the parameter list on `hasattr(TalkClient, name)` means a
        method *renamed away* on the real client drops a case rather than
        failing one — 9 params become 8, all green, while the double keeps a
        method shadowing nothing.
        """
        shadowing = {
            m for m, value in vars(FakeTalkClient).items()
            if not m.startswith("_") and callable(value)
        } - DOUBLE_ONLY
        assert shadowing == SEAM_METHODS | WEB_SEAM_METHODS
        orphaned = {m for m in shadowing if not hasattr(talk.TalkClient, m)}
        assert orphaned == set()

    @pytest.mark.parametrize("name", sorted(SEAM_METHODS | WEB_SEAM_METHODS))
    def test_each_shadowed_signature_matches_the_real_client(self, name):
        """A parameter renamed or reordered on `TalkClient` would leave the
        double accepting calls the real client rejects. Names and kinds only —
        the double's defaults and annotations are its own business.

        Parametrized over the written-down set rather than over an intersection,
        so a method vanishing from either side is a failure and not a silently
        smaller run.
        """
        def shape(fn):
            return [
                (p.name, p.kind)
                for p in inspect.signature(fn).parameters.values()
            ]

        assert shape(getattr(FakeTalkClient, name)) == shape(
            getattr(talk.TalkClient, name)
        )
