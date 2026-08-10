"""ISSUE-242 / ISSUE-243 — one room, one question, one answer, on every surface.

Two halves of one defect:

* **242** — a notification delivered to ``talk:<token>`` wrote nothing into that
  token's canonical transcript, so the web view of the same room showed
  nothing. For the confirmation prompt that is silent mail loss.
* **243** — a bare "yes" answered a parked confirmation in Talk and started a
  new task in web, where ``_chat_create_web_task``'s room-scoped cancel could
  discard the very question the answer approved.

The shared parse / lookup / act verbs live in ``confirmations.py`` so the three
surfaces cannot drift; the tests below are grouped by which of them they pin.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from istota import confirmations, db
from istota.config import Config, SiteConfig, UserConfig, WebConfig

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

_needs_web_deps = pytest.mark.skipif(
    not _has_web_deps, reason="web dependencies not installed",
)

if _has_web_deps:
    from httpx import ASGITransport, AsyncClient

ORIGIN = {"origin": "https://example.com"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _park(conn, user_id, *, token, prompt="mail", confirmation="Email from A",
          source_type="email"):
    task_id = db.create_task(
        conn, prompt=prompt, user_id=user_id, source_type=source_type,
        conversation_token=token,
    )
    db.set_task_confirmation(conn, task_id, confirmation)
    return task_id


def _rows(conn, token):
    return conn.execute(
        "SELECT id, role, body, origin_surface FROM messages WHERE room_token = ? "
        "ORDER BY id",
        (token,),
    ).fetchall()


# ---------------------------------------------------------------------------
# 1. The shared parse
# ---------------------------------------------------------------------------


class TestParseAnswer:
    @pytest.mark.parametrize("text", [
        "yes", "y", "OK", "okay", " proceed ", "confirm", "do it", "Go ahead",
    ])
    def test_affirmative_words(self, text):
        answer = confirmations.parse_answer(text)
        assert answer == confirmations.Answer(approve=True, trust_sender=False)

    @pytest.mark.parametrize("text", ["no", "n", "cancel", "abort", "stop",
                                      "don't", "nevermind"])
    def test_negative_words(self, text):
        answer = confirmations.parse_answer(text)
        assert answer == confirmations.Answer(approve=False, trust_sender=False)

    @pytest.mark.parametrize("text", ["yes trust", "yes, trust", "Y TRUST"])
    def test_trust_variants(self, text):
        answer = confirmations.parse_answer(text)
        assert answer == confirmations.Answer(approve=True, trust_sender=True)

    @pytest.mark.parametrize("text", [
        "no thanks, I already did that",
        "yes please write it up",
        "",
        "confirmation of the booking",
    ])
    def test_a_message_that_merely_starts_with_a_word_is_not_an_answer(self, text):
        """Exact match, not a prefix test — swallowing a real message loses it."""
        assert confirmations.parse_answer(text) is None


# ---------------------------------------------------------------------------
# 2. The shared three-path lookup
# ---------------------------------------------------------------------------


class TestResolve:
    def test_path_b_same_conversation(self, make_config):
        config = make_config()
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task_id = _park(conn, "alice", token="room-1")
            _park(conn, "alice", token="room-2", prompt="other")
            conn.commit()
            res = confirmations.resolve(conn, "alice", conversation_token="room-1")
        assert res.task is not None and res.task.id == task_id

    def test_path_c_single_open_anywhere(self, make_config):
        config = make_config()
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task_id = _park(conn, "alice", token="thread-hash")
            conn.commit()
            res = confirmations.resolve(conn, "alice", conversation_token="room-1")
        assert res.task is not None and res.task.id == task_id

    def test_several_open_is_ambiguous_and_answers_none(self, make_config):
        """ISSUE-241's rule: a bare "yes" must not land on whichever arrived last."""
        config = make_config()
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            first = _park(conn, "alice", token="t1", prompt="one")
            second = _park(conn, "alice", token="t2", prompt="two")
            conn.commit()
            res = confirmations.resolve(conn, "alice", conversation_token="room-1")
        assert res.task is None
        assert [t.id for t in res.ambiguous] == [first, second]

    def test_path_a_wins_over_path_b(self, make_config):
        config = make_config()
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            here = _park(conn, "alice", token="room-1", prompt="here")
            elsewhere = _park(conn, "alice", token="thread", prompt="elsewhere")
            db.update_talk_response_id(conn, elsewhere, 4242)
            conn.commit()
            res = confirmations.resolve(
                conn, "alice", conversation_token="room-1", talk_response_id=4242,
            )
        assert res.task is not None and res.task.id == elsewhere
        assert here != elsewhere

    def test_another_users_question_is_not_resolvable(self, make_config):
        """The ownership check moved inside `resolve`; A and B are what need it."""
        config = make_config()
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            _park(conn, "bob", token="room-1")
            conn.commit()
            res = confirmations.resolve(conn, "alice", conversation_token="room-1")
        assert res.task is None and res.ambiguous == ()


# ---------------------------------------------------------------------------
# 3. The shared act + ack
# ---------------------------------------------------------------------------


class TestApplyAnswer:
    def test_plain_approve_releases_and_acks(self, make_config):
        config = make_config()
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task_id = _park(conn, "alice", token="room-1")
            conn.commit()
            ack = confirmations.apply_answer(
                conn, db.get_task(conn, task_id),
                confirmations.Answer(approve=True, trust_sender=False),
            )
            conn.commit()
            assert db.get_task(conn, task_id).status == "pending"
        assert ack == "Confirmed."

    def test_decline_cancels_and_acks(self, make_config):
        config = make_config()
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task_id = _park(conn, "alice", token="room-1")
            conn.commit()
            ack = confirmations.apply_answer(
                conn, db.get_task(conn, task_id),
                confirmations.Answer(approve=False, trust_sender=False),
            )
            conn.commit()
            assert db.get_task(conn, task_id).status == "cancelled"
        assert ack == "Task cancelled."

    def test_trust_names_the_sender_only_when_there_is_one(self, make_config):
        config = make_config()
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            with_sender = _park(conn, "alice", token="t1", prompt="one")
            db.mark_email_processed(
                conn, email_id="1", sender_email="x@y.z", subject="Invite",
                thread_id="t1", message_id="<m@y.z>", references=None,
                user_id="alice", task_id=with_sender, routing_method="plus_address",
            )
            without = _park(conn, "alice", token="t2", prompt="two")
            conn.commit()
            trust = confirmations.Answer(approve=True, trust_sender=True)
            named = confirmations.apply_answer(
                conn, db.get_task(conn, with_sender), trust,
            )
            unnamed = confirmations.apply_answer(
                conn, db.get_task(conn, without), trust,
            )
            conn.commit()
        assert named == (
            "Trusted x@y.z — future emails will be processed automatically."
        )
        assert unnamed == "Confirmed."


class TestConfirmCommand:
    async def _dispatch(self, config, conn, text, *, surface="web", token="room-1"):
        from istota import commands
        return await commands.dispatch(
            config, "alice", token, text, surface=surface, conn=conn,
        )

    @pytest.mark.asyncio
    async def test_it_returns_the_ids_the_web_client_stamps(self, make_config):
        """`!confirm` writes the same durable pair a bare "yes" does, so it needs
        the same `command_data` — without it the client leaves both rows
        unstamped and the room stream's echo appends a second copy of each."""
        config = make_config()
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "room-1", "alice", origin="web")
            task_id = _park(conn, "alice", token="t1")
            conn.commit()
            result = await self._dispatch(config, conn, f"!confirm {task_id}")
            rows = _rows(conn, "room-1")

        assert result.data["kind"] == "confirmation_answered"
        assert [r["role"] for r in rows] == ["user", "system"]
        assert result.data["user_msg_id"] == rows[0]["id"]
        assert result.data["system_msg_id"] == rows[1]["id"]
        assert rows[1]["body"] == result.text

    @pytest.mark.asyncio
    async def test_a_cli_answer_leaves_no_half_visible_row(self, make_config):
        """`TRANSCRIPT_SURFACE_FILTER` renders a `role='user'` row only for
        web/talk/email, while every `role='system'` row renders. Recording a
        `cli` answer would show the ack with an invisible question above it."""
        config = make_config()
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "room-1", "alice", origin="web")
            task_id = _park(conn, "alice", token="t1")
            conn.commit()
            result = await self._dispatch(
                config, conn, f"!confirm {task_id}", surface="cli",
            )
            assert db.get_task(conn, task_id).status == "pending"
            rows = _rows(conn, "room-1")
        assert result.text
        assert rows == []

    @pytest.mark.asyncio
    async def test_the_ambiguity_listing_records_nothing(self, make_config):
        config = make_config()
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "room-1", "alice", origin="web")
            _park(conn, "alice", token="t1", prompt="one")
            _park(conn, "alice", token="t2", prompt="two")
            conn.commit()
            result = await self._dispatch(config, conn, "!confirm")
            rows = _rows(conn, "room-1")
        assert "2 things are waiting" in result.text
        assert rows == []


class TestRecordExchange:
    def test_both_halves_land_in_the_room_transcript(self, make_config):
        config = make_config()
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "room-1", "alice", origin="talk")
            confirmations.record_exchange(
                conn, "room-1", answer_text="yes", ack="Confirmed.",
                origin_surface="talk",
            )
            conn.commit()
            rows = _rows(conn, "room-1")
        assert [(r["role"], r["body"]) for r in rows] == [
            ("user", "yes"), ("system", "Confirmed."),
        ]
        assert all(r["origin_surface"] == "talk" for r in rows)

    def test_a_token_with_no_room_is_a_no_op(self, make_config):
        """A synthetic email-thread token is not a room and must not mint one."""
        config = make_config()
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            ids = confirmations.record_exchange(
                conn, "a1b2c3d4e5f60718", answer_text="yes", ack="Confirmed.",
                origin_surface="talk",
            )
            conn.commit()
            assert _rows(conn, "a1b2c3d4e5f60718") == []
            assert db.get_room(conn, "a1b2c3d4e5f60718") is None
        assert ids == (None, None)

    def test_the_answer_row_carries_no_task_id(self, make_config):
        """`(room, role, task_id)` is unique and the per-task user slot belongs
        to the original prompt; an answer row is display-only, like `!steer`."""
        config = make_config()
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "room-1", "alice", origin="web")
            confirmations.record_exchange(
                conn, "room-1", answer_text="yes", ack="Confirmed.",
                origin_surface="web",
            )
            conn.commit()
            task_ids = [
                r["task_id"] for r in conn.execute(
                    "SELECT task_id FROM messages WHERE room_token = 'room-1'",
                ).fetchall()
            ]
        assert task_ids == [None, None]


# ---------------------------------------------------------------------------
# 4. Talk keeps its behaviour, and gains an ack on a plain approve
# ---------------------------------------------------------------------------


class TestTalkAnswer:
    @pytest.fixture
    def talk_config(self, make_config):
        config = make_config()
        db.init_db(config.db_path)
        config.users = {"alice": UserConfig(display_name="Alice")}
        return config

    async def _reply(self, config, conn, text, token, *, reply_to_talk_id=None):
        from istota.transport.talk.inbound import handle_confirmation_reply

        client = MagicMock()
        client.send_message = AsyncMock(return_value=1)
        with patch(
            "istota.transport.talk.inbound.get_talk_client", return_value=client,
        ):
            handled = await handle_confirmation_reply(
                conn, config, "alice", text, token,
                reply_to_talk_id=reply_to_talk_id,
            )
        return handled, client

    @pytest.mark.asyncio
    async def test_a_plain_yes_is_acked(self, talk_config):
        """Talk used to stay silent on a plain approve — the web sketch said
        "Confirmed." and the two surfaces have one answer.

        The approved prompt lands above the answer: `confirmations.approve`
        undoes the transcript-mirror suppression the gate applied, so the room
        reads question, answer, ack in that order rather than an answer to
        nothing (ISSUE-136, re-reached through the gate).
        """
        with db.get_db(talk_config.db_path) as conn:
            db.register_room(conn, "room-1", "alice", origin="talk")
            task_id = _park(conn, "alice", token="room-1")
            conn.commit()
            handled, client = await self._reply(talk_config, conn, "yes", "room-1")
            conn.commit()
            assert db.get_task(conn, task_id).status == "pending"
            rows = _rows(conn, "room-1")
        assert handled is True
        client.send_message.assert_awaited_once_with("room-1", "Confirmed.")
        assert [(r["role"], r["body"]) for r in rows] == [
            ("user", "mail"), ("user", "yes"), ("system", "Confirmed."),
        ]

    @pytest.mark.asyncio
    async def test_a_decline_is_acked_and_recorded(self, talk_config):
        with db.get_db(talk_config.db_path) as conn:
            db.register_room(conn, "room-1", "alice", origin="talk")
            task_id = _park(conn, "alice", token="room-1")
            conn.commit()
            handled, client = await self._reply(talk_config, conn, "no", "room-1")
            conn.commit()
            assert db.get_task(conn, task_id).status == "cancelled"
            rows = _rows(conn, "room-1")
        assert handled is True
        client.send_message.assert_awaited_once_with("room-1", "Task cancelled.")
        assert [r["body"] for r in rows] == ["no", "Task cancelled."]

    @pytest.mark.asyncio
    async def test_an_unmatched_yes_falls_through_to_task_creation(self, talk_config):
        """"yes" is a perfectly ordinary reply to a question asked in prose."""
        with db.get_db(talk_config.db_path) as conn:
            db.register_room(conn, "room-1", "alice", origin="talk")
            handled, client = await self._reply(talk_config, conn, "yes", "room-1")
            conn.commit()
            rows = _rows(conn, "room-1")
        assert handled is False
        client.send_message.assert_not_awaited()
        assert rows == []

    @pytest.mark.asyncio
    async def test_the_ambiguous_listing_answers_none_and_records_nothing(
        self, talk_config,
    ):
        """The listing decides nothing, so it leaves no durable row — the
        `!command` precedent rather than the decision precedent."""
        with db.get_db(talk_config.db_path) as conn:
            db.register_room(conn, "room-1", "alice", origin="talk")
            first = _park(conn, "alice", token="t1", prompt="one")
            second = _park(conn, "alice", token="t2", prompt="two")
            conn.commit()
            handled, client = await self._reply(talk_config, conn, "yes", "room-1")
            conn.commit()
            assert db.get_task(conn, first).status == "pending_confirmation"
            assert db.get_task(conn, second).status == "pending_confirmation"
            rows = _rows(conn, "room-1")
        assert handled is True
        assert "2 things are waiting" in client.send_message.await_args[0][1]
        assert rows == []

    @pytest.mark.asyncio
    async def test_path_b_survives_a_promoted_rooms_token_split(self, talk_config):
        """A task parks under the canonical token, so the lookup has to resolve
        the Talk token first — otherwise Path B misses and a second open
        question turns a same-room answer into an ambiguity listing."""
        with db.get_db(talk_config.db_path) as conn:
            db.register_room(conn, "canonical", "alice", origin="web")
            db.add_room_binding(conn, "canonical", "talk", "talk-token")
            here = _park(conn, "alice", token="canonical", prompt="here")
            elsewhere = _park(conn, "alice", token="thread", prompt="elsewhere")
            conn.commit()
            handled, client = await self._reply(
                talk_config, conn, "yes", "talk-token",
            )
            conn.commit()
            assert db.get_task(conn, here).status == "pending"
            assert db.get_task(conn, elsewhere).status == "pending_confirmation"
        assert handled is True
        client.send_message.assert_awaited_once_with("talk-token", "Confirmed.")

    @pytest.mark.asyncio
    async def test_a_promoted_room_records_under_its_canonical_token(self, talk_config):
        """A web room promoted to Talk has a canonical token that is not the
        Talk one; the transcript row belongs to the canonical room."""
        with db.get_db(talk_config.db_path) as conn:
            db.register_room(conn, "canonical", "alice", origin="web")
            db.add_room_binding(conn, "canonical", "talk", "talk-token")
            _park(conn, "alice", token="canonical")
            conn.commit()
            handled, _client = await self._reply(
                talk_config, conn, "yes", "talk-token",
            )
            conn.commit()
            assert _rows(conn, "talk-token") == []
            rows = _rows(conn, "canonical")
        assert handled is True
        assert [r["body"] for r in rows] == ["mail", "yes", "Confirmed."]


# ---------------------------------------------------------------------------
# 5. ISSUE-242 — a Talk-delivered notification lands in the web transcript
# ---------------------------------------------------------------------------


class TestTalkToRoomMirror:
    def _config(self, make_config):
        from istota.config import NextcloudConfig
        config = make_config()
        db.init_db(config.db_path)
        config.users = {"alice": UserConfig(display_name="Alice")}
        config.nextcloud = NextcloudConfig(url="https://cloud.test")
        return config

    def _send(self, config, *, surface, message="Heads up"):
        from istota import notifications
        with (
            patch.object(notifications, "_send_talk", new=AsyncMock(return_value=77)),
            patch.object(notifications, "_send_web", return_value=True),
        ):
            return notifications.send_notification(
                config, "alice", message, surface=surface, title="Alert",
            )

    def test_a_talk_notification_is_mirrored_into_the_rooms_transcript(
        self, make_config,
    ):
        config = self._config(make_config)
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "2ay6qic9", "alice", origin="talk")
            conn.commit()

        assert self._send(config, surface="talk:2ay6qic9") is True

        with db.get_db(config.db_path) as conn:
            rows = _rows(conn, "2ay6qic9")
        assert [(r["role"], r["body"]) for r in rows] == [("system", "Heads up")]
        # Provenance, not a visibility gate — `list_system_messages` reads role.
        assert rows[0]["origin_surface"] == "talk"

    def test_the_talk_message_id_is_stamped_for_the_reply_walk_back(
        self, make_config,
    ):
        config = self._config(make_config)
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "2ay6qic9", "alice", origin="talk")
            conn.commit()

        self._send(config, surface="talk:2ay6qic9")

        with db.get_db(config.db_path) as conn:
            msg_id = conn.execute(
                "SELECT id FROM messages WHERE room_token = '2ay6qic9'",
            ).fetchone()["id"]
            assert db.get_message_external_id(conn, msg_id, "talk") == "77"

    def test_a_token_with_no_room_row_is_left_alone(self, make_config):
        """Same gate `_store_room_turn` uses: room existence, not surface config."""
        config = self._config(make_config)
        assert self._send(config, surface="talk:a1b2c3d4e5f60718") is True
        with db.get_db(config.db_path) as conn:
            assert db.get_room(conn, "a1b2c3d4e5f60718") is None
            assert _rows(conn, "a1b2c3d4e5f60718") == []

    def test_a_route_naming_both_legs_writes_exactly_one_row(self, make_config):
        """`_send_web` writes its own row; the mirror must not add a second.

        `_send_web` is left unpatched here on purpose — patched out, this test
        would pass just as well if the mirror were writing the only row, or if
        the web leg had been deleted outright.
        """
        from istota import notifications
        config = self._config(make_config)
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "2ay6qic9", "alice", origin="talk")
            conn.commit()

        with patch.object(notifications, "_send_talk", new=AsyncMock(return_value=77)):
            notifications.send_notification(
                config, "alice", "Heads up",
                surface="talk:2ay6qic9,web:2ay6qic9", title="Alert",
            )

        with db.get_db(config.db_path) as conn:
            rows = _rows(conn, "2ay6qic9")
        assert [(r["role"], r["body"]) for r in rows] == [("system", "Heads up")]
        # The web leg's row, not the mirror's.
        assert rows[0]["origin_surface"] != "talk"

    def test_a_promoted_rooms_two_names_are_not_two_rows(self, make_config):
        """A promoted room's Talk token differs from its own, so comparing the
        raw tokens let both legs write."""
        from istota import notifications
        config = self._config(make_config)
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "canonical", "alice", origin="web")
            db.add_room_binding(conn, "canonical", "talk", "talk-token")
            conn.commit()

        with patch.object(notifications, "_send_talk", new=AsyncMock(return_value=77)):
            notifications.send_notification(
                config, "alice", "Heads up",
                surface="talk:talk-token,web:canonical", title="Alert",
            )

        with db.get_db(config.db_path) as conn:
            assert len(_rows(conn, "canonical")) == 1

    def test_a_bare_talk_destination_mirrors_into_the_resolved_token(
        self, make_config,
    ):
        """The mirror needs the token a bare `talk` actually lands on, which is
        why `_dispatch` resolves it rather than leaving that to `_send_talk`."""
        from istota.config import BriefingConfig
        config = self._config(make_config)
        config.users["alice"] = UserConfig(
            display_name="Alice",
            briefings=[BriefingConfig(
                name="morning", cron="0 6 * * *", conversation_token="briefroom",
            )],
        )
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "briefroom", "alice", origin="talk")
            conn.commit()

        self._send(config, surface="talk")

        with db.get_db(config.db_path) as conn:
            assert [r["body"] for r in _rows(conn, "briefroom")] == ["Heads up"]

    def test_a_failed_transcript_write_never_reports_the_send_as_failed(
        self, make_config,
    ):
        """The Talk post has already happened. Best-effort, and loud in the log."""
        from istota import notifications
        config = self._config(make_config)
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "2ay6qic9", "alice", origin="talk")
            conn.commit()

        with (
            patch.object(notifications, "_send_talk", new=AsyncMock(return_value=77)),
            patch.object(db, "add_message", side_effect=RuntimeError("disk full")),
        ):
            sent = notifications.send_notification(
                config, "alice", "Heads up", surface="talk:2ay6qic9",
            )
        assert sent is True

    def test_the_confirmation_prompt_reaches_the_web_view_of_its_room(
        self, make_config,
    ):
        """The repro: the gate posts to `alerts_channel`, and the web reader of
        that same room saw nothing."""
        from istota import notifications
        config = self._config(make_config)
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "2ay6qic9", "alice", origin="talk")
            conn.commit()

        with patch.object(
            notifications, "_send_talk", new=AsyncMock(return_value=104045),
        ):
            delivered, talk_id = notifications.send_confirmation_prompt(
                config, "alice",
                "Email from unknown sender x@y.z\nReply 'yes' to process.",
                conversation_token="2ay6qic9",
            )

        assert delivered is True and talk_id == 104045
        with db.get_db(config.db_path) as conn:
            bodies = [r["body"] for r in _rows(conn, "2ay6qic9")]
        assert bodies and "Reply 'yes' to process." in bodies[0]


# ---------------------------------------------------------------------------
# 6. ISSUE-243 — the web composer answers, and does not cancel the question
# ---------------------------------------------------------------------------


def _web_config(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    return Config(
        db_path=db_path,
        nextcloud_mount_path=tmp_path / "mount",
        site=SiteConfig(hostname="example.com"),
        users={"alice": UserConfig(display_name="Alice"),
               "bob": UserConfig(display_name="Bob")},
        web=WebConfig(
            enabled=True, port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web", oauth2_client_secret="s",
            session_secret_key="test-session-key",
        ),
        bot_name="Istota",
    )


@pytest.fixture
async def web_client(tmp_path):
    if not _has_web_deps:
        pytest.skip("web dependencies not installed")
    import istota.web_app as mod
    config = _web_config(tmp_path)
    mod._config = config
    mod.app.state.istota_config = config
    mod._oauth = MagicMock()
    mod._oauth.nextcloud = MagicMock()
    transport = ASGITransport(app=mod.app)
    async with AsyncClient(transport=transport, base_url="https://example.com") as c:
        yield c


async def _login(client, username):
    import istota.web_app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(
        return_value={"user_id": username},
    )
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


async def _default_room(client, cookies):
    rooms = (await client.get("/istota/api/chat/rooms", cookies=cookies)).json()
    return rooms["rooms"][0]


@_needs_web_deps
class TestWebAnswer:
    @pytest.mark.asyncio
    async def test_yes_in_the_room_answers_instead_of_cancelling(self, web_client):
        """The regression that motivated this: `_chat_create_web_task`
        room-scoped-cancels on a new message, so a same-room gate was cancelled
        by its own approval."""
        import istota.web_app as mod
        cookies = await _login(web_client, "alice")
        room = await _default_room(web_client, cookies)

        with db.get_db(mod._config.db_path) as conn:
            task_id = _park(
                conn, "alice", token=room["token"], prompt="do the thing",
                confirmation="Do the thing?", source_type="web",
            )
            conn.commit()

        resp = await web_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "yes"}, cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] is None
        assert body["inline_result"] == "Confirmed."
        assert body["command_data"]["kind"] == "confirmation_answered"

        with db.get_db(mod._config.db_path) as conn:
            assert db.get_task(conn, task_id).status == "pending"
            # No second task saying "yes".
            assert conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE prompt = 'yes'",
            ).fetchone()[0] == 0

    @pytest.mark.asyncio
    async def test_a_cross_room_email_gate_is_answered_from_any_room(self, web_client):
        """The `plus_address` case: parked under a synthetic thread token, so
        Path B cannot see it and Path C is what answers."""
        import istota.web_app as mod
        cookies = await _login(web_client, "alice")
        room = await _default_room(web_client, cookies)

        with db.get_db(mod._config.db_path) as conn:
            task_id = _park(conn, "alice", token="a1b2c3d4e5f60718")
            conn.commit()

        resp = await web_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "no"}, cookies=cookies, headers=ORIGIN,
        )
        assert resp.json()["inline_result"] == "Task cancelled."
        with db.get_db(mod._config.db_path) as conn:
            assert db.get_task(conn, task_id).status == "cancelled"

    @pytest.mark.asyncio
    async def test_yes_with_nothing_parked_still_creates_a_task(self, web_client):
        """Falling through is required, not a fallback."""
        cookies = await _login(web_client, "alice")
        room = await _default_room(web_client, cookies)

        resp = await web_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "yes"}, cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 200
        assert resp.json()["task_id"] is not None

    @pytest.mark.asyncio
    async def test_the_exchange_survives_a_reload(self, web_client):
        """An authorization decision deserves an answer after a refresh, which
        the `!command` inline-only precedent does not give."""
        import istota.web_app as mod
        cookies = await _login(web_client, "alice")
        room = await _default_room(web_client, cookies)

        with db.get_db(mod._config.db_path) as conn:
            _park(conn, "alice", token="a1b2c3d4e5f60718")
            conn.commit()

        resp = await web_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "yes"}, cookies=cookies, headers=ORIGIN,
        )
        data = resp.json()["command_data"]
        assert isinstance(data["user_msg_id"], int)
        assert isinstance(data["system_msg_id"], int)

        history = await web_client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )
        bodies = [(m["role"], m["text"]) for m in history.json()["messages"]]
        assert ("user", "yes") in bodies
        assert ("system", "Confirmed.") in bodies

    @pytest.mark.asyncio
    async def test_yes_trust_is_reachable_from_the_composer(self, web_client):
        """The banner has no trust affordance at all, so this is the only place
        web gains one."""
        import istota.web_app as mod
        cookies = await _login(web_client, "alice")
        room = await _default_room(web_client, cookies)

        with db.get_db(mod._config.db_path) as conn:
            task_id = _park(conn, "alice", token="a1b2c3d4e5f60718")
            db.mark_email_processed(
                conn, email_id="1", sender_email="x@y.z", subject="Invite",
                thread_id="a1b2c3d4e5f60718", message_id="<m@y.z>", references=None,
                user_id="alice", task_id=task_id, routing_method="plus_address",
            )
            conn.commit()

        resp = await web_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "yes trust"}, cookies=cookies, headers=ORIGIN,
        )
        assert "Trusted x@y.z" in resp.json()["inline_result"]
        with db.get_db(mod._config.db_path) as conn:
            assert db.is_sender_trusted_in_db(conn, "alice", "x@y.z")

    @pytest.mark.asyncio
    async def test_a_retried_answer_replays_rather_than_answering_again(
        self, web_client,
    ):
        """A send the server accepted but never got to report is re-POSTed with
        the same `client_msg_id`. Re-resolving would be worse than wasteful: the
        first attempt consumed the question, so a gate that parked in between
        would be the single open one and get approved on a "yes" the user typed
        at something else. `_is_own_replay` cannot see this exchange — its
        lookup inner-joins `tasks` and these rows carry no task id.
        """
        import istota.web_app as mod
        cookies = await _login(web_client, "alice")
        room = await _default_room(web_client, cookies)

        with db.get_db(mod._config.db_path) as conn:
            answered = _park(conn, "alice", token="a1b2c3d4e5f60718")
            conn.commit()

        body = {"text": "yes", "client_msg_id": "cid-retry-1"}
        first = await web_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json=body, cookies=cookies, headers=ORIGIN,
        )
        assert first.json()["inline_result"] == "Confirmed."

        # A second gate arrives between the accepted send and the retry.
        with db.get_db(mod._config.db_path) as conn:
            arrived_later = _park(conn, "alice", token="t-later", prompt="later")
            conn.commit()

        second = await web_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json=body, cookies=cookies, headers=ORIGIN,
        )
        assert second.status_code == 200
        assert second.json()["inline_result"] == "Confirmed."
        # Same ids, so the client folds it into the rows it already drew.
        assert second.json()["command_data"] == first.json()["command_data"]

        with db.get_db(mod._config.db_path) as conn:
            assert db.get_task(conn, answered).status == "pending"
            # The one the user never answered is untouched.
            assert db.get_task(conn, arrived_later).status == "pending_confirmation"
            assert conn.execute(
                "SELECT COUNT(*) FROM messages WHERE room_token = ? AND role = 'user'",
                (room["token"],),
            ).fetchone()[0] == 1

    @pytest.mark.asyncio
    async def test_an_ambiguous_answer_writes_no_transcript_rows(self, web_client):
        """It answers nothing, and it is the one branch that consumes no
        question — so recording it would let a loop append rows without bound,
        this path returning before the rate limit in `_chat_create_web_task`."""
        import istota.web_app as mod
        cookies = await _login(web_client, "alice")
        room = await _default_room(web_client, cookies)

        with db.get_db(mod._config.db_path) as conn:
            _park(conn, "alice", token="t1", prompt="one")
            _park(conn, "alice", token="t2", prompt="two")
            conn.commit()

        resp = await web_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "yes"}, cookies=cookies, headers=ORIGIN,
        )
        data = resp.json()["command_data"]
        assert data["user_msg_id"] is None and data["system_msg_id"] is None
        with db.get_db(mod._config.db_path) as conn:
            assert _rows(conn, room["token"]) == []

    @pytest.mark.asyncio
    async def test_several_open_gates_list_instead_of_answering(self, web_client):
        import istota.web_app as mod
        cookies = await _login(web_client, "alice")
        room = await _default_room(web_client, cookies)

        with db.get_db(mod._config.db_path) as conn:
            first = _park(conn, "alice", token="t1", prompt="one")
            second = _park(conn, "alice", token="t2", prompt="two")
            conn.commit()

        resp = await web_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "yes"}, cookies=cookies, headers=ORIGIN,
        )
        assert "2 things are waiting" in resp.json()["inline_result"]
        with db.get_db(mod._config.db_path) as conn:
            assert db.get_task(conn, first).status == "pending_confirmation"
            assert db.get_task(conn, second).status == "pending_confirmation"

    @pytest.mark.asyncio
    async def test_replying_to_the_mirrored_prompt_picks_that_question(
        self, web_client,
    ):
        """Path A on web: the mirrored row carries the Talk id, so a cited web
        reply walks back to `tasks.talk_response_id`."""
        import istota.web_app as mod
        cookies = await _login(web_client, "alice")
        room = await _default_room(web_client, cookies)

        with db.get_db(mod._config.db_path) as conn:
            _park(conn, "alice", token="t1", prompt="one")
            target = _park(conn, "alice", token="t2", prompt="two")
            db.update_talk_response_id(conn, target, 104045)
            prompt_msg = db.add_message(
                conn, room["token"], role="system",
                body="Email from x@y.z — reply 'yes' to process.",
                origin_surface="talk", external_ids={"talk": "104045"},
            )
            conn.commit()

        resp = await web_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "yes", "reply_to_msg_id": prompt_msg},
            cookies=cookies, headers=ORIGIN,
        )
        assert resp.json()["inline_result"] == "Confirmed."
        with db.get_db(mod._config.db_path) as conn:
            assert db.get_task(conn, target).status == "pending"

    @pytest.mark.asyncio
    async def test_another_users_gate_is_not_answerable(self, web_client):
        import istota.web_app as mod
        cookies = await _login(web_client, "alice")
        room = await _default_room(web_client, cookies)

        with db.get_db(mod._config.db_path) as conn:
            task_id = _park(conn, "bob", token=room["token"])
            conn.commit()

        resp = await web_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "yes"}, cookies=cookies, headers=ORIGIN,
        )
        # Not an answer — it falls through and becomes an ordinary message.
        assert resp.json()["task_id"] is not None
        with db.get_db(mod._config.db_path) as conn:
            assert db.get_task(conn, task_id).status == "pending_confirmation"
