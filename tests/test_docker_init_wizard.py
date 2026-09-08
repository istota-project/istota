"""The Docker first-run wizard, driven the way an operator drives it.

``docker/init.sh`` is 900 lines of prompts whose only output is a ``.env``, and
nothing in the suite could reach it before. Two of its answers are worth
holding still:

**The Claude credential.** The CLI takes either a Claude Code OAuth token or an
Anthropic API key, and the entrypoint checks for both — but the wizard used to
prompt for the token alone and then mention `ANTHROPIC_API_KEY` in passing, as
a thing to set later by hand in a file it was in the middle of writing. Skipping
the token now asks for the key instead.

The driver below runs the real script under a pty, because ``read -rp`` prints
its prompt only when input is a terminal. It answers by matching the prompt
text rather than by position, so a wizard that grows a question somewhere else
does not silently shift every answer in this file by one.
"""

from __future__ import annotations

import os
import pty
import re
import select
import shutil
import subprocess
import termios
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INIT_SH = REPO / "docker" / "init.sh"
ENV_EXAMPLE = REPO / "docker" / ".env.example"

#: A prompt is the only thing the script writes that ends in ": " without a
#: newline behind it — `dim()` output always ends in a newline.
_PROMPT_TAIL = re.compile(r": (?:\x1b\[[0-9;]*m)?$")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def run_wizard(tmp_path: Path, answers: dict[str, str], timeout: float = 60.0) -> dict[str, str]:
    """Run init.sh in a copy of docker/, answering prompts by substring.

    `answers` maps a substring of the prompt to the line to type. Anything
    unmatched gets an empty line, which takes the offered default.
    """
    work = tmp_path / "docker"
    work.mkdir()
    shutil.copy(INIT_SH, work / "init.sh")
    shutil.copy(ENV_EXAMPLE, work / ".env.example")

    master, slave = pty.openpty()
    # Echo off: the answers we type would otherwise come back and land in the
    # middle of the next prompt we are trying to match.
    mode = termios.tcgetattr(slave)
    mode[3] &= ~termios.ECHO
    termios.tcsetattr(slave, termios.TCSANOW, mode)

    proc = subprocess.Popen(
        ["bash", "init.sh", "--no-start", "--force"],
        cwd=work,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)

    transcript = []
    buf = ""
    try:
        while True:
            ready, _, _ = select.select([master], [], [], timeout)
            if not ready:
                raise AssertionError(
                    f"wizard produced nothing for {timeout}s; last output:\n{''.join(transcript)[-2000:]}"
                )
            try:
                chunk = os.read(master, 4096)
            except OSError:  # pty closed — the script exited
                break
            if not chunk:
                break
            text = chunk.decode("utf-8", "replace")
            transcript.append(text)
            buf += text
            if _PROMPT_TAIL.search(buf):
                line = _ANSI.sub("", buf).splitlines()[-1]
                reply = ""
                for needle, value in answers.items():
                    if needle in line:
                        reply = value
                        break
                os.write(master, (reply + "\n").encode())
                buf = ""
    finally:
        os.close(master)
        proc.wait(timeout=30)

    assert proc.returncode == 0, f"init.sh exited {proc.returncode}:\n{''.join(transcript)[-3000:]}"

    env: dict[str, str] = {}
    for raw in (work / ".env").read_text().splitlines():
        if raw.startswith("#") or "=" not in raw:
            continue
        k, _, v = raw.partition("=")
        env[k] = v
    return env


pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="init.sh generates passwords with openssl"
)


class TestTheClaudeCredential:
    def test_an_oauth_token_is_written_and_the_key_is_not_asked_for(self, tmp_path):
        """The second prompt exists only as the fallback for a skipped token.
        `USER_NAME` proves it: if the key had been asked for anyway, the name
        below would have been eaten by that prompt."""
        env = run_wizard(
            tmp_path,
            {
                "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token-from-the-test",
                "ANTHROPIC_API_KEY": "api-key-that-should-not-be-asked-for",
                "USER_NAME": "operator",
            },
        )
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token-from-the-test"
        assert env["ANTHROPIC_API_KEY"] == ""
        assert env["USER_NAME"] == "operator"

    def test_skipping_the_token_asks_for_an_api_key_instead(self, tmp_path):
        env = run_wizard(
            tmp_path,
            {
                "CLAUDE_CODE_OAUTH_TOKEN": "",
                "ANTHROPIC_API_KEY": "api-key-from-the-test",
                "USER_NAME": "operator",
            },
        )
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
        assert env["ANTHROPIC_API_KEY"] == "api-key-from-the-test"
        assert env["USER_NAME"] == "operator"

    def test_neither_credential_leaves_both_keys_empty(self, tmp_path):
        env = run_wizard(tmp_path, {})
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
        assert env["ANTHROPIC_API_KEY"] == ""
