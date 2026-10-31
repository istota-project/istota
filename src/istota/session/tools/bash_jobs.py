"""Background jobs: Bash's ``run_in_background``, BashOutput and KillShell.

One table per tool server, so per task attempt. A job is forked from the server
exactly as a foreground command is, so it is in the namespace and the task
cgroup by inheritance, and it cannot outlive the attempt: under bwrap the
namespace ends with the server, and everywhere else ``kill_all_jobs`` runs on
the server's way out (``tool_server.main``). Two things only the namespace
ends: a job after a SIGKILL to the server, and a child that closed the job's
pipe once its command has exited, which has detached (``nohup x >log &``) and
is left alone as it is under a foreground command.

There is no Monitor tool. BashOutput's ``wait_seconds`` blocks until the job
ends or the window does, which covers waiting on a build, and watching a file
is a background ``tail -f | grep -m1`` read the same way. No result arrives
unasked, because neither brain has a way to deliver one between turns.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
import weakref
from dataclasses import dataclass, field

from istota.agent.tools import AgentTool, ToolResult
from istota.llm.types import TextContent, ToolParameter, ToolSchema
from istota.sandbox.shell_exec import SIGPIPE_EXIT, SIGPIPE_NOTE

from .bash import _READ_CHUNK_BYTES, _reap, _SpillWriter, spawn_shell
from .env import ToolEnv

MAX_RUNNING_JOBS = 8
MAX_KEPT_JOBS = 32
# The foreground spill is bounded by its command's timeout and a job has none,
# and `temp_dir` is tmpfs on the stack, charged to the task's memory limit.
JOB_OUTPUT_LIMIT_BYTES = 64 * 1024 * 1024
# Under `HARNESS_TEXT_CAP_CHARS`, so the dispatcher never cuts a read's status
# line off, whatever `max_output_bytes` an operator sets.
_MAX_READ_BYTES = 30_000
_COMMAND_SHOWN_CHARS = 120

BASH_OUTPUT_SCHEMA = ToolSchema(
    name="BashOutput",
    description=(
        "Read the output of a job started with Bash `run_in_background`. Each "
        "read resumes where the last one ended and closes with the job's status "
        "and the next offset; pass `offset` to read from a byte offset instead. "
        "`wait_seconds` blocks until the job ends or that many seconds pass, "
        "which is how to wait for a build. Output cannot arrive unasked, so read "
        "again to see what changed. With no `job_id`, lists this task's jobs."
    ),
    parameters=[
        ToolParameter(name="job_id", type="string", description="The job id Bash returned.", required=False),
        ToolParameter(name="offset", type="integer", description="Byte offset to read from.", required=False),
        ToolParameter(
            name="wait_seconds",
            type="integer",
            description="Seconds to wait for the job to end before reading.",
            required=False,
        ),
    ],
)

KILL_SHELL_SCHEMA = ToolSchema(
    name="KillShell",
    description=(
        "Stop a job started with Bash `run_in_background`, along with everything "
        "in its process group. Its output stays readable with BashOutput. A job "
        "that has already ended is left alone, and so is a child it detached by "
        "redirecting the output away."
    ),
    parameters=[
        ToolParameter(name="job_id", type="string", description="The job id Bash returned."),
    ],
)


@dataclass
class _Job:
    id: str
    command: str
    proc: object
    spill: _SpillWriter | None
    head: bytearray = field(default_factory=bytearray)
    spilled_bytes: int = 0
    total_bytes: int = 0
    cursor: int = 0
    # Set once, before `ended`: "killed" or "timed out after Ns". None is a job
    # that ran to its own exit, whose returncode says how.
    stopped_as: str | None = None
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    started_at: float = field(default_factory=time.monotonic)
    pump: asyncio.Task | None = None

    @property
    def stored_bytes(self) -> int:
        if self.spill is not None and self.spill.path is not None:
            return self.spilled_bytes
        return len(self.head)

    def read(self, offset: int, size: int) -> bytes:
        if self.spill is not None and self.spill.path is not None:
            return self.spill.read_at(offset, size)
        return bytes(self.head[offset:offset + size])

    def status(self) -> str:
        if not self.ended.is_set():
            # The elapsed time also keeps two polls of a quiet job from being
            # identical, which the native loop detector would end the attempt on.
            running = f"running for {time.monotonic() - self.started_at:.0f}s"
            if self.proc.returncode is not None:
                return f"{running}, its command exited {self.proc.returncode} and a child holds its output"
            return running
        if self.stopped_as is not None:
            return self.stopped_as
        if self.proc.returncode == SIGPIPE_EXIT:
            return f"exit code: {SIGPIPE_EXIT} {SIGPIPE_NOTE}"
        return f"exit code: {self.proc.returncode}"


class BackgroundJobs:
    """The jobs of one tool server."""

    def __init__(self, env: ToolEnv) -> None:
        self._env = env
        self._jobs: dict[str, _Job] = {}
        self._next_id = 0
        _TABLES.add(self)

    def get(self, job_id: str) -> _Job | None:
        return self._jobs.get(job_id)

    def jobs(self) -> list[_Job]:
        return list(self._jobs.values())

    async def start(self, command: str, timeout_ms) -> ToolResult:
        running = [job.id for job in self._jobs.values() if not job.ended.is_set()]
        if len(running) >= MAX_RUNNING_JOBS:
            return _error(
                f"Already running {len(running)} background jobs, the most one task "
                f"may hold: {', '.join(running)}. Stop one with KillShell or wait for it."
            )
        try:
            proc = await spawn_shell(self._env, command, stdin=asyncio.subprocess.DEVNULL)
        except (OSError, ValueError) as exc:
            return _error(f"Failed to start command: {exc}")

        self._next_id += 1
        spill = _SpillWriter(self._env) if self._env.bash_spill_full_output else None
        job = _Job(id=f"bg_{self._next_id}", command=command, proc=proc, spill=spill)
        self._jobs[job.id] = job
        job.pump = asyncio.ensure_future(self._pump(job, timeout_ms))
        self._forget_oldest_finished()
        return ToolResult(content=[TextContent(text=(
            f"Started background job {job.id}. Read it with BashOutput and stop it "
            "with KillShell. It is killed when this task attempt ends."
        ))])

    def _forget_oldest_finished(self) -> None:
        excess = len(self._jobs) - MAX_KEPT_JOBS
        for job in list(self._jobs.values()):
            if excess <= 0:
                return
            if job.ended.is_set():
                if job.spill is not None:
                    job.spill.close()
                del self._jobs[job.id]
                excess -= 1

    async def _pump(self, job: _Job, timeout_ms) -> None:
        """A job has ended once its pipe is closed and its command has exited.

        Unlike a foreground call, neither alone ends it. `server & echo up`
        exits at once with the server still writing to the pipe, and
        `exec > log; build` closes the pipe with the build still running.
        """
        proc = job.proc
        timeout_s = int(timeout_ms) / 1000.0 if timeout_ms else None
        deadline = time.monotonic() + timeout_s if timeout_s else None
        pipe_open = True
        try:
            while job.stopped_as is None:
                wait = 0.5
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        job.stopped_as = f"timed out after {timeout_s:.0f}s"
                        break
                    wait = min(wait, remaining)
                if not pipe_open:
                    if proc.returncode is not None:
                        break
                    await asyncio.sleep(min(wait, 0.05))
                    continue
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(_READ_CHUNK_BYTES), timeout=wait)
                except asyncio.TimeoutError:
                    continue
                if chunk:
                    self._store(job, chunk)
                else:
                    pipe_open = False
        finally:
            # A job that ended by itself is not signalled: its number may be
            # free by now, and a child that closed the pipe has detached.
            if pipe_open or proc.returncode is None:
                _kill_group(proc)
            await _reap(proc)
            job.ended.set()

    def _store(self, job: _Job, chunk: bytes) -> None:
        job.total_bytes += len(chunk)
        cap = self._env.max_output_bytes or _MAX_READ_BYTES
        room = cap - len(job.head)
        if room > 0:
            job.head.extend(chunk[:room])
            chunk = chunk[room:]
            if len(job.head) >= cap and job.spill is not None:
                job.spill.start(bytes(job.head))
                job.spilled_bytes = len(job.head)
        if chunk and job.spill is not None:
            kept = chunk[:max(0, JOB_OUTPUT_LIMIT_BYTES - job.spilled_bytes)]
            if kept:
                job.spill.write(kept)
                job.spilled_bytes += len(kept)

    def kill_all(self) -> None:
        """Synchronous, because a signal handler and a ``finally`` call it."""
        for job in self._jobs.values():
            if not job.ended.is_set():
                _kill_group(job.proc)


_TABLES: weakref.WeakSet[BackgroundJobs] = weakref.WeakSet()


def kill_all_jobs() -> None:
    """Kill every job this process started."""
    for table in list(_TABLES):
        table.kill_all()


def _kill_group(proc) -> None:
    """Signal a job's group by its number, for a job that has not ended.

    Not ``kill_group_if_live``, which stops once the leader is reaped: the
    command in ``server & echo up`` has been, and the server is what needs
    killing. Its group number stays reserved while the group has a member.
    """
    with contextlib.suppress(OSError):
        os.killpg(proc.pid, signal.SIGKILL)


def _error(text: str) -> ToolResult:
    return ToolResult(content=[TextContent(text=text)], is_error=True)


def _whole_characters(data: bytes) -> bytes:
    """``data`` without a UTF-8 sequence its end cuts in two, unless that leaves
    nothing, in which case the bytes are not text worth protecting."""
    for back in range(1, min(4, len(data) + 1)):
        byte = data[-back]
        if byte & 0xC0 == 0x80:
            continue
        needed = 2 if byte & 0xE0 == 0xC0 else 3 if byte & 0xF0 == 0xE0 else 4 if byte & 0xF8 == 0xF0 else 1
        if needed > back and len(data) > back:
            return data[:-back]
        break
    return data


def _status_line(job: _Job, next_offset: int) -> str:
    parts = [f"job {job.id}: {job.status()}"]
    stored = job.stored_bytes
    if next_offset < stored:
        parts.append(f"{stored - next_offset} more bytes, read on with offset={next_offset}")
    else:
        parts.append(f"next offset={next_offset}")
    if job.spill is not None and job.spill.path is not None:
        parts.append(f"full output: {job.spill.path}")
    if job.total_bytes > stored:
        parts.append(f"{job.total_bytes - stored} bytes of output were not kept")
    return "[" + "; ".join(parts) + "]"


async def _wait_for_end(job: _Job, seconds: float, abort: asyncio.Event | None) -> None:
    if seconds <= 0 or job.ended.is_set():
        return
    waiters = [asyncio.ensure_future(job.ended.wait())]
    if abort is not None:
        waiters.append(asyncio.ensure_future(abort.wait()))
    try:
        await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()


def make_bash_output_tool(env: ToolEnv, jobs: BackgroundJobs) -> AgentTool:
    async def _execute(call_id, args, on_update, abort):
        job_id = args.get("job_id")
        if not job_id:
            return ToolResult(content=[TextContent(text=_listing(jobs))])
        job = jobs.get(str(job_id))
        if job is None:
            return _error(f"Unknown background job: {job_id}")

        wait = args.get("wait_seconds")
        if wait:
            await _wait_for_end(job, min(float(wait), float(env.bash_timeout_seconds)), abort)

        offset = args.get("offset")
        start = job.cursor if offset is None else max(0, int(offset))
        size = min(env.max_output_bytes or _MAX_READ_BYTES, _MAX_READ_BYTES)
        data = job.read(start, size)
        stored = job.stored_bytes
        more_may_come = not job.ended.is_set() or job.total_bytes > stored
        if more_may_come or start + len(data) < stored:
            data = _whole_characters(data)
        next_offset = start + len(data)
        job.cursor = next_offset

        status = _status_line(job, next_offset)
        text = data.decode("utf-8", "replace")
        body = f"{text}\n{status}" if text else status
        return ToolResult(content=[TextContent(text=body)])

    return AgentTool(schema=BASH_OUTPUT_SCHEMA, execute=_execute, execution_mode="parallel")


def _listing(jobs: BackgroundJobs) -> str:
    entries = jobs.jobs()
    if not entries:
        return "No background jobs."
    lines = []
    for job in entries:
        command = " ".join(job.command.split())
        if len(command) > _COMMAND_SHOWN_CHARS:
            command = command[:_COMMAND_SHOWN_CHARS - 3] + "..."
        lines.append(f"{job.id}: {job.status()}: {command}")
    return "\n".join(lines)


def make_kill_shell_tool(jobs: BackgroundJobs) -> AgentTool:
    async def _execute(call_id, args, on_update, abort):
        job_id = str(args["job_id"])
        job = jobs.get(job_id)
        if job is None:
            return _error(f"Unknown background job: {job_id}")
        if not job.ended.is_set():
            job.stopped_as = "killed"
            _kill_group(job.proc)
            await job.ended.wait()
        return ToolResult(content=[TextContent(text=f"[job {job.id}: {job.status()}]")])

    return AgentTool(schema=KILL_SHELL_SCHEMA, execute=_execute, execution_mode="sequential")
