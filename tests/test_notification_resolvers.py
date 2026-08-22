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
# registration
# ---------------------------------------------------------------------------


def test_both_sources_are_registered_and_neither_auto_resolves():
    resolvers = sources.all_resolvers()
    assert set(resolvers) >= {"confirmation", "outbound_draft"}
    assert sources.auto_resolve_sources() & {"confirmation", "outbound_draft"} == set()
