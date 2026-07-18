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
import signal
import time

from istota.agent.tools import AgentTool, ToolResult
from istota.llm.types import TextContent, ToolParameter, ToolSchema

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

        cmd = ["bash", "-c", command]
        if env.sandbox_wrap:
            cmd = env.sandbox_wrap(cmd)

        try:
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
            )
        except (OSError, ValueError) as exc:
            return ToolResult(content=[TextContent(text=f"Failed to start command: {exc}")])

        out = bytearray()
        total_bytes = 0
        truncated = False
        deadline = time.monotonic() + timeout_s
        status = "ok"

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
                if len(out) < env.max_output_bytes:
                    out.extend(chunk[: env.max_output_bytes - len(out)])
                    if len(out) >= env.max_output_bytes:
                        truncated = True
                if on_update is not None:
                    await on_update(chunk.decode("utf-8", "replace"))

            if status == "ok":
                # Reap so returncode is available for the exit-code suffix.
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=5)
        finally:
            _kill_process_group(proc)
            await _reap(proc)

        text = out.decode("utf-8", "replace")
        if truncated:
            text += f"\n… [output truncated at {env.max_output_bytes} bytes]"

        # Failure markers are kept separate so they can ride along even when the
        # body is excluded from context — a failed/aborted/timed-out command the
        # model can't see the status of would be reasoned about as a success.
        status_suffix = ""
        if status == "aborted":
            status_suffix = "\n[command aborted]"
        elif status == "timeout":
            status_suffix = f"\n[command timed out after {timeout_s:.0f}s]"
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


def _kill_process_group(proc) -> None:
    """SIGKILL the subprocess's whole process group.

    ``start_new_session=True`` gave the child its own group, so killing the
    group takes down any backgrounded grandchildren (and a bwrap wrapper) that
    a bare ``proc.kill()`` would leave running (NB-7). Falls back to killing the
    direct child if the group can't be resolved. Synchronous (no await) so it
    still fires while a CancelledError is unwinding the coroutine."""
    if proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


async def _reap(proc) -> None:
    # Best-effort: the SIGKILL already fired synchronously, so even if this await
    # is interrupted (CancelledError) the OS/asyncio child watcher still reaps
    # the dead process — this just avoids a "pending task destroyed" warning.
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
