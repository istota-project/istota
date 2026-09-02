"""The Talk double's negative controls, which are the point of this file.

A double that cannot refuse is worse than no double: it reports coverage that
does not exist, which is the failure `.claude/rules/testbed.md` records four
instances of. So the tests here are mostly about what `FakeTalkClient` *rejects*
— a canonical room token, a dead ref, a string naming nothing — and about the
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

import pytest

from istota import db, talk
from istota.config import NextcloudConfig, TalkConfig
from istota.scheduler import edit_talk_message
from istota.transport import talk as talk_pkg
from istota.transport.talk import inbound as talk_inbound

from .support.rooms import plain_talk_room, promoted_room
from .support.talk_double import (
    FakeTalkClient,
    TalkCall,
    UnknownTalkRoom,
    talk_refs_in,
)


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

    async def test_a_dead_binding_is_accepted(self, fake_talk, db_path):
        """ISSUE-401's shape, and explicitly out of scope.

        A binding whose Nextcloud conversation has been deleted is
        indistinguishable from a live one at the database level, so the double
        accepts it. Nothing here covers 401, and this test exists so that is
        not mistaken.
        """
        with db.get_db(db_path) as conn:
            room = plain_talk_room(conn, "alice")
            db.set_room_archived(conn, room.canonical, True)
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

    def test_both_modules_still_import_the_name_at_module_level(self):
        """If either stops, the fixture is patching a name nothing reads and
        every control above becomes vacuous."""
        assert talk_pkg.get_talk_client is not None
        assert talk_inbound.get_talk_client is not None


class TestPinnedAgainstTheSeams:
    """A method a seam calls and the double lacks is an `AttributeError` raised
    inside the same `except Exception` that swallows a 404 — a false pass, of
    exactly the kind this whole spec is about. So the list is walked, not
    written down."""

    @staticmethod
    def _methods_called_on_the_client(module) -> set[str]:
        source = Path(module.__file__).read_text()
        return set(re.findall(r"\bclient\.([a-z_][a-z_0-9]*)\(", source))

    def test_every_method_the_two_seams_call_exists_on_the_double(self):
        called = (
            self._methods_called_on_the_client(talk_pkg)
            | self._methods_called_on_the_client(talk_inbound)
        )
        assert called, "the regex found nothing; it has stopped matching"
        missing = {m for m in called if not hasattr(FakeTalkClient, m)}
        assert missing == set()

    @pytest.mark.parametrize("name", sorted(
        m for m, value in vars(FakeTalkClient).items()
        if not m.startswith("_") and callable(value)
        and callable(getattr(talk.TalkClient, m, None))
    ))
    def test_each_shadowed_signature_matches_the_real_client(self, name):
        """A parameter renamed or reordered on `TalkClient` would leave the
        double accepting calls the real client rejects. Names and kinds only —
        the double's defaults and annotations are its own business."""
        def shape(fn):
            return [
                (p.name, p.kind)
                for p in inspect.signature(fn).parameters.values()
            ]

        assert shape(getattr(FakeTalkClient, name)) == shape(
            getattr(talk.TalkClient, name)
        )
