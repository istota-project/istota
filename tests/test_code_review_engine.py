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
arise. The snapshot keeps the rule: it reads blobs with `cat-file --batch` and
never writes a symlink, pinned in `tests/test_code_review_snapshot.py`.

Fixtures shell out to real git, because the hardening under test is git's own
behaviour and a hand-built `.git` directory would not exercise it. They pin
`GIT_CONFIG_GLOBAL` and `GIT_CONFIG_NOSYSTEM` so the developer's own git
configuration cannot change what a fixture repository does.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from istota.skills.code_review import engine
from istota.skills.code_review.engine import (
    Finding,
    ReviewError,
    ReviewerReply,
    build_prompt,
    collect_commits,
    collect_diff,
    finalize_findings,
    git_dir,
    parse_findings,
    parse_ruled_out,
    resolve_range,
    run_review,
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
    """A `DEVELOPER_REPOS_DIR` with nothing in it yet.

    The variable is one user's own subtree of `developer.repos_dir`, which is
    what `setup_env` derives and what `build_bwrap_cmd` binds, so
    `developer_repos_root` requires it to be named for `ISTOTA_USER_ID`.
    """
    root = tmp_path / "repos" / "alice"
    root.mkdir(parents=True)
    monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(root))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
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
        # Something only git says, so the test cannot pass against an
        # implementation that echoes the command and drops stderr.
        assert "bad revision" in str(excinfo.value)
        assert excinfo.value.reason == "bad_range"

    def test_a_dangling_origin_head_falls_through_to_a_local_branch(self, repo):
        """Ordinary after the upstream default branch is renamed."""
        branch_with_change(repo)
        run_git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/gone")

        assert resolve_range(repo) == "main...HEAD"

    def test_an_option_shaped_range_is_refused_before_git_sees_it(self, repo, tmp_path):
        """A range is a bare argv element, so an option is what git reads.

        `--output=<path>` is an arbitrary daemon-side write and `--ext-diff`
        turns the attribute diff driver back on. Both exit 0, so nothing
        downstream reports a problem. Leaving this to the validating command is
        not a boundary either: the option sets differ per subcommand, so a
        spelling `rev-list` rejects can still be one `diff` accepts.
        """
        branch_with_change(repo)
        target = tmp_path / "written_by_git"

        # Positive control: git really does honour it, and really does write.
        subprocess.run(
            ["git", "diff", "--no-ext-diff", "--no-textconv", f"--output={target}", "--"],
            cwd=str(repo),
            capture_output=True,
            env={**os.environ, **GIT_ISOLATION},
        )
        assert target.exists(), "fixture is wrong: --output did not write"
        target.unlink()

        for bad in (f"--output={target}", "--ext-diff", "--textconv", "--stdin", "--all"):
            with pytest.raises(ReviewError) as excinfo:
                resolve_range(repo, explicit=bad)
            assert excinfo.value.reason == "bad_range"
            with pytest.raises(ReviewError):
                collect_diff(repo, bad, 200_000)
        assert not target.exists()

    def test_an_option_shaped_base_is_refused(self, repo):
        branch_with_change(repo)
        with pytest.raises(ReviewError) as excinfo:
            resolve_range(repo, base="--ext-diff")
        assert excinfo.value.reason == "bad_range"

    def test_a_git_command_cannot_read_the_daemons_stdin(self, repo):
        """`rev-list --stdin` would otherwise block on an inherited stdin.

        Hashing empty input rather than hanging is the proof that stdin is
        closed; a test that actually hangs proves the same thing far too slowly.
        """
        from istota.skills.code_review.engine import _git

        # The hash of the empty blob. Reached only if stdin gave EOF at once.
        assert _git(repo, ["hash-object", "--stdin"]).strip() == (
            "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
        )

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
        root = outer / "repos" / "alice"
        plain = root / "plain"
        plain.mkdir(parents=True)
        run_git(outer, "init", "-q", "-b", "main", ".")
        (outer / "outer_secret.py").write_text("OUTER_SENTINEL = 1\n")
        commit(outer, "outer")
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(root))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

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

    def test_a_commondir_redirect_out_of_the_root_is_refused(self, repos_root, tmp_path):
        """The `gitdir:` escape's second spelling, and the one that looks legitimate.

        A linked worktree's git dir is a small directory holding `HEAD`,
        `gitdir` and `commondir`, and `commondir` names the *real* repository —
        objects, refs and config all live there. The model can create such a
        directory inside the root and point `commondir` outside it. Then
        `--absolute-git-dir` reports a contained path and the obvious check
        passes, while every read comes from a repository the operator never put
        in the root, under a config file the model wrote.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        run_git(outside, "init", "-q", "-b", "main", ".")
        (outside / "outside_secret.py").write_text("OUTSIDE_SENTINEL = 1\n")
        commit(outside, "outside")

        main_repo = repos_root / "proj"
        main_repo.mkdir()
        run_git(main_repo, "init", "-q", "-b", "main", ".")
        (main_repo / "f").write_text("x\n")
        commit(main_repo, "base")

        evil = main_repo / ".git" / "worktrees" / "evil"
        evil.mkdir(parents=True)
        (evil / "commondir").write_text(f"{outside / '.git'}\n")
        (evil / "gitdir").write_text(f"{repos_root / 'wt' / '.git'}\n")
        (evil / "HEAD").write_text("ref: refs/heads/main\n")
        wt = repos_root / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {evil}\n")

        # Positive control: the contained-looking answer really is contained,
        # and the outside repository really is readable through it. Without
        # this the assertion below would pass against a check that refused for
        # some unrelated reason.
        reported = run_git(wt, "rev-parse", "--absolute-git-dir").strip()
        assert Path(reported).resolve() == evil.resolve()
        assert str(repos_root) in reported
        assert "OUTSIDE_SENTINEL" in run_git(wt, "show", "main:outside_secret.py")

        with pytest.raises(ReviewError) as excinfo:
            git_dir(wt)
        assert excinfo.value.reason == "common_dir_not_allowed"

    def test_log_show_signature_does_not_run_a_repo_local_gpg_program(self, repo, tmp_path):
        """`git log` is a content command too, and it had none of the flags.

        `log.showSignature` is a plain repo-local boolean and `gpg.program` a
        plain repo-local path, so a `git log` over a signed commit runs a
        chosen command as the daemon user — past `-c diff.external=` and
        `--no-ext-diff`, neither of which has anything to do with signatures.
        """
        sentinel = tmp_path / "gpg_sentinel"
        fake_gpg = tmp_path / "gpg.sh"
        fake_gpg.write_text(f"#!/bin/sh\necho pwned > {sentinel}\nexit 0\n")
        fake_gpg.chmod(0o755)

        # A commit object carrying a gpgsig header, built by hand: making a
        # real `commit -S` succeed needs a program that speaks gpg's status
        # protocol, and the header is all `--show-signature` needs to bite.
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "app.py").write_text("def existing():\n    return 2\n")
        commit(repo, "ordinary")
        tree = run_git(repo, "rev-parse", "HEAD^{tree}").strip()
        parent = run_git(repo, "rev-parse", "HEAD").strip()
        raw = (
            f"tree {tree}\n"
            f"parent {parent}\n"
            "author Test <test@example.invalid> 1700000000 +0000\n"
            "committer Test <test@example.invalid> 1700000000 +0000\n"
            "gpgsig -----BEGIN PGP SIGNATURE-----\n"
            " \n"
            " ZmFrZQ==\n"
            " -----END PGP SIGNATURE-----\n"
            "\n"
            "signed commit\n"
        )
        proc = subprocess.run(
            ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
            cwd=str(repo),
            input=raw,
            capture_output=True,
            text=True,
            env={**os.environ, **GIT_ISOLATION},
        )
        assert proc.returncode == 0, proc.stderr
        run_git(repo, "update-ref", "refs/heads/feature", proc.stdout.strip())
        run_git(repo, "config", "log.showSignature", "true")
        run_git(repo, "config", "gpg.program", str(fake_gpg))

        # Positive control: the attack is live against a log invocation that
        # carries the diff hardening but nothing about signatures.
        subprocess.run(
            ["git", "-c", "diff.external=", "log", "--format=%s", "--no-ext-diff", "main..HEAD"],
            cwd=str(repo),
            capture_output=True,
            env={**os.environ, **GIT_ISOLATION},
        )
        assert sentinel.exists(), "fixture is wrong: gpg.program never fired"
        sentinel.unlink()

        bundle = collect_diff(repo, "main...HEAD", 200_000)
        commits = collect_commits(repo, bundle)

        assert not sentinel.exists()
        assert "signed commit" in commits

    def test_forced_colour_does_not_silently_empty_the_diff(self, repo):
        """`color.ui = always` is not execution, and is just as load-bearing.

        With colour forced on, every diff header arrives wrapped in ANSI
        escapes, the section splitter matches none of them, and the reviewer is
        handed an empty diff — with `truncated` still False and nothing
        anywhere reporting a loss. A review of nothing that says it reviewed
        something is the worst output this module could produce.
        """
        branch_with_change(repo)
        run_git(repo, "config", "color.ui", "always")

        # Positive control: colour really is forced for a plain invocation.
        plain = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--no-textconv", "main...HEAD"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            env={**os.environ, **GIT_ISOLATION},
        )
        assert "\x1b[" in plain.stdout

        bundle = collect_diff(repo, "main...HEAD", 200_000)

        assert "\x1b[" not in bundle.body
        assert "\x1b[" not in bundle.stat
        assert "def added_helper" in bundle.body
        assert bundle.files == ["app.py"]

    def test_an_attribute_driven_textconv_does_not_execute(self, repo, tmp_path):
        """The second route to a command, which the `-c` overrides do not cover.

        `-c diff.external=` clears the global external driver and does nothing
        about a `.gitattributes` line naming a driver plus a `[diff "name"]
        textconv=` entry. Only `--no-textconv` closes that.
        """
        sentinel = tmp_path / "textconv_sentinel"
        script = tmp_path / "tc.sh"
        script.write_text(f"#!/bin/sh\necho pwned > {sentinel}\necho converted\n")
        script.chmod(0o755)
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / ".gitattributes").write_text("*.py diff=evil\n")
        (repo / "app.py").write_text("def existing():\n    return 2\n")
        commit(repo, "attribute driver")
        run_git(repo, "config", "diff.evil.textconv", str(script))

        # Positive control: the attribute driver fires when the flag is absent.
        # (Not run with `-c diff.external=` — an empty external command is one
        # git tries to execute, so it dies on the first file and proves
        # nothing. That is itself why `--no-ext-diff` carries the weight here.)
        subprocess.run(
            ["git", "diff", "--textconv", "main...HEAD"],
            cwd=str(repo),
            capture_output=True,
            env={**os.environ, **GIT_ISOLATION},
        )
        assert sentinel.exists(), "fixture is wrong: the textconv driver never fired"
        sentinel.unlink()

        bundle = collect_diff(repo, "main...HEAD", 200_000)
        shown = engine._show(repo, bundle.head, "app.py")

        assert not sentinel.exists()
        assert "converted" not in bundle.body
        assert "converted" not in (shown or "")

    def test_core_fsmonitor_is_neutralised(self, repo, tmp_path):
        """Pinned as defence in depth, not as a hole that was open.

        `core.fsmonitor` fires when git refreshes the index, which a
        working-tree diff does and a range diff does not — and this module only
        ever diffs ranges. So the positive control below uses the working-tree
        form to show the config is live; the `-c core.fsmonitor=` override is
        what keeps that true if a later verb ever reads the working tree.
        """
        sentinel = tmp_path / "fsm_sentinel"
        script = tmp_path / "fsm.sh"
        script.write_text(f"#!/bin/sh\necho fired > {sentinel}\nexit 1\n")
        script.chmod(0o755)
        branch_with_change(repo)
        run_git(repo, "config", "core.fsmonitor", str(script))
        (repo / "app.py").write_text("def existing():\n    return 99\n")

        subprocess.run(
            ["git", "diff", "--stat", "--"],
            cwd=str(repo),
            capture_output=True,
            env={**os.environ, **GIT_ISOLATION},
        )
        assert sentinel.exists(), "fixture is wrong: core.fsmonitor never fired"
        sentinel.unlink()

        collect_diff(repo, "main...HEAD", 200_000)

        assert not sentinel.exists()

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

    def test_the_head_is_the_ranges_endpoint_not_always_HEAD(self, repo):
        """The snapshot is written at `bundle.head`.

        `resolve_range` produces `<base>...HEAD`, but an explicit range need
        not end there, and writing the tree at HEAD for a range that ends
        elsewhere hands the reviewer a different tree than the diff with
        nothing saying so.
        """
        branch_with_change(repo)
        (repo / "app.py").write_text("def existing():\n    return 1\n\n\nLATER_SENTINEL = 1\n")
        commit(repo, "a later commit")
        earlier = run_git(repo, "rev-parse", "HEAD~1").strip()

        bundle = collect_diff(repo, f"main..{earlier}", 200_000)
        bodies = engine._show(repo, bundle.head, "app.py") or ""

        assert bundle.head == earlier
        assert "def added_helper" in bodies
        assert "LATER_SENTINEL" not in bodies

    def test_a_bad_range_raises_with_the_git_stderr(self, repo):
        with pytest.raises(ReviewError) as excinfo:
            collect_diff(repo, "nope...HEAD", 200_000)
        assert excinfo.value.reason == "bad_range"


class TestRequireObjectId:
    """The guard that stands in for `--end-of-options` where git lacks it.

    `git grep` rejected the flag on Debian bookworm's git 2.39, which is what
    `docker/istota/Dockerfile` shipped until ISSUE-440, so the substitute is a
    full object id. The snapshot asks for one before every `ls-tree` and
    history call, and these exercise the guard directly, independent of which
    git is installed.
    """

    def test_an_option_shaped_revision_is_refused(self):
        with pytest.raises(ReviewError, match="not a resolved object id") as excinfo:
            engine._require_object_id("--output=/tmp/pwned", "revision")
        assert excinfo.value.reason == "bad_range"

    def test_a_bare_ref_is_refused_rather_than_resolved(self):
        """Stricter than `--end-of-options`, deliberately: accepting a ref would
        make the guard "does it start with a dash", which is the check it
        replaced."""
        with pytest.raises(ReviewError, match="not a resolved object id"):
            engine._require_object_id("HEAD", "revision")

    def test_a_resolved_id_passes(self, repo):
        head = run_git(repo, "rev-parse", "HEAD").strip()
        assert engine._require_object_id(f" {head}\n", "revision") == head


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
        found = parse_findings(json.dumps(self.PAYLOAD))
        assert len(found) == 1
        assert found[0].file == "app.py"
        assert found[0].line == 12

    def test_fenced_json_parses(self):
        raw = "```json\n" + json.dumps(self.PAYLOAD) + "\n```"
        assert len(parse_findings(raw)) == 1

    def test_leading_prose_is_tolerated(self):
        raw = "Here is what I found.\n\n" + json.dumps(self.PAYLOAD)
        assert len(parse_findings(raw)) == 1

    def test_a_bare_list_of_findings_parses(self):
        assert len(parse_findings(json.dumps(self.PAYLOAD["findings"]))) == 1

    def test_malformed_output_returns_nothing_so_the_caller_can_retry(self):
        assert parse_findings("I could not review this, sorry.") == []
        assert parse_findings("") == []
        assert parse_findings("{ not json at all") == []

    def test_severity_is_normalised_and_an_unknown_one_is_kept_as_medium(self):
        raw = json.dumps(
            {
                "findings": [
                    {"severity": "MUST_FIX", "file": "a.py", "line": 1, "claim": "x"},
                    {"severity": "wat", "file": "b.py", "line": 1, "claim": "y"},
                ]
            }
        )
        found = parse_findings(raw)
        assert [f.severity for f in found] == ["must-fix", "medium"]

    def test_a_finding_without_a_file_is_dropped(self):
        raw = json.dumps({"findings": [{"severity": "high", "claim": "vague"}]})
        assert parse_findings(raw) == []

    def test_a_missing_line_is_none_rather_than_a_guess(self):
        raw = json.dumps({"findings": [{"severity": "high", "file": "a.py", "claim": "x"}]})
        assert parse_findings(raw)[0].line is None

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
        assert parse_findings(raw)[0].unverified is True

    def test_settle_is_read_and_capped(self):
        raw = json.dumps(
            {
                "findings": [
                    {
                        "severity": "high",
                        "file": "a.py",
                        "line": 3,
                        "claim": "x",
                        "unverified": True,
                        "settle": "  read b.py:10 " + "y" * 400,
                    },
                    {"severity": "high", "file": "c.py", "line": 1, "claim": "z", "settle": 7},
                ]
            }
        )
        first, second = parse_findings(raw)
        assert first.settle.startswith("read b.py:10 ")
        assert len(first.settle) == engine.MAX_SETTLE_CHARS
        assert second.settle == ""

    def test_a_finding_with_no_settle_has_an_empty_one(self):
        assert parse_findings(json.dumps(self.PAYLOAD))[0].settle == ""


class TestParseRuledOut:
    def test_well_formed_items_are_returned(self):
        payload = {
            "findings": [],
            "ruled_out": [
                {"theory": "race on the cache", "checked": "the lock is held in caller.py:12"},
            ],
        }
        assert parse_ruled_out(payload) == [
            {"theory": "race on the cache", "checked": "the lock is held in caller.py:12"}
        ]

    def test_the_raw_response_is_accepted_too(self):
        raw = "Done.\n```json\n" + json.dumps({"ruled_out": [{"theory": "t"}]}) + "\n```"
        assert parse_ruled_out(raw) == [{"theory": "t", "checked": ""}]

    def test_a_missing_checked_defaults_to_empty(self):
        assert parse_ruled_out({"ruled_out": [{"theory": "t"}]}) == [
            {"theory": "t", "checked": ""}
        ]

    def test_items_without_a_theory_are_dropped(self):
        payload = {
            "ruled_out": [
                {"checked": "nothing to say"},
                {"theory": "   ", "checked": "blank"},
                {"theory": 5, "checked": "not text"},
                "a bare string",
                {"theory": "kept"},
            ]
        }
        assert parse_ruled_out(payload) == [{"theory": "kept", "checked": ""}]

    @pytest.mark.parametrize(
        "payload",
        [
            {"ruled_out": "none"},
            {"ruled_out": {"theory": "t"}},
            {"findings": []},
            [{"theory": "t"}],
            None,
            "not json at all",
        ],
    )
    def test_anything_but_a_list_under_ruled_out_is_empty(self, payload):
        assert parse_ruled_out(payload) == []

    def test_the_list_is_capped(self):
        payload = {"ruled_out": [{"theory": f"t{i}"} for i in range(50)]}
        out = parse_ruled_out(payload)
        assert len(out) == engine.MAX_RULED_OUT_ITEMS
        assert out[-1]["theory"] == f"t{engine.MAX_RULED_OUT_ITEMS - 1}"

    def test_long_strings_are_capped(self):
        payload = {"ruled_out": [{"theory": "t" * 500, "checked": "c" * 500}]}
        [item] = parse_ruled_out(payload)
        assert len(item["theory"]) == engine.MAX_RULED_OUT_CHARS
        assert len(item["checked"]) == engine.MAX_RULED_OUT_CHARS


class TestBuildPrompt:
    def _bundle(self, **overrides):
        from istota.skills.code_review.engine import DiffBundle

        fields = dict(
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
        fields.update(overrides)
        return DiffBundle(**fields)

    def _snapshot(self, tmp_path, **overrides):
        from istota.skills.code_review.snapshot import Snapshot

        # A fixed path: `tmp_path` carries the test's name, which would leak
        # words like "need_files" into the prompt under test.
        run_dir = Path("/review-root/alice/run-abc")
        fields = dict(
            run_dir=run_dir,
            tree_dir=run_dir / "tree",
            files=2140,
            bytes=31_000_000,
            skipped={},
            truncated=False,
        )
        fields.update(overrides)
        return Snapshot(**fields)

    def test_it_carries_the_method_the_range_the_intent_and_the_diff(self, tmp_path):
        prompt = build_prompt(
            self._bundle(), self._snapshot(tmp_path), "fix the header", file_budget=8
        )

        assert engine.REVIEWER_METHOD.strip() in prompt
        assert prompt.startswith(engine.REVIEWER_METHOD.strip())
        assert "Review the changes in main...HEAD." in prompt
        assert "fix the header" in prompt
        assert "added_helper" in prompt
        assert " app.py | 2 +-" in prompt

    def test_the_budget_line_carries_the_configured_number(self, tmp_path):
        prompt = build_prompt(self._bundle(), self._snapshot(tmp_path), "", file_budget=13)

        assert "Read at most 13 files beyond the changed files." in prompt

    def test_the_snapshot_summary_names_where_it_is_and_what_was_left_out(self, tmp_path):
        snapshot = self._snapshot(
            tmp_path, truncated=True, skipped={"symlink": 3, "too_large": 1, "missing": 0}
        )
        prompt = build_prompt(self._bundle(), snapshot, "", file_budget=8)

        assert str(snapshot.tree_dir) in prompt
        assert str(snapshot.run_dir / "meta") in prompt
        assert "2140 files" in prompt
        assert "size cap" in prompt
        assert "3 symlink" in prompt
        assert "1 too_large" in prompt
        assert "missing" not in prompt.split("## The snapshot")[1].split("## Diff stat")[0]

    def test_an_untruncated_snapshot_does_not_claim_a_cap(self, tmp_path):
        prompt = build_prompt(self._bundle(), self._snapshot(tmp_path), "", file_budget=8)

        assert "size cap" not in prompt

    def test_the_output_contract_asks_for_ruled_out_and_settle(self, tmp_path):
        prompt = build_prompt(self._bundle(), self._snapshot(tmp_path), "", file_budget=8)

        contract = prompt.rsplit("Return one JSON object", 1)[1]
        assert '"ruled_out"' in contract
        assert '"settle"' in contract
        assert '"theory"' in contract

    def test_nothing_of_the_need_files_round_trip_is_left(self, tmp_path):
        prompt = build_prompt(self._bundle(), self._snapshot(tmp_path), "", file_budget=8)

        assert "need_files" not in prompt
        assert "need_files" not in engine.REVIEWER_METHOD

    def test_a_truncated_diff_points_at_the_full_patch(self, tmp_path):
        bundle = self._bundle(truncated=True, truncated_files=["big.py"])
        prompt = build_prompt(bundle, self._snapshot(tmp_path), "", file_budget=8)

        snapshot = self._snapshot(tmp_path)
        header = prompt.split("## The snapshot")[0].split("Review the changes in")[1]
        assert "big.py" in header
        # Absolute, never cwd-relative: the reviewer's cwd is `tree/`, where
        # `meta/diff.patch` would be a file the branch itself carries.
        assert f"The full patch is at {snapshot.run_dir / 'meta' / 'diff.patch'};" in header

    def test_a_text_only_run_says_there_are_no_tools(self, tmp_path):
        bundle = self._bundle(truncated=True, truncated_files=["big.py"])
        prompt = build_prompt(bundle, None, "", file_budget=8)

        assert "This run has no tools and no snapshot." in prompt
        assert "## The snapshot" not in prompt
        assert "Read at most" not in prompt
        # The truncation note cannot send a reviewer with no tools to a file.
        tail = prompt.split("Review the changes in", 1)[1]
        header = tail.split("## Diff stat")[0]
        assert "big.py" in header
        assert "meta/diff.patch" not in header
        # One instruction for an unseen part, matching the no-tools note.
        assert "is unverified" in header
        assert "Do not report" not in header

    def test_commits_are_included_and_bounded(self, tmp_path):
        commits = "app: add a helper\n\n--\n" + "x" * 10_000
        prompt = build_prompt(
            self._bundle(), self._snapshot(tmp_path), "", file_budget=8, commits=commits
        )

        section = prompt.split("## Commits in the range\n\n", 1)[1]
        fenced = section.split("\n\nReturn one JSON object", 1)[0]
        opener, rest = fenced.split("\n", 1)
        body, closer = rest.rsplit("\n", 1)
        # Fenced as branch-author text, and cut before the fence goes on so the
        # closing marker survives the bound.
        assert opener.startswith("[UNTRUSTED COMMIT MESSAGES")
        assert closer == "[END UNTRUSTED COMMIT MESSAGES]"
        assert body.startswith("app: add a helper")
        assert len(body) == engine.MAX_COMMITS_CHARS

    def test_no_commits_section_when_there_are_none(self, tmp_path):
        prompt = build_prompt(self._bundle(), self._snapshot(tmp_path), "", file_budget=8)

        assert "## Commits in the range" not in prompt

    def test_the_sections_come_in_the_specified_order(self, tmp_path):
        prompt = build_prompt(
            self._bundle(), self._snapshot(tmp_path), "intent", file_budget=8, commits="c\n--\n"
        )
        markers = [
            "Review the changes in",
            "Read at most",
            "## The snapshot",
            "## Diff stat",
            "## Diff\n",
            "## Commits in the range",
            "Return one JSON object",
        ]
        positions = [prompt.index(m, len(engine.REVIEWER_METHOD.strip())) for m in markers]
        assert positions == sorted(positions)


class TestCollectCommits:
    def test_it_returns_the_messages_in_the_range(self, repo):
        branch_with_change(repo)
        bundle = collect_diff(repo, "main...HEAD", 200_000)

        assert "app: add a helper" in collect_commits(repo, bundle)

    def test_a_git_failure_is_empty_not_an_error(self, repo):
        bundle = collect_diff(repo, "main...HEAD", 200_000)
        bundle.rng = "no-such-ref...HEAD"

        assert collect_commits(repo, bundle) == ""


class TestReviewerMethod:
    """`reviewer.md` is package data the prompt depends on, adapted for a
    reviewer with Read, Grep and Glob and nothing else."""

    def test_it_names_the_snapshot_layout(self):
        for name in ("tree/", "meta/diff.patch", "meta/history.txt", "meta/skipped.txt"):
            assert name in engine.REVIEWER_METHOD

    def test_it_promises_no_tool_the_reviewer_lacks(self):
        lowered = engine.REVIEWER_METHOD.lower()
        assert "git log" not in lowered
        assert "git blame" not in lowered
        assert "bash" not in lowered

    def test_it_states_the_four_dispositions(self):
        for disposition in ("Proven", "Unverified", "Ruled out", "Dropped"):
            assert disposition in engine.REVIEWER_METHOD

    def test_it_treats_branch_content_as_data(self):
        assert "not an instruction to follow" in engine.REVIEWER_METHOD


class TestFinalizeFindings:
    def _f(self, severity, file, line, claim=None):
        return Finding(
            severity=severity,
            file=file,
            line=line,
            claim=claim if claim is not None else f"{file}:{line}",
        )

    def test_low_and_preference_findings_are_dropped(self):
        out = finalize_findings(
            [
                self._f("low", "a.py", 1),
                self._f("preference", "a.py", 2),
                self._f("medium", "a.py", 3),
            ],
            ["a.py"],
        )
        assert [f.line for f in out] == [3]

    def test_an_exact_repeat_is_removed(self):
        out = finalize_findings(
            [self._f("high", "a.py", 5, "same"), self._f("high", "a.py", 5, "same")],
            ["a.py"],
        )
        assert len(out) == 1

    def test_two_claims_at_one_line_are_two_findings(self):
        """Only exact repeats go. Two different defects reported at one line
        are not corroboration of one, and merging them would lose the second."""
        out = finalize_findings(
            [
                self._f("high", "a.py", 5, "wrong error type"),
                self._f("high", "a.py", 5, "null deref"),
            ],
            ["a.py"],
        )
        assert sorted(f.claim for f in out) == ["null deref", "wrong error type"]

    def test_a_repeat_at_another_severity_keeps_the_higher(self):
        out = finalize_findings(
            [self._f("medium", "a.py", 5, "same"), self._f("high", "a.py", 5, "same")],
            ["a.py"],
        )
        assert [f.severity for f in out] == ["high"]

    def test_a_finding_outside_the_diff_is_kept_and_marked(self):
        out = finalize_findings(
            [self._f("high", "untouched.py", 5), self._f("high", "app.py", 2)],
            ["app.py"],
        )
        marks = {f.file: f.outside_diff for f in out}
        assert marks == {"untouched.py": True, "app.py": False}

    def test_sorting_is_severity_then_path_then_line(self):
        out = finalize_findings(
            [
                self._f("medium", "a.py", 1),
                self._f("must-fix", "z.py", 9),
                self._f("must-fix", "a.py", 20),
                self._f("must-fix", "a.py", None),
                self._f("must-fix", "a.py", 3),
                self._f("high", "b.py", 1),
            ],
            ["a.py"],
        )
        assert [(f.severity, f.file, f.line) for f in out] == [
            ("must-fix", "a.py", None),
            ("must-fix", "a.py", 3),
            ("must-fix", "a.py", 20),
            ("must-fix", "z.py", 9),
            ("high", "b.py", 1),
            ("medium", "a.py", 1),
        ]


class FakeInvoke:
    """The brain seam: answers from a script and records what it was asked.

    Each call is `(prompt, timeout, tools)`. A `str` reply is a successful
    answer, a `ReviewerReply` is returned as is, and an exception is raised.
    """

    def __init__(self, *replies, delay: float = 0.0):
        self.replies = list(replies)
        self.calls: list[tuple[str, int, bool]] = []
        self.handed: list[tuple] = []
        self.delay = delay

    def __call__(self, prompt, timeout, *, tools, snapshot=None, sandbox=None):
        self.calls.append((prompt, timeout, tools))
        self.handed.append((snapshot, sandbox))
        if self.delay:
            time.sleep(self.delay)
        reply = self.replies.pop(0) if self.replies else '{"findings": []}'
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, ReviewerReply):
            return reply
        return ReviewerReply(ok=True, text=reply, model="resolved/smart")


class FakeSnapshots:
    """`build_snapshot` and `build_sandbox`, recording that each was asked."""

    def __init__(self, tmp_path, *, snapshot_error=None, refused=False, sandbox_error=None):
        self.run_dir = tmp_path / "review-root" / "run-abc"
        self.snapshot_error = snapshot_error
        self.refused = refused
        self.sandbox_error = sandbox_error
        self.snapshots: list = []
        self.sandboxes: list = []

    def build_snapshot(self, worktree, bundle):
        from istota.skills.code_review.snapshot import Snapshot

        if self.snapshot_error is not None:
            raise self.snapshot_error
        snapshot = Snapshot(
            run_dir=self.run_dir,
            tree_dir=self.run_dir / "tree",
            files=12,
            bytes=3456,
            skipped={"symlink": 1},
            truncated=False,
        )
        self.snapshots.append((worktree, bundle, snapshot))
        return snapshot

    def build_sandbox(self, snapshot):
        self.sandboxes.append(snapshot)
        if self.sandbox_error is not None:
            raise self.sandbox_error
        sandbox = SimpleNamespace(refused=self.refused, wrap=None)
        self.built = sandbox
        return sandbox


# The whole envelope `run_review` returns on a clean run. Spelt out rather than
# derived, so a key added or removed is a deliberate edit here: the workflow
# reads these by name, and `skill.md` documents them.
OK_KEYS = {
    "status",
    "range",
    "files_changed",
    "lines_changed",
    "truncated",
    "truncated_files",
    "rounds",
    "agent_timeout_seconds",
    "overhead_seconds",
    "counts",
    "findings",
    "ruled_out",
    "dropped_findings",
    "empty",
    "reviewer",
    "snapshot",
    "notice",
}

FINDING_KEYS = {
    "severity",
    "file",
    "line",
    "claim",
    "evidence",
    "action",
    "unverified",
    "settle",
    "outside_diff",
}


def answer(*findings, ruled_out=()) -> str:
    return json.dumps({"findings": list(findings), "ruled_out": list(ruled_out)})


def a_finding(severity="high", file="app.py", line=4, claim="a defect", **extra):
    return {
        "severity": severity,
        "file": file,
        "line": line,
        "claim": claim,
        "evidence": "observed",
        "action": "fix it",
        **extra,
    }


class TestRunReview:
    def _run(self, repo, invoke, fakes, **kwargs):
        return run_review(
            repo,
            base="main",
            intent=kwargs.pop("intent", "add a helper"),
            invoke=invoke,
            build_snapshot=fakes.build_snapshot,
            build_sandbox=fakes.build_sandbox,
            timeout_seconds=kwargs.pop("timeout_seconds", 120),
            **kwargs,
        )

    def test_happy_path_envelope(self, repo, tmp_path):
        branch_with_change(repo)
        invoke = FakeInvoke(
            answer(
                a_finding(severity="high", claim="unbounded loop"),
                a_finding(
                    severity="medium", file="caller.py", line=3, claim="stale call",
                    unverified=True, settle="read caller.py:3",
                ),
                a_finding(severity="low", line=9, claim="naming"),
                ruled_out=[{"theory": "race on the cache", "checked": "single writer"}],
            )
        )
        fakes = FakeSnapshots(tmp_path)

        envelope = self._run(repo, invoke, fakes)

        assert set(envelope) == OK_KEYS
        assert envelope["status"] == "ok"
        assert envelope["range"] == "main...HEAD"
        assert envelope["rounds"] == 1
        assert envelope["empty"] is False
        assert envelope["agent_timeout_seconds"] == 120
        assert envelope["reviewer"] == {
            "model": "resolved/smart", "tools": True, "tools_reason": "",
        }
        assert envelope["snapshot"] == {
            "files": 12, "bytes": 3456, "truncated": False, "skipped": {"symlink": 1},
        }
        assert envelope["counts"] == {
            "must-fix": 0, "high": 1, "medium": 1, "total": 2,
        }
        assert envelope["dropped_findings"] == 0
        assert envelope["ruled_out"] == [
            {"theory": "race on the cache", "checked": "single writer"}
        ]
        assert envelope["notice"] == engine.NOTICE
        assert all(set(f) == FINDING_KEYS for f in envelope["findings"])
        high, medium = envelope["findings"]
        assert high["claim"] == "unbounded loop"
        assert high["outside_diff"] is False
        assert medium["outside_diff"] is True
        assert medium["unverified"] is True
        assert medium["settle"] == "read caller.py:3"

        # One reviewer, one call, with tools, against the snapshot just built.
        [(prompt, timeout, tools)] = invoke.calls
        assert tools is True
        assert timeout == 120
        assert "## The snapshot" in prompt
        assert str(fakes.run_dir / "tree") in prompt
        assert "add a helper" in prompt
        assert len(fakes.snapshots) == 1
        assert fakes.sandboxes == [fakes.snapshots[0][2]]

    def test_the_commit_messages_reach_the_prompt_fenced(self, repo, tmp_path):
        """Branch-author text, so inside the repository's one untrusted fence."""
        branch_with_change(repo)
        invoke = FakeInvoke()

        self._run(repo, invoke, FakeSnapshots(tmp_path))

        prompt = invoke.calls[0][0]
        section = prompt.split("## Commits in the range\n\n", 1)[1]
        opened = section.split("\n", 1)[0]
        assert opened.startswith("[UNTRUSTED")
        assert "COMMIT MESSAGES" in opened
        assert "app: add a helper" in section.split("Return one JSON object")[0]

    def test_malformed_then_reformatted_is_one_round(self, repo, tmp_path):
        """The reformat is text-only: a tooled run is a whole agent loop, and
        repeating it to fix formatting doubles the cost for no new evidence."""
        branch_with_change(repo)
        first = "I found one thing: app.py line 4 loops forever. " + "z" * 40_000
        invoke = FakeInvoke(first, answer(a_finding(claim="loops forever")))

        envelope = self._run(repo, invoke, FakeSnapshots(tmp_path))

        assert envelope["status"] == "ok"
        assert envelope["rounds"] == 1
        assert [f["claim"] for f in envelope["findings"]] == ["loops forever"]
        assert envelope["reviewer"]["tools"] is True
        assert [tools for _, _, tools in invoke.calls] == [True, False]
        reformat = invoke.calls[1][0]
        assert "could not be parsed" in reformat
        assert "Return one JSON object" in reformat
        assert "app.py line 4 loops forever" in reformat
        # Not the review prompt again: no method, no diff, no snapshot to read.
        assert engine.REVIEWER_METHOD.strip() not in reformat
        assert "## The snapshot" not in reformat
        # The previous answer is capped, not repeated whole.
        assert reformat.count("z") <= engine.REFORMAT_ANSWER_CHARS
        assert reformat.count("z") > engine.REFORMAT_ANSWER_CHARS - 100

    def test_malformed_twice_is_skipped_and_carries_the_raw_output(self, repo, tmp_path):
        branch_with_change(repo)
        invoke = FakeInvoke("not json", "still not json")

        envelope = self._run(repo, invoke, FakeSnapshots(tmp_path))

        assert envelope["status"] == "skipped"
        assert envelope["reason"] == "malformed_output"
        assert "not json" in envelope["error"]
        assert envelope["rounds"] == 1
        assert envelope["notice"] == engine.FAILED_NOTICE
        assert set(envelope) == OK_KEYS | {"reason", "error"}
        assert len(invoke.calls) == 2

    def test_every_item_unusable_is_malformed_not_clean(self, repo, tmp_path):
        branch_with_change(repo)
        invoke = FakeInvoke(
            json.dumps({"findings": [{"severity": "must-fix", "claim": "no file"}]}),
            answer(a_finding(severity="must-fix")),
        )

        envelope = self._run(repo, invoke, FakeSnapshots(tmp_path))

        assert len(invoke.calls) == 2
        assert envelope["counts"]["must-fix"] == 1

    def test_partly_unusable_items_are_counted(self, repo, tmp_path):
        branch_with_change(repo)
        invoke = FakeInvoke(
            json.dumps({"findings": [a_finding(), {"severity": "high", "claim": "no file"}]})
        )

        envelope = self._run(repo, invoke, FakeSnapshots(tmp_path))

        assert len(invoke.calls) == 1
        assert envelope["counts"]["total"] == 1
        assert envelope["dropped_findings"] == 1

    def test_a_failed_call_is_skipped_and_not_retried(self, repo, tmp_path):
        branch_with_change(repo)
        invoke = FakeInvoke(ReviewerReply(ok=False, error="reviewer failed (stop_reason=timeout)"))

        envelope = self._run(repo, invoke, FakeSnapshots(tmp_path))

        assert envelope["status"] == "skipped"
        assert envelope["reason"] == "review_failed"
        assert "timeout" in envelope["error"]
        assert envelope["rounds"] == 1
        assert len(invoke.calls) == 1

    def test_a_raising_brain_is_a_failed_call(self, repo, tmp_path):
        branch_with_change(repo)
        invoke = FakeInvoke(RuntimeError("api exploded"))

        envelope = self._run(repo, invoke, FakeSnapshots(tmp_path))

        assert envelope["status"] == "skipped"
        assert envelope["reason"] == "review_failed"
        assert "api exploded" in envelope["error"]
        assert envelope["rounds"] == 1

    def test_a_request_fault_during_the_call_still_blocks_the_push(self, repo, tmp_path):
        """`skipped` tells the workflow to land the branch, so a containment
        refusal that surfaced inside the call must not degrade to it."""
        branch_with_change(repo)
        invoke = FakeInvoke(ReviewError("reaches outside", reason="git_dir_not_allowed"))

        envelope = self._run(repo, invoke, FakeSnapshots(tmp_path))

        assert envelope["status"] == "error"
        assert envelope["reason"] == "git_dir_not_allowed"

    def test_a_reformat_with_no_time_left_is_not_started(self, repo, tmp_path, monkeypatch):
        branch_with_change(repo)
        invoke = FakeInvoke("prose", answer(a_finding()))
        monkeypatch.setattr(engine, "_remaining", lambda started, timeout: (3, 15))

        envelope = self._run(repo, invoke, FakeSnapshots(tmp_path))

        assert len(invoke.calls) == 1
        assert envelope["reason"] == "malformed_output"
        assert "3s" in envelope["error"]

    def test_the_reformat_runs_on_what_is_left_of_the_budget(self, repo, tmp_path):
        branch_with_change(repo)
        invoke = FakeInvoke("prose", answer(), delay=1.2)

        self._run(repo, invoke, FakeSnapshots(tmp_path), timeout_seconds=60)

        assert invoke.calls[0][1] == 60
        assert invoke.calls[1][1] < 60

    def test_a_snapshot_failure_falls_back_to_text_only(self, repo, tmp_path, caplog):
        """A degraded review, not a request fault: logged, marked, and run."""
        branch_with_change(repo)
        invoke = FakeInvoke(answer(a_finding()))
        fakes = FakeSnapshots(
            tmp_path, snapshot_error=ReviewError("disk full", reason="snapshot_failed")
        )

        with caplog.at_level("WARNING", logger=engine.logger.name):
            envelope = self._run(repo, invoke, fakes)

        assert envelope["status"] == "ok"
        assert envelope["reviewer"]["tools"] is False
        assert envelope["reviewer"]["tools_reason"] == "snapshot_failed"
        assert envelope["snapshot"] is None
        assert fakes.sandboxes == []
        [(prompt, _, tools)] = invoke.calls
        assert tools is False
        assert "This run has no tools and no snapshot." in prompt
        assert "## The snapshot" not in prompt
        assert any("snapshot_failed" in r.getMessage() for r in caplog.records)

    def test_an_unexpected_snapshot_exception_also_falls_back(self, repo, tmp_path):
        branch_with_change(repo)
        invoke = FakeInvoke(answer())
        fakes = FakeSnapshots(tmp_path, snapshot_error=OSError("no space left"))

        envelope = self._run(repo, invoke, fakes)

        assert envelope["status"] == "ok"
        assert envelope["reviewer"]["tools_reason"] == "snapshot_failed"
        assert invoke.calls[0][2] is False

    def test_a_refused_sandbox_falls_back_to_text_only(self, repo, tmp_path):
        """Never tools without the namespace that was wanted for them."""
        branch_with_change(repo)
        invoke = FakeInvoke(answer())
        fakes = FakeSnapshots(tmp_path, refused=True)

        envelope = self._run(repo, invoke, fakes)

        assert envelope["status"] == "ok"
        assert envelope["reviewer"] == {
            "model": "resolved/smart", "tools": False, "tools_reason": "sandbox_refused",
        }
        # The snapshot was built (and the caller still removes it), so it is
        # reported, but the prompt does not send a tool-less reviewer to it.
        assert envelope["snapshot"]["files"] == 12
        [(prompt, _, tools)] = invoke.calls
        assert tools is False
        assert "This run has no tools and no snapshot." in prompt
        assert str(fakes.run_dir) not in prompt

    def test_a_raising_sandbox_builder_is_a_refusal(self, repo, tmp_path):
        branch_with_change(repo)
        invoke = FakeInvoke(answer())
        fakes = FakeSnapshots(tmp_path, sandbox_error=RuntimeError("bwrap missing"))

        envelope = self._run(repo, invoke, fakes)

        assert envelope["reviewer"]["tools_reason"] == "sandbox_refused"
        assert invoke.calls[0][2] is False

    def test_the_tooled_call_is_handed_what_the_builders_returned(self, repo, tmp_path):
        """The grant and its confinement travel together: the tooled call gets
        the very snapshot and namespace objects, and the reformat gets neither."""
        branch_with_change(repo)
        invoke = FakeInvoke("prose", answer())
        fakes = FakeSnapshots(tmp_path)

        self._run(repo, invoke, fakes)

        snapshot = fakes.snapshots[0][2]
        assert invoke.handed[0][0] is snapshot
        assert invoke.handed[0][1] is fakes.built
        assert invoke.handed[1] == (None, None)

    def test_a_text_only_call_is_handed_no_namespace(self, repo, tmp_path):
        branch_with_change(repo)
        invoke = FakeInvoke(answer())

        self._run(repo, invoke, FakeSnapshots(tmp_path, refused=True))

        assert invoke.handed == [(None, None)]

    def test_a_snapshot_containment_refusal_is_a_request_fault(self, repo, tmp_path):
        """Only `snapshot_failed` degrades. A containment refusal from the
        snapshot step must block the push, not become a text-only review."""
        branch_with_change(repo)
        invoke = FakeInvoke(answer())
        fakes = FakeSnapshots(
            tmp_path,
            snapshot_error=ReviewError("reaches outside", reason="git_dir_not_allowed"),
        )

        with pytest.raises(ReviewError) as excinfo:
            self._run(repo, invoke, fakes)

        assert excinfo.value.reason == "git_dir_not_allowed"
        assert invoke.calls == []

    def test_a_redirect_after_the_diff_stops_the_run_before_git_log(
        self, repo, repos_root, tmp_path
    ):
        """The worktree stays writable while the snapshot is built. A `.git`
        pointed outside the root since `collect_diff` must refuse the run, not
        hand another repository's history to the reviewer."""
        branch_with_change(repo)
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        run_git(outside, "init", "-q", "-b", "main", ".")
        (outside / "f.txt").write_text("x\n")
        commit(outside, "OUTSIDE_HISTORY_SENTINEL")
        invoke = FakeInvoke(answer())

        class Redirecting(FakeSnapshots):
            def build_snapshot(self, worktree, bundle):
                git = worktree / ".git"
                moved = worktree.parent / "proj-git-moved"
                git.rename(moved)
                (worktree / ".git").write_text(f"gitdir: {outside / '.git'}\n")
                raise ReviewError("snapshot gave up", reason="snapshot_failed")

        with pytest.raises(ReviewError) as excinfo:
            self._run(repo, invoke, Redirecting(tmp_path))

        assert excinfo.value.reason == "git_dir_not_allowed"
        assert invoke.calls == []

    def test_a_raise_after_a_call_keeps_what_that_call_cost(self, repo, tmp_path):
        branch_with_change(repo)
        invoke = FakeInvoke("prose", RuntimeError("api exploded"), delay=1.0)

        envelope = self._run(repo, invoke, FakeSnapshots(tmp_path))

        assert envelope["reason"] == "review_failed"
        assert envelope["reviewer"]["model"] == "resolved/smart"
        # Both calls' wall time is model time, not overhead.
        assert envelope["overhead_seconds"] < 1.0

    def test_an_empty_range_builds_nothing_and_calls_nothing(self, repo, tmp_path):
        run_git(repo, "checkout", "-q", "-b", "feature")
        invoke = FakeInvoke()
        fakes = FakeSnapshots(tmp_path)

        envelope = self._run(repo, invoke, fakes)

        assert set(envelope) == OK_KEYS
        assert envelope["status"] == "ok"
        assert envelope["empty"] is True
        assert envelope["rounds"] == 0
        assert envelope["snapshot"] is None
        assert envelope["reviewer"]["tools"] is False
        assert envelope["notice"] == engine.EMPTY_NOTICE
        assert invoke.calls == []
        assert fakes.snapshots == []
        assert fakes.sandboxes == []

    def test_overhead_excludes_the_model_call(self, repo, tmp_path):
        branch_with_change(repo)
        invoke = FakeInvoke(answer(), delay=2.0)

        envelope = self._run(repo, invoke, FakeSnapshots(tmp_path))

        assert 0 < envelope["overhead_seconds"] < 2.0

    def test_one_info_line_per_run_and_never_the_findings_text(self, repo, tmp_path, caplog):
        branch_with_change(repo)
        invoke = FakeInvoke(
            answer(
                a_finding(claim="SECRET_CLAIM_TEXT"),
                ruled_out=[{"theory": "SECRET_THEORY_TEXT", "checked": "x"}],
            )
        )

        with caplog.at_level("INFO", logger=engine.logger.name):
            self._run(repo, invoke, FakeSnapshots(tmp_path))

        lines = [r.getMessage() for r in caplog.records if r.name == engine.logger.name]
        assert len(lines) == 1
        [line] = lines
        assert "main...HEAD" in line
        assert "tools=on" in line
        assert "high=1" in line
        assert "ruled_out=1" in line
        assert "snapshot_bytes=3456" in line
        assert "SECRET" not in line

    def test_it_needs_every_callable(self, repo, tmp_path):
        fakes = FakeSnapshots(tmp_path)
        for missing in ("invoke", "build_snapshot", "build_sandbox"):
            kwargs = dict(
                invoke=FakeInvoke(),
                build_snapshot=fakes.build_snapshot,
                build_sandbox=fakes.build_sandbox,
            )
            kwargs[missing] = None
            with pytest.raises(ReviewError):
                run_review(repo, base="main", **kwargs)
