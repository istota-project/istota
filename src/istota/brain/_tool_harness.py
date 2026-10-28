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
with its tools in the tool server, which the CLI spawns itself (#862). The
harness writes ``mcp_config.json`` naming one stdio server,
``python -I -m istota.sandbox.tool_launcher --report FIFO --env-file PATH --
<wrapped tool server argv>``; the launcher clears the CLI's environment,
reports its pid down the FIFO and execs the tool server under the NATIVE wrap
with ``--mcp --hello``. Every call then goes CLI to tool server and back, two
processes, with the server's own ``ToolDispatcher`` applying the subset, the
lock and the text cap. Nothing in the daemon accepts a connection.

``claude_code.py`` reaches the harness only through ``tool_harness``,
``HarnessHandle``, ``set_cli_root`` and ``release_calls``.

- **The files**: in the harness dir (bound by no sandbox) the env file, the pid
  FIFO and the MCP config; in the task's control dir (bound read-only into the
  tool server's namespace) the hello and the call-gate FIFO. All 0600, all
  removed at close.
- **WebFetch** is a second MCP server, ``istota_web``
  (``brain/_web_fetch_mcp.py``), when the grant names it and
  ``HarnessSpec.web_fetch`` is on: unwrapped, in the daemon's network
  namespace as native's runs, reading its policy, corpus and gate from 0600
  files in the harness dir. It is never reported to the proxies.
  ``HarnessHandle.mcp_servers`` and ``mcp_tools`` are what ``system/init``
  must then list.
- **The pid report** is the one input that grants proxy access, so it fails
  closed: a pid that does not descend from the CLI ``set_cli_root`` named, or
  a second pid under the same CLI, is refused, SIGKILLed if it is still the
  process that was read, and fails the attempt. A checked pid goes to
  ``req.on_pid``; the CLI's never does.
- **Calls are held** by each server until ``release_calls`` writes one byte
  to its gate, once the brain's init check passed.
- **The watcher** fails the attempt when the server exits while the CLI is
  still running 2 s later and ``close`` has not begun. The pinned CLI ends a
  session by SIGINTing its server ~5 ms after the ``result`` line and exits
  ~100 ms after that, so an exit followed by the CLI's own is the normal end.
  The exit is also the reap report (``on_pid(0)``): the daemon is not the
  server's parent.
- **Failure**: a refused report or a dead server sets
  ``HarnessHandle.failure`` and calls ``on_failure``, which the brain uses to
  kill the CLI's group. A start failure is reported the same way, without the
  call: the handle is yielded with ``failure`` set, so the brain needs no
  exception type from this module.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import select
import shutil
import signal
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

from istota.agent.events import HARNESS_MCP_SERVER, WEB_FETCH_MCP_SERVER
from istota.llm.providers import CLI_AUTH_VARIABLE
from istota.sandbox.claude_runtime_env import without_claude_runtime_env
from istota.sandbox.peer_process import (
    descends_from,
    report_child_reaped,
    split_pid_lines,
    start_time,
)
from istota.sandbox.process_group import kill_process_group
from istota.session.tools.dispatch import HARNESS_TEXT_CAP_CHARS

from ._cli_config_dir import _write_new_file, make_cli_config_dir
from ._types import BrainRequest

logger = logging.getLogger(__name__)

# A report that arrives before `set_cli_root` waits this long for the CLI to be
# registered: the brain registers it as soon as `Popen` returns, and a fast CLI
# can spawn its server first.
_ROOT_GRACE_SECONDS = 2.0
# How long after the server exits the CLI may take to exit too before the exit
# counts as the server dying mid-attempt (Stage 1: ~100 ms in practice).
_EXIT_GRACE_SECONDS = 2.0
# How long `close` waits for the server to go on its own before killing it.
_CLOSE_WAIT_SECONDS = 5.0
_POLL_SECONDS = 0.25


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


def _web_fetch_corpus(req: BrainRequest, policy) -> frozenset[str] | None:
    """The provenance corpus: the URLs in ``req.prompt``, the user half only,
    and only when the policy asks for one."""
    return extract_urls(req.prompt) if policy.require_url_provenance else None


def request_web_fetch_tool(req: BrainRequest, policy):
    """The daemon-side ``WebFetch`` for one request, or ``None``.

    ``None`` when ``policy`` is absent or disabled. NativeBrain's; the CLI
    harness's ``istota_web`` server (``_web_fetch_mcp``) is bound to the same
    workspace and the same corpus (``_web_fetch_corpus``).
    It runs in the daemon, not the tool server: a credential-free request
    whose hardening is about the addresses it resolves.
    """
    if policy is None or not policy.enabled:
        return None
    from istota.session.tools import ToolEnv, make_web_fetch_tool

    corpus = _web_fetch_corpus(req, policy)
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
    (ISSUE-390). No task env carries the CLI's credential any more
    (``build_clean_env`` names the mode only), so this is defence in depth: a
    future env builder that put it back would hand it to a Bash ``echo``,
    since nothing in the tool server reads it.

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

    ``tools`` is the grant narrowed to what the server runs, so a turn whose
    grant leaves out Write is refused Write by the server's dispatcher as well
    as by the loop. No ``text_cap``: native's results are not capped.
    """
    from istota.session.tools import SANDBOXED_TOOL_NAMES, ToolEnv, hello_payload

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
        tools=[name for name in req.allowed_tools if name in SANDBOXED_TOOL_NAMES],
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

    The wrap is ``req.tool_server_wrap``. A failure raises
    ``ToolServerError``; there is no fallback to in-process tools.

    ``start_tool_server`` is looked up on ``istota.session.tools`` at call time,
    which is where the tests patch it.
    """
    if not req.allowed_tools:
        return None
    from istota.session.tools import start_tool_server

    return await start_tool_server(
        request_hello(req, bash_spill_full_output=bash_spill_full_output),
        sandbox_wrap=req.tool_server_wrap,
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
    offered: tuple[str, ...]  # canonical names the tool server's tools/list offers
    failure: str | None = None  # the tool server died or the harness failed
    # What `system/init` must list: every MCP server in the config, and every
    # tool they offer by its `mcp__<server>__<tool>` name.
    mcp_servers: tuple[str, ...] = ()
    mcp_tools: tuple[str, ...] = ()
    _harness: Any = field(default=None, repr=False, compare=False)


def write_mcp_config(
    path: Path, *, report_fifo: Path, env_file: Path, command: list[str],
    web_fetch_config: Path | None = None,
) -> None:
    """The ``--mcp-config`` file: the tool server's launcher, and with
    ``web_fetch_config`` the WebFetch server beside it. No env for either.

    The CLI passes its own env to every server whatever ``env`` says, which is
    why both clear it first thing. ``command`` is the wrapped tool server argv
    with an absolute ``argv[0]``: the launcher does not search.
    """
    servers = {
        HARNESS_MCP_SERVER: {
            "type": "stdio",
            "command": sys.executable,
            "args": [
                "-I", "-m", "istota.sandbox.tool_launcher",
                "--report", str(report_fifo),
                "--env-file", str(env_file),
                "--", *command,
            ],
            "env": {},
        }
    }
    if web_fetch_config is not None:
        servers[WEB_FETCH_MCP_SERVER] = {
            "type": "stdio",
            "command": sys.executable,
            "args": [
                "-I", "-m", "istota.brain._web_fetch_mcp",
                "--config", str(web_fetch_config),
            ],
            "env": {},
        }
    _write_new_file(path, json.dumps({"mcpServers": servers}))


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


class _StartFailed(Exception):
    pass


@dataclass(eq=False)
class _Server:
    """One reported, authorized tool server."""

    pid: int
    started: int
    exited: threading.Event = field(default_factory=threading.Event)
    reap_reported: bool = False


class _DirectHarness:
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
        self._lock = threading.Lock()
        self._root_changed = threading.Condition(self._lock)
        self._root: tuple[int, int] | None = None
        self._server: _Server | None = None
        self._closing = threading.Event()
        self._done = threading.Event()
        self._files: list[Path] = []
        self._env_file: Path | None = None
        self._env_text = ""
        self._gate_paths: list[Path] = []
        self._read_fd: int | None = None
        self._keep_fd: int | None = None
        self._reader: threading.Thread | None = None
        self.handle: HarnessHandle | None = None

    # -- start ---------------------------------------------------------------

    def start(self) -> HarnessHandle:
        """Make the directory and write the files the CLI and the tool server
        read. Never raises: a failure is returned on ``handle.failure``."""
        spec = self._req.harness
        cwd = request_tool_workspace(self._req)[0]
        if spec is None:
            return self._failed_handle(cwd, Path(), "the request carries no harness spec")

        token = secrets.token_hex(4)
        harness_dir = Path(spec.harness_root) / f"h-{int(self._req.attempt)}-{token}"
        handle = HarnessHandle(
            mcp_config_path=harness_dir / "mcp_config.json",
            config_dir=harness_dir / "config",
            home_dir=harness_dir / "home",
            tmp_dir=harness_dir / "tmp",
            bin_dir=harness_dir / "bin",
            cwd=cwd,
            offered=(),
            _harness=self,
        )
        self.handle = handle
        if not self._req.allowed_tools:
            return self._start_failed("the request grants no tools")

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
            self._write_tool_files(harness_dir, token)
        except _StartFailed as exc:
            return self._start_failed(str(exc))
        except OSError as exc:
            return self._start_failed(f"could not write the tool server's files: {exc}")
        except Exception as exc:  # noqa: BLE001 — every start failure is reported, not raised
            return self._start_failed(f"{type(exc).__name__}: {exc}")
        return handle

    def _write_tool_files(self, harness_dir: Path, token: str) -> None:
        from istota.session.tools.remote import server_command

        req = self._req
        spec = req.harness
        handle = self.handle
        control_dir = Path(spec.control_dir) if spec.control_dir is not None else harness_dir
        hello_path = control_dir / f"tool-hello-{token}.json"
        gate_path = control_dir / f"gate-{token}.fifo"
        # What `start_request_tool_server` gives the server process: `or {}`,
        # never `or None`, which would hand the launcher nothing to exec with.
        server_env = without_claude_runtime_env(req.env) if req.env else {}

        raw = server_command() + ["--mcp", "--hello", str(hello_path)]
        wrap = req.tool_server_wrap
        command = list(wrap(list(raw))) if wrap is not None else list(raw)
        if spec.control_dir is None and command != raw:
            raise _StartFailed(
                "the request names no control directory, and the sandboxed "
                "tool server can read its hello only from one"
            )
        if not os.path.isabs(command[0]):
            # `build_bwrap_cmd` returns a bare `bwrap`, and `execve` searches
            # nothing (Stage 1).
            found = shutil.which(command[0], path=server_env.get("PATH") or os.defpath)
            if found is None:
                raise _StartFailed(f"{command[0]!r} is not on the tool server's PATH")
            command[0] = os.path.abspath(found)

        hello = request_hello(req, bash_spill_full_output=spec.bash_spill_full_output)
        hello["text_cap"] = HARNESS_TEXT_CAP_CHARS
        hello["gate"] = str(gate_path)
        handle.offered = tuple(hello.get("tools") or ())
        handle.mcp_servers = (HARNESS_MCP_SERVER,)
        handle.mcp_tools = tuple(f"mcp__{HARNESS_MCP_SERVER}__{name}" for name in handle.offered)

        os.mkfifo(gate_path, 0o600)
        self._files.append(gate_path)
        self._gate_paths.append(gate_path)
        _write_new_file(hello_path, json.dumps(hello))
        self._files.append(hello_path)

        web_fetch_config = self._write_web_fetch_files(harness_dir)
        if web_fetch_config is not None:
            handle.mcp_servers += (WEB_FETCH_MCP_SERVER,)
            handle.mcp_tools += (f"mcp__{WEB_FETCH_MCP_SERVER}__WebFetch",)

        env_file = harness_dir / "tool-env.json"
        self._env_text = json.dumps(server_env)
        _write_new_file(env_file, self._env_text)
        self._files.append(env_file)
        self._env_file = env_file

        pid_fifo = harness_dir / "pid.fifo"
        os.mkfifo(pid_fifo, 0o600)
        self._files.append(pid_fifo)
        # The read end before the CLI spawns, so the launcher's non-blocking
        # open finds a reader. The write end this process keeps is only there
        # so the read end never sees end-of-file between launchers.
        self._read_fd = os.open(pid_fifo, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        self._keep_fd = os.open(pid_fifo, os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)

        write_mcp_config(
            handle.mcp_config_path,
            report_fifo=pid_fifo, env_file=env_file, command=command,
            web_fetch_config=web_fetch_config,
        )
        self._files.append(handle.mcp_config_path)

        self._reader = threading.Thread(
            target=self._read_reports, daemon=True, name="tool-harness-pids",
        )
        self._reader.start()

    def _write_web_fetch_files(self, harness_dir: Path) -> Path | None:
        """The ``istota_web`` server's config and gate, both in the harness dir,
        which no sandbox binds: that server runs outside every sandbox. Only
        when the grant names WebFetch and the policy is on. Not the tool
        server's control dir: nothing in a sandbox reads these."""
        from ._web_fetch_mcp import config_document

        req = self._req
        policy = req.harness.web_fetch
        if policy is None or not policy.enabled or "WebFetch" not in req.allowed_tools:
            return None
        gate_path = harness_dir / "web-gate.fifo"
        os.mkfifo(gate_path, 0o600)
        self._files.append(gate_path)
        self._gate_paths.append(gate_path)
        config_path = harness_dir / "web-fetch.json"
        document = config_document(
            policy,
            cwd=request_tool_workspace(req)[0],
            url_corpus=_web_fetch_corpus(req, policy),
            gate=gate_path,
        )
        _write_new_file(config_path, json.dumps(document))
        self._files.append(config_path)
        return config_path

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
            _harness=self,
        )
        return self.handle

    def _start_failed(self, reason: str) -> HarnessHandle:
        assert self.handle is not None
        if self.handle.failure is None:
            self.handle.failure = f"could not start the tool harness: {reason}"
        logger.warning("tool harness start failed (%s): %s", self._label, reason)
        return self.handle

    # -- pid reports ---------------------------------------------------------

    def _read_reports(self) -> None:
        tail = b""
        while not self._closing.is_set():
            try:
                ready, _, _ = select.select([self._read_fd], [], [], _POLL_SECONDS)
            except (OSError, ValueError):
                return
            if not ready:
                continue
            try:
                chunk = os.read(self._read_fd, 4096)
            except BlockingIOError:
                continue
            except OSError:
                return
            if not chunk:
                # No writer at this instant; a later launcher can still open it.
                time.sleep(_POLL_SECONDS)
                continue
            pids, tail = split_pid_lines(tail + chunk)
            if len(tail) > 32:
                tail = b""
            for pid in pids:
                try:
                    self._on_report(pid)
                except Exception:  # noqa: BLE001 — never raise out of the reader
                    logger.exception("tool harness: handling a pid report raised")

    def _on_report(self, pid: int) -> None:
        seen_start = start_time(pid)
        with self._lock:
            deadline = time.monotonic() + _ROOT_GRACE_SECONDS
            while self._root is None and not self._closing.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._root_changed.wait(remaining)
            root = self._root
            server = None
            if self._closing.is_set():
                verdict = "a tool server reported after the harness began closing"
            elif root is None:
                verdict = "a tool server reported before the CLI was registered"
            elif self._server is not None:
                verdict = "a second tool server reported under one CLI"
            elif seen_start is None:
                verdict = "a tool server exited before its report was checked"
            elif not descends_from(pid, {root[0]: root[1]}):
                verdict = "a tool server reported from outside the CLI"
            else:
                verdict = None
                server = _Server(pid=pid, started=seen_start)
                self._server = server
        if server is None:
            self._refuse(pid, seen_start, verdict, fail=not self._closing.is_set())
            return
        on_pid = self._req.on_pid
        if on_pid is not None:
            try:
                on_pid(pid)
            except Exception:  # noqa: BLE001
                logger.warning("tool harness: on_pid raised", exc_info=True)
        threading.Thread(
            target=self._watch, args=(server,), daemon=True, name="tool-harness-watch",
        ).start()

    def _refuse(self, pid: int, seen_start: int | None, reason: str, *, fail: bool) -> None:
        logger.warning("tool harness (%s) refused pid %d: %s", self._label, pid, reason)
        if seen_start is not None and start_time(pid) == seen_start:
            with suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        if fail:
            self._fail(reason)

    # -- the watcher ---------------------------------------------------------

    def _watch(self, server: _Server) -> None:
        self._wait_for_exit(server)
        server.exited.set()
        self._report_reaped(server)
        # The normal end: the CLI SIGINTs its server and exits a moment later,
        # and the brain leaves the harness. Either one ends this.
        if self._closing.wait(_EXIT_GRACE_SECONDS):
            return
        with self._lock:
            current = self._server is server
        if current and self._cli_alive():
            self._fail("the tool server exited")

    def _wait_for_exit(self, server: _Server) -> None:
        pidfd = None
        if hasattr(os, "pidfd_open"):
            try:
                pidfd = os.pidfd_open(server.pid)
            except ProcessLookupError:
                return
            except OSError:
                pidfd = None
            if pidfd is not None and start_time(server.pid) != server.started:
                os.close(pidfd)
                return
        try:
            while not self._done.is_set():
                if pidfd is not None:
                    try:
                        ready, _, _ = select.select([pidfd], [], [], _POLL_SECONDS)
                    except (OSError, ValueError):
                        return
                    if ready:
                        return
                else:
                    if start_time(server.pid) != server.started:
                        return
                    time.sleep(_POLL_SECONDS)
        finally:
            if pidfd is not None:
                os.close(pidfd)

    def _report_reaped(self, server: _Server) -> None:
        with self._lock:
            if server.reap_reported:
                return
            server.reap_reported = True
        report_child_reaped(self._req.on_pid)

    def _cli_alive(self) -> bool:
        with self._lock:
            root = self._root
        if root is None:
            return False
        pid, started = root
        if start_time(pid) != started:
            return False
        # The CLI is the brain's child: an exited one not yet reaped still has
        # its start time, and `WNOWAIT` asks without reaping it.
        try:
            exited = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return True
        except (OSError, AttributeError):
            return True
        return exited is None

    def _fail(self, reason: str) -> None:
        handle = self.handle
        with self._lock:
            if handle is None or handle.failure is not None or self._closing.is_set():
                return
            handle.failure = reason
        logger.warning("tool harness failed (%s): %s", self._label, reason)
        try:
            self._on_failure()
        except Exception:  # noqa: BLE001 — never raise into a harness thread
            logger.exception("tool harness on_failure raised")

    # -- the seam ------------------------------------------------------------

    def set_root(self, pid: int) -> None:
        started = start_time(pid)
        with self._lock:
            old = self._server
            self._server = None
            self._root = None
            report_old = old is not None and not old.reap_reported
            if report_old:
                old.reap_reported = True
        # A respawned CLI: the previous one's server belongs to a process that
        # is gone, and the new CLI's launcher reports a server of its own.
        if old is not None and start_time(old.pid) == old.started:
            kill_process_group(old.pid, signal.SIGKILL)
        if report_old:
            report_child_reaped(self._req.on_pid)
        self._restore_env_file()
        with self._lock:
            if started is None:
                logger.warning(
                    "tool harness: the CLI (pid %d) was gone before it was registered", pid,
                )
            else:
                self._root = (pid, started)
            self._root_changed.notify_all()

    def _restore_env_file(self) -> None:
        """The launcher unlinks the env file once read; a respawned CLI's
        launcher needs it again."""
        if self._env_file is None:
            return
        try:
            _write_new_file(self._env_file, self._env_text)
        except FileExistsError:
            pass
        except OSError as exc:
            logger.warning("tool harness: could not rewrite the env file: %s", exc)

    def release(self) -> None:
        for gate_path in self._gate_paths:
            self._open_gate(gate_path)

    def _open_gate(self, gate_path: Path) -> None:
        try:
            fd = os.open(gate_path, os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except OSError as exc:
            logger.warning("tool harness: could not open the call gate: %s", exc.strerror)
            return
        try:
            if not stat.S_ISFIFO(os.fstat(fd).st_mode):
                logger.warning("tool harness: the call gate is not a FIFO")
                return
            os.write(fd, b"\x01")
        except OSError as exc:
            logger.warning("tool harness: could not write the call gate: %s", exc.strerror)
        finally:
            os.close(fd)

    # -- close ---------------------------------------------------------------

    def close(self) -> None:
        """The server gone (killed after a wait), its reap reported once, the
        reader stopped and every file this harness wrote removed. Never raises."""
        try:
            self._close()
        except Exception:  # noqa: BLE001
            logger.warning("tool harness close did not finish cleanly", exc_info=True)

    def _close(self) -> None:
        self._closing.set()
        with self._lock:
            self._root_changed.notify_all()
            server = self._server
        if server is not None:
            if not server.exited.wait(_CLOSE_WAIT_SECONDS):
                if start_time(server.pid) == server.started:
                    kill_process_group(server.pid, signal.SIGKILL)
                server.exited.wait(_EXIT_GRACE_SECONDS)
            self._report_reaped(server)
        self._done.set()
        if self._reader is not None:
            self._reader.join(timeout=5)
        for fd in (self._read_fd, self._keep_fd):
            if fd is not None:
                with suppress(OSError):
                    os.close(fd)
        self._read_fd = self._keep_fd = None
        for path in self._files:
            with suppress(OSError):
                os.unlink(path)


@contextmanager
def tool_harness(
    req: BrainRequest,
    *,
    brain_label: str,
    on_failure: Callable[[], None],
) -> Iterator[HarnessHandle]:
    """One harness: a directory, the files the CLI's launcher and the tool
    server read, the pid reader and the MCP config.

    The handle comes back with ``failure`` set when any of that could not be
    made; the caller fails the attempt with ``stop_reason="error"`` rather than
    starting the CLI. ``on_failure`` is called on a harness thread when a pid
    report is refused or the tool server dies mid-attempt.
    """
    harness = _DirectHarness(req, brain_label=brain_label, on_failure=on_failure)
    try:
        handle = harness.start()
    except BaseException:
        harness.close()
        raise
    try:
        yield handle
    finally:
        harness.close()


def set_cli_root(handle: HarnessHandle, pid: int) -> None:
    """The CLI was (re)spawned with ``pid``: accept one tool server descending
    from it, and drop the previous CLI's."""
    if handle._harness is not None:
        handle._harness.set_root(pid)


def release_calls(handle: HarnessHandle) -> None:
    """The CLI's init check passed; the tool server, and the WebFetch server
    where there is one, may run calls."""
    if handle._harness is not None:
        handle._harness.release()
