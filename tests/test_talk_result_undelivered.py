"""ISSUE-404 — a Talk result post that never landed left no trace but a log line.

`TalkTransport.deliver` catches every exception, logs once and returns None.
That contract is right — a Nextcloud outage must not turn a successful task into
a failed one — but the value it returns to say so was read by nothing. In
`process_one_task` the result of the post lands in `response_msg_id`, and every
site below it is written `if response_msg_id:` with no `else`: the web mirror,
the `talk_response_id` write, the `external_ids` stamp, the result cache. So a
934-character answer, generated and paid for, was delivered nowhere and the task
was marked completed.

The email leg has carried the whole recovery for exactly this case since
ISSUE-255: an inbox row through `_write_undelivered_row` plus a `failure_alert`
carrying the body, so the answer survives the failure. None of it was on a Talk
path.

**Two things about how this file drives the failure are load-bearing.**

The Talk post fails through the double's `send_failures`, not through an unknown
token. Both end at the same `except Exception` in `deliver`, but they are not the
same test: an unknown token drives the *misroute* path, where the binding lookup
and `_talk_lands_here` answer differently and the room the answer was for does
not exist. What production hit was a `ReadTimeout` on a room that was fine, and
that is the shape the fix keys on.

And nothing here asserts that no exception was raised, which would assert
nothing at all against a product that swallows. Every case names a row in
`notifications`, an argument to `send_notification`, or the `tasks.status`
column — and the landed-post control in each class is what shows the assertion
can go the other way.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import pytest

from istota import db, notification_sources as sources
from istota.config import (
    Config,
    EmailConfig,
    NextcloudConfig,
    SchedulerConfig,
    TalkConfig,
    UserConfig,
)
from istota.notification_resolvers import task_alert
from istota.scheduler import process_one_task

from .support.rooms import plain_talk_room, promoted_room

USER = "testuser"
ANSWER = "The invoice total is 1,240 EUR."


@pytest.fixture(autouse=True)
def _registry():
    sources.reset_registry()
    yield
    sources.reset_registry()


@pytest.fixture
def config(tmp_path, db_path):
    # `db_path` rather than a database of this file's own: `fake_talk` resolves
    # bindings against that fixture, and a second path leaves the double reading
    # an empty `room_bindings` and refusing every token here.
    mount = tmp_path / "mount"
    mount.mkdir(exist_ok=True)
    cfg = Config(
        db_path=db_path,
        nextcloud=NextcloudConfig(
            url="https://nc.example.com", username="istota", app_password="s",
        ),
        talk=TalkConfig(enabled=True, bot_username="istota"),
        email=EmailConfig(enabled=False),
        scheduler=SchedulerConfig(),
        nextcloud_mount_path=mount,
        temp_dir=tmp_path / "temp",
        users={USER: UserConfig(display_name="Alice", alerts_channel="alerts")},
    )
    cfg.temp_dir.mkdir(exist_ok=True)
    return cfg


@pytest.fixture
def plain(config):
    with db.get_db(config.db_path) as conn:
        return plain_talk_room(conn, USER)


@pytest.fixture
def promoted(config):
    with db.get_db(config.db_path) as conn:
        shape = promoted_room(conn, USER)
        # The divergence every mirror assertion here rests on: the canonical
        # token is bound on no `talk` row, so the two legs address two strings.
        assert db.resolve_room_token(conn, "talk", shape.canonical) is None
    return shape


def _timeout(fake_talk, token):
    """What production saw: a room that resolved, and a read that timed out."""
    fake_talk.send_failures[token] = httpx.ReadTimeout("")


def _rows(config):
    with db.get_db(config.db_path) as conn:
        return conn.execute(
            "SELECT * FROM notifications WHERE source = 'task_alert' "
            "AND dedup_key LIKE 'undelivered:%' ORDER BY id",
        ).fetchall()


def _status(config, task_id):
    with db.get_db(config.db_path) as conn:
        return db.get_task(conn, task_id).status


def _result_landed(fake_talk, task_id):
    """Whether the *result* post landed, identified by its reference_id.

    Not "did any send land": a mirror leg reposts the user's question first and
    a Talk-origin task posts an ack, so a room under a Nextcloud outage produces
    several failed sends and only one of them is the answer. A failed send is
    recorded with no `sent_id`, which is what separates it from a refusal.
    """
    ref = f"istota:task:{task_id}:result"
    calls = [
        c for c in fake_talk.calls
        if c.method == "send_message" and c.args.get("reference_id") == ref
    ]
    assert calls, f"no result post was attempted for task {task_id}"
    return calls[-1].sent_id is not None


def _run(config, task_id=None, *, result=ANSWER, success=True):
    with patch(
        "istota.scheduler.execute_task", return_value=(success, result, None, None),
    ):
        process_one_task(config)
    return task_id


def _queue(config, token, **kwargs):
    with db.get_db(config.db_path) as conn:
        return db.create_task(
            conn, prompt="what is the total?", user_id=USER,
            source_type=kwargs.pop("source_type", "talk"),
            conversation_token=token, **kwargs,
        )


# ---------------------------------------------------------------------------
# The reported failure
# ---------------------------------------------------------------------------


@patch("istota.scheduler.run_coro", side_effect=asyncio.run)
class TestATalkPostThatNeverLanded:
    """An ordinary Talk room, a valid token, and a send that raised."""

    def test_the_lost_answer_is_recorded(
        self, mock_run, config, fake_talk, plain,
    ):
        _timeout(fake_talk, plain.talk_ref)
        task_id = _queue(config, plain.canonical)
        with patch("istota.scheduler.send_notification", return_value=True):
            _run(config)

        # The call was made and named the right room — this is not a misroute.
        assert fake_talk.refusals == []
        assert not _result_landed(fake_talk, task_id)

        rows = _rows(config)
        assert len(rows) == 1
        assert rows[0]["dedup_key"] == task_alert.undelivered_key(task_id)
        assert rows[0]["user_id"] == USER
        assert rows[0]["state"] == "open"

    def test_the_row_carries_the_answer_rather_than_announcing_its_loss(
        self, mock_run, config, fake_talk, plain,
    ):
        """The point of the email arm this mirrors is that the answer survives
        the failure, not that the failure is announced."""
        _timeout(fake_talk, plain.talk_ref)
        _queue(config, plain.canonical)
        with patch("istota.scheduler.send_notification", return_value=True):
            _run(config)

        assert "1,240 EUR" in _rows(config)[0]["body"]

    def test_the_alert_send_carries_the_full_body(
        self, mock_run, config, fake_talk, plain,
    ):
        """Routed by `alert` purpose, like every other notice on this path, so
        it reaches whichever surface the user actually reads."""
        _timeout(fake_talk, plain.talk_ref)
        _queue(config, plain.canonical)
        with patch(
            "istota.scheduler.send_notification", return_value=True,
        ) as send:
            _run(config)

        assert send.call_count == 1
        assert send.call_args.kwargs["purpose"] == "alert"
        assert ANSWER in send.call_args.args[2]

    def test_a_landed_post_records_nothing(
        self, mock_run, config, fake_talk, plain,
    ):
        """The control. Same room, same task, no transport failure."""
        task_id = _queue(config, plain.canonical)
        with patch(
            "istota.scheduler.send_notification", return_value=True,
        ) as send:
            _run(config)

        assert _result_landed(fake_talk, task_id)
        assert _rows(config) == []
        assert send.call_count == 0

    def test_the_task_is_still_completed(
        self, mock_run, config, fake_talk, plain,
    ):
        """An invariant pin, not coverage of the fix — it was green before the
        change as well, and a control confirmed that.

        It is here because the decision it pins is a real one, taken against the
        email arm's precedent: that arm marks the task failed and this one leaves
        the status alone. `_finalize_log_channel` has already posted a completed
        line and the assistant row is already in the canonical store, so flipping
        it would leave three records of one task disagreeing about whether it
        ran. What failed is a delivery leg, not the task.
        """
        task_id = _queue(config, plain.canonical)
        _timeout(fake_talk, plain.talk_ref)
        with patch("istota.scheduler.send_notification", return_value=True):
            _run(config)

        assert _status(config, task_id) == "completed"


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


@patch("istota.scheduler.run_coro", side_effect=asyncio.run)
class TestTheGuardIsWhatWasPosted:
    """The issue proposed `plan_talk and talk_token`, the pair the email arm
    keys on. That pair is wrong here, and the case it drops is a real one.

    A silent scheduled job with an ACTION result sets `post_talk_message` under
    `if talk_token:` alone — the branch bypasses the delivery plan entirely,
    which `talk_token`'s own fallback exists for — so `plan_talk` is False and
    the answer is just as lost. The predicate that means "a post was attempted"
    is the one the delivery block itself is gated on.
    """

    def _silent_job(self, config, token):
        with db.get_db(config.db_path) as conn:
            return db.create_task(
                conn, prompt="check the feed", user_id=USER,
                source_type="scheduled", conversation_token=token,
                heartbeat_silent=True,
            )

    def test_a_silent_jobs_lost_action_is_recorded(
        self, mock_run, config, fake_talk, plain,
    ):
        task_id = self._silent_job(config, plain.canonical)
        _timeout(fake_talk, plain.talk_ref)
        with patch("istota.scheduler.send_notification", return_value=True):
            _run(config, result="ACTION: the feed had three new items.")

        rows = _rows(config)
        assert len(rows) == 1
        assert rows[0]["dedup_key"] == task_alert.undelivered_key(task_id)

    def test_a_silent_job_with_nothing_to_say_records_nothing(
        self, mock_run, config, fake_talk, plain,
    ):
        """The control for the case above: NO_ACTION posts nothing, so there is
        no failed post and nothing was lost."""
        self._silent_job(config, plain.canonical)
        _timeout(fake_talk, plain.talk_ref)
        with patch("istota.scheduler.send_notification", return_value=True):
            _run(config, result="NO_ACTION: nothing new.")

        assert _rows(config) == []

    def test_a_task_with_no_talk_leg_at_all_records_nothing(
        self, mock_run, config, fake_talk,
    ):
        """A CLI task posts to no room. Nothing was attempted, so nothing can
        have been lost — the state the old code was indistinguishable from."""
        with db.get_db(config.db_path) as conn:
            db.create_task(
                conn, prompt="x", user_id=USER, source_type="cli",
                conversation_token=None,
            )
        with patch("istota.scheduler.send_notification", return_value=True):
            _run(config)

        assert _rows(config) == []


# ---------------------------------------------------------------------------
# The mirror carve-out
# ---------------------------------------------------------------------------


@patch("istota.scheduler.run_coro", side_effect=asyncio.run)
class TestAMirrorLegIsNotAlerted:
    """A mirror Talk leg is the room fan-out of a web- or email-origin task, so
    the origin surface is what carries the answer and Talk is the copy.

    Alerting there would tell a user their answer was lost while it sits in the
    web room they are looking at — a false alarm on every web-origin task for
    the length of a Nextcloud outage, on the one channel that must stay worth
    reading.
    """

    def test_a_web_origin_task_whose_talk_copy_fails_records_nothing(
        self, mock_run, config, fake_talk, promoted,
    ):
        _timeout(fake_talk, promoted.talk_ref)
        # `output_target="room"` is what the web composer queues (`web_app.py`),
        # and it is what produces the mirror leg at all: the room fan-out is a
        # meta-destination, not a source-type default.
        task_id = _queue(
            config, promoted.canonical, source_type="web", output_target="room",
        )
        with patch(
            "istota.scheduler.send_notification", return_value=True,
        ) as send:
            _run(config)

        # The mirror really was attempted and really did fail — without this the
        # case would pass on a task that had no Talk leg to begin with.
        assert not _result_landed(fake_talk, task_id)
        assert _rows(config) == []
        assert send.call_count == 0
        # And the answer is where the carve-out says it is.
        with db.get_db(config.db_path) as conn:
            row = conn.execute(
                "SELECT body FROM messages WHERE room_token = ? "
                "AND task_id = ? AND role = 'assistant'",
                (promoted.canonical, task_id),
            ).fetchone()
        assert row is not None and ANSWER in row["body"]

    def test_the_same_room_reached_from_talk_is_alerted(
        self, mock_run, config, fake_talk, promoted,
    ):
        """The control that keeps the carve-out from being a blanket exemption.

        Identical room, identical failure. What differs is the origin: here Talk
        is the surface the user asked on, so the copy is the delivery.
        """
        _timeout(fake_talk, promoted.talk_ref)
        _queue(config, promoted.canonical, source_type="talk")
        with patch("istota.scheduler.send_notification", return_value=True):
            _run(config)

        assert len(_rows(config)) == 1


# ---------------------------------------------------------------------------
# The adjacent hole this opened in the email arm
# ---------------------------------------------------------------------------


@patch("istota.scheduler.run_coro", side_effect=asyncio.run)
class TestWhenBothLegsFail:
    """The email arm's guard reads `not (plan_talk and talk_token)`, and its own
    comment says why: "with a room leg the Talk post has already landed and the
    assistant row is stored, so a failed send costs the mail copy alone".

    ISSUE-404 is that the Talk post has *not* necessarily landed. With both legs
    down the guard suppressed the one notice left, so an emailed request whose
    answer reached neither surface was silent on both — and the mirror carve-out
    above depends on that arm firing, since it is what covers an email-origin
    task whose Talk copy is only a copy.
    """

    def _emailed(self, config, promoted):
        with db.get_db(config.db_path) as conn:
            return db.create_task(
                conn, prompt="what is the total?", user_id=USER,
                source_type="email", conversation_token=promoted.canonical,
                output_target=f"room:{promoted.canonical}",
                withheld_from_room=True,
            )

    def test_the_email_arm_no_longer_assumes_the_talk_post_landed(
        self, mock_run, config, fake_talk, promoted,
    ):
        _timeout(fake_talk, promoted.talk_ref)
        task_id = self._emailed(config, promoted)
        with (
            patch("istota.scheduler.post_result_to_email", return_value=False),
            patch(
                "istota.scheduler.send_notification", return_value=True,
            ) as send,
        ):
            _run(config)

        assert not _result_landed(fake_talk, task_id)
        assert len(_rows(config)) == 1
        assert ANSWER in send.call_args.args[2]

    def test_a_landed_talk_post_still_suppresses_the_email_notice(
        self, mock_run, config, fake_talk, promoted,
    ):
        """The control, and the behaviour the guard was written for: the answer
        is in the room, so a failed mail copy is not worth an alert."""
        task_id = self._emailed(config, promoted)
        with (
            patch("istota.scheduler.post_result_to_email", return_value=False),
            patch(
                "istota.scheduler.send_notification", return_value=True,
            ) as send,
        ):
            _run(config)

        assert _result_landed(fake_talk, task_id)
        assert _rows(config) == []
        assert send.call_count == 0


# ---------------------------------------------------------------------------
# The shapes that are not a completed answer
# ---------------------------------------------------------------------------


@patch("istota.scheduler.run_coro", side_effect=asyncio.run)
class TestAConfirmationPromptThatNeverLanded:
    """The branch that parks a task writes its `confirmation` row whatever else
    carries the question, and withholds the row's *push* when the Talk post is
    going to carry it. A post that never lands therefore leaves an actionable,
    object-backed row that nothing delivered, and the task dies at
    `expire_stale_confirmations` two hours later having asked nobody.

    The repair is to deliver that row, not to raise a `task_alert`: the alert is
    non-actionable and auto-resolves on being seen, so it would close itself in
    front of the row carrying the `!confirm` verbs. Both are asserted, because
    delivering the right one and also minting the wrong one is the failure.

    The two send paths are two different patch targets, which is what lets these
    cases tell them apart: the generic arm calls `scheduler.send_notification`
    directly, while `deliver_pending` imports it from `.notifications` inside the
    function.
    """

    QUESTION = "I need your confirmation before I delete the archive. Please confirm."

    def test_a_talk_confirmation_delivers_the_row_it_withheld(
        self, mock_run, config, fake_talk, plain,
    ):
        _timeout(fake_talk, plain.talk_ref)
        _queue(config, plain.canonical)
        with patch(
            "istota.notifications.send_notification", return_value=True,
        ) as send:
            _run(config, result=self.QUESTION)

        assert send.call_count == 1
        assert "delete the archive" in send.call_args.args[2]

    def test_and_raises_no_task_alert_beside_it(
        self, mock_run, config, fake_talk, plain,
    ):
        _timeout(fake_talk, plain.talk_ref)
        _queue(config, plain.canonical)
        with (
            patch("istota.notifications.send_notification", return_value=True),
            patch(
                "istota.scheduler.send_notification", return_value=True,
            ) as generic,
        ):
            _run(config, result=self.QUESTION)

        assert _rows(config) == []
        assert generic.call_count == 0

    def test_an_email_confirmation_on_its_mirror_leg_is_delivered_too(
        self, mock_run, config, fake_talk, promoted,
    ):
        """The case the mirror carve-out must not take. An email-origin
        confirmation is posted on its mirror leg deliberately — that leg is the
        only push surface that can reach the user, since the email leg must
        never carry the question to the correspondent — so suppressing here
        would restore exactly the silence the confirmation gate records fixing.
        """
        _timeout(fake_talk, promoted.talk_ref)
        with db.get_db(config.db_path) as conn:
            db.create_task(
                conn, prompt="delete it?", user_id=USER, source_type="email",
                conversation_token=promoted.canonical,
                output_target=f"room:{promoted.canonical}",
            )
        with patch(
            "istota.notifications.send_notification", return_value=True,
        ) as send:
            _run(config, result=self.QUESTION)

        assert send.call_count == 1
        assert "delete the archive" in send.call_args.args[2]

    def test_a_landed_confirmation_post_delivers_nothing_extra(
        self, mock_run, config, fake_talk, plain,
    ):
        """The control. The room has the question, so pushing the row as well
        would put it in front of the user twice — which is the decision the
        parking branch made and this must not undo."""
        task_id = _queue(config, plain.canonical)
        with patch(
            "istota.notifications.send_notification", return_value=True,
        ) as send:
            _run(config, result=self.QUESTION)

        assert _result_landed(fake_talk, task_id)
        assert send.call_count == 0
        assert _rows(config) == []


@patch("istota.scheduler.run_coro", side_effect=asyncio.run)
class TestTheOtherTwoShapesTheArmRunsOn:
    """`post_talk_message` is not always a completed answer, and the arm was
    written as though it were. Both of these are decisions rather than
    accidents, so each has a case."""

    def _fail_permanently(self, config, token):
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="do the thing", user_id=USER, source_type="talk",
                conversation_token=token,
            )
            # One attempt, so the failure is permanent rather than requeued —
            # the retry ladder never reaches the delivery block.
            conn.execute(
                "UPDATE tasks SET max_attempts = 1 WHERE id = ?", (task_id,),
            )
        return task_id

    def test_an_undelivered_failure_notice_is_recorded_and_stays_failed(
        self, mock_run, config, fake_talk, plain,
    ):
        """A permanently failed task posts an apology to Talk. If that does not
        land the user is told nothing at all — not the answer, not the failure —
        so the notice is worth raising. The status is already `failed` here,
        which is why the arm writes no status of its own.
        """
        task_id = self._fail_permanently(config, plain.canonical)
        _timeout(fake_talk, plain.talk_ref)
        with patch("istota.scheduler.send_notification", return_value=True):
            _run(config, result="the tool exited 1", success=False)

        assert len(_rows(config)) == 1
        assert _status(config, task_id) == "failed"

    def test_an_automated_tasks_lost_body_is_recorded_too(
        self, mock_run, config, fake_talk, plain,
    ):
        """A briefing, and the decision is that it is treated like any other
        undelivered body. What the arm exists to stop is content generated and
        delivered nowhere, and an automated task's output is lost exactly as
        thoroughly as an interactive one's. The sibling suppression for
        `briefing` and `scheduled` on the permanent-failure path is about
        errors, which is a different thing to put in front of a user.
        """
        with db.get_db(config.db_path) as conn:
            db.create_task(
                conn, prompt="morning briefing", user_id=USER,
                source_type="briefing", conversation_token=plain.canonical,
            )
        _timeout(fake_talk, plain.talk_ref)
        with patch("istota.scheduler.send_notification", return_value=True):
            _run(config, result="Three things happened overnight.")

        assert len(_rows(config)) == 1
        assert "overnight" in _rows(config)[0]["body"]
