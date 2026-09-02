"""What each `SandboxProfile` actually puts in the namespace, on a real kernel.

`tests/test_sandbox.py::TestSandboxProfiles` asserts that the Claude runtime
paths are absent from the NATIVE *argv*. That is the strongest thing darwin can
say, and it is an assertion about a command line rather than about a boundary:
a mount plan can be right and the namespace still wrong, which is why every
containment claim in this repo is made here instead.

The claim under test is ISSUE-389's: a sandboxed `NativeBrain` Bash call could
`cat "$HOME/.claude/.credentials.json"` and get the subscription token back as
a tool result, because `build_bwrap_cmd` bound the Claude CLI's runtime block
unconditionally. Read-only stopped the token being rewritten, never read.

**The positive control is the point of the file.** "The credential cannot be
read under NATIVE" passes perfectly on a sandbox that mounted nothing at all,
on a `$HOME` with no Claude install, and on a probe that never ran. So every
absence below is paired with the same probe under CLAUDE, where the sentinel
must come back — and `run_probe` (reused from `test_sandbox_real.py`, including
its `assert cmd[0] == "bwrap"` guard) fails rather than returning if bwrap
declined to build a command.

Run with `scripts/test-linux.sh`. Carries the `linux` marker.
"""

import os
import shlex
import sys
from pathlib import Path

import pytest

from istota import db
from istota.config import SecurityConfig
from istota.executor import SandboxProfile, _bwrap_available, build_bwrap_cmd

from .test_sandbox_real import _unavailable, run_probe

pytestmark = pytest.mark.linux

#: What the credential sentinel contains. Distinctive enough that finding it in
#: a probe's stdout cannot be a coincidence, and it is not a credential shape.
CREDENTIAL_SENTINEL = "sentinel-not-a-real-token"


@pytest.fixture(autouse=True)
def _requires_real_bwrap():
    """Same gate as `test_sandbox_real.py`, restated because a fixture is not
    inherited across modules."""
    if sys.platform != "linux":
        _unavailable("needs a real Linux kernel")
    if not _bwrap_available():
        _unavailable("needs a bubblewrap that can create namespaces")


def _q(path):
    return shlex.quote(str(path))


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    """A HOME carrying a sentinel at every path the Claude runtime block binds.

    `build_bwrap_cmd` reads `$HOME` at call time and `_ro_bind` skips a source
    that does not exist, so without this the whole file would pass on a host
    with no Claude install by asserting the absence of mounts that were never
    emitted. The positive control below is what proves that has not happened.
    """
    home = tmp_path / "home"
    for d in (
        ".local/bin", ".local/share/claude", ".local/state/claude",
        ".claude/projects", ".claude/debug", ".claude/todos",
    ):
        (home / d).mkdir(parents=True)
    (home / ".local" / "bin" / "claude").write_text("#!/bin/sh\necho claude\n")
    (home / ".local" / "share" / "claude" / "versions").write_text("1.2.3\n")
    (home / ".local" / "state" / "claude" / "lock").write_text("lock\n")
    (home / ".claude" / ".credentials.json").write_text(
        '{"token": "%s"}\n' % CREDENTIAL_SENTINEL
    )
    (home / ".claude" / "settings.json").write_text('{"settings": "sentinel"}\n')
    (home / ".claude" / "projects" / "note.txt").write_text("session-sentinel\n")
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def layout(tmp_path, make_config):
    db_dir = tmp_path / "app" / "data"
    db_dir.mkdir(parents=True)
    (db_dir / "istota.db").write_text("framework-db-contents")

    mount = tmp_path / "mount"
    (mount / "Users" / "alice").mkdir(parents=True)

    return make_config(
        db_path=db_dir / "istota.db",
        module_data_dir=tmp_path / "app" / "moduledbs",
        nextcloud_mount_path=mount,
        temp_dir=tmp_path / "temp",
        security=SecurityConfig(sandbox_enabled=True),
    )


@pytest.fixture
def user_temp(layout):
    d = layout.temp_dir / "alice"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def task():
    return db.Task(
        id=1, prompt="probe", user_id="alice", source_type="talk",
        status="running", conversation_token=None,
    )


def _sentinel_probe(claude_home):
    """`stat`, `cat` and `touch` against every Claude runtime path.

    Three verbs rather than one: read-only is what the CLAUDE profile grants
    the credential, so a test that only tried to write would report a boundary
    where there is none — that difference is the whole bug.
    """
    targets = {
        "BIN": claude_home / ".local" / "bin" / "claude",
        "SHARE": claude_home / ".local" / "share" / "claude" / "versions",
        "STATE": claude_home / ".local" / "state" / "claude" / "lock",
        "CREDS": claude_home / ".claude" / ".credentials.json",
        "SETTINGS": claude_home / ".claude" / "settings.json",
        "PROJECTS": claude_home / ".claude" / "projects" / "note.txt",
    }
    parts = []
    for label, path in targets.items():
        parts.append(
            f'stat {_q(path)} >/dev/null 2>&1 && echo "{label}_STAT_OK" '
            f'|| echo "{label}_STAT_FAIL"'
        )
        # `echo` with the body in a substitution, never `… | sed -e "s/^/L=/"`.
        # sed is line-oriented: given *zero* input lines it emits nothing at
        # all rather than an empty labelled line, so the absent-file case —
        # which is the whole NATIVE assertion — produced no marker and
        # `"{label}_BODY=;" in out` was false for every label. Measured:
        # `printf '' | sed -e 's/^/X=/'` writes no bytes.
        parts.append(
            f'echo "{label}_BODY=$(cat {_q(path)} 2>/dev/null | tr -d "\\n");"'
        )
    # A write into the tmpfs base, which under CLAUDE is a writable mount and
    # under NATIVE is not in the namespace at all.
    claude_dir = claude_home / ".claude"
    parts.append(
        f'touch {_q(claude_dir / "planted")} 2>/dev/null && echo DIR_WRITE_OK '
        f"|| echo DIR_WRITE_FAIL"
    )
    return "; ".join(parts)


class TestTheClaudeProfileIsThePositiveControl:
    """Without this class the file passes on a sandbox that mounted nothing."""

    def test_the_credential_sentinel_comes_back_under_claude(
        self, layout, task, user_temp, claude_home,
    ):
        result = run_probe(
            _sentinel_probe(claude_home), layout, task, user_temp,
            profile=SandboxProfile.CLAUDE,
        )
        out = result.stdout

        assert "CREDS_STAT_OK" in out, (out, result.stderr)
        assert CREDENTIAL_SENTINEL in out, (out, result.stderr)
        # And the rest of the block, so "the CLAUDE profile works" is not one
        # bind standing in for six.
        for label in ("BIN", "SHARE", "STATE", "SETTINGS", "PROJECTS"):
            assert f"{label}_STAT_OK" in out, (label, out)

    def test_the_credential_is_read_only_under_claude(
        self, layout, task, user_temp, claude_home,
    ):
        """Which is the shape of the bug: read-only never stopped a read."""
        creds = claude_home / ".claude" / ".credentials.json"
        result = run_probe(
            f'echo tampered > {_q(creds)} 2>/dev/null && echo WRITE_OK '
            f"|| echo WRITE_FAIL",
            layout, task, user_temp, profile=SandboxProfile.CLAUDE,
        )
        assert "WRITE_FAIL" in result.stdout, result.stdout
        assert creds.read_text().strip().endswith('"}'), "the host file was rewritten"


class TestTheNativeProfileHasNoneOfIt:
    def test_no_claude_runtime_path_can_be_stat_read_or_written(
        self, layout, task, user_temp, claude_home,
    ):
        result = run_probe(
            _sentinel_probe(claude_home), layout, task, user_temp,
            profile=SandboxProfile.NATIVE,
        )
        out = result.stdout

        for label in ("BIN", "SHARE", "STATE", "CREDS", "SETTINGS", "PROJECTS"):
            assert f"{label}_STAT_FAIL" in out, (label, out, result.stderr)
            assert f"{label}_STAT_OK" not in out, (label, out)
            assert f"{label}_BODY=;" in out, (label, out)
        assert CREDENTIAL_SENTINEL not in out, out
        assert "DIR_WRITE_FAIL" in out, out

    def test_the_probe_really_ran_under_native(
        self, layout, task, user_temp, claude_home,
    ):
        """The control for the control.

        Every assertion above is an absence, and a probe that failed to start
        produces the same empty stdout. So: something the NATIVE namespace does
        contain has to come back on the same run.
        """
        alice = Path(layout.nextcloud_mount_path) / "Users" / "alice"
        (alice / "mine.txt").write_text("alice-can-read-this\n")
        result = run_probe(
            f"{_sentinel_probe(claude_home)}; cat {_q(alice / 'mine.txt')}",
            layout, task, user_temp, profile=SandboxProfile.NATIVE,
        )

        assert result.returncode == 0, (result.stdout, result.stderr)
        assert "alice-can-read-this" in result.stdout, result.stdout
        assert CREDENTIAL_SENTINEL not in result.stdout, result.stdout

    def test_the_home_directory_itself_holds_nothing_of_claudes(
        self, layout, task, user_temp, claude_home,
    ):
        """Path by path is what the profile gate is written in terms of; this
        asks the namespace instead, so a future path added to the block and not
        to `_sentinel_probe` is still caught."""
        home_entries = f'ls -A {_q(claude_home)} 2>/dev/null | tr "\\n" " "'
        local_entries = f'ls -A {_q(claude_home / ".local")} 2>/dev/null | tr "\\n" " "'
        result = run_probe(
            f'echo "home=[$({home_entries})]"; echo "local=[$({local_entries})]"',
            layout, task, user_temp, profile=SandboxProfile.NATIVE,
        )
        out = result.stdout

        assert "home=[]" in out, (out, result.stderr)
        assert "local=[]" in out, (out, result.stderr)


class TestTheGenericPlanIsIdenticalUnderBothProfiles:
    """The masks, the per-user binds and `/bin/sh` itself — the half of the plan
    the split must not have touched.

    `doctor`'s `sandbox.masks` check moved to the NATIVE profile with this
    change, and its verdict is only unchanged if this holds.
    """

    def _mask_probe(self, layout):
        db_dir = Path(layout.db_path).parent
        return (
            f'cd {_q(db_dir)} 2>/dev/null && echo PRESENT || echo ABSENT; '
            f'echo "entries=[$(ls -A {_q(db_dir)} 2>&1)]"; '
            f'cat {_q(layout.db_path)} 2>/dev/null && echo READ_OK || echo READ_FAIL; '
            f'touch {_q(db_dir / "probe")} 2>/dev/null && echo WRITE_OK '
            f"|| echo WRITE_FAIL"
        )

    def test_the_database_masks_hold_under_both(
        self, layout, task, user_temp, claude_home,
    ):
        outputs = {}
        for profile in SandboxProfile:
            result = run_probe(
                self._mask_probe(layout), layout, task, user_temp, profile=profile,
            )
            outputs[profile] = result.stdout
            assert "PRESENT" in result.stdout, (profile, result.stderr)
            assert "entries=[]" in result.stdout, (profile, result.stdout)
            assert "READ_FAIL" in result.stdout, (profile, result.stdout)
            assert "WRITE_FAIL" in result.stdout, (profile, result.stdout)
            assert "framework-db-contents" not in result.stdout, profile

        # Byte for byte: this is the `doctor` verdict-unchanged claim, made
        # against the namespace rather than against the argv.
        assert outputs[SandboxProfile.CLAUDE] == outputs[SandboxProfile.NATIVE]

    def test_the_users_own_directory_is_writable_under_both(
        self, layout, task, user_temp, claude_home,
    ):
        alice = Path(layout.nextcloud_mount_path) / "Users" / "alice"
        for profile in SandboxProfile:
            result = run_probe(
                f'touch {_q(alice / f"probe-{profile.value}")} 2>/dev/null '
                f"&& echo WRITE_OK || echo WRITE_FAIL",
                layout, task, user_temp, profile=profile,
            )
            assert "WRITE_OK" in result.stdout, (profile, result.stderr)

    def test_another_users_directory_is_absent_under_both(
        self, layout, task, user_temp, claude_home,
    ):
        bob = Path(layout.nextcloud_mount_path) / "Users" / "bob"
        bob.mkdir(parents=True)
        (bob / "secret.txt").write_text("bobs-private-bytes")
        for profile in SandboxProfile:
            result = run_probe(
                f'cat {_q(bob / "secret.txt")} 2>/dev/null || echo ABSENT',
                layout, task, user_temp, profile=profile,
            )
            assert "ABSENT" in result.stdout, (profile, result.stdout)
            assert "bobs-private-bytes" not in result.stdout, profile


class TestTheProfileIsRequiredHereToo:
    def test_no_profile_is_a_typeerror_before_any_namespace_is_built(
        self, layout, task, user_temp,
    ):
        """Restated on the tier that runs bwrap for real: the failure has to be
        a `TypeError` at the call site, not a namespace built with a default."""
        with pytest.raises(TypeError):
            build_bwrap_cmd(
                ["/bin/sh", "-c", "true"], layout, task, False, [], user_temp,
            )

    def test_a_native_namespace_still_runs_the_command(
        self, layout, task, user_temp, claude_home,
    ):
        """`~/.local/bin` is gone under NATIVE, so the interpreter has to come
        from the system binds. If it did not, every assertion in this file about
        an absence would be reporting a sandbox that cannot exec anything."""
        result = run_probe(
            "echo NATIVE_RAN; id -u >/dev/null && echo EXEC_OK",
            layout, task, user_temp, profile=SandboxProfile.NATIVE,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert "NATIVE_RAN" in result.stdout, result.stdout
        assert "EXEC_OK" in result.stdout, result.stdout


def test_the_sentinel_home_is_real(claude_home):
    """A guard on the fixture rather than on the product: every absence in this
    file is only meaningful if these files exist on the host."""
    assert (claude_home / ".claude" / ".credentials.json").exists()
    assert CREDENTIAL_SENTINEL in (
        claude_home / ".claude" / ".credentials.json"
    ).read_text()
    assert os.environ["HOME"] == str(claude_home)


def test_the_probe_helper_refuses_an_unsandboxed_run(layout, task, user_temp):
    """`run_probe`'s own guard, restated where a reader of this file will see
    it: `build_bwrap_cmd` returns the command unchanged when the sandbox is
    unavailable, and a probe that ran on the host would satisfy every
    "the credential is unreachable" assertion by reading the wrong machine."""
    from unittest.mock import patch

    with patch("istota.executor._bwrap_available", return_value=False), \
         pytest.raises(AssertionError, match="sandbox unavailable"):
        run_probe("true", layout, task, user_temp, profile=SandboxProfile.NATIVE)
