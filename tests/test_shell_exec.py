"""The one place istota decides how a command string becomes a shell argv.

The property under test is not "the flag is in the list" — that would be
satisfied by `-o pipefail` sitting somewhere the shell ignores it. It is that
the argv, run, hands back the status of the command that failed. So most of
these execute what `shell_argv` returned.
"""

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from istota import shell_exec
from istota.shell_exec import (
    PIPEFAIL_SHELLOPTS,
    SHELLOPTS_VAR,
    SIGPIPE_NOTE,
    POSIX_SH,
    is_sigpipe_failure,
    pipefail_env,
    shell_argv,
)


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


class TestTheFallbackAnnouncesItself:
    """A host with no bash gets the old behaviour — and the old *silence*.

    That is the failure this module exists to remove, so the one case where it
    cannot deliver has to say so. An operator who read the changelog otherwise
    believes failing pipelines are reported when they are not.
    """

    def test_falling_back_warns(self, caplog):
        shell_exec._fallback_warned = False
        with caplog.at_level(logging.WARNING, logger="istota.shell_exec"):
            shell_exec.shell_argv("echo hi", bash="")
        assert "pipefail" in caplog.text
        assert POSIX_SH in caplog.text

    def test_it_warns_once_per_process_not_once_per_command(self, caplog):
        """This runs on every cron tick and every heartbeat check."""
        shell_exec._fallback_warned = False
        with caplog.at_level(logging.WARNING, logger="istota.shell_exec"):
            for _ in range(5):
                shell_exec.shell_argv("echo hi", bash="")
        assert len([r for r in caplog.records if "pipefail" in r.getMessage()]) == 1

    def test_the_working_path_is_silent(self, caplog):
        shell_exec._fallback_warned = False
        with caplog.at_level(logging.WARNING, logger="istota.shell_exec"):
            shell_exec.shell_argv("echo hi", bash="bash")
        assert caplog.text == ""


class TestIsSigpipeFailure:
    def test_recognises_a_message_carrying_the_note(self):
        assert is_sigpipe_failure(f"Exit code 141. {SIGPIPE_NOTE}")

    def test_an_ordinary_failure_is_not_sigpipe(self):
        assert not is_sigpipe_failure("Exit code 1")
        assert not is_sigpipe_failure("boom: no such file")

    def test_a_bare_141_without_the_note_is_not_claimed(self):
        """The predicate keys on this module's own annotation, not on a number.

        A command that genuinely exits 141 of its own accord is a real failure,
        and the producing call site is what decides whether SIGPIPE is the
        explanation. Matching the digits here would excuse it from a retry it
        deserves.
        """
        assert not is_sigpipe_failure("Exit code 141")

    def test_empty_and_none_are_safe(self):
        assert not is_sigpipe_failure("")
        assert not is_sigpipe_failure(None)


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


class TestPipefailEnv:
    """The env-side half of the same rule (ISSUE-321).

    `shell_argv` can only fix a shell istota spawns itself. The Claude Code CLI
    spawns its own Bash tool, so the option has to arrive through the
    environment the CLI inherits.
    """

    def test_it_names_shellopts(self):
        assert pipefail_env() == {SHELLOPTS_VAR: PIPEFAIL_SHELLOPTS}

    def test_it_returns_a_fresh_dict_each_call(self):
        """Callers merge it into an env they then mutate."""
        first = pipefail_env()
        first["SOMETHING"] = "else"
        assert "SOMETHING" not in pipefail_env()

    def test_the_value_names_only_shell_options(self):
        """The reason this is SHELLOPTS and not BASH_ENV.

        BASH_ENV names a *file* bash sources before every non-interactive
        shell, which is why `executor._SHELL_STARTUP_ENV_VARS` strips it. Bash
        parses SHELLOPTS as a colon-separated list of option names and rejects
        anything else as an invalid option name, so it cannot carry code.
        """
        assert re.fullmatch(r"[a-z_]+(:[a-z_]+)*", PIPEFAIL_SHELLOPTS)


class TestTheEnvActuallyBehaves:
    """Run a real bash under the env the helper returns.

    Same reasoning as `TestTheArgvActuallyBehaves`: the property is not "the
    variable is in the dict", it is that a bash started with that variable
    reports the failing stage. These would all have passed before the fix if
    they asserted on the dict alone.
    """

    @pytest.fixture(autouse=True)
    def _needs_bash(self):
        if not shutil.which("bash"):
            pytest.skip("no bash on this host")

    def _run(self, command: str, *, argv: list[str] | None = None):
        return subprocess.run(
            argv or ["bash", "-c", command],
            env={**os.environ, **pipefail_env()},
            capture_output=True, text=True, timeout=30,
        )

    def test_a_failing_stage_is_the_pipelines_status(self):
        """The reported bug, reproduced through the env rather than the argv.

        Pre-fix this is 0: `bash -c` starts with pipefail off, and nothing in
        the environment turned it on.
        """
        assert self._run("false | head -1").returncode != 0

    def test_a_succeeding_pipeline_is_still_zero(self):
        """Control. Without it a shell that failed everything would pass above."""
        assert self._run("echo hi | head -1").returncode == 0

    def test_the_option_is_really_set_in_the_shell_that_runs(self):
        out = self._run("set -o | grep pipefail")
        assert out.stdout.split() == ["pipefail", "on"], out.stdout

    def test_it_survives_a_sourced_shell_snapshot(self):
        """The shape the CLI's Bash tool actually runs.

        Claude Code invokes `bash -c 'source <shell-snapshot> && eval <cmd>'`.
        The snapshot restores functions, aliases and PATH, and the real ones on
        the machine where this was written contain no `set -o` or `shopt` line,
        so the environment's option survives being sourced.

        **What this pins is the shape, not the artifact.** The fixture below is
        three lines written by the test, and the snapshot is a third-party file
        this repo neither generates nor version-pins — so a future Claude Code
        that emitted `set +o pipefail` into one would revert the fix with this
        test still green. The snapshots inspected were also all zsh-derived; a
        bash-derived one, which is what a Debian daemon user's login shell
        would produce, has not been looked at. Recorded as a known limit of the
        evidence rather than dressed up: the honest version of this test is a
        live task asserting on its own shell, which needs a real model.
        """
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "snapshot.sh"
            snapshot.write_text(
                "greet() { echo hi; }\nalias ll='ls -l'\nexport PATH=\"$PATH\"\n",
                encoding="utf-8",
            )
            out = self._run(f"source '{snapshot}' >/dev/null 2>&1; false | head -1")
        assert out.returncode != 0

    def test_it_reaches_a_nested_bash_script(self):
        """Deeper than `-o pipefail` on one argv reaches.

        ISSUE-307 recorded that the flag stops at one shell, so a pipeline
        inside a `bash script.sh` was unguarded again. An environment variable
        is inherited, so it is not.

        Three things this needs to be worth running, all of them found in
        review. The script has to be invoked *from* a shell, or it is the
        top-level bash the class already covers and nothing is nested. The
        interpreter comes from `shutil.which`, because a hardcoded
        `#!/bin/bash` on a host with only `/opt/homebrew/bin/bash` fails to
        exec and returns 126, which is `!= 0` and green for the wrong reason.
        And it takes both controls below: a nested pipeline that succeeds, and
        the same nesting under `-o pipefail` with no environment, which is the
        contrast the docstring rests on.
        """
        bash = shutil.which("bash")
        with tempfile.TemporaryDirectory() as tmp:
            failing = self._nested_script(tmp, "failing.sh", "false | head -1", bash)
            passing = self._nested_script(tmp, "passing.sh", "true | head -1", bash)

            assert self._run(f"{bash} {failing}").returncode != 0
            assert self._run(f"{bash} {passing}").returncode == 0, (
                "control: a good nested pipeline must still pass"
            )

            # The contrast: the flag on the outer argv does not descend.
            flagged = subprocess.run(
                [bash, "-o", "pipefail", "-c", f"{bash} {failing}"],
                env={k: v for k, v in os.environ.items() if k != SHELLOPTS_VAR},
                capture_output=True, text=True, timeout=30,
            )
            assert flagged.returncode == 0, (
                "control: -o pipefail is supposed to stop at one shell; if this "
                "fails the test is measuring nothing"
            )

    @staticmethod
    def _nested_script(tmp: str, name: str, body: str, bash: str | None) -> Path:
        script = Path(tmp) / name
        script.write_text(f"#!{bash}\n{body}\n", encoding="utf-8")
        script.chmod(0o755)
        return script

    def test_a_command_can_still_opt_out_and_back_in(self):
        """The escape hatch, pinned — and it is the *toggle* that is measured.

        Asserting only that `set +o pipefail` yields rc=0 would pass against
        the pre-fix shell, where the option was off to begin with and the line
        did nothing. So this walks all three states in one shell: on by
        inheritance, off after `+o`, on again after `-o`.

        SHELLOPTS itself is readonly in bash, so a command cannot unset the
        variable; `set +o pipefail` is the supported way to want the old
        behaviour for one pipeline.
        """
        out = self._run(
            "false | head -1; echo a=$?; "
            "set +o pipefail; false | head -1; echo b=$?; "
            "set -o pipefail; false | head -1; echo c=$?"
        )
        assert "a=1" in out.stdout, out.stdout
        assert "b=0" in out.stdout, out.stdout
        assert "c=1" in out.stdout, out.stdout

    def test_the_two_documented_costs_reproduce(self):
        """ISSUE-307 paid for these; they arrive here unchanged.

        141 is SIGPIPE and has a fixed code, so `shell_exec.SIGPIPE_NOTE` can
        annotate it. The second has no marker and is documented instead.
        """
        assert self._run("yes | head -1 >/dev/null").returncode == shell_exec.SIGPIPE_EXIT
        assert self._run("grep -c zzz /etc/hosts | wc -l >/dev/null").returncode != 0
