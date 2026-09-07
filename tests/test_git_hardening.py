"""`git_hardening.run_git` and `GIT_SUBPROCESS_ENV`.

The four environment variables were written out at four call sites before this
module owned them, and the failure mode a green suite does not notice is one of
them going missing from the shared copy: `GIT_TERMINAL_PROMPT` costs a hang on
a prompt nobody is at, `GIT_OPTIONAL_LOCKS` costs the worktree reaper its idle
clock, and neither shows up as a wrong answer anywhere.

So the values here are written out again rather than imported and compared to
themselves, and what `run_git` actually hands to `git` is read off a shim
standing in for it. A test that asserted `GIT_SUBPROCESS_ENV == GIT_SUBPROCESS_ENV`
could not fail, and a test that asserted the module's own dict against the
module's own dict is the same test with more steps.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from istota.git_hardening import (
    GIT_HARDENING,
    GIT_SUBPROCESS_ENV,
    GIT_SUBPROCESS_ENV_UNSET,
    git_env,
    run_git,
)

# Written out, not imported. This is the restatement that makes the test able
# to fail when a variable is dropped from the constant.
EXPECTED_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
}

TEST_IDENTITY = {
    "GIT_AUTHOR_NAME": "Istota Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Istota Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def _plain_git(cwd: Path, *args: str) -> str:
    """The real git, for building fixtures. Not the thing under test."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd), capture_output=True, text=True,
        env={**os.environ, **EXPECTED_ENV, **TEST_IDENTITY},
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{proc.stderr}")
    return proc.stdout


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    _plain_git(path, "init", "-q", "-b", "main", ".")
    (path / "README").write_text("base\n")
    _plain_git(path, "add", "README")
    _plain_git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def shim(tmp_path, monkeypatch):
    """A `git` first on PATH that records its argv and environment.

    Records rather than asserts, because the question is what `run_git` handed
    over — reading the source of `run_git` answers a different one.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    argv_log = tmp_path / "argv.log"
    env_log = tmp_path / "env.log"
    script = bindir / "git"
    # A python shim writing JSON, not `env >> log`: `env` emits one line per
    # variable, so an inherited value containing a newline shifts every line
    # after it and a name reappearing as a fragment overwrites a real reading
    # silently. The paths are quoted because `tmp_path` may hold a space.
    script.write_text(
        "#!/bin/sh\n"
        f'exec {sys.executable} -c \'\nimport json, os, sys\n'
        f'open(sys.argv[1], "a").write(" ".join(sys.argv[3:]) + "\\n")\n'
        f'open(sys.argv[2], "w").write(json.dumps(dict(os.environ)))\n'
        f"\' \"{argv_log}\" \"{env_log}\" \"$@\"\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def read():
        return argv_log.read_text().strip(), json.loads(env_log.read_text())

    return read


class TestTheEnvironmentOverlay:
    def test_it_states_exactly_the_four_variables(self):
        assert dict(GIT_SUBPROCESS_ENV) == EXPECTED_ENV

    def test_it_cannot_be_mutated_in_place(self):
        """A shared safety constant that one caller can edit is one every other
        caller can silently lose a variable from."""
        with pytest.raises(TypeError):
            GIT_SUBPROCESS_ENV["GIT_TERMINAL_PROMPT"] = "1"  # type: ignore[index]

    def test_run_git_hands_all_four_to_the_subprocess(self, tmp_path, shim):
        run_git(tmp_path, "status")

        _, env = shim()
        for name, value in EXPECTED_ENV.items():
            assert env.get(name) == value, f"{name} did not reach the subprocess"

    def test_the_overlay_beats_a_hostile_process_environment(self, repo, monkeypatch):
        """`GIT_CONFIG_GLOBAL` and `GIT_CONFIG_NOSYSTEM` are only worth
        anything if they win over what the daemon was started with."""
        hostile = repo.parent / "hostile.gitconfig"
        hostile.write_text("[user]\n\tname = leaked-from-the-environment\n")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile))

        status, out = run_git(repo, "config", "--get", "user.name")

        assert status != 0
        assert "leaked-from-the-environment" not in out


class TestTheRedirectVariablesAreRemoved:
    """ISSUE-457: the overlay set four names and unset none.

    `GIT_DIR` and its relatives are read at a scope that outranks the `-C` a
    caller passes, so an inherited one silently points the command at another
    repository. The caller that matters is `worktree_reaper`, which removes
    worktrees — misdirection there is destructive rather than merely wrong.

    Restated below rather than imported, for the reason at the top of this
    file: a test comparing the module's tuple to itself cannot fail when a
    name is dropped from it.
    """

    def test_it_removes_exactly_these(self):
        assert list(GIT_SUBPROCESS_ENV_UNSET) == [
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_GRAFT_FILE",
            "GIT_EXTERNAL_DIFF",
        ]

    def test_the_two_halves_do_not_overlap(self):
        """What makes `git_env`'s pop-then-overlay order inert. Asserted rather
        than reasoned about, because the order is otherwise untestable and a
        name added to both lists would make it decide the answer."""
        assert not set(GIT_SUBPROCESS_ENV) & set(GIT_SUBPROCESS_ENV_UNSET)

    @pytest.mark.parametrize("name", GIT_SUBPROCESS_ENV_UNSET)
    def test_none_of_them_reaches_the_subprocess(self, name, tmp_path, shim, monkeypatch):
        monkeypatch.setenv(name, "/somewhere/else")

        run_git(tmp_path, "status")

        _, env = shim()
        assert name not in env, f"{name} was inherited by the git subprocess"

    def test_an_inherited_git_dir_does_not_redirect_the_command(
        self, repo, tmp_path, monkeypatch
    ):
        """The reported failure, against real git rather than the shim.

        Measured on git 2.55: with `GIT_DIR` set, `git -C <repo> rev-parse`
        answers with the *other* repository's directory.
        """
        other = tmp_path / "other"
        other.mkdir()
        _plain_git(other, "init", "-q", "-b", "main", ".")
        monkeypatch.setenv("GIT_DIR", str(other / ".git"))

        status, out = run_git(repo, "rev-parse", "--absolute-git-dir")

        assert status == 0, out
        assert Path(out.strip()).resolve() == (repo / ".git").resolve()

    def test_an_inherited_work_tree_does_not_redirect_the_command(
        self, repo, tmp_path, monkeypatch
    ):
        """The half that decides what `git worktree remove` and `git status`
        are looking at, which is the destructive direction."""
        other = tmp_path / "other"
        other.mkdir()
        (other / "README").write_text("a different tree entirely\n")
        monkeypatch.setenv("GIT_WORK_TREE", str(other))

        status, out = run_git(repo, "status", "--porcelain")

        assert status == 0, out
        assert out.strip() == "", f"status read another work tree: {out!r}"

    def test_an_inherited_graft_file_cannot_forge_a_merge(
        self, repo, tmp_path, monkeypatch
    ):
        """The destructive one. `worktree_reaper` authorises `worktree remove`
        on `merge-base --is-ancestor` and `rev-list --parents`, and a graft file
        rewrites parentage, so an inherited one turns an unmerged branch into a
        merged one and the sweep deletes the worktree and its branch ref.

        Measured on git 2.55, which still honours the file while calling it
        deprecated.
        """
        _plain_git(repo, "checkout", "-q", "-b", "side")
        (repo / "README").write_text("side work\n")
        _plain_git(repo, "commit", "-q", "-am", "side")
        side = _plain_git(repo, "rev-parse", "HEAD").strip()
        _plain_git(repo, "checkout", "-q", "main")
        (repo / "README").write_text("main work\n")
        _plain_git(repo, "commit", "-q", "-am", "main")
        main = _plain_git(repo, "rev-parse", "HEAD").strip()

        graft = tmp_path / "grafts"
        graft.write_text(f"{main} {side}\n")
        monkeypatch.setenv("GIT_GRAFT_FILE", str(graft))

        status, _ = run_git(repo, "merge-base", "--is-ancestor", side, main)

        assert status != 0, "an unmerged branch was reported as merged"

    def test_a_replace_ref_cannot_forge_a_merge(self, repo):
        """The same forgery from the repository side, which needs no hostile
        environment at all — a checkout under `developer.repos_dir` is bound
        read-write into the sandbox, so the model can write `refs/replace/*`
        itself. `--no-replace-objects` in `GIT_HARDENING` is what refuses it.
        """
        _plain_git(repo, "checkout", "-q", "-b", "side")
        (repo / "README").write_text("side work\n")
        _plain_git(repo, "commit", "-q", "-am", "side")
        side = _plain_git(repo, "rev-parse", "HEAD").strip()
        _plain_git(repo, "checkout", "-q", "main")
        (repo / "README").write_text("main work\n")
        _plain_git(repo, "commit", "-q", "-am", "main")
        main = _plain_git(repo, "rev-parse", "HEAD").strip()

        forged = _plain_git(
            repo, "commit-tree", f"{main}^{{tree}}", "-p", side, "-m", "forged"
        ).strip()
        _plain_git(repo, "update-ref", f"refs/replace/{main}", forged)

        status, _ = run_git(repo, "merge-base", "--is-ancestor", side, main)

        assert status != 0, "an unmerged branch was reported as merged"

    def test_an_inherited_external_diff_is_not_executed(
        self, repo, tmp_path, monkeypatch
    ):
        """`GIT_EXTERNAL_DIFF` is not in the `GIT_DIR` family and is here for a
        measured reason: it beats the `-c diff.external=` override in
        `GIT_HARDENING`, which that list names as one of its run-a-command
        defences. Removing the variable is what makes that claim true.
        """
        marker = tmp_path / "external-diff-ran.txt"
        script = tmp_path / "ext.sh"
        script.write_text(f"#!/bin/sh\necho ran > {marker}\n")
        script.chmod(0o755)
        (repo / "README").write_text("changed\n")
        monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(script))

        run_git(repo, "diff")

        assert not marker.exists(), "GIT_EXTERNAL_DIFF ran a program of its own choosing"

    def test_the_rest_of_the_environment_still_reaches_git(
        self, tmp_path, shim, monkeypatch
    ):
        """The control. `run_git` overlays `os.environ`; a fix that built the
        environment from nothing instead would pass every test above and take
        `PATH`, the proxy settings and the CA bundle with it."""
        monkeypatch.setenv("ISTOTA_UNRELATED_MARKER", "kept")

        run_git(tmp_path, "status")

        _, env = shim()
        assert env.get("ISTOTA_UNRELATED_MARKER") == "kept"
        assert env.get("PATH")


class TestTheEnvironmentBuilder:
    """`git_env` is the whole policy in one call, which is what keeps a second
    consumer from applying half of it."""

    def test_it_applies_the_overlay_and_the_removals_together(self, monkeypatch):
        monkeypatch.setenv("GIT_DIR", "/somewhere/else")
        monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")

        env = git_env()

        assert "GIT_DIR" not in env
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_it_returns_a_fresh_mapping_each_time(self):
        """A caller adding one variable of its own must not edit the process
        environment, which is what `os.environ` itself would give it.

        The injected name is in neither half on purpose. A name this function
        removes could not fail here: against `env = os.environ` the write would
        land in the real environment and the next call's `pop` would take it
        straight back out, so both assertions would hold with the defect present.
        """
        first = git_env()
        first["ISTOTA_FRESHNESS_MARKER"] = "injected"

        assert "ISTOTA_FRESHNESS_MARKER" not in git_env()
        assert "ISTOTA_FRESHNESS_MARKER" not in os.environ


class TestTheHardeningOverrides:
    def test_the_overrides_lead_and_the_repository_follows(self, tmp_path, shim):
        """`-c` before `-C`: a later `-c` beats the repository's own value, so
        an override placed after the repository is one the repository wins."""
        run_git(tmp_path / "somewhere", "status")

        argv, _ = shim()
        assert argv.startswith(" ".join(GIT_HARDENING))
        assert f"-C {tmp_path / 'somewhere'} status" in argv

    def test_a_repo_configured_fsmonitor_is_not_executed(self, repo, tmp_path):
        marker = tmp_path / "pwned.txt"
        hook = tmp_path / "fsmonitor.sh"
        hook.write_text(f"#!/bin/sh\necho pwned > {marker}\n")
        hook.chmod(0o755)
        _plain_git(repo, "config", "core.fsmonitor", str(hook))

        run_git(repo, "status", "--porcelain")

        assert not marker.exists(), "core.fsmonitor ran: GIT_HARDENING is not applied"

    def test_a_status_does_not_rewrite_the_index(self, repo):
        """`GIT_OPTIONAL_LOCKS=0`, verified rather than asserted. The index
        mtime is the worktree reaper's idle clock; a sweep that stamps it reaps
        nothing after the first pass.

        The `utime` is what makes this able to fail. `git status` rewrites the
        index only when the stat data it cached has gone stale, so against a
        just-committed tree it would leave the index alone with or without the
        variable, and the assertion would hold for the wrong reason.
        """
        stale = (repo / "README").stat().st_mtime - 3600
        os.utime(repo / "README", (stale, stale))
        before = (repo / ".git" / "index").stat().st_mtime_ns

        run_git(repo, "status", "--porcelain")

        assert (repo / ".git" / "index").stat().st_mtime_ns == before


class TestTheOutputContract:
    def test_stdout_only_by_default(self, repo):
        status, out = run_git(repo, "rev-parse", "--verify", "no-such-ref")

        assert status != 0
        assert out == ""

    def test_merge_stderr_carries_gits_own_diagnosis(self, repo):
        status, out = run_git(
            repo, "rev-parse", "--verify", "no-such-ref", merge_stderr=True
        )

        assert status != 0
        assert "fatal:" in out, "stderr was dropped"

    def test_non_utf8_output_is_decoded_rather_than_rejected(self, tmp_path, monkeypatch):
        """`text=True` would raise `UnicodeDecodeError` — a `ValueError`, caught
        by neither handler — and abort a sweep from inside a helper every caller
        treats as total."""
        bindir = tmp_path / "bin"
        bindir.mkdir()
        script = bindir / "git"
        script.write_text("#!/bin/sh\nprintf 'bad\\377name\\n'\n")
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

        status, out = run_git(tmp_path, "status")

        assert status == 0
        assert out.startswith("bad")
        assert out.encode("utf-8", "surrogateescape") == b"bad\xffname\n"


class TestItNeverRaises:
    """Every caller is a never-raises path — a cleanup sweep, a relocation
    reporting what it could not do. A missing `git` must come back as a status."""

    @pytest.fixture
    def no_git(self, tmp_path, monkeypatch):
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))

    def test_a_missing_git_is_a_status_not_an_exception(self, tmp_path, no_git):
        assert run_git(tmp_path, "status") == (1, "")

    def test_on_error_none_returns_the_exceptions_own_message(self, tmp_path, no_git):
        status, out = run_git(tmp_path, "status", on_error=None)

        assert status == 1
        assert out, "on_error=None must carry the reason, not an empty string"

    def test_a_timeout_is_a_status_not_an_exception(self, tmp_path, monkeypatch):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        script = bindir / "git"
        script.write_text("#!/bin/sh\nexec sleep 30\n")
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

        assert run_git(tmp_path, "status", timeout=0.5) == (1, "")


class TestTheOverlayIsStatedOnce:
    """The grep guard. F16 is four hand-rolled copies of one env dict; this is
    what fails when a fifth appears.

    Two or more of the four names in one file is a restatement of the overlay.
    One on its own is not — `forge_cli` carries `GIT_TERMINAL_PROMPT` in an
    allowlist of variables to pass through to a forge subprocess, which is a
    different mechanism, and a guard that exempted it by name would go blind to
    a copy that later grew inside it.

    Parsed rather than grepped, for two reasons that pull in opposite
    directions. A substring search over the text misses
    `dict(GIT_TERMINAL_PROMPT="0")` and anything built from a variable, which
    is a form a fifth copy is as likely to take as a quoted key. And it hits
    every docstring that merely *names* a variable, of which this change added
    several — so the naive widening that fixes the first problem makes the
    guard fire on prose. Matching a string constant exactly, a keyword argument,
    or an assignment target does both.
    """

    @staticmethod
    def _names_stated_as_code(path: Path) -> list[str]:
        names = set(EXPECTED_ENV)
        found = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and node.value in names:
                found.add(node.value)
            elif isinstance(node, ast.keyword) and node.arg in names:
                found.add(node.arg)
            elif isinstance(node, ast.Name) and node.id in names:
                found.add(node.id)
        return sorted(found)

    def test_the_matcher_sees_an_unquoted_restatement(self, tmp_path):
        """The guard's own negative control. A guard that could only see one
        spelling would report green against the other."""
        quoted = tmp_path / "quoted.py"
        quoted.write_text('E = {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}\n')
        kwargs = tmp_path / "kwargs.py"
        kwargs.write_text('E = dict(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")\n')
        prose = tmp_path / "prose.py"
        prose.write_text('"""GIT_TERMINAL_PROMPT and GIT_OPTIONAL_LOCKS are set."""\n')

        assert len(self._names_stated_as_code(quoted)) == 2
        assert len(self._names_stated_as_code(kwargs)) == 2
        assert self._names_stated_as_code(prose) == []

    def test_no_module_restates_the_overlay(self):
        src = Path(__file__).resolve().parents[1] / "src" / "istota"
        offenders = {}
        for path in sorted(src.rglob("*.py")):
            hits = self._names_stated_as_code(path)
            if len(hits) >= 2:
                offenders[path.relative_to(src).as_posix()] = hits

        assert offenders == {
            "git_hardening.py": sorted(EXPECTED_ENV),
        }, f"a second copy of the git environment overlay: {offenders}"
