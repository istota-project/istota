"""Run one OCR pass in a child process, so its memory and its hang leave with it.

Patterned on `skills/whisper/out_of_process.py`, and for that module's actual
argument: memory isolation and an enforceable timeout. It is deliberately *not*
patterned on a claim that `pytesseract` is absent from the daemon — that claim
would be false. `health/ocr.py:_ocr_image` calls `pytesseract.image_to_string`
in-process today, once per rendered PDF page, and is knowingly left alone.

What a child buys here is smaller than for whisper and still worth having.
Tesseract is a subprocess either way, but `pytesseract` writes the image to a
temp file, reads it back and holds the page dictionary; twenty attachments in
one send is twenty of those cycles on a `WorkerPool` *thread*, where the
allocator high-water mark stays for the daemon's lifetime. And a wedged
Tesseract has no bound at all in-process: the shared OCR deadline is only
enforceable because there is a process group to kill.

Stdlib-only on purpose. Nothing here can pull Pillow or pytesseract back into
the caller, which is what makes the boundary a fact rather than a habit. The
JSON scan below is a near-copy of the whisper module's rather than an import
from it: these are two leaf modules in two skill packages, and a cross-skill
import would tie each one's boundary to the other's.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

from istota.process_group import kill_process_group

__all__ = ["ocr_image_out_of_process", "DEFAULT_TIMEOUT_SECONDS"]

logger = logging.getLogger("istota.transcribe_out_of_process")

# The whole automatic-OCR pass shares one 60-second deadline, so this default
# is the caller's *total* budget rather than a per-image one. `prepare_image_
# attachments` hands each spawn what is left of it, which is why nothing here
# tries to divide anything up.
DEFAULT_TIMEOUT_SECONDS = 60.0

# How long to wait for a killed child to be reaped. The group is already dead
# by then, so this only covers the kernel getting around to it.
_REAP_TIMEOUT_SECONDS = 10.0

# Enough of a traceback to name the failure without pasting a screenful of it
# into the task log.
_ERROR_DETAIL_MAX_CHARS = 500


def ocr_image_out_of_process(path: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """OCR `path` in a child process and return the CLI's result dict.

    Same contract as `transcribe.cmd_ocr`: a dict carrying `status` of `"ok"`
    or `"error"`, and on success `text`, `confidence` and `word_count`.

    Never raises. Every failure — a child that cannot be spawned, one that
    times out, one that writes nothing parseable, a missing `pytesseract` or a
    missing Tesseract binary — comes back as an error dict, because the caller
    treats OCR as best-effort context beside an image the model can see for
    itself.
    """
    if not sys.executable:
        return {"status": "error", "error": "no interpreter to spawn an OCR process with"}

    argv = [
        sys.executable,
        # `-m` prepends the working directory to the child's `sys.path`. `-P`
        # takes it back off: the daemon's cwd is operator-chosen and this is
        # not the place to inherit an import surface from it.
        "-P",
        "-m",
        "istota.skills.transcribe",
        "ocr",
        # Deliberately no `--preprocess`. Automatic OCR runs the normal mode
        # once; a second enhanced pass would double the work for every image on
        # a guess about the input. The explicit skill command still offers it
        # when a user asks for another attempt on a poor scan.
        #
        # Last, behind `--`. The path is derived from a sender-supplied
        # attachment name and argparse reads a leading `-` as an option:
        # without the guard a file called `-x.png` fails as a usage error on
        # stderr with exit 2, which reaches the caller as "no usable result"
        # rather than as anything about the file. argparse strips the separator
        # itself, so the parser needs no change.
        "--",
        path,
    ]

    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # UTF-8 on both halves of the pipe, pinned rather than inherited.
            # OCR text is in whatever language the image was written in and the
            # CLI writes it with `ensure_ascii=False`, so the bytes are
            # routinely non-ASCII — while a daemon under LANG=C would get an
            # ASCII stdout in the child and an ASCII decode here, losing a
            # German scan on a host where an English one works.
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            # Never the daemon's stdin. Under systemd that is /dev/null and
            # this changes nothing, but `istota serve` in a terminal would hand
            # the child the operator's tty.
            stdin=subprocess.DEVNULL,
            # Its own session, so the timeout path can signal the whole group.
            # `pytesseract` spawns the real `tesseract` binary, so there is
            # always a grandchild here — and one that inherited the pipes would
            # otherwise survive the kill and keep `communicate()` blocked on a
            # read that never ends.
            start_new_session=True,
        )
    except Exception as e:
        logger.warning("Could not spawn OCR process: %s", e)
        return {"status": "error", "error": f"could not spawn OCR process: {e}"}

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # The hang is most likely *after* the answer — the CLI prints its JSON
        # and only then tears down whatever Pillow and pytesseract registered
        # at exit. `communicate()` after a timeout returns everything buffered
        # so far, so the reap often holds a perfectly good result.
        stdout, _ = _kill_and_reap(proc)
        recovered = _parse_cli_json(stdout)
        if recovered is not None:
            logger.warning(
                "OCR timed out after %.0fs, but the child had already written a "
                "result; using it",
                timeout,
            )
            return recovered
        logger.warning("OCR timed out after %.0fs", timeout)
        return {"status": "error", "error": f"OCR timed out after {timeout:.0f}s"}
    except Exception as e:
        # A broken pipe, or a decode that failed despite the pinned encoding.
        # The child is still running and `start_new_session` means nothing else
        # will ever signal it, so kill it here rather than leaving it to finish
        # unsupervised.
        logger.warning("OCR process failed: %s", e)
        _kill_and_reap(proc)
        return {"status": "error", "error": f"OCR process failed: {e}"}

    result = _parse_cli_json(stdout)
    if result is not None:
        # A non-zero exit with a parseable body is an error *result*, which the
        # CLI reports that way by design. Pass it through as it was written.
        return result

    detail = (stderr or stdout or "").strip()
    logger.warning("OCR process exited %s with no usable result", proc.returncode)
    error = f"OCR process exited {proc.returncode} without a usable result"
    if detail:
        error = f"{error}: {_tail(detail)}"
    return {"status": "error", "error": error}


def _kill_and_reap(proc) -> tuple[str, str]:
    """SIGKILL the child's group and collect whatever it had already written.

    Returns `(stdout, stderr)`, empty strings if nothing could be collected.
    Never raises: both callers are already handling a failure.
    """
    kill_process_group(proc.pid)
    try:
        return proc.communicate(timeout=_REAP_TIMEOUT_SECONDS)
    except Exception as e:
        # The group took SIGKILL, so the realistic residue is a child stuck in
        # uninterruptible sleep. Say so rather than swallowing it — and close
        # the pipes by hand, since the reap that would normally do it did not
        # finish and the fds would otherwise sit until garbage collection.
        logger.warning("Could not reap the OCR process: %s", e)
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        return "", ""


def _parse_cli_json(stdout: str) -> dict | None:
    """Pull the CLI's result object out of `stdout`, or None if it isn't there.

    The CLI prints exactly one JSON document, but it is not guaranteed to be
    alone on the stream: a library that writes to the process's stdout directly
    rather than through Python's `sys.stderr` can land a line ahead of it.

    So scan forward from each `{` and take the first object carrying `status`.
    That key is the one the CLI always sets, so matching on the first `{` alone
    would return a nested fragment out of a document that started with noise.
    """
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


def _tail(text: str) -> str:
    """The last few hundred characters — a traceback's useful end, not its head."""
    if len(text) <= _ERROR_DETAIL_MAX_CHARS:
        return text
    return "..." + text[-_ERROR_DETAIL_MAX_CHARS:]
