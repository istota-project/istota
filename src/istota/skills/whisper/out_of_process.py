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

from istota.process_group import kill_group_if_live

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


#: The four names the child's allowlist is derived from. Cleared before the
#: identity is applied, so an unsupplied one is absent rather than inherited.
_IDENTITY_VARS = (
    "ISTOTA_USER_ID",
    "ISTOTA_WORKSPACE_PATH",
    "NEXTCLOUD_MOUNT_PATH",
    "ISTOTA_DEFERRED_DIR",
)


def _identity_env(
    user_id: str | None,
    mount_path: str | os.PathLike[str] | None,
    deferred_dir: str | os.PathLike[str] | None,
) -> dict[str, str]:
    """The task identity the child derives its allowlist from.

    Four of the five workspace identity variables `build_task_runtime` exports;
    `ISTOTA_CONVERSATION_TOKEN` is deliberately not among them, so the child
    gets no `{mount}/Channels/{token}` root. Nothing writes an audio
    attachment there — Talk shares land in `{mount}/Talk`, web-chat uploads
    in the workspace inbox or the per-user temp dir — and the caller stages
    anything out of reach into the temp dir anyway
    (`executor._audio_in_reach`), so the root would widen what the child may
    name without making any shipped attachment reachable.

    `whisper transcribe`'s path argument is scoped against the allowlist the
    *child's* environment names (ISSUE-447), and the daemon's own environment
    carries none of these variables — so a spawn that passed `os.environ`
    alone would hand the child an empty allowlist, which refuses everything.
    `executor._pre_transcribe_attachments` logs a failed transcription at
    **debug** and carries on, so that would have stopped voice messages being
    transcribed with nothing above debug saying why.

    The general rule, which applies to any future daemon-side spawn of a
    skill CLI: **pass the task identity, or the CLI refuses every path.** A
    `--trusted-caller` flag was rejected — it is a flag the model can pass
    too — and so was detecting the daemon, which is a special case wearing a
    predicate.

    A value that was not given is left out rather than exported blank: an
    empty string reads to `env_host_roots` exactly as an unset variable does,
    and inventing one would only obscure which caller failed to say.

    **The caller has to clear the four names first**, which `_child_env`
    does, and that is not tidiness. The child inherits `os.environ`, so a
    name omitted here would otherwise fall through to whatever the daemon's
    own environment holds — and the daemon has no task, so any value it
    carries belongs to something else. That also makes the negative case
    honest rather than dependent on the machine the tests run on.
    """
    out: dict[str, str] = {}
    if user_id:
        out["ISTOTA_USER_ID"] = str(user_id)
    if mount_path:
        out["ISTOTA_WORKSPACE_PATH"] = str(mount_path)
        out["NEXTCLOUD_MOUNT_PATH"] = str(mount_path)
    if deferred_dir:
        out["ISTOTA_DEFERRED_DIR"] = str(deferred_dir)
    return out


def _child_env(
    user_id: str | None,
    mount_path: str | os.PathLike[str] | None,
    deferred_dir: str | os.PathLike[str] | None,
) -> dict[str, str]:
    """The daemon's environment, with the identity replaced rather than merged."""
    env = {k: v for k, v in os.environ.items() if k not in _IDENTITY_VARS}
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(_identity_env(user_id, mount_path, deferred_dir))
    return env


def transcribe_audio_out_of_process(
    path: str,
    model: str = "auto",
    language: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    *,
    user_id: str | None = None,
    mount_path: str | os.PathLike[str] | None = None,
    deferred_dir: str | os.PathLike[str] | None = None,
) -> dict:
    """Transcribe `path` in a child process and return the CLI's result dict.

    Same contract as the in-process `transcribe.transcribe_audio`: a dict
    carrying `status` of `"ok"` or `"error"`, and on success the transcript
    under `text`.

    The three identity arguments are what let the child resolve `path` at all;
    see `_identity_env`. They are keyword-only and default to absent, so a
    caller that has no task — a script, a test — gets the honest answer that
    nothing is allowed rather than a widened allowlist.

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
            env=_child_env(user_id, mount_path, deferred_dir),
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
        # If the child is still running it is still holding the memory this
        # module exists to bound, and `start_new_session` means nothing else
        # will ever signal it — so kill it here rather than leaving it to
        # finish unsupervised. Whether it is still running is `_kill_and_reap`'s
        # to decide: `communicate()` can also fail after the reap.
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

    The kill is skipped when the child has already been reaped: both callers
    reach this after something went wrong with `communicate()`, and one of the
    ways that goes wrong is a failure raised once the child is gone. The pid
    would then be the OS's to reuse, and the group signalled somebody else's
    (ISSUE-456).

    Returns `(stdout, stderr)`, empty strings if nothing could be collected.
    Never raises: both callers are already handling a failure.
    """
    kill_group_if_live(proc)
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
