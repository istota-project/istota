"""Does `--append-system-prompt-file` reach the model? (`live`)

The spec that introduced `BrainRequest.composed_system_prompt_path` names this
as the largest unmeasured risk in the change, and it lands on the two backends
ISSUE-375 does not affect. Everything else about the Claude Code path is
argv-verified: `tests/test_brain.py` proves the flag is emitted once, with the
right path, in every combination with the operator file, and a `strings` read
of the pinned bundle proves the CLI has a handler for it at all. None of that
says the appended text reaches the model. Before this change the composed
prompt arrived on stdin, where it plainly did; after it, the whole of Istota's
identity, rules and tool surface travels through one undocumented flag —
`claude --help` on 2.1.241 omits it, because it is registered `.hideHelp()`.

So the failure this exists to catch is a CLI that accepts the flag, opens the
file, and ignores its contents. Nothing at any other tier can see that: the
smoke tier's model stub exercises the *native* brain, and the argv assertions
are satisfied by a CLI that writes the bytes to /dev/null.

**The sentinel is the discriminator.** It is generated per run and appears in
exactly one place — the composed file — so the model cannot produce it from the
user prompt, from its training, or from this repository. The test asserts that
too, before spending anything, which is what stands in for the paired negative
control the repository's rules would otherwise want: a run that cannot reach
the sentinel by any other channel does not need a second paid call to prove it
can fail.

**Running it costs money and it is not a merge gate.** Run it before merge and
report what it did:

    uv run pytest -m live -n0

With no Claude Code credential it skips, for the reason
`test_claude_code_read_image.py` gives at length. `ISTOTA_LIVE_TIER=1` turns
each skip into a failure, for the run that exists to make this execute.
"""

import dataclasses as _dc
import os
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

from istota.brain._types import BrainRequest
from istota.brain.claude_code import ClaudeCodeBrain, build_claude_cli_flags
from istota.process_group import kill_process_group
from istota.subscription_usage import resolve_token

from .stream_json import answer_text, iter_frames, transcript_summary

pytestmark = pytest.mark.live

# Captured at import, before conftest's autouse credential scrub runs — the
# same module-scope read `test_claude_code_read_image.py` documents.
_AMBIENT_ENV = dict(os.environ)

_TURN_TIMEOUT_SECONDS = 300


def _unavailable(reason: str) -> None:
    if _AMBIENT_ENV.get("ISTOTA_LIVE_TIER") == "1":
        pytest.fail(f"ISTOTA_LIVE_TIER=1 says this host can run the live tier: {reason}")
    pytest.skip(reason)


@pytest.fixture(autouse=True)
def _requires_claude_code() -> None:
    """Skip unless a `claude` binary and a credential are both in reach."""
    if shutil.which("claude") is None:
        _unavailable("no `claude` on PATH")
    if _AMBIENT_ENV.get("ISTOTA_LIVE_TIER") == "1":
        return
    if _AMBIENT_ENV.get("ANTHROPIC_API_KEY"):
        return
    if resolve_token(_AMBIENT_ENV) is None:
        _unavailable("no Claude Code credential in reach")


def _child_env() -> dict[str, str]:
    env = dict(_AMBIENT_ENV)
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and geteuid() == 0:
        env.setdefault("IS_SANDBOX", "1")
    env["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"] = "1"
    return env


def _run_cli(cmd: list[str], prompt: str, cwd: Path) -> tuple[int, str, str]:
    """Run the CLI to completion, or kill its whole process group and say so."""
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(cwd),
        env=_child_env(),
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(prompt, timeout=_TURN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        kill_process_group(process.pid)
        stdout, stderr = process.communicate()
        pytest.fail(
            f"the CLI did not finish inside {_TURN_TIMEOUT_SECONDS}s. "
            f"{transcript_summary(iter_frames(stdout or ''))}"
        )
    return process.returncode, stdout, stderr


class TestTheComposedSystemPromptReachesTheModel:
    def test_an_instruction_only_the_appended_file_carries_is_obeyed(self, tmp_path):
        sentinel = f"ISTOTA-SYS-{secrets.token_hex(6).upper()}"
        composed = tmp_path / "task_1_system_prompt.txt"
        composed.write_text(
            "You are a test fixture for Istota's prompt assembly.\n\n"
            "## Important rules\n\n"
            "1. When asked for your build tag, reply with exactly this token "
            f"and nothing else: {sentinel}\n",
            encoding="utf-8",
        )

        # No tools. The claim is about the system channel, and a tool-bearing
        # request adds --dangerously-skip-permissions and a working directory
        # the model may go exploring in, which buys nothing here and can only
        # make a paid run slower and less determinate.
        req = BrainRequest(
            prompt="What is your build tag?",
            allowed_tools=[],
            cwd=tmp_path,
            env={},
            timeout_seconds=_TURN_TIMEOUT_SECONDS,
            composed_system_prompt_path=composed,
        )

        # The shipped argv, not a second copy of it: a witness that builds its
        # own command line is a witness for a command line nothing runs.
        cmd = ClaudeCodeBrain._build_command(_dc.replace(req, streaming=True))
        assert "--append-system-prompt-file" in cmd, (
            f"the shipped argv does not carry the flag: {cmd}"
        )
        assert str(composed) in cmd

        # The discriminator, asserted before anything is spent. If the sentinel
        # could reach the model by any channel but the appended file, a pass
        # below would mean nothing.
        assert sentinel not in req.prompt
        assert not any(sentinel in part for part in cmd), (
            "the sentinel is on the command line, so the file is not its only "
            "route to the model"
        )
        assert sentinel not in " ".join(build_claude_cli_flags(req))

        returncode, stdout, stderr = _run_cli(cmd, req.prompt, tmp_path)
        frames = iter_frames(stdout)
        assert frames, (
            f"no stream-json frames (rc={returncode}): {stderr.strip()[:400]}"
        )

        answer = answer_text(frames)
        assert sentinel in answer, (
            "the CLI accepted --append-system-prompt-file and opened the file, "
            "but the instruction it carries did not reach the model. "
            f"{transcript_summary(frames)} answer={answer.strip()[:400]!r}"
        )

    def test_a_missing_appended_file_fails_the_run_rather_than_dropping_it(
        self, tmp_path
    ):
        """The fail-closed contract, end to end and for free.

        `build_claude_cli_flags` deliberately has no `exists()` gate for this
        path, on the strength of an error string found in the bundle. This runs
        the binary against a path that does not resolve and requires it to
        refuse — the same claim, measured. It reaches no model and costs
        nothing, which is why it is safe to keep beside the paid case.
        """
        missing = tmp_path / "task_1_system_prompt.txt"
        req = BrainRequest(
            prompt="What is your build tag?",
            allowed_tools=[],
            cwd=tmp_path,
            env={},
            timeout_seconds=_TURN_TIMEOUT_SECONDS,
            composed_system_prompt_path=missing,
        )
        cmd = ClaudeCodeBrain._build_command(_dc.replace(req, streaming=True))

        returncode, stdout, stderr = _run_cli(cmd, req.prompt, tmp_path)
        assert returncode != 0, (
            "the CLI ran with the composed system prompt silently dropped, "
            "which is ISSUE-375 reintroduced on the Claude Code backends. "
            f"{transcript_summary(iter_frames(stdout))}"
        )
        assert "not found" in (stderr + stdout).lower(), (
            f"unexpected refusal (rc={returncode}): {stderr.strip()[:400]}"
        )
