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

**A round is a wave of calls, and it is charged on invocations made rather than
on answers parsed.** A run refused by a guard and one short-circuited by the
availability breaker are free, because they spent nothing; the retry half of a
malformed-output round rides on the round that provoked it, because it did. A
reviewer that answers in prose twice has spent real money, and counting only
clean rounds would leave that loop unbounded — the failure the cap exists to
prevent, inverted. One run charges 1, the reformat of an unparseable answer
included.

The brain is the mock boundary, the same place the sleep-cycle and explainer
tests draw it. There is no live model call anywhere in this file.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
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
    """The caller's own subtree of `developer.repos_dir`.

    `setup_env` derives the variable as `{repos_dir}/{user_id}` and
    `developer_repos_root` refuses a value not named for `ISTOTA_USER_ID`, so
    the user id here has to match the one `review_env` sets.
    """
    root = tmp_path / "repos" / "admin"
    root.mkdir(parents=True)
    monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(root))
    monkeypatch.setenv("ISTOTA_USER_ID", "admin")
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
    # Mirrors BrainResult. A double that drifts from the contract it stands in
    # for stops testing the caller and starts testing the double.
    brain_kind: str = ""
    actions_taken: object | None = None
    execution_trace: object | None = None
    partial_text: str | None = None


@dataclass
class StubBrain:
    """A brain that answers from a per-agent script and records what it saw."""

    replies: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)
    prompts: list = field(default_factory=list)
    timeouts: list = field(default_factory=list)
    # Whether each call asked the brain to stream. Recorded rather than assumed:
    # the non-streaming path discards a timed-out call's usage and its partial
    # text, so which path the reviewer takes is a property of the CLI worth
    # pinning (ISSUE-448).
    streaming: list = field(default_factory=list)
    # Wall time one call burns, for the tests that drive a budget to exhaustion.
    delay: float = 0.0
    # The whole request, for the tests about what a call is granted.
    requests: list = field(default_factory=list)

    def resolve_model_name(self, name: str) -> str:
        return f"resolved/{name}"

    def execute(self, req):
        if self.delay:
            time.sleep(self.delay)
        # One reviewer, so one script: the review call and its reformat, if
        # any, take replies from it in order.
        agent = "reviewer"
        self.calls.append(agent)
        self.prompts.append(req.prompt)
        self.timeouts.append(req.timeout_seconds)
        self.streaming.append(req.streaming)
        self.requests.append(req)
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
    monkeypatch.setattr(
        "istota.brain.report_brain_result", lambda result, cfg, **kwargs: None
    )
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
        # The per-user temp dir a deployment creates at startup. Without it
        # `build_daemon_sandbox` refuses and every run here would be text-only.
        (tmp_path / "temp" / "admin").mkdir(parents=True, exist_ok=True)
        cfg.developer = DeveloperConfig(
            enabled=True,
            # Read back from the environment on purpose: the `worktree` fixture
            # (line 87) sets `DEVELOPER_REPOS_DIR` to the tmp root it built, and
            # `_make` has no other way to reach it. Not an ambient read despite
            # appearances — `test_repos_root_missing_from_the_environment_is_skipped`
            # clears it deliberately, and the ISSUE-301 scrub runs before the
            # `worktree` fixture, so what lands here is always the test's own.
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
            'model = "fast:low"\n'
            "file_budget = 3\n"
            "snapshot_max_bytes = 5000\n"
            "snapshot_max_file_bytes = 700\n"
            "max_diff_chars = 1234\n"
            "max_calls_per_task = 3\n"
            "timeout_seconds = 90\n"
        )
        cfg = load_config(path)
        review = cfg.developer.review
        assert review.enabled is False
        assert review.model == "fast:low"
        assert review.file_budget == 3
        assert review.snapshot_max_bytes == 5000
        assert review.snapshot_max_file_bytes == 700
        assert review.max_diff_chars == 1234
        assert review.max_calls_per_task == 3
        assert review.timeout_seconds == 90

    def test_defaults_hold_when_the_block_is_absent(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('[developer]\nenabled = true\nrepos_dir = "/srv/repos"\n')
        review = load_config(path).developer.review
        assert review.enabled is True
        assert review.model == "smart:high"
        assert review.file_budget == 8
        assert review.snapshot_max_bytes == 104_857_600
        assert review.snapshot_max_file_bytes == 2_097_152
        assert review.max_calls_per_task == 8

    def test_unknown_key_is_ignored_rather_than_fatal(self, tmp_path, caplog):
        path = tmp_path / "config.toml"
        path.write_text(
            "[developer]\nenabled = true\n"
            "[developer.review]\nno_such_key = 7\nmax_calls_per_task = 2\n"
        )
        with caplog.at_level("WARNING", logger="istota.config"):
            review = load_config(path).developer.review
        assert review.max_calls_per_task == 2
        # Still reported: the hook walks the section with its own unknown list.
        assert any("developer.review.no_such_key" in r.getMessage() for r in caplog.records)

    def test_bughunt_model_stands_in_for_an_absent_model(self, tmp_path, caplog):
        """The one reviewer is the correctness reviewer `bughunt_model` chose a
        model for, so an upgraded deployment keeps the model it configured."""
        path = tmp_path / "config.toml"
        path.write_text(
            "[developer]\nenabled = true\n"
            '[developer.review]\nbughunt_model = "general:medium"\n'
        )
        with caplog.at_level("INFO", logger="istota.config"):
            review = load_config(path).developer.review
        assert review.model == "general:medium"
        assert any("bughunt_model" in r.getMessage() for r in caplog.records)

    def test_model_wins_over_bughunt_model(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(
            "[developer]\nenabled = true\n"
            '[developer.review]\nmodel = "fast"\nbughunt_model = "general:medium"\n'
        )
        assert load_config(path).developer.review.model == "fast"

    def test_retired_keys_are_dropped_without_an_unknown_key_warning(
        self, tmp_path, caplog
    ):
        """An upgraded deployment did nothing wrong by still having them."""
        path = tmp_path / "config.toml"
        path.write_text(
            "[developer]\nenabled = true\n"
            "[developer.review]\n"
            'conformance_model = "general"\n'
            'bughunt_model = "smart:high"\n'
            "both_agents_threshold_lines = 150\n"
            'boundary_patterns = ["auth"]\n'
            "max_context_chars = 60000\n"
            "max_file_chars = 20000\n"
            "max_callers_per_symbol = 8\n"
            "max_need_files = 6\n"
        )
        with caplog.at_level("INFO", logger="istota.config"):
            review = load_config(path).developer.review
        assert review.model == "smart:high"
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert not any("unrecognised" in m for m in warnings), warnings
        infos = " ".join(r.getMessage() for r in caplog.records if r.levelname == "INFO")
        assert "max_need_files" in infos and "boundary_patterns" in infos

    @pytest.mark.parametrize("value", [0, -3])
    def test_a_non_positive_file_budget_reads_as_the_default(
        self, tmp_path, caplog, value
    ):
        """Zero files would turn the reviewer back into a text-only one by
        configuration."""
        path = tmp_path / "config.toml"
        path.write_text(
            "[developer]\nenabled = true\n"
            f"[developer.review]\nfile_budget = {value}\n"
        )
        with caplog.at_level("WARNING", logger="istota.config"):
            review = load_config(path).developer.review
        assert review.file_budget == 8
        assert any("file_budget" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("key", ["snapshot_max_bytes", "snapshot_max_file_bytes"])
    def test_a_non_positive_snapshot_cap_keeps_the_default(self, tmp_path, key):
        """A zero cap would write an empty tree for a reviewer with tools."""
        path = tmp_path / "config.toml"
        path.write_text(
            "[developer]\nenabled = true\n"
            f"[developer.review]\n{key} = 0\n"
        )
        review = load_config(path).developer.review
        assert getattr(review, key) == getattr(ReviewConfig(), key)

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

    def test_a_multi_round_charge_lands_in_one_statement(self, review_db, task_row):
        """The helper takes a count, and charging it in one upsert is what keeps
        the guarantee that two concurrent reviews cannot interleave into a
        single increment. A run charges 1 today; the count stays general."""
        with db.get_db(review_db) as conn:
            assert db.code_review_calls_increment(conn, task_row, 2) == 2
            assert db.code_review_calls_increment(conn, task_row, 2) == 4

    def test_the_cascade_is_decorative_like_every_other_fk_here(
        self, review_db, task_row
    ):
        """`PRAGMA foreign_keys` is never enabled on these connections, so the
        `ON DELETE CASCADE` on `code_review_calls` does not fire — matching every
        other FK in `db.py`, each annotated the same way. Pinned because the
        docstring used to claim the opposite, and a test that switched the pragma
        on itself would have validated a behaviour production never has.
        """
        with db.get_db(review_db) as conn:
            db.code_review_calls_increment(conn, task_row)
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_row,))
            conn.commit()
            assert db.code_review_calls_get(conn, task_row) == 1


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

    def test_review_disabled_is_skipped_not_errored(
        self, capsys, worktree, review_env, developer_config, no_brain
    ):
        """An operator switch is a state of the deployment, not of the diff.

        An `error` blocks the push, so filing it here would mean a deployment
        that deliberately turned review off could never land anything. The
        config block shipped alongside says as much: "false disables the CLI;
        the workflow then reports 'review unavailable' and lands anyway".
        """
        developer_config(enabled=False)
        code, envelope = drive(capsys, "run", "--worktree", str(worktree))
        assert code == 0
        assert envelope["status"] == "skipped"
        assert envelope["reason"] == "review_disabled"

    def test_repos_root_missing_from_the_environment_is_skipped(
        self, capsys, monkeypatch, tmp_path, review_env, developer_config, no_brain
    ):
        """`repos_dir` in config and `DEVELOPER_REPOS_DIR` in the environment can
        disagree: the variable is injected for *authorized* skills only, so a
        deployment with `[developer]` configured but no resolved credential has
        the config key and not the variable. Reporting that as
        `path_not_allowed` would blame the caller's path and block the push for
        something no amount of not-pushing fixes.
        """
        cfg = developer_config()
        cfg.developer.repos_dir = str(tmp_path / "repos")
        monkeypatch.delenv("DEVELOPER_REPOS_DIR", raising=False)
        code, envelope = drive(capsys, "run", "--worktree", str(tmp_path / "repos/x"))
        assert code == 0
        assert envelope["status"] == "skipped"
        assert envelope["reason"] == "repos_root_unavailable"

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
        self, capsys, tmp_path, monkeypatch, worktree, review_env, review_db, no_brain
    ):
        cfg = Config(db_path=tmp_path / "db", temp_dir=tmp_path / "t")
        cfg.developer = DeveloperConfig(enabled=False, repos_dir=str(worktree.parent))
        monkeypatch.setattr("istota.config.load_config", lambda *a, **k: cfg)
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
        stub_brain.replies["reviewer"] = [
            findings_json(finding(severity="must-fix", line=4, claim="no test"))
        ]
        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--intent", "add a helper",
        )
        assert code == 0
        assert envelope["status"] == "ok"
        assert envelope["range"] == "main...HEAD"
        assert envelope["counts"]["must-fix"] == 1
        assert envelope["counts"]["total"] == 1
        assert envelope["findings"][0]["file"] == "app.py"
        assert "sources" not in envelope["findings"][0]
        assert envelope["notice"]
        assert envelope["rounds"] == 1

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
        developer_config(model="general:medium")
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert seen["model"] == "resolved/general"
        assert seen["effort"] == "medium"

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

    def test_malformed_once_then_good_is_one_round(
        self, capsys, worktree, review_env, developer_config, stub_brain, review_db
    ):
        developer_config()
        stub_brain.replies["reviewer"] = [
            "I would rather explain this in prose.",
            findings_json(finding()),
        ]
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 0
        assert envelope["status"] == "ok"
        assert len(envelope["findings"]) == 1
        assert stub_brain.calls == ["reviewer", "reviewer"]
        with db.get_db(review_db) as conn:
            assert db.code_review_calls_get(conn, review_env) == 1

    def test_malformed_twice_is_skipped_not_errored_and_carries_the_raw_output(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """A bad *response* is not a bad request. Nothing about the diff, the
        range or the paths caused it, and no change to any of them fixes it —
        so blocking the push on it strands finished work for a reason the
        branch cannot answer for. A broken adapter did exactly that on
        2026-08-21: every review on the deployment came back malformed."""
        developer_config()
        stub_brain.replies["reviewer"] = ["not json", "still not json"]
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 0
        assert envelope["status"] == "skipped"
        assert envelope["reason"] == "malformed_output"
        assert "not json" in envelope["error"]

    def test_a_failed_reviewer_call_is_skipped_not_errored(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """The other half of the same block. A reviewer whose call never
        returned is the degraded brain `skipped` already exists for; it reached
        the caller as `error` only because it shared a return with the
        malformed path."""
        developer_config()
        stub_brain.replies["reviewer"] = [
            StubResult(success=False, stop_reason="api_error")
        ]
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 0
        assert envelope["status"] == "skipped"
        assert envelope["reason"] == "review_failed"
        assert "api_error" in envelope["error"]
        # A failed call is not reformatted: there is no answer to reformat.
        assert stub_brain.calls == ["reviewer"]

    def test_the_error_quotes_what_the_reviewer_actually_said(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """`skill.md` promises the reading model the head of the reviewer's own
        output on a failed review, and the code dropped `result_text`, so only
        the `stop_reason` slug arrived. Every cause — no credential, no route,
        an unknown model — is `error`, and the sentence the CLI exits on is the
        one thing that tells them apart."""
        developer_config()
        stub_brain.replies["reviewer"] = [
            StubResult(
                success=False,
                stop_reason="error",
                result_text="Not logged in \u00b7 Please run /login\n",
            )
        ]
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 0
        assert envelope["reason"] == "review_failed"
        assert "Not logged in" in envelope["error"]
        # One line: the envelope is JSON a model reads, not a log.
        assert "\n" not in envelope["error"]

    def test_a_reviewer_that_said_nothing_still_gets_a_usable_error(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        developer_config()
        stub_brain.replies["reviewer"] = [
            StubResult(success=False, stop_reason="timeout", result_text="")
        ]
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 0
        assert "timeout" in envelope["error"]
        assert not envelope["error"].rstrip().endswith(":")

    def test_a_reviewer_that_said_far_too_much_is_capped(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        developer_config()
        stub_brain.replies["reviewer"] = [
            StubResult(success=False, stop_reason="error", result_text="x" * 5000)
        ]
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 0
        assert envelope["error"].count("x") == code_review._ERROR_TEXT_CHARS

    def test_a_request_fault_inside_a_reviewer_still_blocks_the_push(
        self, capsys, monkeypatch, worktree, review_env, developer_config, stub_brain
    ):
        """`run_review`'s catch-all around the reviewer sits between a
        containment refusal and a push. A failed reviewer is `skipped`, so a
        `ReviewError` caught there as one would tell the workflow to land a
        branch whose worktree reaches outside the allowed roots. Nothing raises
        there today — this pins the classification so a future unwrapped raiser
        fails closed."""
        from istota.skills.code_review import engine

        developer_config()
        monkeypatch.setattr(
            engine, "_run_reviewer",
            lambda *a, **k: (_ for _ in ()).throw(
                engine.ReviewError(
                    "repo reaches outside DEVELOPER_REPOS_DIR",
                    reason="git_dir_not_allowed",
                )
            ),
        )
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 1
        assert envelope["status"] == "error"
        # The ReviewError's own slug, not a category: the workflow branches on it.
        assert envelope["reason"] == "git_dir_not_allowed"
        assert "reaches outside" in envelope["error"]

    def test_a_skip_that_spent_model_calls_says_so(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """`rounds` is what separates a skip that burned invocations from one
        that refused before spending any. Both are `status: skipped` with an
        empty `findings`, and a caller deciding whether re-running is free
        cannot tell them apart otherwise."""
        developer_config()
        stub_brain.replies["reviewer"] = ["not json", "still not json"]
        _, spent = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert spent["status"] == "skipped"
        assert spent["rounds"] == 1

        developer_config(enabled=False)
        _, refused = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert refused["status"] == "skipped"
        assert refused["reason"] == "review_disabled"
        assert refused.get("rounds", 0) == 0

    def test_an_all_failed_review_carries_its_own_notice(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """The ordinary notice opens by talking about findings, and there are
        none here. What this envelope carries is raw reviewer output in `error`,
        on a status whose instruction is to land the work and name the reason —
        so the untrusted-input warning has to cover that field, not findings."""
        developer_config()
        stub_brain.replies["reviewer"] = ["not json", "still not json"]
        _, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert envelope["findings"] == []
        assert "error` field quotes raw reviewer output" in envelope["notice"]

    def test_a_round_that_spent_calls_and_failed_still_charges_the_budget(
        self, capsys, worktree, review_env, developer_config, stub_brain, review_db
    ):
        """Otherwise a reviewer that reliably answers in prose loops forever:
        the round returns nothing usable, the workflow re-runs, and the cap that
        is supposed to stop the spend never moves because no round ever
        "succeeded". The charge is what bounds it, not the exit code."""
        developer_config()
        stub_brain.replies["reviewer"] = ["not json", "still not json"]
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        with db.get_db(review_db) as conn:
            assert db.code_review_calls_get(conn, review_env) == 1

    def test_a_well_shaped_envelope_of_unusable_findings_is_not_a_clean_review(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """`parse_findings` drops any item naming no file, so a must-fix without
        one empties to `[]` and would otherwise be reported as `ok` with zero
        findings — indistinguishable from a reviewer that found nothing. The
        prompt asks explicitly for findings the reviewer could not verify, which
        is exactly where a missing `file` comes from."""
        developer_config()
        stub_brain.replies["reviewer"] = [
            json.dumps({"findings": [{"severity": "must-fix", "claim": "secret leaked"}]}),
            findings_json(finding(severity="must-fix", file="app.py", line=4)),
        ]
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        # Retried rather than accepted, and the retry's usable finding survives.
        assert stub_brain.calls == ["reviewer", "reviewer"]
        assert code == 0
        assert envelope["counts"]["must-fix"] == 1

    def test_partly_unusable_findings_are_counted_not_swallowed(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        developer_config()
        stub_brain.replies["reviewer"] = [
            json.dumps({
                "findings": [
                    finding(severity="high", file="app.py", line=4),
                    {"severity": "must-fix", "claim": "no file named"},
                ]
            })
        ]
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 0
        assert envelope["counts"]["total"] == 1
        assert envelope["dropped_findings"] == 1

    def test_an_empty_range_is_flagged_machine_readably(
        self, capsys, empty_worktree, review_env, developer_config, stub_brain
    ):
        """A gate reading `status == "ok" and counts["must-fix"] == 0` would
        otherwise take an unreviewed empty range for a clean review; prose in
        `notice` is not something a consumer branches on."""
        developer_config()
        _, envelope = drive(
            capsys, "run", "--worktree", str(empty_worktree), "--base", "main"
        )
        assert envelope["empty"] is True

    def test_every_envelope_carries_the_same_keys(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """A consumer must be able to read `findings` or `counts` without first
        branching on `status`, and the all-reviewers-failed path — the only one
        that embeds raw model text — must carry the untrusted-input notice like
        the rest."""
        developer_config()
        _, ok = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        stub_brain.replies["reviewer"] = ["not json", "still not json"]
        _, err = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        for key in ("findings", "counts", "ruled_out", "reviewer", "snapshot", "notice", "range"):
            assert key in ok, key
            assert key in err, key

    def test_bad_range_returns_git_stderr(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """Git's own diagnosis has to survive into the envelope. A generic "bad
        range" costs the caller a round trip working out which ref was wrong."""
        developer_config()
        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--range", "no-such-ref...HEAD"
        )
        assert code == 1
        assert envelope["status"] == "error"
        assert "no-such-ref" in envelope["error"]

    def test_an_unexpected_exception_still_produces_an_envelope(
        self, capsys, monkeypatch, worktree, review_env, developer_config, stub_brain
    ):
        """The facade contract is one line of JSON and an exit code, and the
        scheduler sniffs stdout for that shape. The engine shells out through
        `subprocess.Popen`, which raises OSError and friends outside
        `ReviewError`."""
        developer_config()
        monkeypatch.setattr(
            "istota.skills.code_review.engine.run_review",
            lambda *a, **k: (_ for _ in ()).throw(OSError("git vanished")),
        )
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 1
        assert envelope["status"] == "error"
        assert envelope["reason"] == "internal_error"
        assert "git vanished" in envelope["error"]


# --------------------------------------------------------------------------
# The tool grant, its confinement, and cleanup
# --------------------------------------------------------------------------


def run_dirs(tmp_path: Path) -> list[Path]:
    root = tmp_path / "temp" / ".review"
    return sorted(root.rglob("run-*")) if root.exists() else []


def work_dirs(tmp_path: Path) -> list[Path]:
    root = tmp_path / "temp" / ".sandbox-work"
    return sorted(root.rglob("work-*")) if root.exists() else []


class TestTheReviewerRequest:
    def test_the_review_call_reads_the_snapshot_and_nothing_else(
        self, capsys, tmp_path, worktree, review_env, developer_config, stub_brain
    ):
        developer_config()
        seen = {}

        real_execute = StubBrain.execute

        def _capture(self, req):
            # Taken while the call runs: the run directory is gone afterwards.
            seen["tree_files"] = sorted(
                p.name for p in Path(req.cwd).iterdir()
            ) if req.allowed_tools else None
            return real_execute(self, req)

        stub_brain.execute = _capture.__get__(stub_brain)
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")

        assert code == 0
        req = stub_brain.requests[0]
        assert req.allowed_tools == ["Read", "Grep", "Glob"]
        assert len(req.fs_read_roots) == 1
        run_dir = Path(req.fs_read_roots[0])
        assert run_dir.resolve().parent == (tmp_path / "temp" / ".review" / "admin").resolve()
        assert run_dir.name.startswith("run-")
        assert Path(req.cwd) == run_dir / "tree"
        assert req.sandbox_wrap is not None
        assert seen["tree_files"] == ["AGENTS.md", "app.py"]
        assert envelope["reviewer"]["tools"] is True
        assert envelope["snapshot"]["files"] == 2
        assert envelope["deprecated_flags"] == []

    def test_the_namespace_withholds_every_scope_and_binds_the_run_dir(
        self, capsys, monkeypatch, worktree, review_env, developer_config, stub_brain
    ):
        from istota import executor

        developer_config()
        calls = []
        real = executor.build_daemon_sandbox

        def _spy(config, user_id, **kwargs):
            calls.append((user_id, kwargs))
            return real(config, user_id, **kwargs)

        monkeypatch.setattr(executor, "build_daemon_sandbox", _spy)
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")

        assert len(calls) == 1
        user_id, kwargs = calls[0]
        assert user_id == "admin"
        withheld = kwargs["withheld_scopes"]
        assert {"files", "memory", "developer"} <= withheld
        # The whole scope list, not the three named: every private skill too.
        assert len(withheld) > 3
        run_dir = Path(stub_brain.requests[0].fs_read_roots[0])
        assert [Path(p) for p in kwargs["extra_ro_binds"]] == [run_dir]

    def test_the_reformat_call_is_granted_nothing(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        developer_config()
        stub_brain.replies["reviewer"] = ["prose, not json", findings_json(finding())]
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")

        tooled, reformat = stub_brain.requests
        assert tooled.allowed_tools == ["Read", "Grep", "Glob"]
        assert reformat.allowed_tools == []
        assert reformat.sandbox_wrap is None
        assert not reformat.fs_read_roots

    def test_a_refused_namespace_reviews_text_only(
        self, capsys, tmp_path, worktree, review_env, developer_config, stub_brain
    ):
        """A per-user temp dir that is a link names no directory the namespace
        can be built around: the review still runs, with no tool grant, and
        says why."""
        developer_config()
        (tmp_path / "temp" / "admin").rmdir()
        (tmp_path / "elsewhere").mkdir()
        (tmp_path / "temp" / "admin").symlink_to(tmp_path / "elsewhere")
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")

        assert code == 0
        assert envelope["status"] == "ok"
        assert envelope["reviewer"]["tools"] is False
        assert envelope["reviewer"]["tools_reason"] == "sandbox_refused"
        assert [r.allowed_tools for r in stub_brain.requests] == [[]]
        assert run_dirs(tmp_path) == []

    def test_a_snapshot_that_cannot_be_built_reviews_text_only(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        cfg = developer_config()
        cfg.temp_dir = ""
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")

        assert code == 0
        assert envelope["reviewer"]["tools"] is False
        assert envelope["reviewer"]["tools_reason"] == "snapshot_failed"
        assert envelope["snapshot"] is None
        assert [r.allowed_tools for r in stub_brain.requests] == [[]]

    def test_a_successful_run_leaves_no_run_or_work_dir(
        self, capsys, tmp_path, worktree, review_env, developer_config, stub_brain,
        review_db,
    ):
        developer_config()
        stub_brain.replies["reviewer"] = [findings_json(finding())]
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")

        assert stub_brain.requests[0].allowed_tools  # the tooled path ran
        assert run_dirs(tmp_path) == []
        assert work_dirs(tmp_path) == []
        with db.get_db(review_db) as conn:
            assert db.code_review_calls_get(conn, review_env) == 1

    def test_a_failed_model_call_leaves_no_run_or_work_dir(
        self, capsys, tmp_path, worktree, review_env, developer_config, stub_brain
    ):
        developer_config()
        stub_brain.replies["reviewer"] = [
            StubResult(success=False, stop_reason="api_error")
        ]
        _, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")

        assert envelope["reason"] == "review_failed"
        assert stub_brain.requests[0].allowed_tools
        assert run_dirs(tmp_path) == []
        assert work_dirs(tmp_path) == []

    def test_a_request_fault_after_the_snapshot_leaves_no_run_or_work_dir(
        self, capsys, monkeypatch, tmp_path, worktree, review_env,
        developer_config, stub_brain,
    ):
        from istota.skills.code_review import engine

        developer_config()
        seen = {}

        def _raise(*args, **kwargs):
            seen["run_dirs"] = run_dirs(tmp_path)
            raise engine.ReviewError("reaches outside", reason="git_dir_not_allowed")

        monkeypatch.setattr(engine, "_run_reviewer", _raise)
        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--agents", "both",
        )

        assert code == 1
        assert envelope["reason"] == "git_dir_not_allowed"
        assert envelope["deprecated_flags"] == ["--agents"]
        # Control: the snapshot did exist while the run was in progress.
        assert len(seen["run_dirs"]) == 1
        assert run_dirs(tmp_path) == []
        assert work_dirs(tmp_path) == []

    def test_a_guard_refusal_reports_the_retired_flag_too(
        self, capsys, worktree, review_env, developer_config, no_brain
    ):
        developer_config(enabled=False)
        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--agents", "both",
        )
        assert code == 0
        assert envelope["reason"] == "review_disabled"
        assert envelope["deprecated_flags"] == ["--agents"]

    def test_a_linked_temp_dir_gives_the_reviewer_resolved_paths(
        self, capsys, tmp_path, worktree, review_env, developer_config, stub_brain
    ):
        """The namespace binds the run directory at its resolved path, so the
        roots, the cwd and the paths in the prompt have to be that one too."""
        cfg = developer_config()
        real = tmp_path / "real-temp"
        (real / "admin").mkdir(parents=True)
        link = tmp_path / "linked-temp"
        link.symlink_to(real)
        cfg.temp_dir = link
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")

        req = stub_brain.requests[0]
        assert req.allowed_tools  # the tooled path ran
        run_dir = Path(req.fs_read_roots[0])
        assert run_dir == run_dir.resolve()
        assert run_dir.parent == (real / ".review" / "admin").resolve()
        assert Path(req.cwd) == run_dir / "tree"
        assert str(run_dir / "tree") in req.prompt

    @pytest.mark.parametrize("value", ["both", "conformance", "bughunt"])
    def test_the_retired_agents_flag_is_accepted_and_reported(
        self, capsys, worktree, review_env, developer_config, stub_brain, value
    ):
        """argparse would otherwise turn an old workflow's flag into a usage
        error that reads like a broken skill."""
        developer_config()
        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--agents", value,
        )
        assert code == 0
        assert envelope["status"] == "ok"
        assert envelope["deprecated_flags"] == ["--agents"]
        assert len(stub_brain.requests) == 1

    def test_the_configured_model_is_the_reviewer_s(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        developer_config(model="fast:low")
        _, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        req = stub_brain.requests[0]
        assert req.model == "resolved/fast"
        assert req.effort == "low"
        assert envelope["reviewer"]["model"] == "resolved/fast"


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

    def test_a_cap_of_zero_permits_nothing_rather_than_everything(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """On a spend control the expensive reading of 0 is the wrong one to
        guess at. An operator who wants the feature off has `enabled = false`."""
        developer_config(max_calls_per_task=0)
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 0
        assert envelope["status"] == "skipped"
        assert envelope["reason"] == "call_cap"
        assert stub_brain.calls == []

    def test_an_unreadable_budget_does_not_sink_the_review(
        self, capsys, monkeypatch, worktree, review_env, developer_config, stub_brain
    ):
        """Losing the cap check is a bounded cost risk; refusing the review turns
        a transient database lock into a blocked push."""
        developer_config()
        monkeypatch.setattr(
            db, "code_review_calls_get",
            lambda conn, task_id: (_ for _ in ()).throw(RuntimeError("database is locked")),
        )
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 0
        assert envelope["status"] == "ok"

    def test_an_unrecordable_charge_does_not_lose_a_paid_for_review(
        self, capsys, monkeypatch, worktree, review_env, developer_config, stub_brain
    ):
        """The model calls are already paid for by the time the counter is
        written. A traceback here would violate the facade contract and hand the
        caller nothing at all for the money."""
        developer_config()
        stub_brain.replies["reviewer"] = [findings_json(finding())]
        monkeypatch.setattr(
            db, "code_review_calls_increment",
            lambda conn, task_id, count=1: (_ for _ in ()).throw(
                RuntimeError("disk I/O error")
            ),
        )
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 0
        assert envelope["status"] == "ok"
        assert len(envelope["findings"]) == 1


# --------------------------------------------------------------------------
# The timeout budget
# --------------------------------------------------------------------------


MIN = code_review.MIN_AGENT_TIMEOUT_SECONDS


class TestTimeoutBudget:
    def test_the_reviewer_gets_the_configured_timeout(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        developer_config(timeout_seconds=45)
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert stub_brain.timeouts == [45]

    def test_a_budget_over_the_proxy_ceiling_is_clamped_and_warned_about(
        self, capsys, caplog, worktree, review_env, developer_config, stub_brain
    ):
        """The proxy kills the command at `security.skill_proxy_timeout`. Warning
        and then handing each agent the full budget anyway describes the problem
        without avoiding it: every review would be killed half-finished having
        paid for its agents. Shrinking is the only outcome that returns
        anything."""
        cfg = developer_config(timeout_seconds=400)
        cfg.security.skill_proxy_timeouts = {"code_review": 300}
        with caplog.at_level("WARNING"):
            drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert any("skill_proxy_timeout" in r.message for r in caplog.records)
        assert stub_brain.timeouts == [300 - code_review.RESERVED_SECONDS]

    def test_a_raised_client_wait_raises_the_review_ceiling_too(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """ISSUE-450: the review's ceiling and the proxy's have to be the same
        answer, so the clamp here reads the same `skill_client_wait_seconds`
        the proxy derives its cap from. A per-skill entry past the old fixed
        570 must reach the agents once the deployment raised the wait."""
        cfg = developer_config(timeout_seconds=900)
        cfg.security.skill_client_wait_seconds = 1200
        cfg.security.skill_proxy_timeouts = {"code_review": 840}
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert stub_brain.timeouts == [840 - code_review.RESERVED_SECONDS]

    def test_the_ceiling_warning_fires_even_when_the_run_is_capped(
        self, capsys, caplog, worktree, review_env, developer_config,
        stub_brain, review_db,
    ):
        """A warning that only fires on the runs that were going to work anyway
        is not much of a warning."""
        cfg = developer_config(timeout_seconds=400, max_calls_per_task=1)
        cfg.security.skill_proxy_timeouts = {"code_review": 300}
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        caplog.clear()
        with caplog.at_level("WARNING"):
            _, envelope = drive(
                capsys, "run", "--worktree", str(worktree), "--base", "main"
            )
        assert envelope["reason"] == "call_cap"
        assert any("skill_proxy_timeout" in r.message for r in caplog.records)

    def test_the_envelope_reports_the_budget_each_agent_actually_got(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """A caller reporting a review has to be able to say what it ran on.
        Unclamped, the effective budget is the configured one and `clamped` is
        false — the field is present on every run, not only on the short ones,
        because a reader who has to infer "not clamped" from a missing key is
        back to guessing."""
        developer_config(timeout_seconds=45)
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
        )
        assert envelope["agent_timeout_seconds"] == 45
        assert envelope["agent_timeout_configured"] == 45
        assert envelope["agent_timeout_clamped"] is False

    def test_a_clamped_budget_says_so_in_the_envelope(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """The clamp warns into the daemon journal, which the model that invoked
        the CLI cannot read. Without this the only difference between a review
        that had its whole budget and one cut to a third of it is in a log the
        caller has no route to — same shape, same `status: ok`, quietly less
        thinking behind the findings."""
        cfg = developer_config(timeout_seconds=400)
        cfg.security.skill_proxy_timeouts = {"code_review": 300}
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
        )
        effective = 300 - code_review.RESERVED_SECONDS
        assert envelope["agent_timeout_seconds"] == effective
        assert envelope["agent_timeout_configured"] == 400
        assert envelope["agent_timeout_clamped"] is True
        assert stub_brain.timeouts == [effective]

    def test_an_empty_range_still_carries_the_budget_fields(
        self, capsys, empty_worktree, review_env, developer_config, stub_brain
    ):
        """`run_review` promises every return path the same key set, and the
        empty-range path returns before any reviewer is sized. A consumer that
        reads the budget without first branching on `empty` must not hit a
        KeyError."""
        developer_config(timeout_seconds=45)
        _, envelope = drive(
            capsys, "run", "--worktree", str(empty_worktree), "--base", "main",
        )
        assert envelope["empty"] is True
        assert envelope["agent_timeout_seconds"] == 45
        assert envelope["agent_timeout_clamped"] is False

    def test_a_clamp_that_changes_nothing_does_not_claim_a_short_review(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """A budget already at the floor trips the ceiling arithmetic without
        losing a second. `clamped` answers "did this review run short", not "was
        the branch taken", so it stays false."""
        cfg = developer_config(timeout_seconds=MIN)
        cfg.security.skill_proxy_timeouts = {"code_review": MIN + 50}
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
        )
        assert envelope["agent_timeout_seconds"] == MIN
        assert envelope["agent_timeout_configured"] == MIN
        assert envelope["agent_timeout_clamped"] is False
        assert stub_brain.timeouts == [MIN]

    def test_the_clamp_never_raises_a_budget_that_already_fit(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """`max(floor, ceiling - allowance)` on its own turned a configured 25s
        into 30s — a "clamp" that made the fit worse, under a ceiling the
        original 25s already fit. The floor may still raise the budget, but the
        ceiling arithmetic must only ever lower it, and a budget that came out
        above the configured one is not a short review."""
        cfg = developer_config(timeout_seconds=MIN - 5)
        cfg.security.skill_proxy_timeouts = {"code_review": MIN + 50}
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
        )
        assert envelope["agent_timeout_seconds"] == MIN - 5
        assert envelope["agent_timeout_configured"] == MIN - 5
        assert envelope["agent_timeout_clamped"] is False
        assert stub_brain.timeouts == [MIN - 5]

    def test_a_nonpositive_budget_is_floored_rather_than_passed_to_the_brain(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """Nothing in the config loader floors `timeout_seconds`, and the brains
        disagree about what a 0 means: the native one runs unbounded until the
        proxy kills the command, `claude_code` hands it to a `threading.Timer`
        and kills each agent at once. Neither is a review, and before this the
        envelope reported the deployment had got exactly what it asked for."""
        developer_config(timeout_seconds=0)
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
        )
        assert stub_brain.timeouts == [MIN]
        assert envelope["agent_timeout_seconds"] == MIN
        assert envelope["agent_timeout_configured"] == 0
        assert envelope["agent_timeout_clamped"] is False

    def test_a_ceiling_too_tight_for_the_floor_says_so_on_its_own_line(
        self, capsys, caplog, worktree, review_env, developer_config, stub_brain
    ):
        """The clamp cannot deliver a fit under a ceiling smaller than the
        assembly allowance plus the floor, so the proxy kills the command with
        empty stdout and the caller gets no envelope at all. The log is the only
        place that deployment can say what happened, so it gets its own line
        rather than the ordinary "being given less" warning."""
        cfg = developer_config(timeout_seconds=120)
        cfg.security.skill_proxy_timeouts = {
            "code_review": code_review.ASSEMBLY_ALLOWANCE_SECONDS
        }
        with caplog.at_level("WARNING"):
            drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert any("cannot fit a review at all" in r.message for r in caplog.records)
        assert stub_brain.timeouts == [MIN]

    def test_a_nonpositive_proxy_ceiling_does_not_blame_a_clamp(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """A negative ceiling is truthy. Read as a real ceiling it pinned every
        review to the floor and reported a clamp whose stated cause never
        happened; the proxy surfaces the misconfiguration itself by killing the
        command immediately."""
        cfg = developer_config(timeout_seconds=45)
        cfg.security.skill_proxy_timeout = -1
        cfg.security.skill_proxy_timeouts = {}
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
        )
        assert envelope["agent_timeout_seconds"] == 45
        assert envelope["agent_timeout_clamped"] is False


class TestTheShippedDefaultsFitAReviewer:
    """ISSUE-448: 240s was the largest per-agent budget the shipped pair allowed,
    and bughunt — a `smart:high` reviewer, sized onto the *large* diffs only —
    died at exactly that number on every real diff, twice out of two.

    The arithmetic that produced 240 had two independent faults. The ceiling was
    `security.skill_proxy_timeout`, one global applied to every proxied skill
    call, so the only lever was a limit on everything else too. And the reserve
    subtracted from it was a 60-second guess against about one second of measured
    assembly, while the 10 seconds of join slack it did not model pushed the real
    wall bound past the ceiling anyway.
    """

    def test_a_default_deployment_gives_a_reviewer_its_whole_configured_budget(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """The headline regression. Nothing here overrides anything: this is what
        an operator who sets nothing gets, and before the fix it was 240."""
        developer_config()
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
        )
        configured = ReviewConfig().timeout_seconds
        assert configured > 240, (
            "the code default must itself be more than the budget bughunt was "
            "measured dying at, or a bare install reproduces the bug"
        )
        assert envelope["agent_timeout_seconds"] == configured
        assert envelope["agent_timeout_clamped"] is False
        assert stub_brain.timeouts == [configured]

    def test_the_ceiling_that_binds_a_review_is_the_review_s_own(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """`security.skill_proxy_timeout` is one number for every proxied skill,
        and `code_review` is the only one that drives model calls. A per-skill
        entry is what lets the review have minutes without handing them to
        everything else — so the global must not be what bounds it."""
        cfg = developer_config(timeout_seconds=400)
        cfg.security.skill_proxy_timeout = 300
        cfg.security.skill_proxy_timeouts = {"code_review": 540}
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
        )
        assert envelope["agent_timeout_seconds"] == 400
        assert envelope["agent_timeout_clamped"] is False

    def test_a_table_naming_another_skill_leaves_the_review_ceiling_alone(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """A `dict` config field replaces its default rather than merging, so a
        shipped `code_review` entry would be dropped by an operator who wrote
        the table to configure something else — taking the ceiling back to the
        global and reproducing ISSUE-448 with only a log line. The shipped
        policy lives in `skill_proxy.DEFAULT_SKILL_TIMEOUTS` for that reason,
        and this is the case that would go red if it moved back."""
        cfg = developer_config()
        cfg.security.skill_proxy_timeout = 300
        cfg.security.skill_proxy_timeouts = {"browse": 90}
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
        )
        assert envelope["agent_timeout_seconds"] == ReviewConfig().timeout_seconds
        assert envelope["agent_timeout_clamped"] is False

    def test_the_clamp_reserves_the_join_slack_it_actually_spends(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """The command's wall bound is the reviewer's budget plus the slack for a
        brain that overruns it plus assembly. Reserving only the assembly
        allowance meant a budget clamped to "just fit" still overran the
        ceiling by the slack (ISSUE-448)."""
        cfg = developer_config(timeout_seconds=1000)
        cfg.security.skill_proxy_timeouts = {"code_review": 300}
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
        )
        effective = envelope["agent_timeout_seconds"]
        assert effective == 300 - code_review.RESERVED_SECONDS
        assert effective + code_review.JOIN_SLACK_SECONDS \
            + code_review.ASSEMBLY_ALLOWANCE_SECONDS <= 300

    def test_an_explicit_timeout_overrides_the_configured_budget(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """The escape hatch. Config comes from a deploy, so before this there was
        no way to ask what a reviewer needs on a real diff without one — and
        every attempt cost a `smart:high` call that was then thrown away."""
        developer_config(timeout_seconds=480)
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--timeout", "77",
        )
        assert envelope["agent_timeout_override"] == 77
        assert envelope["agent_timeout_seconds"] == 77
        assert stub_brain.timeouts == [77]
        # The deployment's own number stays reported. The flag reaches this CLI
        # from the model's argv and shortens as readily as it lengthens, so a
        # `--timeout 30` that overwrote `agent_timeout_configured` would leave
        # nothing in the envelope saying 480 was asked for.
        assert envelope["agent_timeout_configured"] == 480

    def test_a_run_with_no_flag_reports_no_override(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """`None` rather than a repeat of the configured value, so a caller can
        tell "not overridden" from "overridden to the same number"."""
        developer_config(timeout_seconds=480)
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
        )
        assert envelope["agent_timeout_override"] is None
        assert envelope["agent_timeout_configured"] == 480

    def test_an_explicit_timeout_is_still_bounded_by_the_ceiling(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """A flag that could outrun the proxy would be a way to guarantee the
        failure this issue is about rather than a way to measure it."""
        cfg = developer_config()
        cfg.security.skill_proxy_timeouts = {"code_review": 300}
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--timeout", "9000",
        )
        assert envelope["agent_timeout_override"] == 9000
        assert envelope["agent_timeout_seconds"] == 300 - code_review.RESERVED_SECONDS
        assert envelope["agent_timeout_clamped"] is True

    def test_the_envelope_reports_what_everything_outside_the_agents_cost(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """The allowance is a constant and the entry's whole complaint is that
        constants here are not measuring the thing they gate. Reporting the
        measurement is what lets the next reader check 20 against a real diff
        instead of picking another number blind.

        The delay is the discriminating half: a figure that included the agent
        phase would be at least as large as it, so an overhead below it can only
        have come from excluding it.
        """
        developer_config()
        stub_brain.delay = 3.0
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
        )
        overhead = envelope["overhead_seconds"]
        assert isinstance(overhead, (int, float))
        # Non-zero because assembly shells out to git several times, so a flat
        # 0.0 would mean the clock is not running rather than that the work is
        # free — and under the delay because that is the only way it can have
        # excluded the agent phase. Three seconds rather than one: the suite
        # runs `-n auto` and is throughput-bound, so a handful of git spawns
        # under contention can cross a one-second bound and turn the
        # discriminating half of this assertion into a flake.
        assert 0 < overhead < 3.0

    def test_the_reviewer_calls_stream(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """On the non-streaming path a timeout is a `TimeoutExpired` out of
        `subprocess.run`, and `_execute_simple_once` reads its usage from an
        accounting dict only filled after the process exits and its output
        parses. So the most expensive call the deployment makes was also the one
        it never billed, and `persist_brain_usage` returns immediately on a
        `None` usage. The streaming path stamps usage at a single exit from the
        per-request frames it has already parsed, and hands back `partial_text`."""
        developer_config()
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert stub_brain.streaming == [True]

    def test_what_a_timed_out_reviewer_wrote_reaches_the_log(
        self, capsys, caplog, worktree, review_env, developer_config, stub_brain
    ):
        """The half of the streaming switch that has an observable outcome here.
        `stub_brain.streaming` pins the argument; this pins what the argument is
        for — a `partial_text` on the result is now read and kept, where before
        the field was ignored by this caller because the non-streaming path
        never populated it.

        Not in the envelope: a reviewer answers in one JSON blob at the end, so
        a timed-out one has written prose, and prose in a findings-adjacent
        field is a diagnostic wearing the wrong label."""
        developer_config()
        stub_brain.replies = {
            "reviewer": [StubResult(
                success=False,
                result_text="Claude Code timed out after 480s",
                stop_reason="timeout",
                partial_text="I was partway through checking the migration when",
            )],
        }
        with caplog.at_level("INFO"):
            _, envelope = drive(
                capsys, "run", "--worktree", str(worktree), "--base", "main",
            )
        assert envelope["status"] == "skipped"
        assert any(
            "checking the migration" in r.getMessage() for r in caplog.records
        )

    def test_an_empty_range_carries_the_new_fields_too(
        self, capsys, empty_worktree, review_env, developer_config, stub_brain
    ):
        """`run_review` promises every return path the same key set, and the
        empty-range path returns before any reviewer is sized — so it is the one
        where `agent_seconds` is still zero when the overhead is stamped, and
        the one a consumer reading either field without branching on `empty`
        would hit a KeyError on."""
        developer_config()
        _, envelope = drive(
            capsys, "run", "--worktree", str(empty_worktree), "--base", "main",
        )
        assert envelope["empty"] is True
        assert envelope["ruled_out"] == []
        assert envelope["snapshot"] is None
        assert isinstance(envelope["overhead_seconds"], (int, float))
        assert envelope["overhead_seconds"] > 0


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
