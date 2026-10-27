"""The tool server's daemon side, shared by NativeBrain and the CLI tool harness.

Two things live here.

**The shared tool-server start.** ``start_request_tool_server``,
``request_hello`` and ``request_tool_workspace`` were NativeBrain's
``_start_tool_server``, ``_hello_payload`` and ``_tool_workspace``. NativeBrain
keeps those names as one-line delegates. The env rules moved with them:
``without_claude_runtime_env`` on the spawn env and on the hello's
``subprocess_env``, and ``or {}`` (never ``or None``) on the spawn env, because
``None`` there inherits the daemon's whole environment.

**The tool harness** (``tool_harness``): a ``claude`` CLI running outside bwrap
with its tools in the tool server. The CLI spawns
``python -I -m istota.sandbox.mcp_adapter --socket <path>`` from the
``mcp_config.json`` written here; the adapter connects to a Unix socket this
module listens on, and each call goes through a ``ToolDispatcher`` over
``build_remote_tools`` to the tool server under the NATIVE profile, or, for
``WebFetch`` when ``HarnessSpec.web_fetch`` carries a policy, to the
daemon-side tool native uses (``request_web_fetch_tool``).

``claude_code.py`` reaches the harness only through ``tool_harness``,
``HarnessHandle``, ``set_cli_root`` and ``release_calls``. The listener, the
peer check and the per-harness ``AsyncRuntime`` are private: they are the
relay the unified tool server (#862) deletes, so nothing outside this module
may depend on them.

- **One loop per harness**: an ``AsyncRuntime`` instance on its own thread, not
  the process-wide Talk runtime and never ``asyncio.run`` on the caller's
  thread.
- **The socket** sits in ``tempfile.gettempdir()``, not under the control
  directory, which is bound read-only into the sandbox and so connectable from
  inside it. 0600 before ``listen``; ``bind`` fails on an existing path and
  nothing here unlinks a path it did not create.
- **Peer check**: a connection is admitted only when its peer descends from the
  CLI process ``set_cli_root`` named last. A nonce would be readable by the
  same uid (ISSUE-550). ``PeerRoots.contains`` blocks on a condition, so it
  runs in an executor, never on the loop.
- **Calls are held** until ``release_calls``: the brain releases them once the
  CLI's ``system/init`` frame passed its check, so no tool runs for a CLI that
  is about to be killed. ``set_cli_root`` holds them again, since a respawned
  CLI has its own frame to pass.
- **Failure**: a dead tool server fails every pending and later call, sets
  ``HarnessHandle.failure`` and calls ``on_failure``, which the brain uses to
  kill the CLI's group. A start failure is reported the same way: the handle
  is yielded with ``failure`` set, so the brain needs no exception type from
  this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import re
import secrets
import socket
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

from istota.agent.events import HARNESS_MCP_SERVER
from istota.async_runtime import AsyncRuntime
from istota.llm.providers import CLI_AUTH_VARIABLE
from istota.sandbox import tool_server_protocol as proto
from istota.sandbox.claude_runtime_env import without_claude_runtime_env
from istota.sandbox.peer_process import PeerRoots, peer_pid
from istota.session.tools.dispatch import HARNESS_TEXT_CAP_CHARS, ToolDispatcher

from ._cli_config_dir import _write_new_file, make_cli_config_dir
from ._types import BrainRequest

logger = logging.getLogger(__name__)

# How long an accepted connection waits for its CLI to be registered. The CLI
# spawns the adapter well after `set_cli_root`, so this only covers a race.
_PEER_GRACE_SECONDS = 2.0

# Bounds on the teardown, which must always complete.
_CLOSE_TIMEOUT_SECONDS = 30.0
_RUNTIME_STOP_TIMEOUT_SECONDS = 10.0
_START_TIMEOUT_SECONDS = proto.STARTUP_TIMEOUT_SECONDS + 15.0

# `sun_path`: 104 bytes on macOS, 108 on Linux, including the terminating NUL.
_SUN_PATH_MAX = 104 if sys.platform == "darwin" else 108


# ---- The shared tool-server start -------------------------------------------


def _lexically_inside(path: Path, root: Path) -> bool:
    """``path`` is ``root`` or below it, by spelling. Nothing is resolved: the
    roots are enforced again, resolved, inside the tool server."""
    candidate = PurePath(os.path.normpath(str(path)))
    base = PurePath(os.path.normpath(str(root)))
    return candidate == base or base in candidate.parents


def request_tool_workspace(req: BrainRequest):
    """The confinement roots and the working directory for one request.

    When the executor supplies file-access roots (NB-1) the tools are confined
    to them. The cwd is ``req.cwd`` when it lies inside a write root, else the
    first write root, so a request whose cwd is a workspace inside the task's
    reach (`istota repl --workspace`) keeps it, and one whose cwd is the shared
    temp root does not land there. With no roots, ``req.cwd``.

    Shared by the ``hello`` frame and the daemon-side ``ToolEnv`` that
    ``WebFetch`` is bound to, so the two cannot disagree about where a task is.
    """
    read_roots = tuple(req.fs_read_roots) if req.fs_read_roots else None
    write_roots = tuple(req.fs_write_roots) if req.fs_write_roots else None
    write_denied_roots = tuple(req.fs_write_denied_roots or ())
    cwd = Path(req.cwd)
    if write_roots and not any(_lexically_inside(cwd, root) for root in write_roots):
        cwd = write_roots[0]
    return cwd, read_roots, write_roots, write_denied_roots


# URL harvester for require_url_provenance. Matches http(s) URLs in free text;
# trailing punctuation the wrapping prose contributes is trimmed.
_URL_RE = re.compile(r"https?://[^\s<>\"'`\])}]+", re.IGNORECASE)


def extract_urls(text: str) -> frozenset[str]:
    if not text:
        return frozenset()
    return frozenset(m.rstrip(".,;:!?") for m in _URL_RE.findall(text))


def request_web_fetch_tool(req: BrainRequest, policy):
    """The daemon-side ``WebFetch`` for one request, or ``None``.

    ``None`` when ``policy`` is absent or disabled. Shared by NativeBrain and
    the CLI harness's bridge, so both bind it to the same workspace and the
    same provenance corpus: the URLs in ``req.prompt``, the user half only.
    It runs in the daemon, not the tool server: a credential-free request
    whose hardening is about the addresses it resolves.
    """
    if policy is None or not policy.enabled:
        return None
    from istota.session.tools import ToolEnv, make_web_fetch_tool

    corpus = extract_urls(req.prompt) if policy.require_url_provenance else None
    cwd, read_roots, write_roots, write_denied_roots = request_tool_workspace(req)
    return make_web_fetch_tool(ToolEnv(
        cwd=cwd,
        read_roots=read_roots,
        write_roots=write_roots,
        write_denied_roots=write_denied_roots,
        web_fetch=policy,
        web_fetch_url_corpus=corpus,
    ))


def request_hello(req: BrainRequest, *, bash_spill_full_output: bool) -> dict:
    """What the tool server is told about this task.

    ``subprocess_env`` is ``req.env`` minus the Claude runtime names
    (ISSUE-390): ``build_clean_env`` puts the CLI's credential in every task
    env whatever brain runs it, and nothing in the tool server reads it, so its
    only effect would be a Bash ``echo`` that returns it to the model.

    **The strip happens here and at the spawn, and both are needed.** This
    frame decides what a Bash child is handed; the spawn decides what the
    server process carries, which a Bash child reads from
    ``/proc/<server-pid>/environ``.

    ``or None`` goes on the input, never the output: ``ToolEnv`` reads ``None``
    as "inherit the parent environment", so collapsing a fully stripped ``{}``
    back to ``None`` would hand the server, and through ``merge_proxy_env`` the
    Bash child, the daemon's whole environment.

    The three caps come from ``ToolEnv``'s own defaults: they were never
    request-derived, and restating them would be a second default.
    """
    from istota.session.tools import ToolEnv, hello_payload

    defaults = ToolEnv(cwd=Path(req.cwd))
    cwd, read_roots, write_roots, write_denied_roots = request_tool_workspace(req)
    deferred = (req.env or {}).get("ISTOTA_DEFERRED_DIR")
    return hello_payload(
        cwd=cwd,
        subprocess_env=without_claude_runtime_env(req.env or None),
        read_roots=read_roots,
        write_roots=write_roots,
        write_denied_roots=write_denied_roots,
        deferred_dir=Path(deferred) if deferred else None,
        bash_timeout_seconds=max(1, req.timeout_seconds),
        max_output_bytes=defaults.max_output_bytes,
        max_read_lines=defaults.max_read_lines,
        max_read_bytes=defaults.max_read_bytes,
        bash_spill_full_output=bash_spill_full_output,
    )


async def start_request_tool_server(
    req: BrainRequest,
    abort: asyncio.Event,
    *,
    bash_spill_full_output: bool,
):
    """One sandboxed tool server for this attempt, or ``None``.

    ``None`` for a text-only request (empty ``allowed_tools``): those callers
    spawn nothing.

    The wrap is ``req.native_sandbox_wrap``, never ``req.sandbox_wrap``, which
    builds the CLI's namespace, credential and all (ISSUE-389). A failure raises
    ``ToolServerError``; there is no fallback to in-process tools.

    ``start_tool_server`` is looked up on ``istota.session.tools`` at call time,
    which is where the tests patch it.
    """
    if not req.allowed_tools:
        return None
    from istota.session.tools import start_tool_server

    return await start_tool_server(
        request_hello(req, bash_spill_full_output=bash_spill_full_output),
        sandbox_wrap=req.native_sandbox_wrap,
        task_cgroup_path=req.task_cgroup,
        cwd=Path(req.cwd),
        # Stripped here as well as in the hello: this is what the server
        # process itself carries. `or {}`, never `or None`, which would inherit
        # the daemon's environment into the namespace.
        env=without_claude_runtime_env(req.env) if req.env else {},
        on_pid=req.on_pid,
        loop_abort=abort,
    )


# ---- The tool harness ---------------------------------------------------------


@dataclass
class HarnessHandle:
    mcp_config_path: Path
    config_dir: Path        # CLAUDE_CONFIG_DIR
    home_dir: Path          # HOME
    tmp_dir: Path           # TMPDIR
    bin_dir: Path           # first on the CLI's PATH: the stub git
    cwd: Path               # the CLI's cwd: the tool server's cwd
    offered: tuple[str, ...]  # canonical names tools/list offers
    failure: str | None = None  # the tool server died or the harness failed
    _bridge: Any = field(default=None, repr=False, compare=False)


def write_mcp_config(path: Path, socket_path: str) -> None:
    """The ``--mcp-config`` file: one stdio server, the adapter, no env.

    The CLI passes its own env to the adapter whatever ``env`` says, which is
    why the adapter clears it first thing.
    """
    document = {
        "mcpServers": {
            HARNESS_MCP_SERVER: {
                "type": "stdio",
                "command": sys.executable,
                "args": ["-I", "-m", "istota.sandbox.mcp_adapter", "--socket", socket_path],
                "env": {},
            }
        }
    }
    _write_new_file(path, json.dumps(document))


#: What the CLI runs as `git`: git's own "not a git repository" status.
GIT_STUB = "#!/bin/sh\nexit 128\n"


def write_git_stub(bin_dir: Path) -> Path:
    """Create ``bin_dir`` 0700 holding a ``git`` that exits 128, and return it.

    The unwrapped CLI runs ``git`` in its cwd at start-up, outside the sandbox,
    in a folder the model can write to; with this first on its PATH no real git
    reads that folder's config. The tool server keeps its own PATH.
    """
    bin_dir.mkdir(mode=0o700)
    stub = bin_dir / "git"
    _write_new_file(stub, GIT_STUB)
    os.chmod(stub, 0o700)
    return stub


def _socket_path(task_id: int) -> str:
    name = f"istota-tools-{os.getpid()}-{int(task_id)}-{secrets.token_hex(4)}.sock"
    return os.path.join(tempfile.gettempdir(), name)


def _error_wire(text: str) -> dict:
    return {"content": [{"type": proto.CONTENT_TEXT, "text": text}], "is_error": True}


class _Connection:
    """One admitted adapter connection: its writer and its calls in flight."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.write_lock = asyncio.Lock()
        self.calls: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}

    async def send(self, message: dict) -> None:
        async with self.write_lock:
            self.writer.write(proto.encode(message))
            await self.writer.drain()

    def close(self) -> None:
        for task, abort in list(self.calls.values()):
            abort.set()
            task.cancel()
        with contextlib.suppress(Exception):
            self.writer.close()


class _Bridge:
    """Everything behind a ``HarnessHandle``. Private to this module."""

    def __init__(
        self,
        req: BrainRequest,
        *,
        brain_label: str,
        on_failure: Callable[[], None],
    ) -> None:
        self._req = req
        self._label = brain_label
        self._on_failure = on_failure
        self._runtime = AsyncRuntime()
        self._roots = PeerRoots()
        self._server = None
        self._dispatcher: ToolDispatcher | None = None
        self._listener: asyncio.AbstractServer | None = None
        self._socket_path: str | None = None
        self._socket_created = False
        self._connections: set[_Connection] = set()
        self._handler_tasks: set[asyncio.Task] = set()
        self._watcher: asyncio.Task | None = None
        self._released: asyncio.Event | None = None
        self._failed: asyncio.Event | None = None
        self.handle: HarnessHandle | None = None

    # -- start ---------------------------------------------------------------

    def start(self) -> HarnessHandle:
        """Make the directory, start the server, open the listener, write the
        config. Never raises: a failure is returned on ``handle.failure``."""
        spec = self._req.harness
        cwd = request_tool_workspace(self._req)[0]
        if spec is None:
            return self._failed_handle(cwd, Path(), "the request carries no harness spec")

        root = Path(spec.harness_root)
        harness_dir = root / f"h-{int(self._req.attempt)}-{secrets.token_hex(4)}"
        handle = HarnessHandle(
            mcp_config_path=harness_dir / "mcp_config.json",
            config_dir=harness_dir / "config",
            home_dir=harness_dir / "home",
            tmp_dir=harness_dir / "tmp",
            bin_dir=harness_dir / "bin",
            cwd=cwd,
            offered=(),
            _bridge=self,
        )
        self.handle = handle

        try:
            harness_dir.mkdir(mode=0o700)
            for sub in (handle.home_dir, handle.tmp_dir):
                sub.mkdir(mode=0o700)
            write_git_stub(handle.bin_dir)
        except OSError as exc:
            return self._start_failed(f"could not create the harness directory: {exc}")
        try:
            make_cli_config_dir(
                harness_dir,
                spec.cli_env.get(CLI_AUTH_VARIABLE),
                home=spec.cli_env.get("HOME") or os.environ.get("HOME", ""),
                launch_cwd=cwd,
            )
        except OSError as exc:
            return self._start_failed(f"could not create the CLI config directory: {exc}")

        try:
            self._runtime.start()
        except Exception as exc:  # noqa: BLE001
            return self._start_failed(f"could not start the harness loop: {exc}")

        try:
            self._runtime.submit(
                self._astart(spec.bash_spill_full_output), timeout=_START_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 — every start failure is reported, not raised
            return self._start_failed(str(exc) or type(exc).__name__)

        handle.offered = self._dispatcher.offered if self._dispatcher else ()
        try:
            write_mcp_config(handle.mcp_config_path, self._socket_path or "")
        except OSError as exc:
            return self._start_failed(f"could not write the MCP config: {exc}")
        return handle

    def _failed_handle(self, cwd: Path, harness_dir: Path, reason: str) -> HarnessHandle:
        self.handle = HarnessHandle(
            mcp_config_path=harness_dir / "mcp_config.json",
            config_dir=harness_dir / "config",
            home_dir=harness_dir / "home",
            tmp_dir=harness_dir / "tmp",
            bin_dir=harness_dir / "bin",
            cwd=cwd,
            offered=(),
            failure=f"could not start the tool harness: {reason}",
            _bridge=self,
        )
        return self.handle

    def _start_failed(self, reason: str) -> HarnessHandle:
        assert self.handle is not None
        if self.handle.failure is None:
            self.handle.failure = f"could not start the tool harness: {reason}"
        logger.warning("tool harness start failed (%s): %s", self._label, reason)
        return self.handle

    async def _astart(self, bash_spill_full_output: bool) -> None:
        from istota.session.tools import build_remote_tools

        self._released = asyncio.Event()
        self._failed = asyncio.Event()
        server_abort = asyncio.Event()
        try:
            server = await start_request_tool_server(
                self._req, server_abort, bash_spill_full_output=bash_spill_full_output,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"the tool server did not start: {exc}") from exc
        if server is None:
            raise RuntimeError("the request grants no tools")
        self._server = server

        tools = {tool.schema.name: tool for tool in build_remote_tools(server)}
        web_fetch = request_web_fetch_tool(self._req, self._req.harness.web_fetch)
        if web_fetch is not None:
            tools[web_fetch.schema.name] = web_fetch
        self._dispatcher = ToolDispatcher(
            tools, offered=self._req.allowed_tools, text_cap=HARNESS_TEXT_CAP_CHARS,
        )
        self._watcher = asyncio.ensure_future(self._watch_server(server_abort))
        await self._open_listener()

    async def _open_listener(self) -> None:
        path = _socket_path(self._req.task_id)
        if len(os.fsencode(path)) >= _SUN_PATH_MAX:
            raise RuntimeError(
                f"the bridge socket path is {len(os.fsencode(path))} bytes, over the "
                f"{_SUN_PATH_MAX - 1}-byte limit; set a shorter TMPDIR for the daemon"
            )
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # Fails on an existing path; nothing here unlinks a path it did not
            # create.
            sock.bind(path)
            self._socket_path = path
            self._socket_created = True
            os.chmod(path, 0o600)
            self._listener = await asyncio.start_unix_server(self._accept, sock=sock)
        except Exception as exc:
            with contextlib.suppress(Exception):
                sock.close()
            raise RuntimeError(f"could not open the bridge socket: {exc}") from exc

    # -- failure -------------------------------------------------------------

    async def _watch_server(self, server_abort: asyncio.Event) -> None:
        # `RemoteToolServer._fail` is the only thing that sets this event.
        await server_abort.wait()
        server = self._server
        if server is not None and server.failure is not None:
            self._fail(server.failure)

    def _fail(self, reason: str) -> None:
        handle = self.handle
        if handle is None or handle.failure is not None:
            return
        handle.failure = reason
        if self._failed is not None:
            self._failed.set()
        logger.warning("tool harness failed (%s): the tool server is gone", self._label)
        try:
            self._on_failure()
        except Exception:  # noqa: BLE001 — never raise into the loop
            logger.exception("tool harness on_failure raised")

    # -- peers ---------------------------------------------------------------

    def set_root(self, pid: int) -> None:
        for old in self._roots.pids:
            self._roots.revoke(old)
        if not self._roots.authorize(pid):
            logger.warning("tool harness: the CLI (pid %d) was gone before it was registered", pid)
        # The previous CLI's adapter belongs to a process that is gone, and a
        # respawned CLI has its own init check to pass before calls run again.
        if self._runtime.is_running:
            with contextlib.suppress(Exception):
                self._runtime.loop.call_soon_threadsafe(self._new_root)

    def _new_root(self) -> None:
        if self._released is not None:
            self._released.clear()
        self._drop_connections()

    def _drop_connections(self) -> None:
        for conn in list(self._connections):
            conn.close()
        self._connections.clear()

    def release(self) -> None:
        if self._released is not None and self._runtime.is_running:
            with contextlib.suppress(Exception):
                self._runtime.loop.call_soon_threadsafe(self._released.set)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handler_tasks.add(task)
        try:
            await self._serve_connection(reader, writer)
        finally:
            if task is not None:
                self._handler_tasks.discard(task)

    async def _serve_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        pid = peer_pid(writer.get_extra_info("socket"))
        loop = asyncio.get_running_loop()
        admitted = await loop.run_in_executor(
            None, functools.partial(self._roots.contains, pid, grace_seconds=_PEER_GRACE_SECONDS),
        )
        if not admitted:
            logger.warning("tool harness refused a connection from pid %s", pid)
            with contextlib.suppress(Exception):
                writer.close()
            return

        conn = _Connection(writer)
        self._connections.add(conn)
        decoder = proto.FrameDecoder()
        greeted = False
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    return
                for msg in decoder.feed(data):
                    kind = msg.get("type")
                    if kind == proto.MSG_HELLO and not greeted:
                        greeted = True
                        await conn.send({
                            "type": proto.MSG_READY,
                            "protocol": proto.PROTOCOL_VERSION,
                            "tools": self._dispatcher.mcp_tool_list() if self._dispatcher else [],
                        })
                    elif kind == proto.MSG_CALL and greeted:
                        self._start_call(conn, msg)
                    elif kind == proto.MSG_ABORT and greeted:
                        entry = conn.calls.get(str(msg.get("id")))
                        if entry is not None:
                            entry[1].set()
                    elif kind == proto.MSG_SHUTDOWN:
                        return
                    else:
                        logger.warning("tool harness: unexpected %r frame from the adapter", kind)
                        await conn.send({"type": proto.MSG_FATAL, "message": f"unexpected {kind!r} frame"})
                        return
        except asyncio.CancelledError:
            raise
        except proto.ProtocolError as exc:
            logger.warning("tool harness: bad frame from the adapter: %s", type(exc).__name__)
            with contextlib.suppress(Exception):
                await conn.send({"type": proto.MSG_FATAL, "message": "bad frame"})
        except Exception:  # noqa: BLE001 — a connection failure ends that connection
            logger.debug("tool harness connection failed", exc_info=True)
        finally:
            conn.close()
            self._connections.discard(conn)

    def _start_call(self, conn: _Connection, msg: dict) -> None:
        call_id = str(msg.get("id"))
        name = str(msg.get("tool") or "")
        args = msg.get("args")
        abort = asyncio.Event()
        task = asyncio.ensure_future(
            self._run_call(conn, call_id, name, args if isinstance(args, dict) else {}, abort)
        )
        conn.calls[call_id] = (task, abort)
        task.add_done_callback(lambda _t, key=call_id: conn.calls.pop(key, None))

    async def _run_call(
        self, conn: _Connection, call_id: str, name: str, args: dict, abort: asyncio.Event,
    ) -> None:
        outcome = await self._outcome(call_id, name, args, abort)
        with contextlib.suppress(Exception):
            await conn.send({
                "type": proto.MSG_RESULT,
                "id": call_id,
                "content": outcome["content"],
                "is_error": outcome["is_error"],
            })

    async def _outcome(self, call_id: str, name: str, args: dict, abort: asyncio.Event) -> dict:
        from istota.session.tools.remote import content_to_wire

        if not await self._wait_for_release():
            return _error_wire(f"Tool server failed: {self.handle.failure}")
        if abort.is_set():
            return _error_wire("Tool call aborted")
        try:
            result = await self._dispatcher.call(call_id, name, args, None, abort)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a call failure is a result
            return _error_wire(f"Tool error: {exc}")
        return {"content": content_to_wire(result.content), "is_error": bool(result.is_error)}

    async def _wait_for_release(self) -> bool:
        """True once calls may run, False once the harness failed."""
        if self._failed.is_set():
            return False
        if self._released.is_set():
            return True
        released = asyncio.ensure_future(self._released.wait())
        failed = asyncio.ensure_future(self._failed.wait())
        try:
            await asyncio.wait({released, failed}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            released.cancel()
            failed.cancel()
        return not self._failed.is_set()

    # -- close ---------------------------------------------------------------

    def close(self) -> None:
        """Listener closed and the socket unlinked, the server shut down and
        reaped, the loop stopped. Never raises."""
        try:
            if self._runtime.is_running:
                self._runtime.submit(self._aclose(), timeout=_CLOSE_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001
            logger.warning("tool harness close did not finish cleanly", exc_info=True)
        finally:
            self._unlink_socket()
            with contextlib.suppress(Exception):
                self._runtime.stop(timeout=_RUNTIME_STOP_TIMEOUT_SECONDS)

    async def _aclose(self) -> None:
        if self._watcher is not None:
            self._watcher.cancel()
        if self._listener is not None:
            with contextlib.suppress(Exception):
                self._listener.close()
        self._unlink_socket()
        self._drop_connections()
        for task in list(self._handler_tasks):
            task.cancel()
        if self._server is not None:
            with contextlib.suppress(BaseException):
                await self._server.aclose()

    def _unlink_socket(self) -> None:
        if self._socket_created and self._socket_path:
            self._socket_created = False
            with contextlib.suppress(FileNotFoundError, OSError):
                os.unlink(self._socket_path)


@contextmanager
def tool_harness(
    req: BrainRequest,
    *,
    brain_label: str,
    on_failure: Callable[[], None],
) -> Iterator[HarnessHandle]:
    """One harness: a directory, a tool server, a listener and the MCP config.

    The handle comes back with ``failure`` set when any of that could not be
    made; the caller fails the attempt with ``stop_reason="error"`` rather than
    starting the CLI. ``on_failure`` is called on the harness's own thread when
    the tool server dies.
    """
    bridge = _Bridge(req, brain_label=brain_label, on_failure=on_failure)
    try:
        handle = bridge.start()
    except BaseException:
        bridge.close()
        raise
    try:
        yield handle
    finally:
        bridge.close()


def set_cli_root(handle: HarnessHandle, pid: int) -> None:
    """The CLI was (re)spawned with ``pid``: admit its descendants, and only
    theirs, from now on, and hold calls until ``release_calls`` again."""
    if handle._bridge is not None:
        handle._bridge.set_root(pid)


def release_calls(handle: HarnessHandle) -> None:
    """The CLI's init check passed; held and later calls may run."""
    if handle._bridge is not None:
        handle._bridge.release()
