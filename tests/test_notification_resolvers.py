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

    def test_a_module_job_is_not_worth_notifying_about(self):
        """`_sync_module_jobs` lifts these suspensions hourly whether or not
        the job works.

        A row would ride that loop — raised on the suspend, `stale` after the
        rescue cleared the column, then *reopened* an hour later, and the reopen
        branch delivers. A permanently broken module job would become an hourly
        push about something with no `!cron enable` to run against it.
        """
        assert cron_source.should_notify("nightly-digest") is True
        assert cron_source.should_notify("_module.health.garmin_sync") is False
        assert cron_source.is_module_job("_module.feeds.poll") is True

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
