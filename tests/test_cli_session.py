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
    full_session(
        root / "bob" / "2026-08-31T14-00-00-000Z_task-6000-1.jsonl", user_id="bob",
    )
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

    def test_a_header_naming_another_user_is_flagged_rather_than_believed(
        self, deployment, capsys,
    ):
        # The owner shown is the directory, which is the identity the scoping
        # used; the header is file content. Saying nothing about a disagreement
        # would leave an operator reading the row as bob's run.
        cfg, root = deployment
        write_log(
            root / "alice" / f"{STAMP}_task-8100-1.jsonl",
            [_session(task_id=8100, user_id="bob"), _context(), _user(), _result()],
        )
        cli.cmd_session_list(_FakeArgs(config=str(cfg)))
        out = capsys.readouterr().out
        assert "header claims user bob" in out
        assert "filed as alice" in out

    def test_an_agreeing_header_adds_no_line(self, tree, capsys):
        cfg, _root = tree
        cli.cmd_session_list(_FakeArgs(config=str(cfg)))
        assert "header claims" not in capsys.readouterr().out

    def test_a_header_task_id_does_not_relabel_the_row(self, deployment, capsys):
        # The row is labelled from the name because the name is what `--task`
        # filters on. Taking the header's made `list --task 8200` print "task
        # 4471", after which `show 4471` found nothing.
        cfg, root = deployment
        write_log(
            root / "alice" / f"{STAMP}_task-8200-1.jsonl",
            [_session(task_id=4471, attempt=9), _context(), _user(), _result()],
        )
        cli.cmd_session_list(_FakeArgs(config=str(cfg), task=8200))
        out = capsys.readouterr().out
        assert "task 8200-1" in out
        assert "task 4471-9" not in out
        assert "header claims task 4471" in out
        assert "header claims attempt 9" in out

    def test_an_empty_user_flag_finds_nothing_rather_than_everything(
        self, tree, capsys,
    ):
        # `find_logs` refuses a falsy id by contract, and branching on
        # truthiness here reached around it. It is the operator CLI, so this is
        # not a boundary — it is the pattern the skill verb inherits, where the
        # falsy value is `ISTOTA_USER_ID` unset.
        cfg, _root = tree
        cli.cmd_session_list(_FakeArgs(config=str(cfg), user=""))
        out = capsys.readouterr().out
        assert "No session logs" in out
        assert "task-4471" not in out
        assert "task-6000" not in out


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

    def test_a_steer_is_rendered_where_it_landed(self, deployment, capsys):
        # The one command an operator reads a transcript with. Without this the
        # injected user turn below appears in the middle of an agent loop with
        # nothing saying where it came from, and the run reads as one the model
        # wandered into rather than one somebody redirected.
        cfg, root = deployment
        path = root / "alice" / f"{STAMP}_task-6500-1.jsonl"
        write_log(
            path,
            [
                _session(task_id=6500), _context(), _user(),
                _assistant(text="looking at main", stop_reason="tool_use"),
                {"type": "steer", "ts": "t", "text": "check the staging branch"},
                _user("check the staging branch"),
                _assistant(text="on staging now", stop_reason="end_turn"),
                _result(),
            ],
        )
        cli.cmd_session_show(_FakeArgs(config=str(cfg), target=str(path)))
        out = capsys.readouterr().out
        assert "steer (injected mid-run)" in out
        assert "check the staging branch" in out
        # In place, not appended: the steer is between the turn it interrupted
        # and the user turn it became.
        assert out.index("looking at main") < out.index("steer (injected mid-run)")
        assert out.index("steer (injected mid-run)") < out.index("on staging now")

    def test_a_nudge_is_rendered_as_the_framework_and_not_as_the_user(
        self, deployment, capsys,
    ):
        cfg, root = deployment
        path = root / "alice" / f"{STAMP}_task-6310-1.jsonl"
        write_log(
            path,
            [
                _session(task_id=6310), _context(), _user(),
                {"type": "nudge", "ts": "t", "phase": "late",
                 "remaining": 5, "turns": 95, "max_turns": 100},
                _assistant(text="wrapping up", stop_reason="end_turn"),
                _result(),
            ],
        )
        cli.cmd_session_show(_FakeArgs(config=str(cfg), target=str(path)))
        out = capsys.readouterr().out
        assert "nudge (late): 5 of 100 turns remaining" in out

    def test_a_lost_record_is_named_rather_than_passed_over(
        self, deployment, capsys,
    ):
        # The writer's own marker for a record it could not serialize. Rendering
        # nothing would let the operator believe they read the whole run, which
        # is the failure the malformed count exists to prevent.
        cfg, root = deployment
        path = root / "alice" / f"{STAMP}_task-6320-1.jsonl"
        write_log(
            path,
            [
                _session(task_id=6320), _context(), _user(),
                {"type": "serialization_error", "ts": "t",
                 "record_type": "message", "error": "TypeError: not JSON"},
                _result(),
            ],
        )
        cli.cmd_session_show(_FakeArgs(config=str(cfg), target=str(path)))
        out = capsys.readouterr().out
        assert "serialization error" in out
        assert "1 record(s) the writer could not serialize" in out

    def test_a_capped_display_says_what_the_run_also_holds(
        self, deployment, capsys,
    ):
        # The mid-run events render in place now, so a cut display can drop the
        # error that explains the whole run. The count comes from the digest,
        # which reads the file whole.
        cfg, root = deployment
        path = root / "alice" / f"{STAMP}_task-6330-1.jsonl"
        write_log(
            path,
            [
                _session(task_id=6330), _context(),
                _user("q" * 4000),
                {"type": "steer", "ts": "t", "text": "check staging"},
                {"type": "error", "ts": "t", "kind": "ProviderError",
                 "message": "429 rate limited"},
                _result(stop_reason="error", success=False),
            ],
        )
        cli.cmd_session_show(
            _FakeArgs(config=str(cfg), target=str(path), max_chars=100)
        )
        out = capsys.readouterr().out
        assert "display capped" in out
        assert "1 error" in out
        assert "1 steer" in out

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

    def test_a_directory_named_like_a_task_id_does_not_shadow_the_task(
        self, tree, capsys, monkeypatch, tmp_path,
    ):
        # `4471` names a task and, in whatever shell the operator is standing
        # in, might also name a file. Taking the path unconditionally makes the
        # failure name the wrong problem.
        cfg, _root = tree
        cwd = tmp_path / "cwd"
        (cwd / "4471").mkdir(parents=True)
        monkeypatch.chdir(cwd)
        assert cli.cmd_session_show(_FakeArgs(config=str(cfg), target="4471")) is None
        assert "task 4471" in capsys.readouterr().out

    def test_the_identity_line_is_the_name_and_not_the_header(
        self, deployment, capsys,
    ):
        # `show` and `list` must not disagree about the same file, and the one
        # they have to agree on is the identity every lookup filters on.
        cfg, root = deployment
        path = root / "alice" / f"{STAMP}_task-8300-1.jsonl"
        write_log(
            path,
            [_session(task_id=4471, attempt=9, user_id="bob"), _context(),
             _user(), _result()],
        )
        cli.cmd_session_show(_FakeArgs(config=str(cfg), target=str(path)))
        out = capsys.readouterr().out
        assert "task 8300 attempt 1 user alice" in out
        assert "header claims task 4471" in out
        assert "header claims user bob" in out

    def test_a_record_field_of_the_wrong_type_renders_rather_than_crashing(
        self, deployment, capsys,
    ):
        # `digest` used to raise `TypeError` on a scalar `content` and it came
        # straight out of here as a traceback. The file may be damaged or
        # half-written — that is the module's premise.
        cfg, root = deployment
        path = root / "alice" / f"{STAMP}_task-8400-1.jsonl"
        write_log(
            path,
            [
                _session(task_id=8400),
                {"type": "context", "ts": "t", "system_prompt": 7, "tools": 7},
                {"type": "message", "ts": "t",
                 "message": {"role": "assistant", "content": 7}},
                {"type": "compaction", "ts": "t", "summary": 7},
                _result(),
            ],
        )
        assert cli.cmd_session_show(_FakeArgs(config=str(cfg), target=str(path))) is None
        assert "Session" in capsys.readouterr().out

    def test_a_read_that_stopped_early_is_said_and_not_passed_off_as_the_run(
        self, deployment, capsys, monkeypatch,
    ):
        from istota.session import session_log_read as slr

        cfg, root = deployment
        path = full_session(root / "alice" / f"{STAMP}_task-8500-1.jsonl")
        real = slr.read_records

        def _partial(p, *, skip_malformed=True, stats=None):
            for index, record in enumerate(
                real(p, skip_malformed=skip_malformed, stats=stats)
            ):
                if index >= 3:
                    if stats is not None:
                        stats.unreadable = "OSError: [Errno 5] Input/output error"
                    return
                yield record

        monkeypatch.setattr(slr, "read_records", _partial)
        cli.cmd_session_show(_FakeArgs(config=str(cfg), target=str(path)))
        out = capsys.readouterr().out
        assert "the read stopped early" in out
        assert "prefix of the run" in out

    def test_a_missing_attempt_is_named_even_when_it_is_zero(self, tree, capsys):
        # Attempts are 1-based, so `--attempt 0` matches nothing. Reporting "no
        # log for task N" would name a different problem from the real one.
        cfg, _root = tree
        assert cli.cmd_session_show(
            _FakeArgs(config=str(cfg), target="4471", attempt=0)
        ) == 1
        assert "task 4471 attempt 0" in capsys.readouterr().err

    def test_a_fallback_run_says_so_on_the_brain_line(self, deployment, capsys):
        # ISSUE-378. Both runs of a rerouted attempt share a task id and an
        # attempt, so this line is where an operator learns which `task_usage`
        # row the transcript in front of them belongs to.
        cfg, root = deployment
        path = root / "alice" / f"{STAMP}_task-4471-1.jsonl"
        write_log(path, [_session(is_fallback=True), _context(), _user(), _result()])
        cli.cmd_session_show(_FakeArgs(config=str(cfg), target=str(path)))
        out = capsys.readouterr().out
        assert "fallback=yes" in out

    def test_a_primary_run_says_no_rather_than_nothing(self, deployment, capsys):
        cfg, root = deployment
        path = root / "alice" / f"{STAMP}_task-4471-1.jsonl"
        write_log(path, [_session(is_fallback=False), _context(), _user(), _result()])
        cli.cmd_session_show(_FakeArgs(config=str(cfg), target=str(path)))
        assert "fallback=no" in capsys.readouterr().out

    def test_a_log_from_before_the_field_says_nothing_at_all(self, tree, capsys):
        # The `tree` fixture's files predate `is_fallback` — `_session()` writes
        # no such key. `fallback=` must be absent entirely, not rendered `no`:
        # printing `no` would answer a question the run never answered, which is
        # the whole reason `summarize` keeps this field tri-state.
        cfg, root = tree
        path = root / "alice" / f"{STAMP}_task-4471-1.jsonl"
        cli.cmd_session_show(_FakeArgs(config=str(cfg), target=str(path)))
        assert "fallback=" not in capsys.readouterr().out


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

    def test_a_partial_trailing_line_does_not_move_the_follow_offset(
        self, deployment, capsys, monkeypatch,
    ):
        # The offset and the printed records have to come from one read. A
        # separate `getsize` puts the offset *past* the unterminated line a live
        # session always has, so the follow loop then prints that record's tail
        # as though it were a whole line — the very thing the non-follow half
        # refuses to do.
        import time

        cfg, root = deployment
        path = root / "alice" / f"{STAMP}_task-6600-1.jsonl"
        write_log(
            path, [_session(task_id=6600), _context()],
            trailing_newline=False, extra_lines=['{"type":"result","ts":"t"'],
        )

        finished = {"done": False}

        def _sleep(_seconds):
            if not finished["done"]:
                finished["done"] = True
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(',"stop_reason":"completed"}\n')

        monkeypatch.setattr(time, "sleep", _sleep)
        cli.cmd_session_tail(
            _FakeArgs(
                config=str(cfg), target=str(path), lines=0,
                no_follow=False, poll_limit=2, interval=0.0,
            )
        )
        lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        # Every line is whole JSON, and the completed record arrives entire.
        assert [json.loads(line)["type"] for line in lines] == [
            "session", "context", "result",
        ]

    def test_a_file_replaced_under_the_follow_loop_is_said_rather_than_hung_on(
        self, deployment, capsys, monkeypatch,
    ):
        # The retention sweep unlinks under this root on the scheduler's
        # interval. Waiting silently for a size that will never come back leaves
        # a follower staring at nothing with no reason given.
        import time

        cfg, root = deployment
        path = root / "alice" / f"{STAMP}_task-6610-1.jsonl"
        write_log(path, [_session(task_id=6610), _context(), _result()])

        replaced = {"done": False}

        def _sleep(_seconds):
            if not replaced["done"]:
                replaced["done"] = True
                write_log(path, [_session(task_id=6610)])

        monkeypatch.setattr(time, "sleep", _sleep)
        cli.cmd_session_tail(
            _FakeArgs(
                config=str(cfg), target=str(path), lines=0,
                no_follow=False, poll_limit=3, interval=0.0,
            )
        )
        assert "truncated or replaced" in capsys.readouterr().out

    def test_a_file_unlinked_under_the_follow_loop_is_not_a_silent_exit_zero(
        self, deployment, capsys, monkeypatch,
    ):
        # The sweep unlinks under this root on the scheduler's interval.
        # Breaking out in silence returned 0 with nothing printed, which to a
        # script and to an operator is the session having ended normally.
        import time

        cfg, root = deployment
        path = full_session(root / "alice" / f"{STAMP}_task-6620-1.jsonl")

        gone = {"done": False}

        def _sleep(_seconds):
            if not gone["done"]:
                gone["done"] = True
                path.unlink()

        monkeypatch.setattr(time, "sleep", _sleep)
        assert cli.cmd_session_tail(
            _FakeArgs(
                config=str(cfg), target=str(path), lines=0,
                no_follow=False, poll_limit=3, interval=0.0,
            )
        ) == 1
        assert "No such file" in capsys.readouterr().err

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
        out = capsys.readouterr().out
        assert "ceiling: 2.0 GB" in out
        # Two adjacent figures invite a comparison they do not support: the
        # sweep counts blocks, and it counts every file under a user directory
        # where this counts recognised transcripts only. Printing them side by
        # side without saying so is what makes the difference a trap.
        assert "the sweep measures blocks" in out
        assert "counts transcripts only" in out

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
