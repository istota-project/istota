"""ISSUE-401 — a room whose Talk conversation is deleted must be recoverable.

A `talk` binding used to be written once and never revisited: `add_room_binding`
is `INSERT OR IGNORE`, the promote guard refused outright on any existing
binding, and the only statement that removed a `room_bindings` row was the room
teardown. So a room whose Talk conversation was deleted in Nextcloud kept a
`surface_ref` naming nothing, every Talk delivery for it 404'd, and the only way
out was deleting the room and its whole web transcript.

The recovery path is the promote button doing double duty: with a binding
already present it probes the bound conversation, and replaces the ref only when
Nextcloud says that conversation is gone.

The bot's own 404 does not settle that, which is the subtlety these tests exist
for. Talk's room GET is participant-scoped, so it answers 404 both for a deleted
conversation and for one the bot was removed from — and replacing the ref in the
second case forks a live room, stranding its history and its other participants
behind a binding that no longer names it. So a 404 is settled by asking again as
the requesting user, who was made a participant when the room was promoted.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from istota import db
from istota.config import Config, NextcloudConfig


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


@pytest.fixture
def web_config(db_path):
    from istota import web_app
    cfg = Config()
    cfg.db_path = db_path
    cfg.nextcloud = NextcloudConfig(
        url="https://nc.example", username="bot", app_password="pw",
    )
    web_app._config = cfg
    return cfg


def _http_error(status: int) -> httpx.HTTPStatusError:
    """What `raise_for_status()` raises for `status` — the shape the liveness
    probe classifies on."""
    request = httpx.Request("GET", "https://nc.example/ocs/room/dead")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


def _talk_client(*, info_side_effect=None, info_return=None) -> MagicMock:
    """A mocked TalkClient. `get_conversation_info` is the liveness probe; a
    fresh conversation always comes back as `new-tok` so a replaced ref is
    distinguishable from the dead one it replaced."""
    fake = MagicMock()
    fake.create_conversation = AsyncMock(return_value={"token": "new-tok"})
    fake.add_participant = AsyncMock(return_value={})
    fake.send_message = AsyncMock(return_value={})
    fake.aclose = AsyncMock()
    fake.delete_conversation = AsyncMock(return_value=None)
    fake.get_conversation_info = AsyncMock(
        side_effect=info_side_effect,
        return_value=info_return if info_return is not None else {"token": "dead-tok"},
    )
    return fake


def _user_probe(verdict: str):
    """Stub the user-scoped second stage, so a test can say what the requesting
    user sees without standing up the OAuth token store."""
    from istota import web_app
    return patch.object(
        web_app, "_talk_conversation_seen_by_user",
        AsyncMock(return_value=verdict),
    )


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


class TestReplaceRoomBinding:
    """`add_room_binding` stays `INSERT OR IGNORE` for the three inbound
    writers; replacement is a separate, compare-and-set verb."""

    def test_replaces_the_ref_when_the_expected_one_matches(self, db_path):
        with db.get_db(db_path) as conn:
            db.add_room_binding(conn, "room1", "talk", "dead-tok")
            won = db.replace_room_binding(
                conn, "room1", "talk", "new-tok", expected_ref="dead-tok",
            )
            binding = db.get_room_binding(conn, "room1", "talk")
        assert won is True
        assert binding is not None and binding.surface_ref == "new-tok"

    def test_add_room_binding_still_ignores_a_second_write(self, db_path):
        # The contract the three inbound writers depend on, restated here so a
        # change to it fails in this file rather than somewhere far away.
        with db.get_db(db_path) as conn:
            db.add_room_binding(conn, "room1", "talk", "first")
            db.add_room_binding(conn, "room1", "talk", "second")
            binding = db.get_room_binding(conn, "room1", "talk")
        assert binding is not None and binding.surface_ref == "first"

    def test_refuses_when_the_ref_changed_under_it(self, db_path):
        # Two promotes racing: the second must not orphan the first's room.
        with db.get_db(db_path) as conn:
            db.add_room_binding(conn, "room1", "talk", "someone-elses-tok")
            won = db.replace_room_binding(
                conn, "room1", "talk", "new-tok", expected_ref="dead-tok",
            )
            binding = db.get_room_binding(conn, "room1", "talk")
        assert won is False
        assert binding is not None and binding.surface_ref == "someone-elses-tok"

    def test_inserts_when_no_binding_existed(self, db_path):
        with db.get_db(db_path) as conn:
            won = db.replace_room_binding(
                conn, "room1", "talk", "new-tok", expected_ref=None,
            )
            binding = db.get_room_binding(conn, "room1", "talk")
        assert won is True
        assert binding is not None and binding.surface_ref == "new-tok"

    def test_expecting_none_refuses_when_a_binding_appeared(self, db_path):
        # The other half of the race: the caller looked, saw nothing, and by the
        # time it wrote someone had bound the room.
        with db.get_db(db_path) as conn:
            db.add_room_binding(conn, "room1", "talk", "someone-elses-tok")
            won = db.replace_room_binding(
                conn, "room1", "talk", "new-tok", expected_ref=None,
            )
            binding = db.get_room_binding(conn, "room1", "talk")
        assert won is False
        assert binding is not None and binding.surface_ref == "someone-elses-tok"

    def test_leaves_other_surfaces_alone(self, db_path):
        with db.get_db(db_path) as conn:
            db.add_room_binding(conn, "room1", "web", "room1")
            db.add_room_binding(conn, "room1", "talk", "dead-tok")
            db.replace_room_binding(
                conn, "room1", "talk", "new-tok", expected_ref="dead-tok",
            )
            web = db.get_room_binding(conn, "room1", "web")
        assert web is not None and web.surface_ref == "room1"


class TestClearRoomExternalIds:
    """A rebind invalidates the room's recorded Talk message ids: they name a
    conversation that is gone, and Talk ids are per-conversation, so a stale one
    can name a *different* message in the replacement."""

    def _stamp(self, conn, room_token, body, ext):
        import json
        cur = conn.execute(
            "INSERT INTO messages (room_token, role, body, origin_surface, external_ids) "
            "VALUES (?, 'assistant', ?, 'talk', ?)",
            (room_token, body, json.dumps(ext)),
        )
        return cur.lastrowid

    def test_drops_the_named_surface_and_keeps_the_others(self, db_path):
        with db.get_db(db_path) as conn:
            mid = self._stamp(conn, "room1", "hi", {"talk": "42", "email": "m1"})
            changed = db.clear_room_external_ids(conn, "room1", "talk")
            assert db.get_message_external_id(conn, mid, "talk") is None
            assert db.get_message_external_id(conn, mid, "email") == "m1"
        assert changed == 1

    def test_leaves_other_rooms_alone(self, db_path):
        with db.get_db(db_path) as conn:
            mine = self._stamp(conn, "room1", "hi", {"talk": "42"})
            theirs = self._stamp(conn, "room2", "hi", {"talk": "42"})
            db.clear_room_external_ids(conn, "room1", "talk")
            assert db.get_message_external_id(conn, mine, "talk") is None
            assert db.get_message_external_id(conn, theirs, "talk") == "42"

    def test_a_row_with_nothing_left_stops_counting_as_synced(self, db_path):
        # `room_max_talk_synced_message_id` caps the Talk read-sync cursor, and
        # a row whose only id was the stale one must stop capping it there.
        with db.get_db(db_path) as conn:
            self._stamp(conn, "room1", "hi", {"talk": "42"})
            assert db.room_max_talk_synced_message_id(conn, "room1") > 0
            db.clear_room_external_ids(conn, "room1", "talk")
            assert db.room_max_talk_synced_message_id(conn, "room1") == 0

    def test_unparseable_json_is_skipped_rather_than_raising(self, db_path):
        with db.get_db(db_path) as conn:
            conn.execute(
                "INSERT INTO messages (room_token, role, body, origin_surface, external_ids) "
                "VALUES ('room1', 'assistant', 'hi', 'talk', 'not json')",
            )
            assert db.clear_room_external_ids(conn, "room1", "talk") == 0


class TestClearStaleTalkDeliveryToken:
    """`talk_channel_for_task`'s rung 0 returns this column before the binding
    is consulted, so a task carrying the dead ref outlives the repair."""

    def _task(self, conn, *, room, delivery, status="pending"):
        task_id = db.create_task(
            conn, "do a thing", "alice", conversation_token=room,
            talk_delivery_token=delivery,
        )
        conn.execute(
            "UPDATE tasks SET status = ? WHERE id = ?", (status, task_id),
        )
        return task_id

    def test_clears_an_in_flight_task_pointing_at_the_dead_ref(self, db_path):
        with db.get_db(db_path) as conn:
            tid = self._task(conn, room="room1", delivery="dead-tok")
            changed = db.clear_stale_talk_delivery_token(conn, "room1", "dead-tok")
            task = db.get_task(conn, tid)
        assert changed == 1
        assert task is not None and task.talk_delivery_token is None

    def test_leaves_a_finished_task_alone(self, db_path):
        # Its column is a record of where its answer went, not a routing
        # instruction with a future.
        with db.get_db(db_path) as conn:
            tid = self._task(
                conn, room="room1", delivery="dead-tok", status="completed",
            )
            db.clear_stale_talk_delivery_token(conn, "room1", "dead-tok")
            task = db.get_task(conn, tid)
        assert task is not None and task.talk_delivery_token == "dead-tok"

    def test_leaves_another_conversation_alone(self, db_path):
        with db.get_db(db_path) as conn:
            tid = self._task(conn, room="room1", delivery="other-tok")
            db.clear_stale_talk_delivery_token(conn, "room1", "dead-tok")
            task = db.get_task(conn, tid)
        assert task is not None and task.talk_delivery_token == "other-tok"

    def test_leaves_another_room_alone(self, db_path):
        with db.get_db(db_path) as conn:
            tid = self._task(conn, room="room2", delivery="dead-tok")
            db.clear_stale_talk_delivery_token(conn, "room1", "dead-tok")
            task = db.get_task(conn, tid)
        assert task is not None and task.talk_delivery_token == "dead-tok"


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


class TestRePromoteOverADeadBinding:
    @pytest.mark.asyncio
    async def test_a_deleted_conversation_is_replaced(self, web_config, db_path):
        """The reported bug. The bound conversation is gone, so re-promoting
        mints a new one and points the binding at it."""
        from istota import web_app
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "Ideas")
            db.add_room_binding(conn, room.token, "talk", "dead-tok")
        fake = _talk_client(info_side_effect=_http_error(404))
        with patch("istota.talk.TalkClient", return_value=fake), _user_probe("gone"):
            status, result = await web_app._chat_promote_to_talk("alice", room.id)
        assert status == "reconnected"
        assert result is not None and result["talk_token"] == "new-tok"
        fake.get_conversation_info.assert_awaited_once_with("dead-tok")
        fake.create_conversation.assert_awaited_once()
        with db.get_db(db_path) as conn:
            binding = db.get_room_binding(conn, room.token, "talk")
        assert binding is not None and binding.surface_ref == "new-tok"

    @pytest.mark.asyncio
    async def test_a_reconnect_drops_the_dead_conversations_message_ids(
        self, web_config, db_path,
    ):
        """Those ids named the conversation that just went. Left behind, a web
        reply's `replyTo` would carry one into the replacement, where the same
        number may belong to a different message."""
        import json
        from istota import web_app
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "Ideas")
            db.add_room_binding(conn, room.token, "talk", "dead-tok")
            cur = conn.execute(
                "INSERT INTO messages (room_token, role, body, origin_surface, external_ids) "
                "VALUES (?, 'assistant', 'earlier', 'talk', ?)",
                (room.token, json.dumps({"talk": "17"})),
            )
            mid = cur.lastrowid
        fake = _talk_client(info_side_effect=_http_error(404))
        with patch("istota.talk.TalkClient", return_value=fake), _user_probe("gone"):
            status, _ = await web_app._chat_promote_to_talk("alice", room.id)
        assert status == "reconnected"
        with db.get_db(db_path) as conn:
            assert db.get_message_external_id(conn, mid, "talk") is None

    @pytest.mark.asyncio
    async def test_a_live_conversation_is_left_alone(self, web_config, db_path):
        """The guard's original job, which must survive: no second Talk room
        for a room that is already properly bound."""
        from istota import web_app
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "Ideas")
            db.add_room_binding(conn, room.token, "talk", "live-tok")
        fake = _talk_client(info_return={"token": "live-tok"})
        with patch("istota.talk.TalkClient", return_value=fake):
            status, result = await web_app._chat_promote_to_talk("alice", room.id)
        assert status == "live"
        fake.create_conversation.assert_not_awaited()
        with db.get_db(db_path) as conn:
            binding = db.get_room_binding(conn, room.token, "talk")
        assert binding is not None and binding.surface_ref == "live-tok"

    @pytest.mark.asyncio
    async def test_an_unreachable_nextcloud_refuses_rather_than_replacing(
        self, web_config, db_path,
    ):
        """A probe that failed says nothing about the conversation. Refusing
        preserves a good binding; the alternative mints a Talk room on every
        transient blip."""
        from istota import web_app
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "Ideas")
            db.add_room_binding(conn, room.token, "talk", "live-tok")
        fake = _talk_client(info_side_effect=httpx.ConnectError("no route"))
        with patch("istota.talk.TalkClient", return_value=fake):
            status, result = await web_app._chat_promote_to_talk("alice", room.id)
        assert status == "unreachable"
        assert result is None
        fake.create_conversation.assert_not_awaited()
        with db.get_db(db_path) as conn:
            binding = db.get_room_binding(conn, room.token, "talk")
        assert binding is not None and binding.surface_ref == "live-tok"

    @pytest.mark.asyncio
    async def test_a_server_error_refuses_too(self, web_config, db_path):
        """Only 404 means gone. A 500 is Nextcloud having a bad day, and a 403
        is an auth fault that minting a new room would not fix."""
        from istota import web_app
        for status_code in (403, 500, 502):
            with db.get_db(db_path) as conn:
                room = db.create_web_chat_room(conn, "alice", f"Ideas{status_code}")
                db.add_room_binding(conn, room.token, "talk", "live-tok")
            fake = _talk_client(info_side_effect=_http_error(status_code))
            with patch("istota.talk.TalkClient", return_value=fake):
                status, _ = await web_app._chat_promote_to_talk("alice", room.id)
            assert status == "unreachable", f"HTTP {status_code} must not read as gone"
            fake.create_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_conversation_the_bot_was_removed_from_is_not_replaced(
        self, web_config, db_path,
    ):
        """The bot's 404 is ambiguous, and this is the reading that must not
        win. The conversation is alive with its history and its other people;
        minting a replacement would fork it and strand the original behind a
        binding that no longer names it. Adding the bot back is the repair."""
        from istota import web_app
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "Ideas")
            db.add_room_binding(conn, room.token, "talk", "live-tok")
        fake = _talk_client(info_side_effect=_http_error(404))
        with patch("istota.talk.TalkClient", return_value=fake), _user_probe(
            "bot_removed"
        ):
            status, result = await web_app._chat_promote_to_talk("alice", room.id)
        assert status == "bot_removed"
        fake.create_conversation.assert_not_awaited()
        assert result is not None and result["talk_token"] == "live-tok"
        with db.get_db(db_path) as conn:
            binding = db.get_room_binding(conn, room.token, "talk")
        assert binding is not None and binding.surface_ref == "live-tok"

    @pytest.mark.asyncio
    async def test_an_unsettled_404_refuses_rather_than_replacing(
        self, web_config, db_path,
    ):
        """No user token, or a user probe that failed, leaves the two readings
        of the 404 unseparated — so nothing is replaced."""
        from istota import web_app
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "Ideas")
            db.add_room_binding(conn, room.token, "talk", "live-tok")
        fake = _talk_client(info_side_effect=_http_error(404))
        with patch("istota.talk.TalkClient", return_value=fake), _user_probe("unknown"):
            status, _ = await web_app._chat_promote_to_talk("alice", room.id)
        assert status == "unreachable"
        fake.create_conversation.assert_not_awaited()
        with db.get_db(db_path) as conn:
            binding = db.get_room_binding(conn, room.token, "talk")
        assert binding is not None and binding.surface_ref == "live-tok"

    @pytest.mark.asyncio
    async def test_an_unbound_room_never_probes(self, web_config, db_path):
        """The ordinary first promote costs no extra OCS round trip."""
        from istota import web_app
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "Ideas")
        fake = _talk_client()
        with patch("istota.talk.TalkClient", return_value=fake):
            status, result = await web_app._chat_promote_to_talk("alice", room.id)
        assert status == "ok"
        assert result is not None and result["talk_token"] == "new-tok"
        fake.get_conversation_info.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_lost_race_does_not_clobber_the_winner(self, web_config, db_path):
        """Between the probe and the write, another promote bound the room. The
        loser must leave the winner's ref in place — it has minted an orphan
        Talk room either way, and overwriting adds a *second* orphan while
        pointing the room at a conversation the other request is not using."""
        from istota import web_app
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "Ideas")
            db.add_room_binding(conn, room.token, "talk", "dead-tok")

        fake = _talk_client(info_side_effect=_http_error(404))

        async def _create_then_race(*a, **kw):
            # The winner lands while this request is inside create_conversation.
            with db.get_db(db_path) as conn:
                db.replace_room_binding(
                    conn, room.token, "talk", "winner-tok", expected_ref="dead-tok",
                )
            return {"token": "new-tok"}

        fake.create_conversation = AsyncMock(side_effect=_create_then_race)
        with patch("istota.talk.TalkClient", return_value=fake), _user_probe("gone"):
            status, result = await web_app._chat_promote_to_talk("alice", room.id)
        assert status == "raced"
        with db.get_db(db_path) as conn:
            binding = db.get_room_binding(conn, room.token, "talk")
        assert binding is not None and binding.surface_ref == "winner-tok"
        # The loser hands back the winner's ref, so its client stops showing the
        # dead one instead of waiting out the 30s rooms poll.
        assert result is not None and result["talk_token"] == "winner-tok"
        # And it takes its own now-unbindable conversation back out, rather than
        # leaving a participant-less room only the log names.
        fake.delete_conversation.assert_awaited_once_with("new-tok")


class TestTheUserScopedProbe:
    """The second stage on its own. It is what separates a deleted conversation
    from one the bot was merely removed from, so each of its three answers has
    to come from a different observation rather than from a default."""

    @pytest.mark.asyncio
    async def test_the_user_still_seeing_it_means_only_the_bot_left(
        self, web_config,
    ):
        from istota import web_app
        fake = _talk_client(info_return={"token": "live-tok"})
        with patch("istota.web_tokens.feature_enabled", return_value=True), patch(
            "istota.web_tokens.get_access_token", return_value="user-token"
        ), patch("istota.talk.TalkClient", return_value=fake):
            verdict = await web_app._talk_conversation_seen_by_user(
                "live-tok", "alice",
            )
        assert verdict == "bot_removed"
        fake.aclose.assert_awaited()

    @pytest.mark.asyncio
    async def test_the_user_not_seeing_it_either_means_gone(self, web_config):
        from istota import web_app
        fake = _talk_client(info_side_effect=_http_error(404))
        with patch("istota.web_tokens.feature_enabled", return_value=True), patch(
            "istota.web_tokens.get_access_token", return_value="user-token"
        ), patch("istota.talk.TalkClient", return_value=fake):
            verdict = await web_app._talk_conversation_seen_by_user(
                "dead-tok", "alice",
            )
        assert verdict == "gone"

    @pytest.mark.asyncio
    async def test_no_user_token_cannot_settle_it(self, web_config):
        """A deployment with no user tokens is a fact about the deployment, not
        about the conversation — so it must not read as 'gone'."""
        from istota import web_app
        with patch("istota.web_tokens.feature_enabled", return_value=True), patch(
            "istota.web_tokens.get_access_token", return_value=None
        ):
            verdict = await web_app._talk_conversation_seen_by_user(
                "dead-tok", "alice",
            )
        assert verdict == "unknown"

    @pytest.mark.asyncio
    async def test_the_feature_being_off_cannot_settle_it(self, web_config):
        from istota import web_app
        with patch("istota.web_tokens.feature_enabled", return_value=False):
            verdict = await web_app._talk_conversation_seen_by_user(
                "dead-tok", "alice",
            )
        assert verdict == "unknown"

    @pytest.mark.asyncio
    async def test_any_other_status_cannot_settle_it(self, web_config):
        from istota import web_app
        fake = _talk_client(info_side_effect=_http_error(403))
        with patch("istota.web_tokens.feature_enabled", return_value=True), patch(
            "istota.web_tokens.get_access_token", return_value="user-token"
        ), patch("istota.talk.TalkClient", return_value=fake):
            verdict = await web_app._talk_conversation_seen_by_user(
                "dead-tok", "alice",
            )
        assert verdict == "unknown"


class TestPromoteRouteStatuses:
    """The endpoint has to say which of these happened — the client renders a
    different sentence for each, and a bare 404 for all of them is what made
    the dead-binding case read as "nothing happened"."""

    @pytest.mark.asyncio
    async def test_live_is_not_a_404(self, web_config, db_path):
        from istota import web_app
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "Ideas")
            db.add_room_binding(conn, room.token, "talk", "live-tok")
        fake = _talk_client(info_return={"token": "live-tok"})
        with patch("istota.talk.TalkClient", return_value=fake):
            resp = await web_app.chat_promote_room(
                room.id, user={"username": "alice"}, _csrf=None,
            )
        body = _body(resp)
        assert body["status"] == "live"

    @pytest.mark.asyncio
    async def test_reconnected_carries_the_new_room(self, web_config, db_path):
        from istota import web_app
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "Ideas")
            db.add_room_binding(conn, room.token, "talk", "dead-tok")
        fake = _talk_client(info_side_effect=_http_error(404))
        with patch("istota.talk.TalkClient", return_value=fake), _user_probe("gone"):
            resp = await web_app.chat_promote_room(
                room.id, user={"username": "alice"}, _csrf=None,
            )
        body = _body(resp)
        assert body["status"] == "reconnected"
        assert body["room"]["talk_token"] == "new-tok"

    @pytest.mark.asyncio
    async def test_a_conversation_nextcloud_never_made_is_a_502(
        self, web_config, db_path,
    ):
        # Distinct from the 404, which means the room is not promotable at all.
        # `PromoteStatus` deliberately does not model this one, so the client
        # falls to its generic catch — which only works if it is an error status.
        from istota import web_app
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "Ideas")
        fake = _talk_client()
        fake.create_conversation = AsyncMock(return_value={})
        with patch("istota.talk.TalkClient", return_value=fake):
            resp = await web_app.chat_promote_room(
                room.id, user={"username": "alice"}, _csrf=None,
            )
        assert resp.status_code == 502
        with db.get_db(db_path) as conn:
            assert db.get_room_binding(conn, room.token, "talk") is None

    @pytest.mark.asyncio
    async def test_an_unknown_room_is_still_a_404(self, web_config):
        from istota import web_app
        resp = await web_app.chat_promote_room(
            99999, user={"username": "alice"}, _csrf=None,
        )
        assert resp.status_code == 404


def _body(resp) -> dict:
    """The JSON a route returned, whether it handed back a JSONResponse or a
    plain dict."""
    import json
    if isinstance(resp, dict):
        return resp
    return json.loads(bytes(resp.body).decode())
