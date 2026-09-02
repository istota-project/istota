"""The two resolvers registered by this stage, and the two ways a row closes.

Resolvers are a **backstop**, not the primary close path. Every producer closes
its own rows when it closes the object, so each source gets two cases:

1. close the object through the real producer verb and assert the row went
   `resolved` without anyone reading the panel;
2. close it behind the store's back and assert the next `list_open` sees the
   resolver return `None` and flips the row to `stale`.

The second is the anti-staleness story in full: approving a confirmation over
Talk changes `tasks.status`, and the panel must never render a question that has
already been answered somewhere else.
"""

from __future__ import annotations

import pytest

from istota import (
    confirmations,
    db,
    notification_sources as sources,
    notification_store as store,
    outbound_drafts as drafts,
)
from istota.config import Config, UserConfig
from istota.notification_resolvers import confirmation as confirmation_source
from istota.notification_resolvers import connected_service as service_source
from istota.notification_resolvers import cron_job as cron_source
from istota.notification_resolvers import outbound_draft as draft_source


@pytest.fixture(autouse=True)
def _registry():
    sources.reset_registry()
    yield
    sources.reset_registry()


@pytest.fixture
def config(tmp_path):
    return Config(
        db_path=tmp_path / "test.db",
        nextcloud_mount_path=tmp_path / "mount",
        users={"alice": UserConfig(display_name="Alice")},
    )


@pytest.fixture
def conn(config):
    db.init_db(config.db_path)
    with db.get_db(config.db_path) as c:
        yield c


def _state(conn, source):
    row = conn.execute(
        "SELECT state, resolved_by FROM notifications WHERE source = ?", (source,),
    ).fetchone()
    return (row["state"], row["resolved_by"]) if row else (None, None)


def _held_task(conn, prompt="Shall I proceed?"):
    task_id = db.create_task(
        conn, prompt="do the thing", user_id="alice", source_type="web",
        conversation_token="room-1",
    )
    db.set_task_confirmation(conn, task_id, prompt)
    confirmation_source.write(conn, "alice", task_id=task_id, title=prompt)
    return task_id


def _held_draft(conn, to="someone@example.invalid"):
    draft_id = drafts.hold(
        conn, user_id="alice", task_id=None, room_token=None,
        to_addrs=[to], cc_addrs=[], bcc_addrs=[],
        subject="Re: hello", body="the reply", html=False,
        in_reply_to=None, references=None, attachments=[],
        origin_target=None, hold_reason="untrusted_recipient",
    )
    draft_source.write(
        conn, "alice", draft_id=draft_id,
        title=f"Email reply to {to} is waiting for your approval",
    )
    return draft_id


# ---------------------------------------------------------------------------
# the keys, which stage 4's backfill depends on verbatim
# ---------------------------------------------------------------------------


def test_the_dedup_keys_are_exactly_what_the_backfill_will_generate(conn):
    task_id = _held_task(conn)
    draft_id = _held_draft(conn)
    rows = dict(conn.execute(
        "SELECT source, dedup_key FROM notifications",
    ).fetchall())
    assert rows == {
        "confirmation": f"task:{task_id}",
        "outbound_draft": f"draft:{draft_id}",
    }
    assert confirmation_source.dedup_key(task_id) == f"task:{task_id}"
    assert draft_source.dedup_key(draft_id) == f"draft:{draft_id}"


def test_a_second_write_for_the_same_object_is_one_row(conn):
    task_id = _held_task(conn)
    confirmation_source.write(
        conn, "alice", task_id=task_id, title="Shall I proceed?",
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE source = 'confirmation'",
    ).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# confirmation
# ---------------------------------------------------------------------------


class TestConfirmationResolver:
    def test_the_open_row_renders_confirm_and_discard(self, config, conn):
        task_id = _held_task(conn)
        items, total = store.list_open(config, conn, "alice")
        assert total == 1 and len(items) == 1
        item = items[0]
        assert item.actionable is True
        endpoints = {a.id: a.endpoint for a in item.actions}
        assert endpoints == {
            "confirm": f"/chat/tasks/{task_id}/confirm",
            "discard": f"/chat/tasks/{task_id}/cancel",
        }

    def test_approving_closes_the_row_without_a_panel_read(self, config, conn):
        task_id = _held_task(conn)
        task = db.get_task(conn, task_id)
        confirmations.approve(conn, task, config=config, by="web")
        assert _state(conn, "confirmation") == ("resolved", "web")
        items, total = store.list_open(config, conn, "alice")
        assert items == [] and total == 0

    def test_declining_closes_the_row(self, config, conn):
        task_id = _held_task(conn)
        task = db.get_task(conn, task_id)
        confirmations.decline(conn, task, by="talk")
        assert _state(conn, "confirmation") == ("resolved", "talk")

    def test_a_row_left_open_over_an_answered_task_goes_stale(self, config, conn):
        """The backstop: the object moved on and nobody closed the row."""
        task_id = _held_task(conn)
        db.confirm_task(conn, task_id)
        assert _state(conn, "confirmation") == ("open", None)

        items, total = store.list_open(config, conn, "alice")
        assert items == [] and total == 0
        assert _state(conn, "confirmation") == ("stale", "system")

    def test_a_row_for_a_task_that_no_longer_exists_goes_stale(self, config, conn):
        _held_task(conn)
        conn.execute("DELETE FROM tasks")
        items, _total = store.list_open(config, conn, "alice")
        assert items == []
        assert _state(conn, "confirmation")[0] == "stale"

    def test_a_scheduler_parked_email_task_shows_its_own_question(
        self, config, conn,
    ):
        """The two producers are indistinguishable from the row.

        An email-origin task whose *answer* asks a question parks exactly like
        any other, so `source_type == "email"` does not mean "held by the
        inbound gate". A resolver branching on it renders the gate's wording —
        "nothing has been run, and the message body is not shown" — over a task
        that ran to completion and asked something of its own.
        """
        task_id = db.create_task(
            conn, prompt="<email_metadata>…</email_metadata> do the thing",
            user_id="alice", source_type="email", conversation_token="thread-abc",
        )
        question = "I need your confirmation before deleting three files."
        db.set_task_confirmation(conn, task_id, question)
        confirmation_source.write(
            conn, "alice", task_id=task_id,
            title=confirmations.describe_prompt(question),
            body=confirmation_source.body_for(question),
        )

        items, _total = store.list_open(config, conn, "alice")
        assert len(items) == 1
        assert "deleting three files" in items[0].body
        assert "not shown" not in items[0].body
        assert "do the thing" not in items[0].body

    def test_the_gates_stored_body_is_what_the_resolver_recomputes(
        self, config, conn,
    ):
        """Stored and rendered come from one function, so they cannot drift."""
        task_id = db.create_task(
            conn, prompt="untrusted", user_id="alice", source_type="email",
        )
        gate_message = "Email from unknown sender x@example.invalid\nSubject: Hi"
        db.set_task_confirmation(conn, task_id, gate_message)
        confirmation_source.write(
            conn, "alice", task_id=task_id, title="email from x — Hi",
            body=confirmation_source.body_for(gate_message),
        )
        stored = conn.execute(
            "SELECT body FROM notifications WHERE dedup_key = ?",
            (f"task:{task_id}",),
        ).fetchone()["body"]
        items, _total = store.list_open(config, conn, "alice")
        assert items[0].body == stored

    def test_a_row_naming_another_users_task_is_never_rendered(self, config, conn):
        """`object_id` is a value on the row, so the resolver re-checks the owner."""
        task_id = db.create_task(
            conn, prompt="bob's task", user_id="bob", source_type="web",
        )
        db.set_task_confirmation(conn, task_id, "Shall I proceed?")
        confirmation_source.write(
            conn, "alice", task_id=task_id, title="Shall I proceed?",
        )
        items, _total = store.list_open(config, conn, "alice")
        assert items == []


# ---------------------------------------------------------------------------
# outbound_draft
# ---------------------------------------------------------------------------


class TestOutboundDraftResolver:
    def test_the_open_row_renders_approve_and_discard(self, config, conn):
        draft_id = _held_draft(conn)
        items, total = store.list_open(config, conn, "alice")
        assert total == 1 and len(items) == 1
        endpoints = {a.id: a.endpoint for a in items[0].actions}
        assert endpoints == {
            "approve": f"/chat/drafts/{draft_id}/approve",
            "discard": f"/chat/drafts/{draft_id}/discard",
        }

    def test_discarding_closes_the_row(self, config, conn):
        draft_id = _held_draft(conn)
        drafts.discard(conn, draft_id)
        assert _state(conn, "outbound_draft") == ("resolved", "system")
        items, total = store.list_open(config, conn, "alice")
        assert items == [] and total == 0

    def test_a_row_left_open_over_a_sent_draft_goes_stale(self, config, conn):
        draft_id = _held_draft(conn)
        conn.execute(
            "UPDATE outbound_drafts SET status = 'sent', sent_message_id = ? "
            "WHERE id = ?", ("<x@example.invalid>", draft_id),
        )
        items, total = store.list_open(config, conn, "alice")
        assert items == [] and total == 0
        assert _state(conn, "outbound_draft")[0] == "stale"

    def test_a_draft_mid_send_renders_with_a_status_note_and_no_actions(
        self, config, conn,
    ):
        """`status_note` is what separates this from "nobody registered this"."""
        draft_id = _held_draft(conn)
        conn.execute(
            "UPDATE outbound_drafts SET status = 'sending' WHERE id = ?", (draft_id,),
        )
        items, _total = store.list_open(config, conn, "alice")
        assert len(items) == 1
        assert items[0].actions == ()
        assert items[0].status_note
        assert _state(conn, "outbound_draft")[0] == "open"

    def test_a_row_naming_another_users_draft_is_never_rendered(self, config, conn):
        draft_id = drafts.hold(
            conn, user_id="bob", task_id=None, room_token=None,
            to_addrs=["x@example.invalid"], cc_addrs=[], bcc_addrs=[],
            subject="s", body="b", html=False,
            in_reply_to=None, references=None, attachments=[],
            origin_target=None, hold_reason="untrusted_recipient",
        )
        draft_source.write(conn, "alice", draft_id=draft_id, title="waiting")
        items, _total = store.list_open(config, conn, "alice")
        assert items == []


# ---------------------------------------------------------------------------
# cron_job
# ---------------------------------------------------------------------------


def _disabled_job(conn, *, name="nightly-digest", failures=5, user="alice"):
    """A job in the state the three suspend sites leave behind.

    `enabled` stays 1: the user never asked for this job to stop, and writing
    their column is what let the next CRON.md sync undo the suspension.
    """
    conn.execute(
        "INSERT INTO scheduled_jobs "
        "(user_id, name, cron_expression, prompt, enabled, consecutive_failures, "
        " last_error, auto_disabled_at) "
        "VALUES (?, ?, ?, ?, 1, ?, ?, datetime('now'))",
        (user, name, "0 7 * * *", "summarise the news", failures, "boom"),
    )
    job_id = conn.execute(
        "SELECT id FROM scheduled_jobs WHERE user_id = ? AND name = ?",
        (user, name),
    ).fetchone()[0]
    cron_source.write(
        conn, user, job_id=job_id, job_name=name, fail_count=failures,
        cron_expression="0 7 * * *", last_error="boom",
    )
    return job_id


class TestCronJobResolver:
    def test_the_row_appears_with_the_producers_key(self, conn):
        job_id = _disabled_job(conn)
        row = conn.execute(
            "SELECT user_id, dedup_key, object_type, object_id, actionable, state "
            "FROM notifications WHERE source = 'cron_job'",
        ).fetchone()
        assert tuple(row) == ("alice", f"job:{job_id}", "scheduled_job",
                              str(job_id), 1, "open")
        assert cron_source.dedup_key(job_id) == f"job:{job_id}"

    def test_the_open_row_renders_the_job_and_says_how_to_re_enable_it(
        self, config, conn,
    ):
        """No POST endpoint re-enables a job, so the note carries the verb."""
        _disabled_job(conn)
        items, total = store.list_open(config, conn, "alice")
        assert total == 1 and len(items) == 1
        item = items[0]
        assert "nightly-digest" in item.title
        assert "boom" in item.body
        assert item.actions == ()
        assert "!cron enable nightly-digest" in item.status_note
        # No action to take *in the panel*, so it must not be filed under
        # "Needs action" — the store's own rule, asserted at the source.
        assert item.actionable is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("verb", ["enable", "disable"])
    async def test_the_cron_command_closes_the_row(self, config, conn, verb):
        """Both verbs, driven through the real handler.

        `disable` is the one the resolver cannot cover: disabling by hand writes
        the user's column and leaves the scheduler's `auto_disabled_at` where it
        was, so the row would keep telling the user to re-enable a job they have
        just switched off — forever, since object-backed rows are never
        age-swept.
        """
        from istota.commands import CommandContext, cmd_cron

        _disabled_job(conn, name="nightly-digest")

        result = await cmd_cron(CommandContext(
            config=config, conn=conn, user_id="alice",
            conversation_token="room1", args=f"{verb} nightly-digest",
            surface="web",
        ))

        assert "nightly-digest" in result
        assert _state(conn, "cron_job") == ("resolved", "web")

    def test_a_row_left_open_over_a_recovered_job_goes_stale(self, config, conn):
        """The backstop: the suspension lifted and nobody closed the row."""
        job_id = _disabled_job(conn)
        db.reset_scheduled_job_failures(conn, job_id)
        assert _state(conn, "cron_job") == ("open", None)

        items, total = store.list_open(config, conn, "alice")
        assert items == [] and total == 0
        assert _state(conn, "cron_job") == ("stale", "system")

    def test_a_row_stays_open_while_the_job_is_suspended(self, config, conn):
        """The condition has not ended, so neither has the row.

        Stated against a full CRON.md sync tick rather than a hand-written
        UPDATE, because the tick is what used to end this row prematurely: it
        writes `enabled` from the file and, before the column split, that was
        the whole state a resolver could read.
        """
        from istota.cron_loader import CronJob, sync_cron_jobs_to_db

        _disabled_job(conn)
        sync_cron_jobs_to_db(
            conn, "alice",
            [CronJob(name="nightly-digest", cron="0 7 * * *",
                     prompt="summarise the news")],
        )

        items, total = store.list_open(config, conn, "alice")
        assert total == 1 and len(items) == 1
        assert _state(conn, "cron_job")[0] == "open"

    def test_a_row_closes_when_the_suspension_lifts(self, config, conn):
        """The third close path, and the one no surface touches.

        The user edits the prompt in CRON.md; the next sync reads that as a fix
        and clears `auto_disabled_at`. Nothing calls `resolve_for_job`, so the
        row closes through the resolver alone — which is exactly what it is for.
        """
        from istota.cron_loader import CronJob, sync_cron_jobs_to_db

        _disabled_job(conn)
        sync_cron_jobs_to_db(
            conn, "alice",
            [CronJob(name="nightly-digest", cron="0 7 * * *",
                     prompt="summarise the news, but shorter")],
        )

        items, total = store.list_open(config, conn, "alice")
        assert items == [] and total == 0
        assert _state(conn, "cron_job")[0] == "stale"

    def test_a_job_the_user_switched_off_by_hand_is_not_the_condition(
        self, config, conn,
    ):
        """`enabled` is not the predicate, in the other direction.

        A job the user disabled has `auto_disabled_at` NULL and is not being
        held back by anything, so there is nothing here to tell them about. The
        `!cron disable` verb closes the row itself; this is what happens if it
        did not.
        """
        job_id = _disabled_job(conn)
        db.reset_scheduled_job_failures(conn, job_id)
        db.disable_scheduled_job(conn, job_id)

        items, _total = store.list_open(config, conn, "alice")
        assert items == []
        assert _state(conn, "cron_job")[0] == "stale"

    def test_a_row_for_a_job_that_no_longer_exists_goes_stale(self, config, conn):
        _disabled_job(conn)
        conn.execute("DELETE FROM scheduled_jobs")
        items, _total = store.list_open(config, conn, "alice")
        assert items == []
        assert _state(conn, "cron_job")[0] == "stale"

    def test_a_row_naming_another_users_job_is_never_rendered(self, config, conn):
        job_id = _disabled_job(conn, user="bob")
        conn.execute("DELETE FROM notifications")
        cron_source.write(
            conn, "alice", job_id=job_id, job_name="nightly-digest", fail_count=5,
        )
        items, _total = store.list_open(config, conn, "alice")
        assert items == []

    def test_every_suspension_is_worth_notifying_about(self):
        """Module jobs included, since ISSUE-391.

        The exclusion rested on the rescue-reopen loop, which is now closed in
        the resolver instead, and on the user having no verb to run against a
        `_module.*` row, which ISSUE-392 retired. `is_module_job` stays: the
        resolver and the note both still branch on it.
        """
        assert cron_source.should_notify("nightly-digest") is True
        assert cron_source.should_notify("_module.health.garmin_sync") is True
        assert cron_source.is_module_job("_module.feeds.poll") is True
        assert cron_source.is_module_job("nightly-digest") is False

    def test_the_module_prefix_is_matched_on_the_raw_name(self):
        """`flatten` maps `_` to a space, so a flattened name never matches."""
        from istota.confirmations import flatten

        name = "_module.health.garmin_sync"
        assert not flatten(name).startswith(cron_source.MODULE_JOB_PREFIX)
        assert cron_source.is_module_job(name) is True

    def test_a_long_job_name_is_capped_in_the_title(self, config, conn):
        """The title reaches ntfy as an HTTP header; an oversized one is refused.

        `confirmations.describe_email` caps both its halves for exactly this
        reason and says so. `flatten` does not truncate — the caller always does.
        """
        _disabled_job(conn, name="d" * 400)
        items, _total = store.list_open(config, conn, "alice")
        stored = conn.execute(
            "SELECT title FROM notifications WHERE source = 'cron_job'",
        ).fetchone()["title"]
        assert len(stored) < 200
        assert items[0].title == stored

    def test_a_job_name_carrying_markup_is_not_printed_as_a_command(
        self, config, conn,
    ):
        """A flattened name is not the string `!cron enable` takes.

        Job names come out of CRON.md as free text and the note is delivered
        into Talk, which renders markdown — so the name is flattened. Printing
        the flattened form beside the verb would be an instruction that does not
        work on the job it names, so the generic form is used instead. The title
        still carries the (flattened, safe) name.
        """
        _disabled_job(conn, name="[click](http://evil.invalid) *digest*")
        items, _total = store.list_open(config, conn, "alice")
        note = items[0].status_note
        assert "`!cron enable <name>`" in note
        assert "http://evil.invalid" not in note
        for ch in "[]()`*_~<>|":
            assert ch not in items[0].title


# ---------------------------------------------------------------------------
# cron_job — the `_module.*` rows (ISSUE-391)
# ---------------------------------------------------------------------------

_MODULE_JOB = "_module.feeds.prune"


def _suspended_module_job(conn, *, name=_MODULE_JOB, failures=5, user="alice"):
    """A module row in the state the three suspend sites leave behind.

    `skip_log_channel = 1` mirrors what `_sync_module_jobs` seeds, and is half
    of why this suspension used to be silent; `should_notify` was the other
    half. Daily cron, because the cadence is what ISSUE-391 was filed about.
    """
    conn.execute(
        "INSERT INTO scheduled_jobs "
        "(user_id, name, cron_expression, prompt, skill, skill_args, enabled, "
        " skip_log_channel, consecutive_failures, last_error, auto_disabled_at) "
        "VALUES (?, ?, ?, '', 'feeds', '[\"prune\"]', 1, 1, ?, ?, datetime('now'))",
        (user, name, "17 3 * * *", failures, "entry_prune_not_before is malformed"),
    )
    return conn.execute(
        "SELECT id FROM scheduled_jobs WHERE user_id = ? AND name = ?",
        (user, name),
    ).fetchone()[0]


def _rescue(conn, job_id):
    """What `_sync_module_jobs` writes an hour after a module row is suspended.

    Spelled out rather than driven through `_sync_feeds_module_jobs`, so the
    assertion is about the three columns the rescue clears and not about which
    module happens to seed a daily job today.
    """
    conn.execute(
        "UPDATE scheduled_jobs SET auto_disabled_at = NULL, "
        "consecutive_failures = 0, last_error = NULL WHERE id = ?",
        (job_id,),
    )


class TestModuleJobSuspensionIsNotSilent:
    """ISSUE-391: a permanently failing `_module.*` job told nobody, anywhere.

    `_sync_module_jobs` seeds every module row `skip_log_channel = 1` and
    `should_notify` excluded the whole prefix, so the only trace of a job that
    failed on every run was `scheduled_jobs.last_error`, which a human had to
    go and read. For `_module.feeds.prune` the symptom is a feeds database that
    grows without bound — the exact condition the job was added to prevent.
    """

    def test_a_suspended_module_job_raises_a_row(self, conn):
        job_id = _suspended_module_job(conn)
        assert cron_source.should_notify(_MODULE_JOB) is True
        cron_source.write(
            conn, "alice", job_id=job_id, job_name=_MODULE_JOB, fail_count=5,
            cron_expression="17 3 * * *", last_error="boom",
        )
        assert _state(conn, "cron_job") == ("open", None)

    def test_the_hourly_rescue_does_not_close_the_row(self, config, conn):
        """The rescue is a retry, not a repair, so it does not end the condition.

        This is the predicate the whole fix turns on. `auto_disabled_at IS NULL`
        closes a CRON.md row because only something that fixed the job writes
        it; on a `_module.*` row `_sync_module_jobs` writes it on a cooldown
        whether or not anything was fixed.
        """
        job_id = _suspended_module_job(conn)
        cron_source.write(
            conn, "alice", job_id=job_id, job_name=_MODULE_JOB, fail_count=5,
            cron_expression="17 3 * * *", last_error="boom",
        )
        _rescue(conn, job_id)

        items, total = store.list_open(config, conn, "alice")
        assert total == 1 and len(items) == 1
        assert _state(conn, "cron_job")[0] == "open"

    def test_the_rescue_reopen_loop_cannot_form(self, config, conn):
        """One push on the first suspension, then silence — the property
        `should_notify`'s exclusion was protecting, kept without the exclusion.

        A row that went `stale` on the rescue would be *reopened* by the next
        suspension, and the reopen branch delivers. Holding it open turns that
        reopen into a bump, which does not. Asserted on `last_delivered_at`
        rather than on `occurrences` alone: the count climbing is the wanted
        half, a second delivery is the half that was the hazard.
        """
        job_id = _suspended_module_job(conn)
        first = cron_source.write(
            conn, "alice", job_id=job_id, job_name=_MODULE_JOB, fail_count=5,
            cron_expression="17 3 * * *", last_error="boom",
        )
        assert first is not None and first.deliver is True

        # Five more days: rescue, re-fail, re-suspend, panel read in between.
        for _cycle in range(3):
            _rescue(conn, job_id)
            store.list_open(config, conn, "alice")
            conn.execute(
                "UPDATE scheduled_jobs SET auto_disabled_at = datetime('now'), "
                "consecutive_failures = 5, last_error = 'boom' WHERE id = ?",
                (job_id,),
            )
            again = cron_source.write(
                conn, "alice", job_id=job_id, job_name=_MODULE_JOB, fail_count=5,
                cron_expression="17 3 * * *", last_error="boom",
            )
            assert again is not None
            assert again.deliver is False, "a bump must not push again"

        row = conn.execute(
            "SELECT state, occurrences FROM notifications WHERE source = 'cron_job'",
        ).fetchone()
        assert row["state"] == "open"
        assert row["occurrences"] == 4

    def test_a_module_row_closes_when_the_job_actually_succeeds(self, config, conn):
        """The backstop, and the only thing that genuinely ends the condition.

        `reset_scheduled_job_failures` is the one writer of `last_success_at`,
        so a success is the one state change the rescue cannot forge — it wipes
        `auto_disabled_at`, `consecutive_failures` and `last_error` and leaves
        that column alone.

        The row is aged a minute first because the comparison is at second
        precision and everything in this test happens inside one second, which
        no real sequence does — the row is raised on a failed run and the
        success is the next fire, minutes to a day later.
        """
        job_id = _suspended_module_job(conn)
        cron_source.write(
            conn, "alice", job_id=job_id, job_name=_MODULE_JOB, fail_count=5,
            cron_expression="17 3 * * *", last_error="boom",
        )
        conn.execute(
            "UPDATE notifications SET updated_at = "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 minute')",
        )
        db.reset_scheduled_job_failures(conn, job_id)

        items, total = store.list_open(config, conn, "alice")
        assert items == [] and total == 0
        assert _state(conn, "cron_job")[0] == "stale"

    def test_a_success_before_the_row_was_raised_does_not_close_it(
        self, config, conn,
    ):
        """The comparison is *since the row*, not *ever*.

        A module job that worked for months and then broke has
        `last_success_at` set, and after the rescue it also has zero failures
        and a null `auto_disabled_at` — the whole row state is back to what a
        healthy job looks like. Reading `last_success_at IS NOT NULL` alone
        would close exactly the row this fix exists to keep open.
        """
        job_id = _suspended_module_job(conn)
        conn.execute(
            "UPDATE scheduled_jobs SET last_success_at = datetime('now', '-30 days') "
            "WHERE id = ?", (job_id,),
        )
        cron_source.write(
            conn, "alice", job_id=job_id, job_name=_MODULE_JOB, fail_count=5,
            cron_expression="17 3 * * *", last_error="boom",
        )
        _rescue(conn, job_id)

        items, total = store.list_open(config, conn, "alice")
        assert total == 1 and len(items) == 1

    def test_the_rendered_row_survives_the_rescue_wiping_the_evidence(
        self, config, conn,
    ):
        """The rescue clears `consecutive_failures` and `last_error`, so
        recomputing the text from the live job renders "failed 0 times in a row"
        and no error at all — a row whose whole purpose is to carry that error.
        The stored text is what was true at the suspension, and every
        re-suspension refreshes it.
        """
        job_id = _suspended_module_job(conn)
        cron_source.write(
            conn, "alice", job_id=job_id, job_name=_MODULE_JOB, fail_count=5,
            cron_expression="17 3 * * *",
            last_error="prune cutoff is malformed",
        )
        _rescue(conn, job_id)

        items, _total = store.list_open(config, conn, "alice")
        assert "5 times in a row" in items[0].title
        assert "prune cutoff is malformed" in items[0].body

        # The control: what the recomputing path would have rendered off the
        # same live row, so this asserts the difference and not just a string.
        job = db.get_scheduled_job(conn, job_id)
        assert job.consecutive_failures == 0 and job.last_error is None
        assert "0 times in a row" in cron_source.title_for(job.name, 0)
        assert "prune cutoff" not in cron_source.body_for(
            job.name, job.cron_expression, job.last_error,
        )

    def test_the_note_names_a_verb_that_works_on_a_module_job(self, config, conn):
        """ISSUE-392 gave `_module.*` rows the `!cron` verbs, which is what
        retired the premise of the old exclusion — that the user had nothing to
        run against one of these.

        Two things have to be right. The note must not say "re-enable it", since
        the rescue does that on its own within the hour; and it must name the
        job, which the generic `note_for` path could never do — `flatten` strips
        `_`, so `safe == raw` is False for every name starting `_module.`.
        """
        job_id = _suspended_module_job(conn)
        cron_source.write(
            conn, "alice", job_id=job_id, job_name=_MODULE_JOB, fail_count=5,
            cron_expression="17 3 * * *", last_error="boom",
        )
        note = store.list_open(config, conn, "alice")[0][0].status_note
        assert f"`!cron disable {_MODULE_JOB}`" in note
        assert "<name>" not in note

    @pytest.mark.parametrize(
        "name",
        ["_module.", "_module.feeds`; rm -rf /", "_module.feeds\nprune",
         "_module.feeds prune"],
    )
    def test_an_unexpected_module_name_is_not_printed_beside_the_verb(self, name):
        """The two predicates are not the same test, on purpose.

        `is_module_job` is a bare `startswith`, so it admits anything under the
        prefix; the allowlist is strictly narrower, so a name the seeder could
        not have produced degrades to the generic form instead of being emitted
        into Talk, which renders markdown — a backtick would close the code span
        the verb is wrapped in. No shipped module can produce one of these; the
        branch is a guard, and a guard with no test is a claim.
        """
        assert cron_source.is_module_job(name) is True
        note = cron_source.note_for(name)
        assert "`!cron disable <name>`" in note
        assert name not in note

    def test_dismissing_the_row_does_not_turn_it_into_a_recurring_push(
        self, config, conn,
    ):
        """The hole the resolver alone does not plug.

        Holding the row open makes a re-suspension a bump, but only while it is
        open — and a resolver never sees a closed row. `dismiss` writes
        `dismissed`, `write_notification` reads any non-open state as
        `reopening`, and a reopen delivers. On a CRON.md job that is harmless
        because a suspended job never fires again; on a module job the rescue
        guarantees it will, which is the recurring push the prefix exclusion
        existed to prevent, reached through the one door the resolver cannot.
        """
        job_id = _suspended_module_job(conn)
        first = cron_source.write(
            conn, "alice", job_id=job_id, job_name=_MODULE_JOB, fail_count=5,
            cron_expression="17 3 * * *", last_error="boom",
        )
        assert first is not None and first.deliver is True

        notification_id = conn.execute(
            "SELECT id FROM notifications WHERE source = 'cron_job'",
        ).fetchone()[0]
        assert store.dismiss(conn, notification_id, "alice") is True
        assert _state(conn, "cron_job")[0] == "dismissed"

        # The rescue puts the job back, it fails again, and it is re-suspended.
        _rescue(conn, job_id)
        conn.execute(
            "UPDATE scheduled_jobs SET auto_disabled_at = datetime('now'), "
            "consecutive_failures = 5, last_error = 'boom' WHERE id = ?",
            (job_id,),
        )
        again = cron_source.write(
            conn, "alice", job_id=job_id, job_name=_MODULE_JOB, fail_count=5,
            cron_expression="17 3 * * *", last_error="boom",
        )
        assert again is None, "the same outage re-delivered after a dismiss"
        assert _state(conn, "cron_job")[0] == "dismissed"

    def test_a_new_outage_after_a_recovery_does_reopen_and_deliver(
        self, config, conn,
    ):
        """The other half, and why the predicate is not "was it dismissed".

        Suppressing on the state alone would make `dismiss` mean "never again",
        which contradicts what the store documents it as. Keyed on a success in
        between, a genuinely new outage is news and still pushes.
        """
        job_id = _suspended_module_job(conn)
        cron_source.write(
            conn, "alice", job_id=job_id, job_name=_MODULE_JOB, fail_count=5,
            cron_expression="17 3 * * *", last_error="boom",
        )
        notification_id = conn.execute(
            "SELECT id FROM notifications WHERE source = 'cron_job'",
        ).fetchone()[0]
        store.dismiss(conn, notification_id, "alice")
        conn.execute(
            "UPDATE notifications SET updated_at = "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 hour')",
        )

        # It works again — the one thing the rescue cannot forge.
        db.reset_scheduled_job_failures(conn, job_id)
        # Then, later, it breaks again.
        conn.execute(
            "UPDATE scheduled_jobs SET auto_disabled_at = datetime('now'), "
            "consecutive_failures = 5, last_error = 'boom' WHERE id = ?",
            (job_id,),
        )
        again = cron_source.write(
            conn, "alice", job_id=job_id, job_name=_MODULE_JOB, fail_count=5,
            cron_expression="17 3 * * *", last_error="boom",
        )
        assert again is not None and again.deliver is True
        assert _state(conn, "cron_job")[0] == "open"

    def test_the_pushed_text_does_not_claim_the_job_has_stopped(self):
        """`status_note` reaches the panel alone, so a correction made only
        there never gets to the person who only sees the push — which is the
        surface ISSUE-391 was filed about.

        Two claims a module row must not make: that it was switched off, and
        that it will not run again. Both are false within the hour, and the
        second one invites a dismiss, which is the one shape that would deliver
        again on the next suspension.
        """
        title = cron_source.title_for(_MODULE_JOB, 5)
        body = cron_source.body_for(_MODULE_JOB, "17 3 * * *", "boom")
        assert "switched off" not in title
        assert "will not run again" not in body
        assert "5 times in a row" in title
        # The verb has to travel with the alert, not sit in the panel.
        assert f"`!cron disable {_MODULE_JOB}`" in body
        assert "boom" in body

    def test_a_cron_md_job_keeps_its_original_wording(self):
        """The control for the branch above."""
        title = cron_source.title_for("nightly-digest", 5)
        body = cron_source.body_for("nightly-digest", "0 7 * * *", "boom")
        assert "was switched off after 5 failures" in title
        assert "will not run again" in body
        assert "!cron disable" not in body

    def test_a_success_check_that_cannot_run_holds_the_row_open(self, conn):
        """The refusal direction, which the docstring calls load-bearing.

        `_succeeded_since` runs inside `list_open`'s sweep of the whole open
        set, so it must not take the panel down — and it must not answer "yes,
        it recovered" on a question it could not settle, which would close a row
        the user has not read, silently and for good. Both refusals return the
        same False, so both are asserted.
        """
        job_id = _suspended_module_job(conn)

        class _Raises:
            def execute(self, *_a, **_k):
                raise RuntimeError("no such column: last_success_at")

        assert cron_source._succeeded_since(_Raises(), job_id, "2026-01-01") is False
        assert cron_source._succeeded_since(conn, job_id, "") is False


    def test_a_cron_md_job_still_closes_when_its_suspension_lifts(
        self, config, conn,
    ):
        """The other half of the split, asserted here so a future edit cannot
        give every job the module rule by accident. A CRON.md job has no rescue
        arm behind it, so `auto_disabled_at IS NULL` still means somebody fixed
        it.
        """
        job_id = _disabled_job(conn, name="nightly-digest")
        conn.execute(
            "UPDATE scheduled_jobs SET auto_disabled_at = NULL WHERE id = ?",
            (job_id,),
        )
        items, total = store.list_open(config, conn, "alice")
        assert items == [] and total == 0
        assert _state(conn, "cron_job")[0] == "stale"


# ---------------------------------------------------------------------------
# connected_service
# ---------------------------------------------------------------------------


@pytest.fixture
def _secret_key(monkeypatch):
    monkeypatch.setenv(
        "ISTOTA_SECRET_KEY", "test-key-test-key-test-key-test-key-test-key",
    )


def _expired_garmin(conn, user="alice"):
    from istota.health import garmin as gm

    service_source.write(conn, user, service=gm.SECRET_SERVICE, reason="token_expired")


class TestConnectedServiceResolver:
    def test_the_row_appears_with_the_producers_key(self, conn, _secret_key):
        _expired_garmin(conn)
        row = conn.execute(
            "SELECT user_id, dedup_key, object_type, object_id, state "
            "FROM notifications WHERE source = 'connected_service'",
        ).fetchone()
        assert tuple(row) == ("alice", "service:garmin", "secret", "garmin", "open")
        assert service_source.dedup_key("garmin") == "service:garmin"

    def test_the_open_row_renders_a_reconnect_link(self, config, conn, _secret_key):
        _expired_garmin(conn)
        items, total = store.list_open(config, conn, "alice")
        assert total == 1 and len(items) == 1
        (action,) = items[0].actions
        assert (action.id, action.method, action.href) == (
            "reconnect", "LINK", "/settings",
        )
        assert action.endpoint is None
        assert items[0].link is None

    def test_reconnecting_closes_the_row_without_a_panel_read(
        self, config, conn, _secret_key,
    ):
        """`store_tokens` is the real producer verb for a successful reconnect."""
        from istota.health import garmin as gm

        _expired_garmin(conn)
        conn.commit()
        gm.store_tokens(config.db_path, "alice", {"oauth1": "x"}, email="a@b.invalid")

        with db.get_db(config.db_path) as c:
            assert _state(c, "connected_service") == ("resolved", "system")

    def test_disconnecting_closes_the_row(self, config, conn, _secret_key):
        from istota.health import garmin as gm

        _expired_garmin(conn)
        conn.commit()
        gm.clear_tokens(config.db_path, "alice")

        with db.get_db(config.db_path) as c:
            assert _state(c, "connected_service") == ("resolved", "system")

    def test_a_row_left_open_over_a_working_credential_goes_stale(
        self, config, conn, _secret_key,
    ):
        """The backstop: the blob came back behind the store's back."""
        from istota import secrets_store
        from istota.health import garmin as gm

        _expired_garmin(conn)
        conn.commit()
        secrets_store.set_secret(
            config.db_path, "alice", gm.SECRET_SERVICE, gm.SECRET_KEY_BLOB,
            '{"sdk": {"oauth1": "x"}}',
        )

        with db.get_db(config.db_path) as c:
            items, total = store.list_open(config, c, "alice")
            assert items == [] and total == 0
            assert _state(c, "connected_service") == ("stale", "system")

    def test_a_row_naming_an_unregistered_service_is_never_rendered(
        self, config, conn, _secret_key,
    ):
        """`object_id` is free text here, so the allowlist stands in for `int()`."""
        store.write_notification(
            conn, "alice", source=service_source.SOURCE,
            dedup_key="service:../../admin", title="reconnect",
            object_type=service_source.OBJECT_TYPE, object_id="../../admin",
            actionable=True,
        )
        items, _total = store.list_open(config, conn, "alice")
        assert items == []


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


_STAGE_SOURCES = {
    "confirmation", "outbound_draft", "cron_job", "connected_service",
    "health_panel",
}


def test_every_object_backed_source_is_registered_and_none_auto_resolves():
    resolvers = sources.all_resolvers()
    assert set(resolvers) >= _STAGE_SOURCES
    assert sources.auto_resolve_sources() & _STAGE_SOURCES == set()
