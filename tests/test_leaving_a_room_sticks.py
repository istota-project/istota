"""ISSUE-408 — leaving a synced room has to stay left.

Two independent mechanisms undid it, either one on its own:

1. `provision-rooms` runs on every deploy, finds the remembered `general`
   bot-only because its one human walked out, reads that as "an invite this
   tool left behind" and re-invites them.
2. Deleting a *promoted* room in web chat (web-origin, later bound to Talk)
   hard-deleted every row keyed on the token, the dismissal tombstone among
   them, while sending nothing to Talk — so the next poll found a live
   conversation with no registry row and registered it from scratch.

The first is settled by recording whether the invite landed: only a token whose
last recorded invite *failed* may be retried. The second by treating a room
bound to Talk the way a Talk-origin room is already treated — hidden per user,
never destroyed.
"""

import pytest

from istota import db
from istota.config import Config


# ---------------------------------------------------------------------------
# 1. Provisioning must not drag a user back into a room they left
# ---------------------------------------------------------------------------


def _client(rooms=None, participants=None):
    from unittest.mock import AsyncMock, MagicMock

    client = MagicMock()
    client.list_conversations = AsyncMock(return_value=list(rooms or []))
    client.get_participants = AsyncMock(
        side_effect=lambda token: list((participants or {}).get(token, []))
    )
    client.create_conversation = AsyncMock(return_value={"token": "newtok"})
    client.add_participant = AsyncMock(return_value={})
    return client


def _left_room():
    """The reported state: the remembered `general`, its only human gone."""
    return _client(
        rooms=[{"token": "G1", "displayName": "#general", "type": 2}],
        participants={"G1": [{"actorType": "users", "actorId": "bot"}]},
    )


class TestProvisioningRespectsALeave:
    @pytest.mark.asyncio
    async def test_a_room_whose_invite_landed_is_not_re_invited(self):
        from istota.provision_rooms import ProvisionedRecord, ensure_room

        client = _left_room()
        result = await ensure_room(
            client, "general", "alice", bot_user_id="bot",
            known=ProvisionedRecord(token="G1", invite_failed=False),
        )

        client.add_participant.assert_not_awaited()
        client.create_conversation.assert_not_awaited()
        assert (result.token, result.reinvited, result.invited) == ("G1", False, False)
        assert result.absent is True
        # Leaving is not a deploy failure, so the play must not fail on it.
        assert result.invite_failed is False

    @pytest.mark.asyncio
    async def test_a_room_whose_invite_failed_is_still_retried(self):
        # The ISSUE-342 self-heal, which the fix must not cost: a room this
        # tool created and could not add the user to is bot-only for a reason
        # that has nothing to do with anybody leaving.
        from istota.provision_rooms import ProvisionedRecord, ensure_room

        client = _left_room()
        result = await ensure_room(
            client, "general", "alice", bot_user_id="bot",
            known=ProvisionedRecord(token="G1", invite_failed=True),
        )

        client.add_participant.assert_awaited_once_with("G1", "alice")
        assert (result.token, result.reinvited, result.invited) == ("G1", True, True)

    @pytest.mark.asyncio
    async def test_a_record_with_no_recorded_outcome_does_not_re_invite(self):
        # Every record written before this change carries no outcome. Reading
        # the absence as "may have failed" would re-invite the reporter on
        # their very next deploy, which is the bug.
        from istota.provision_rooms import ProvisionedRecord, ensure_room

        client = _left_room()
        result = await ensure_room(
            client, "general", "alice", bot_user_id="bot",
            known=ProvisionedRecord(token="G1"),
        )

        client.add_participant.assert_not_awaited()
        assert result.reinvited is False

    @pytest.mark.asyncio
    async def test_a_failed_retry_is_recorded_as_still_failed(self):
        from istota.provision_rooms import ProvisionedRecord, ensure_room

        client = _left_room()
        client.add_participant = _client().add_participant
        client.add_participant.side_effect = RuntimeError("nope")
        result = await ensure_room(
            client, "general", "alice", bot_user_id="bot",
            known=ProvisionedRecord(token="G1", invite_failed=True),
        )

        assert (result.invited, result.reinvited) == (False, True)
        assert result.invite_failed is True

    @pytest.mark.asyncio
    async def test_a_landed_retry_clears_the_failure(self):
        from istota.provision_rooms import ProvisionedRecord, ensure_room

        client = _left_room()
        result = await ensure_room(
            client, "general", "alice", bot_user_id="bot",
            known=ProvisionedRecord(token="G1", invite_failed=True),
        )
        # `invite_failed` alone cannot tell a landed retry from a refused one:
        # both leave it False, one because the invite worked and one because
        # nothing was attempted. The awaited call and `reinvited` are what
        # separate them.
        client.add_participant.assert_awaited_once_with("G1", "alice")
        assert (result.invited, result.reinvited) == (True, True)
        assert result.invite_failed is False

    @pytest.mark.asyncio
    async def test_a_created_room_whose_invite_failed_reports_it(self):
        client = _client(rooms=[])
        client.add_participant.side_effect = RuntimeError("nope")
        from istota.provision_rooms import ensure_room

        result = await ensure_room(client, "general", "alice", bot_user_id="bot")
        assert (result.created, result.invited, result.invite_failed) == (
            True, False, True,
        )

    @pytest.mark.asyncio
    async def test_a_room_the_user_is_in_reports_no_failure(self):
        from istota.provision_rooms import ProvisionedRecord, ensure_room

        client = _client(
            rooms=[{"token": "G1", "displayName": "#general", "type": 2}],
            participants={"G1": [{"actorType": "users", "actorId": "alice"}]},
        )
        result = await ensure_room(
            client, "general", "alice", bot_user_id="bot",
            known=ProvisionedRecord(token="G1", invite_failed=True),
        )
        assert result.invite_failed is False


class TestARunThatObservedNothingRecordsNothing:
    """The record answers "is an invite still outstanding", which is not the
    same question as "did this run fail" — and a run that attempted nothing and
    saw nothing must not answer it. Writing this run's silence as "no failure"
    erases a recorded one, and the retry it authorizes never fires again."""

    @pytest.mark.asyncio
    async def test_a_failed_participant_read_does_not_erase_a_recorded_failure(self):
        # One transient Talk error on the participant read was enough: the
        # empty list is treated as a failed read, nothing is attempted, and the
        # run used to record `invite_failed=False` over the True that was there.
        from istota.provision_rooms import ProvisionedRecord, ensure_room

        client = _client(
            rooms=[{"token": "G1", "displayName": "#general", "type": 2}],
            participants={"G1": []},
        )
        result = await ensure_room(
            client, "general", "alice", bot_user_id="bot",
            known=ProvisionedRecord(token="G1", invite_failed=True),
        )

        client.add_participant.assert_not_awaited()
        # This run did not fail — it did not try — so the deploy must not fail.
        assert result.invite_failed is False
        # But the record must still say the invite is outstanding.
        assert result.record_invite_failed is True

    @pytest.mark.asyncio
    async def test_a_left_shared_room_keeps_a_recorded_failure(self):
        from istota.provision_rooms import ProvisionedRecord, ensure_room

        client = _client(
            rooms=[{"token": "G1", "displayName": "#general", "type": 2}],
            participants={"G1": [
                {"actorType": "users", "actorId": "bot"},
                {"actorType": "users", "actorId": "carol"},
            ]},
        )
        result = await ensure_room(
            client, "general", "alice", bot_user_id="bot",
            known=ProvisionedRecord(token="G1", invite_failed=True),
        )

        client.add_participant.assert_not_awaited()
        assert result.record_invite_failed is True

    @pytest.mark.asyncio
    async def test_seeing_the_user_in_the_room_clears_a_recorded_failure(self):
        # The one arm that settles it without attempting anything: they are in
        # the room, so whatever an earlier run recorded, nothing is outstanding.
        from istota.provision_rooms import ProvisionedRecord, ensure_room

        client = _client(
            rooms=[{"token": "G1", "displayName": "#general", "type": 2}],
            participants={"G1": [{"actorType": "users", "actorId": "alice"}]},
        )
        result = await ensure_room(
            client, "general", "alice", bot_user_id="bot",
            known=ProvisionedRecord(token="G1", invite_failed=True),
        )

        assert result.record_invite_failed is False

    @pytest.mark.asyncio
    async def test_a_left_room_with_no_recorded_failure_stays_that_way(self):
        from istota.provision_rooms import ProvisionedRecord, ensure_room

        result = await ensure_room(
            _left_room(), "general", "alice", bot_user_id="bot",
            known=ProvisionedRecord(token="G1"),
        )
        assert result.record_invite_failed is False

    def test_the_record_persists_the_outstanding_answer_not_this_runs(
        self, tmp_path,
    ):
        """Through the writer, since that is where the two could diverge."""
        from istota.provision_rooms import (
            ProvisionedRoom,
            read_provisioned_records,
            record_provisioned_rooms,
        )

        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        record_provisioned_rooms(db_path, "alice", [
            ProvisionedRoom(
                name="general", token="G1", created=False, invited=False,
                absent=True, carried_invite_failed=True,
            ),
        ])

        assert read_provisioned_records(db_path, "alice")["general"].invite_failed is True


class TestTheDeployReportsALeftRoom:
    """The visibility half. Declining to re-invite is right, but `existing` is
    what a room the user is happily in prints — and a room stranded by an
    invite that failed before outcomes were recorded lands on the same branch.
    The Ansible role fails the play on `invite FAILED` and computes `changed`
    from `STATE:`, so neither may move for a room somebody merely left."""

    def test_a_left_room_is_named_without_failing_the_deploy(
        self, tmp_path, monkeypatch, capsys,
    ):
        from istota import cli, provision_rooms as pr

        cfg = tmp_path / "config.toml"
        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        cfg.write_text(
            "db_path = '%s'\n\n[nextcloud]\nurl = 'https://nc.example'\n"
            "username = 'bot'\napp_password = 'pw'\n" % db_path
        )
        monkeypatch.setattr(
            pr, "provision_user_rooms",
            lambda config, user_id, names, **kw: [
                pr.ProvisionedRoom(
                    name="general", token="G1", created=False, invited=False,
                    absent=True,
                )
            ],
        )

        class _A:
            def __init__(self):
                self.config, self.user = str(cfg), "alice"
                self.room = ["general"]
                self.no_seed = self.reseed = self.json = False
                self.adopt = None

        cli.cmd_nextcloud_provision_rooms(_A())
        captured = capsys.readouterr()

        assert "user not a member" in captured.out
        assert "invite FAILED" not in captured.out
        assert "could not add" not in captured.err
        assert "STATE: noop" in captured.out


class TestTheProvisioningRecord:
    @pytest.fixture
    def db_path(self, tmp_path):
        path = tmp_path / "istota.db"
        db.init_db(path)
        return path

    def test_the_outcome_round_trips(self, db_path):
        from istota.provision_rooms import (
            ProvisionedRoom,
            read_provisioned_records,
            record_provisioned_rooms,
        )

        record_provisioned_rooms(db_path, "alice", [
            ProvisionedRoom(name="general", token="G1", created=True, invited=False),
            ProvisionedRoom(name="logs", token="L1", created=True, invited=True),
        ])
        got = read_provisioned_records(db_path, "alice")

        assert got["general"].token == "G1"
        assert got["general"].invite_failed is True
        assert got["logs"].invite_failed is False

    def test_a_legacy_json_string_value_reads_as_no_failure(self, db_path):
        import json

        from istota.provision_rooms import (
            PROVISIONED_NAMESPACE,
            read_provisioned_records,
        )

        with db.get_db(db_path) as conn:
            db.kv_set(
                conn, "alice", PROVISIONED_NAMESPACE, "general", json.dumps("G1"),
            )
        got = read_provisioned_records(db_path, "alice")

        assert got["general"].token == "G1"
        assert got["general"].invite_failed is False

    def test_a_legacy_bare_string_value_reads_as_no_failure(self, db_path):
        from istota.provision_rooms import (
            PROVISIONED_NAMESPACE,
            read_provisioned_records,
        )

        with db.get_db(db_path) as conn:
            db.kv_set(conn, "alice", PROVISIONED_NAMESPACE, "general", "G1")
        got = read_provisioned_records(db_path, "alice")

        assert got["general"].token == "G1"
        assert got["general"].invite_failed is False

    def test_an_unreadable_record_provisions_by_name(self, tmp_path):
        from istota.provision_rooms import read_provisioned_records

        assert read_provisioned_records(tmp_path / "nope.db", "alice") == {}

    @pytest.mark.asyncio
    async def test_a_deploy_after_a_leave_leaves_membership_alone(self, db_path):
        """End to end through the record: a run that put the user in the room,
        then the user leaves, then another deploy."""
        from istota.provision_rooms import (
            provision_rooms,
            read_provisioned_records,
            record_provisioned_rooms,
        )

        first = await provision_rooms(
            _client(rooms=[]), "alice", ("general",), bot_user_id="bot",
        )
        record_provisioned_rooms(db_path, "alice", first)

        # alice walks out; the room is bot-only under the token just recorded.
        client = _client(
            rooms=[{"token": "newtok", "displayName": "general", "type": 2}],
            participants={"newtok": [{"actorType": "users", "actorId": "bot"}]},
        )
        second = await provision_rooms(
            client, "alice", ("general",), bot_user_id="bot",
            known_records=read_provisioned_records(db_path, "alice"),
        )

        client.add_participant.assert_not_awaited()
        client.create_conversation.assert_not_awaited()
        assert second[0].token == "newtok"


# ---------------------------------------------------------------------------
# 2. Hiding a promoted room in web chat has to survive the Talk poll
# ---------------------------------------------------------------------------


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


def _promoted_room(conn, user_id: str = "alice", talk_ref: str = "talkref1") -> str:
    """A web-origin room later promoted to Talk — origin stays `web`."""
    room = db.create_web_chat_room(conn, user_id, "general")
    db.add_room_binding(conn, room.token, "talk", talk_ref)
    return room.token


class TestHidingAPromotedRoom:
    def test_delete_hides_rather_than_destroying(self, web_config, db_path):
        from istota import web_app

        with db.get_db(db_path) as conn:
            token = _promoted_room(conn)
        rid = next(
            r["id"] for r in web_app._chat_list_rooms("alice") if r["token"] == token
        )
        assert web_app._chat_delete_room("alice", rid) == "ok"

        with db.get_db(db_path) as conn:
            assert db.get_room(conn, token) is not None
            assert db.is_room_dismissed(conn, token, "alice")
            assert db.get_room_binding(conn, token, "talk") is not None
            # Membership dropped, but the transcript and the other participants
            # are untouched — the delete must not reach Talk's side of it.
            assert not db.is_room_member(conn, token, "alice")

        # `token` being absent from the listing proves nothing on its own: the
        # old hard-delete path satisfies it too, by destroying the room. What
        # distinguishes them is that the room still exists (above) and that the
        # listing's replacement is a *different*, newly-minted default room —
        # `ensure_default_web_chat_room` invents one because the user's registry
        # is now empty — rather than the hidden token coming back.
        listed = web_app._chat_list_rooms("alice")
        assert token not in {r["token"] for r in listed}
        assert [r["token"] for r in listed] != []
        with db.get_db(db_path) as conn:
            for room in listed:
                assert db.get_room_binding(conn, room["token"], "talk") is None

    def test_the_tombstone_survives_a_poll_re_registration(self, web_config, db_path):
        """The mechanism that brought the room back: the poll registers a Talk
        conversation it finds no registry row for. With the room and its
        tombstone still there, there is nothing to re-register."""
        from istota import web_app

        with db.get_db(db_path) as conn:
            token = _promoted_room(conn)
        rid = next(
            r["id"] for r in web_app._chat_list_rooms("alice") if r["token"] == token
        )
        web_app._chat_delete_room("alice", rid)

        with db.get_db(db_path) as conn:
            # The binding is intact, so the poll resolves the conversation to
            # this canonical token rather than minting a Talk-origin room.
            assert db.resolve_room_token(conn, "talk", "talkref1") == token
            # And even the membership re-seed cannot resurface it.
            db.add_room_member(conn, token, "alice")
        assert token not in {r["token"] for r in web_app._chat_list_rooms("alice")}

    def test_archiving_a_promoted_room_writes_a_tombstone(self, web_config, db_path):
        from istota import web_app

        with db.get_db(db_path) as conn:
            token = _promoted_room(conn)
        rid = next(
            r["id"] for r in web_app._chat_list_rooms("alice") if r["token"] == token
        )
        web_app._chat_update_room("alice", rid, name=None, archived=True)

        with db.get_db(db_path) as conn:
            assert db.is_room_dismissed(conn, token, "alice")
            # Never the global flag — the conversation is shared.
            assert db.get_room(conn, token).archived is False

    def test_unarchiving_a_promoted_room_clears_the_tombstone(
        self, web_config, db_path
    ):
        from istota import web_app

        with db.get_db(db_path) as conn:
            token = _promoted_room(conn)
        rid = next(
            r["id"] for r in web_app._chat_list_rooms("alice") if r["token"] == token
        )
        web_app._chat_update_room("alice", rid, name=None, archived=True)
        with db.get_db(db_path) as conn:
            assert db.is_room_dismissed(conn, token, "alice")

        web_app._chat_update_room("alice", rid, name=None, archived=False)
        with db.get_db(db_path) as conn:
            assert not db.is_room_dismissed(conn, token, "alice")
            assert db.is_room_member(conn, token, "alice")
        assert token in {r["token"] for r in web_app._chat_list_rooms("alice")}

    def test_unarchiving_clears_a_global_flag_set_before_the_fix(
        self, web_config, db_path
    ):
        """A promoted room archived by the old code path took the other arm and
        set `rooms.archived`. The tombstone arm never clears that, and
        `list_member_rooms` subtracts it too — so without this the room stays
        hidden with no control left that could bring it back."""
        from istota import web_app

        with db.get_db(db_path) as conn:
            token = _promoted_room(conn)
        rid = next(
            r["id"] for r in web_app._chat_list_rooms("alice") if r["token"] == token
        )
        with db.get_db(db_path) as conn:
            db.set_room_archived(conn, token, True)  # the pre-fix state
        assert token not in {r["token"] for r in web_app._chat_list_rooms("alice")}

        web_app._chat_update_room("alice", rid, name=None, archived=False)

        with db.get_db(db_path) as conn:
            assert db.get_room(conn, token).archived is False
        assert token in {r["token"] for r in web_app._chat_list_rooms("alice")}

    def test_unarchiving_a_talk_origin_room_leaves_the_global_flag_alone(
        self, web_config, db_path
    ):
        """On a Talk-origin room that flag is `archive_orphaned_talk_rooms`
        saying the bot left the conversation — a fact about the deployment, not
        this user's hide, and not theirs to clear."""
        from istota import web_app

        with db.get_db(db_path) as conn:
            db.register_room(conn, "r77", "alice", origin="talk", name="#team")
            db.add_room_binding(conn, "r77", "talk", "r77")
        rid = next(
            r["id"] for r in web_app._chat_list_rooms("alice") if r["token"] == "r77"
        )
        with db.get_db(db_path) as conn:
            db.set_room_archived(conn, "r77", True)

        web_app._chat_update_room("alice", rid, name=None, archived=False)

        with db.get_db(db_path) as conn:
            assert db.get_room(conn, "r77").archived is True

    def test_a_room_with_a_dead_talk_binding_is_hidden_not_deleted(
        self, web_config, db_path
    ):
        """Pinning the accepted consequence. A binding row outlives the
        conversation it names (ISSUE-401), and this predicate reads the row
        rather than probing Nextcloud — so such a room can no longer be
        hard-deleted from web. Reconnecting it is the promote button's job."""
        from istota import web_app

        with db.get_db(db_path) as conn:
            token = _promoted_room(conn, talk_ref="deadref")
        rid = next(
            r["id"] for r in web_app._chat_list_rooms("alice") if r["token"] == token
        )
        assert web_app._chat_delete_room("alice", rid) == "ok"

        with db.get_db(db_path) as conn:
            assert db.get_room(conn, token) is not None
            assert db.is_room_dismissed(conn, token, "alice")

    def test_an_unpromoted_web_room_is_still_hard_deleted(self, web_config, db_path):
        # The widened branch must not swallow the plain case: a web room with
        # no Talk conversation behind it has nothing to preserve.
        from istota import web_app

        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "scratch")
            token = room.token
        rid = next(
            r["id"] for r in web_app._chat_list_rooms("alice") if r["token"] == token
        )
        assert web_app._chat_delete_room("alice", rid) == "ok"

        with db.get_db(db_path) as conn:
            assert db.get_room(conn, token) is None
