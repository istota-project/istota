"""Run `istota.lib.gif_frames` in a child process, with a deadline (ISSUE-647).

The daemon half of the GIF decode. The child is a parser on a file a stranger
chose, so it gets its own process for the reasons the OCR runner
(`skills/transcribe/out_of_process.py`) gives: its memory and its hang leave
with it, and a deadline is enforceable because there is a process group to
kill. It runs on the inbound worker thread before the batch's write lock is
taken, never under it.

Returns the child's error code or `None` for success, and never raises. The
JSON scan is the OCR runner's rule (first object carrying `status`), restated
here rather than imported across a skill boundary.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

from istota.sandbox.process_group import kill_group_if_live

__all__ = ["DEFAULT_TIMEOUT_SECONDS", "ERROR_CODES", "child_argv", "extract_frames"]

logger = logging.getLogger(__name__)

#: The inbound worker is serial, so this bounds how long one GIF can hold
#: every message behind it.
DEFAULT_TIMEOUT_SECONDS = 20.0
_REAP_TIMEOUT_SECONDS = 10.0

#: Every code the child can answer, plus the runner's own two.
ERROR_CODES = frozenset({
    "decoder_missing", "unreadable", "no_video", "too_large", "too_long",
    "no_frames", "write_failed", "timed_out", "no_result",
})


def child_argv(src: str, dest: str) -> list[str]:
    """The child's argv: `-P` for the OCR runner's reason, paths behind `--`."""
    return [sys.executable, "-P", "-m", "istota.lib.gif_frames", "--", src, dest]


def extract_frames(src: str, dest: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str | None:
    """Tile the frames of *src* into a new JPEG at *dest*.

    `None` on success, else one of `ERROR_CODES`. Never raises.
    """
    if not sys.executable:
        return "no_result"
    try:
        proc = subprocess.Popen(
            child_argv(src, dest),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            start_new_session=True,
        )
    except Exception as e:  # noqa: BLE001 — a spawn failure is a failed decode
        logger.warning("whatsapp.gif.spawn_failed: %s", type(e).__name__)
        return "no_result"
    try:
        stdout, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_and_reap(proc)
        logger.warning("whatsapp.gif.timed_out after=%.0fs", timeout)
        return "timed_out"
    except Exception as e:  # noqa: BLE001
        logger.warning("whatsapp.gif.child_failed: %s", type(e).__name__)
        _kill_and_reap(proc)
        return "no_result"
    result = _parse_result(stdout or "")
    if result is None:
        return "no_result"
    if result.get("status") == "ok":
        return None
    code = result.get("error")
    return code if code in ERROR_CODES else "no_result"


def _kill_and_reap(proc) -> None:
    kill_group_if_live(proc)
    try:
        proc.communicate(timeout=_REAP_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 — the group took SIGKILL; nothing more to do
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass


def _parse_result(stdout: str) -> dict | None:
    decoder = json.JSONDecoder()
    start = stdout.find("{")
    while start != -1:
        try:
            obj, _ = decoder.raw_decode(stdout, start)
        except ValueError:
            pass
        else:
            if isinstance(obj, dict) and "status" in obj:
                return obj
        start = stdout.find("{", start + 1)
    return None
