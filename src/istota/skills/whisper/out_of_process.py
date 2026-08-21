"""Run a transcription in a child process, so its memory leaves with it.

`import faster_whisper` costs roughly 293 MB of resident set, and each
construct-transcribe-drop cycle leaves about 450 MB behind on glibc's free
lists after Python has collected the model. That memory is reclaimable —
`malloc_trim(0)` returns it in full — but the daemon never calls `malloc_trim`
and no `MALLOC_TRIM_THRESHOLD_` is set anywhere, so it is never returned. It is
an allocator high-water mark rather than a leak, which is why it looks like it
is flattening: the next transcription of a similar size is served from the free
lists and costs nothing extra, while a longer recording pushes the mark up
again. Nothing bounds it.

Measured on the production host over one 66-hour run, five voice messages moved
the scheduler's RSS from 820 MB to 2894 MB in four discrete steps, each landing
within three minutes of a `Pre-transcribed audio for task NNN` line, and it was
still stepping upward when the daemon restarted (ISSUE-273).

`WorkerPool` workers are threads, so all of that lands in the daemon and stays
for its lifetime. The fix is not to reclaim the memory but to spend it in a
process that ends: a child exits, and every byte it took goes back to the OS
with it. `python -m istota.skills.whisper transcribe` already did the work and
already spoke JSON — the daemon was simply the one caller importing the skill's
code into itself instead of spawning it.

This module is the seam between the two, and it imports nothing heavy on
purpose. Keeping it stdlib-only is what makes the boundary real rather than a
matter of habit: nothing here can pull `faster_whisper` back into the caller.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

from istota.process_group import kill_process_group

__all__ = ["transcribe_audio_out_of_process", "DEFAULT_TIMEOUT_SECONDS"]

logger = logging.getLogger("istota.whisper_out_of_process")

# A voice message is normally seconds of audio and `WHISPER_MAX_MODEL` caps the
# model at `small`, so this is a backstop against a wedged child rather than a
# budget anyone should hit. The in-process version had no bound at all: a hung
# transcription held the worker thread until the daemon restarted, and having a
# separate process is what makes a bound enforceable in the first place.
DEFAULT_TIMEOUT_SECONDS = 900.0

# How long to wait for a killed child to be reaped. The process group is
# already dead by then, so this only covers the kernel getting around to it.
_REAP_TIMEOUT_SECONDS = 10.0

# Enough of a traceback to name the failure without pasting a screenful of it
# into the task log.
_ERROR_DETAIL_MAX_CHARS = 500


def transcribe_audio_out_of_process(
    path: str,
    model: str = "auto",
    language: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Transcribe `path` in a child process and return the CLI's result dict.

    Same contract as the in-process `transcribe.transcribe_audio`: a dict
    carrying `status` of `"ok"` or `"error"`, and on success the transcript
    under `text`.

    Never raises. Every failure — a child that cannot be spawned, one that
    times out, one that writes nothing parseable — comes back as an error dict,
    because the caller in the daemon treats transcription as best-effort
    enrichment of a prompt it can send perfectly well without it. A missing
    `faster-whisper` install arrives the same way, as an ordinary error result
    from the child rather than as an ImportError here.
    """
    if not sys.executable:
        return {
            "status": "error",
            "error": "no interpreter to spawn a transcription process with",
        }

    argv = [
        sys.executable,
        # `-m` prepends the working directory to the child's `sys.path`, which
        # the in-process import never did. `-P` takes it back off: the daemon's
        # cwd is operator-chosen and this is not the place to inherit an import
        # surface from it.
        "-P",
        "-m",
        "istota.skills.whisper",
        "transcribe",
        "--model",
        model,
        "--output",
        "json",
        # The caller wants `text`; `segments` is one dict per *word*, because
        # `transcribe_audio` asks for word timestamps. Pretty-printed at
        # indent=2 that is megabytes for a long recording, and `communicate`
        # has no size cap — so the daemon would buffer it, decode it into tens
        # of thousands of transient dicts and floats, and read one key off the
        # result. Smaller than the 450 MB this module exists to remove, and the
        # same mechanism on the same allocator in the same process.
        "--no-segments",
    ]
    if language:
        argv += ["--language", language]
    # Last, behind `--`. The path is built from a sender-supplied attachment
    # name, and argparse reads a leading `-` as an option: without the guard a
    # file called `-x.mp3` fails as a usage error on stderr with exit 2, which
    # reaches the caller as "no usable result" rather than as anything about
    # the file.
    argv += ["--", path]

    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # UTF-8 on both halves of the pipe, pinned rather than inherited. A
            # transcript is in whatever language was spoken and the CLI writes
            # it with `ensure_ascii=False`, so the bytes are routinely
            # non-ASCII — while a daemon under LANG=C gets an ASCII stdout in
            # the child and an ASCII decode here. That pair loses a German
            # voice message to a UnicodeEncodeError in one process and a
            # UnicodeDecodeError in the other, on a host where an English one
            # works, which is the least debuggable shape this could take.
            # `errors="replace"` because a mangled character in a transcript is
            # worth more than no transcript.
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            # Never the daemon's stdin. Under systemd that is /dev/null and
            # this changes nothing, but `istota serve` in a terminal would hand
            # the child the operator's tty — and a child that reads from it (an
            # HF auth prompt on a cache miss) then blocks for the whole timeout
            # with nothing in the log to say why.
            stdin=subprocess.DEVNULL,
            # Its own session, so the timeout path can signal the whole group.
            # faster-whisper decodes in-process today and spawns nothing, but a
            # grandchild that inherited the pipes would otherwise survive the
            # kill and keep `communicate()` blocked on a read that never ends.
            start_new_session=True,
        )
    except OSError as e:
        logger.warning("Could not spawn transcription process for %s: %s", path, e)
        return {"status": "error", "error": f"could not spawn transcription process: {e}"}

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # A hang here is most likely *after* the answer: the CLI prints its
        # JSON and only then tears down ctranslate2's worker threads and
        # whatever the HF client registered at exit. `communicate()` after a
        # timeout returns everything buffered so far, so the reap often holds a
        # perfectly good transcript — throwing it away would report total
        # failure for a transcription that had in fact finished.
        stdout, _ = _kill_and_reap(proc, path)
        recovered = _parse_cli_json(stdout)
        if recovered is not None:
            logger.warning(
                "Transcription of %s timed out after %.0fs, but the child had already "
                "written a result; using it",
                path,
                timeout,
            )
            return recovered
        logger.warning("Transcription of %s timed out after %.0fs", path, timeout)
        return {"status": "error", "error": f"transcription timed out after {timeout:.0f}s"}
    except Exception as e:
        # A broken pipe, or a decode that failed despite the pinned encoding.
        # The child is still running and still holding the memory this module
        # exists to bound, and `start_new_session` means nothing else will ever
        # signal it — so kill it here rather than leaving it to finish
        # unsupervised.
        logger.warning("Transcription process for %s failed: %s", path, e, exc_info=True)
        _kill_and_reap(proc, path)
        return {"status": "error", "error": f"transcription process failed: {e}"}

    result = _parse_cli_json(stdout)
    if result is not None:
        # A non-zero exit with a parseable body is an error *result*, which the
        # CLI reports that way by design. Pass it through as it was written.
        return result

    detail = (stderr or stdout or "").strip()
    logger.warning(
        "Transcription process for %s exited %s with no usable result: %s",
        path,
        proc.returncode,
        _tail(detail),
    )
    error = f"transcription process exited {proc.returncode} without a usable result"
    if detail:
        error = f"{error}: {_tail(detail)}"
    return {"status": "error", "error": error}


def _kill_and_reap(proc, path: str) -> tuple[str, str]:
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
        logger.warning("Could not reap the transcription process for %s: %s", path, e)
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
    alone on the stream: ctranslate2 and huggingface_hub both warn from code
    that writes to the process's stdout directly rather than through Python's
    `sys.stderr`, so a model-download line can land ahead of the result.

    So scan forward from each `{` and take the first object carrying `status`.
    That key is the one the CLI always sets and the one no nested object in the
    result has — matching on the first `{` alone would return a `segments` or
    `words` fragment out of a document that happened to start with noise.
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
