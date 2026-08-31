"""``istota session list|show|tail|stats`` against a fixture tree.

The parsing is `session/session_log_read.py`'s and is tested in
`tests/test_session_log_read.py`; what is asserted here is the part that only
exists in `cli.py` — that an operator is told the truth about a file rather than
shown a plausible rendering of it. Three cases carry that, and each is a thing a
`json.loads` loop plus a print gets wrong:

- **an interrupted run gets a row and is labelled one.** Its last line is an
  assistant message, not a `result`. Reading the last line as the result record
  is the obvious implementation and it reports a run that never finished as
  though it had.
- **a malformed middle line is skipped and the count is printed.** Skipping in
  silence is worse than refusing the file: the operator believes they read the
  whole run.
- **a file whose first line is not a header is reported unreadable and nothing
  from its body is printed.** The negative control for that one is explicit: a
  well-formed file with the same body *does* render, so the assertion is about
  the header rule and not about the fixture being empty.

Handlers are called directly with a fake args object, the way
`tests/test_cli_bot_icon.py` does, since that is where the behaviour is.
`TestTheParserWiring` is the exception and exists because that leaves the
subparser and the dispatch entry covered by nothing: a command that parses but
reaches no handler exits 2 with a usage message, and no assertion about
rendering would notice.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from istota import cli
from tests.test_session_log_read import (
    STAMP,
    _assistant,
    _context,
    _result,
    _session,
    _user,
    full_session,
    write_log,
)


class _FakeArgs:
    def __init__(self, **kwargs):
        defaults = {
            "config": None,
            "user": None,
            "task": None,
            "limit": 20,
            "target": None,
            "attempt": None,
            "no_thinking": False,
            "full": False,
            "max_chars": 200_000,
            "lines": 20,
            "no_follow": True,
            "interval": 0.0,
            "poll_limit": 0,
            "days": 0,
        }
        defaults.update(kwargs)
        self.__dict__.update(defaults)


@pytest.fixture
def deployment(tmp_path: Path):
    """A config whose db_path puts the log root at ``{tmp}/data/logs``.

    The directory is derived rather than configured, which is the shipped shape:
    `resolve_session_log_dir` hangs `logs` off `db_path.parent` when
    `[brain.native.session_log] dir` is blank. Asserting through that resolution
    rather than pointing the CLI at a hand-made directory is what makes these
    tests cover the path an operator actually gets.
    """
    data = tmp_path / "data"
    data.mkdir()
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'db_path = "{data / "istota.db"}"\n'
        f'temp_dir = "{tmp_path / "tmp"}"\n'
    )
    return cfg, data / "logs"


@pytest.fixture
def tree(deployment):
    """alice: 4471-1 complete, 4471-2 interrupted, 5000-1 complete; bob: 6000-1."""
    cfg, root = deployment
    full_session(root / "alice" / f"{STAMP}_task-4471-1.jsonl")
    write_log(
        root / "alice" / "2026-08-31T15-00-00-000Z_task-4471-2.jsonl",
        [_session(attempt=2), _context(), _user(), _assistant(text="half a run")],
    )
    full_session(root / "alice" / "2026-08-31T16-00-00-000Z_task-5000-1.jsonl")
    full_session(root / "bob" / "2026-08-31T14-00-00-000Z_task-6000-1.jsonl")
    return cfg, root


class TestList:
    def test_it_lists_every_user_newest_first(self, tree, capsys):
        cfg, root = tree
        cli.cmd_session_list(_FakeArgs(config=str(cfg)))
        out = capsys.readouterr().out
        assert str(root) in out
        # Newest first: 16:00, then 15:00, then the two 14:00 files.
        order = [line for line in out.splitlines() if "task-" in line]
        assert "task-5000-1" in order[0]
        assert "task-4471-2" in order[1]

    def test_a_user_filter_excludes_the_other_user(self, tree, capsys):
        cfg, _root = tree
        cli.cmd_session_list(_FakeArgs(config=str(cfg), user="alice"))
        out = capsys.readouterr().out
        assert "task-6000" not in out
        assert "task-4471-1" in out

    def test_a_task_filter_shows_every_attempt_of_that_task(self, tree, capsys):
        cfg, _root = tree
        cli.cmd_session_list(_FakeArgs(config=str(cfg), task=4471))
        out = capsys.readouterr().out
        assert "task-4471-1" in out and "task-4471-2" in out
        assert "task-5000" not in out

    def test_the_limit_bounds_the_rows(self, tree, capsys):
        cfg, _root = tree
        cli.cmd_session_list(_FakeArgs(config=str(cfg), limit=1))
        out = capsys.readouterr().out
        assert len([line for line in out.splitlines() if ".jsonl" in line]) == 1

    def test_a_finished_run_shows_its_stop_reason_and_turn_count(self, tree, capsys):
        cfg, _root = tree
        cli.cmd_session_list(_FakeArgs(config=str(cfg), task=5000))
        out = capsys.readouterr().out
        assert "completed" in out
        assert "2 turns" in out

    def test_an_interrupted_run_is_a_row_that_says_so(self, tree, capsys):
        # The naive read — "the last line is the result record" — reports this
        # file as whatever its last assistant message happened to say.
        cfg, _root = tree
        cli.cmd_session_list(_FakeArgs(config=str(cfg), task=4471))
        out = capsys.readouterr().out
        row = next(line for line in out.splitlines() if "task 4471-2" in line)
        assert "interrupted" in row
        assert "completed" not in row

    def test_a_failed_run_is_marked(self, deployment, capsys):
        cfg, root = deployment
        write_log(
            root / "alice" / f"{STAMP}_task-9000-1.jsonl",
            [_session(task_id=9000), _context(), _user(),
             _result(stop_reason="timeout", success=False)],
        )
        cli.cmd_session_list(_FakeArgs(config=str(cfg)))
        out = capsys.readouterr().out
        assert "timeout" in out and "FAILED" in out

    def test_a_headerless_file_is_listed_as_unreadable(self, deployment, capsys):
        cfg, root = deployment
        write_log(
            root / "alice" / f"{STAMP}_task-8000-1.jsonl",
            [_context(), _user("the withheld prompt"), _result()],
        )
        cli.cmd_session_list(_FakeArgs(config=str(cfg)))
        out = capsys.readouterr().out
        assert "unreadable" in out
        # The identity still comes off the name, which is what it is for.
        assert "task 8000-1" in out
        assert "the withheld prompt" not in out

    def test_an_empty_tree_says_so_rather_than_printing_nothing(
        self, deployment, capsys,
    ):
        cfg, root = deployment
        cli.cmd_session_list(_FakeArgs(config=str(cfg)))
        assert "No session logs" in capsys.readouterr().out


class TestShow:
    def test_it_renders_a_known_session(self, tree, capsys):
        cfg, root = tree
        path = root / "alice" / f"{STAMP}_task-4471-1.jsonl"
        assert cli.cmd_session_show(_FakeArgs(config=str(cfg), target=str(path))) is None
        out = capsys.readouterr().out
        assert "task 4471 attempt 1" in out
        assert "rebuild the index" in out            # the assembled prompt
        assert "Let me look." in out                 # assistant text
        assert "[tool_call Bash call_1]" in out      # the call
        assert "drwxr-xr-x" in out                   # the tool's output
        assert "result: completed (success)" in out

    def test_a_bare_task_id_resolves_to_the_newest_attempt(self, tree, capsys):
        cfg, _root = tree
        cli.cmd_session_show(_FakeArgs(config=str(cfg), target="4471"))
        out = capsys.readouterr().out
        assert "task 4471 attempt 2" in out

    def test_the_attempt_flag_picks_an_older_attempt(self, tree, capsys):
        cfg, _root = tree
        cli.cmd_session_show(_FakeArgs(config=str(cfg), target="4471", attempt=1))
        out = capsys.readouterr().out
        assert "task 4471 attempt 1" in out

    def test_thinking_is_shown_by_default_and_omitted_on_request(self, tree, capsys):
        cfg, root = tree
        path = str(root / "alice" / f"{STAMP}_task-4471-1.jsonl")
        cli.cmd_session_show(_FakeArgs(config=str(cfg), target=path))
        assert "[thinking]" in capsys.readouterr().out
        cli.cmd_session_show(
            _FakeArgs(config=str(cfg), target=path, no_thinking=True)
        )
        out = capsys.readouterr().out
        assert "[thinking]" not in out
        # Only the thinking went: the rest of the turn is still rendered.
        assert "Let me look." in out

    def test_a_malformed_middle_line_is_skipped_and_counted(self, deployment, capsys):
        cfg, root = deployment
        path = root / "alice" / f"{STAMP}_task-7000-1.jsonl"
        write_log(
            path,
            [_session(task_id=7000), _context(), _user("still here")],
            extra_lines=["{not json", json.dumps(_result())],
        )
        cli.cmd_session_show(_FakeArgs(config=str(cfg), target=str(path)))
        out = capsys.readouterr().out
        assert "still here" in out
        assert "1 malformed line(s) skipped" in out

    def test_a_headerless_file_is_reported_unreadable_and_not_rendered(
        self, deployment, capsys,
    ):
        cfg, root = deployment
        body = [_context(), _user("the withheld prompt"), _result()]
        bad = root / "alice" / f"{STAMP}_task-8000-1.jsonl"
        write_log(bad, body)

        rc = cli.cmd_session_show(_FakeArgs(config=str(cfg), target=str(bad)))
        captured = capsys.readouterr()
        assert rc == 1
        assert "no session header" in captured.err
        assert "the withheld prompt" not in captured.out

        # Negative control: the same body behind a header does render, so the
        # assertion above is about the header rule rather than about the
        # fixture having nothing in it.
        good = root / "alice" / f"{STAMP}_task-8001-1.jsonl"
        write_log(good, [_session(task_id=8001), *body])
        assert cli.cmd_session_show(_FakeArgs(config=str(cfg), target=str(good))) is None
        assert "the withheld prompt" in capsys.readouterr().out

    def test_an_interrupted_run_renders_and_says_there_is_no_result(
        self, tree, capsys,
    ):
        cfg, root = tree
        path = root / "alice" / "2026-08-31T15-00-00-000Z_task-4471-2.jsonl"
        cli.cmd_session_show(_FakeArgs(config=str(cfg), target=str(path)))
        out = capsys.readouterr().out
        assert "half a run" in out
        assert "interrupted run" in out

    def test_a_compaction_and_an_error_are_reported(self, deployment, capsys):
        cfg, root = deployment
        path = root / "alice" / f"{STAMP}_task-6100-1.jsonl"
        write_log(
            path,
            [
                _session(task_id=6100), _context(), _user(),
                {"type": "compaction", "ts": "t", "trigger": "overflow",
                 "messages_dropped": 12, "recovery_index": 1},
                {"type": "error", "ts": "t", "kind": "ProviderError",
                 "message": "429 rate limited"},
                _result(stop_reason="error", success=False),
            ],
        )
        cli.cmd_session_show(_FakeArgs(config=str(cfg), target=str(path)))
        out = capsys.readouterr().out
        assert "compaction (overflow): dropped 12 messages" in out
        assert "error: ProviderError: 429 rate limited" in out

    def test_the_display_cap_reports_itself_and_full_lifts_it(
        self, deployment, capsys,
    ):
        cfg, root = deployment
        path = full_session(root / "alice" / f"{STAMP}_task-6200-1.jsonl")
        cli.cmd_session_show(
            _FakeArgs(config=str(cfg), target=str(path), max_chars=100)
        )
        out = capsys.readouterr().out
        assert "display capped" in out
        cli.cmd_session_show(
            _FakeArgs(config=str(cfg), target=str(path), max_chars=100, full=True)
        )
        out = capsys.readouterr().out
        assert "display capped" not in out
        assert "drwxr-xr-x" in out

    def test_an_unresolvable_target_exits_non_zero(self, tree, capsys):
        cfg, _root = tree
        assert cli.cmd_session_show(_FakeArgs(config=str(cfg), target="999999")) == 1
        assert "No session log for task 999999" in capsys.readouterr().err

    def test_a_target_that_is_neither_a_path_nor_an_id_exits_non_zero(
        self, tree, capsys,
    ):
        cfg, _root = tree
        assert cli.cmd_session_show(_FakeArgs(config=str(cfg), target="nope.jsonl")) == 1
        assert "No such file" in capsys.readouterr().err


class TestTail:
    def test_it_prints_the_last_records_as_jsonl(self, tree, capsys):
        cfg, root = tree
        path = root / "alice" / f"{STAMP}_task-4471-1.jsonl"
        cli.cmd_session_tail(_FakeArgs(config=str(cfg), target=str(path), lines=2))
        lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert len(lines) == 2
        assert json.loads(lines[-1])["type"] == "result"

    def test_lines_zero_prints_the_whole_file(self, tree, capsys):
        cfg, root = tree
        path = root / "alice" / f"{STAMP}_task-4471-1.jsonl"
        cli.cmd_session_tail(_FakeArgs(config=str(cfg), target=str(path), lines=0))
        lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert len(lines) == 7

    def test_a_bare_task_id_resolves(self, tree, capsys):
        cfg, _root = tree
        cli.cmd_session_tail(_FakeArgs(config=str(cfg), target="5000", lines=1))
        assert json.loads(capsys.readouterr().out.strip())["type"] == "result"

    def test_following_picks_up_a_record_appended_after_the_first_read(
        self, deployment, capsys, monkeypatch,
    ):
        # The whole point of `tail`: a session being written right now. The
        # follow loop is bounded by `--poll-limit` so the test can reach it,
        # and the append rides the sleep between polls — which is exactly the
        # window a real follower notices a record in.
        import time

        cfg, root = deployment
        path = root / "alice" / f"{STAMP}_task-6300-1.jsonl"
        write_log(path, [_session(task_id=6300), _context()])

        appended = {"done": False}

        def _sleep(_seconds):
            if not appended["done"]:
                appended["done"] = True
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(_result()) + "\n")

        monkeypatch.setattr(time, "sleep", _sleep)
        cli.cmd_session_tail(
            _FakeArgs(
                config=str(cfg), target=str(path), lines=0,
                no_follow=False, poll_limit=2, interval=0.0,
            )
        )

        lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert [json.loads(line)["type"] for line in lines] == [
            "session", "context", "result",
        ]

    def test_a_partial_trailing_line_is_not_printed_as_a_record(
        self, deployment, capsys,
    ):
        cfg, root = deployment
        path = root / "alice" / f"{STAMP}_task-6400-1.jsonl"
        write_log(
            path, [_session(task_id=6400), _context()],
            trailing_newline=False, extra_lines=['{"type":"messa'],
        )
        cli.cmd_session_tail(_FakeArgs(config=str(cfg), target=str(path), lines=0))
        lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert [json.loads(line)["type"] for line in lines] == ["session", "context"]

    def test_an_unresolvable_target_exits_non_zero(self, tree, capsys):
        cfg, _root = tree
        assert cli.cmd_session_tail(_FakeArgs(config=str(cfg), target="999999")) == 1


class TestTheParserWiring:
    """Everything above calls a handler directly, which leaves the subparser
    and the dispatch entry covered by nothing. Those are the two lines that rot
    silently — a command that parses but reaches no handler exits 2 with a
    usage message, and no assertion about rendering would notice."""

    def _main(self, argv, monkeypatch):
        monkeypatch.setattr("sys.argv", ["istota", *argv])
        cli.main()

    def test_list_reaches_its_handler(self, tree, capsys, monkeypatch):
        cfg, _root = tree
        self._main(["-c", str(cfg), "session", "list"], monkeypatch)
        assert "task 4471-1" in capsys.readouterr().out

    def test_show_reaches_its_handler(self, tree, capsys, monkeypatch):
        cfg, _root = tree
        self._main(["-c", str(cfg), "session", "show", "5000"], monkeypatch)
        assert "result: completed" in capsys.readouterr().out

    def test_stats_reaches_its_handler(self, tree, capsys, monkeypatch):
        cfg, _root = tree
        self._main(["-c", str(cfg), "session", "stats"], monkeypatch)
        assert "4 file(s)" in capsys.readouterr().out

    def test_tail_reaches_its_handler(self, tree, capsys, monkeypatch):
        cfg, _root = tree
        self._main(
            ["-c", str(cfg), "session", "tail", "5000", "--no-follow", "-n", "1"],
            monkeypatch,
        )
        assert json.loads(capsys.readouterr().out.strip())["type"] == "result"

    def test_an_unresolvable_show_exits_non_zero_through_main(
        self, tree, capsys, monkeypatch,
    ):
        # A handler's `return 1` thrown away by the dispatch is a failure a
        # script cannot see, which is the reason the `session` branch checks it.
        cfg, _root = tree
        with pytest.raises(SystemExit) as excinfo:
            self._main(["-c", str(cfg), "session", "show", "999999"], monkeypatch)
        assert excinfo.value.code == 1


class TestStats:
    def test_the_totals_match_the_fixture(self, tree, capsys):
        cfg, root = tree
        expected_files = list(root.glob("*/*.jsonl"))
        expected_bytes = sum(p.stat().st_size for p in expected_files)

        cli.cmd_session_stats(_FakeArgs(config=str(cfg)))
        out = capsys.readouterr().out
        assert f"{len(expected_files)} file(s)" in out
        # The rendered size is rounded, so compare the figure the renderer was
        # given rather than re-deriving the string.
        assert cli._fmt_bytes(expected_bytes) in out

    def test_the_per_user_split_is_reported(self, tree, capsys):
        cfg, _root = tree
        cli.cmd_session_stats(_FakeArgs(config=str(cfg)))
        out = capsys.readouterr().out
        alice = next(line for line in out.splitlines() if "alice" in line)
        bob = next(line for line in out.splitlines() if "bob" in line)
        assert "3 file(s)" in alice
        assert "1 file(s)" in bob

    def test_the_ceiling_is_reported_beside_the_total(self, tree, capsys):
        # The size ceiling is the operator-visible half of the disk story, and
        # a total with nothing to compare it against is a number nobody acts on.
        cfg, _root = tree
        cli.cmd_session_stats(_FakeArgs(config=str(cfg)))
        assert "ceiling: 2.0 GB" in capsys.readouterr().out

    def test_days_excludes_an_older_file(self, tree, capsys):
        import os
        import time

        cfg, root = tree
        old = root / "bob" / "2026-08-31T14-00-00-000Z_task-6000-1.jsonl"
        stale = time.time() - 30 * 86400
        os.utime(old, (stale, stale))

        cli.cmd_session_stats(_FakeArgs(config=str(cfg), days=7))
        out = capsys.readouterr().out
        assert "3 file(s), " in out
        assert "in the last 7 day(s)" in out
        assert "bob" not in out

    def test_an_empty_tree_reports_zero_rather_than_failing(self, deployment, capsys):
        cfg, _root = deployment
        cli.cmd_session_stats(_FakeArgs(config=str(cfg)))
        assert "0 file(s)" in capsys.readouterr().out
