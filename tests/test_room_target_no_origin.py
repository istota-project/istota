"""ISSUE-511 — a `room` destination on a task that originated on no surface.

`_expand_room_destinations` used to ask `registry._surface_for_source_type` for
the origin surface it skips. That map answers "where do I deliver this result"
and returns `"talk"` for everything it does not recognise, so a scheduled job
carrying `target = "room:<token>"` had the room's Talk binding skipped as an
origin leg that does not exist, and its web binding skipped as a canonical view
an origin leg was assumed to have written. Rooms bind only `talk` and `web`, so
the plan came out empty, `scheduled` is not in `_INTERACTIVE_SOURCE_TYPES` so
nothing caught it, and the answer went nowhere while the job reported success.

The fix asks `surfaces.origin_surface_for_source_type`, which answers `None` for
a task that originated on no surface, and reads that as "no origin leg wrote
anything": every binding is emitted, the canonical one included.

Two halves are covered here. The planner is the fix; the scheduler is what makes
it land, and emitting the canonical leg is what the answer's transcript row
depends on — `_room_turn_belongs_here`'s first rung reads
`own_room_canonical_dests`, which is empty unless the web binding is in the
plan. On a web-only room that rung is the only one available, because a cron
task deposits no `role='user'` row for the evidence rung to find.

The classes below the fix are controls: every source type that *does* originate
on a surface has to come out of the expansion exactly as it did before, and the
email-reply path is the function's main consumer today.
"""

import asyncio
from unittest.mock import patch

import pytest

from istota import db
from istota.config import (
    Config,
    EmailConfig,
    NextcloudConfig,
    SchedulerConfig,
    TalkConfig,
)
from istota.transport.routing import resolve_delivery_plan

# Obviously fabricated tokens throughout (ISSUE-514): nothing here is a real
# Nextcloud Talk conversation token.
WEB_ROOM = "web-alice-000000000001"
TALK_ROOM = "talk-fake-room-1"
PROMOTED_ROOM = "web-alice-000000000002"
PROMOTED_TALK_REF = "talk-fake-room-2"


def _inline_run_coro(coro, *, timeout=None):
    """Run a delivery coroutine here instead of on the process-global runtime.

    `process_one_task` delivers through `async_runtime.run_coro`, which starts
    the persistent ``async-runtime`` loop thread in whichever xdist worker runs
    the test. `tests/conftest.py` resets that singleton around every test, but
    the coroutines reached from here open their own database connections and
    thread-pool workers inside it, and what survives perturbs the wall-clock
    and concurrency tests sharing the worker — a full run then fails one
    unrelated test, a different one each time, each green in isolation. That is
    the shared-worker-state signature, not a flake.

    A fresh loop that is never installed as the thread's current loop and is
    closed straight after leaves no global state behind, and the delivery still
    really runs — which the system-note case below depends on, since the row it
    asserts is written inside `WebTransport.deliver`.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        # What `asyncio.run` does on the way out and `close()` alone does not:
        # the default executor is a thread pool the loop spawns lazily for
        # `to_thread`, and closing the loop neither joins nor discards it.
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
        finally:
            loop.close()


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "istota.db"
    db.init_db(cfg.db_path)
    return cfg


def _task(**kwargs):
    defaults = dict(
        id=1, status="pending", source_type="scheduled", user_id="alice",
        prompt="x", conversation_token=None, priority=5,
        attempt_count=0, max_attempts=3,
    )
    defaults.update(kwargs)
    return db.Task(**defaults)


def _web_only_room(config):
    with db.get_db(config.db_path) as conn:
        db.register_room(conn, WEB_ROOM, "alice", origin="web")
        db.add_room_binding(conn, WEB_ROOM, "web", WEB_ROOM)


def _talk_room(config):
    with db.get_db(config.db_path) as conn:
        db.register_room(conn, TALK_ROOM, "alice", origin="talk")
        db.add_room_binding(conn, TALK_ROOM, "talk", TALK_ROOM)


def _promoted_room(config):
    with db.get_db(config.db_path) as conn:
        db.register_room(conn, PROMOTED_ROOM, "alice", origin="web")
        db.add_room_binding(conn, PROMOTED_ROOM, "web", PROMOTED_ROOM)
        db.add_room_binding(conn, PROMOTED_ROOM, "talk", PROMOTED_TALK_REF)


def _legs(plan):
    return {(d.surface, d.channel, d.kind) for d in plan}


class TestASourceTypeThatOriginatesNowhere:
    """The reported failure, one room shape at a time."""

    def test_a_scheduled_job_reaches_a_web_only_rooms_web_leg(self, config):
        _web_only_room(config)
        task = _task(source_type="scheduled", conversation_token=WEB_ROOM,
                     output_target=f"room:{WEB_ROOM}")
        plan = resolve_delivery_plan(config, task, None)
        assert ("web", WEB_ROOM, "push") in _legs(plan), (
            f"a scheduled job targeting its own web room planned {_legs(plan)}"
        )

    def test_a_scheduled_job_reaches_a_talk_rooms_talk_leg(self, config):
        _talk_room(config)
        task = _task(source_type="scheduled", conversation_token=TALK_ROOM,
                     output_target=f"room:{TALK_ROOM}")
        plan = resolve_delivery_plan(config, task, None)
        assert ("talk", TALK_ROOM, "push") in _legs(plan), _legs(plan)

    def test_a_scheduled_job_reaches_both_legs_of_a_promoted_room(self, config):
        """The shape `room:` exists for: one descriptor, re-expanded live.

        `room_target_descriptor` makes a human assemble `web:<token>,talk:<ref>`
        by hand, so a room promoted to Talk *after* the job was written keeps
        delivering to the web leg alone. The `room` form re-reads the bindings at
        every delivery, which is the whole point of it.
        """
        _promoted_room(config)
        task = _task(source_type="scheduled", conversation_token=PROMOTED_ROOM,
                     output_target=f"room:{PROMOTED_ROOM}")
        plan = resolve_delivery_plan(config, task, None)
        legs = _legs(plan)
        assert ("web", PROMOTED_ROOM, "push") in legs, legs
        assert ("talk", PROMOTED_TALK_REF, "push") in legs, legs

    def test_no_emitted_leg_is_marked_a_mirror(self, config):
        """`mirror` is a relation to an origin leg, so a task with no origin
        has none. It is not a synonym for "produced by the fan-out".

        All three readers of the flag treat it as "this duplicates a delivery
        that happened somewhere else". The one that bites is the scheduler's
        undelivered-result arm, covered below: it drops the inbox row and the
        alert for a Talk post that came back `None`. For a web-origin task
        whose answer already streamed that is right; for an originless `room:`
        expansion the Talk leg is the whole plan and the answer is lost.
        """
        _promoted_room(config)
        task = _task(source_type="scheduled", conversation_token=PROMOTED_ROOM,
                     output_target=f"room:{PROMOTED_ROOM}")
        plan = resolve_delivery_plan(config, task, None)
        assert plan and not any(d.mirror for d in plan), [
            (d.surface, d.mirror) for d in plan
        ]

    def test_a_web_origin_mirror_leg_is_still_a_mirror(self, config):
        """Control for the above. Flipping the flag off unconditionally would
        pass that test and silently un-suppress a web-origin confirmation's
        Talk cross-post, which the gate at `scheduler.py` reads negated."""
        _promoted_room(config)
        task = _task(source_type="web", conversation_token=PROMOTED_ROOM,
                     output_target="room")
        plan = resolve_delivery_plan(config, task, None)
        talk = next(d for d in plan if d.surface == "talk")
        assert talk.mirror is True

    def test_a_room_naming_no_registered_room_is_logged(self, config, caplog):
        """The one silent exit, and this fix removed its only other signal.

        `room` is no longer an unknown surface at cron-load time, so a mistyped
        or non-canonical token reproduces the reported symptom exactly — job
        reports success, nothing delivered — with nothing anywhere saying so.
        """
        task = _task(source_type="scheduled", conversation_token="no-such-room",
                     output_target="room:no-such-room")
        with caplog.at_level("WARNING"):
            assert resolve_delivery_plan(config, task, None) == []
        assert [
            r for r in caplog.records if "names no registered room" in r.getMessage()
        ], [r.getMessage() for r in caplog.records]

    @pytest.mark.parametrize(
        "source_type", ["scheduled", "subtask", "heartbeat", "cli", "doctor", ""],
    )
    def test_every_originless_source_type_reaches_the_room(
        self, config, source_type,
    ):
        """Not just `scheduled`. `registry._surface_for_source_type` answered
        `"talk"` for all of these and for the empty string besides, so each one
        had the same two skips fire over a leg that never existed. `subtask` is
        the second reachable producer: `scheduler_deferred` passes a
        model-written `output_target` straight through.
        """
        _web_only_room(config)
        task = _task(source_type=source_type, conversation_token=WEB_ROOM,
                     output_target=f"room:{WEB_ROOM}")
        plan = resolve_delivery_plan(config, task, None)
        assert ("web", WEB_ROOM, "push") in _legs(plan), _legs(plan)

    def test_a_room_that_went_away_still_plans_nothing(self, config):
        """The `None` reading must not become "emit something regardless"."""
        task = _task(source_type="scheduled", conversation_token="no-such-room",
                     output_target="room:no-such-room")
        assert resolve_delivery_plan(config, task, None) == []

    def test_an_archived_room_is_still_skipped(self, config):
        _web_only_room(config)
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE rooms SET archived = 1 WHERE token = ?",
                         (WEB_ROOM,))
            conn.commit()
        task = _task(source_type="scheduled", conversation_token=WEB_ROOM,
                     output_target=f"room:{WEB_ROOM}")
        assert resolve_delivery_plan(config, task, None) == []


class TestTheOriginBearingSourceTypesAreUnchanged:
    """Controls. `_expand_room_destinations` serves every `room` destination on
    every source type, and the email-reply path is its main consumer today."""

    def test_an_email_reply_still_pushes_to_talk_and_never_to_web(self, config):
        """The main consumer, and the one a wrong reading of `None` would break.

        `origin_descriptor` stamps `room:<canonical>` onto `sent_emails.
        origin_target`, and an inbound reply reads it back. `email` is a room
        *guest*: it owns no binding, so the Talk leg is a non-origin push and the
        web leg is skipped as the canonical view whose row `_store_room_turn`
        writes. Pushing to the web binding as well renders the answer a second
        time as a `role='system'` note (ISSUE-164).
        """
        _promoted_room(config)
        task = _task(source_type="email", conversation_token=PROMOTED_ROOM,
                     output_target=f"room:{PROMOTED_ROOM},email")
        plan = resolve_delivery_plan(config, task, None)
        legs = _legs(plan)
        assert ("talk", PROMOTED_TALK_REF, "push") in legs, legs
        assert not [
            d for d in plan if d.surface == "web" and d.kind == "push"
        ], legs
        assert [d.surface for d in plan].count("email") == 1, legs

    def test_a_web_origin_task_still_mirrors_only_to_talk(self, config):
        _promoted_room(config)
        task = _task(source_type="web", conversation_token=PROMOTED_ROOM,
                     output_target="room")
        plan = resolve_delivery_plan(config, task, None)
        legs = _legs(plan)
        assert ("web", "stream", "stream") in legs, legs
        assert ("talk", PROMOTED_TALK_REF, "push") in legs, legs
        assert not [
            d for d in plan if d.surface == "web" and d.kind == "push"
        ], legs

    def test_a_talk_origin_task_still_pushes_nothing_to_its_web_binding(
        self, config,
    ):
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, TALK_ROOM, "alice", origin="talk")
            db.add_room_binding(conn, TALK_ROOM, "talk", TALK_ROOM)
            db.add_room_binding(conn, TALK_ROOM, "web", TALK_ROOM)
        task = _task(source_type="talk", conversation_token=TALK_ROOM,
                     output_target="room")
        plan = resolve_delivery_plan(config, task, None)
        assert [d.surface for d in plan] == ["talk"], _legs(plan)

    def test_an_istota_file_task_now_reaches_the_rooms_talk_leg(self, config):
        """The one source type that moves without becoming `None`.

        `istota_file` is a real surface name, so the origin map answers
        `"istota_file"` where the delivery map answered `"talk"`. Its origin
        delivery writes TASKS.md and posts to Talk not at all, so the room's
        Talk binding was being skipped as a leg that had delivered nothing.
        """
        _promoted_room(config)
        task = _task(source_type="istota_file", conversation_token=PROMOTED_ROOM,
                     output_target=f"room:{PROMOTED_ROOM}")
        plan = resolve_delivery_plan(config, task, None)
        legs = _legs(plan)
        assert ("istota_file", None, "push") in legs, legs
        assert ("talk", PROMOTED_TALK_REF, "push") in legs, legs

    def test_a_briefing_naming_another_room_reaches_both(self, config):
        """Deferred from review, pinned rather than fixed.

        `_infer_default_plan` is prepended as the "origin delivery", and for a
        briefing it is a source-type *default* (`talk`, channel-less) rather
        than an origin leg. Where the target names room A while the task's own
        channel is room B, the default resolves to B's Talk binding and the
        expansion adds A's, so the briefing reaches both.

        Not a regression: B was always reached, and A is what the user asked
        for, so the fix moves this strictly closer to correct. Not fixed here
        because both available fixes have a blast radius of their own — the
        early returns hand `dests` back so a briefing whose room went away still
        falls through to the user's notification channel, and an email
        continuation's stored `room:<token>` descriptor leans on the same
        prepend for its email leg.
        """
        _talk_room(config)
        _promoted_room(config)
        task = _task(source_type="briefing", conversation_token=TALK_ROOM,
                     output_target=f"room:{PROMOTED_ROOM}")
        plan = resolve_delivery_plan(config, task, None)
        channels = {d.channel for d in plan if d.surface == "talk"}
        assert channels == {TALK_ROOM, PROMOTED_TALK_REF}, channels

    def test_a_briefing_does_not_plan_its_talk_leg_twice(self, config):
        """`briefing` is the one originless source type with a non-empty default
        plan, so the bare `talk` origin delivery and the room's own talk binding
        both reach the resolver. They dedup on `(surface, channel)`."""
        _talk_room(config)
        task = _task(source_type="briefing", conversation_token=TALK_ROOM,
                     output_target=f"room:{TALK_ROOM}")
        plan = resolve_delivery_plan(config, task, None)
        assert [d.surface for d in plan].count("talk") == 1, _legs(plan)


class TestTheAnswerReachesTheRoomsTranscript:
    """The scheduler half. The entry claims "nothing reaches the transcript
    either" and names both rungs of `_room_turn_belongs_here` as failing. The
    evidence rung stays failing — a cron task deposits no `role='user'` row —
    so the transcript row depends entirely on the first rung, which reads
    `own_room_canonical_dests`. That list is empty unless the expansion emits
    the room's canonical (web) binding, which is the half of the fix that would
    be easy to leave out.
    """

    def _config(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud=NextcloudConfig(
                url="https://nc.example.invalid", username="istota",
                app_password="secret",
            ),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(),
            workspace_path=mount,
            temp_dir=tmp_path / "temp",
        )
        db.init_db(cfg.db_path)
        return cfg

    def _run(self, config, token, *, set_room=True):
        from istota.scheduler import process_one_task

        with db.get_db(config.db_path) as conn:
            db.register_room(conn, token, "alice", origin="web")
            db.add_room_binding(conn, token, "web", token)
            task_id = db.create_task(
                conn, prompt="the weekly digest", user_id="alice",
                source_type="scheduled",
                conversation_token=token if set_room else None,
                output_target=f"room:{token}",
            )
        with patch("istota.scheduler.execute_task",
                   return_value=(True, "Monday: three things.", None, None)), \
                patch("istota.scheduler.run_coro", _inline_run_coro), \
                patch("istota.scheduler.asyncio.run", return_value=None):
            assert process_one_task(config) is not None
        return task_id

    def test_a_scheduled_job_lands_as_an_assistant_turn_in_its_room(
        self, tmp_path,
    ):
        config = self._config(tmp_path)
        task_id = self._run(config, WEB_ROOM)
        with db.get_db(config.db_path) as conn:
            rows = [
                (m.role, m.body, m.origin_surface)
                for m in db.get_messages(conn, WEB_ROOM)
            ]
            assert db.get_task(conn, task_id).status == "completed"
        assert rows == [
            ("assistant", "Monday: three things.", "scheduled")
        ], rows

    def test_it_is_a_turn_rather_than_an_unsolicited_system_note(
        self, tmp_path,
    ):
        """The discriminator. A web push that is *not* recognised as the task's
        own room goes down `web_foreign_dests` and lands as `role='system'`,
        which the web renderer draws as command output rather than as a reply
        (ISSUE-164). Asserting only that "something arrived" passes in both
        states.
        """
        config = self._config(tmp_path)
        self._run(config, WEB_ROOM)
        with db.get_db(config.db_path) as conn:
            roles = [m.role for m in db.get_messages(conn, WEB_ROOM)]
        # Both halves, or an empty room passes this vacuously — which is the
        # pre-fix state.
        assert roles == ["assistant"], roles

    def test_without_room_set_the_answer_arrives_as_a_system_note(
        self, tmp_path,
    ):
        """`target` alone is not enough, and the docs say to pair it with `room`.

        `room` in CRON.md is the job's `conversation_token`, and it is what
        `transcript_room_for_task` resolves — so without it there is no
        transcript room, the web leg falls into `web_foreign_dests` rather than
        `own_room_canonical_dests`, and `WebTransport.deliver` writes the
        answer as an unsolicited `role='system'` note. Delivered rather than
        lost, which is the whole of ISSUE-511, and not the reply a reader
        wants. Pinned rather than fixed: making a target imply a room is a
        change to what `room` means, not to this defect.
        """
        config = self._config(tmp_path)
        self._run(config, WEB_ROOM, set_room=False)
        with db.get_db(config.db_path) as conn:
            rows = [(m.role, m.body) for m in db.get_messages(conn, WEB_ROOM)]
        assert [r for r, _ in rows] == ["system"], rows
        assert "Monday: three things." in rows[0][1], rows


class TestALostAnswerIsStillReported:
    """The `mirror` flag's third reader, and the one the first draft of this fix
    got wrong. `scheduler.py`'s undelivered-result arm suppresses the inbox row
    and the alert for a Talk post that came back `None` when the Talk leg is a
    mirror — right for a web-origin task, whose answer already streamed and
    whose canonical row is written, and wrong for an originless `room:`
    expansion where the Talk leg is the entire plan.
    """

    def _config(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud=NextcloudConfig(
                url="https://nc.example.invalid", username="istota",
                app_password="secret",
            ),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(),
            workspace_path=mount,
            temp_dir=tmp_path / "temp",
        )
        db.init_db(cfg.db_path)
        return cfg

    def _run_with_talk_down(self, config, source_type):
        """`asyncio.run` returning None is what a Talk post that delivered
        nothing looks like here — `TalkTransport.deliver` swallows the
        exception and returns None (ISSUE-404)."""
        from istota.scheduler import process_one_task

        with db.get_db(config.db_path) as conn:
            db.register_room(conn, TALK_ROOM, "alice", origin="talk")
            db.add_room_binding(conn, TALK_ROOM, "talk", TALK_ROOM)
            db.create_task(
                conn, prompt="the weekly digest", user_id="alice",
                source_type=source_type, conversation_token=TALK_ROOM,
                output_target=f"room:{TALK_ROOM}",
            )
        with patch("istota.scheduler.execute_task",
                   return_value=(True, "Monday: three things.", None, None)), \
                patch("istota.scheduler.asyncio.run", return_value=None), \
                patch("istota.scheduler.send_notification", return_value=False), \
                patch("istota.scheduler.run_coro", return_value=None):
            assert process_one_task(config) is not None

    def test_a_scheduled_jobs_failed_talk_post_leaves_an_inbox_row(
        self, tmp_path,
    ):
        config = self._config(tmp_path)
        self._run_with_talk_down(config, "scheduled")
        with db.get_db(config.db_path) as conn:
            rows = conn.execute(
                "SELECT title, body FROM notifications WHERE user_id = ?",
                ("alice",),
            ).fetchall()
        assert [r["title"] for r in rows if "Could not post to Talk" in
                (r["title"] or "")], (
            "a scheduled job whose only delivery leg failed left no record; "
            f"notifications held {[r['title'] for r in rows]}"
        )


class TestTheCronLoaderStopsCallingItATypo:
    """`room` was correctly absent from `_KNOWN_TARGET_SURFACES` only while the
    warning was the sole signal that the job was broken. A working descriptor
    that warns on every sync is worse than no warning at all.
    """

    def test_a_room_target_no_longer_warns(self, caplog):
        from istota.cron_loader import _validate_target

        with caplog.at_level("WARNING"):
            _validate_target("digest", "alice", f"room:{WEB_ROOM}")
        assert not [
            r for r in caplog.records if "not recognized" in r.getMessage()
        ], [r.getMessage() for r in caplog.records]

    def test_a_bare_room_target_no_longer_warns(self, caplog):
        from istota.cron_loader import _validate_target

        with caplog.at_level("WARNING"):
            _validate_target("digest", "alice", "room")
        assert not [
            r for r in caplog.records if "not recognized" in r.getMessage()
        ], [r.getMessage() for r in caplog.records]

    def test_a_room_target_without_a_matching_room_field_warns(self, caplog):
        """Finding from review. The job half works: it delivers, and the answer
        arrives as a standalone note rather than as a turn in the room, which is
        invisible from the file. `TestTheAnswerReachesTheRoomsTranscript` pins
        the behaviour; this is what tells the user about it."""
        from istota.cron_loader import _validate_room_pairing

        with caplog.at_level("WARNING"):
            _validate_room_pairing("digest", "alice", f"room:{WEB_ROOM}", "")
        assert [
            r for r in caplog.records if "standalone note" in r.getMessage()
        ], [r.getMessage() for r in caplog.records]

    def test_a_matching_room_field_is_quiet(self, caplog):
        """Control. Without it, warning unconditionally would pass above."""
        from istota.cron_loader import _validate_room_pairing

        with caplog.at_level("WARNING"):
            _validate_room_pairing(
                "digest", "alice", f"room:{WEB_ROOM}", WEB_ROOM,
            )
            _validate_room_pairing("digest", "alice", "talk", "")
            _validate_room_pairing("digest", "alice", "room", "")
        assert not caplog.records, [r.getMessage() for r in caplog.records]

    def test_a_genuine_typo_still_warns(self, caplog):
        """Control. Without it, emptying the set entirely would pass above."""
        from istota.cron_loader import _validate_target

        with caplog.at_level("WARNING"):
            _validate_target("digest", "alice", "rooom:whatever")
        assert [
            r for r in caplog.records if "not recognized" in r.getMessage()
        ], "an unknown surface stopped warning"


class TestTheTwoSourceTypeMapsStayApart:
    """`.claude/rules/leaf-modules.md` and `surfaces.origin_surface_for_source_
    type`'s own docstring both say these are two questions. This is the site
    where confusing them had a consequence, so pin that the delivery map is
    still the one `_resolve_one` asks — it is what routes a scheduled task's web
    leg down the foreign-push branch instead of short-circuiting it to a stream
    no-op.
    """

    def test_the_expansion_asks_the_origin_map(self):
        """Both halves have to look at the *call*, not at the import.

        The import moved to module scope with the fix, so a guard phrased on
        the import line would be looking at a place the delivery map need never
        reappear in. And a bare `"_surface_for_source_type(" not in body` is
        unusable: it is a substring of `origin_surface_for_source_type(`, so it
        would fail against the correct code. Hence the lookbehind.
        """
        import re

        from tests.support.drift import source_of

        from istota.transport import routing

        body = source_of(routing._expand_room_destinations)
        assert re.search(r"\borigin_surface_for_source_type\(", body), (
            "the expansion is not asking the origin map"
        )
        assert not re.search(r"(?<!origin)_surface_for_source_type\(", body), (
            "the expansion is calling the delivery map again"
        )

    def test_the_stream_short_circuit_still_asks_the_delivery_map(self):
        from tests.support.drift import source_of

        from istota.transport import routing

        body = source_of(routing._resolve_one)
        assert "_surface_for_source_type(" in body, (
            "the stream short-circuit must keep asking the delivery map: it is "
            "what sends a scheduled task's web leg down the foreign-push branch"
        )
