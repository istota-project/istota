"""Bash tool.

Runs a shell command via ``asyncio.create_subprocess_exec`` so it can stream
partial output (``on_update``), honor the ``abort`` event (kill on cancel), and
enforce a wall-clock timeout — none of which a blocking ``subprocess.run`` gives
cleanly. The raw argv is wrapped by ``ToolEnv.sandbox_wrap`` (bwrap on Linux,
no-op on macOS) so each command is sandboxed per-execution.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import time
from pathlib import Path

from istota.agent.tools import AgentTool, ToolResult
from istota.llm.types import TextContent, ToolParameter, ToolSchema
from istota import task_cgroup
from istota.process_group import kill_process_group
from istota.shell_exec import SIGPIPE_EXIT, SIGPIPE_NOTE, shell_argv

from .env import ToolEnv

# Read the pipe in chunks rather than by line. ``StreamReader.readline`` raises
# ValueError once a single line exceeds the reader's 64 KiB limit (minified JS,
# base64, ``jq -c``); chunked reads have no per-line ceiling and stream just as
# incrementally (NB-6).
_READ_CHUNK_BYTES = 65536


def make_bash_tool(env: ToolEnv) -> AgentTool:
    schema = ToolSchema(
        name="Bash",
        description=(
            "Run a bash command in the working directory. Output (stdout+stderr) "
            "is captured and capped. Provide a short `description` for progress "
            "display. Optional `timeout` in milliseconds."
        ),
        parameters=[
            ToolParameter(name="command", type="string", description="The command to run."),
            ToolParameter(name="description", type="string", description="5-10 word description.", required=False),
            ToolParameter(name="timeout", type="integer", description="Timeout in milliseconds.", required=False),
            ToolParameter(
                name="exclude_from_context",
                type="boolean",
                description=(
                    "If true, keep the (possibly large/noisy) output out of the "
                    "model's context — it still streams to the user. Use for "
                    "commands whose output you don't need to reason over."
                ),
                required=False,
            ),
        ],
    )

    async def _execute(call_id, args, on_update, abort):
        command = args["command"]
        exclude_from_context = bool(args.get("exclude_from_context"))
        timeout_ms = args.get("timeout")
        timeout_s = (int(timeout_ms) / 1000.0) if timeout_ms else float(env.bash_timeout_seconds)

        # `pipefail` on, because the result this tool returns ends in
        # `[exit code: N]` and the model reads that as whether the command
        # worked. Without it a pipeline reported its last stage, so
        # `pytest … | tail -3` came back clean on a suite that failed. The bare
        # name rather than a probed absolute path: the argv below is wrapped in
        # bubblewrap, and PATH resolution inside that namespace is what has
        # always worked here.
        cmd = shell_argv(command, bash="bash")
        if env.sandbox_wrap:
            cmd = env.sandbox_wrap(cmd)

        # Per-task cgroup (A6), placed from the child before it execs. This used
        # to be a write to `cgroup.procs` after the spawn, on the reasoning that
        # a `preexec_fn` in a threaded process must not open a file — true, and
        # the reason the open happens in the parent here and the child does one
        # `os.write` to the inherited descriptor. The window that reasoning
        # called bounded is not: membership is inherited at fork, so anything
        # the child forked first stays outside the cgroup permanently, and under
        # `sandbox_wrap` the child is bwrap, which forks during namespace setup
        # every time (ISSUE-285).
        try:
            with task_cgroup.placement(env.task_cgroup) as place_in_cgroup:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(env.cwd),
                    env=env.subprocess_env,
                    # Own process group so a timeout/abort/cancel can SIGKILL the
                    # whole tree — a command that backgrounds children (or a bwrap
                    # wrapper) otherwise survives a bare child kill (NB-7).
                    start_new_session=True,
                    preexec_fn=place_in_cgroup,
                )
        except (OSError, ValueError) as exc:
            return ToolResult(content=[TextContent(text=f"Failed to start command: {exc}")])

        # Only where placement engaged — otherwise `placement` has already named
        # the cause and this would report it a second time. A command short
        # enough to have exited by now is not reported at all; `verify_placement`
        # checks liveness, because leaving the cgroup at exit and never having
        # been in it look identical from here.
        if place_in_cgroup is not None and env.task_cgroup is not None:
            task_cgroup.verify_placement(proc.pid, env.task_cgroup)

        out = bytearray()
        total_bytes = 0
        truncated = False
        deadline = time.monotonic() + timeout_s
        status = "ok"

        # Spill the full output to a task-scoped file when it exceeds the cap, so
        # the tail isn't silently lost — the model can Read it back. Best-effort:
        # any spill error degrades to cap-only truncation. Skipped when the caller
        # already excluded the output from context (they don't want it).
        spill = _SpillWriter(env) if (env.bash_spill_full_output and not exclude_from_context) else None

        # try/finally so *every* exit path — normal, timeout, abort, and a hard
        # task cancellation (CancelledError, a BaseException the loop's
        # `except Exception` won't catch) — kills and reaps the process group
        # instead of leaking a live subprocess holding its pipe (NB-6/NB-11).
        try:
            while True:
                if abort is not None and abort.is_set():
                    status = "aborted"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    status = "timeout"
                    break
                try:
                    chunk = await asyncio.wait_for(
                        proc.stdout.read(_READ_CHUNK_BYTES), timeout=min(remaining, 0.5)
                    )
                except asyncio.TimeoutError:
                    if proc.returncode is not None:
                        break
                    continue
                if not chunk:
                    break  # EOF
                total_bytes += len(chunk)
                room = env.max_output_bytes - len(out)
                if room > 0:
                    out.extend(chunk[:room])
                    if len(out) >= env.max_output_bytes:
                        truncated = True
                        # Seed the spill with the buffered head + this chunk's
                        # overflow, then subsequent chunks stream straight to it.
                        if spill is not None:
                            spill.start(bytes(out))
                            spill.write(chunk[room:])
                elif spill is not None:
                    spill.write(chunk)
                if on_update is not None:
                    await on_update(chunk.decode("utf-8", "replace"))

            if status == "ok":
                # Reap so returncode is available for the exit-code suffix.
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=5)
        finally:
            if spill is not None:
                spill.close()
            _kill_process_group(proc)
            await _reap(proc)

        text = out.decode("utf-8", "replace")
        if truncated:
            spill_path = spill.path if spill is not None else None
            if spill_path is not None:
                text += (
                    f"\n… [output truncated at {env.max_output_bytes} bytes; "
                    f"full output: {spill_path}]"
                )
            else:
                text += f"\n… [output truncated at {env.max_output_bytes} bytes]"

        # Failure markers are kept separate so they can ride along even when the
        # body is excluded from context — a failed/aborted/timed-out command the
        # model can't see the status of would be reasoned about as a success.
        status_suffix = ""
        if status == "aborted":
            status_suffix = "\n[command aborted]"
        elif status == "timeout":
            status_suffix = f"\n[command timed out after {timeout_s:.0f}s]"
        elif proc.returncode == SIGPIPE_EXIT:
            # `pipefail`'s one recognisable cost. A bare 141 reads as a failure;
            # `yes | head -1` is a correct command that now reports one.
            status_suffix = f"\n[exit code: {SIGPIPE_EXIT}] {SIGPIPE_NOTE}"
        elif proc.returncode not in (0, None):
            status_suffix = f"\n[exit code: {proc.returncode}]"
        text += status_suffix

        if not text.strip():
            text = "(no output)"

        # The full output already streamed to the progress surface via
        # ``on_update``. When the caller asked to exclude it from context, the
        # model sees only a short stub so noisy output doesn't bloat the window —
        # but the status suffix is appended so a failure still surfaces.
        if exclude_from_context:
            # Report the true byte count (NB-20), not the truncation-capped
            # buffer length — the point of the stub is to tell the model how
            # much output it isn't seeing.
            stub = (
                f"[output shown to user; {total_bytes} bytes omitted from context]"
                + status_suffix
            )
            return ToolResult(content=[TextContent(text=stub)])
        return ToolResult(content=[TextContent(text=text)])

    return AgentTool(schema=schema, execute=_execute, execution_mode="sequential")


class _SpillWriter:
    """Lazily-opened file for the full over-cap Bash output.

    Best-effort: any open/write failure nulls ``path`` so the caller degrades to
    cap-only truncation. The file is opened on the first ``start`` (i.e. the
    first time output crosses the cap), so a small command never touches disk.
    """

    def __init__(self, env: ToolEnv):
        self._base = env.deferred_dir or Path(tempfile.gettempdir())
        self._fh = None
        self.path: Path | None = None

    def start(self, head: bytes) -> None:
        if self._fh is not None or self.path is not None:
            return
        try:
            self._base.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix="bash_output_", suffix=".txt", dir=str(self._base))
            self._fh = os.fdopen(fd, "wb")
            self.path = Path(name)
            self._fh.write(head)
        except OSError:
            self._fail()

    def write(self, data: bytes) -> None:
        if self._fh is None:
            return
        try:
            self._fh.write(data)
        except OSError:
            self._fail()

    def _fail(self) -> None:
        if self._fh is not None:
            with contextlib.suppress(OSError):
                self._fh.close()
        self._fh = None
        self.path = None

    def close(self) -> None:
        if self._fh is not None:
            with contextlib.suppress(OSError):
                self._fh.close()
            self._fh = None


def _kill_process_group(proc) -> None:
    """SIGKILL the subprocess's whole process group.

    ``start_new_session=True`` gave the child its own group, so killing the
    group takes down any backgrounded grandchildren (and a bwrap wrapper) that
    a bare ``proc.kill()`` would leave running (NB-7). Falls back to killing the
    direct child if the group can't be resolved. Synchronous (no await) so it
    still fires while a CancelledError is unwinding the coroutine."""
    if proc.returncode is not None:
        return
    kill_process_group(proc.pid)


async def _reap(proc) -> None:
    # Best-effort: the SIGKILL already fired synchronously, so even if this await
    # is interrupted (CancelledError) the OS/asyncio child watcher still reaps
    # the dead process — this just avoids a "pending task destroyed" warning.
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
