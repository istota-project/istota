"""The one place istota decides how a command string becomes a shell argv.

The property under test is not "the flag is in the list" — that would be
satisfied by `-o pipefail` sitting somewhere the shell ignores it. It is that
the argv, run, hands back the status of the command that failed. So most of
these execute what `shell_argv` returned.
"""

import shutil
import subprocess

import pytest

from istota.shell_exec import POSIX_SH, shell_argv


class TestShellArgv:
    def test_uses_bash_with_pipefail_when_bash_is_available(self):
        argv = shell_argv("echo hi", bash="/usr/bin/bash")
        assert argv == ["/usr/bin/bash", "-o", "pipefail", "-c", "echo hi"]

    def test_an_explicit_name_is_used_verbatim(self):
        """The sandboxed caller passes a bare name on purpose.

        `session/tools/bash.py` runs its argv inside bubblewrap, which binds
        `/usr` but need not reproduce the host's `/bin` symlink — so an absolute
        path probed on the host is not necessarily a path in the namespace where
        the command runs. PATH resolution is what works there and is what that
        caller has always relied on.
        """
        assert shell_argv("echo hi", bash="bash")[0] == "bash"

    def test_falls_back_to_posix_sh_when_there_is_no_bash(self):
        """Debian's `/bin/sh` is dash, which has no `pipefail`.

        The fallback is the *pre-existing* behaviour rather than a degraded one:
        every caller was `shell=True` before this module existed, which is
        `/bin/sh -c`. A host without bash therefore loses nothing it had.
        """
        assert shell_argv("echo hi", bash="") == [POSIX_SH, "-c", "echo hi"]

    def test_the_command_stays_one_argv_element(self):
        """Never split, never re-quoted — the shell does that."""
        command = "echo 'a b'  |  tail -1 && printf '%s\\n' \"$HOME\""
        argv = shell_argv(command, bash="bash")
        assert argv[-1] == command
        assert argv.count(command) == 1

    def test_probes_the_path_when_no_interpreter_is_named(self):
        argv = shell_argv("echo hi")
        assert argv[-2:] == ["-c", "echo hi"]
        if shutil.which("bash"):
            assert argv[1:3] == ["-o", "pipefail"]
        else:
            assert argv[0] == POSIX_SH


class TestTheArgvActuallyBehaves:
    """Run what the builder returned. A flag check cannot make these claims."""

    @pytest.fixture(autouse=True)
    def _needs_bash(self):
        if not shutil.which("bash"):
            pytest.skip("no bash on this host")

    def _rc(self, command: str) -> int:
        return subprocess.run(
            shell_argv(command), capture_output=True, timeout=30,
        ).returncode

    def test_a_failing_stage_is_the_pipelines_status(self):
        assert self._rc("false | tail -1") != 0

    def test_a_succeeding_pipeline_is_still_zero(self):
        """Control. Without it a shell that failed everything would pass above."""
        assert self._rc("echo hi | tail -1") == 0

    def test_a_plain_successful_command_is_unaffected(self):
        assert self._rc("echo hi") == 0

    def test_the_option_is_really_set_in_the_shell_that_runs(self):
        out = subprocess.run(
            shell_argv("set -o | grep pipefail"),
            capture_output=True, text=True, timeout=30,
        )
        assert out.stdout.split() == ["pipefail", "on"], out.stdout
