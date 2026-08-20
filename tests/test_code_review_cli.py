"""Tests for the code_review CLI — the guards, the call cap, and the model call.

Everything the review does *without* a model lives in `engine.py` and is tested
by `test_code_review_engine.py`. This file covers the layer above it: the gates
that run before a single token is spent, the budget counter that stops a loop
from spending the operator's money, and the envelope the workflow branches on.

Three properties are load-bearing here and none of them is visible from the
happy path:

**A refused run must not construct a brain.** Every guard test monkeypatches
`make_brain` to raise, so a gate that lets a call through fails loudly rather
than passing quietly with an unasserted side effect.

**The counter is in the framework database, not in a file.** `ISTOTA_DEFERRED_DIR`
is bound read-write into the sandbox, so a loop that hit a file-backed cap could
delete the counter and carry on spending. The cap tests read `code_review_calls`
back through `db` directly rather than trusting the envelope's own count.

**Only successful model rounds increment it.** A run refused by a guard, one
short-circuited by the availability breaker, and the retry half of a
malformed-output round are all free. Otherwise a task could exhaust its budget
without a single review coming back, which is the failure the cap exists to
prevent inverted.

The brain is the mock boundary, the same place the sleep-cycle and explainer
tests draw it. There is no live model call anywhere in this file.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from istota import db
from istota.config import Config, DeveloperConfig, ReviewConfig, load_config
from istota.skills import code_review

# Enough identity to commit, and enough isolation that the developer's own
# ~/.gitconfig cannot decide what a fixture repository does.
GIT_ISOLATION = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def run_git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ISOLATION},
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{proc.stderr}")
    return proc.stdout


def commit(repo: Path, message: str) -> None:
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", message)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def repos_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(root))
    return root.resolve()


@pytest.fixture
def worktree(repos_root) -> Path:
    """A repository inside the repos root with one commit on a `feature` branch."""
    wt = repos_root / "proj"
    wt.mkdir()
    run_git(wt, "init", "-q", "-b", "main", ".")
    (wt / "AGENTS.md").write_text("# Rules\n\nSpaces, never tabs.\n")
    (wt / "app.py").write_text("def existing():\n    return 1\n")
    commit(wt, "base")
    run_git(wt, "checkout", "-q", "-b", "feature")
    (wt / "app.py").write_text(
        "def existing():\n    return 1\n\n\ndef added(value):\n    return value * 2\n"
    )
    commit(wt, "app: add a helper")
    return wt


@pytest.fixture
def empty_worktree(repos_root) -> Path:
    """A repository whose `feature` branch adds nothing over `main`."""
    wt = repos_root / "empty"
    wt.mkdir()
    run_git(wt, "init", "-q", "-b", "main", ".")
    (wt / "app.py").write_text("def existing():\n    return 1\n")
    commit(wt, "base")
    run_git(wt, "checkout", "-q", "-b", "feature")
    return wt


@pytest.fixture
def review_db(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "framework.db"
    db.init_db(path)
    monkeypatch.setenv("ISTOTA_DB_PATH", str(path))
    return path


@pytest.fixture
def task_row(review_db):
    """A real `tasks` row, because `code_review_calls` has a FK against it."""
    with db.get_db(review_db) as conn:
        row = conn.execute(
            "INSERT INTO tasks (prompt, user_id, source_type, status) "
            "VALUES ('review me', 'admin', 'cli', 'running') RETURNING id"
        ).fetchone()
        conn.commit()
        return int(row["id"])


@pytest.fixture
def review_env(monkeypatch, task_row):
    monkeypatch.setenv("ISTOTA_USER_ID", "admin")
    monkeypatch.setenv("ISTOTA_TASK_ID", str(task_row))
    return task_row


# --------------------------------------------------------------------------
# Stub brain
# --------------------------------------------------------------------------


@dataclass
class StubResult:
    success: bool = True
    result_text: str = ""
    stop_reason: str = "completed"
    usage: object | None = None
    model_used: str = ""
    actions_taken: object | None = None
    execution_trace: object | None = None


@dataclass
class StubBrain:
    """A brain that answers from a per-agent script and records what it saw."""

    replies: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)
    prompts: list = field(default_factory=list)
    timeouts: list = field(default_factory=list)

    def resolve_model_name(self, name: str) -> str:
        return f"resolved/{name}"

    def execute(self, req):
        # Which reviewer this is, read off the prompt the engine built. The
        # brain has no other way to tell them apart, and asserting on it here
        # is what proves the CLI routed each agent to its own model.
        agent = "bughunt" if "skeptical bug-hunter" in req.prompt else "conformance"
        self.calls.append(agent)
        self.prompts.append(req.prompt)
        self.timeouts.append(req.timeout_seconds)
        script = self.replies.get(agent, [])
        if not script:
            return StubResult(result_text='{"findings": []}')
        reply = script.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, StubResult):
            return reply
        return StubResult(result_text=reply)


def findings_json(*findings) -> str:
    return json.dumps({"findings": list(findings)})


def finding(severity="high", file="app.py", line=4, claim="a defect"):
    return {
        "severity": severity,
        "file": file,
        "line": line,
        "claim": claim,
        "evidence": "observed",
        "action": "fix it",
    }


@pytest.fixture
def stub_brain(monkeypatch):
    brain = StubBrain()
    monkeypatch.setattr("istota.brain.make_brain", lambda cfg: brain)
    monkeypatch.setattr(
        "istota.brain.primary_brain_unavailable", lambda cfg: (True, "")
    )
    monkeypatch.setattr("istota.brain.report_brain_result", lambda result, cfg: None)
    return brain


@pytest.fixture
def no_brain(monkeypatch):
    """Every guard test installs this: constructing a brain is a test failure."""

    def _explode(cfg):
        raise AssertionError("make_brain was called after a guard should have refused")

    monkeypatch.setattr("istota.brain.make_brain", _explode)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


@pytest.fixture
def developer_config(tmp_path, monkeypatch):
    """A Config with `developer` enabled and review defaults, installed as `load_config`."""

    def _make(**review_overrides):
        cfg = Config(
            db_path=tmp_path / "framework.db",
            temp_dir=tmp_path / "temp",
        )
        cfg.developer = DeveloperConfig(
            enabled=True,
            repos_dir=str(os.environ.get("DEVELOPER_REPOS_DIR", "")),
            review=ReviewConfig(**review_overrides),
        )
        monkeypatch.setattr("istota.config.load_config", lambda *a, **k: cfg)
        return cfg

    return _make


class TestReviewConfigParsing:
    def test_block_parses(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(
            "[developer]\n"
            "enabled = true\n"
            'repos_dir = "/srv/repos"\n'
            'author_credit = "Co-Authored-By: Bot <bot@example.invalid>"\n'
            "\n"
            "[developer.review]\n"
            "enabled = false\n"
            'conformance_model = "fast:low"\n'
            'bughunt_model = "smart:high"\n'
            "both_agents_threshold_lines = 42\n"
            'boundary_patterns = ["auth", "billing"]\n'
            "max_diff_chars = 1234\n"
            "max_calls_per_task = 3\n"
            "timeout_seconds = 90\n"
        )
        cfg = load_config(path)
        review = cfg.developer.review
        assert review.enabled is False
        assert review.conformance_model == "fast:low"
        assert review.bughunt_model == "smart:high"
        assert review.both_agents_threshold_lines == 42
        assert review.boundary_patterns == ["auth", "billing"]
        assert review.max_diff_chars == 1234
        assert review.max_calls_per_task == 3
        assert review.timeout_seconds == 90

    def test_defaults_hold_when_the_block_is_absent(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('[developer]\nenabled = true\nrepos_dir = "/srv/repos"\n')
        review = load_config(path).developer.review
        assert review.enabled is True
        assert review.max_calls_per_task == 8
        assert review.both_agents_threshold_lines == 150
        assert "auth" in review.boundary_patterns

    def test_unknown_key_is_ignored_rather_than_fatal(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(
            "[developer]\nenabled = true\n"
            "[developer.review]\nno_such_key = 7\nmax_calls_per_task = 2\n"
        )
        review = load_config(path).developer.review
        assert review.max_calls_per_task == 2

    def test_author_credit_is_parsed(self, tmp_path):
        """Declared on the dataclass and by the env spec, but never read from TOML.

        The `commit` skill makes this the one permitted commit trailer, so a
        silently-dead field would ship a rule nothing can satisfy.
        """
        path = tmp_path / "config.toml"
        path.write_text(
            "[developer]\nenabled = true\n"
            'author_credit = "Co-Authored-By: Bot <bot@example.invalid>"\n'
        )
        cfg = load_config(path)
        assert cfg.developer.author_credit == "Co-Authored-By: Bot <bot@example.invalid>"


# --------------------------------------------------------------------------
# The call counter
# --------------------------------------------------------------------------


class TestCallCounterHelpers:
    def test_unknown_task_reads_zero(self, review_db, task_row):
        with db.get_db(review_db) as conn:
            assert db.code_review_calls_get(conn, task_row) == 0

    def test_increment_returns_the_new_count(self, review_db, task_row):
        with db.get_db(review_db) as conn:
            assert db.code_review_calls_increment(conn, task_row) == 1
            assert db.code_review_calls_increment(conn, task_row) == 2
            assert db.code_review_calls_get(conn, task_row) == 2

    def test_counters_do_not_outlive_their_task(self, review_db, task_row):
        with db.get_db(review_db) as conn:
            db.code_review_calls_increment(conn, task_row)
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_row,))
            conn.commit()
            assert db.code_review_calls_get(conn, task_row) == 0


# --------------------------------------------------------------------------
# Guards — none of these may construct a brain
# --------------------------------------------------------------------------


class TestGuards:
    def test_developer_disabled(
        self, capsys, tmp_path, monkeypatch, worktree, review_env, no_brain
    ):
        cfg = Config(db_path=tmp_path / "db", temp_dir=tmp_path / "t")
        cfg.developer = DeveloperConfig(enabled=False, repos_dir=str(worktree.parent))
        monkeypatch.setattr("istota.config.load_config", lambda *a, **k: cfg)
        code, envelope = drive(capsys, "run", "--worktree", str(worktree))
        assert code == 1
        assert envelope["status"] == "error"
        assert envelope["reason"] == "developer_disabled"

    def test_repos_dir_unset(
        self, capsys, tmp_path, monkeypatch, worktree, review_env, no_brain
    ):
        cfg = Config(db_path=tmp_path / "db", temp_dir=tmp_path / "t")
        cfg.developer = DeveloperConfig(enabled=True, repos_dir="")
        monkeypatch.setattr("istota.config.load_config", lambda *a, **k: cfg)
        code, envelope = drive(capsys, "run", "--worktree", str(worktree))
        assert code == 1
        assert envelope["reason"] == "repos_dir_unset"

    def test_review_disabled(
        self, capsys, worktree, review_env, developer_config, no_brain
    ):
        developer_config(enabled=False)
        code, envelope = drive(capsys, "run", "--worktree", str(worktree))
        assert code == 1
        assert envelope["reason"] == "review_disabled"

    def test_non_admin_refused(
        self, capsys, monkeypatch, worktree, review_env, developer_config, no_brain
    ):
        cfg = developer_config()
        cfg.admin_users = {"someone-else"}
        monkeypatch.setenv("ISTOTA_USER_ID", "nonadmin")
        code, envelope = drive(capsys, "run", "--worktree", str(worktree))
        assert code == 1
        assert envelope["reason"] == "not_admin"

    def test_admin_check_fails_open_with_no_admins_file(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """Matches the sandbox bind exactly: an empty admin set binds repos_dir
        for everyone, so refusing here would deny a worktree the deployment
        already handed out. `is_shared_kv_writer` deliberately does the
        opposite; the two must not be collapsed."""
        cfg = developer_config()
        assert cfg.admin_users == set()
        code, envelope = drive(capsys, "run", "--worktree", str(worktree))
        assert code == 0
        assert envelope["status"] == "ok"

    def test_worktree_outside_repos_dir(
        self, capsys, tmp_path, worktree, review_env, developer_config, no_brain
    ):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        developer_config()
        code, envelope = drive(capsys, "run", "--worktree", str(outside))
        assert code == 1
        assert envelope["reason"] == "path_not_allowed"

    def test_symlink_out_of_repos_dir(
        self, capsys, tmp_path, repos_root, review_env, developer_config, no_brain
    ):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        link = repos_root / "sneaky"
        link.symlink_to(outside, target_is_directory=True)
        developer_config()
        code, envelope = drive(capsys, "run", "--worktree", str(link))
        assert code == 1
        assert envelope["reason"] == "path_not_allowed"

    def test_tmux_brain_is_skipped_not_errored(
        self, capsys, worktree, review_env, developer_config, no_brain
    ):
        """A tmux deployment has no text-only path at all. Reporting it as an
        error would block every push on a deployment that can never review."""
        cfg = developer_config()
        cfg.brain.kind = "tmux_claude"
        code, envelope = drive(capsys, "run", "--worktree", str(worktree))
        assert code == 0
        assert envelope["status"] == "skipped"
        assert envelope["reason"] == "brain_unsupported"

    def test_a_refused_run_does_not_increment_the_counter(
        self, capsys, worktree, review_env, developer_config, review_db, no_brain
    ):
        developer_config(enabled=False)
        drive(capsys, "run", "--worktree", str(worktree))
        with db.get_db(review_db) as conn:
            assert db.code_review_calls_get(conn, review_env) == 0


# --------------------------------------------------------------------------
# The brain seam
# --------------------------------------------------------------------------


class TestReviewRun:
    def test_happy_path_envelope(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        developer_config()
        stub_brain.replies["conformance"] = [
            findings_json(finding(severity="must-fix", line=4, claim="no test"))
        ]
        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--intent", "add a helper",
        )
        assert code == 0
        assert envelope["status"] == "ok"
        assert envelope["agents"] == ["conformance"]
        assert envelope["range"] == "main...HEAD"
        assert envelope["sizing_reason"]
        assert envelope["counts"]["must-fix"] == 1
        assert envelope["counts"]["total"] == 1
        assert envelope["findings"][0]["file"] == "app.py"
        assert envelope["findings"][0]["sources"] == ["conformance"]
        assert envelope["notice"]
        assert envelope["partial"] is False

    def test_intent_reaches_the_prompt_and_the_model_is_resolved(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        developer_config()
        drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--intent", "add a doubling helper",
        )
        assert "add a doubling helper" in stub_brain.prompts[0]
        assert "## Diff" in stub_brain.prompts[0]

    def test_effort_modifier_is_split_off_rather_than_swallowed(
        self, capsys, worktree, review_env, developer_config, stub_brain, monkeypatch
    ):
        """`resolve_model_name` strips a `:effort` tail and keeps only the base,
        so a config of `smart:high` handed to it whole runs at default effort
        and silently ignores the operator's setting."""
        seen = {}

        def _capture(req):
            seen["model"] = req.model
            seen["effort"] = req.effort
            return StubResult(result_text='{"findings": []}')

        monkeypatch.setattr(StubBrain, "execute", lambda self, req: _capture(req))
        developer_config(conformance_model="general:medium")
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert seen["model"] == "resolved/general"
        assert seen["effort"] == "medium"

    def test_both_agents_when_forced(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        developer_config()
        stub_brain.replies["conformance"] = [findings_json(finding(line=4))]
        stub_brain.replies["bughunt"] = [findings_json(finding(line=4))]
        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--agents", "both",
        )
        assert code == 0
        assert sorted(envelope["agents"]) == ["bughunt", "conformance"]
        # Merged by (file, line), so one entry carrying both sources.
        assert len(envelope["findings"]) == 1
        assert envelope["findings"][0]["sources"] == ["bughunt", "conformance"]

    def test_empty_diff_is_ok_and_costs_nothing(
        self, capsys, empty_worktree, review_env, developer_config, stub_brain
    ):
        developer_config()
        code, envelope = drive(
            capsys, "run", "--worktree", str(empty_worktree), "--base", "main"
        )
        assert code == 0
        assert envelope["status"] == "ok"
        assert envelope["findings"] == []
        assert envelope["notice"]
        assert stub_brain.calls == []

    def test_breaker_open_skips_without_calling(
        self, capsys, worktree, review_env, developer_config, stub_brain, monkeypatch
    ):
        monkeypatch.setattr(
            "istota.brain.primary_brain_unavailable",
            lambda cfg: (False, "usage_limit"),
        )
        developer_config()
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 0
        assert envelope["status"] == "skipped"
        assert envelope["reason"] == "brain_unavailable"
        assert stub_brain.calls == []

    def test_breaker_skip_does_not_increment(
        self, capsys, worktree, review_env, developer_config, stub_brain,
        review_db, monkeypatch,
    ):
        monkeypatch.setattr(
            "istota.brain.primary_brain_unavailable", lambda cfg: (False, "usage_limit")
        )
        developer_config()
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        with db.get_db(review_db) as conn:
            assert db.code_review_calls_get(conn, review_env) == 0

    def test_second_agent_failing_leaves_the_first_intact(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        developer_config()
        stub_brain.replies["conformance"] = [findings_json(finding(claim="real"))]
        stub_brain.replies["bughunt"] = [RuntimeError("api exploded")]
        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--agents", "both",
        )
        assert code == 0
        assert envelope["status"] == "ok"
        assert envelope["partial"] is True
        assert "bughunt" in envelope["partial_reason"]
        assert envelope["agents"] == ["conformance"]
        assert len(envelope["findings"]) == 1

    def test_malformed_once_then_good_is_one_round(
        self, capsys, worktree, review_env, developer_config, stub_brain, review_db
    ):
        developer_config()
        stub_brain.replies["conformance"] = [
            "I would rather explain this in prose.",
            findings_json(finding()),
        ]
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 0
        assert envelope["status"] == "ok"
        assert len(envelope["findings"]) == 1
        assert stub_brain.calls == ["conformance", "conformance"]
        with db.get_db(review_db) as conn:
            assert db.code_review_calls_get(conn, review_env) == 1

    def test_malformed_twice_is_an_error_carrying_the_raw_output(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        developer_config()
        stub_brain.replies["conformance"] = ["not json", "still not json"]
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 1
        assert envelope["status"] == "error"
        assert envelope["reason"] == "malformed_output"
        assert "not json" in envelope["error"]

    def test_bad_range_returns_git_stderr(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        developer_config()
        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--range", "no-such-ref...HEAD"
        )
        assert code == 1
        assert envelope["status"] == "error"
        assert envelope["error"]


# --------------------------------------------------------------------------
# The cap
# --------------------------------------------------------------------------


class TestCallCap:
    def test_runs_up_to_the_cap_then_degrade_to_skipped(
        self, capsys, worktree, review_env, developer_config, stub_brain, review_db
    ):
        developer_config(max_calls_per_task=2)
        for _ in range(2):
            code, envelope = drive(
                capsys, "run", "--worktree", str(worktree), "--base", "main"
            )
            assert code == 0
            assert envelope["status"] == "ok"

        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 0, "the cap degrades rather than blocking a task that already worked"
        assert envelope["status"] == "skipped"
        assert envelope["reason"] == "call_cap"
        assert envelope["calls_used"] == 2
        assert envelope["max_calls"] == 2

    def test_the_counter_is_read_back_from_the_database(
        self, capsys, worktree, review_env, developer_config, stub_brain, review_db
    ):
        """Not from a file under ISTOTA_DEFERRED_DIR, which the model can write."""
        developer_config(max_calls_per_task=5)
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        with db.get_db(review_db) as conn:
            assert db.code_review_calls_get(conn, review_env) == 2

    def test_at_the_cap_no_model_call_is_made(
        self, capsys, worktree, review_env, developer_config, stub_brain, review_db
    ):
        developer_config(max_calls_per_task=1)
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        stub_brain.calls.clear()
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert stub_brain.calls == []


# --------------------------------------------------------------------------
# The timeout budget
# --------------------------------------------------------------------------


class TestTimeoutBudget:
    def test_each_agent_gets_the_configured_timeout(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """Both agents run concurrently, so wall time is max(t1, t2) and each
        gets the whole `timeout_seconds` rather than half of it."""
        developer_config(timeout_seconds=45)
        drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--agents", "both",
        )
        assert stub_brain.timeouts == [45, 45]

    def test_a_budget_over_the_proxy_ceiling_warns_the_operator(
        self, capsys, caplog, worktree, review_env, developer_config, stub_brain
    ):
        """The proxy kills the command at `security.skill_proxy_timeout`, so an
        operator who raises `timeout_seconds` past it should find out before a
        review dies half-finished rather than after."""
        cfg = developer_config(timeout_seconds=400)
        cfg.security.skill_proxy_timeout = 300
        with caplog.at_level("WARNING"):
            drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert any("skill_proxy_timeout" in r.message for r in caplog.records)


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


def drive(capsys, *argv) -> tuple[int, dict]:
    """Run `main(argv)`, returning `(exit_code, parsed envelope)`.

    The facade contract is one line of JSON on stdout and an exit code, so the
    tests read exactly what the workflow reads.
    """
    capsys.readouterr()
    with pytest.raises(SystemExit) as excinfo:
        code_review.main(list(argv))
    out = capsys.readouterr().out.strip()
    assert out, "the CLI printed nothing"
    envelope = json.loads(out.splitlines()[-1])
    code = excinfo.value.code
    return (0 if code is None else int(code)), envelope
