"""The bounded retry behind a Talk post, and the readback that makes it safe.

ISSUE-405. A single `ReadTimeout` on a Talk POST ended delivery permanently —
thirteen of them on the production host in a week, on an idle machine answering
`status.php` in 16 ms, so a second attempt seconds later would very likely have
landed. What stopped a retry being written earlier is that a `ReadTimeout` on a
POST is not evidence the message was *not* stored: Nextcloud may have accepted
and written it and merely been slow to answer, and a blind re-post then puts a
duplicate in the user's room, which is worse than the silence it replaces.

So every test here is built around telling those two states apart, and each one
has to be able to fail in **both** directions:

- a post that did not land must be re-posted (pre-fix: it never was), and
- a post that *did* land must not be (a blind retry: it would be).

Asserting only "no exception was raised" proves neither, and neither does
asserting the final outcome alone. `TalkTransport.deliver` catches everything
and returns `None`, and with a retry in place a test that passes because the
first attempt succeeded looks identical to one that passes because the retry
worked. So every case names the returned id **and** the number of sends the
double recorded.

The third rule is the one the split-message cases exist for: a `referenceId`
names the whole answer rather than one post, so on a message split into parts a
match proves *some* part is in the room and never that all of them are. The
readback is refused there outright — see `TestASplitMessageIsNotSettled`.

`fake_talk` rather than a `MagicMock`: the readback addresses a room, and a
readback aimed at a room's canonical token instead of its Talk binding is
ISSUE-400 again in a new place. The double refuses that token the way Nextcloud
does, so `refusals == []` is a real assertion here.
"""

from __future__ import annotations

import dataclasses

import httpx
import pytest

from istota import db
from istota.config import NextcloudConfig, TalkConfig
from istota.transport import talk as talk_pkg
from istota.transport.talk import TalkTransport, _posted_message_id

from .support.rooms import plain_talk_room, promoted_room

REF = "istota:task:7:result"
BOT = "istota"
BOTS = {BOT}


@pytest.fixture
def rooms(db_path):
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
            url="https://nc.example.com", username=BOT, app_password="s",
        ),
        talk=TalkConfig(enabled=True, bot_username=BOT),
    )


@pytest.fixture(autouse=True)
def slept(monkeypatch):
    """Record the backoff instead of serving it, and hand the record back.

    It patches the *module's* `asyncio` name rather than `asyncio.sleep`
    itself: `talk_pkg.asyncio` is the stdlib module object, so setting `sleep`
    on it would rebind the coroutine process-wide — including for the
    persistent `AsyncRuntime` loop running in another thread — for the duration
    of every test in this file.

    Patching the sleep and not `_POST_BACKOFF_SECONDS` is also what keeps
    `TestTheAttemptsAreBounded::test_the_bound_is_small` honest: a fixture that
    rebound the constant would leave that pin asserting against its own patch.
    """
    import types

    waits: list[float] = []

    async def _record(seconds):
        waits.append(seconds)

    monkeypatch.setattr(
        talk_pkg, "asyncio", types.SimpleNamespace(sleep=_record),
    )
    return waits


def _task(**overrides):
    defaults = dict(
        id=7, status="completed", source_type="talk",
        user_id="alice", prompt="hi", conversation_token="room",
    )
    defaults.update(overrides)
    return db.Task(**defaults)


def _timeout() -> httpx.ReadTimeout:
    """What production actually threw: `ReadTimeout('')`, no message."""
    return httpx.ReadTimeout("")


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://nc.example.com/ocs")
    return httpx.HTTPStatusError(
        f"{code}", request=request, response=httpx.Response(code, request=request),
    )


def _landed(msg_id, *, reference_id: str = REF, actor: str = BOT, **extra):
    """A message Talk kept, shaped as the chat API returns one."""
    msg = {
        "id": msg_id,
        "referenceId": reference_id,
        "actorType": "users",
        "actorId": actor,
        "message": "stored",
    }
    msg.update(extra)
    return msg


def _sends(client):
    return [c for c in client.calls if c.method == "send_message"]


def _readbacks(client):
    return [c for c in client.calls if c.method == "fetch_chat_history"]


class TestATransientFailureIsRetried:
    """The reported symptom: one `ReadTimeout`, nothing stored, answer lost."""

    async def test_a_read_timeout_that_stored_nothing_is_re_posted(
        self, fake_talk, rooms, talk_config,
    ):
        ref = rooms["plain"].talk_ref
        fake_talk.send_failures[ref] = [_timeout(), None]

        result = await TalkTransport(talk_config).deliver(
            ref, "the answer", task=_task(), reference_id=REF,
        )

        sends = _sends(fake_talk)
        assert len(sends) == 2, "the point is the second attempt, not the outcome"
        assert result == sends[-1].sent_id
        assert result is not None
        assert sends[0].sent_id is None
        assert fake_talk.refusals == []

    async def test_the_re_post_carries_the_same_body_and_reference(
        self, fake_talk, rooms, talk_config,
    ):
        ref = rooms["plain"].talk_ref
        fake_talk.send_failures[ref] = [_timeout(), None]

        await TalkTransport(talk_config).deliver(
            ref, "the answer", task=_task(), reference_id=REF,
        )

        first, second = _sends(fake_talk)
        assert first.args["message"] == second.args["message"] == "the answer"
        assert second.args["reference_id"] == REF

    async def test_a_5xx_is_retried(self, fake_talk, rooms, talk_config):
        ref = rooms["plain"].talk_ref
        fake_talk.send_failures[ref] = [_status_error(503), None]

        result = await TalkTransport(talk_config).deliver(
            ref, "x", task=_task(), reference_id=REF,
        )

        assert result is not None
        assert len(_sends(fake_talk)) == 2

    async def test_two_failures_still_land(self, fake_talk, rooms, talk_config):
        ref = rooms["plain"].talk_ref
        fake_talk.send_failures[ref] = [_timeout(), _timeout(), None]

        result = await TalkTransport(talk_config).deliver(
            ref, "x", task=_task(), reference_id=REF,
        )

        assert len(_sends(fake_talk)) == 3
        assert result == _sends(fake_talk)[-1].sent_id


class TestAStoredMessageIsNotPostedTwice:
    """The half a blind retry gets wrong, and the reason none was written.

    The POST timed out and Nextcloud stored the message anyway. The double's
    reads and writes are unconnected precisely so a test can say that: the send
    raises, and the message Talk kept is seeded into the room's history by hand.
    """

    async def test_the_readback_finds_it_and_nothing_is_re_posted(
        self, fake_talk, rooms, talk_config,
    ):
        ref = rooms["plain"].talk_ref
        fake_talk.send_failures[ref] = [_timeout(), None]
        fake_talk.messages[ref] = [_landed(4242)]

        result = await TalkTransport(talk_config).deliver(
            ref, "the answer", task=_task(), reference_id=REF,
        )

        # Delivered — the user can see it — so the id, never None. A `None`
        # here is what ISSUE-404's undelivered branch keys on, and it would
        # fire on a message already in the room.
        assert result == 4242
        assert len(_sends(fake_talk)) == 1
        assert len(_readbacks(fake_talk)) == 1

    async def test_it_is_found_after_the_last_attempt_too(
        self, fake_talk, rooms, talk_config,
    ):
        """The readback runs at the *top* of an attempt, so without one after
        the loop the final failure would report None for a message Nextcloud
        had written — the same ambiguity, moved to attempt three.

        The first two failures are connect-class on purpose: those settle
        themselves, so the loop reaches its last attempt having asked the room
        nothing. One readback in `calls` is then the one under test.
        """
        ref = rooms["plain"].talk_ref
        fake_talk.send_failures[ref] = [
            httpx.ConnectError("refused"), httpx.ConnectError("refused"),
            _timeout(),
        ]
        fake_talk.messages[ref] = [_landed(777)]

        result = await TalkTransport(talk_config).deliver(
            ref, "the answer", task=_task(), reference_id=REF,
        )

        assert len(_sends(fake_talk)) == talk_pkg._POST_ATTEMPTS
        assert len(_readbacks(fake_talk)) == 1
        assert result == 777

    async def test_the_readback_addresses_the_room_the_post_did(
        self, fake_talk, rooms, talk_config,
    ):
        """A promoted room: canonical token and Talk ref are different strings,
        and only the Talk ref names a conversation Nextcloud will answer for."""
        room = rooms["promoted"]
        assert room.diverges
        fake_talk.send_failures[room.talk_ref] = [_timeout(), None]
        fake_talk.messages[room.talk_ref] = [_landed(99)]

        result = await TalkTransport(talk_config).deliver(
            room.talk_ref, "x", task=_task(), reference_id=REF,
        )

        assert result == 99
        assert {c.token for c in fake_talk.calls} == {room.talk_ref}
        assert fake_talk.refusals == []

    async def test_a_message_from_another_actor_does_not_suppress_the_post(
        self, fake_talk, rooms, talk_config,
    ):
        """`referenceId` is free text on Talk's chat API and any participant in
        the room can set it — the same property `inbound` guards against. A
        readback matching on the reference alone would let a room member
        suppress the bot's own answer by claiming its key."""
        ref = rooms["plain"].talk_ref
        fake_talk.send_failures[ref] = [_timeout(), None]
        fake_talk.messages[ref] = [_landed(4242, actor="mallory")]

        result = await TalkTransport(talk_config).deliver(
            ref, "the answer", task=_task(), reference_id=REF,
        )

        assert len(_sends(fake_talk)) == 2
        assert result == _sends(fake_talk)[-1].sent_id
        assert result != 4242

    async def test_a_message_carrying_another_reference_is_not_ours(
        self, fake_talk, rooms, talk_config,
    ):
        ref = rooms["plain"].talk_ref
        fake_talk.send_failures[ref] = [_timeout(), None]
        fake_talk.messages[ref] = [_landed(4242, reference_id="istota:task:7:ack")]

        result = await TalkTransport(talk_config).deliver(
            ref, "the answer", task=_task(), reference_id=REF,
        )

        assert len(_sends(fake_talk)) == 2
        assert result != 4242


def _split_transport(config, limit: int = 30) -> TalkTransport:
    """A transport whose per-message limit forces an ordinary body to split.

    `capabilities` is a frozen dataclass on the class; a copy on the instance
    shadows it, so this changes what `deliver` reads without touching the class
    every other test in the process shares.
    """
    transport = TalkTransport(config)
    transport.capabilities = dataclasses.replace(
        TalkTransport.capabilities, max_message_length=limit,
    )
    return transport


SPLIT_TEXT = "part one. part two. part three. part four. part five."


class TestASplitMessageIsNotSettled:
    """One `referenceId` is stamped on every part, so a match proves *some*
    part landed and never that all of them did.

    Reporting success on that evidence would turn a loud, recoverable failure
    into a silently truncated answer — the user gets the first fifth of their
    reply and the scheduler is told it was delivered. So a split send gets no
    readback at all, and keeps the honest `None` it has today. It is also why
    the alternative (require all N parts before answering) is not taken: with
    one key across N parts, "part three never landed" and "the window returned
    three of five" are the same evidence.
    """

    def test_the_premise(self, talk_config):
        """These tests mean nothing unless the body really does split."""
        transport = _split_transport(talk_config)
        parts = talk_pkg.split_message(
            SPLIT_TEXT, transport.capabilities.max_message_length,
        )
        assert len(parts) > 1
        assert len(talk_pkg.split_message("short", 30)) == 1

    async def test_only_some_parts_landing_still_reports_nothing_delivered(
        self, fake_talk, rooms, talk_config,
    ):
        ref = rooms["plain"].talk_ref
        # Parts one and two land; every later post times out.
        fake_talk.send_failures[ref] = [None, None] + [_timeout()] * 20
        # And the room holds what landed, carrying the same reference id.
        fake_talk.messages[ref] = [_landed(101), _landed(102)]

        result = await _split_transport(talk_config).deliver(
            ref, SPLIT_TEXT, task=_task(), reference_id=REF,
        )

        assert result is None, "a partly-posted answer is not a delivered one"
        assert _readbacks(fake_talk) == [], "nothing may settle a split send"
        assert len(_sends(fake_talk)) == 3, "it stopped at the part that failed"

    async def test_a_split_send_is_not_retried_on_a_timeout(
        self, fake_talk, rooms, talk_config,
    ):
        """Not even once: the first failure cannot be settled, so it stops."""
        ref = rooms["plain"].talk_ref
        fake_talk.send_failures[ref] = _timeout()

        result = await _split_transport(talk_config).deliver(
            ref, SPLIT_TEXT, task=_task(), reference_id=REF,
        )

        assert result is None
        assert len(_sends(fake_talk)) == 1
        assert _readbacks(fake_talk) == []

    async def test_a_split_send_still_retries_when_nothing_was_sent(
        self, fake_talk, rooms, talk_config,
    ):
        """A connect failure needs no readback — nothing reached Nextcloud, so
        re-posting the part can neither duplicate nor truncate."""
        ref = rooms["plain"].talk_ref
        transport = _split_transport(talk_config)
        parts = talk_pkg.split_message(
            SPLIT_TEXT, transport.capabilities.max_message_length,
        )
        fake_talk.send_failures[ref] = [httpx.ConnectError("refused")] + [None] * 20

        result = await transport.deliver(
            ref, SPLIT_TEXT, task=_task(), reference_id=REF,
        )

        assert result is not None
        assert len(_sends(fake_talk)) == len(parts) + 1
        assert _readbacks(fake_talk) == []

    async def test_a_single_part_message_under_the_same_limit_is_settled(
        self, fake_talk, rooms, talk_config,
    ):
        """The control for the three above: same low limit, a body that fits."""
        ref = rooms["plain"].talk_ref
        fake_talk.send_failures[ref] = [_timeout(), None]
        fake_talk.messages[ref] = [_landed(55)]

        result = await _split_transport(talk_config).deliver(
            ref, "short", task=_task(), reference_id=REF,
        )

        assert result == 55
        assert len(_readbacks(fake_talk)) == 1


class TestAnAnswerIsNotABlip:
    """404 and 403 are Nextcloud telling us something. Re-posting into a room
    that is gone, or that we may not write to, buys nothing and costs latency
    on a path that blocks a worker thread."""

    @pytest.mark.parametrize("code", [400, 403, 404, 413, 422])
    async def test_a_4xx_is_not_retried_and_needs_no_readback(
        self, fake_talk, rooms, talk_config, code,
    ):
        ref = rooms["plain"].talk_ref
        fake_talk.send_failures[ref] = _status_error(code)

        result = await TalkTransport(talk_config).deliver(
            ref, "x", task=_task(), reference_id=REF,
        )

        assert result is None
        assert len(_sends(fake_talk)) == 1
        # The server rejected the post, so nothing was stored and there is
        # nothing to ask about.
        assert _readbacks(fake_talk) == []

    async def test_an_unclassifiable_failure_is_not_retried_but_is_questioned(
        self, fake_talk, rooms, talk_config,
    ):
        """`send_message` calls `raise_for_status()` and then `response.json()`,
        so a 2xx whose body does not parse raises after Nextcloud has written
        the message. Not worth retrying, and it still has to be asked about, or
        `None` would be reported for a post the user can see."""
        ref = rooms["plain"].talk_ref
        fake_talk.send_failures[ref] = ValueError("Expecting value")
        fake_talk.messages[ref] = [_landed(31337)]

        result = await TalkTransport(talk_config).deliver(
            ref, "x", task=_task(), reference_id=REF,
        )

        assert len(_sends(fake_talk)) == 1
        assert len(_readbacks(fake_talk)) == 1
        assert result == 31337

    async def test_a_misroute_is_not_retried(self, fake_talk, rooms, talk_config):
        """The double's refusal for a token naming no conversation.

        It is a plain `Exception` rather than an `HTTPStatusError`, so the
        classifier cannot see the 404 in it and asks the room once before
        giving up — which the double refuses again. In production this case
        arrives as a real 404 and costs no readback; that shape is covered by
        `test_a_4xx_is_not_retried_and_needs_no_readback[404]`.
        """
        result = await TalkTransport(talk_config).deliver(
            rooms["promoted"].canonical, "x", task=_task(), reference_id=REF,
        )

        assert result is None
        assert len(_sends(fake_talk)) == 1
        assert all(c.refused for c in fake_talk.calls)


class TestTheAttemptsAreBounded:
    async def test_a_persistent_failure_stops_and_returns_none(
        self, fake_talk, rooms, talk_config,
    ):
        """`None` has to keep meaning "nothing was posted": ISSUE-404's
        undelivered branch is written against exactly this value."""
        ref = rooms["plain"].talk_ref
        fake_talk.send_failures[ref] = _timeout()

        result = await TalkTransport(talk_config).deliver(
            ref, "x", task=_task(), reference_id=REF,
        )

        assert result is None
        assert len(_sends(fake_talk)) == talk_pkg._POST_ATTEMPTS

    def test_the_bound_is_small(self):
        """A judgement, pinned so a later change to it is deliberate. Each
        extra attempt costs a full POST timeout on a worker thread that is
        blocking either a task's start (the ack) or its completion (the
        result). The `slept` fixture patches the sleep rather than these
        constants, so this reads the shipped values."""
        assert talk_pkg._POST_ATTEMPTS == 3
        assert talk_pkg._POST_BACKOFF_SECONDS == (1.0, 3.0)
        assert len(talk_pkg._POST_BACKOFF_SECONDS) == talk_pkg._POST_ATTEMPTS - 1
        assert talk_pkg._READBACK_TIMEOUT_SECONDS < 30

    async def test_the_readback_is_given_a_tighter_timeout_than_the_default(
        self, fake_talk, rooms, talk_config,
    ):
        """`fetch_chat_history`'s own default is 30s, which inside a ladder on
        a thread blocking a task would multiply the wait rather than shorten
        it."""
        ref = rooms["plain"].talk_ref
        fake_talk.send_failures[ref] = [_timeout(), None]

        await TalkTransport(talk_config).deliver(
            ref, "x", task=_task(), reference_id=REF,
        )

        read = _readbacks(fake_talk)[0]
        assert read.args["timeout"] == talk_pkg._READBACK_TIMEOUT_SECONDS
        assert read.args["limit"] == talk_pkg._READBACK_LIMIT

    async def test_it_also_gives_up_on_elapsed_time(
        self, fake_talk, rooms, talk_config, monkeypatch,
    ):
        """The attempt count does not bound the wait — each attempt can sit for
        the client's whole timeout, and the log-channel subscriber re-enters
        this per tool call for as long as its first post keeps failing."""
        import types

        ref = rooms["plain"].talk_ref
        fake_talk.send_failures[ref] = _timeout()
        ticks = iter([0.0] + [999.0] * 10)
        monkeypatch.setattr(
            talk_pkg, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)),
        )

        result = await TalkTransport(talk_config).deliver(
            ref, "x", task=_task(), reference_id=REF,
        )

        assert result is None
        assert len(_sends(fake_talk)) == 1, "the deadline cut it short"
        # It still asks once on the way out: `deliver` is about to report None,
        # and None has to mean the message is not in the room.
        assert len(_readbacks(fake_talk)) == 1


class TestTheBackoff:
    async def test_it_waits_between_attempts(
        self, fake_talk, rooms, talk_config, slept,
    ):
        ref = rooms["plain"].talk_ref
        fake_talk.send_failures[ref] = [_timeout(), _timeout(), None]

        await TalkTransport(talk_config).deliver(
            ref, "x", task=_task(), reference_id=REF,
        )

        assert slept == list(talk_pkg._POST_BACKOFF_SECONDS)

    async def test_a_success_waits_for_nothing(
        self, fake_talk, rooms, talk_config, slept,
    ):
        await TalkTransport(talk_config).deliver(
            rooms["plain"].talk_ref, "x", task=_task(), reference_id=REF,
        )

        assert slept == []


class TestWhenTheQuestionCannotBeSettled:
    """An unanswerable "did it land?" holds the message back. That direction is
    chosen deliberately: the failure being guarded against is a duplicate in
    the user's room, and the pre-fix behaviour — no retry at all — is exactly
    what falling back to costs, so nothing is lost that was not already lost."""

    async def test_a_readback_that_fails_stops_the_retry(
        self, fake_talk, rooms, talk_config,
    ):
        ref = rooms["plain"].talk_ref
        fake_talk.send_failures[ref] = _timeout()
        fake_talk.history_failures[ref] = _timeout()

        result = await TalkTransport(talk_config).deliver(
            ref, "x", task=_task(), reference_id=REF,
        )

        assert result is None
        assert len(_sends(fake_talk)) == 1
        # Asked once. Not asked a second time on the way out, because the
        # question about that same failure had already gone unanswered.
        assert len(_readbacks(fake_talk)) == 1

    async def test_no_reference_id_means_no_re_post(
        self, fake_talk, rooms, talk_config,
    ):
        """With no idempotency key there is nothing to read back, so a re-post
        could only be blind. Every scheduler post carries one; a caller that
        does not gets the old behaviour rather than a duplicate."""
        ref = rooms["plain"].talk_ref
        fake_talk.send_failures[ref] = _timeout()

        result = await TalkTransport(talk_config).deliver(ref, "x", task=_task())

        assert result is None
        assert len(_sends(fake_talk)) == 1
        assert _readbacks(fake_talk) == []

    async def test_no_bot_account_name_means_no_re_post(
        self, fake_talk, rooms, make_config, db_path,
    ):
        """Without one a readback cannot tell our own message from a room
        member's, so it cannot be trusted to suppress a post."""
        config = make_config(
            db_path=db_path,
            nextcloud=NextcloudConfig(
                url="https://nc.example.com", username="", app_password="s",
            ),
            talk=TalkConfig(enabled=True, bot_username=""),
        )
        ref = rooms["plain"].talk_ref
        fake_talk.send_failures[ref] = _timeout()

        result = await TalkTransport(config).deliver(
            ref, "x", task=_task(), reference_id=REF,
        )

        assert result is None
        assert len(_sends(fake_talk)) == 1
        assert _readbacks(fake_talk) == []

    async def test_an_unnameable_message_of_ours_is_not_absence(
        self, fake_talk, rooms, talk_config,
    ):
        """A history entry that is ours by reference and actor and whose id is
        not a number: we cannot name it, so we must not call it absent and
        post again."""
        ref = rooms["plain"].talk_ref
        fake_talk.send_failures[ref] = _timeout()
        fake_talk.messages[ref] = [_landed("not-a-number")]

        result = await TalkTransport(talk_config).deliver(
            ref, "x", task=_task(), reference_id=REF,
        )

        assert result is None
        assert len(_sends(fake_talk)) == 1


class TestWhenNothingWasEverSent:
    """A connect-class failure establishes on its own that Nextcloud never saw
    the request, so the readback is skipped — which is also what keeps the
    retry useful during an outage, where the readback would fail too."""

    @pytest.mark.parametrize("exc", [
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout(""),
        httpx.PoolTimeout(""),
    ])
    async def test_it_re_posts_without_a_readback(
        self, fake_talk, rooms, talk_config, exc,
    ):
        ref = rooms["plain"].talk_ref
        fake_talk.send_failures[ref] = [exc, None]

        result = await TalkTransport(talk_config).deliver(
            ref, "x", task=_task(), reference_id=REF,
        )

        assert len(_sends(fake_talk)) == 2
        assert result == _sends(fake_talk)[-1].sent_id
        assert _readbacks(fake_talk) == []

    async def test_a_connect_failure_re_posts_even_unlabelled(
        self, fake_talk, rooms, talk_config,
    ):
        ref = rooms["plain"].talk_ref
        fake_talk.send_failures[ref] = [httpx.ConnectError("refused"), None]

        result = await TalkTransport(talk_config).deliver(ref, "x", task=_task())

        assert result is not None
        assert len(_sends(fake_talk)) == 2


class TestThePureReadbackFilter:
    """`_posted_message_id` is where the whole idempotency decision is made, so
    it is tested directly rather than only through the double — a double that
    reimplemented the filter would hide a bug in the real one."""

    def test_it_takes_the_newest_match(self):
        history = [_landed(1), _landed(2), _landed(3)]
        assert _posted_message_id(history, REF, BOTS) == (3, True)

    def test_history_is_oldest_first(self):
        """Which is what `TalkClient.fetch_chat_history` returns after its own
        reverse. Reading it newest-first would take the wrong end."""
        history = [_landed(3), _landed(1)]
        assert _posted_message_id(history, REF, BOTS) == (1, True)

    def test_another_actor_is_not_us(self):
        history = [_landed(1, actor="mallory")]
        assert _posted_message_id(history, REF, BOTS) == (None, True)

    def test_a_guest_actor_is_not_us(self):
        history = [_landed(1, actorType="guests")]
        assert _posted_message_id(history, REF, BOTS) == (None, True)

    def test_a_bot_actor_is_not_us(self):
        """The user API posts as `users`. A `bots` actor with our own name is
        somebody else's integration."""
        history = [_landed(1, actorType="bots")]
        assert _posted_message_id(history, REF, BOTS) == (None, True)

    def test_an_actor_mismatch_never_makes_the_answer_unreadable(self):
        """A room member who could turn the question unanswerable could block
        delivery outright, which is the same denial by another route. So a
        foreign message under our key is a decided "not ours"."""
        history = [_landed(1, actor="mallory"), _landed(2, actorType="guests")]
        assert _posted_message_id(history, REF, BOTS) == (None, True)

    def test_another_reference_is_not_ours(self):
        history = [_landed(1, reference_id="istota:task:7:ack")]
        assert _posted_message_id(history, REF, BOTS) == (None, True)

    def test_a_deleted_message_still_counts(self):
        """The question is whether Nextcloud stored the post. It did — and
        re-posting something the user has since deleted is the duplicate this
        path exists to avoid."""
        history = [_landed(1, deleted=True, messageType="comment_deleted")]
        assert _posted_message_id(history, REF, BOTS) == (1, True)

    @pytest.mark.parametrize("bad", ["12", None, 1.0, True])
    def test_an_unnameable_message_of_ours_is_unreadable(self, bad):
        """Not "absent". Every field comes off `response.json()`, so the type
        is whatever was on the wire, and reading this as absence posts again."""
        assert _posted_message_id([_landed(bad)], REF, BOTS) == (None, False)

    def test_a_non_dict_entry_is_unreadable(self):
        assert _posted_message_id(["nonsense", 12, None], REF, BOTS) == (None, False)

    def test_a_match_answers_the_question_whatever_else_was_there(self):
        """Only an empty result has to be well-formed to mean "not there"."""
        found, readable = _posted_message_id(
            ["nonsense", _landed(6)], REF, BOTS,
        )
        assert found == 6
        assert readable is False

    def test_an_empty_history_is_a_readable_absence(self):
        assert _posted_message_id([], REF, BOTS) == (None, True)

    def test_either_configured_bot_name_matches(self):
        """`talk.bot_username` and `nextcloud.username` are configured
        separately and are the same string on every shipped deployment.
        Matching only one would make the readback miss our own post where they
        differ, and the transport would re-post."""
        history = [_landed(1, actor="istota-svc")]
        assert _posted_message_id(history, REF, {"istota", "istota-svc"}) == (1, True)


class TestTheClassification:
    @pytest.mark.parametrize("exc", [
        httpx.ReadTimeout(""),
        httpx.ConnectTimeout(""),
        httpx.WriteTimeout(""),
        httpx.PoolTimeout(""),
        httpx.ConnectError("x"),
        httpx.ReadError("x"),
        httpx.WriteError("x"),
        httpx.RemoteProtocolError("x"),
        httpx.ProxyError("x"),
    ])
    def test_transport_failures_are_transient(self, exc):
        assert talk_pkg._is_transient(exc) is True

    @pytest.mark.parametrize("code", [500, 502, 503, 504, 599])
    def test_5xx_is_transient(self, code):
        assert talk_pkg._is_transient(_status_error(code)) is True

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 409, 413, 429])
    def test_4xx_is_an_answer(self, code):
        assert talk_pkg._is_transient(_status_error(code)) is False

    @pytest.mark.parametrize("exc", [
        ValueError("x"),
        KeyError("x"),
        httpx.LocalProtocolError("x"),
        Exception("boom"),
    ])
    def test_everything_else_is_not_transient(self, exc):
        assert talk_pkg._is_transient(exc) is False

    @pytest.mark.parametrize("exc", [
        httpx.ConnectError("x"), httpx.ConnectTimeout(""), httpx.PoolTimeout(""),
    ])
    def test_connect_class_failures_prove_nothing_was_sent(self, exc):
        assert talk_pkg._request_never_sent(exc) is True
        assert talk_pkg._may_have_been_stored(exc) is False

    @pytest.mark.parametrize("exc", [
        httpx.ReadTimeout(""),
        httpx.WriteTimeout(""),
        httpx.ReadError("x"),
        httpx.WriteError("x"),
        httpx.RemoteProtocolError("x"),
    ])
    def test_everything_else_may_have_been_stored(self, exc):
        """A `WriteTimeout` is in here on purpose: part of the request was on
        the wire, so the server may have completed it."""
        assert talk_pkg._request_never_sent(exc) is False
        assert talk_pkg._may_have_been_stored(exc) is True

    def test_a_5xx_may_have_been_stored(self):
        """Nextcloud answered, so it saw the request. A 500 raised after the
        row was written is a message in the room with an error for an answer."""
        assert talk_pkg._may_have_been_stored(_status_error(500)) is True

    @pytest.mark.parametrize("code", [400, 403, 404, 413])
    def test_a_4xx_settles_it_without_a_request(self, code):
        """The server rejected the post, so nothing was stored, and a readback
        would only cost a round trip against a room that has already said no."""
        assert talk_pkg._may_have_been_stored(_status_error(code)) is False

    def test_an_unclassifiable_exception_may_have_been_stored(self):
        """`send_message` parses the body after `raise_for_status()`, so a 2xx
        whose body does not parse raises here with the message written. The
        retry predicate says no; this one has to say yes."""
        assert talk_pkg._is_transient(ValueError("x")) is False
        assert talk_pkg._may_have_been_stored(ValueError("x")) is True


class TestTheBotActorIds:
    def test_both_configured_names_are_accepted(self, make_config):
        config = make_config(
            nextcloud=NextcloudConfig(url="https://nc", username="svc"),
            talk=TalkConfig(bot_username="istota"),
        )
        assert talk_pkg._bot_actor_ids(config) == {"istota", "svc"}

    def test_an_empty_name_is_not_one(self, make_config):
        config = make_config(
            nextcloud=NextcloudConfig(url="https://nc", username=""),
            talk=TalkConfig(bot_username="istota"),
        )
        assert talk_pkg._bot_actor_ids(config) == {"istota"}

    def test_no_names_at_all_is_empty(self, make_config):
        config = make_config(
            nextcloud=NextcloudConfig(url="https://nc", username=""),
            talk=TalkConfig(bot_username=""),
        )
        assert talk_pkg._bot_actor_ids(config) == set()
