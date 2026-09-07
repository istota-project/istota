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

import os
import subprocess
from pathlib import Path

import pytest

from istota.git_hardening import GIT_HARDENING, GIT_SUBPROCESS_ENV, run_git

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
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {argv_log}\n'
        f"env >> {env_log}\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def read():
        env = {}
        for line in env_log.read_text().splitlines():
            key, _, value = line.partition("=")
            env[key] = value
        return argv_log.read_text().strip(), env

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
        script.write_text("#!/bin/sh\nsleep 30\n")
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
    """

    def test_no_module_restates_the_overlay(self):
        names = tuple(EXPECTED_ENV)
        src = Path(__file__).resolve().parents[1] / "src" / "istota"
        offenders = {}
        for path in sorted(src.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            hits = [n for n in names if f'"{n}"' in text or f"'{n}'" in text]
            if len(hits) >= 2:
                offenders[path.relative_to(src).as_posix()] = hits

        assert offenders == {
            "git_hardening.py": list(names),
        }, f"a second copy of the git environment overlay: {offenders}"
