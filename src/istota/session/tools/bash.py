"""Bash tool.

Runs a shell command via ``asyncio.create_subprocess_exec`` so it can stream
partial output (``on_update``), honor the ``abort`` event (kill on cancel), and
enforce a wall-clock timeout — none of which a blocking ``subprocess.run`` gives
cleanly. The raw argv is wrapped by ``ToolEnv.sandbox_wrap`` (bwrap on Linux,
no-op on macOS) so each command is sandboxed per-execution.
"""

from __future__ import annotations

import asyncio
import time

from istota.agent.tools import AgentTool, ToolResult
from istota.llm.types import TextContent, ToolParameter, ToolSchema

from .env import ToolEnv


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
            )
        except (OSError, ValueError) as exc:
            return ToolResult(content=[TextContent(text=f"Failed to start command: {exc}")])

        out = bytearray()
        truncated = False
        deadline = time.monotonic() + timeout_s
        status = "ok"

        while True:
            if abort is not None and abort.is_set():
                status = "aborted"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                status = "timeout"
                break
            try:
                chunk = await asyncio.wait_for(proc.stdout.readline(), timeout=min(remaining, 0.5))
            except asyncio.TimeoutError:
                if proc.returncode is not None:
                    break
                continue
            if not chunk:
                break  # EOF
            if len(out) < env.max_output_bytes:
                out.extend(chunk)
                if len(out) >= env.max_output_bytes:
                    truncated = True
            if on_update is not None:
                await on_update(chunk.decode("utf-8", "replace"))

        if status in ("aborted", "timeout"):
            _kill(proc)
            await _reap(proc)
        else:
            await proc.wait()

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
            stub = (
                f"[output shown to user; {len(out)} bytes omitted from context]"
                + status_suffix
            )
            return ToolResult(content=[TextContent(text=stub)])
        return ToolResult(content=[TextContent(text=text)])

    return AgentTool(schema=schema, execute=_execute, execution_mode="sequential")


def _kill(proc) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        pass


async def _reap(proc) -> None:
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        pass
