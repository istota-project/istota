"""ISSUE-254 — a thread reply the user sends from their own address stays out of
the origin room.

The origin mirror ISSUE-247 built for emissary replies keyed on the thread, not
on who wrote it, so it applied to the user's own replies too. A private email
exchange between the user and the bot was therefore copied into the room the
original send came from, quoted chain and all — and since each reply quotes the
whole prior thread, the N-th message wrote roughly N copies of the conversation
into a transcript that is then the LLM context for every later task in that
room.

Two legs key on the same room token, so suppressing either alone changes
nothing: the delivery plan (`output_target` names the origin room) and the
transcript mirror (`record_inbound` stores the question there because the
task inherited the room as its `conversation_token`). Both are decided from one
answer — the envelope sender is one of the routed user's own addresses — and the
answer side then no-ops by construction, since it needs either a delivery into
the room or a question already in it.

The regression guard that matters is the *external* correspondent: their reply
must keep mirroring, because the room copy is the only way the user learns it
arrived. Driven through `poll_emails` → `process_one_task` rather than against
the helpers, because both legs live in the wiring between them.
"""

from unittest.mock import patch

import pytest

from istota import db
from istota.config import Config, EmailConfig, UserConfig
from istota.scheduler import process_one_task
from istota.skills.email import Email, EmailEnvelope
from istota.transport.email.inbound import poll_emails

ROOM = "rm_web123"
USER = "carol"
USER_ADDR = "carol@test.com"
EXTERNAL_ADDR = "ext@x.com"
ORIGIN_MESSAGE_ID = "<origin_out@bot.com>"


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
    """The web room the original send went out from, bound on both surfaces so
    the `room:` descriptor has something to expand to."""
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
    """Poll one thread-matched reply and return the created task."""
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


def _room_rows(db_path, role=None):
    with db.get_db(db_path) as conn:
        return [
            (m.role, m.body) for m in db.get_messages(conn, ROOM)
            if role is None or m.role == role
        ]


# ---------------------------------------------------------------------------
# The user's own reply — both legs
# ---------------------------------------------------------------------------


class TestSelfAddressedThreadReply:
    def test_the_plan_keeps_no_room_leg(self, db_path, config):
        """Leg 1. The reply is delivered by mail alone: the user is demonstrably
        on the email surface, so the room copy only duplicates the exchange."""
        with db.get_db(db_path) as conn:
            _origin_room(conn)
            _sent_from_the_room(conn, to_addr=USER_ADDR)

        task = _poll_reply(config, sender=USER_ADDR)

        assert task.output_target == "email"
        # The origin is still *recovered* — the reply continues that
        # conversation's context, it just is not delivered back into it.
        assert task.conversation_token == ROOM

    def test_the_question_is_not_mirrored_into_the_room(self, db_path, config):
        """Leg 2. Suppressing the delivery leg alone changes nothing: the task
        inherits the room as its token, so rung 1 of `transcript_room` stores the
        question there anyway."""
        with db.get_db(db_path) as conn:
            _origin_room(conn)
            _sent_from_the_room(conn, to_addr=USER_ADDR)

        _poll_reply(config, sender=USER_ADDR)

        assert _room_rows(db_path) == []

    def test_a_policy_of_origin_only_still_delivers_to_email(self, db_path, config):
        """`origin` names the room and nothing else, so dropping the origin leg
        would leave an empty plan. The reply must not be lost — the user asked
        for it and it reaches them where they wrote from."""
        config.users[USER].email_reply_routing = "origin"
        with db.get_db(db_path) as conn:
            _origin_room(conn)
            _sent_from_the_room(conn, to_addr=USER_ADDR)

        task = _poll_reply(config, sender=USER_ADDR)

        assert task.output_target == "email"

    def test_a_legacy_null_origin_row_is_covered_too(self, db_path, config):
        """`origin_descriptor` returns None for a send with no deliverable
        origin, and that branch hardcodes `talk,email` — the same duplication on
        a path the entry did not name. It is live for new rows, not only
        pre-migration ones."""
        config.users[USER].alerts_channel = ROOM
        with db.get_db(db_path) as conn:
            _origin_room(conn)
            _sent_from_the_room(conn, to_addr=USER_ADDR, origin_target=None)

        task = _poll_reply(config, sender=USER_ADDR)

        assert task.output_target == "email"
        assert _room_rows(db_path) == []

    def test_the_threads_talk_room_survives_the_suppression(self, db_path, config):
        """A per-message decision must not have a per-thread side effect.

        `talk_delivery_token` is the one thing that can name a Talk room the
        registry never heard of (rung 0, ISSUE-057), and the bot's own reply
        copies it onto the next `sent_emails` row. Clearing it on a self-reply
        would lose the room for every later message in the thread — including an
        external correspondent's, whose mirror this fix leaves alone. So the
        legacy ladder still runs; only the plan changes."""
        with db.get_db(db_path) as conn:
            _origin_room(conn)
            db.record_sent_email(
                conn, user_id=USER, message_id=ORIGIN_MESSAGE_ID,
                to_addr=USER_ADDR, subject="Question",
                conversation_token="deadbeefdeadbeef",  # synthetic thread hash
                talk_delivery_token="legacy_talk_room",
                origin_target=None,
            )

        task = _poll_reply(config, sender=USER_ADDR)

        assert task.output_target == "email"
        assert task.talk_delivery_token == "legacy_talk_room"

    @patch("istota.scheduler.post_result_to_email", return_value=True)
    @patch("istota.scheduler.run_coro", return_value=True)
    def test_the_answer_side_no_ops(
        self, mock_run_coro, mock_post_email, db_path, config,
    ):
        """No third change is needed. `_room_turn_belongs_here` stores the answer
        when the plan delivers into the room *or* the room holds the question;
        with both legs fixed, neither holds."""
        with db.get_db(db_path) as conn:
            _origin_room(conn)
            _sent_from_the_room(conn, to_addr=USER_ADDR)
        _poll_reply(config, sender=USER_ADDR)

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "Here is the number.", None, None),
        ):
            process_one_task(config)

        assert _room_rows(db_path) == []

    @patch("istota.scheduler.post_result_to_email", return_value=True)
    @patch("istota.scheduler.run_coro", return_value=True)
    def test_a_question_back_is_mailed_rather_than_parked(
        self, mock_run_coro, mock_post_email, db_path, config,
    ):
        """A consequence of dropping the room leg, and a decision rather than an
        accident: without it the task is no longer a `_confirmable_surface`
        (`scheduler.py`), so an answer shaped like a question completes and is
        mailed instead of parking.

        That is the right outcome *here* and only here. The rule it appears to
        break — an email task parks and asks in the room — exists because the
        room leg is the only surface that can carry the question, the email leg
        going to an external correspondent (see `process_one_task`'s
        `is_confirmation_request` branch). On a self-reply the email leg goes to
        the user themselves, so the question reaches exactly the person who has
        to answer it, on the surface they are already reading. Parking instead
        would deliver the question nowhere and let the task die at
        `expire_stale_confirmations` two hours later, which is the failure that
        comment records having fixed.

        The cost, named: deferred ops a park would have held until the answer
        now apply on completion. The outbound email gate is unaffected — it runs
        on the delivery leg and still holds mail to an unapproved recipient."""
        with db.get_db(db_path) as conn:
            _origin_room(conn)
            _sent_from_the_room(conn, to_addr=USER_ADDR)
        task = _poll_reply(config, sender=USER_ADDR)

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "I drafted it. Should I proceed?", None, None),
        ):
            process_one_task(config)

        with db.get_db(db_path) as conn:
            after = db.get_task(conn, task.id)
        assert after.status == "completed"
        # And the question went out by mail, to the address it came from.
        assert mock_post_email.called

    @patch("istota.scheduler.post_result_to_email", return_value=True)
    @patch("istota.scheduler.post_result_to_talk")
    @patch("istota.scheduler.run_coro", return_value=414)
    def test_an_external_thread_still_parks_on_a_question_back(
        self, mock_run_coro, mock_post_talk, mock_post_email, db_path, config,
    ):
        """The guard for the test above. An emissary reply keeps its room leg, so
        it keeps parking — the bot must not mail "should I proceed?" to the
        correspondent and take their answer as the user's."""
        with db.get_db(db_path) as conn:
            _origin_room(conn)
            _sent_from_the_room(conn)
        task = _poll_reply(config, sender=EXTERNAL_ADDR)

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "I drafted it. Should I proceed?", None, None),
        ):
            process_one_task(config)

        with db.get_db(db_path) as conn:
            after = db.get_task(conn, task.id)
        assert after.status == "pending_confirmation"


# ---------------------------------------------------------------------------
# The regression guard: everybody else keeps their mirror
# ---------------------------------------------------------------------------


class TestEveryoneElseKeepsTheMirror:
    def test_an_external_correspondent_still_reaches_the_room(
        self, db_path, config,
    ):
        """The case the mirror exists for. The user is not in this thread, so the
        room copy is the only way they learn the reply arrived."""
        with db.get_db(db_path) as conn:
            _origin_room(conn)
            _sent_from_the_room(conn)

        task = _poll_reply(config, sender=EXTERNAL_ADDR)

        assert task.output_target == f"room:{ROOM},email"
        assert [r for r, _ in _room_rows(db_path)] == ["user"]

    def test_a_plus_address_reply_from_a_third_party_is_not_the_user(
        self, db_path, config,
    ):
        """`not is_emissary_reply` would have been the wrong predicate: it is
        false for a plus-address route too, which is a third party writing to
        `bot+<user>@`, not the user.

        Its mirror is withheld here, but by the *untrusted-sender gate* and only
        until the user answers — a different mechanism with a restore path, which
        `TestApprovalDoesNotRestoreIt` drives to completion. What matters is that
        the plan keeps its room leg, which a self-reply's would not."""
        with db.get_db(db_path) as conn:
            _origin_room(conn)
            _sent_from_the_room(conn)

        task = _poll_reply(
            config, sender=EXTERNAL_ADDR, to=("bot+carol@test.com",),
        )

        assert task.output_target == f"room:{ROOM},email"
        assert task.status == "pending_confirmation"

    @patch("istota.scheduler.post_result_to_email", return_value=True)
    @patch("istota.scheduler.post_result_to_talk")
    @patch("istota.scheduler.run_coro", return_value=414)
    def test_the_external_exchange_keeps_both_halves(
        self, mock_run_coro, mock_post_talk, mock_post_email, db_path, config,
    ):
        with db.get_db(db_path) as conn:
            _origin_room(conn)
            _sent_from_the_room(conn)
        _poll_reply(config, sender=EXTERNAL_ADDR)

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "They said yes.", None, None),
        ):
            process_one_task(config)

        assert [r for r, _ in _room_rows(db_path)] == ["user", "assistant"]

    def test_a_reply_to_another_user_is_judged_against_that_user(
        self, db_path, config,
    ):
        """`claims_to_be_user` is checked against the *routed* user's addresses.
        Alice's address is not carol's, so alice replying into carol's thread is
        an external sender here — which is also why the payload-vs-identity
        guard above it drops the mismatched origin."""
        config.users["alice"] = UserConfig(email_addresses=["alice@test.com"])
        with db.get_db(db_path) as conn:
            _origin_room(conn)
            _sent_from_the_room(conn)

        task = _poll_reply(config, sender="alice@test.com")

        assert task.user_id == "alice"
        # The origin belonged to carol, so it is dropped outright — the reply
        # never reaches carol's room by any leg.
        assert task.output_target is None
        assert _room_rows(db_path) == []


# ---------------------------------------------------------------------------
# What the suppression does *not* reach
# ---------------------------------------------------------------------------


class TestTheResidueKeyedOnConversationToken:
    """The task still carries the origin room as its `conversation_token`, so
    everything keyed on that column rather than on the transcript once saw this
    exchange anyway.

    **Closed by ISSUE-255**, which took the option this class's original note
    called the narrower one: the decision is recorded on the task
    (`tasks.withheld_from_room`) and each consumer reads it, rather than the
    inheritance being dropped — which would have moved history, the per-channel
    active-task gate, memory recall and the sleep cycle at once, and is a
    different question from "does the room show this". The inheritance therefore
    stays, and the assertions below now pin both halves as closed.
    `tests/test_email_self_reply_residue.py` covers the other four consumers."""

    def test_the_room_transcript_is_clean(self, db_path, config):
        """The half that *is* fixed, stated next to the half that is not: the
        canonical store holds nothing, so a room whose history reads from
        `messages` pays nothing for this exchange."""
        with db.get_db(db_path) as conn:
            _origin_room(conn)
            _sent_from_the_room(conn, to_addr=USER_ADDR)
        task = _poll_reply(config, sender=USER_ADDR)
        with db.get_db(db_path) as conn:
            db.update_task_status(conn, task.id, "completed", result="42.")

        with db.get_db(db_path) as conn:
            assert db.get_messages(conn, ROOM) == []

    def test_and_the_tasks_fallback_reader_no_longer_sees_it_either(
        self, db_path, config,
    ):
        """`get_conversation_history` serves from `messages` only when
        `_messages_caught_up` says the store is complete for the room, which
        needs a completed talk/web task still in `tasks`. A room with none — one
        used only by mail, or one whose last chat turn aged past
        `task_retention_days` — falls back to selecting straight from `tasks
        WHERE conversation_token = ?`, where this task is still keyed.

        That fallback used to serve the turn, which is why this assertion was
        written as one rather than as a comment. ISSUE-255 closed it: the
        fallback now excludes `withheld_from_room`, so the context cost is gone
        on both paths. The room is still on the fallback — that half is
        unchanged and asserted, because the fix must work *there*, not by moving
        the room onto the `messages` path."""
        with db.get_db(db_path) as conn:
            _origin_room(conn)
            _sent_from_the_room(conn, to_addr=USER_ADDR)
        task = _poll_reply(config, sender=USER_ADDR)
        with db.get_db(db_path) as conn:
            db.update_task_status(conn, task.id, "completed", result="42.")

        with db.get_db(db_path) as conn:
            assert db._messages_caught_up(conn, ROOM) is False
            history = db.get_conversation_history(
                conn, ROOM,
                exclude_source_types=["scheduled", "briefing", "subtask", "heartbeat"],
            )
        assert history == []


# ---------------------------------------------------------------------------
# The approval path must not resurrect the suppressed copy
# ---------------------------------------------------------------------------


class TestApprovalDoesNotRestoreIt:
    """`confirmations.approve` re-publishes a turn the untrusted-sender gate
    withheld. That flag means "not yet", while this suppression means "never" —
    so the restore has to tell the two apart or approving hands back exactly the
    copy the fix removed. Reachable only under `confirm_sender_match`, which
    stops the own-address claim from counting as trust."""

    def _gated_self_reply(self, db_path, config):
        config.email.confirm_sender_match = True
        with db.get_db(db_path) as conn:
            _origin_room(conn)
            _sent_from_the_room(conn, to_addr=USER_ADDR)
        task = _poll_reply(config, sender=USER_ADDR)
        assert task.status == "pending_confirmation"
        return task

    def test_approving_a_self_reply_writes_no_room_row(self, db_path, config):
        from istota import confirmations

        task = self._gated_self_reply(db_path, config)

        with db.get_db(db_path) as conn:
            confirmations.approve(conn, task, config=config)

        assert _room_rows(db_path) == []

    def test_approving_a_self_addressed_first_contact_still_restores_it(
        self, db_path, config,
    ):
        """The scope boundary, which is what `_room_holds_no_copy_of_this_exchange`
        has to keep getting right.

        A first-contact self-addressed mail carries no thread to suppress, so it
        gets the `room:<tok>,email` routing ISSUE-247 gave it and keeps its
        mirror — the sender is the user, and the room still holds the exchange.
        It used to be the case that separated the two halves of a reconstruction;
        since ISSUE-255 it is the case that must leave `withheld_from_room` False,
        which is asserted directly in
        `tests/test_email_self_reply_residue.py::TestTheDecisionIsRecorded`. Kept
        here as the end-to-end half: the row must actually reappear on approval."""
        from istota import confirmations

        config.email.confirm_sender_match = True
        config.users[USER].alerts_channel = ROOM
        with db.get_db(db_path) as conn:
            _origin_room(conn)
        # No `sent_emails` row: nothing to thread against, so this is first
        # contact routed by the user's own address.
        task = _poll_reply(config, sender=USER_ADDR)
        assert task.status == "pending_confirmation"
        assert task.output_target == f"room:{ROOM},email"
        assert _room_rows(db_path) == []

        with db.get_db(db_path) as conn:
            confirmations.approve(conn, task, config=config)

        assert [r for r, _ in _room_rows(db_path)] == ["user"]

    def test_approving_an_external_reply_still_restores_its_question(
        self, db_path, config,
    ):
        """The restore's own regression guard. A gated *external* sender's turn
        is withheld until answered and published on approval, unchanged."""
        from istota import confirmations

        config.email.confirm_sender_match = True
        with db.get_db(db_path) as conn:
            _origin_room(conn)
            _sent_from_the_room(conn)
        # Plus-addressed so the gate applies: a bare thread match is never gated.
        task = _poll_reply(
            config, sender=EXTERNAL_ADDR, to=("bot+carol@test.com",),
        )
        assert task.status == "pending_confirmation"
        assert _room_rows(db_path) == []

        with db.get_db(db_path) as conn:
            confirmations.approve(conn, task, config=config)

        assert [r for r, _ in _room_rows(db_path)] == ["user"]
