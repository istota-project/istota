"""ISSUE-255 — the self-reply suppression is recorded on the task, so the five
consumers keyed on `conversation_token` stop seeing the exchange.

ISSUE-254 kept a thread reply the user sends from their own address out of the
origin room's *transcript*. It did not keep it out of the room's context, its
memory namespace, or its error path: the task still carries the origin room as
its `conversation_token` — deliberately, so the reply continues that
conversation — and the decision that produced the suppression existed only for
the length of one function call. Every consumer reading the column rather than
the transcript therefore behaved as though nothing had been withheld.

The fix records the fact (`tasks.withheld_from_room`) and teaches each consumer
to consult it. Three of the five were re-charging the context bill ISSUE-254 was
filed about; the other two were losing a message outright, because dropping the
origin leg leaves an email-only plan with no error channel at all.

The guard that matters throughout is an **external correspondent's** reply,
which keeps every one of these behaviours unchanged — it is the case the room
mirror exists for.
"""

from unittest.mock import patch

import pytest

from istota import db
from istota.config import Config, EmailConfig, MemorySearchConfig, UserConfig
from istota.scheduler import process_one_task
from istota.skills.email import Email, EmailEnvelope
from istota.transport.email.inbound import poll_emails

ROOM = "rm_web123"
USER = "carol"
USER_ADDR = "carol@test.com"
EXTERNAL_ADDR = "ext@x.com"
ORIGIN_MESSAGE_ID = "<origin_out@bot.com>"
# A synthetic email-thread token: 16 hex chars, the shape `compute_thread_id`
# produces for a thread that names no room. Spelled from a deliberately small
# alphabet — a random-looking hex blob of this length reads as a credential to
# the pre-commit secret scan, and the neighbouring ISSUE-254 tests use the same
# stand-in for the same reason.
THREAD_HASH = "deadbeefdeadbeef"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


@pytest.fixture
def config(db_path, tmp_path):
    config = Config()
    config.db_path = db_path
    config.temp_dir = tmp_path / "temp"
    config.temp_dir.mkdir(exist_ok=True)
    config.skills_dir = tmp_path / "skills"
    config.skills_dir.mkdir(exist_ok=True)
    config.email = EmailConfig(
        enabled=True,
        imap_host="imap.test", imap_port=993,
        imap_user="user", imap_password="pass",
        smtp_host="smtp.test", smtp_port=587,
        bot_email="bot@test.com",
    )
    config.users = {USER: UserConfig(email_addresses=[USER_ADDR])}
    return config


def _origin_room(conn):
    db.register_room(conn, ROOM, USER, origin="web")
    db.add_room_binding(conn, ROOM, "web", ROOM)
    db.add_room_binding(conn, ROOM, "talk", ROOM)


def _sent_from_the_room(conn, *, to_addr=EXTERNAL_ADDR, origin_target=f"room:{ROOM}"):
    db.record_sent_email(
        conn,
        user_id=USER,
        message_id=ORIGIN_MESSAGE_ID,
        to_addr=to_addr,
        subject="Question",
        conversation_token=ROOM,
        origin_target=origin_target,
    )


def _poll_reply(config, *, sender, to=("bot@test.com",), body="My answer"):
    envelope = EmailEnvelope(
        id="20", subject="Re: Question", sender=sender,
        date="Mon, 01 Jan 2026 12:00:00 +0000", is_read=False,
    )
    email = Email(
        id="20", subject="Re: Question", sender=sender,
        date="Mon, 01 Jan 2026 12:00:00 +0000",
        body=body, attachments=[],
        message_id="<r20@x.com>", references=ORIGIN_MESSAGE_ID,
        to=to, cc=(), authentication_results=None,
    )
    with (
        patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
        patch("istota.transport.email.inbound.read_email", return_value=email),
        patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        patch("istota.transport.email.inbound._deliver_confirmation_prompts"),
        patch("istota.transport.email.inbound._deliver_dmarc_alerts"),
    ):
        task_ids = poll_emails(config)
    assert len(task_ids) == 1
    with db.get_db(config.db_path) as conn:
        return db.get_task(conn, task_ids[0])


def tmp_deferred_dir(config, task):
    """The user temp dir the deferred handlers read, created on demand."""
    path = config.temp_dir / task.user_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _self_reply(config, db_path):
    with db.get_db(db_path) as conn:
        _origin_room(conn)
        _sent_from_the_room(conn, to_addr=USER_ADDR)
    return _poll_reply(config, sender=USER_ADDR)


def _external_reply(config, db_path):
    with db.get_db(db_path) as conn:
        _origin_room(conn)
        _sent_from_the_room(conn)
    return _poll_reply(config, sender=EXTERNAL_ADDR)


# ---------------------------------------------------------------------------
# The fact itself
# ---------------------------------------------------------------------------


class TestTheDecisionIsRecorded:
    """One column, written at ingest by the poller that already computed the
    answer. Everything below is a reader of it."""

    def test_a_self_reply_is_marked_withheld(self, db_path, config):
        task = _self_reply(config, db_path)

        assert task.withheld_from_room is True
        # And the inheritance it exists to compensate for is still in place.
        assert task.conversation_token == ROOM

    def test_an_external_reply_is_not(self, db_path, config):
        task = _external_reply(config, db_path)

        assert task.withheld_from_room is False

    def test_a_self_addressed_first_contact_is_not(self, db_path, config):
        """The scope boundary ISSUE-254 drew. A first-contact self-addressed mail
        keeps its `room:<tok>,email` plan and its mirror — one message pair with
        no quoted chain, in the room the user's own routing chose — so the room
        genuinely does hold this exchange and no consumer should skip it."""
        config.users[USER].alerts_channel = ROOM
        with db.get_db(db_path) as conn:
            _origin_room(conn)
        # No `sent_emails` row: nothing to thread against.
        task = _poll_reply(config, sender=USER_ADDR)

        assert task.output_target == f"room:{ROOM},email"
        assert task.withheld_from_room is False

    def test_a_gated_turn_is_not_marked_withheld(self, db_path, config):
        """The two flags are different questions and must not collapse. The
        untrusted-sender gate withholds a turn that *does* belong in the room,
        and `confirmations.approve` publishes it once answered — so a gated
        external turn is not withheld in this column's sense, and the room's
        context must keep counting it once it lands."""
        config.email.confirm_sender_match = True
        with db.get_db(db_path) as conn:
            _origin_room(conn)
            _sent_from_the_room(conn)
        task = _poll_reply(
            config, sender=EXTERNAL_ADDR, to=("bot+carol@test.com",),
        )

        assert task.status == "pending_confirmation"
        assert task.withheld_from_room is False


# ---------------------------------------------------------------------------
# Consumer 1 — the history fallback
# ---------------------------------------------------------------------------


class TestTheHistoryFallback:
    """`get_conversation_history` serves from `messages` only when the store is
    complete for the room, which needs a completed talk/web task still in
    `tasks`. A mail-only room has none, and so does a room whose last chat turn
    aged past `task_retention_days` — both fall to a straight
    `SELECT … WHERE conversation_token = ?`. That is the path still charging the
    context bill ISSUE-254 was filed about."""

    def _completed(self, config, db_path, task):
        with db.get_db(db_path) as conn:
            db.update_task_status(conn, task.id, "completed", result="42.")

    def _history(self, db_path):
        with db.get_db(db_path) as conn:
            return db.get_conversation_history(
                conn, ROOM,
                exclude_source_types=["scheduled", "briefing", "subtask", "heartbeat"],
            )

    def test_a_self_reply_is_no_longer_served(self, db_path, config):
        task = _self_reply(config, db_path)
        self._completed(config, db_path, task)

        with db.get_db(db_path) as conn:
            # Still the fallback path — this room has no conversational task.
            assert db._messages_caught_up(conn, ROOM) is False
        assert self._history(db_path) == []

    def test_an_external_reply_still_is(self, db_path, config):
        """The guard. The room is where the user learns this arrived, so its
        history must keep the turn."""
        task = _external_reply(config, db_path)
        self._completed(config, db_path, task)

        assert [h.source_type for h in self._history(db_path)] == ["email"]

    def test_the_re_surfacing_reader_drops_it_too(self, db_path, config):
        """`get_previous_tasks` is the wider of the two history leaks, not the
        narrower. `executor._build_db_context` runs it on **every** task in the
        room with no `_messages_caught_up` gate above it, so without the filter a
        withheld exchange reached LLM context even for a room whose history reads
        cleanly from `messages`."""
        task = _self_reply(config, db_path)
        self._completed(config, db_path, task)

        with db.get_db(db_path) as conn:
            assert db.get_previous_tasks(conn, ROOM) == []

    def test_the_re_surfacing_reader_keeps_an_external_reply(self, db_path, config):
        task = _external_reply(config, db_path)
        self._completed(config, db_path, task)

        with db.get_db(db_path) as conn:
            assert [t.id for t in db.get_previous_tasks(conn, ROOM)] == [task.id]

    def test_a_thread_with_no_room_keeps_its_own_history(self, db_path, config):
        """The boundary the column has to respect, and the one place a naive
        `not mirror_to_room` gets it wrong.

        The poller suppresses the mirror for *every* self-addressed thread reply,
        including a genuine email-only thread whose `conversation_token` is a
        synthetic hash naming no room. There is no room to be withheld from
        there — and no `messages` store to fall back to either, so this fallback
        is the only history that thread has. Flagging it would make a plain
        multi-turn email conversation with the bot forget its own previous
        turns."""
        with db.get_db(db_path) as conn:
            db.record_sent_email(
                conn, user_id=USER, message_id=ORIGIN_MESSAGE_ID,
                to_addr=USER_ADDR, subject="Question",
                conversation_token=THREAD_HASH,
                origin_target=None,
            )
        task = _poll_reply(config, sender=USER_ADDR)
        assert task.conversation_token == THREAD_HASH
        assert task.withheld_from_room is False
        self._completed(config, db_path, task)

        with db.get_db(db_path) as conn:
            assert db.get_conversation_history(conn, task.conversation_token) != []
            assert db.get_previous_tasks(conn, task.conversation_token) != []

    def test_the_quoted_chain_is_what_was_costing(self, db_path, config):
        """Stated concretely rather than by source_type alone: the prompt the
        fallback used to hand back carries the whole quoted thread, which is the
        quadratic cost the entry was about."""
        with db.get_db(db_path) as conn:
            _origin_room(conn)
            _sent_from_the_room(conn, to_addr=USER_ADDR)
        task = _poll_reply(
            config, sender=USER_ADDR,
            body="Short answer.\n\n> a very long quoted prior thread",
        )
        self._completed(config, db_path, task)

        assert not any(
            "quoted prior thread" in (h.prompt or "")
            for h in self._history(db_path)
        )


# ---------------------------------------------------------------------------
# Consumer 2 — the room's memory namespace
# ---------------------------------------------------------------------------


def _channel_chunks(db_path):
    with db.get_db(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM memory_chunks WHERE user_id = ?",
            (f"channel:{ROOM}",),
        ).fetchone()[0]


def _user_chunks(db_path):
    with db.get_db(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM memory_chunks WHERE user_id = ?", (USER,),
        ).fetchone()[0]


class TestTheRoomsMemoryNamespace:
    """Even where the transcript is clean, recall was not: the scheduler indexes
    a completed turn under `channel:{conversation_token}`, and
    `executor._recall_memories` serves that namespace back to every later task in
    the room."""

    @pytest.fixture(autouse=True)
    def _indexing_on(self, config):
        config.memory_search = MemorySearchConfig(
            enabled=True, auto_index_conversations=True,
        )

    @patch("istota.scheduler.post_result_to_email", return_value=True)
    @patch("istota.scheduler.run_coro", return_value=True)
    def test_a_self_reply_is_kept_out_of_it(
        self, mock_run_coro, mock_post_email, db_path, config,
    ):
        _self_reply(config, db_path)

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "Here is the number.", None, None),
        ):
            process_one_task(config)

        assert _channel_chunks(db_path) == 0

    @patch("istota.scheduler.post_result_to_email", return_value=True)
    @patch("istota.scheduler.run_coro", return_value=True)
    def test_but_the_users_own_memory_still_has_it(
        self, mock_run_coro, mock_post_email, db_path, config,
    ):
        """Only the *room's* namespace is wrong. The exchange is the user's own
        — they wrote it and the bot answered them — so their personal memory is
        exactly where it belongs, and dropping that too would lose a real
        conversation from recall."""
        _self_reply(config, db_path)

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "Here is the number.", None, None),
        ):
            process_one_task(config)

        assert _user_chunks(db_path) > 0

    @patch("istota.scheduler.post_result_to_email", return_value=True)
    @patch("istota.scheduler.post_result_to_talk")
    @patch("istota.scheduler.run_coro", return_value=414)
    def test_an_external_reply_still_reaches_it(
        self, mock_run_coro, mock_post_talk, mock_post_email, db_path, config,
    ):
        _external_reply(config, db_path)

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "They said yes.", None, None),
        ):
            process_one_task(config)

        assert _channel_chunks(db_path) > 0


# ---------------------------------------------------------------------------
# Consumer 3 — the channel sleep cycle
# ---------------------------------------------------------------------------


class TestTheChannelSleepCycle:
    """`check_channel_sleep_cycles` collects a channel's completed tasks by the
    same column and distils them into `CHANNEL.md`, which is durable and reaches
    every later prompt in the room."""

    def _completed(self, db_path, task):
        with db.get_db(db_path) as conn:
            db.update_task_status(conn, task.id, "completed", result="42.")

    def test_a_self_reply_is_not_collected(self, db_path, config):
        task = _self_reply(config, db_path)
        self._completed(db_path, task)

        with db.get_db(db_path) as conn:
            collected = db.get_completed_channel_tasks_since(
                conn, ROOM, "2000-01-01T00:00:00",
            )
        assert collected == []

    def test_nor_does_it_make_the_room_look_active(self, db_path, config):
        """The discovery half. A room whose only recent traffic was withheld is
        not an active channel — collecting it would run a distillation pass over
        nothing, every cycle, for as long as the mail keeps coming."""
        task = _self_reply(config, db_path)
        self._completed(db_path, task)

        with db.get_db(db_path) as conn:
            assert ROOM not in db.get_active_channel_tokens(
                conn, "2000-01-01T00:00:00",
            )

    def test_an_external_reply_still_is_collected(self, db_path, config):
        task = _external_reply(config, db_path)
        self._completed(db_path, task)

        with db.get_db(db_path) as conn:
            collected = db.get_completed_channel_tasks_since(
                conn, ROOM, "2000-01-01T00:00:00",
            )
            assert ROOM in db.get_active_channel_tokens(
                conn, "2000-01-01T00:00:00",
            )
        assert [t.id for t in collected] == [task.id]


# ---------------------------------------------------------------------------
# Consumer 4 — a permanent failure has nowhere to go
# ---------------------------------------------------------------------------


class TestAPermanentFailureReachesTheUser:
    """`process_one_task` delivers a user-facing error only under `plan_talk and
    talk_token`, beside a comment recording a deliberate decision never to email
    errors. With no room leg there is no Talk leg either, so the user mails the
    bot, the task fails, and nothing tells them — no room notice, no mail, no
    trace anywhere they look.

    Not a new failure: any `email_reply_routing = "thread"` user already had it.
    What changed is that an email-only plan is now the *default* outcome for a
    self-reply rather than a setting somebody chose."""

    def _fail_permanently(self, config, db_path, task):
        with db.get_db(db_path) as conn:
            db.update_task_status(conn, task.id, "running")
            conn.execute(
                "UPDATE tasks SET attempt_count = max_attempts WHERE id = ?",
                (task.id,),
            )
        with (
            patch(
                "istota.scheduler.execute_task",
                return_value=(False, "Brain exploded", None, None),
            ),
            patch("istota.scheduler.run_coro", return_value=None),
            patch("istota.scheduler.post_result_to_email", return_value=True),
            patch("istota.scheduler.send_notification") as notify,
        ):
            with db.get_db(db_path) as conn:
                db.update_task_status(conn, task.id, "pending")
                conn.execute(
                    "UPDATE tasks SET attempt_count = max_attempts - 1 WHERE id = ?",
                    (task.id,),
                )
            process_one_task(config)
        return notify

    def test_the_user_is_told(self, db_path, config):
        task = _self_reply(config, db_path)

        notify = self._fail_permanently(config, db_path, task)

        alerts = [
            c for c in notify.call_args_list
            if c.kwargs.get("purpose") == "alert"
        ]
        assert alerts, "a failed self-reply told the user nothing"
        assert str(task.id) in alerts[0].args[2]

    def test_an_email_only_plan_that_is_not_withheld_stays_silent(
        self, db_path, config,
    ):
        """The scoping guard, and the reason this cannot simply be switched on
        for every email-only plan.

        An external correspondent's thread reply under `email_reply_routing =
        "thread"` has exactly the same email-only plan and exactly the same
        absent error channel — it is the population that already had this
        failure before ISSUE-254 widened it. Gating on the recorded fact rather
        than on the shape of the plan is what keeps it silent, so this is the
        test that fails if the gate is widened to `not plan_talk`."""
        config.users[USER].email_reply_routing = "thread"
        task = _external_reply(config, db_path)
        assert task.output_target == "email"
        assert task.withheld_from_room is False

        notify = self._fail_permanently(config, db_path, task)

        assert [
            c for c in notify.call_args_list
            if c.kwargs.get("purpose") == "alert"
        ] == []

    def test_a_cron_is_suppressed_earlier_still(self, db_path, config):
        """Not the scoping guard — a scheduled job never reaches the new branch
        at all, because `source_type in ("briefing", "scheduled")` intercepts
        above it and suppresses user-facing error delivery for automated tasks.
        Recorded so the ordering is deliberate: were that branch ever removed,
        the fact-based gate below it is what would still have to keep a cron
        mailing a report to an external address quiet."""
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Send the weekly report.", user_id=USER,
                source_type="scheduled", output_target="email",
            )
            task = db.get_task(conn, task_id)
        assert task.withheld_from_room is False

        notify = self._fail_permanently(config, db_path, task)

        assert [
            c for c in notify.call_args_list
            if c.kwargs.get("purpose") == "alert"
        ] == []


# ---------------------------------------------------------------------------
# The fact has to survive the two paths that copy a task
# ---------------------------------------------------------------------------


class TestTheFactIsCarriedForward:
    """Both paths inherit `conversation_token` from a task they copy, so a copy
    that drops the column re-enters every reader above under the origin room."""

    def _completed(self, db_path, task_id):
        with db.get_db(db_path) as conn:
            db.update_task_status(conn, task_id, "completed", result="42.")

    def test_a_retry_stays_withheld(self, db_path, config):
        """Reachability is *raised* by this issue, not lowered: the new
        permanent-failure alert is what now tells the user their mailed request
        failed, and `!retry` is what they reach for next. A bare `!retry` typed
        in the origin room can land on the withheld task, since the retry target
        is the newest failed task for that token."""
        from istota.commands import _create_retry_task

        task = _self_reply(config, db_path)
        with db.get_db(db_path) as conn:
            db.update_task_status(conn, task.id, "failed", error="boom")
            retry_id = _create_retry_task(conn, task, task.prompt)
            retry = db.get_task(conn, retry_id)

        assert retry.conversation_token == ROOM
        assert retry.withheld_from_room is True

        self._completed(db_path, retry_id)
        with db.get_db(db_path) as conn:
            assert db.get_conversation_history(conn, ROOM) == []

    def test_a_deferred_subtask_stays_withheld(self, db_path, config):
        """A subtask's token is pinned to its parent's so deferred JSON cannot
        drive routing; the flag is pinned with it for the same reason. Without it
        the subtask's prompt and result are indexed under the origin room's
        memory namespace and collected into that room's sleep cycle.

        Driven through the real deferred handler rather than by passing the flag
        in — the handler is the thing that has to carry it."""
        import json as _json

        from istota.scheduler_deferred import _process_deferred_subtasks

        task = _self_reply(config, db_path)
        deferred_dir = tmp_deferred_dir(config, task)
        (deferred_dir / f"task_{task.id}_subtasks.json").write_text(
            _json.dumps([{"prompt": "follow up"}]), encoding="utf-8",
        )

        created = _process_deferred_subtasks(config, task, deferred_dir)
        assert created == 1

        with db.get_db(db_path) as conn:
            sub = [
                t for t in db.list_tasks(conn, user_id=USER)
                if t.source_type == "subtask"
            ][0]
        assert sub.conversation_token == ROOM
        assert sub.withheld_from_room is True

        self._completed(db_path, sub.id)
        with db.get_db(db_path) as conn:
            assert db.get_completed_channel_tasks_since(
                conn, ROOM, "2000-01-01T00:00:00",
            ) == []


# ---------------------------------------------------------------------------
# Consumer 5 — a delivery failure loses the answer
# ---------------------------------------------------------------------------


class TestADeliveryFailureKeepsTheAnswer:
    """When email is the only destination and the send fails, the task is
    flipped to `failed` and the composed answer survives only in `tasks.result`.
    Before ISSUE-254 the Talk leg had already posted it and the assistant row was
    stored, so the answer was recoverable by reading the room."""

    def _run_with_failing_send(self, config, db_path):
        with (
            patch(
                "istota.scheduler.execute_task",
                return_value=(True, "The number is 42.", None, None),
            ),
            patch("istota.scheduler.post_result_to_email", return_value=False),
            patch("istota.scheduler.post_result_to_talk"),
            patch("istota.scheduler.run_coro", return_value=False),
            patch("istota.scheduler.send_notification") as notify,
        ):
            process_one_task(config)
        return notify

    def test_the_answer_is_delivered_another_way(self, db_path, config):
        _self_reply(config, db_path)

        notify = self._run_with_failing_send(config, db_path)

        alerts = [
            c for c in notify.call_args_list
            if c.kwargs.get("purpose") == "alert"
        ]
        assert alerts, "the only copy of the answer was left in tasks.result"
        assert "The number is 42." in alerts[0].args[2]

    def test_a_structured_result_is_unwrapped_first(self, db_path, config):
        """An email task's `result` may *be* the `{"subject","body","format"}`
        envelope the send path parses, so a notice promising the answer has to
        unwrap it exactly as the room transcript does (ISSUE-247). Handing over
        the raw envelope would deliver a JSON blob under the words "the answer is
        below"."""
        import json as _json

        _self_reply(config, db_path)
        envelope = _json.dumps({
            "subject": "Re: Question", "body": "The number is 42.",
            "format": "plain",
        })

        with (
            patch(
                "istota.scheduler.execute_task",
                return_value=(True, envelope, None, None),
            ),
            patch("istota.scheduler.post_result_to_email", return_value=False),
            patch("istota.scheduler.post_result_to_talk"),
            patch("istota.scheduler.run_coro", return_value=False),
            patch("istota.scheduler.send_notification") as notify,
        ):
            process_one_task(config)

        body = [
            c for c in notify.call_args_list
            if c.kwargs.get("purpose") == "alert"
        ][0].args[2]
        assert "The number is 42." in body
        assert '"subject"' not in body

    def test_an_external_thread_is_left_alone(self, db_path, config):
        """The guard. An emissary reply keeps its room leg, so a failed send
        already leaves the answer in the room transcript — raising a second
        notice would be duplicate noise, and the answer was never lost."""
        _external_reply(config, db_path)

        notify = self._run_with_failing_send(config, db_path)

        assert [
            c for c in notify.call_args_list
            if c.kwargs.get("purpose") == "alert"
        ] == []
