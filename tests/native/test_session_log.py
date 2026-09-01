"""The native-brain session log: the writer, the caps, and the sweep.

No stack, no brain, no config — `session_log` is a leaf that takes its root and
its policy as parameters, which is what makes this file cheap enough to hold the
whole contract.

Two groups here carry most of the weight. `TestNeverRaises` is the writer's
stated contract: a task must not fail because a log could not be written, and
every assertion there is about an exception *not* escaping plus one warning
rather than a stream of them. `TestTheCeiling` is a deletion path, so its cases
are written to distinguish the specified rule from the rules it is easy to
implement by accident — `test_the_heaviest_user_is_trimmed_before_a_quiet_one`
is red against a plain global oldest-first sweep, which passes every other case
in the class.

The ceiling's real floor is half a gibibyte (`MIN_MAX_TOTAL_GB`), so almost
every case here passes `floor_gb=0` to make a tree of a few kilobytes exceed it.
`test_a_ceiling_below_the_floor_is_clamped` deliberately does not, and therefore
gets the real one.
"""

import ast
import base64
import hashlib
import json
import logging
import os
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from istota.llm.types import (
    AssistantMessage,
    ImageContent,
    TextContent,
    ThinkingContent,
    ToolCallContent,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from istota.session import session_log
from istota.session.session_log import (
    LIVE_WINDOW_SECONDS,
    SessionLogIdentity,
    SessionLogPolicy,
    SessionLogWriter,
    SweepResult,
    serialize_content,
    serialize_message,
    session_log_path,
    sweep_session_logs,
)

IDENT = SessionLogIdentity(
    task_id=4471,
    attempt=1,
    user_id="alice",
    source_type="talk",
    conversation_token="a1b2c3d4",
)

POLICY = SessionLogPolicy()

NOW = datetime(2026, 8, 31, 14, 22, 1, 993000, tzinfo=timezone.utc)


def read_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ==========================================================================
# session_log_path
# ==========================================================================

class TestSessionLogPath:
    def test_builds_the_documented_name(self, tmp_path):
        path = session_log_path(tmp_path, IDENT, NOW)
        assert path == tmp_path / "alice" / "2026-08-31T14-22-01-993Z_task-4471-1.jsonl"

    def test_the_timestamp_carries_no_filesystem_hostile_characters(self, tmp_path):
        name = session_log_path(tmp_path, IDENT, NOW).name
        assert ":" not in name
        # One dot only, and it is the suffix: `.` is replaced in the timestamp
        # so a reader splitting on the extension cannot be fooled.
        assert name.count(".") == 1
        assert name.endswith(".jsonl")

    def test_a_naive_datetime_is_read_as_utc_rather_than_local(self, tmp_path):
        naive = NOW.replace(tzinfo=None)
        assert session_log_path(tmp_path, IDENT, naive) == session_log_path(tmp_path, IDENT, NOW)

    def test_two_attempts_of_one_task_are_two_files(self, tmp_path):
        second = SessionLogIdentity(task_id=4471, attempt=2, user_id="alice")
        first_path = session_log_path(tmp_path, IDENT, NOW)
        second_path = session_log_path(tmp_path, second, NOW)
        assert first_path != second_path
        assert first_path.name.endswith("task-4471-1.jsonl")
        assert second_path.name.endswith("task-4471-2.jsonl")

    def test_grepping_the_task_id_finds_every_attempt(self, tmp_path):
        names = [
            session_log_path(tmp_path, SessionLogIdentity(4471, n, "alice"), NOW).name
            for n in (1, 2, 3)
        ]
        assert all("task-4471-" in name for name in names)

    def test_lexical_order_is_chronological_order(self, tmp_path):
        stamps = [NOW + timedelta(minutes=n) for n in (0, 5, 61, 1500)]
        names = [session_log_path(tmp_path, IDENT, s).name for s in stamps]
        assert names == sorted(names)

    def test_the_collision_suffix_is_appended_only_when_the_path_exists(self, tmp_path):
        plain = session_log_path(tmp_path, IDENT, NOW, session_id="b3f9aaaa-0000")
        assert plain.name == "2026-08-31T14-22-01-993Z_task-4471-1.jsonl"

        plain.parent.mkdir(parents=True)
        plain.write_text("{}\n")

        # A usage_limit reroute to a second native run for the same
        # (task_id, attempt) must never overwrite the first run's record.
        second = session_log_path(tmp_path, IDENT, NOW, session_id="b3f9aaaa-0000")
        assert second.name == "2026-08-31T14-22-01-993Z_task-4471-1-b3f9.jsonl"
        assert plain.read_text() == "{}\n"

    def test_a_third_collision_takes_a_counter_rather_than_looping(self, tmp_path):
        first = session_log_path(tmp_path, IDENT, NOW, session_id="b3f9aaaa")
        first.parent.mkdir(parents=True)
        first.write_text("")
        second = session_log_path(tmp_path, IDENT, NOW, session_id="b3f9aaaa")
        second.write_text("")
        third = session_log_path(tmp_path, IDENT, NOW, session_id="b3f9aaaa")
        assert third.name == "2026-08-31T14-22-01-993Z_task-4471-1-b3f9-2.jsonl"

    @pytest.mark.parametrize("user_id", ["", ".", "..", "../evil", "a/b", "/abs", "n\x00ul"])
    def test_a_user_id_that_is_not_one_component_is_refused(self, tmp_path, user_id):
        ident = SessionLogIdentity(task_id=1, attempt=1, user_id=user_id)
        with pytest.raises(ValueError):
            session_log_path(tmp_path, ident, NOW)


# ==========================================================================
# serialize_content
# ==========================================================================

class TestSerializeText:
    def test_text_under_the_cap_is_untouched(self):
        block = TextContent(text="x" * 99)
        out = serialize_content(block, SessionLogPolicy(max_content_chars=100))
        assert out == {"type": "text", "text": "x" * 99}

    def test_text_exactly_at_the_cap_is_untouched(self):
        block = TextContent(text="x" * 100)
        out = serialize_content(block, SessionLogPolicy(max_content_chars=100))
        assert out["text"] == "x" * 100
        assert "truncated" not in out

    def test_text_one_over_the_cap_is_head_and_tail(self):
        original = "H" * 50 + "M" * 1 + "T" * 50  # 101 chars
        out = serialize_content(TextContent(text=original), SessionLogPolicy(max_content_chars=100))

        assert out["truncated"] is True
        assert out["chars_total"] == 101

        # Head *and* tail: a build log's error is in the tail, so a head-only
        # truncation discards the one part anybody opens the file for. The
        # surviving halves must be a real prefix and a real suffix of the
        # original, not a re-rendering of it.
        text = out["text"]
        head = text[: text.index("\n… [truncated")]
        tail = text[text.index(" …\n") + len(" …\n") :]
        assert len(head) == len(tail) > 0
        assert original.startswith(head)
        assert original.endswith(tail)
        assert set(tail) == {"T"}

    def test_the_cap_bounds_the_result_and_not_just_the_two_halves(self):
        # Charging the note to the caller rather than to the budget is what let
        # the "cap" hand back something longer than its own limit.
        for limit in (64, 100, 512, 4096):
            out = serialize_content(
                TextContent(text="x" * 100_000), SessionLogPolicy(max_content_chars=limit)
            )
            assert len(out["text"]) <= limit, limit

    @pytest.mark.parametrize("limit", [1, 2, 3, 10, 40])
    def test_a_limit_too_small_for_a_note_clips_rather_than_growing_the_text(self, limit):
        original = "abcdefghij" * 10
        out = serialize_content(TextContent(text=original), SessionLogPolicy(max_content_chars=limit))
        assert len(out["text"]) <= limit
        assert out["text"] == original[:limit]
        assert out["truncated"] is True
        assert out["chars_total"] == len(original)

    def test_the_truncation_note_accounts_for_every_dropped_char(self):
        out = serialize_content(TextContent(text="x" * 1000), SessionLogPolicy(max_content_chars=100))
        text = out["text"]
        head = text[: text.index("\n… [truncated")]
        tail = text[text.index(" …\n") + len(" …\n") :]
        dropped = int(text.split("[truncated ", 1)[1].split(" chars]", 1)[0])
        assert len(head) + dropped + len(tail) == out["chars_total"] == 1000

    def test_thinking_is_capped_by_the_same_rule(self):
        out = serialize_content(
            ThinkingContent(thinking="y" * 500), SessionLogPolicy(max_content_chars=100)
        )
        assert out["type"] == "thinking"
        assert out["truncated"] is True
        assert out["chars_total"] == 500

    def test_thinking_is_dropped_entirely_when_include_thinking_is_false(self):
        policy = SessionLogPolicy(include_thinking=False)
        assert serialize_content(ThinkingContent(thinking="secret reasoning"), policy) is None
        # Not a drop for anything else.
        assert serialize_content(TextContent(text="kept"), policy)["text"] == "kept"


class TestSerializeImage:
    def test_an_image_is_a_descriptor_and_never_carries_its_bytes(self):
        raw = b"\x89PNG" + b"\x00" * 4096
        block = ImageContent(
            media_type="image/png",
            data=base64.b64encode(raw).decode(),
            display_name="screenshot.png",
        )
        out = serialize_content(block, POLICY)

        # Assert on key *absence*: a size assertion passes against a descriptor
        # that still carries a small image.
        assert "data" not in out
        assert out["type"] == "image"
        assert out["media_type"] == "image/png"
        assert out["display_name"] == "screenshot.png"
        assert out["bytes"] == len(raw)
        assert out["sha256"] == hashlib.sha256(raw).hexdigest()

    def test_bytes_is_the_decoded_length_not_the_base64_length(self):
        raw = b"a" * 3000
        encoded = base64.b64encode(raw).decode()
        out = serialize_content(ImageContent(media_type="image/jpeg", data=encoded), POLICY)
        assert out["bytes"] == 3000 != len(encoded)

    def test_the_same_image_twice_hashes_the_same(self):
        encoded = base64.b64encode(b"identical").decode()
        first = serialize_content(ImageContent(data=encoded), POLICY)
        second = serialize_content(ImageContent(data=encoded, display_name="other.png"), POLICY)
        assert first["sha256"] == second["sha256"]

    def test_undecodable_base64_says_so_rather_than_raising(self):
        out = serialize_content(ImageContent(media_type="image/png", data="!!!!not b64="), POLICY)
        assert out["type"] == "image"
        assert "data" not in out
        assert out.get("decode_error") is True


class TestSerializeToolCall:
    def test_small_arguments_are_kept_whole(self):
        block = ToolCallContent(id="call_1", name="Bash", arguments={"command": "ls -la"})
        out = serialize_content(block, POLICY)
        assert out == {
            "type": "tool_call",
            "id": "call_1",
            "name": "Bash",
            "arguments": {"command": "ls -la"},
        }

    def test_arguments_over_the_cap_become_a_marker_that_is_valid_json(self):
        block = ToolCallContent(id="c", name="Write", arguments={"content": "z" * 5000})
        out = serialize_content(block, SessionLogPolicy(max_args_chars=200))

        args = out["arguments"]
        assert args["_truncated"] is True
        assert args["chars_total"] > 5000
        assert "preview" in args
        # The anti-goal is a truncated *fragment* of a JSON object. The whole
        # record must still round trip.
        assert json.loads(json.dumps(out)) == out

    def test_unserializable_arguments_do_not_lose_the_call(self):
        block = ToolCallContent(id="c", name="Odd", arguments={"handle": object()})
        out = serialize_content(block, POLICY)
        assert out["name"] == "Odd"
        assert out["arguments"]["_unserializable"] is True
        assert json.loads(json.dumps(out))["id"] == "c"


class TestSerializeUnknownBlock:
    def test_an_unrecognized_block_is_recorded_rather_than_dropped(self):
        class Weird:
            type = "weird"

        out = serialize_content(Weird(), POLICY)
        assert out["type"] == "weird"
        assert out["unrecognized"] is True


# ==========================================================================
# serialize_message
# ==========================================================================

class TestSerializeMessage:
    def test_a_user_message(self):
        out = serialize_message(UserMessage(content=[TextContent(text="hi")]), POLICY)
        assert out == {"role": "user", "content": [{"type": "text", "text": "hi"}]}

    def test_an_assistant_message_carries_usage_stop_reason_and_model(self):
        msg = AssistantMessage(
            content=[ThinkingContent(thinking="hm"), ToolCallContent(id="c1", name="Bash")],
            usage=Usage(input_tokens=24763, output_tokens=121, cost_usd=0.0646884),
            stop_reason="tool_use",
            model="anthropic/claude-opus-4.8",
        )
        out = serialize_message(msg, POLICY)
        assert out["role"] == "assistant"
        assert out["model"] == "anthropic/claude-opus-4.8"
        assert out["stop_reason"] == "tool_use"
        assert out["error_message"] is None
        assert out["usage"] == {
            "input_tokens": 24763,
            "output_tokens": 121,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cost_usd": 0.0646884,
        }
        assert [c["type"] for c in out["content"]] == ["thinking", "tool_call"]

    def test_a_tool_result_message_carries_its_output(self):
        msg = ToolResultMessage(
            tool_call_id="call_1",
            tool_name="Bash",
            content=[TextContent(text="total 48\ndrwxr-xr-x")],
        )
        out = serialize_message(msg, POLICY)
        assert out["role"] == "tool_result"
        assert out["tool_call_id"] == "call_1"
        assert out["tool_name"] == "Bash"
        assert out["is_error"] is False
        assert out["content"][0]["text"] == "total 48\ndrwxr-xr-x"

    def test_field_names_stay_snake_case(self):
        out = serialize_message(AssistantMessage(stop_reason="end_turn"), POLICY)
        assert "stop_reason" in out and "stopReason" not in out

    def test_thinking_is_absent_from_an_assistant_message_when_it_is_off(self):
        msg = AssistantMessage(
            content=[ThinkingContent(thinking="hm"), TextContent(text="answer")]
        )
        out = serialize_message(msg, SessionLogPolicy(include_thinking=False))
        assert [c["type"] for c in out["content"]] == ["text"]


# ==========================================================================
# The writer
# ==========================================================================

class TestTheWriter:
    def test_a_full_session_round_trips_line_by_line(self, tmp_path):
        writer = SessionLogWriter(tmp_path, IDENT, POLICY)
        writer.open({"brain": "native", "model": "anthropic/claude-opus-4.8", "effort": "high"})
        writer.context("You are …", ["Bash", "Read"], "3f9a")
        writer.message(UserMessage(content=[TextContent(text="do the thing")]))
        writer.message(
            AssistantMessage(
                content=[ToolCallContent(id="c1", name="Bash", arguments={"command": "ls"})],
                stop_reason="tool_use",
            )
        )
        writer.message(
            ToolResultMessage(tool_call_id="c1", tool_name="Bash", content=[TextContent(text="ok")])
        )
        writer.compaction(trigger="proactive", summary="…", tokens_before=184320)
        writer.steer("check staging first")
        writer.nudge(phase="early", remaining=50)
        writer.error(ValueError("boom"))
        writer.result(success=True, stop_reason="completed", result_text="done")
        writer.close()

        records = read_records(writer.path)
        assert [r["type"] for r in records] == [
            "session",
            "context",
            "message",
            "message",
            "message",
            "compaction",
            "steer",
            "nudge",
            "error",
            "result",
        ]
        assert all("ts" in r for r in records)
        assert writer.path.read_text(encoding="utf-8").endswith("\n")

    def test_line_one_is_the_header_and_carries_the_identity(self, tmp_path):
        writer = SessionLogWriter(tmp_path, IDENT, POLICY)
        writer.open({"brain": "native", "provider": "openai_compat", "base_url_host": "openrouter.ai"})
        writer.close()

        header = read_records(writer.path)[0]
        assert header["type"] == "session"
        assert header["v"] == 1
        assert header["task_id"] == 4471
        assert header["attempt"] == 1
        assert header["user_id"] == "alice"
        assert header["source_type"] == "talk"
        assert header["conversation_token"] == "a1b2c3d4"
        assert header["is_group_chat"] is False
        assert header["session_id"] == writer.session_id
        assert header["base_url_host"] == "openrouter.ai"

    def test_a_header_cannot_overwrite_the_records_own_fields(self, tmp_path):
        writer = SessionLogWriter(tmp_path, IDENT, POLICY)
        writer.open({"type": "not-a-session", "v": 99, "ts": "whenever"})
        writer.close()
        header = read_records(writer.path)[0]
        assert header["type"] == "session"
        assert header["v"] == 1
        assert header["ts"] != "whenever"

    def test_the_context_record_holds_the_system_prompt_and_the_tool_names(self, tmp_path):
        writer = SessionLogWriter(tmp_path, IDENT, POLICY)
        writer.open()
        writer.context("You are Istota.", ["Bash", "Read", "Edit"], "3f9adeadbeef")
        writer.close()

        ctx = read_records(writer.path)[1]
        assert ctx["system_prompt"] == "You are Istota."
        assert ctx["tools"] == ["Bash", "Read", "Edit"]
        assert ctx["tools_schema_sha256"] == "3f9adeadbeef"

    def test_result_text_is_never_capped(self, tmp_path):
        writer = SessionLogWriter(tmp_path, IDENT, SessionLogPolicy(max_content_chars=100))
        writer.open()
        deliverable = "R" * 5000
        writer.result(success=True, result_text=deliverable)
        writer.close()

        # The deliverable, on the same reasoning that put `result` in
        # events._UNCAPPED_EVENT_KINDS.
        assert read_records(writer.path)[-1]["result_text"] == deliverable

    def test_the_context_record_caps_an_oversized_system_prompt(self, tmp_path):
        writer = SessionLogWriter(tmp_path, IDENT, SessionLogPolicy(max_content_chars=200))
        writer.open()
        writer.context("S" * 5000, ["Bash"], "sha")
        writer.close()

        ctx = read_records(writer.path)[1]
        assert ctx["truncated"] is True
        assert ctx["chars_total"] == 5000
        assert len(ctx["system_prompt"]) <= 200

    def test_truncated_records_counts_records_not_blocks(self, tmp_path):
        writer = SessionLogWriter(tmp_path, IDENT, SessionLogPolicy(max_content_chars=10))
        writer.open()
        assert writer.truncated_records == 0
        writer.message(UserMessage(content=[TextContent(text="x" * 100)]))
        assert writer.truncated_records == 1
        # Two capped blocks in one message is still one capped record.
        writer.message(
            UserMessage(content=[TextContent(text="y" * 100), TextContent(text="z" * 100)])
        )
        assert writer.truncated_records == 2
        writer.message(UserMessage(content=[TextContent(text="short")]))
        assert writer.truncated_records == 2
        writer.close()

    def test_a_capped_arguments_object_still_counts_as_a_truncation(self, tmp_path):
        writer = SessionLogWriter(tmp_path, IDENT, SessionLogPolicy(max_args_chars=50))
        writer.open()
        writer.message(
            AssistantMessage(
                content=[ToolCallContent(id="c", name="Write", arguments={"body": "z" * 500})]
            )
        )
        writer.close()
        assert writer.truncated_records == 1

    def test_the_model_cannot_inflate_the_truncation_count(self, tmp_path):
        # `truncated_records` exists to tell "the model saw a short result"
        # from "the log is short". A number the model can raise by writing a
        # word into its own tool arguments answers neither question.
        writer = SessionLogWriter(tmp_path, IDENT, POLICY)
        writer.open()
        writer.message(
            AssistantMessage(
                content=[
                    ToolCallContent(
                        id="c",
                        name="Bash",
                        arguments={"truncated": True, "note": {"_truncated": True}},
                    )
                ]
            )
        )
        writer.close()
        assert writer.truncated_records == 0

    def test_a_stray_field_cannot_rename_a_record(self, tmp_path):
        # The reader's contract is stated in terms of `type` — "`result` is
        # always the last line" — so a caller's field must not forge one.
        writer = SessionLogWriter(tmp_path, IDENT, POLICY)
        writer.open()
        writer.result(type="not-a-result", ts="whenever", success=True)
        writer.compaction(type="session", trigger="proactive")
        writer.close()

        records = read_records(writer.path)
        assert [r["type"] for r in records] == ["session", "result", "compaction"]
        assert records[1]["ts"] != "whenever"
        assert records[1]["success"] is True

    def test_the_error_record_carries_the_kind_message_and_traceback(self, tmp_path):
        writer = SessionLogWriter(tmp_path, IDENT, POLICY)
        writer.open()
        try:
            raise RuntimeError("429 rate limited")
        except RuntimeError as exc:
            writer.error(exc)
        writer.close()

        rec = read_records(writer.path)[-1]
        assert rec["type"] == "error"
        assert rec["kind"] == "RuntimeError"
        assert rec["message"] == "429 rate limited"
        assert "Traceback (most recent call last)" in rec["traceback"]

    def test_close_is_idempotent(self, tmp_path):
        writer = SessionLogWriter(tmp_path, IDENT, POLICY)
        writer.open()
        writer.close()
        writer.close()
        assert writer.path.exists()

    def test_a_second_open_does_not_start_a_second_file(self, tmp_path):
        writer = SessionLogWriter(tmp_path, IDENT, POLICY)
        writer.open()
        first = writer.path
        writer.open()
        writer.close()
        assert writer.path == first
        assert len(list((tmp_path / "alice").iterdir())) == 1

    def test_reopening_after_close_does_not_orphan_the_first_file(self, tmp_path):
        # One attempt is one file. A guard reading `_fh is not None` passes the
        # case above and still starts a second file here, with `path` silently
        # renaming itself to the new one.
        writer = SessionLogWriter(tmp_path, IDENT, POLICY)
        writer.open()
        first = writer.path
        writer.close()
        writer.open({"brain": "native"})
        writer.close()

        assert writer.path == first
        assert len(list((tmp_path / "alice").iterdir())) == 1

    def test_records_after_close_are_dropped_rather_than_raising(self, tmp_path):
        writer = SessionLogWriter(tmp_path, IDENT, POLICY)
        writer.open()
        writer.close()
        before = writer.path.read_text()
        writer.message(UserMessage(content=[TextContent(text="late")]))
        writer.result(success=False)
        assert writer.path.read_text() == before

    def test_two_attempts_write_two_files_and_neither_truncates_the_other(self, tmp_path):
        first = SessionLogWriter(tmp_path, IDENT, POLICY)
        first.open({"attempt_note": "one"})
        first.result(success=False, stop_reason="error")
        first.close()

        second_ident = SessionLogIdentity(task_id=4471, attempt=2, user_id="alice")
        second = SessionLogWriter(tmp_path, second_ident, POLICY)
        second.open({"attempt_note": "two"})
        second.result(success=True, stop_reason="completed")
        second.close()

        assert first.path != second.path
        assert first.session_id != second.session_id
        assert read_records(first.path)[0]["attempt"] == 1
        assert read_records(second.path)[0]["attempt"] == 2


class TestTheDisabledWriter:
    @pytest.mark.parametrize(
        "kwargs, root_is_none",
        [({}, True), ({"enabled": False}, False)],
    )
    def test_it_creates_nothing_and_costs_nothing(self, tmp_path, kwargs, root_is_none):
        root = None if root_is_none else tmp_path / "logs"
        writer = SessionLogWriter(root, IDENT, POLICY, **kwargs)

        writer.open({"brain": "native"})
        writer.context("prompt", ["Bash"], "sha")
        writer.message(UserMessage(content=[TextContent(text="hi")]))
        writer.compaction(trigger="proactive")
        writer.steer("nope")
        writer.nudge(phase="early")
        writer.error(ValueError("boom"))
        writer.result(success=True)
        writer.close()

        assert writer.path is None
        assert writer.truncated_records == 0
        assert not writer.active
        assert list(tmp_path.iterdir()) == []


class TestNeverRaises:
    """A task must never fail because a log could not be written."""

    @pytest.mark.requires_dac
    def test_an_unwritable_root_disables_the_writer_and_warns_once(self, tmp_path, caplog):
        root = tmp_path / "logs"
        root.mkdir()
        os.chmod(root, stat.S_IRUSR | stat.S_IXUSR)
        try:
            with caplog.at_level(logging.WARNING, logger="istota.session.session_log"):
                writer = SessionLogWriter(root, IDENT, POLICY)
                writer.open({"brain": "native"})
                writer.message(UserMessage(content=[TextContent(text="hi")]))
                writer.result(success=True)
                writer.close()
        finally:
            os.chmod(root, stat.S_IRWXU)

        assert writer.path is None
        assert not writer.active
        # One warning, not one per record: a disk that filled up must not
        # produce a line per tool call for the rest of the day.
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1

    def test_a_root_whose_parent_is_a_file_disables_the_writer(self, tmp_path, caplog):
        blocker = tmp_path / "notadir"
        blocker.write_text("i am a file")

        with caplog.at_level(logging.WARNING, logger="istota.session.session_log"):
            writer = SessionLogWriter(blocker / "logs", IDENT, POLICY)
            writer.open()
            writer.message(UserMessage(content=[TextContent(text="hi")]))
            writer.close()

        assert writer.path is None
        assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1

    def test_a_user_id_that_escapes_the_root_disables_the_writer(self, tmp_path, caplog):
        # Defence in depth rather than the boundary — `user_id` comes off the
        # task row — but the cost of the check is nothing and the cost of
        # missing it is an append somewhere else on the disk.
        ident = SessionLogIdentity(task_id=1, attempt=1, user_id="../escape")
        with caplog.at_level(logging.WARNING, logger="istota.session.session_log"):
            writer = SessionLogWriter(tmp_path / "logs", ident, POLICY)
            writer.open()
            writer.message(UserMessage(content=[TextContent(text="hi")]))
            writer.close()

        assert writer.path is None
        assert not (tmp_path / "escape").exists()

    def test_a_write_failing_mid_session_keeps_the_prefix_and_stops(self, tmp_path, caplog):
        writer = SessionLogWriter(tmp_path, IDENT, POLICY)
        writer.open({"brain": "native"})
        writer.message(UserMessage(content=[TextContent(text="first")]))

        class _Broken:
            def write(self, _data):
                raise OSError(28, "No space left on device")

            def flush(self):
                raise OSError(28, "No space left on device")

            def close(self):
                pass

        writer._fh = _Broken()
        with caplog.at_level(logging.WARNING, logger="istota.session.session_log"):
            writer.message(UserMessage(content=[TextContent(text="second")]))
            writer.message(UserMessage(content=[TextContent(text="third")]))
            writer.result(success=True)
            writer.close()

        records = read_records(writer.path)
        assert [r["type"] for r in records] == ["session", "message"]
        assert not writer.active
        assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1

    def test_a_record_that_will_not_serialize_costs_that_record_only(self, tmp_path):
        writer = SessionLogWriter(tmp_path, IDENT, POLICY)
        writer.open()

        class Exploding:
            role = "assistant"

            def __repr__(self):
                raise RuntimeError("repr exploded")

        writer.message(Exploding())
        writer.message(UserMessage(content=[TextContent(text="the session continues")]))
        writer.result(success=True)
        writer.close()

        records = read_records(writer.path)
        assert [r["type"] for r in records] == [
            "session",
            "serialization_error",
            "message",
            "result",
        ]
        assert records[1]["record_type"] == "message"
        assert "repr exploded" in records[1]["error"]

    def test_deeply_nested_tool_arguments_do_not_raise_out_of_message(self, tmp_path):
        # `arguments` is JSON the model emitted, so its *structure* is
        # attacker-influenced and not only its size. 600 levels encode to 1200
        # characters — far under `max_args_chars`, so no marker fires and the
        # whole thing is embedded — while a naive recursive walk over the
        # assembled record blows the frame limit and raises into the agent
        # loop, failing a task that had nothing wrong with it.
        deep = json.loads("[" * 600 + "]" * 600)
        writer = SessionLogWriter(tmp_path, IDENT, POLICY)
        writer.open()
        writer.message(
            AssistantMessage(content=[ToolCallContent(id="c", name="Bash", arguments={"x": deep})])
        )
        writer.message(UserMessage(content=[TextContent(text="the session continues")]))
        writer.result(success=True)
        writer.close()

        records = read_records(writer.path)
        assert [r["type"] for r in records] == ["session", "message", "message", "result"]
        assert writer.truncated_records == 0

    def test_a_header_that_is_not_a_mapping_does_not_raise(self, tmp_path):
        writer = SessionLogWriter(tmp_path, IDENT, POLICY)
        writer.open("not a mapping")
        writer.message(UserMessage(content=[TextContent(text="hi")]))
        writer.close()

        records = read_records(writer.path)
        assert [r["type"] for r in records] == ["session", "message"]

    def test_a_dangling_symlink_at_the_chosen_name_is_renamed_past(self, tmp_path):
        # `Path.exists()` follows a symlink and reports nothing there; the
        # writer's `O_EXCL` open does not follow it and refuses. The two
        # disagreeing turned a rename into a disabled log for the whole
        # attempt.
        directory = tmp_path / "alice"
        directory.mkdir(parents=True)
        wanted = session_log_path(tmp_path, IDENT, session_log._utcnow())
        wanted.symlink_to(tmp_path / "nowhere-at-all")

        writer = SessionLogWriter(tmp_path, IDENT, POLICY)
        writer.open()
        writer.result(success=True)
        writer.close()

        assert writer.path is not None
        assert writer.path != wanted
        assert not (tmp_path / "nowhere-at-all").exists()
        assert [r["type"] for r in read_records(writer.path)] == ["session", "result"]

    @pytest.mark.parametrize(
        "payload",
        [
            "bad \udcff byte",          # lone low surrogate
            "bad \ud800 byte",          # lone high surrogate
            "pair 😀 here",   # adjacent surrogates
            "astral 𝕏 here",            # a real astral codepoint
            "nul \x00 here",
            "esc \x1b[31m here",
        ],
    )
    def test_undecodable_content_does_not_turn_a_finished_run_into_a_traceback(
        self, tmp_path, payload
    ):
        # `backslashreplace` on a UTF-8 handle only ever escapes surrogates,
        # which render as `\uXXXX` and stay valid JSON. Every line must still
        # parse.
        writer = SessionLogWriter(tmp_path, IDENT, POLICY)
        writer.open()
        writer.message(UserMessage(content=[TextContent(text=payload)]))
        writer.result(success=True)
        writer.close()

        records = read_records(writer.path)
        assert [r["type"] for r in records] == ["session", "message", "result"]

    def test_non_ascii_is_written_as_utf8_rather_than_escaped(self, tmp_path):
        writer = SessionLogWriter(tmp_path, IDENT, POLICY)
        writer.open()
        writer.message(UserMessage(content=[TextContent(text="ćma — 日本語")]))
        writer.close()
        assert "ćma — 日本語" in writer.path.read_text(encoding="utf-8")

    def test_close_failing_is_debug_only(self, tmp_path, caplog):
        writer = SessionLogWriter(tmp_path, IDENT, POLICY)
        writer.open()

        class _BadClose:
            def flush(self):
                pass

            def close(self):
                raise OSError("nope")

        writer._fh = _BadClose()
        with caplog.at_level(logging.DEBUG, logger="istota.session.session_log"):
            writer.close()
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.requires_dac
class TestPermissions:
    """A session log holds the assembled prompt, which holds USER.md. There is
    no group-readable case."""

    def test_the_user_directory_is_0700_and_the_file_is_0600(self, tmp_path):
        root = tmp_path / "logs"
        writer = SessionLogWriter(root, IDENT, POLICY)
        writer.open()
        writer.close()

        assert stat.S_IMODE(os.stat(root / "alice").st_mode) == 0o700
        assert stat.S_IMODE(os.stat(writer.path).st_mode) == 0o600


# ==========================================================================
# The sweep
# ==========================================================================

# Wall clock rather than a fixed constant, and deliberately. Every mtime these
# cases care about is stamped relative to it, but a directory a test merely
# `mkdir`s carries the *real* clock — so a constant far from the real one silently
# reads those as ancient and the empty-directory rule removes them mid-case.
SWEEP_NOW = time.time()


def write_log(root: Path, user: str, name: str, *, size: int = 4096, age_days: float = 30.0,
              now: float = SWEEP_NOW) -> Path:
    """One log file of *size* bytes, stamped *age_days* before *now*."""
    directory = Path(root) / user
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"x" * size)
    stamp = now - age_days * 86400.0
    os.utime(path, (stamp, stamp))
    return path


def measured(*paths: Path) -> int:
    """The same du-style rule the sweep uses, so a ceiling can be derived from
    a tree rather than guessed at."""
    return sum(os.lstat(p).st_blocks * 512 for p in paths)


def age_dir(path: Path, days: float, now: float = SWEEP_NOW) -> None:
    stamp = now - days * 86400.0
    os.utime(path, (stamp, stamp))


def sweep(root, **kwargs) -> SweepResult:
    """`sweep_session_logs` with the real half-gibibyte floor dropped.

    Keeping it would mean every ceiling case below writing more than half a
    gigabyte. The clamp itself is asserted on its own in
    `test_a_ceiling_below_the_floor_is_clamped`, which does not use this helper
    and therefore gets the real floor.
    """
    kwargs.setdefault("retention_days", 0)
    kwargs.setdefault("max_total_gb", 0)
    kwargs.setdefault("now", SWEEP_NOW)
    kwargs.setdefault("floor_gb", 0)
    return sweep_session_logs(root, **kwargs)


class TestTheAgeRule:
    def test_a_file_older_than_the_cutoff_goes_and_a_recent_one_stays(self, tmp_path):
        old = write_log(tmp_path, "alice", "old.jsonl", age_days=30)
        fresh = write_log(tmp_path, "alice", "fresh.jsonl", age_days=1)

        result = sweep(tmp_path, retention_days=14)

        assert result.deleted_age == 1
        assert not old.exists()
        assert fresh.exists()

    def test_retention_days_zero_deletes_nothing_by_age(self, tmp_path):
        ancient = write_log(tmp_path, "alice", "ancient.jsonl", age_days=3650)
        result = sweep(tmp_path, retention_days=0)
        assert result.deleted_age == 0
        assert ancient.exists()

    def test_an_empty_user_directory_older_than_the_cutoff_is_removed(self, tmp_path):
        stale = tmp_path / "departed"
        stale.mkdir(parents=True)
        age_dir(stale, 60)

        result = sweep(tmp_path, retention_days=14)

        assert result.dirs_removed == 1
        assert not stale.exists()

    def test_an_empty_user_directory_younger_than_the_cutoff_survives(self, tmp_path):
        # The in-flight-task race: `open` creates the per-user directory and
        # writes its first record a moment later. A cleanup tick in between must
        # not rmdir it.
        fresh = tmp_path / "alice"
        fresh.mkdir(parents=True)
        age_dir(fresh, 0.001)

        result = sweep(tmp_path, retention_days=14)

        assert result.dirs_removed == 0
        assert fresh.exists()

    def test_a_directory_emptied_by_this_sweep_is_judged_on_its_pre_sweep_mtime(self, tmp_path):
        # Deleting the files stamps the directory `now`, so the age gate has to
        # read the mtime it had before the sweep touched it.
        write_log(tmp_path, "gone", "only.jsonl", age_days=60)
        age_dir(tmp_path / "gone", 60)

        result = sweep(tmp_path, retention_days=14)

        assert result.deleted_age == 1
        assert result.dirs_removed == 1
        assert not (tmp_path / "gone").exists()

    def test_a_user_directory_with_survivors_is_left_alone(self, tmp_path):
        kept = write_log(tmp_path, "alice", "keep.jsonl", age_days=1)
        age_dir(tmp_path / "alice", 60)

        result = sweep(tmp_path, retention_days=14)

        assert result.dirs_removed == 0
        assert kept.exists()

    def test_a_missing_root_is_not_an_error(self, tmp_path):
        result = sweep(tmp_path / "never-created", retention_days=14)
        assert result == SweepResult()

    def test_a_non_jsonl_file_is_left_alone_by_the_age_rule(self, tmp_path):
        write_log(tmp_path, "alice", "notes.txt", age_days=900)
        result = sweep(tmp_path, retention_days=14)
        assert result.deleted_age == 0
        assert (tmp_path / "alice" / "notes.txt").exists()


class TestTheCeiling:
    def test_the_tree_is_trimmed_to_the_ceiling_and_no_further(self, tmp_path):
        files = [
            write_log(tmp_path, "alice", f"{n}.jsonl", age_days=30 - n) for n in range(4)
        ]
        # Room for three of the four.
        ceiling = measured(*files[1:])

        result = sweep(tmp_path, max_total_gb=ceiling / (1024 ** 3))

        assert result.deleted_size == 1
        # Which files survived, not merely how many: the oldest goes first.
        assert not files[0].exists()
        assert all(f.exists() for f in files[1:])
        assert result.bytes_after <= ceiling
        assert result.still_over is False

    def test_max_total_gb_zero_disables_the_ceiling_and_the_age_rule_runs_on(self, tmp_path):
        keep = [write_log(tmp_path, "alice", f"{n}.jsonl", age_days=1) for n in range(4)]
        aged = write_log(tmp_path, "alice", "aged.jsonl", age_days=90)

        result = sweep(tmp_path, retention_days=14, max_total_gb=0)

        assert result.deleted_size == 0
        assert all(f.exists() for f in keep)
        # The other half of the `or` gate, from the opposite side.
        assert result.deleted_age == 1
        assert not aged.exists()
        assert result.bytes_after == measured(*keep)

    def test_a_non_finite_ceiling_reads_as_no_ceiling_rather_than_raising(self, tmp_path):
        # TOML accepts `inf` as a float, so this is a config-reachable value and
        # a plausible spelling of "no ceiling". `int(inf * GiB)` raises.
        files = [write_log(tmp_path, "alice", f"{n}.jsonl", age_days=30) for n in range(3)]
        result = sweep(tmp_path, max_total_gb=float("inf"))
        assert result.deleted_size == 0
        assert result.still_over is False
        assert all(f.exists() for f in files)

    def test_the_two_rules_are_independent(self, tmp_path):
        # `retention_days = 0` keeps everything indefinitely by age and must
        # still leave the disk bound in force. Wiring the gate as `and` is the
        # easy mistake and it silently disables the ceiling.
        files = [write_log(tmp_path, "alice", f"{n}.jsonl", age_days=1 + n) for n in range(4)]
        ceiling = measured(*files[:2])

        result = sweep(tmp_path, retention_days=0, max_total_gb=ceiling / (1024 ** 3))

        assert result.deleted_age == 0
        assert result.deleted_size == 2

    def test_the_ceiling_measures_the_whole_tree_across_every_user(self, tmp_path):
        # Neither user alone exceeds the ceiling; the pair does. A per-user
        # ceiling deletes nothing here.
        alice = [write_log(tmp_path, "alice", f"a{n}.jsonl", age_days=30 - n) for n in range(2)]
        bob = [write_log(tmp_path, "bob", f"b{n}.jsonl", age_days=30 - n) for n in range(2)]
        ceiling = measured(*alice, *bob) - 1

        result = sweep(tmp_path, max_total_gb=ceiling / (1024 ** 3))

        assert result.deleted_size >= 1

    def test_within_a_user_the_oldest_goes_first(self, tmp_path):
        oldest = write_log(tmp_path, "alice", "oldest.jsonl", age_days=90)
        middle = write_log(tmp_path, "alice", "middle.jsonl", age_days=60)
        newest = write_log(tmp_path, "alice", "newest.jsonl", age_days=30)
        ceiling = measured(newest)

        result = sweep(tmp_path, max_total_gb=ceiling / (1024 ** 3))

        assert result.deleted_size == 2
        assert not oldest.exists() and not middle.exists()
        assert newest.exists()

    def test_the_heaviest_user_is_trimmed_before_a_quiet_one(self, tmp_path):
        # The case that separates the specified rule from plain global
        # oldest-first, which passes every other assertion in this class.
        # The quiet user owns the *globally oldest* file, so oldest-first takes
        # it and evicts a quiet user's whole history for a noisy neighbour.
        quiet = write_log(tmp_path, "quiet", "ancient.jsonl", age_days=365)
        heavy = [
            write_log(tmp_path, "heavy", f"h{n}.jsonl", age_days=30 - n) for n in range(5)
        ]
        ceiling = measured(quiet, *heavy[:3])

        result = sweep(tmp_path, max_total_gb=ceiling / (1024 ** 3))

        assert quiet.exists(), "a quiet user's history was evicted for a noisy neighbour"
        assert result.deleted_size == 2
        assert not heavy[0].exists() and not heavy[1].exists()
        assert all(f.exists() for f in heavy[2:])

    def test_water_filling_alternates_once_two_users_converge(self, tmp_path):
        # Four each. Trimming to five files total must not empty one user
        # before touching the other: the sizes converge and eviction alternates.
        alice = [write_log(tmp_path, "alice", f"a{n}.jsonl", age_days=40 - n) for n in range(4)]
        bob = [write_log(tmp_path, "bob", f"b{n}.jsonl", age_days=30 - n) for n in range(4)]
        ceiling = measured(*alice[:3], *bob[:2])

        result = sweep(tmp_path, max_total_gb=ceiling / (1024 ** 3))

        assert result.deleted_size == 3
        surviving_alice = [f for f in alice if f.exists()]
        surviving_bob = [f for f in bob if f.exists()]
        assert len(surviving_alice) >= 2 and len(surviving_bob) >= 2

    def test_a_file_inside_the_live_window_is_never_evicted(self, tmp_path):
        # Evicting a running task's own file would corrupt the record of the run
        # happening now.
        live = write_log(tmp_path, "alice", "live.jsonl", age_days=0.0)
        older = write_log(tmp_path, "alice", "older.jsonl", age_days=30)
        ceiling = measured(older)

        result = sweep(tmp_path, max_total_gb=ceiling / (1024 ** 3))

        assert live.exists()
        assert not older.exists()
        assert result.deleted_size == 1

    def test_a_tree_that_is_all_live_reports_still_over_and_deletes_nothing(self, tmp_path):
        live = [
            write_log(tmp_path, "alice", f"{n}.jsonl", age_days=(LIVE_WINDOW_SECONDS / 4) / 86400.0)
            for n in range(3)
        ]
        result = sweep(tmp_path, max_total_gb=measured(live[0]) / (1024 ** 3))

        assert result.still_over is True
        assert result.deleted_size == 0
        assert all(f.exists() for f in live)

    def test_a_non_jsonl_file_holding_the_tree_over_reports_still_over(self, tmp_path):
        # The sweep counts what fills the volume and deletes only what it owns.
        big = write_log(tmp_path, "alice", "dump.txt", size=200_000, age_days=90)
        log = write_log(tmp_path, "alice", "one.jsonl", age_days=90)

        result = sweep(tmp_path, max_total_gb=measured(log) / (1024 ** 3))

        assert big.exists()
        assert not log.exists()
        assert result.deleted_size == 1
        assert result.still_over is True

    def test_deleted_size_is_reported_apart_from_deleted_age(self, tmp_path):
        # doctor keys its "retention_days is not the real retention" warning on
        # this field, so the two counters must not be merged.
        aged = write_log(tmp_path, "alice", "aged.jsonl", age_days=90)
        keep = [write_log(tmp_path, "alice", f"k{n}.jsonl", age_days=5 - n / 10) for n in range(3)]

        result = sweep(
            tmp_path,
            retention_days=14,
            max_total_gb=measured(*keep[:2]) / (1024 ** 3),
        )

        assert not aged.exists()
        assert result.deleted_age == 1
        assert result.deleted_size == 1

    def test_a_ceiling_below_the_floor_is_clamped(self, tmp_path):
        # No `floor_gb=0` here, so this gets the real half-gibibyte floor. A
        # kilobyte ceiling would otherwise empty the tree.
        files = [write_log(tmp_path, "alice", f"{n}.jsonl", age_days=90) for n in range(3)]

        result = sweep_session_logs(
            tmp_path, retention_days=0, max_total_gb=0.000001, now=SWEEP_NOW
        )

        assert result.deleted_size == 0
        assert all(f.exists() for f in files)

    def test_sizes_are_measured_du_style_rather_than_by_st_size(self, tmp_path):
        # A sparse file reports a large `st_size` and almost no blocks. A
        # ceiling driven by `st_size` deletes here; one driven by `st_blocks`
        # does not, because the file occupies nothing.
        directory = tmp_path / "alice"
        directory.mkdir(parents=True)
        sparse = directory / "sparse.jsonl"
        with open(sparse, "wb") as handle:
            handle.truncate(64 * 1024 * 1024)
        stamp = SWEEP_NOW - 90 * 86400.0
        os.utime(sparse, (stamp, stamp))

        if os.lstat(sparse).st_blocks * 512 >= os.lstat(sparse).st_size:
            pytest.skip("filesystem does not support sparse files")

        result = sweep(tmp_path, max_total_gb=(1024 * 1024) / (1024 ** 3))

        assert sparse.exists()
        assert result.deleted_size == 0
        assert result.bytes_after < os.lstat(sparse).st_size


class TestSweepRobustness:
    @pytest.mark.requires_dac
    def test_an_unreadable_user_directory_is_counted_and_does_not_abort_the_tick(self, tmp_path):
        locked = tmp_path / "locked"
        locked.mkdir()
        os.chmod(locked, 0)
        aged = write_log(tmp_path, "alice", "aged.jsonl", age_days=90)
        try:
            result = sweep(tmp_path, retention_days=14)
        finally:
            os.chmod(locked, stat.S_IRWXU)

        assert result.errors >= 1
        # The rest of the tree was still swept.
        assert not aged.exists()
        assert result.deleted_age == 1

    def test_a_file_that_vanished_under_the_sweep_does_not_cost_a_second_one(self, tmp_path):
        # ENOENT means the bytes are already off the volume, so the running
        # total has to come down. Leaving it up evicted a second file to
        # reclaim space that was free, and reported a `bytes_after` describing
        # a tree that no longer existed — on a delete path, an accounting error
        # in the direction of more deletion.
        first = write_log(tmp_path, "alice", "a.jsonl", age_days=90)
        second = write_log(tmp_path, "alice", "b.jsonl", age_days=60)
        third = write_log(tmp_path, "alice", "c.jsonl", age_days=30)
        ceiling = measured(second, third)

        real_unlink = Path.unlink

        def vanishing_unlink(self, *args, **kwargs):
            if self.name == "a.jsonl":
                real_unlink(self)
                raise FileNotFoundError(2, "No such file or directory", str(self))
            return real_unlink(self, *args, **kwargs)

        Path.unlink = vanishing_unlink
        try:
            result = sweep(tmp_path, max_total_gb=ceiling / (1024 ** 3))
        finally:
            Path.unlink = real_unlink

        assert not first.exists()
        assert second.exists() and third.exists()
        assert result.errors == 0
        assert result.bytes_after == measured(second, third)

    def test_a_file_at_the_root_is_not_mistaken_for_a_user_directory(self, tmp_path):
        stray = tmp_path / "stray.jsonl"
        stray.write_text("{}\n")
        result = sweep(tmp_path, retention_days=14)
        assert stray.exists()
        assert result.errors == 0

    def test_a_symlinked_user_entry_is_refused_and_its_target_untouched(self, tmp_path):
        outside = tmp_path / "outside-the-root"
        outside.mkdir()
        victim = outside / "precious.jsonl"
        victim.write_bytes(b"x" * 4096)
        stamp = SWEEP_NOW - 900 * 86400.0
        os.utime(victim, (stamp, stamp))

        root = tmp_path / "logs"
        root.mkdir()
        (root / "alice").symlink_to(outside, target_is_directory=True)

        result = sweep(root, retention_days=14, max_total_gb=0.000000001)

        assert victim.exists()
        assert result.deleted_age == 0
        assert result.deleted_size == 0

    def test_an_empty_root_reports_nothing(self, tmp_path):
        result = sweep(tmp_path, retention_days=14, max_total_gb=1.0)
        assert result == SweepResult()

    def test_the_sweep_never_raises_on_a_root_that_is_a_file(self, tmp_path):
        blocker = tmp_path / "afile"
        blocker.write_text("not a directory")
        result = sweep(blocker, retention_days=14, max_total_gb=1.0)
        assert isinstance(result, SweepResult)
        assert blocker.exists()


def test_the_module_reaches_nothing_but_llm_types():
    """A leaf: no config, no brain, no database, roots and policy as parameters.

    Stated in the docstring and held here, because an import added later is what
    quietly makes it not one — and this module is imported from the agent loop's
    hot path.
    """
    source = Path(session_log.__file__).read_text(encoding="utf-8")
    reached: list[str] = []
    # The AST rather than the lines: the import that actually gets added later
    # to a leaf on a hot path is the *lazy* one, indented inside a function or
    # a `TYPE_CHECKING` block, precisely because a top-level one would look
    # obviously wrong. A `startswith` on the raw line cannot see it.
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("istota"):
            reached.append(node.module)
        elif isinstance(node, ast.Import):
            reached.extend(a.name for a in node.names if a.name.startswith("istota"))

    assert reached == ["istota.llm.types"]
