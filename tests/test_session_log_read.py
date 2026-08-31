"""The session-log reader: parsing rules that two consumers have to share.

`session_log_read` exists because the `istota session` CLI and the `tasks
transcript` skill verb both have to answer the same questions about the same
files, and a second copy of the parsing is how the two start disagreeing about
what a transcript says. So the assertions here are about the *rules*, not about
a rendering: what counts as a readable file, what a malformed line costs, how a
turn is bounded, and what `max_chars` does when it bites.

Three cases carry most of the weight, and each separates a reader from a
`json.loads` loop:

- a file whose line 1 is not a `session` header is **unreadable**, not
  partially rendered. A loop happily renders whatever it can parse, which is
  how a truncated or foreign file gets presented as a transcript.
- a malformed line in the middle is skipped **and counted**. Silently skipping
  is worse than not reading at all, because the caller then believes it saw the
  whole run.
- a file whose last line is not a `result` is an interrupted run and says so.
  The naive reading is that the last line is the result record.

The fixtures write real records through the same shapes
`session/session_log.py` emits — snake_case fields, 1-based `attempt`, tool
results as `role: "tool_result"` — rather than the spec's illustrative JSON,
which differs in places.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from istota.session import session_log_read as reader

STAMP = "2026-08-31T14-22-01-993Z"


# --------------------------------------------------------------------------
# Fixture records — the shapes the writer actually emits
# --------------------------------------------------------------------------

def _session(task_id=4471, attempt=1, user_id="alice", **extra) -> dict:
    record = {
        "type": "session",
        "v": 1,
        "ts": "2026-08-31T14:22:01.993Z",
        "session_id": "01a054ad-2b57-7758-a0ac-9b27c39",
        "task_id": task_id,
        "attempt": attempt,
        "user_id": user_id,
        "source_type": "talk",
        "conversation_token": "a1b2c3d4",
        "is_group_chat": False,
        "brain": "native",
        "provider": "openai_compat",
        "base_url_host": "openrouter.ai",
        "model": "anthropic/claude-opus-4.8",
        "effort": "high",
    }
    record.update(extra)
    return record


def _context(**extra) -> dict:
    record = {
        "type": "context",
        "ts": "2026-08-31T14:22:02.000Z",
        "system_prompt": "You are a helpful assistant.",
        "tools": ["Bash", "Read"],
        "tools_schema_sha256": "3f9a",
        "system_prompt_source": "config/system-prompt.md",
    }
    record.update(extra)
    return record


def _user(text="rebuild the index") -> dict:
    return {
        "type": "message",
        "ts": "2026-08-31T14:22:02.100Z",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _assistant(*, text="", thinking="", calls=(), stop_reason="tool_use") -> dict:
    content: list[dict] = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking})
    if text:
        content.append({"type": "text", "text": text})
    for call_id, name, arguments in calls:
        content.append(
            {"type": "tool_call", "id": call_id, "name": name, "arguments": arguments}
        )
    return {
        "type": "message",
        "ts": "2026-08-31T14:22:03.000Z",
        "message": {
            "role": "assistant",
            "model": "anthropic/claude-opus-4.8",
            "stop_reason": stop_reason,
            "error_message": None,
            "usage": {
                "input_tokens": 24763,
                "output_tokens": 121,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "cost_usd": 0.06,
            },
            "content": content,
        },
    }


def _tool_result(call_id="call_1", name="Bash", text="total 48", is_error=False,
                 chars_total=None) -> dict:
    block: dict = {"type": "text", "text": text}
    if chars_total is not None:
        block["truncated"] = True
        block["chars_total"] = chars_total
    return {
        "type": "message",
        "ts": "2026-08-31T14:22:04.000Z",
        "message": {
            "role": "tool_result",
            "tool_call_id": call_id,
            "tool_name": name,
            "is_error": is_error,
            "content": [block],
        },
    }


def _result(stop_reason="completed", success=True, text="Done.") -> dict:
    return {
        "type": "result",
        "ts": "2026-08-31T14:25:00.000Z",
        "success": success,
        "stop_reason": stop_reason,
        "result_text": text,
        "model_used": "anthropic/claude-opus-4.8",
        "duration_ms": 184320,
        "usage": {"input_tokens": 124763, "output_tokens": 8121, "cost_usd": 0.94},
        "turns": 2,
        "compactions": 0,
        "truncated_records": 0,
    }


def write_log(path: Path, records, *, trailing_newline=True, extra_lines=()) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r, ensure_ascii=False) for r in records]
    lines.extend(extra_lines)
    body = "\n".join(lines)
    if trailing_newline:
        body += "\n"
    path.write_text(body, encoding="utf-8")
    return path


def full_session(path: Path, *, user_id="alice", task_id=None, attempt=None) -> Path:
    """The canonical two-turn run: prompt, tool call, tool result, answer.

    The three identity fields default to **what the path says**, and a fixture
    that wants a disagreement has to write one deliberately. The reader reports
    a header disagreeing with the name or the directory, so a fixture disagreeing
    by accident makes every tree look tampered with — and worse, it hides the
    real assertion: this helper used to write `task_id=4471` into a file named
    `task-5000-1`, which is exactly the defect the reporting exists to catch,
    sitting unnoticed in the fixture every listing test ran against.
    """
    parsed = reader.parse_log_name(path.name) or {}
    return write_log(
        path,
        [
            _session(
                user_id=user_id,
                task_id=parsed.get("task_id", 4471) if task_id is None else task_id,
                attempt=parsed.get("attempt", 1) if attempt is None else attempt,
            ),
            _context(),
            _user(),
            _assistant(
                thinking="I should list the directory first.",
                text="Let me look.",
                calls=[("call_1", "Bash", {"command": "ls -la"})],
            ),
            _tool_result(text="total 48\ndrwxr-xr-x 4 root root"),
            _assistant(text="Done. The index rebuilt.", stop_reason="end_turn"),
            _result(),
        ],
    )


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A fixture root with two users and four sessions.

    alice: task 4471 attempt 1 (complete), task 4471 attempt 2 (interrupted),
           task 5000 attempt 1 (complete)
    bob:   task 6000 attempt 1 (complete)
    """
    root = tmp_path / "logs"
    full_session(root / "alice" / f"{STAMP}_task-4471-1.jsonl")
    write_log(
        root / "alice" / "2026-08-31T15-00-00-000Z_task-4471-2.jsonl",
        [_session(attempt=2), _context(), _user(), _assistant(text="half a run")],
    )
    full_session(root / "alice" / "2026-08-31T16-00-00-000Z_task-5000-1.jsonl")
    full_session(
        root / "bob" / "2026-08-31T14-00-00-000Z_task-6000-1.jsonl", user_id="bob",
    )
    return root


# --------------------------------------------------------------------------

class TestReadRecords:
    def test_it_yields_every_record_in_file_order(self, tmp_path):
        path = full_session(tmp_path / "a" / f"{STAMP}_task-1-1.jsonl")
        kinds = [r["type"] for r in reader.read_records(path)]
        assert kinds == [
            "session", "context", "message", "message", "message", "message", "result",
        ]

    def test_a_malformed_middle_line_is_skipped_and_counted(self, tmp_path):
        path = tmp_path / "a" / f"{STAMP}_task-1-1.jsonl"
        write_log(
            path,
            [_session(), _context()],
            extra_lines=["{not json", json.dumps(_result())],
        )
        stats = reader.ReadStats()
        kinds = [r["type"] for r in reader.read_records(path, stats=stats)]
        assert kinds == ["session", "context", "result"]
        # Counted, not merely survived: a caller that cannot tell it lost a line
        # believes it saw the whole run.
        assert stats.malformed == 1
        assert stats.records == 3

    def test_a_json_line_that_is_not_an_object_counts_as_malformed(self, tmp_path):
        path = tmp_path / "a" / f"{STAMP}_task-1-1.jsonl"
        write_log(path, [_session()], extra_lines=["[1, 2, 3]", '"a string"'])
        stats = reader.ReadStats()
        kinds = [r["type"] for r in reader.read_records(path, stats=stats)]
        assert kinds == ["session"]
        assert stats.malformed == 2

    def test_a_trailing_partial_line_is_skipped_as_a_live_write(self, tmp_path):
        # A session being written right now: the last line has no newline yet.
        path = tmp_path / "a" / f"{STAMP}_task-1-1.jsonl"
        write_log(
            path,
            [_session(), _context()],
            trailing_newline=False,
            extra_lines=['{"type":"messa'],
        )
        stats = reader.ReadStats()
        kinds = [r["type"] for r in reader.read_records(path, stats=stats)]
        assert kinds == ["session", "context"]
        # Distinguished from a corrupt line: one is expected on a live file and
        # the other is damage.
        assert stats.partial_tail == 1
        assert stats.malformed == 0

    def test_a_blank_line_is_neither_a_record_nor_damage(self, tmp_path):
        path = tmp_path / "a" / f"{STAMP}_task-1-1.jsonl"
        write_log(path, [_session()], extra_lines=["", "   ", json.dumps(_result())])
        stats = reader.ReadStats()
        kinds = [r["type"] for r in reader.read_records(path, stats=stats)]
        assert kinds == ["session", "result"]
        assert stats.malformed == 0

    def test_skip_malformed_false_surfaces_the_bad_line_instead_of_raising(self, tmp_path):
        path = tmp_path / "a" / f"{STAMP}_task-1-1.jsonl"
        write_log(path, [_session()], extra_lines=["{not json"])
        records = list(reader.read_records(path, skip_malformed=False))
        assert [r["type"] for r in records] == ["session", "malformed"]
        assert records[1]["line"] == 2
        assert "not json" in records[1]["raw"]

    def test_a_missing_file_yields_nothing_and_says_why(self, tmp_path):
        stats = reader.ReadStats()
        assert list(reader.read_records(tmp_path / "nope.jsonl", stats=stats)) == []
        assert stats.unreadable

    def test_a_directory_in_place_of_a_file_does_not_raise(self, tmp_path):
        (tmp_path / "d.jsonl").mkdir()
        stats = reader.ReadStats()
        assert list(reader.read_records(tmp_path / "d.jsonl", stats=stats)) == []
        assert stats.unreadable

    def test_undecodable_bytes_do_not_raise(self, tmp_path):
        path = tmp_path / f"{STAMP}_task-1-1.jsonl"
        path.write_bytes(json.dumps(_session()).encode() + b"\n\xff\xfe\n")
        stats = reader.ReadStats()
        kinds = [r["type"] for r in reader.read_records(path, stats=stats)]
        assert kinds == ["session"]
        assert stats.malformed == 1


class TestReadHeader:
    def test_line_one_is_returned_when_it_is_a_session_record(self, tmp_path):
        path = full_session(tmp_path / "a" / f"{STAMP}_task-4471-1.jsonl")
        header = reader.read_header(path)
        assert header["task_id"] == 4471
        # 1-based, as the writer emits it.
        assert header["attempt"] == 1

    def test_a_file_whose_first_line_is_not_a_header_is_none(self, tmp_path):
        path = tmp_path / f"{STAMP}_task-1-1.jsonl"
        write_log(path, [_context(), _session(), _result()])
        assert reader.read_header(path) is None

    def test_a_malformed_first_line_is_none(self, tmp_path):
        path = tmp_path / f"{STAMP}_task-1-1.jsonl"
        write_log(path, [], extra_lines=["{not json", json.dumps(_session())])
        assert reader.read_header(path) is None

    def test_an_empty_file_is_none(self, tmp_path):
        path = tmp_path / f"{STAMP}_task-1-1.jsonl"
        path.write_text("", encoding="utf-8")
        assert reader.read_header(path) is None

    def test_a_missing_file_is_none(self, tmp_path):
        assert reader.read_header(tmp_path / "nope.jsonl") is None

    def test_it_reads_only_the_first_line(self, tmp_path):
        # A 6 MB file must not be read whole to answer "whose is this".
        path = tmp_path / f"{STAMP}_task-1-1.jsonl"
        big = _user("x" * 3_000_000)
        write_log(path, [_session(), big, big, _result()])
        assert reader.read_header(path)["task_id"] == 4471


class TestReadLastRecord:
    def test_it_returns_the_terminal_result_record(self, tmp_path):
        path = full_session(tmp_path / "a" / f"{STAMP}_task-1-1.jsonl")
        last = reader.read_last_record(path)
        assert last["type"] == "result"
        assert last["stop_reason"] == "completed"

    def test_an_interrupted_run_returns_whatever_it_stopped_on(self, tmp_path):
        path = tmp_path / f"{STAMP}_task-1-1.jsonl"
        write_log(path, [_session(), _context(), _assistant(text="mid-run")])
        assert reader.read_last_record(path)["type"] == "message"

    def test_a_trailing_partial_line_is_not_the_last_record(self, tmp_path):
        path = tmp_path / f"{STAMP}_task-1-1.jsonl"
        write_log(
            path, [_session(), _result()],
            trailing_newline=False, extra_lines=['{"type":"res'],
        )
        assert reader.read_last_record(path)["type"] == "result"

    def test_it_finds_the_last_record_past_a_long_tail(self, tmp_path):
        # The backward read takes a bounded window; a complete last record
        # sitting behind an oversized *earlier* record must still be found.
        #
        # The size assertion is the point of the test, not decoration. At the
        # 200,000 this used to write, the whole file fit inside `_TAIL_WINDOW`
        # and the read never took the windowed path at all — it passed through
        # the plain whole-file branch and could not have failed for the property
        # its own comment names. Asserting the file is over the window is what
        # stops a later bump to that constant from quietly neutering it again.
        path = tmp_path / f"{STAMP}_task-1-1.jsonl"
        write_log(path, [_session(), _user("y" * 400_000), _result()])
        assert path.stat().st_size > reader._TAIL_WINDOW
        assert reader.read_last_record(path)["type"] == "result"

    def test_a_single_enormous_line_is_still_found(self, tmp_path):
        path = tmp_path / f"{STAMP}_task-1-1.jsonl"
        write_log(path, [_session(), _result(text="z" * 300_000)])
        last = reader.read_last_record(path)
        assert last["type"] == "result"
        assert len(last["result_text"]) == 300_000

    def test_a_missing_file_is_none(self, tmp_path):
        assert reader.read_last_record(tmp_path / "nope.jsonl") is None


class TestParseLogName:
    def test_it_reads_the_task_and_attempt_out_of_the_name(self):
        parsed = reader.parse_log_name(f"{STAMP}_task-4471-1.jsonl")
        assert parsed["task_id"] == 4471
        assert parsed["attempt"] == 1
        assert parsed["stamp"] == STAMP
        assert parsed["suffix"] == ""

    def test_a_collision_suffix_does_not_change_the_task_or_attempt(self):
        parsed = reader.parse_log_name(f"{STAMP}_task-4471-1-b3f9.jsonl")
        assert (parsed["task_id"], parsed["attempt"]) == (4471, 1)
        assert parsed["suffix"] == "b3f9"

    def test_a_counted_collision_suffix_parses_too(self):
        parsed = reader.parse_log_name(f"{STAMP}_task-4471-1-b3f9-2.jsonl")
        assert (parsed["task_id"], parsed["attempt"]) == (4471, 1)

    @pytest.mark.parametrize("name", [
        "notes.txt",
        f"{STAMP}_task-4471-1.txt",
        f"{STAMP}_task-abc-1.jsonl",
        f"{STAMP}_task-4471.jsonl",
        "task-4471-1.jsonl",
    ])
    def test_a_name_that_is_not_ours_is_none(self, name):
        assert reader.parse_log_name(name) is None


class TestFindLogs:
    def test_it_returns_one_users_logs_newest_first(self, tree):
        found = reader.find_logs(tree, "alice")
        assert [p.name.split("_task-")[1] for p in found] == [
            "5000-1.jsonl", "4471-2.jsonl", "4471-1.jsonl",
        ]

    def test_a_task_filter_finds_every_attempt(self, tree):
        found = reader.find_logs(tree, "alice", task_id=4471)
        assert [reader.parse_log_name(p.name)["attempt"] for p in found] == [2, 1]

    def test_it_never_returns_another_users_file(self, tree):
        found = reader.find_logs(tree, "alice", task_id=6000)
        assert found == []
        # Negative control: the same task id under its owner does resolve, so
        # the assertion above is about scoping and not about a bad task id.
        assert len(reader.find_logs(tree, "bob", task_id=6000)) == 1

    @pytest.mark.parametrize("user_id", ["", ".", "..", "a/b", "../bob", "/etc"])
    def test_a_user_id_that_is_not_one_component_finds_nothing(self, tree, user_id):
        # The scoped finder must not be talked into a different directory, and
        # an empty string must not quietly mean "every user" — the skill verb
        # is the caller and its scope is the boundary.
        assert reader.find_logs(tree, user_id) == []

    def test_a_missing_root_is_empty_rather_than_an_error(self, tmp_path):
        assert reader.find_logs(tmp_path / "nope", "alice") == []

    def test_non_jsonl_files_are_ignored(self, tree):
        (tree / "alice" / "notes.txt").write_text("hello", encoding="utf-8")
        assert all(p.suffix == ".jsonl" for p in reader.find_logs(tree, "alice"))

    def test_a_symlinked_entry_is_not_followed(self, tree, tmp_path):
        outside = tmp_path / "elsewhere.jsonl"
        full_session(outside)
        link = tree / "alice" / "2026-09-01T00-00-00-000Z_task-9999-1.jsonl"
        link.symlink_to(outside)
        assert all(not p.is_symlink() for p in reader.find_logs(tree, "alice"))


class TestFindAllLogs:
    def test_it_spans_every_user(self, tree):
        found = reader.find_all_logs(tree)
        assert len(found) == 4
        assert {p.parent.name for p in found} == {"alice", "bob"}

    def test_a_task_filter_crosses_users(self, tree):
        assert len(reader.find_all_logs(tree, task_id=6000)) == 1

    def test_a_missing_root_is_empty(self, tmp_path):
        assert reader.find_all_logs(tmp_path / "nope") == []


class TestSummarize:
    def test_a_finished_run_carries_its_stop_reason_and_turn_count(self, tmp_path):
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        row = reader.summarize(path)
        assert row["readable"] is True
        assert row["complete"] is True
        assert row["stop_reason"] == "completed"
        assert row["turns"] == 2
        assert row["task_id"] == 4471
        assert row["attempt"] == 1
        assert row["user_id"] == "alice"
        assert row["size"] > 0

    def test_an_interrupted_run_is_a_row_and_not_an_omission(self, tmp_path):
        # The last line is an assistant message, not a `result`: the daemon
        # died, or the run is still going. Naively reading the last line as the
        # result record is what this separates.
        path = tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl"
        write_log(path, [_session(), _context(), _user(), _assistant(text="mid")])
        row = reader.summarize(path)
        assert row["readable"] is True
        assert row["complete"] is False
        assert row["stop_reason"] == ""
        assert row["task_id"] == 4471

    def test_a_file_with_no_header_is_unreadable_but_still_a_row(self, tmp_path):
        path = tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl"
        write_log(path, [_context(), _result()])
        row = reader.summarize(path)
        assert row["readable"] is False
        assert row["reason"]
        # The identity still comes off the file name, which is what the naming
        # convention is for.
        assert row["task_id"] == 4471
        assert row["user_id"] == "alice"


class TestDigest:
    def test_it_reports_the_tools_in_order_with_their_status_and_sizes(self, tmp_path):
        path = write_log(
            tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl",
            [
                _session(), _context(), _user(),
                _assistant(calls=[("call_1", "Bash", {"command": "ls"})]),
                _tool_result("call_1", "Bash", text="total 48"),
                _assistant(calls=[("call_2", "Read", {"file_path": "/x"})]),
                _tool_result("call_2", "Read", text="boom", is_error=True),
                _assistant(text="done", stop_reason="end_turn"),
                _result(),
            ],
        )
        out = reader.digest(path)
        assert out["ok"] is True
        assert [t["name"] for t in out["tools"]] == ["Bash", "Read"]
        assert out["tools"][0]["is_error"] is False
        assert out["tools"][0]["output_chars"] == len("total 48")
        assert out["tools"][1]["is_error"] is True
        assert out["turns"] == 3

    def test_a_capped_tool_result_reports_the_size_the_model_saw_and_the_real_one(
        self, tmp_path,
    ):
        path = write_log(
            tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl",
            [
                _session(), _context(), _user(),
                _assistant(calls=[("call_1", "Bash", {"command": "ls"})]),
                _tool_result("call_1", text="short", chars_total=180999),
                _result(),
            ],
        )
        tool = reader.digest(path)["tools"][0]
        assert tool["output_chars"] == len("short")
        assert tool["output_chars_total"] == 180999
        assert tool["truncated"] is True

    def test_a_tool_call_with_no_result_is_reported_unanswered(self, tmp_path):
        path = write_log(
            tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl",
            [
                _session(), _context(), _user(),
                _assistant(calls=[("call_1", "Bash", {"command": "ls"})]),
                _result(stop_reason="timeout", success=False),
            ],
        )
        tool = reader.digest(path)["tools"][0]
        assert tool["answered"] is False

    def test_compactions_steers_and_nudges_are_counted_and_described(self, tmp_path):
        path = write_log(
            tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl",
            [
                _session(), _context(), _user(),
                {"type": "steer", "ts": "t", "text": "check staging first"},
                {"type": "nudge", "ts": "t", "phase": "early", "remaining": 50},
                {"type": "compaction", "ts": "t", "trigger": "proactive",
                 "messages_dropped": 41, "recovery_index": None},
                {"type": "compaction", "ts": "t", "trigger": "overflow",
                 "messages_dropped": 12, "recovery_index": 1},
                _assistant(text="ok", stop_reason="end_turn"),
                _result(),
            ],
        )
        out = reader.digest(path)
        assert [c["trigger"] for c in out["compactions"]] == ["proactive", "overflow"]
        assert out["compactions"][1]["recovery_index"] == 1
        assert out["steers"] == 1
        assert out["nudges"] == 1

    def test_an_error_record_is_surfaced(self, tmp_path):
        path = write_log(
            tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl",
            [
                _session(), _context(), _user(),
                {"type": "error", "ts": "t", "kind": "ProviderError",
                 "message": "429 rate limited", "traceback": "Traceback…"},
                _result(stop_reason="error", success=False),
            ],
        )
        out = reader.digest(path)
        assert out["errors"][0]["kind"] == "ProviderError"
        assert out["result"]["stop_reason"] == "error"
        assert out["result"]["success"] is False

    def test_the_result_summary_carries_no_result_text_body(self, tmp_path):
        # The digest is a summary a model or an operator reads at a glance; a
        # multi-megabyte deliverable does not belong in it.
        path = write_log(
            tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl",
            [_session(), _context(), _user(), _result(text="q" * 40_000)],
        )
        out = reader.digest(path)
        assert "result_text" not in out["result"]
        assert out["result"]["result_text_chars"] == 40_000
        assert len(out["result"]["result_text_preview"]) < 1000

    def test_an_interrupted_run_has_no_result_and_says_so(self, tmp_path):
        path = write_log(
            tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl",
            [_session(), _context(), _user(), _assistant(text="mid")],
        )
        out = reader.digest(path)
        assert out["ok"] is True
        assert out["complete"] is False
        assert out["result"] is None

    def test_a_malformed_line_is_counted_in_the_digest(self, tmp_path):
        path = tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl"
        write_log(path, [_session(), _context()], extra_lines=["{oops", json.dumps(_result())])
        out = reader.digest(path)
        assert out["ok"] is True
        assert out["malformed"] == 1

    def test_a_file_whose_first_line_is_not_a_header_is_not_ok(self, tmp_path):
        path = tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl"
        write_log(path, [_context(), _user(), _result()])
        out = reader.digest(path)
        assert out["ok"] is False
        assert out["reason"]
        # Not partially rendered: nothing from the body leaks into the answer.
        assert out.get("tools") in (None, [])

    def test_a_missing_file_is_not_ok(self, tmp_path):
        out = reader.digest(tmp_path / "nope.jsonl")
        assert out["ok"] is False
        assert out["reason"]


class TestExcerpt:
    def test_with_no_selector_it_returns_the_conversation(self, tmp_path):
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        out = reader.excerpt(path)
        assert out["ok"] is True
        assert [r["type"] for r in out["records"]] == ["message"] * 4

    def test_thinking_is_absent_by_default_and_present_on_request(self, tmp_path):
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")

        def kinds(records):
            return [
                b["type"]
                for r in records
                for b in r.get("message", {}).get("content", [])
            ]

        assert "thinking" not in kinds(reader.excerpt(path)["records"])
        assert "thinking" in kinds(reader.excerpt(path, thinking=True)["records"])

    def test_dropping_thinking_does_not_mutate_the_other_blocks(self, tmp_path):
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        out = reader.excerpt(path)
        texts = [
            b["text"]
            for r in out["records"]
            for b in r.get("message", {}).get("content", [])
            if b["type"] == "text"
        ]
        assert "Let me look." in texts

    def test_turn_selects_one_assistant_turn_and_its_results(self, tmp_path):
        path = write_log(
            tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl",
            [
                _session(), _context(), _user(),
                _assistant(text="one", calls=[("c1", "Bash", {"command": "a"})]),
                _tool_result("c1", text="first output"),
                _assistant(text="two", calls=[("c2", "Bash", {"command": "b"})]),
                _tool_result("c2", text="second output"),
                _assistant(text="three", stop_reason="end_turn"),
                _result(),
            ],
        )
        out = reader.excerpt(path, turn=2)
        blob = json.dumps(out["records"])
        assert "second output" in blob
        assert "first output" not in blob
        assert out["turn_count"] == 3

    def test_a_turn_out_of_range_is_an_honest_empty_selection(self, tmp_path):
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        out = reader.excerpt(path, turn=99)
        assert out["ok"] is True
        assert out["records"] == []
        assert out["turn_count"] == 2

    def test_tools_selects_the_tool_results(self, tmp_path):
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        out = reader.excerpt(path, tools=True)
        roles = {r["message"]["role"] for r in out["records"]}
        assert roles == {"tool_result"}

    def test_grep_matches_the_content_and_not_the_field_names(self, tmp_path):
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        assert reader.excerpt(path, grep="drwxr-xr-x")["records"]
        # `tool_call_id` is in every tool result record's JSON, and matching it
        # would make grep return the whole file for a pattern nothing said.
        assert reader.excerpt(path, grep="tool_call_id")["records"] == []

    def test_grep_searches_a_tool_call_command(self, tmp_path):
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        assert reader.excerpt(path, grep="ls -la")["records"]

    def test_an_invalid_pattern_is_refused_rather_than_raised(self, tmp_path):
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        out = reader.excerpt(path, grep="a[b")
        assert out["ok"] is False
        assert "pattern" in out["reason"].lower()

    def test_max_chars_stops_early_and_reports_the_truncation(self, tmp_path):
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        out = reader.excerpt(path, max_chars=200)
        assert out["truncated"] is True
        assert out["records_returned"] < out["records_total"]
        assert out["chars"] <= 200

    def test_max_chars_zero_is_no_cap(self, tmp_path):
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        out = reader.excerpt(path, max_chars=0)
        assert out["truncated"] is False
        assert out["records_returned"] == out["records_total"] == 4

    def test_a_cap_below_one_record_still_returns_something_and_says_it_cut(
        self, tmp_path,
    ):
        # Returning nothing at all reads as "the run had no conversation",
        # which is a different and wrong answer.
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        out = reader.excerpt(path, max_chars=1)
        assert out["truncated"] is True
        assert out["records_returned"] == 1

    def test_a_file_whose_first_line_is_not_a_header_is_not_ok(self, tmp_path):
        path = tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl"
        write_log(path, [_context(), _user("secret prompt"), _result()])
        out = reader.excerpt(path)
        assert out["ok"] is False
        assert out["records"] == []
        assert "secret prompt" not in json.dumps(out)

    def test_a_malformed_line_is_counted(self, tmp_path):
        path = tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl"
        write_log(
            path, [_session(), _context(), _user()],
            extra_lines=["{oops", json.dumps(_result())],
        )
        out = reader.excerpt(path)
        assert out["malformed"] == 1

    def test_the_header_travels_with_the_excerpt(self, tmp_path):
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        out = reader.excerpt(path)
        assert out["header"]["task_id"] == 4471
        assert out["header"]["model"] == "anthropic/claude-opus-4.8"


class TestNeverRaises:
    """Both consumers run somewhere an exception is expensive: a scripted
    `istota session` call and a skill verb answering a task. Every entry point
    takes a path from somewhere else and must degrade to an answer."""

    @pytest.mark.parametrize("call", [
        lambda p: list(reader.read_records(p)),
        lambda p: reader.read_header(p),
        lambda p: reader.read_last_record(p),
        lambda p: reader.summarize(p),
        lambda p: reader.digest(p),
        lambda p: reader.excerpt(p),
    ])
    def test_a_path_that_is_a_directory(self, tmp_path, call):
        (tmp_path / "d.jsonl").mkdir()
        call(tmp_path / "d.jsonl")

    @pytest.mark.parametrize("call", [
        lambda p: list(reader.read_records(p)),
        lambda p: reader.read_header(p),
        lambda p: reader.read_last_record(p),
        lambda p: reader.summarize(p),
        lambda p: reader.digest(p),
        lambda p: reader.excerpt(p),
    ])
    def test_a_path_that_does_not_exist(self, tmp_path, call):
        call(tmp_path / "nope" / "x.jsonl")

    @pytest.mark.requires_dac
    def test_an_unreadable_file_degrades(self, tmp_path):
        # `requires_dac` because the assertion is about a permission bit, and
        # root bypasses them: `scripts/test-linux.sh` runs as root in a
        # container, where the file is still readable, `digest` answers ok and
        # the tier goes red for a reason that says nothing about the code.
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        path.chmod(0o000)
        try:
            assert reader.digest(path)["ok"] is False
        finally:
            path.chmod(0o600)


class TestARecordFieldOfTheWrongType:
    """Every field read off a line comes from `json.loads`, so its type is
    whatever was written and not what the writer meant.

    `TestNeverRaises` above parametrizes *filesystem* states — a directory, a
    missing path, mode 000 — and never a value of an unexpected shape, so this
    whole class was untested by construction. `digest` raised `TypeError` on
    three of them and it escaped all the way out of `istota session show`. The
    module's premise is that the file may be damaged, half-written, or somebody
    else's JSONL that happens to open with a plausible header; in the second
    consumer a traceback here is a failed task.
    """

    @pytest.mark.parametrize("record", [
        # `content` a scalar, where `for block in ...` was iterating it.
        {"type": "message", "ts": "t",
         "message": {"role": "assistant", "content": 7}},
        {"type": "message", "ts": "t",
         "message": {"role": "tool_result", "tool_call_id": "c", "content": 7}},
        # `len()` on a number.
        {"type": "context", "ts": "t", "system_prompt": 7, "tools": 7},
        {"type": "compaction", "ts": "t", "summary": 7, "trigger": 7},
        {"type": "error", "ts": "t", "kind": 7, "message": 7},
        {"type": "result", "ts": "t", "result_text": 7, "stop_reason": 7},
        # The containers themselves being the wrong shape.
        {"type": "message", "ts": "t", "message": "not a dict"},
        {"type": "message", "ts": "t", "message": [1, 2, 3]},
        {"type": 7, "ts": "t"},
    ])
    @pytest.mark.parametrize("call", [
        lambda p: reader.digest(p),
        lambda p: reader.excerpt(p),
        lambda p: reader.excerpt(p, tools=True),
        lambda p: reader.excerpt(p, grep="7"),
        lambda p: reader.summarize(p),
        lambda p: list(reader.read_records(p)),
    ])
    def test_no_entry_point_raises_on_it(self, tmp_path, record, call):
        path = write_log(
            tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl",
            [_session(), record, _result()],
        )
        call(path)

    def test_the_header_itself_being_the_wrong_shape_is_survivable(self, tmp_path):
        path = write_log(
            tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl",
            [{"type": "session", "task_id": [], "user_id": 7, "attempt": {}},
             _user(), _result()],
        )
        assert reader.digest(path)["ok"] is True
        assert reader.summarize(path)["readable"] is True

    def test_a_scalar_content_still_produces_a_tool_row(self, tmp_path):
        # Degrading, not dropping: the call is still in the file and a reader
        # asking "what did it run" gets an answer with zero output rather than
        # a missing row.
        path = write_log(
            tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl",
            [
                _session(), _context(), _user(),
                _assistant(calls=[("c1", "Bash", {"command": "ls"})]),
                {"type": "message", "ts": "t",
                 "message": {"role": "tool_result", "tool_call_id": "c1",
                             "tool_name": "Bash", "content": 7}},
                _result(),
            ],
        )
        tool = reader.digest(path)["tools"][0]
        assert tool["name"] == "Bash"
        assert tool["answered"] is True
        assert tool["output_chars"] == 0


class TestAReadThatStopsPartway:
    """`read_records` has always recorded a mid-read failure and nothing read it
    back, so a truncated record set came out under `ok: True` with an empty
    reason — the caller believing it saw the whole run, which is the one failure
    the malformed count exists to prevent, arriving by the counted route."""

    def _dies_after(self, monkeypatch, after: int):
        """`read_records` stopping partway, the way an EIO makes it.

        `read_records`' own half — catching the failure and recording it — is
        covered by `TestNeverRaises`. What was missing is the other half: the
        two summarizing entry points carrying the field out, which is what
        turned a truncated read into a clean `ok: True`. So the trigger is
        substituted and the plumbing is what is asserted.
        """
        real = reader.read_records

        def _partial(path, *, skip_malformed=True, stats=None):
            for index, record in enumerate(
                real(path, skip_malformed=skip_malformed, stats=stats)
            ):
                if index >= after:
                    if stats is not None:
                        stats.unreadable = "OSError: [Errno 5] Input/output error"
                    return
                yield record

        monkeypatch.setattr(reader, "read_records", _partial)

    def test_digest_reports_it_rather_than_calling_the_run_complete(
        self, tmp_path, monkeypatch,
    ):
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        self._dies_after(monkeypatch, after=3)
        out = reader.digest(path)
        assert out["unreadable"]
        # And it is not reported as a run that finished: the terminal record is
        # one of the ones that never arrived.
        assert out["complete"] is False

    def test_excerpt_reports_it_too(self, tmp_path, monkeypatch):
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        self._dies_after(monkeypatch, after=3)
        assert reader.excerpt(path)["unreadable"]

    def test_a_clean_read_reports_nothing(self, tmp_path):
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        assert reader.digest(path)["unreadable"] == ""
        assert reader.excerpt(path)["unreadable"] == ""


class TestIdentityComesOffTheNameAndNotTheHeader:
    """The three fields every lookup filters on, and the rule that they are the
    name's and the directory's rather than the file body's."""

    def test_a_header_task_id_does_not_relabel_the_row(self, tmp_path):
        # The defect this pins: `list --task 5000` printed a row labelled "task
        # 4471", after which `show 4471` found nothing — the listing named a
        # task the finder could not resolve.
        path = write_log(
            tmp_path / "alice" / "2026-08-31T16-00-00-000Z_task-5000-1.jsonl",
            [_session(task_id=4471, attempt=9), _context(), _user(), _result()],
        )
        row = reader.summarize(path)
        assert row["task_id"] == 5000
        assert row["attempt"] == 1
        assert row["header_task_id"] == 4471
        assert row["header_attempt"] == 9

    def test_an_agreeing_header_reports_no_disagreement(self, tmp_path):
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        row = reader.summarize(path)
        assert row["header_task_id"] is None
        assert row["header_attempt"] is None
        assert row["header_user_id"] == ""

    def test_a_file_deleted_between_the_stat_and_the_header_read_says_gone(
        self, tmp_path, monkeypatch,
    ):
        # `read_header` conflates "gone" with "not a transcript" because a
        # caller needing the difference has already stat'ed. `summarize` is that
        # caller, and without a re-check an operator listing a tree during a
        # sweep gets false corruption reports.
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        real = reader.read_header

        def _unlink_then_read(p):
            # The window itself: the stat above has already succeeded.
            Path(p).unlink(missing_ok=True)
            return real(p)

        monkeypatch.setattr(reader, "read_header", _unlink_then_read)
        row = reader.summarize(path)
        assert "no longer exists" in row["reason"]
        assert "header" not in row["reason"]
        assert row["size"] > 0  # the stat did happen, so this is the race

    def test_a_file_that_really_is_headerless_still_says_so(self, tmp_path):
        # The control for the arm above: the file exists, so the reason must be
        # the header rule and not the existence check.
        path = tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl"
        write_log(path, [_context(), _user(), _result()])
        assert "header" in reader.summarize(path)["reason"]


class TestGrepStaysInsideTheConversation:
    def test_it_never_returns_the_uncapped_result_record(self, tmp_path):
        # `result_text` is uncapped by the writer because it is the deliverable,
        # and `max_chars` always keeps its first record whatever that costs — so
        # a one-character pattern matching a result returned a record of
        # unbounded size with a cap in force.
        path = write_log(
            tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl",
            [_session(), _context(), _user(), _assistant(text="ok"),
             _result(text="q" * 200_000)],
        )
        out = reader.excerpt(path, grep="q", max_chars=1000)
        assert [r["type"] for r in out["records"]] == []
        assert out["chars"] == 0

    def test_a_dot_pattern_returns_the_conversation_and_nothing_else(
        self, tmp_path,
    ):
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        kinds = {r["type"] for r in reader.excerpt(path, grep=".")["records"]}
        assert kinds == {"message"}


class TestNoneRoots:
    """The second consumer reads its root out of a `proxy_only` manifest
    variable, so an unwired or unset one arrives as `None`. The spec requires
    that path to answer "no transcript", not to fail."""

    def test_the_finders_answer_empty(self):
        assert reader.find_logs(None, "alice") == []
        assert reader.find_all_logs(None) == []

    def test_tree_stats_answers_zero(self):
        assert reader.tree_stats(None).files == 0

    @pytest.mark.parametrize("call", [
        lambda p: list(reader.read_records(p)),
        lambda p: reader.read_header(p),
        lambda p: reader.read_last_record(p),
        lambda p: reader.summarize(p),
        lambda p: reader.digest(p),
        lambda p: reader.excerpt(p),
    ])
    def test_a_none_path_degrades(self, call):
        call(None)


class TestRecordText:
    """The text a grep runs against, which is deliberately not the record's JSON."""

    def test_it_gathers_text_thinking_tool_arguments_and_result_text(self):
        blob = reader.record_text(
            _assistant(text="hello", thinking="pondering",
                       calls=[("c", "Bash", {"command": "ls -la"})])
        )
        assert "hello" in blob and "pondering" in blob and "ls -la" in blob

    def test_it_gathers_a_steer_and_a_compaction_summary(self):
        assert "staging" in reader.record_text(
            {"type": "steer", "text": "check staging"}
        )
        assert "the user asked" in reader.record_text(
            {"type": "compaction", "summary": "the user asked"}
        )

    def test_a_record_with_no_text_is_the_empty_string(self):
        assert reader.record_text({"type": "nudge", "remaining": 5}) == ""

    def test_it_never_raises_on_a_shape_it_has_not_seen(self):
        assert isinstance(reader.record_text({"type": "message", "message": 7}), str)
        assert isinstance(reader.record_text("not a dict"), str)


class TestMidRunInjections:
    """A steer and a nudge are why the default selector is a list of kinds.

    Both are injections, and a transcript that drops them shows a user turn
    arriving in the middle of an agent loop with nothing saying where it came
    from — the reader then attributes to the user a sentence the framework
    wrote, or reads a steered run as one the model wandered into. The spec calls
    that "unexplainable", which is a stronger claim than "incomplete".
    """

    def _steered(self, path: Path) -> Path:
        return write_log(
            path,
            [
                _session(), _context(), _user(),
                _assistant(text="one", calls=[("c1", "Bash", {"command": "a"})]),
                _tool_result("c1", text="first output"),
                {"type": "steer", "ts": "t", "text": "check staging first"},
                {"type": "nudge", "ts": "t", "phase": "early",
                 "remaining": 50, "turns": 50, "max_turns": 100},
                _assistant(text="two", stop_reason="end_turn"),
                _result(),
            ],
        )

    def test_the_default_selection_carries_them_in_file_order(self, tmp_path):
        path = self._steered(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        kinds = [r["type"] for r in reader.excerpt(path)["records"]]
        assert kinds == [
            "message", "message", "message", "steer", "nudge", "message",
        ]

    def test_the_steer_text_survives_the_selection(self, tmp_path):
        path = self._steered(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        assert "check staging first" in json.dumps(reader.excerpt(path)["records"])

    def test_the_header_and_the_terminal_record_are_not_conversation(self, tmp_path):
        # `session`, `context` and `result` are reported on their own by
        # `digest`; including them here would render the system prompt as a turn.
        path = self._steered(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        kinds = {r["type"] for r in reader.excerpt(path)["records"]}
        assert kinds.isdisjoint({"session", "context", "result"})

    def test_a_turn_carries_the_injections_that_landed_inside_it(self, tmp_path):
        path = self._steered(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        kinds = [r["type"] for r in reader.excerpt(path, turn=1)["records"]]
        assert kinds == ["message", "message", "steer", "nudge"]

    def test_a_serialization_error_is_a_conversation_record(self, tmp_path):
        # It marks a record that was lost, so its whole value is positional.
        path = write_log(
            tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl",
            [
                _session(), _context(), _user(),
                {"type": "serialization_error", "ts": "t",
                 "record_type": "message", "error": "TypeError: not JSON"},
                _assistant(text="ok", stop_reason="end_turn"),
                _result(),
            ],
        )
        kinds = [r["type"] for r in reader.excerpt(path)["records"]]
        assert "serialization_error" in kinds
        assert reader.digest(path)["serialization_errors"] == 1

    def test_a_serialization_error_is_reachable_by_grep(self, tmp_path):
        path = write_log(
            tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl",
            [
                _session(), _context(), _user(),
                {"type": "serialization_error", "ts": "t",
                 "record_type": "message", "error": "TypeError: not JSON"},
                _result(),
            ],
        )
        assert reader.excerpt(path, grep="TypeError")["records"]


class TestExcerptRefusals:
    def test_turn_zero_is_refused_rather_than_answered(self, tmp_path):
        # Turns are 1-based. A 0 used to fall through the selector and hand back
        # the assembled prompt, which belongs to no turn — a wrong answer where
        # a refusal was meant.
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        out = reader.excerpt(path, turn=0)
        assert out["ok"] is False
        assert out["records"] == []
        assert "1 or greater" in out["reason"]

    def test_a_negative_turn_is_refused(self, tmp_path):
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        assert reader.excerpt(path, turn=-1)["ok"] is False

    def test_an_overlong_grep_pattern_is_refused(self, tmp_path):
        # The second consumer's pattern is written by a model, `re` has no step
        # limit, and the file runs to megabytes. Bounding the pattern and the
        # subject caps the product.
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        out = reader.excerpt(path, grep="a" * 201)
        assert out["ok"] is False
        assert "longer than" in out["reason"]
        assert reader.excerpt(path, grep="a" * 200)["ok"] is True

    def test_grep_never_returns_the_context_record(self, tmp_path):
        # The system prompt is configuration, not a record of this run, and grep
        # is the one selector that could otherwise reach it.
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        out = reader.excerpt(path, grep="helpful assistant")
        assert [r for r in out["records"] if r["type"] == "context"] == []

    def test_an_unreadable_excerpt_carries_the_same_keys_as_a_readable_one(
        self, tmp_path,
    ):
        # `digest` and `excerpt` are two reads of one path, and the retention
        # sweep unlinks under that root on an interval, so a caller can hold a
        # readable answer and get an unreadable one. A missing key there is a
        # KeyError in a consumer that already checked `ok` once.
        good = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        assert set(reader.excerpt(good)) == set(reader.excerpt(tmp_path / "gone.jsonl"))

    def test_an_unreadable_digest_carries_the_same_keys_as_a_readable_one(
        self, tmp_path,
    ):
        good = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        assert set(reader.digest(good)) == set(reader.digest(tmp_path / "gone.jsonl"))


class TestImagesAreCountedAsImages:
    def test_an_image_result_reports_a_picture_and_not_a_character_count(
        self, tmp_path,
    ):
        # The descriptor carries the *decoded byte length* of the picture.
        # Folding that into a character count made a one-megabyte screenshot
        # report `output_chars = 1048576` for a record occupying a line and a
        # half — two different units under one name.
        path = write_log(
            tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl",
            [
                _session(), _context(), _user(),
                _assistant(calls=[("c1", "Shot", {})]),
                {
                    "type": "message", "ts": "t",
                    "message": {
                        "role": "tool_result", "tool_call_id": "c1",
                        "tool_name": "Shot", "is_error": False,
                        "content": [
                            {"type": "text", "text": "ok"},
                            {"type": "image", "media_type": "image/png",
                             "display_name": "shot.png", "bytes": 1_048_576,
                             "sha256": "a3f9"},
                        ],
                    },
                },
                _result(),
            ],
        )
        tool = reader.digest(path)["tools"][0]
        assert tool["images"] == 1
        assert tool["output_chars"] == len("ok")
        assert tool["output_chars_total"] == len("ok")

    def test_a_text_only_result_reports_no_images(self, tmp_path):
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        assert reader.digest(path)["tools"][0]["images"] == 0


class TestScopingIsAboutTheDirectoryAndNotTheName:
    def test_a_symlinked_user_directory_finds_nothing(self, tmp_path):
        # `find_logs` is what the skill verb's ISTOTA_USER_ID scoping calls. A
        # symlinked *user directory* would hand back every file under whatever
        # it points at, filed under the name on the link — name containment
        # alone is not scoping.
        root = tmp_path / "logs"
        full_session(root / "alice" / f"{STAMP}_task-4471-1.jsonl")
        (root / "bob").symlink_to(root / "alice", target_is_directory=True)
        assert reader.find_logs(root, "alice")
        assert reader.find_logs(root, "bob") == []

    def test_find_all_logs_does_not_follow_a_symlinked_user_directory(self, tmp_path):
        root = tmp_path / "logs"
        full_session(root / "alice" / f"{STAMP}_task-4471-1.jsonl")
        (root / "bob").symlink_to(root / "alice", target_is_directory=True)
        assert len(reader.find_all_logs(root)) == 1

    def test_summarize_reports_a_header_that_disagrees_with_the_directory(
        self, tmp_path,
    ):
        # The displayed owner is the directory, because that is the identity the
        # scoping used. The header is file content, and where the two disagree
        # the disagreement is the thing worth saying.
        path = write_log(
            tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl",
            [_session(user_id="bob"), _context(), _user(), _result()],
        )
        row = reader.summarize(path)
        assert row["user_id"] == "alice"
        assert row["header_user_id"] == "bob"

    def test_an_agreeing_header_reports_no_disagreement(self, tmp_path):
        path = full_session(tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl")
        assert reader.summarize(path)["header_user_id"] == ""


class TestDecoderGivingUp:
    def test_a_deeply_nested_line_is_counted_as_malformed(self, tmp_path):
        # A line of 200,000 open brackets exhausts the interpreter's frame limit
        # inside the decoder, and `RecursionError` is not a `ValueError`. A line
        # that cannot be decoded is malformed whichever way the decoder gave up.
        path = tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl"
        write_log(
            path, [_session(), _context()],
            extra_lines=["[" * 200_000, json.dumps(_result())],
        )
        stats = reader.ReadStats()
        kinds = [r["type"] for r in reader.read_records(path, stats=stats)]
        assert kinds == ["session", "context", "result"]
        assert stats.malformed == 1

    def test_a_deeply_nested_first_line_is_not_a_header(self, tmp_path):
        path = tmp_path / "alice" / f"{STAMP}_task-4471-1.jsonl"
        write_log(path, [], extra_lines=["[" * 200_000, json.dumps(_result())])
        assert reader.read_header(path) is None

    @pytest.mark.parametrize("call", [
        lambda p: list(reader.read_records(p)),
        lambda p: reader.read_header(p),
        lambda p: reader.read_last_record(p),
        lambda p: reader.summarize(p),
        lambda p: reader.digest(p),
        lambda p: reader.excerpt(p),
    ])
    def test_a_path_carrying_a_nul_byte_degrades_rather_than_raising(
        self, tmp_path, call,
    ):
        # `ValueError: embedded null byte`, which is not an `OSError`, so every
        # plain `except OSError` in the module would miss it. The consumer that
        # hands these functions a name from outside the daemon is the skill
        # verb, whose target the model writes.
        call(str(tmp_path / "a\x00b.jsonl"))


def test_the_reader_reads_what_the_writer_writes(tmp_path):
    """The one test that pins the two halves together.

    Every other case here builds records by hand, which is what makes them
    readable and also what would let the reader drift from the writer without
    a single test going red. This drives the real `SessionLogWriter`.
    """
    from istota.llm.types import (
        AssistantMessage,
        TextContent,
        ToolCallContent,
        ToolResultMessage,
        UserMessage,
    )
    from istota.session.session_log import (
        SessionLogIdentity,
        SessionLogPolicy,
        SessionLogWriter,
    )

    root = tmp_path / "logs"
    writer = SessionLogWriter(
        root,
        SessionLogIdentity(task_id=77, attempt=1, user_id="alice", source_type="talk"),
        SessionLogPolicy(),
    )
    writer.open({"brain": "native", "model": "m", "effort": ""})
    writer.context("system", ["Bash"], "sha")
    writer.message(UserMessage(content=[TextContent(text="do the thing")]))
    writer.message(
        AssistantMessage(
            content=[
                TextContent(text="on it"),
                ToolCallContent(id="c1", name="Bash", arguments={"command": "ls"}),
            ],
            stop_reason="tool_use",
        )
    )
    writer.message(
        ToolResultMessage(
            tool_call_id="c1", tool_name="Bash",
            content=[TextContent(text="total 48")],
        )
    )
    writer.result(success=True, stop_reason="completed", result_text="done", turns=1)
    writer.close()

    path = writer.path
    assert reader.read_header(path)["task_id"] == 77
    found = reader.find_logs(root, "alice", task_id=77)
    assert found == [path]

    out = reader.digest(path)
    assert out["ok"] is True
    assert out["complete"] is True
    assert out["turns"] == 1
    assert out["tools"][0]["name"] == "Bash"
    assert out["tools"][0]["answered"] is True
    assert out["result"]["stop_reason"] == "completed"

    # The assertion the spec exists for: the tool's *output* is in the file.
    assert "total 48" in json.dumps(reader.excerpt(path, tools=True)["records"])

    row = reader.summarize(path)
    assert (row["task_id"], row["attempt"], row["user_id"]) == (77, 1, "alice")
    assert row["stop_reason"] == "completed"

    # And the name the writer chose parses back to the identity it encodes.
    assert re.fullmatch(r"[0-9T\-Z]+_task-77-1\.jsonl", path.name)
