"""Tests for the code_review engine — everything the review does without a model.

The engine assembles a reviewer's whole view of a change from a worktree the
sandboxed model named. Two properties are load-bearing and neither is obvious
from reading the happy path:

**The git invocations are hardened, and the tests prove the attack first.**
`DEVELOPER_REPOS_DIR` is bound read-write into the admin sandbox, so a worktree
that passes `resolve_under_repos` cleanly can still carry a repository whose
*configuration* the model wrote. Three escapes were demonstrated against such a
path: a repo-local `diff.external` runs a command as the daemon user (the user
holding the forge tokens), a plain directory sends git searching upward past the
root, and a `.git` file containing `gitdir:` redirects the repository out of the
root while `rev-parse --show-toplevel` still reports the contained path. Each
regression test here builds the attack, asserts it is live against a plain git
invocation, and only then asserts the engine refuses it. A hardening test that
never demonstrates the hole passes just as happily against no hardening at all.

**Content comes out of the object store, never off the filesystem.** A symlink
planted in a worktree makes `(worktree / path).read_text()` read straight out of
the root with no race needed, and git lists such a path in `--name-only` quite
happily. `git show <rev>:<path>` returns the link *text*, so the class does not
arise — pinned by `test_a_symlink_yields_its_target_text_not_the_file`.

Fixtures shell out to real git, because the hardening under test is git's own
behaviour and a hand-built `.git` directory would not exercise it. They pin
`GIT_CONFIG_GLOBAL` and `GIT_CONFIG_NOSYSTEM` so the developer's own git
configuration cannot change what a fixture repository does.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from istota.skills.code_review.engine import (
    Caps,
    Finding,
    ReviewConfig,
    ReviewError,
    assemble_context,
    build_prompt,
    changed_symbols,
    collect_callers,
    collect_conventions,
    collect_diff,
    collect_file_bodies,
    git_dir,
    merge_findings,
    parse_findings,
    resolve_range,
    size_review,
)

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


@pytest.fixture
def repos_root(tmp_path, monkeypatch) -> Path:
    """A `DEVELOPER_REPOS_DIR` with nothing in it yet."""
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(root))
    return root.resolve()


@pytest.fixture
def repo(repos_root) -> Path:
    """A repository on `main` with one base commit, inside the repos root."""
    wt = repos_root / "proj"
    wt.mkdir()
    run_git(wt, "init", "-q", "-b", "main", ".")
    (wt / "AGENTS.md").write_text("# Project rules\n\nSpaces, never tabs.\n")
    (wt / "app.py").write_text("def existing():\n    return 1\n")
    (wt / "caller.py").write_text("from app import existing\n\nprint(existing())\n")
    commit(wt, "base")
    return wt


def branch_with_change(repo: Path, *, name: str = "feature") -> Path:
    """A `feature` branch adding one Python function. HEAD ends on it."""
    run_git(repo, "checkout", "-q", "-b", name)
    (repo / "app.py").write_text(
        "def existing():\n    return 1\n\n\ndef added_helper(value):\n    return value * 2\n"
    )
    commit(repo, "app: add a helper")
    return repo


class TestResolveRange:
    def test_an_explicit_range_wins_over_a_base(self, repo):
        branch_with_change(repo)
        assert resolve_range(repo, base="main", explicit="HEAD~1..HEAD") == "HEAD~1..HEAD"

    def test_a_base_produces_the_three_dot_form(self, repo):
        branch_with_change(repo)
        assert resolve_range(repo, base="main") == "main...HEAD"

    def test_neither_falls_back_to_the_tracked_default_branch(self, repo):
        branch_with_change(repo)
        assert resolve_range(repo) == "main...HEAD"

    def test_a_bad_ref_raises_with_the_git_stderr_attached(self, repo):
        branch_with_change(repo)
        with pytest.raises(ReviewError) as excinfo:
            resolve_range(repo, base="no-such-ref")
        assert "no-such-ref" in str(excinfo.value)
        assert excinfo.value.reason == "bad_range"

    def test_a_base_only_commit_is_not_attributed_to_the_branch(self, repo):
        """The regression that motivated the three-dot form.

        Two-dot `main..HEAD` means `git diff main HEAD`, so the moment `main`
        moves ahead of the branch point every base-only commit shows up
        inverted — as a deletion the branch never made. Reviewers then file
        findings about code that is not in the change, which costs the driving
        model a round of chasing them.
        """
        branch_with_change(repo)
        run_git(repo, "checkout", "-q", "main")
        (repo / "base_only.py").write_text("BASE_ONLY_SENTINEL = 1\n")
        commit(repo, "base: unrelated work")
        run_git(repo, "checkout", "-q", "feature")

        bundle = collect_diff(repo, resolve_range(repo, base="main"), 200_000)

        assert "BASE_ONLY_SENTINEL" not in bundle.body
        assert "base_only.py" not in bundle.files
        assert "app.py" in bundle.files


class TestGitHardening:
    """The three escapes demonstrated against a cleanly contained worktree."""

    def test_a_repo_local_diff_external_does_not_execute(self, repo, tmp_path):
        branch_with_change(repo)
        sentinel = tmp_path / "sentinel"
        script = tmp_path / "ext.sh"
        script.write_text(f"#!/bin/sh\necho pwned > {sentinel}\necho 'fake diff'\n")
        script.chmod(0o755)
        run_git(repo, "config", "diff.external", str(script))

        # Positive control: the attack is live. Without this the hardening
        # assertion below would pass against an engine that hardens nothing.
        subprocess.run(
            ["git", "diff", "main...HEAD"],
            cwd=str(repo),
            capture_output=True,
            env={**os.environ, **GIT_ISOLATION},
        )
        assert sentinel.exists(), "fixture is wrong: diff.external never fired"
        sentinel.unlink()

        bundle = collect_diff(repo, "main...HEAD", 200_000)

        assert not sentinel.exists()
        assert "fake diff" not in bundle.body
        assert "added_helper" in bundle.body

    def test_a_plain_directory_does_not_pick_up_a_repository_above_the_root(
        self, tmp_path, monkeypatch
    ):
        outer = tmp_path / "outer"
        root = outer / "repos"
        plain = root / "plain"
        plain.mkdir(parents=True)
        run_git(outer, "init", "-q", "-b", "main", ".")
        (outer / "outer_secret.py").write_text("OUTER_SENTINEL = 1\n")
        commit(outer, "outer")
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(root))

        # Positive control: git really does search upward out of the root.
        found = subprocess.run(
            ["git", "rev-parse", "--absolute-git-dir"],
            cwd=str(plain),
            capture_output=True,
            text=True,
            env={**os.environ, **GIT_ISOLATION},
        )
        assert found.returncode == 0
        assert str(outer.resolve()) in found.stdout

        with pytest.raises(ReviewError) as excinfo:
            git_dir(plain)
        assert excinfo.value.reason == "not_a_repository"

    def test_a_gitdir_redirect_out_of_the_root_is_refused(self, repos_root, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        run_git(outside, "init", "-q", "-b", "main", ".")
        (outside / "outside_secret.py").write_text("OUTSIDE_SENTINEL = 1\n")
        commit(outside, "outside")

        wt = repos_root / "redirected"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {outside / '.git'}\n")

        # Positive control: the obvious hardening does not catch this. The
        # worktree still reports itself as the contained path, so a check
        # built on --show-toplevel would approve it.
        toplevel = run_git(wt, "rev-parse", "--show-toplevel").strip()
        assert Path(toplevel).resolve() == wt.resolve()

        with pytest.raises(ReviewError) as excinfo:
            git_dir(wt)
        assert excinfo.value.reason == "git_dir_not_allowed"

    def test_a_legitimate_worktree_resolves_its_git_dir(self, repo):
        assert git_dir(repo) == (repo / ".git").resolve()

    def test_a_linked_worktree_inside_the_root_is_accepted(self, repo, repos_root):
        """`git worktree add` puts the git dir under the main repo, not the tree."""
        branch_with_change(repo)
        linked = repos_root / "proj-wt"
        run_git(repo, "worktree", "add", "-q", str(linked), "main")

        resolved = git_dir(linked)

        assert str(resolved).startswith(str((repo / ".git").resolve()))


class TestCollectDiff:
    def test_it_reports_files_and_changed_lines(self, repo):
        branch_with_change(repo)
        bundle = collect_diff(repo, "main...HEAD", 200_000)

        assert bundle.files == ["app.py"]
        assert bundle.lines == 4  # the two-line function and the two blanks before it
        assert bundle.truncated is False
        assert "def added_helper" in bundle.body
        assert "app.py" in bundle.stat

    def test_a_deleted_file_is_listed_separately(self, repo):
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "caller.py").unlink()
        commit(repo, "drop the caller")

        bundle = collect_diff(repo, "main...HEAD", 200_000)

        assert bundle.deleted == ["caller.py"]
        assert "caller.py" in bundle.files

    def test_binary_files_are_named_but_not_inlined(self, repo):
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "blob.bin").write_bytes(bytes(range(256)) * 8)
        commit(repo, "add a binary")

        bundle = collect_diff(repo, "main...HEAD", 200_000)

        assert bundle.binary == ["blob.bin"]
        assert "blob.bin" in bundle.stat
        assert "Binary files" not in bundle.body

    def test_over_the_cap_every_file_keeps_its_stat_line_and_some_body(self, repo):
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "big.py").write_text("".join(f"BIG_{n} = {n}\n" for n in range(4000)))
        (repo / "small.py").write_text("SMALL_SENTINEL = 1\n")
        commit(repo, "one big file and one small one")

        bundle = collect_diff(repo, "main...HEAD", 4000)

        assert bundle.truncated is True
        assert "big.py" in bundle.stat and "small.py" in bundle.stat
        assert "big.py" in bundle.truncated_files
        # The small file must not be starved by the big one.
        assert "SMALL_SENTINEL" in bundle.body
        assert "small.py" not in bundle.truncated_files
        assert len(bundle.body) <= 4000

    def test_an_empty_range_is_an_empty_bundle_not_an_error(self, repo):
        bundle = collect_diff(repo, "main...HEAD", 200_000)

        assert bundle.files == []
        assert bundle.body == ""
        assert bundle.lines == 0

    def test_a_bad_range_raises_with_the_git_stderr(self, repo):
        with pytest.raises(ReviewError) as excinfo:
            collect_diff(repo, "nope...HEAD", 200_000)
        assert excinfo.value.reason == "bad_range"


class TestCollectFileBodies:
    def test_a_changed_file_arrives_whole(self, repo):
        branch_with_change(repo)
        bundle = collect_diff(repo, "main...HEAD", 200_000)

        bodies = collect_file_bodies(repo, bundle, max_file_chars=20_000, max_total_chars=60_000)

        assert "def existing():" in bodies  # context the hunk alone would hide
        assert "def added_helper" in bodies
        assert "app.py" in bodies

    def test_a_deleted_file_is_skipped(self, repo):
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "caller.py").unlink()
        commit(repo, "drop the caller")
        bundle = collect_diff(repo, "main...HEAD", 200_000)

        bodies = collect_file_bodies(repo, bundle, max_file_chars=20_000, max_total_chars=60_000)

        assert "caller.py" not in bodies

    def test_a_file_over_the_per_file_cap_falls_back_to_its_hunks(self, repo):
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "big.py").write_text(
            "HEAD_SENTINEL = 0\n"
            + "".join(f"BIG_{n} = {n}\n" for n in range(2000))
            + "TAIL_SENTINEL = 1\n"
        )
        commit(repo, "big file")
        bundle = collect_diff(repo, "main...HEAD", 200_000)

        bodies = collect_file_bodies(repo, bundle, max_file_chars=200, max_total_chars=60_000)

        assert "big.py" in bodies
        assert "hunks only" in bodies
        assert len(bodies) < 20_000

    def test_a_symlink_yields_its_target_text_not_the_file(self, repo, tmp_path):
        """Bodies come from the object store, so a planted link reads as text."""
        outside = tmp_path / "outside_secret.txt"
        outside.write_text("OUTSIDE_SENTINEL\n")
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "link.txt").symlink_to(outside)
        commit(repo, "plant a link")
        bundle = collect_diff(repo, "main...HEAD", 200_000)

        bodies = collect_file_bodies(repo, bundle, max_file_chars=20_000, max_total_chars=60_000)

        assert "OUTSIDE_SENTINEL" not in bodies
        assert str(outside) in bodies  # the link text itself, which is harmless

    def test_the_total_cap_stops_the_gather(self, repo):
        run_git(repo, "checkout", "-q", "-b", "feature")
        for n in range(6):
            (repo / f"mod{n}.py").write_text(f"VALUE_{n} = {n}\n" * 40)
        commit(repo, "six modules")
        bundle = collect_diff(repo, "main...HEAD", 200_000)

        bodies = collect_file_bodies(repo, bundle, max_file_chars=20_000, max_total_chars=1200)

        assert len(bodies) <= 1400  # the cap, plus the closing notice
        assert "VALUE_0" in bodies


class TestChangedSymbols:
    def test_it_finds_python_definitions(self):
        diff = (
            "+++ b/app.py\n"
            "+def added_helper(value):\n"
            "+async def fetch_thing(url):\n"
            "+class Widget:\n"
            "+    return 1\n"
        )
        assert changed_symbols(diff) == ["added_helper", "fetch_thing", "Widget"]

    def test_it_finds_typescript_exports(self):
        diff = (
            "+++ b/web/src/lib/api.ts\n"
            "+export function loadRooms(fetch: typeof globalThis.fetch) {\n"
            "+export const ROOM_LIMIT = 50;\n"
        )
        assert changed_symbols(diff) == ["loadRooms", "ROOM_LIMIT"]

    def test_a_deleted_definition_is_not_reported_as_changed(self):
        diff = "+++ b/app.py\n-def removed_helper(value):\n+    return 1\n"
        assert changed_symbols(diff) == []

    def test_the_diff_header_is_not_mistaken_for_an_addition(self):
        diff = "+++ b/def_something.py\n@@ -1 +1 @@\n+VALUE = 1\n"
        assert changed_symbols(diff) == []

    def test_a_symbol_defined_twice_appears_once(self):
        diff = "+++ b/a.py\n+def dup(x):\n+++ b/b.py\n+def dup(y):\n"
        assert changed_symbols(diff) == ["dup"]


class TestCollectCallers:
    def test_it_finds_a_caller_outside_the_diff(self, repo):
        branch_with_change(repo)
        head = run_git(repo, "rev-parse", "HEAD").strip()

        out = collect_callers(repo, ["existing"], Caps(per_symbol=8, total_chars=10_000), head)

        assert "caller.py" in out
        assert "existing" in out

    def test_a_symbol_with_no_callers_contributes_no_header(self, repo):
        branch_with_change(repo)
        head = run_git(repo, "rev-parse", "HEAD").strip()

        out = collect_callers(
            repo, ["nowhere_at_all"], Caps(per_symbol=8, total_chars=10_000), head
        )

        assert out == ""
        assert "nowhere_at_all" not in out

    def test_the_per_symbol_cap_holds(self, repo):
        run_git(repo, "checkout", "-q", "-b", "feature")
        for n in range(10):
            (repo / f"site{n}.py").write_text("existing()\n")
        commit(repo, "many callers")
        head = run_git(repo, "rev-parse", "HEAD").strip()

        out = collect_callers(repo, ["existing"], Caps(per_symbol=3, total_chars=10_000), head)

        assert len([ln for ln in out.splitlines() if ln.startswith("site")]) <= 3

    def test_the_total_cap_holds(self, repo):
        run_git(repo, "checkout", "-q", "-b", "feature")
        for n in range(10):
            (repo / f"site{n}.py").write_text("existing()\n")
        commit(repo, "many callers")
        head = run_git(repo, "rev-parse", "HEAD").strip()

        out = collect_callers(repo, ["existing"], Caps(per_symbol=50, total_chars=200), head)

        assert len(out) <= 200


class TestCollectConventions:
    def test_root_agents_and_claude_files_are_included(self, repo):
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "CLAUDE.md").write_text("See AGENTS.md.\n")
        (repo / "app.py").write_text("def existing():\n    return 2\n")
        commit(repo, "add CLAUDE.md")
        head = run_git(repo, "rev-parse", "HEAD").strip()

        out = collect_conventions(repo, head, ["app.py"], 60_000)

        assert "Spaces, never tabs." in out
        assert "See AGENTS.md." in out

    def test_a_rules_file_is_included_only_when_its_paths_match(self, repo):
        run_git(repo, "checkout", "-q", "-b", "feature")
        rules = repo / ".claude" / "rules"
        rules.mkdir(parents=True)
        (rules / "brain.md").write_text(
            '---\npaths:\n  - "src/brain/**"\n---\nBRAIN_RULE_SENTINEL\n'
        )
        (rules / "money.md").write_text(
            '---\npaths:\n  - "src/money/**"\n---\nMONEY_RULE_SENTINEL\n'
        )
        commit(repo, "add rules")
        head = run_git(repo, "rev-parse", "HEAD").strip()

        out = collect_conventions(repo, head, ["src/brain/native.py"], 60_000)

        assert "BRAIN_RULE_SENTINEL" in out
        assert "MONEY_RULE_SENTINEL" not in out

    def test_an_absent_rules_directory_is_silent(self, repo):
        branch_with_change(repo)
        head = run_git(repo, "rev-parse", "HEAD").strip()

        out = collect_conventions(repo, head, ["app.py"], 60_000)

        assert ".claude/rules" not in out
        assert "Spaces, never tabs." in out

    def test_the_cap_holds(self, repo):
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "AGENTS.md").write_text("padding\n" * 5000)
        commit(repo, "huge conventions")
        head = run_git(repo, "rev-parse", "HEAD").strip()

        out = collect_conventions(repo, head, ["AGENTS.md"], 500)

        assert len(out) <= 700


class TestSizeReview:
    def _bundle(self, monkeypatch, *, lines: int, files: list[str]):
        from istota.skills.code_review.engine import DiffBundle

        return DiffBundle(
            rng="main...HEAD",
            head="deadbeef",
            stat="",
            body="",
            files=files,
            deleted=[],
            binary=[],
            lines=lines,
            truncated=False,
            truncated_files=[],
        )

    def test_a_small_ordinary_diff_gets_conformance_alone(self, monkeypatch):
        bundle = self._bundle(monkeypatch, lines=20, files=["app.py"])
        agents, reason = size_review(bundle, ReviewConfig(), None)
        assert agents == ["conformance"]
        assert "threshold" in reason

    def test_over_the_line_threshold_gets_both(self, monkeypatch):
        bundle = self._bundle(monkeypatch, lines=400, files=["app.py"])
        agents, reason = size_review(bundle, ReviewConfig(both_agents_threshold_lines=150), None)
        assert agents == ["conformance", "bughunt"]
        assert "400" in reason and "150" in reason

    def test_a_boundary_path_in_a_tiny_diff_gets_both(self, monkeypatch):
        bundle = self._bundle(monkeypatch, lines=3, files=["src/auth/session.py"])
        agents, reason = size_review(bundle, ReviewConfig(), None)
        assert agents == ["conformance", "bughunt"]
        assert "auth" in reason
        assert "src/auth/session.py" in reason

    def test_boundary_matching_is_case_insensitive(self, monkeypatch):
        bundle = self._bundle(monkeypatch, lines=3, files=["db/Migration_007.sql"])
        agents, _ = size_review(bundle, ReviewConfig(), None)
        assert agents == ["conformance", "bughunt"]

    def test_forcing_both_overrides_the_sizing(self, monkeypatch):
        bundle = self._bundle(monkeypatch, lines=3, files=["app.py"])
        agents, reason = size_review(bundle, ReviewConfig(), "both")
        assert agents == ["conformance", "bughunt"]
        assert "requested" in reason

    def test_forcing_conformance_overrides_a_boundary_path(self, monkeypatch):
        bundle = self._bundle(monkeypatch, lines=900, files=["src/auth/session.py"])
        agents, reason = size_review(bundle, ReviewConfig(), "conformance")
        assert agents == ["conformance"]
        assert "requested" in reason


class TestBuildPrompt:
    def _bundle(self):
        from istota.skills.code_review.engine import DiffBundle

        return DiffBundle(
            rng="main...HEAD",
            head="deadbeef",
            stat=" app.py | 2 +-\n",
            body="+def added_helper(value):\n",
            files=["app.py"],
            deleted=[],
            binary=[],
            lines=2,
            truncated=False,
            truncated_files=[],
        )

    def test_it_carries_the_intent_the_diff_and_the_context(self):
        prompt = build_prompt("conformance", self._bundle(), "CONTEXT_SENTINEL", "fix the header")

        assert "fix the header" in prompt
        assert "added_helper" in prompt
        assert "CONTEXT_SENTINEL" in prompt
        assert "main...HEAD" in prompt

    def test_it_states_that_the_reviewer_has_no_tools(self):
        prompt = build_prompt("conformance", self._bundle(), "", "x")

        assert "no tools" in prompt.lower()
        assert "unverified" in prompt

    def test_the_two_agents_ask_for_different_things(self):
        bundle = self._bundle()
        conformance = build_prompt("conformance", bundle, "", "x")
        bughunt = build_prompt("bughunt", bundle, "", "x")

        assert conformance != bughunt
        assert "conformance" in conformance.lower()
        assert "off-by-one" in bughunt.lower()

    def test_a_truncated_diff_says_so_and_names_the_files(self):
        bundle = self._bundle()
        bundle.truncated = True
        bundle.truncated_files = ["big.py"]

        prompt = build_prompt("conformance", bundle, "", "x")

        assert "truncated" in prompt.lower()
        assert "big.py" in prompt

    def test_an_unknown_agent_is_refused(self):
        with pytest.raises(ReviewError):
            build_prompt("skinner", self._bundle(), "", "x")


class TestParseFindings:
    PAYLOAD = {
        "findings": [
            {
                "severity": "must-fix",
                "file": "app.py",
                "line": 12,
                "claim": "Null deref",
                "evidence": "value may be None",
                "action": "guard it",
            }
        ]
    }

    def test_bare_json_parses(self):
        found = parse_findings(json.dumps(self.PAYLOAD), "conformance")
        assert len(found) == 1
        assert found[0].file == "app.py"
        assert found[0].line == 12
        assert found[0].sources == ["conformance"]

    def test_fenced_json_parses(self):
        raw = "```json\n" + json.dumps(self.PAYLOAD) + "\n```"
        assert len(parse_findings(raw, "bughunt")) == 1

    def test_leading_prose_is_tolerated(self):
        raw = "Here is what I found.\n\n" + json.dumps(self.PAYLOAD)
        assert len(parse_findings(raw, "conformance")) == 1

    def test_a_bare_list_of_findings_parses(self):
        assert len(parse_findings(json.dumps(self.PAYLOAD["findings"]), "conformance")) == 1

    def test_malformed_output_returns_nothing_so_the_caller_can_retry(self):
        assert parse_findings("I could not review this, sorry.", "conformance") == []
        assert parse_findings("", "conformance") == []
        assert parse_findings("{ not json at all", "conformance") == []

    def test_severity_is_normalised_and_an_unknown_one_is_kept_as_medium(self):
        raw = json.dumps(
            {
                "findings": [
                    {"severity": "MUST_FIX", "file": "a.py", "line": 1, "claim": "x"},
                    {"severity": "wat", "file": "b.py", "line": 1, "claim": "y"},
                ]
            }
        )
        found = parse_findings(raw, "conformance")
        assert [f.severity for f in found] == ["must-fix", "medium"]

    def test_a_finding_without_a_file_is_dropped(self):
        raw = json.dumps({"findings": [{"severity": "high", "claim": "vague"}]})
        assert parse_findings(raw, "conformance") == []

    def test_a_missing_line_is_none_rather_than_a_guess(self):
        raw = json.dumps({"findings": [{"severity": "high", "file": "a.py", "claim": "x"}]})
        assert parse_findings(raw, "conformance")[0].line is None

    def test_the_unverified_flag_survives(self):
        raw = json.dumps(
            {
                "findings": [
                    {
                        "severity": "high",
                        "file": "a.py",
                        "line": 3,
                        "claim": "x",
                        "unverified": True,
                    }
                ]
            }
        )
        assert parse_findings(raw, "conformance")[0].unverified is True


class TestMergeFindings:
    def _f(self, severity, file, line, source, evidence=""):
        return Finding(
            severity=severity,
            file=file,
            line=line,
            claim=f"{file}:{line}",
            evidence=evidence,
            action="",
            sources=[source],
        )

    def test_the_same_location_from_both_agents_merges_with_both_sources(self):
        merged = merge_findings(
            [
                [self._f("high", "a.py", 5, "conformance", "rule says no")],
                [self._f("high", "a.py", 5, "bughunt", "races with the writer")],
            ]
        )

        assert len(merged) == 1
        assert merged[0].sources == ["bughunt", "conformance"]
        assert "rule says no" in merged[0].evidence
        assert "races with the writer" in merged[0].evidence

    def test_a_merge_keeps_the_higher_severity(self):
        merged = merge_findings(
            [
                [self._f("medium", "a.py", 5, "conformance")],
                [self._f("must-fix", "a.py", 5, "bughunt")],
            ]
        )
        assert merged[0].severity == "must-fix"

    def test_low_and_preference_findings_are_dropped(self):
        merged = merge_findings(
            [
                [
                    self._f("low", "a.py", 1, "conformance"),
                    self._f("preference", "a.py", 2, "conformance"),
                    self._f("medium", "a.py", 3, "conformance"),
                ]
            ]
        )
        assert [f.line for f in merged] == [3]

    def test_sorting_is_severity_then_path_then_line(self):
        merged = merge_findings(
            [
                [
                    self._f("medium", "a.py", 1, "conformance"),
                    self._f("must-fix", "z.py", 9, "conformance"),
                    self._f("must-fix", "a.py", 20, "conformance"),
                    self._f("must-fix", "a.py", 3, "conformance"),
                    self._f("high", "b.py", 1, "conformance"),
                ]
            ]
        )
        assert [(f.severity, f.file, f.line) for f in merged] == [
            ("must-fix", "a.py", 3),
            ("must-fix", "a.py", 20),
            ("must-fix", "z.py", 9),
            ("high", "b.py", 1),
            ("medium", "a.py", 1),
        ]

    def test_different_lines_in_one_file_stay_separate(self):
        merged = merge_findings(
            [[self._f("high", "a.py", 5, "conformance"), self._f("high", "a.py", 6, "conformance")]]
        )
        assert len(merged) == 2

    def test_a_finding_outside_the_diff_is_kept_and_marked(self):
        merged = merge_findings(
            [[self._f("high", "untouched.py", 5, "conformance")]],
            changed_files=["app.py"],
        )
        assert len(merged) == 1
        assert merged[0].outside_diff is True

    def test_a_finding_inside_the_diff_is_not_marked(self):
        merged = merge_findings(
            [[self._f("high", "app.py", 5, "conformance")]],
            changed_files=["app.py"],
        )
        assert merged[0].outside_diff is False


class TestAssembleContext:
    def test_it_carries_conventions_file_bodies_commits_and_callers(self, repo):
        # A *changed signature* rather than a new function, so there is an
        # existing caller for the callers section to find.
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "app.py").write_text("def existing(scale=1):\n    return 1 * scale\n")
        commit(repo, "app: add a helper")
        bundle = collect_diff(repo, "main...HEAD", 200_000)

        context = assemble_context(repo, bundle, ReviewConfig())

        assert "Spaces, never tabs." in context  # conventions
        assert "--- app.py (whole file) ---" in context  # whole-file body
        assert "app: add a helper" in context  # commit subject
        assert "caller.py" in context  # callers of a changed symbol

    def test_the_total_cap_holds(self, repo):
        branch_with_change(repo)
        bundle = collect_diff(repo, "main...HEAD", 200_000)

        context = assemble_context(repo, bundle, ReviewConfig(max_context_chars=300))

        assert len(context) <= 500
