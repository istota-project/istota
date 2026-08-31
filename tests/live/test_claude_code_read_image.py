"""Does Claude Code actually turn the `Read` directive into sight? (`live`)

Every other test of the Claude Code image path asserts against a string istota
built: that the directive names the resolved path, that the path is the one
bwrap binds, that the trace audit notices a missing `Read`. None of them can
say what the model and the CLI do with that directive, and that is the whole
behavioural claim — a model reading an image gets the picture back rather than
a note that the file is binary. Only a real run can answer it, so this test
carries the `live` marker and nothing else.

Two things it deliberately does not do. It does not grade the answer's prose:
whether the model names the colours correctly is a question about the model,
and a witness that asserts on it fails for reasons that have nothing to do with
this code. And it does not read the transcript through
`brain._events.parse_stream_line` — that parser returns `None` for the user
frame a tool result arrives in, so a test written against it could only fall
back to asserting on the path string istota itself wrote, which a model that
opened nothing satisfies.

**Running it costs money and it is not a merge gate.** Run it before merge and
report what it did:

    uv run pytest -m live -n0

With no Claude Code credential it skips, because a suite that hard-fails on
every machine without one is worse than no witness at all. `ISTOTA_LIVE_TIER=1`
turns each skip into a failure, for the run that exists to make this execute —
the same split `tests/linux/test_sandbox_real.py` draws around
`ISTOTA_LINUX_TIER`.
"""

import dataclasses as _dc
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("PIL", reason="Pillow not installed")
from PIL import Image  # noqa: E402

from istota.brain._types import BrainRequest  # noqa: E402
from istota.brain.claude_code import (  # noqa: E402
    ClaudeCodeBrain,
    build_image_prompt,
)
from istota.executor import build_allowed_tools  # noqa: E402
from istota.image_attachments import prepare_image_attachments  # noqa: E402
from istota.subscription_usage import resolve_token  # noqa: E402

from .stream_json import (  # noqa: E402
    carries_image,
    iter_frames,
    read_calls,
    tool_result_content,
    transcript_summary,
)

pytestmark = pytest.mark.live

# Captured at import — during collection, before conftest's autouse scrub runs.
# `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_API_KEY` both match the credential
# patterns in `tests/support/env_isolation.py`, so a *test body* sees neither:
# that scrub is what keeps the rest of the suite from reaching a real API, and
# module scope is the documented way to consume one of the names it eats
# (`tests/test_browse_integration.py` reads `BROWSER_HOST` the same way).
_AMBIENT_ENV = dict(os.environ)

# The CLI turn itself. Generous rather than tight: this is one model call over
# somebody's network, and a witness that fails on a slow minute reports a
# product regression that did not happen.
_TURN_TIMEOUT_SECONDS = 300


def _unavailable(reason: str) -> None:
    """Skip — unless the operator said this host is credentialled.

    A silent skip is the right answer for a laptop that never claimed to have a
    credential, and the wrong one for the run whose entire purpose is to make
    this execute. `ISTOTA_LIVE_TIER=1` is that assertion, and it also covers the
    case the credential probe cannot: on macOS the token usually lives in the
    login keychain under a per-application ACL, so `security find-generic-password`
    from a Python process is refused (or waits out its timeout) while the
    `claude` binary itself reads it without trouble.
    """
    if _AMBIENT_ENV.get("ISTOTA_LIVE_TIER") == "1":
        pytest.fail(f"ISTOTA_LIVE_TIER=1 says this host can run the live tier: {reason}")
    pytest.skip(reason)


@pytest.fixture(autouse=True)
def _requires_claude_code() -> None:
    """Skip unless a `claude` binary and a credential are both in reach.

    A fixture rather than `pytest.mark.skipif`, for the reason the linux tier
    gives: a skipif condition is evaluated at collection, so the credential
    probe would run on every `uv run pytest` in every xdist worker, for a test
    the marker has already deselected.
    """
    if shutil.which("claude") is None:
        _unavailable("no `claude` on PATH")
    if _AMBIENT_ENV.get("ISTOTA_LIVE_TIER") == "1":
        return
    if _AMBIENT_ENV.get("ANTHROPIC_API_KEY"):
        return
    home = Path.home() if os.path.expanduser("~") != "~" else None
    if resolve_token(_AMBIENT_ENV, home) is None:
        _unavailable(
            "no Claude Code credential this process can read "
            "(set ISTOTA_LIVE_TIER=1 if the CLI has one you cannot)"
        )


def _two_region_image(path: Path) -> Path:
    """A deterministic fixture with two flat colour fields and no text.

    No text on purpose: OCR has nothing to contribute, so the only way anything
    downstream can describe this file is by looking at it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (160, 80), (220, 30, 30))
    image.paste(Image.new("RGB", (80, 80), (30, 60, 220)), (80, 0))
    image.save(path, "PNG")
    return path


def _child_env() -> dict[str, str]:
    env = dict(_AMBIENT_ENV)
    # `claude` refuses --dangerously-skip-permissions as root unless an external
    # isolation boundary is signalled — the same thing `ClaudeCodeBrain` does
    # for a tool-bearing request (`_is_root`).
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and geteuid() == 0:
        env.setdefault("IS_SANDBOX", "1")
    return env


class TestReadReturnsThePicture:
    def test_the_directive_produces_an_image_bearing_tool_result(self, tmp_path):
        source = _two_region_image(tmp_path / "inbox" / "two_regions.png")
        prep = prepare_image_attachments(
            [str(source)], tmp_path / "temp", task_id=1,
        )
        assert prep.images, "the fixture did not survive preparation"
        image = prep.images[0]

        req = BrainRequest(
            prompt="Name the two colours in this image, in one short sentence.",
            allowed_tools=build_allowed_tools(is_admin=True, skill_names=[]),
            cwd=tmp_path,
            env={},
            timeout_seconds=_TURN_TIMEOUT_SECONDS,
            images=list(prep.images),
        )
        # The shipped argv and the shipped directive, not a second copy of
        # either: a witness that builds its own command line is a witness for a
        # command line nothing runs.
        cmd = ClaudeCodeBrain._build_command(_dc.replace(req, streaming=True))
        prompt = build_image_prompt(req)
        assert str(image.path) in prompt

        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=_child_env(),
            timeout=_TURN_TIMEOUT_SECONDS,
        )
        frames = iter_frames(proc.stdout)
        assert frames, (
            f"no stream-json frames (rc={proc.returncode}): "
            f"{proc.stderr.strip()[:400]}"
        )

        summary = transcript_summary(frames)
        reads = [
            (call_id, path) for call_id, path in read_calls(frames)
            if Path(path or "").resolve() == image.path
        ]
        assert reads, (
            "the model never called Read on the image the directive named. "
            f"{summary}"
        )

        content = tool_result_content(frames, reads[0][0])
        assert content is not None, f"the Read call has no tool result. {summary}"
        assert carries_image(content), (
            "the Read tool result carried no image block, so the CLI returned "
            f"the file as text rather than as a picture. {summary}"
        )
