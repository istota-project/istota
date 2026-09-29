"""Clean-turn approval for relay questions (ISSUE-565).

A relay question skips the asker's approval only when the daemon's own records
say the turn is clean: the task's prompt names the recipient, and the ask is
the attempt's first and only tool call. The count behind the second half is
written by `TaskStreamAdapter` for every tool call, ahead of every display
setting, and reset when an attempt starts.
"""
import asyncio
import json
from unittest.mock import patch

import pytest

from istota import db, message_relays as relays, whatsapp_requests as requests
from istota.agent.events import PRIVATE_RELAY_TOOL_DESCRIPTION, _describe_tool_use
from istota.brain._events import ToolEndEvent, ToolUseEvent, parse_stream_line
from istota.config import Config, NextcloudConfig, SchedulerConfig, TalkConfig, EmailConfig
from istota.events import EventWriter
from istota.executor_stream import TaskStreamAdapter
from . import test_relay_questions
from .test_relay_questions import approve, hold, park

setup = test_relay_questions.setup

RELAY_ARGS = {"command": "istota-skill relay ask bob --request-key q 'What time?'"}


def relay_call(call_id="relay-1"):
    return ToolUseEvent(tool_name="Bash", tool_call_id=call_id,
                        description=_describe_tool_use("Bash", RELAY_ARGS))


def read_call(call_id="read-1"):
    return ToolUseEvent(tool_name="Read", tool_call_id=call_id,
                        description=_describe_tool_use("Read", {"file_path": "/tmp/page.html"}))


def adapter_for(config, ident, writer="default"):
    with db.get_db(config.db_path) as conn:
        task = db.get_task(conn, ident)
    if writer == "default":
        writer = EventWriter(ident, str(config.db_path), enabled=config.scheduler.event_log_enabled)
    return TaskStreamAdapter(config, task, writer)


def set_prompt(config, ident, prompt, attachments=None):
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET prompt=?, attachments=? WHERE id=?",
                     (prompt, json.dumps(attachments) if attachments else None, ident))


def counts(config, ident):
    with db.get_db(config.db_path) as conn:
        return db.get_attempt_tool_calls(conn, ident)


class TestTheCounter:
    def test_counts_with_the_event_log_and_tool_rows_switched_off(self, setup):
        # The negative control for the display-gating trap: a count that
        # followed either setting would read every turn here as zero calls.
        config, ident, _, _ = setup
        config.scheduler.event_log_enabled = False
        config.scheduler.progress_show_tool_use = False
        adapter = adapter_for(config, ident)
        adapter.on_event(read_call())
        adapter.on_event(relay_call())
        assert counts(config, ident) == (2, False)
        with db.get_db(config.db_path) as conn:
            assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=?", (ident,)).fetchone()[0] == 0

    def test_counts_with_no_event_writer_at_all(self, setup):
        config, ident, _, _ = setup
        adapter = adapter_for(config, ident, writer=None)
        adapter.on_event(relay_call())
        assert counts(config, ident) == (1, True)

    def test_the_first_call_decides_the_relay_flag(self, setup):
        config, ident, _, _ = setup
        adapter = adapter_for(config, ident)
        adapter.on_event(relay_call())
        assert counts(config, ident) == (1, True)
        adapter.on_event(read_call())
        adapter.on_event(ToolEndEvent(tool_name="Read", tool_call_id="read-1", success=True, duration_ms=1))
        assert counts(config, ident) == (2, True)

    def test_a_lost_write_is_carried_by_the_next(self, setup):
        config, ident, _, _ = setup
        adapter = adapter_for(config, ident)
        real = db.record_attempt_tool_call
        calls = []

        def flaky(conn, task_id, **kw):
            calls.append(kw)
            if len(calls) == 1:
                raise RuntimeError("database is locked")
            return real(conn, task_id, **kw)

        with patch("istota.db.record_attempt_tool_call", flaky):
            adapter.on_event(read_call())
            adapter.on_event(relay_call())
        # Without the running total the row would read one relay call.
        assert counts(config, ident) == (2, False)

    def test_a_second_writer_only_adds(self, setup):
        # A reclaimed task's old worker still writing into the new attempt.
        config, ident, _, _ = setup
        stale = adapter_for(config, ident)
        stale.on_event(read_call())
        fresh = adapter_for(config, ident)
        fresh.on_event(relay_call())
        assert counts(config, ident) == (2, False)

    def test_the_claude_code_frame_reaches_the_counter(self, setup):
        config, ident, _, _ = setup
        event = parse_stream_line(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": RELAY_ARGS}]}}))
        assert event.description == PRIVATE_RELAY_TOOL_DESCRIPTION
        adapter_for(config, ident).on_event(event)
        assert counts(config, ident) == (1, True)


class TestTheNativeBrainPath:
    """NativeBrain's tool calls reach the same adapter, before the tool runs."""

    def test_the_count_lands_before_the_tool_executes(self, setup, tmp_path):
        from istota.llm.types import AssistantMessage, TextContent, ToolCallContent
        from .native._mock_provider import MockProvider
        from .native.test_native_brain import _brain, _req

        config, ident, _, _ = setup
        adapter = adapter_for(config, ident)
        target = tmp_path / "out.txt"
        seen = []

        def on_progress(event):
            adapter.on_event(event)
            if isinstance(event, ToolUseEvent):
                seen.append(("use", counts(config, ident), target.exists()))
            elif isinstance(event, ToolEndEvent):
                seen.append(("end", target.exists()))

        provider = MockProvider([
            AssistantMessage(content=[ToolCallContent(
                id="c1", name="Write", arguments={"file_path": str(target), "content": "hi"})],
                stop_reason="tool_use"),
            AssistantMessage(content=[TextContent(text="Done.")], stop_reason="end_turn"),
        ])
        req = _req("write a file", tmp_path, tools=["Write"])
        req.on_progress = on_progress
        result = _brain(provider).execute(req)
        assert result.success
        assert seen == [("use", (1, False), False), ("end", True)]

    def test_a_relay_bash_call_is_flagged(self, setup):
        from istota.brain.native import _tool_use_event

        config, ident, _, _ = setup
        adapter_for(config, ident).on_event(
            _tool_use_event("Bash", _describe_tool_use("Bash", RELAY_ARGS), "c1"))
        assert counts(config, ident) == (1, True)


class TestTheAttemptReset:
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_a_fresh_attempt_counts_from_zero(self, _arun, db_path, tmp_path):
        from istota.scheduler import process_one_task

        mount = tmp_path / "mount"
        mount.mkdir()
        config = Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="secret"),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False), scheduler=SchedulerConfig(),
            workspace_path=mount, temp_dir=tmp_path / "temp",
        )
        with db.get_db(db_path) as conn:
            ident = db.create_task(conn, prompt="Hello", user_id="testuser", source_type="cli")
            # What a previous attempt left behind.
            conn.execute("UPDATE tasks SET attempt_tool_calls=1, attempt_first_tool_relay=1 WHERE id=?", (ident,))
        seen = []

        def execute(task, config, *args, **kwargs):
            with db.get_db(db_path) as conn:
                seen.append(db.get_attempt_tool_calls(conn, task.id))
            return True, "done", None, None

        with patch("istota.scheduler.execute_task", side_effect=execute):
            assert process_one_task(config) is not None
        assert seen == [(0, False)]


def queue_with(setup, prompt, events, **kw):
    config, ident, _, _ = setup
    set_prompt(config, ident, prompt, kw.pop("attachments", None))
    adapter = adapter_for(config, ident)
    for event in events:
        adapter.on_event(event)
    return hold(setup, **kw)


class TestTheRule:
    def test_a_clean_turn_is_queued_without_approval(self, setup):
        config, ident, _, sent = setup
        result = queue_with(setup, "Ask Bob what time dinner is", [relay_call()])
        assert result["status"] == "queued"
        assert result["approval"] == "clean_turn"
        assert "needs_confirmation" not in result and "preview" not in result
        with db.get_db(config.db_path) as conn:
            request = conn.execute("SELECT * FROM whatsapp_skill_requests WHERE id=?",
                                   (result["request_id"],)).fetchone()
            # The preview is still built and stored, and is what was approved.
            assert request["preview"] and request["approved_digest"] == request["preview_digest"]
            assert request["approved_at"] and request["queue_deadline"]
            relay = relays.get_relay(conn, actor_user_id="alice", relay_id=result["relay_id"])
            assert relay["approval"] == "clean_turn" and relay["expires_at"]
        # No park and no preview: the scheduler has nothing to present.
        assert park(setup) is None
        asyncio.run(requests.drain_requests(config))
        asyncio.run(requests.drain_requests(config))
        assert len(sent) == 1
        with db.get_db(config.db_path) as conn:
            assert relays.get_relay(conn, actor_user_id="alice", relay_id=result["relay_id"])["state"] == "waiting"

    def test_the_display_name_counts_and_so_does_case(self, setup):
        assert queue_with(setup, "can you ask BOB for me", [relay_call()])["status"] == "queued"

    @pytest.mark.parametrize("prompt,events", [
        ("Ask Bob what time dinner is", [read_call(), relay_call()]),  # a prior tool call
        ("Ask Bob what time dinner is", []),  # the ask's own call not counted yet
        ("Ask him about it", [relay_call()]),  # named only in context
        ("Ask Bobby what time dinner is", [relay_call()]),  # not a whole word
    ], ids=["prior-tool-call", "zero-count", "context-only", "partial-word"])
    def test_anything_else_is_held(self, setup, prompt, events):
        config, _, token, sent = setup
        with db.get_db(config.db_path) as conn:
            # The recipient is named in the room's history, not in this prompt.
            db.add_message(conn, token, role="user", body="Bob said he would call",
                           origin_surface="web", author_user_id="alice")
        result = queue_with(setup, prompt, events)
        assert result["status"] == "held" and result["needs_confirmation"]
        assert "approval" not in result
        asyncio.run(requests.drain_requests(config))
        assert not sent

    def test_an_attachment_name_does_not_count(self, setup):
        result = queue_with(setup, "(Sent without a message — see attached: bob.pdf)",
                            [relay_call()], attachments=["/tmp/uploads/bob.pdf"])
        assert result["status"] == "held"

    def test_a_second_ask_in_the_turn_is_held(self, setup):
        config, ident, _, _ = setup
        first = queue_with(setup, "Ask Bob, and then ask Bob again", [relay_call()])
        assert first["status"] == "queued"
        with db.get_db(config.db_path) as conn:
            relays.cancel_relay(conn, actor_user_id="alice", relay_id=first["relay_id"])
        adapter_for(config, ident).on_event(relay_call("relay-2"))
        second = hold(setup, request_key="second")
        assert second["status"] == "held"

    def test_an_unverified_surface_is_held(self, setup):
        config, ident, _, _ = setup
        with patch.object(requests, "_CLEAN_TURN_SURFACES", frozenset({"talk"})):
            assert queue_with(setup, "Ask Bob", [relay_call()])["status"] == "held"

    def test_a_replayed_ask_reports_the_same_approval(self, setup):
        first = queue_with(setup, "Ask Bob what time", [relay_call()])
        again = hold(setup)
        assert again["status"] == "queued" and again["approval"] == "clean_turn"
        assert again["request_id"] == first["request_id"]


class TestTheApprovalIsShown:
    def test_a_held_question_records_user_approval(self, setup):
        config, ident, _, _ = setup
        held = hold(setup)
        park(setup)
        approve(setup)
        with db.get_db(config.db_path) as conn:
            relay = relays.get_relay(conn, actor_user_id="alice", relay_id=held["relay_id"])
        assert relay["approval"] == "user"
        assert relay["destination_label"] == "Bob's WhatsApp"

    def test_relay_show_names_the_approval(self, setup):
        from istota.commands import CommandContext, cmd_relay

        config, _, token, _ = setup
        result = queue_with(setup, "Ask Bob what time", [relay_call()])
        with db.get_db(config.db_path) as conn:
            ctx = CommandContext(config, conn, "alice", token, f"show {result['relay_id']}", surface="web")
            text = asyncio.run(cmd_relay(ctx))
        assert "Approval: sent without approval" in text
        assert "Destination: Bob's WhatsApp" in text
