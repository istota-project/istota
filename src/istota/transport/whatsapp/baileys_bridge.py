"""The daemon side of the Baileys sidecar: its socket, its lifetime, its work.

Baileys is a TypeScript library with no Python port, so the adapter is a Node
subprocess rather than a client. This module is everything that is not the
wire format: the listener, the process supervisor, the send round-trip, the
inbound receiver, and the session directory that holds the paired credential.

**The daemon listens and the sidecar dials.** `devbox_proxy` is arranged the
same way and for the same reason — the listener's lifetime is the daemon's, so
a sidecar restart reconnects to a socket that never went away, and the daemon
creates the inode and therefore owns its mode. The spec's test-strategy line
calls the fake sidecar "an in-process asyncio server"; it is a client here,
which is the direction that sentence's own citation (`devbox_proxy`) and the
design's "a local Unix socket the daemon owns, 0600" both describe.

**There is no HMAC and none is faked.** The trust boundary is the socket: it
is `AF_UNIX`, 0600, inside the daemon's own state directory, with no network
peer — the same trust model as the tool server's inherited socketpair, one
notch weaker because this one has a name. That is why the Baileys adapter's
`verify_signature` is `None` and why nothing on this path calls it.

**The session directory is a full-account credential.** Whoever holds
`{session_dir}` can send and read as the paired WhatsApp account, with no
second factor and no revocation short of unlinking the device. It is made 0700
and *asserted* 0700 on an `O_NOFOLLOW` descriptor every start — `mkdir`'s mode
applies only to a directory the call creates, and the second run is the case
that has to be right — and the same descriptor is what refuses one belonging to
another uid. The sidecar is spawned under `umask 0o077` so its own files land
0600 and with its stdio discarded so it cannot print into the daemon's log; the
directory is bound into no sandbox at any path; and neither its contents nor a
`qr` payload is ever logged, a QR being the pairing credential itself.

`sidecar_argv=()` is a first-class mode rather than a test affordance: the
Ansible shape runs the sidecar as its own systemd unit, so the bridge listens
and supervises nothing. Passing an argv is the `istota serve` shape, where the
subprocess is the daemon's the way the scheduler thread is.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import logging
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from ...config import Config
from . import baileys_protocol as proto, message_fingerprint
from ._types import WhatsAppSendOutcome, WhatsAppSendRequest

logger = logging.getLogger(__name__)

#: The socket the sidecar dials, under the daemon's own state directory. It is
#: a sibling of `istota.db` for the reason the session directory is: both are
#: runtime state of this deployment, and `db_path.parent` is the one directory
#: every shape already gives the daemon write access to.
SOCKET_NAME = "whatsapp-baileys.sock"
SESSION_DIR_NAME = "whatsapp-baileys-session"

#: `sun_path` is 104 bytes on macOS and 108 on Linux, and the failure past it
#: is an `OSError` naming neither the path nor the limit. Checked up front so
#: the message says what is wrong.
MAX_SOCKET_PATH_BYTES = 100

#: How long a send waits for its `send_result` before the outcome is ambiguous.
#: Generous rather than tight: a timeout here settles the row `unknown`, which
#: `.claude/rules/whatsapp.md` calls the one state an operator can never
#: resolve, so waiting is cheaper than guessing.
SEND_TIMEOUT_SECONDS = 45.0

#: How long `drain()` may hold the write lock. Its own bound rather than the
#: send's, because it is held across the lock and a peer that connects and
#: stops reading never lets it return — so this is what decides how long one
#: wedged sidecar can stall every other send, and it should be well under
#: `SEND_TIMEOUT_SECONDS`.
DRAIN_TIMEOUT = 10.0

#: Pending inbound events. Deliberately large and deliberately *dropping* past
#: it rather than blocking the reader: the worker's own work can include a
#: reply, which awaits a `send_result` only the reader can deliver, so a reader
#: blocked on a full queue is a deadlock. At this depth the sidecar has been
#: unanswerable for a very long time and the drop is loud.
INBOUND_QUEUE_MAX = 1024

#: A database failure is retried in place, in order, rather than dropped — the
#: spec's error-handling rule, and the analogue of the webhook answering 503 so
#: Meta redelivers. There is nobody to redeliver here, so the retry is ours.
INBOUND_ATTEMPTS = 4
INBOUND_RETRY_BASE_SECONDS = 0.5

#: Sidecar respawn backoff, doubling to the ceiling — and how long a child has
#: to run before its exit counts as a fresh fault rather than a continuing one,
#: which is what returns the backoff to the floor.
RESPAWN_BASE_SECONDS = 1.0
RESPAWN_MAX_SECONDS = 60.0
RESPAWN_RESET_SECONDS = 120.0

#: How long `stop()` gives a sidecar to exit after `shutdown` before SIGTERM,
#: and then before SIGKILL.
SHUTDOWN_GRACE_SECONDS = 5.0
TERMINATE_GRACE_SECONDS = 5.0

#: A `fatal` naming one of these is not retried by respawning. The session is
#: gone and only `istota whatsapp pair` brings it back, so a respawn loop would
#: burn a process every few seconds while changing nothing. An explicit
#: ``"permanent": true`` on the line says the same thing without this table
#: having to know the name.
_PERMANENT_FATALS = frozenset({"logged_out", "unpaired", "bad_session"})

#: What the sidecar's process is handed. An allowlist rather than
#: `os.environ`: the daemon's own environment carries every credential on the
#: deployment — the Claude token, the Nextcloud password, the forge tokens, the
#: master Fernet key — and a Node program that needs none of them should be
#: given none of them. `build_stripped_env` is the near neighbour and is not
#: reused: it is `os.environ` minus a pattern list, i.e. an allowlist's
#: opposite, and it stamps `PRECOMMIT_SCANS_REQUIRED`, which means nothing
#: here.
_CHILD_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "NODE_ENV")

#: How the sidecar learns where to dial and where its session lives. In the
#: environment rather than in argv so the argv stays the operator's — the
#: Ansible unit and the compose service each spell the command their own way —
#: and so neither value shows up in `ps` output.
ENV_SOCKET = "ISTOTA_BAILEYS_SOCKET"
ENV_SESSION_DIR = "ISTOTA_BAILEYS_SESSION_DIR"


# ---------------------------------------------------------------------------
# The one bridge this process holds
# ---------------------------------------------------------------------------

#: Set by whatever started a bridge, read by `providers/baileys.py`'s `send`
#: and by `doctor`. A module global rather than something threaded through,
#: for the reason the adapter seam forces: `make_provider_registry` builds a
#: fresh adapter per send and may do no I/O, so `build_adapter` cannot make a
#: bridge — it can only find the one already running. `signaling._STATS_SOURCE`
#: is the same arrangement for the same reason one surface over.
_ACTIVE_BRIDGE: "BaileysBridge | None" = None


def set_active_bridge(bridge: "BaileysBridge | None") -> None:
    """Publish the bridge this process's sends go through.

    Called by the owner that started it, after `start()` has returned — before
    that, a send resolved through here would be written to a socket nothing is
    listening on yet, and the adapter's honest answer while there is no bridge
    is a *definite* local failure rather than a lost message.
    """
    global _ACTIVE_BRIDGE
    _ACTIVE_BRIDGE = bridge


def clear_active_bridge() -> None:
    """Unpublish. The owner's shutdown path, and test teardown."""
    global _ACTIVE_BRIDGE
    _ACTIVE_BRIDGE = None


def active_bridge() -> "BaileysBridge | None":
    return _ACTIVE_BRIDGE


def read_status() -> dict | None:
    """The bridge's counters as a plain dict, or ``None`` in a process with no
    bridge.

    `doctor`'s reader, shaped like `signaling.read_stats` and for its reasons.
    Two of them are worth restating here. It answers `None` rather than a set
    of zeros in the web process, the CLI and behind `!check`, because on the
    deployment shape where those are separate processes the bridge lives in
    the daemon and reporting it down as well would page an operator about a
    process that was never meant to have one. And it never raises: a
    diagnostic that fails on its own instrument reports the wrong subsystem.
    """
    bridge = _ACTIVE_BRIDGE
    if bridge is None:
        return None
    try:
        return dataclasses.asdict(bridge.status)
    except Exception as exc:  # noqa: BLE001 — a diagnostic must not raise
        logger.debug("whatsapp.baileys.status_unreadable %s", type(exc).__name__)
        return None


# ---------------------------------------------------------------------------
# The session directory
# ---------------------------------------------------------------------------


def default_session_dir(config: Config) -> Path:
    """Where the paired session lives unless the operator named somewhere else."""
    configured = (config.whatsapp.baileys.session_dir or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(config.db_path).parent / SESSION_DIR_NAME


def default_socket_path(config: Config) -> Path:
    return Path(config.db_path).parent / SOCKET_NAME


#: The Node program's entry point inside its own directory.
SIDECAR_ENTRY = "index.js"

#: Where the shipped sidecar lives relative to this file, on a checkout. Four
#: parents up from `transport/whatsapp/baileys_bridge.py` is `src/`'s parent,
#: the repository root.
_SIDECAR_IN_TREE = Path(__file__).resolve().parents[4] / "docker" / "whatsapp-baileys"


def resolve_sidecar_argv(config: Config) -> tuple[str, ...]:
    """The command the **daemon** spawns for the sidecar, or ``()``.

    ``()`` is a first-class answer rather than a failure, and it is the
    default: a deployment running the sidecar as its own systemd unit or
    compose service wants the daemon to listen and supervise nothing
    (`sidecar_argv=()`), and that shape is strictly better there — systemd
    restarts a unit a permanent fatal stopped, which the in-process supervisor
    deliberately does not.

    **There is no in-tree fallback on this path, and that is the point rather
    than an omission.** One exists in `cli._pair_sidecar_argv`, where a
    developer's checkout is the case it serves. Here it would fire on exactly
    the canonical deployment: the Ansible shape installs from a checkout, so
    `docker/whatsapp-baileys/index.js` is present and `node` is usually on
    PATH, and the daemon would spawn a second sidecar beside the unit's —
    two Baileys clients on one auth state, which is the corruption
    `istota whatsapp pair` refuses one whole process to avoid.

    `shlex.split` rather than a shell, so nothing in an operator's value is
    interpreted; a value that will not split is a warning and `()` rather than
    a raise, since the caller is a boot path.

    Never raises.
    """
    import shlex  # noqa: PLC0415

    configured = (config.whatsapp.baileys.sidecar_command or "").strip()
    if not configured:
        return ()
    try:
        argv = shlex.split(configured)
    except ValueError:
        logger.warning(
            "whatsapp.baileys.sidecar_command_unparseable: "
            "[whatsapp.baileys] sidecar_command is not a valid command "
            "line; no sidecar will be spawned",
        )
        return ()
    return tuple(argv)


def in_tree_sidecar_argv() -> tuple[str, ...]:
    """The shipped Node program in this checkout, or ``()``.

    `istota whatsapp pair`'s fallback, and **only** its fallback. Pairing
    needs a sidecar of its own — it is what makes the command work on a
    developer machine and on a standalone install made from a clone with no
    deployment wiring, and it is what gets a logged-out deployment out of the
    one-way door a permanent fatal closes. The daemon must not take it; see
    `resolve_sidecar_argv`.

    `node` is resolved on `PATH` at *this* moment, so an install without it
    answers `()` and the caller says so once rather than the supervisor
    reporting a spawn failure per respawn.
    """
    import shutil  # noqa: PLC0415

    entry = _SIDECAR_IN_TREE / SIDECAR_ENTRY
    if not entry.is_file() or shutil.which("node") is None:
        return ()
    return (str(shutil.which("node")), str(entry))


def ensure_session_dir(path: Path) -> Path:
    """Create or tighten the session directory, and return it.

    `setup_wizard`'s rule for `istota.env`, applied to a directory: the mode
    passed to `mkdir` applies **only to a directory the call creates**, and
    re-running against one that already exists is the whole case this has to
    get right — so the mode is asserted on a descriptor afterwards, every time.
    `os.open(..., O_DIRECTORY | O_NOFOLLOW)` is what makes that descriptor the
    directory itself rather than whatever a symlink planted at the name points
    at; the mode goes on the fd, so nothing can be swapped between the check
    and the change.

    Raises rather than degrading. A session directory that cannot be made
    private is a full WhatsApp account readable by every account on the host,
    and carrying on would pair into it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir(mode=0o700, exist_ok=True)
    except FileExistsError:
        # `exist_ok=True` still raises this for a non-directory at the name.
        raise NotADirectoryError(
            f"whatsapp baileys session path is not a directory: {path}"
        ) from None
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):  # pragma: no cover - O_DIRECTORY covers it
            raise NotADirectoryError(
                f"whatsapp baileys session path is not a directory: {path}"
            )
        # **Ours, not merely private.** A directory already at 0700 skips the
        # `fchmod`, and with it the `EPERM` that would otherwise be the only
        # thing to notice another uid owns it — so a 0700 directory planted at
        # the configured path is adopted in silence and a full WhatsApp account
        # is paired into it. `session_dir` is operator-settable, so the parent
        # is not always the daemon's own state directory.
        # `executor._ensure_control_level` makes the same check after the same
        # `O_NOFOLLOW | O_DIRECTORY` open, and its docstring names this hazard:
        # a type check says the path is a directory, not that it is ours.
        if info.st_uid != os.geteuid():
            raise PermissionError(
                f"whatsapp baileys session directory belongs to uid "
                f"{info.st_uid}, not to this process: {path}"
            )
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
    return path


def harden_session_files(path: Path) -> tuple[int, int]:
    """Narrow any session file wider than 0600. Returns `(narrowed, failed)`.

    Defence in depth behind the child's `umask`, which is the real mechanism:
    the sidecar writes these files, so the daemon cannot create them at the
    right mode and can only correct one afterwards. A non-zero `narrowed`
    means the sidecar is running under a umask that is not this module's; a
    non-zero `failed` is the one `doctor` should be loud about.

    **Two counts rather than one**, because they mean opposite things and an
    earlier version conflated them: it incremented before the `fchmod` and
    caught the `lstat` in the same handler, so a file that merely vanished
    between the listing and the stat was reported as a session possibly
    readable by other accounts, while one that could *not* be narrowed was
    counted as though it had been. The number `doctor` reads has to be the one
    that happened.

    Never raises and never follows a symlink — it runs on the start path,
    where a failure to tidy must not stop a deployment from receiving
    messages. A failure is logged by name, because the name is a path and not
    a secret; the contents never are.
    """
    narrowed = 0
    failed = 0
    try:
        entries = sorted(path.iterdir())
    except OSError:
        return 0, 0
    for entry in entries:
        try:
            info = entry.lstat()
        except OSError:
            # Gone between the listing and the stat, or unreadable. Neither is
            # a widened session file.
            continue
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) == 0o600:
            continue
        try:
            fd = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
        except OSError:
            failed += 1
            logger.warning(
                "whatsapp.baileys.session_mode_unfixed path=%s: the paired "
                "session may be readable by other accounts on this host",
                entry,
            )
            continue
        narrowed += 1
    return narrowed, failed


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@dataclass
class _WriteMark:
    """Whether one send's line reached `writer.write`.

    A mutable flag rather than a return value, because the question is asked
    from the `except` clause of the *caller* — which sees an exception and has
    no other way to know which side of the socket it fell on, and whose answer
    is the ledger's `failed`-against-`unknown` decision.
    """

    written: bool = False


@dataclass
class BridgeStatus:
    """What the bridge will tell `doctor` and `istota whatsapp pair`.

    Counters rather than a log scrape, because both readers ask after the
    fact. `malformed_lines` is the one the spec names — "a run of them trips a
    doctor warning" — and `dropped_events` and `failed_events` are the two ways
    an inbound message can be lost, kept apart because they mean different
    things: one is a wedged worker, the other a database that would not take
    the write after every retry.
    """

    listening: bool = False
    connected: bool = False
    ready: bool = False
    fatal_reason: str | None = None
    fatal_is_permanent: bool = False
    restarts: int = 0
    malformed_lines: int = 0
    dropped_events: int = 0
    failed_events: int = 0
    inbound_applied: int = 0
    queue_depth: int = 0
    session_files_hardened: int = 0
    session_files_unfixed: int = 0
    rejected_connections: int = 0
    socket_path: str = ""
    session_dir: str = ""
    protocol_version: int | None = None
    pending_sends: int = 0


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------


class BaileysBridge:
    """One deployment's Baileys sidecar, from the daemon's side.

    One instance per process. `send` is the adapter's send — Stage 6's
    `providers/baileys.py` hands this bound method straight to the registry,
    which is why it is a real `async def` method rather than a closure over a
    sync function: `_contract_fault` refuses a `send` that is not a coroutine
    function, and it refuses it for the reason that matters here — a
    synchronous one raises inside the claim-to-settle region and settles the
    row `unknown`.
    """

    def __init__(
        self,
        config: Config,
        *,
        sidecar_argv: tuple[str, ...] = (),
        socket_path: Path | None = None,
        session_dir: Path | None = None,
        send_timeout: float = SEND_TIMEOUT_SECONDS,
        on_qr=None,
        on_fatal=None,
    ) -> None:
        self._config = config
        self._sidecar_argv = tuple(sidecar_argv)
        self._socket_path = Path(socket_path or default_socket_path(config))
        self._session_dir = Path(session_dir or default_session_dir(config))
        self._send_timeout = send_timeout
        self._on_qr = on_qr
        # Called once per *transition into* a permanent fatal, with the
        # reason. The bridge itself never writes a notification row: this
        # module's contracts are that it never raises and never logs a payload,
        # and a notification write opens a database connection and delivers to
        # a surface. The owner that started the bridge does that work.
        self._on_fatal = on_fatal

        self._status = BridgeStatus(
            socket_path=str(self._socket_path), session_dir=str(self._session_dir),
        )
        self._server: asyncio.AbstractServer | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=INBOUND_QUEUE_MAX)
        self._worker: asyncio.Task | None = None
        self._supervisor: asyncio.Task | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._qr_tasks: set[asyncio.Task] = set()
        self._stopping = False
        self._write_lock = asyncio.Lock()
        # Set by a permanent `fatal`, and waited on beside the child's exit.
        # Checking it only at the top of the respawn loop would leave a
        # logged-out sidecar running until it exited on its own, which for a
        # long-lived process is never.
        self._permanent_fatal = asyncio.Event()

    # -- lifecycle ----------------------------------------------------------

    @property
    def status(self) -> BridgeStatus:
        self._status.queue_depth = self._queue.qsize()
        self._status.pending_sends = len(self._pending)
        return self._status

    async def start(self) -> None:
        """Make the session directory, open the socket, start the sidecar.

        In that order, and the order is the contract: a sidecar that dialled
        before the listener existed would fail its first connect and burn a
        respawn, and one that started before the session directory was private
        would pair into a world-readable one.
        """
        self._stopping = False
        ensure_session_dir(self._session_dir)
        (
            self._status.session_files_hardened,
            self._status.session_files_unfixed,
        ) = harden_session_files(self._session_dir)
        await self._listen()
        self._worker = asyncio.create_task(self._drain_inbound())
        if self._sidecar_argv:
            self._supervisor = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        """Ask the sidecar to stop, then insist, then close the socket.

        Never raises. It runs from a shutdown path, where an exception would
        leave the socket file behind and the next start refusing an address
        already in use.
        """
        self._stopping = True
        listened = self._status.listening
        await self._request_shutdown()
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._supervisor
            self._supervisor = None
        await self._reap_process()
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._worker
            self._worker = None
        self._close_link(proto.REASON_LINK_LOST)
        if self._server is not None:
            self._server.close()
            # Bounded, because `wait_closed` waits on every handler and a
            # handler is only as cooperative as its peer. `_close_link` above
            # has already dropped the one connection there can be, so this
            # timeout is the backstop rather than the mechanism.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self._server.wait_closed(), timeout=SHUTDOWN_GRACE_SECONDS,
                )
            self._server = None
        self._status.listening = False
        # Only an inode this bridge created, and only if it is still a socket.
        # `_unlink_stale` refuses to delete a non-socket at the name on the
        # grounds that it is somebody else's; `stop()` is reachable after a
        # failed `start()` — the `try/finally` shape every caller uses — so an
        # unconditional unlink here deletes exactly the file that refusal just
        # protected.
        if listened:
            with contextlib.suppress(OSError):
                if stat.S_ISSOCK(self._socket_path.lstat().st_mode):
                    self._socket_path.unlink()

    async def _listen(self) -> None:
        path = self._socket_path
        if len(os.fsencode(path)) > MAX_SOCKET_PATH_BYTES:
            raise ValueError(
                f"whatsapp baileys socket path is too long for AF_UNIX "
                f"({len(os.fsencode(path))} bytes, limit {MAX_SOCKET_PATH_BYTES}): "
                f"{path}"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        self._unlink_stale(path)
        # The inode is created 0600 rather than narrowed to it: between a
        # default-permission create and a `chmod` there is a window in which
        # another account on the host can connect, and a connection here is
        # the right to send as this WhatsApp account. The explicit `chmod` is
        # for a platform whose umask handling differs, which is what
        # `devbox_proxy` says at the same line.
        #
        # The accepted cost, stated rather than left to be rediscovered: the
        # umask is process-global and is held across an await, so anything else
        # on this loop — and any `to_thread` worker — that creates a file
        # during that one iteration gets 0600, and a *directory* 0600, which is
        # not traversable. Under `istota serve` the web app shares the process.
        # Binding the socket by hand and passing `sock=` would close the window
        # entirely; it is not done because the mode would then be set before
        # `start_unix_server` is reached, which is the only point a test can
        # observe, and a control that cannot fail is worse than this window.
        # Precedented by `devbox_proxy.serve`, which takes the same trade.
        previous_umask = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(
                self._on_connect, path=str(path), limit=proto.MAX_LINE_BYTES + 4096,
            )
        finally:
            os.umask(previous_umask)
        os.chmod(path, 0o600)
        self._status.listening = True
        logger.info("whatsapp.baileys.listening socket=%s", path)

    @staticmethod
    def _unlink_stale(path: Path) -> None:
        """Remove a socket left behind by a process that did not clean up.

        Only a socket. A regular file or a directory at the name is somebody
        else's, and unlinking it would be this module deleting data on the
        strength of a name collision.
        """
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        except OSError:
            return
        if not stat.S_ISSOCK(info.st_mode):
            raise FileExistsError(
                f"whatsapp baileys socket path exists and is not a socket: {path}"
            )
        with contextlib.suppress(OSError):
            path.unlink()

    # -- the sidecar process ------------------------------------------------

    def _child_env(self) -> dict[str, str]:
        env = {
            name: os.environ[name]
            for name in _CHILD_ENV_PASSTHROUGH
            if name in os.environ
        }
        env[ENV_SOCKET] = str(self._socket_path)
        env[ENV_SESSION_DIR] = str(self._session_dir)
        return env

    async def _supervise(self) -> None:
        """Keep the sidecar running, backing off, until told to stop.

        A permanent `fatal` ends the loop rather than slowing it down. The
        session is gone and only a re-pair brings it back, so respawning would
        cost a process every few seconds and change nothing; the status carries
        the reason for `doctor` to report and for the operator to act on.

        **On this shape that is a one-way door, and Stage 6 owes the way out.**
        With an argv, ending the loop means no child, so nothing can send the
        `ready` that `_dispatch` clears the latch on — the clearing arm is
        unreachable here, and there is no `resume()`. The remedy the log line
        names has to supply its own sidecar: `istota whatsapp pair` spawning
        one of its own satisfies this, and a `resume()` on the bridge is the
        alternative. Neither is written yet, and a method with no caller is a
        seam with no user, so the requirement is recorded rather than guessed
        at. On the `sidecar_argv=()` shape there is no gap: systemd restarts
        the unit, it reconnects, and its `ready` clears the latch.
        """
        delay = RESPAWN_BASE_SECONDS
        while not self._stopping:
            if self._status.fatal_is_permanent:
                logger.error(
                    "whatsapp.baileys.not_respawning reason=%s: re-pair with "
                    "`istota whatsapp pair`",
                    self._status.fatal_reason,
                )
                return
            started = asyncio.get_running_loop().time()
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *self._sidecar_argv,
                    env=self._child_env(),
                    # The sidecar creates the session files, so its umask is
                    # the only thing that decides their mode at birth.
                    # `harden_session_files` corrects a stray one afterwards
                    # and is the backstop, not the mechanism.
                    umask=0o077,
                    cwd=str(self._session_dir),
                    # **Discarded, not inherited.** Inherited, the Node child
                    # writes into the daemon's own stdout and stderr, which is
                    # the journal and the rotating log the admin Logs pane
                    # reads back — and Baileys' logger is chatty about JIDs and
                    # message bodies. This module's "never logged" rule binds
                    # its own `logger` calls and binds nothing the child
                    # prints, so the child is given nowhere to print. `PIPE`
                    # with no reader is worse than either: the child blocks
                    # once the pipe buffer fills. The sidecar owning its own
                    # log destination — inside the 0700 session directory — is
                    # Stage 6's, with the Node program.
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except Exception:
                logger.warning(
                    "whatsapp.baileys.spawn_failed argv=%s", self._sidecar_argv[0],
                    exc_info=True,
                )
            else:
                code = await self._await_child_or_fatal()
                if self._stopping:
                    return
                if code is None:
                    # A permanent fatal arrived while the child was alive. It
                    # is stopped rather than left running: the session is gone,
                    # so it would sit there re-reporting the same fatal, and a
                    # long-lived process never exits on its own for the loop
                    # above to notice.
                    await self._reap_process()
                    self._process = None
                    logger.error(
                        "whatsapp.baileys.not_respawning reason=%s: re-pair "
                        "with `istota whatsapp pair`",
                        self._status.fatal_reason,
                    )
                    return
                logger.warning("whatsapp.baileys.sidecar_exited code=%s", code)
                self._status.restarts += 1
                # **A child that ran for a while resets the backoff.** Without
                # this the doubling is monotonic for the life of the process,
                # so a sidecar crashing once a day reaches the ceiling after
                # six crashes and every later one costs a full minute of
                # unreachability rather than a second — the opposite of what a
                # backoff is for. The tight crash-loop case is unaffected,
                # since a child that dies immediately never passes the bar.
                if asyncio.get_running_loop().time() - started >= RESPAWN_RESET_SECONDS:
                    delay = RESPAWN_BASE_SECONDS
            await asyncio.sleep(delay)
            delay = min(delay * 2, RESPAWN_MAX_SECONDS)

    async def _await_child_or_fatal(self) -> int | None:
        """The child's exit status, or `None` for a permanent fatal first."""
        process = self._process
        assert process is not None
        exited = asyncio.ensure_future(process.wait())
        fatal = asyncio.ensure_future(self._permanent_fatal.wait())
        try:
            await asyncio.wait(
                {exited, fatal}, return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (exited, fatal):
                if not task.done():
                    task.cancel()
        if exited.done() and not exited.cancelled():
            self._process = None
            return exited.result()
        return None

    async def _request_shutdown(self) -> None:
        writer = self._writer
        if writer is None:
            return
        with contextlib.suppress(Exception):
            writer.write(proto.encode(proto.MSG_SHUTDOWN))
            await asyncio.wait_for(writer.drain(), timeout=1.0)

    async def _reap_process(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=SHUTDOWN_GRACE_SECONDS)
            return
        except Exception:
            pass
        with contextlib.suppress(Exception):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=TERMINATE_GRACE_SECONDS)
            return
        except Exception:
            pass
        with contextlib.suppress(Exception):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()

    # -- the link -----------------------------------------------------------

    async def _on_connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """One sidecar at a time.

        A second connection is closed rather than adopted. Two sidecars on one
        paired session is two readers of one WhatsApp account, and whichever
        one a `send` happened to reach would decide where the reply went —
        which is unanswerable from here, so it is refused where it is
        detectable.
        """
        if self._writer is not None and not self._writer.is_closing():
            self._status.rejected_connections += 1
            logger.warning(
                "whatsapp.baileys.connection_refused reason=already_connected",
            )
            with contextlib.suppress(Exception):
                writer.close()
            return
        try:
            # **The writer is adopted only after version negotiation passes.**
            # `_send`'s liveness gate is "is there a writer", so adopting at
            # accept means a `send` can be written to a peer whose protocol
            # version is unknown or refused — the write direction of exactly
            # what `_accept_hello` guards in the read direction. A peer that
            # connects and never speaks holds nothing open but itself.
            if not await self._negotiate(reader):
                return
            self._writer = writer
            self._status.connected = True
            await self._read_loop(reader)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("whatsapp.baileys.link_error", exc_info=True)
        finally:
            if self._writer is writer:
                self._close_link(proto.REASON_LINK_LOST)
            with contextlib.suppress(Exception):
                writer.close()

    async def _negotiate(self, reader: asyncio.StreamReader) -> bool:
        """Read lines until a `hello` settles the version, or give up.

        A malformed line before the `hello` is counted and skipped, matching
        the read loop; anything else with a type is the peer talking before it
        has introduced itself, and the connection goes.
        """
        while True:
            try:
                line = await reader.readline()
            except (asyncio.LimitOverrunError, ValueError):
                self._status.malformed_lines += 1
                logger.warning("whatsapp.baileys.line_too_long")
                return False
            if not line:
                return False
            try:
                payload = proto.decode(line)
            except proto.BaileysProtocolError as exc:
                self._status.malformed_lines += 1
                logger.warning("whatsapp.baileys.malformed_line reason=%s", exc)
                continue
            if payload["type"] != proto.MSG_HELLO:
                logger.warning(
                    "whatsapp.baileys.hello_expected got=%s", payload["type"],
                )
                return False
            return self._accept_hello(payload)

    def _close_link(self, reason: str) -> None:
        """Drop the connection and settle everything that was riding on it.

        **It closes the transport rather than only forgetting it**, which is
        what makes `stop()` bounded: `Server.wait_closed` waits for every
        in-flight handler, and this handler is blocked on `readline` until its
        peer goes away. A sidecar that ignores `shutdown` — or one wedged
        before it reads the line — would otherwise hold the daemon's shutdown
        open for ever.
        """
        writer, self._writer = self._writer, None
        self._status.connected = False
        self._status.ready = False
        self._status.protocol_version = None
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
        self._fail_pending(reason)

    def _fail_pending(self, reason: str) -> None:
        """Resolve every in-flight send **ambiguously**.

        Never `definite`. Each of these had its line written to the socket, so
        the bytes may be in the kernel buffer or on WhatsApp's servers; the one
        thing that is known is that no answer came back. `.claude/rules/
        whatsapp.md`'s rule holds: `unknown` is a row an operator has to read,
        and reporting `failed` for a message that arrived is worse.
        """
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_result(proto.local_failure(reason, definite=False))

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        """Read lines until the sidecar goes away.

        **It dispatches and never works.** Handling an inbound message can end
        in a reply, and a reply awaits a `send_result` that only this loop can
        deliver — so anything slower than a queue `put` happens on the worker.
        """
        while True:
            try:
                line = await reader.readline()
            except (asyncio.LimitOverrunError, ValueError):
                # A line past the StreamReader limit. The stream is now at an
                # unknown offset, so the connection is the only safe thing to
                # discard: reading on would parse the tail of one line as the
                # head of the next.
                self._status.malformed_lines += 1
                logger.warning("whatsapp.baileys.line_too_long")
                return
            if not line:
                return
            try:
                payload = proto.decode(line)
            except proto.BaileysProtocolError as exc:
                self._status.malformed_lines += 1
                logger.warning("whatsapp.baileys.malformed_line reason=%s", exc)
                continue
            self._dispatch(payload["type"], payload)

    def _accept_hello(self, payload: dict) -> bool:
        """Whether this sidecar speaks a version this daemon understands.

        A refusal drops the connection rather than carrying on, because the
        alternative is reading an older shape's fields out of a newer frame
        and resolving a principal from whatever came out.

        **Nothing here bounds the reconnect that follows**, and an earlier
        draft of this docstring claimed the supervisor's backoff did. It does
        not: a refused `hello` kills the connection and not the child, so a
        mismatched sidecar reconnects as fast as its own client loop allows,
        and on the `sidecar_argv=()` shape there is no supervisor at all. The
        bound is the sidecar's, which is the right place for it — but it means
        a version mismatch can be a log flood, so the line names both versions
        and an operator has something to act on.
        """
        try:
            version = proto.hello_version(payload)
        except proto.BaileysProtocolError as exc:
            self._status.malformed_lines += 1
            logger.warning("whatsapp.baileys.hello_rejected reason=%s", exc)
            return False
        if version != proto.PROTOCOL_VERSION:
            logger.error(
                "whatsapp.baileys.hello_rejected sidecar=%s daemon=%s: the "
                "sidecar and the daemon are different versions",
                version, proto.PROTOCOL_VERSION,
            )
            return False
        self._status.protocol_version = version
        return True

    def _dispatch(self, message_type: str, payload: dict) -> None:
        if message_type == proto.MSG_SEND_RESULT:
            self._resolve_send(payload)
        elif message_type in (proto.MSG_INBOUND, proto.MSG_RECEIPT):
            self._enqueue(message_type, payload)
        elif message_type == proto.MSG_READY:
            self._status.ready = True
            self._status.fatal_reason = None
            self._status.fatal_is_permanent = False
            self._permanent_fatal.clear()
            logger.info("whatsapp.baileys.ready")
        elif message_type == proto.MSG_QR:
            self._handle_qr(payload)
        elif message_type == proto.MSG_FATAL:
            self._handle_fatal(payload)
        else:
            # Including a second `hello`. Counted rather than ignored: an
            # unknown type from a sidecar that passed version negotiation is
            # the two halves disagreeing about the protocol.
            self._status.malformed_lines += 1
            logger.warning(
                "whatsapp.baileys.unexpected_message type=%s", message_type,
            )

    def _handle_qr(self, payload: dict) -> None:
        """Hand a pairing QR to whoever asked for one, and to nobody else.

        **The payload is never logged, at any level.** It is the pairing
        credential for the whole WhatsApp account: anything that scans it is
        linked as a device. The callback is `istota whatsapp pair`'s at Stage
        6; with none set the daemon is not pairing and the frame is noted
        without its contents.
        """
        value = payload.get("qr")
        if not isinstance(value, str) or not value:
            self._status.malformed_lines += 1
            return
        if self._on_qr is None:
            logger.info(
                "whatsapp.baileys.qr_offered: the session is unpaired. Run "
                "`istota whatsapp pair` to link it.",
            )
            return
        self._call_back(self._on_qr, value, label="qr")

    def _call_back(self, callback, *args, label: str) -> None:
        """Run one owner-supplied callback off the read loop. Never raises.

        Shared by the `qr` and `fatal` handlers, which need the same three
        properties and would otherwise each carry them. An `async def`
        callback returns a coroutine object, which is truthy, raises nothing
        and — dropped — does its work never, with only a `RuntimeWarning` that
        `logging` never surfaces; both owners here are plausible async ones
        (`istota whatsapp pair`, and a notification write on a worker thread).
        So an awaitable result is scheduled as **its own task** rather than
        awaited inline, because the read loop must not block on somebody else's
        terminal or somebody else's database, and it is held in a set so the
        loop does not garbage-collect it mid-flight.

        No `exc_info`, and that is the constraint rather than the tidiness: a
        traceback prints frames, and a frame here holds a pairing credential or
        a fatal payload.
        """
        try:
            result = callback(*args)
        except Exception:
            logger.warning("whatsapp.baileys.callback_failed which=%s", label)
            return
        if inspect.isawaitable(result):
            task = asyncio.ensure_future(result)
            self._qr_tasks.add(task)
            task.add_done_callback(self._qr_tasks.discard)

    def _handle_fatal(self, payload: dict) -> None:
        reason = payload.get("reason")
        reason = reason if isinstance(reason, str) and reason else "unknown"
        permanent = payload.get("permanent") is True or reason in _PERMANENT_FATALS
        already = self._status.fatal_is_permanent
        self._status.fatal_reason = reason
        self._status.fatal_is_permanent = permanent
        self._status.ready = False
        if permanent:
            self._permanent_fatal.set()
            if self._on_fatal is not None and not already:
                # **Only the first of a run.** A sidecar reporting `logged_out`
                # and then reconnecting to report it again — which it does,
                # since the session on disk is still the dead one — would
                # otherwise raise and push one alert per attempt. `ready`
                # clears the latch, so a re-pair re-arms it, which is the
                # transition an operator wants to hear about twice.
                self._call_back(self._on_fatal, reason, label="fatal")
        # Branched, because only the permanent arm refuses anything: `_send`'s
        # gate is `fatal_is_permanent`, so a transient fatal clears `ready` and
        # sends carry on. Telling an operator their sends are blocked when they
        # are not is how a real outage gets read as the usual noise.
        logger.error(
            "whatsapp.baileys.fatal reason=%s permanent=%s: %s",
            reason, permanent,
            "WhatsApp sends are refused until the session is re-paired"
            if permanent else
            "the session reported a fault; sends are still attempted",
        )

    def _resolve_send(self, payload: dict) -> None:
        try:
            request_id = proto.result_request_id(payload)
        except proto.BaileysProtocolError as exc:
            self._status.malformed_lines += 1
            logger.warning("whatsapp.baileys.send_result_unkeyed reason=%s", exc)
            return
        future = self._pending.pop(request_id, None)
        if future is None:
            # The send already timed out or the link dropped under it, and its
            # row has been settled. Dropped rather than applied: the ledger is
            # single-writer per logical key and re-settling a settled row is
            # what `_TERMINAL_FOR_STATUS` exists to refuse.
            logger.info("whatsapp.baileys.send_result_late request=%s", request_id)
            return
        try:
            outcome = proto.send_outcome(payload)
        except proto.BaileysProtocolError as exc:
            self._status.malformed_lines += 1
            logger.warning("whatsapp.baileys.send_result_malformed reason=%s", exc)
            # An answer arrived and could not be read, which is exactly the
            # ambiguous case: the sidecar believes it did something.
            outcome = proto.local_failure(proto.REASON_LINK_LOST, definite=False)
        if not future.done():
            future.set_result(outcome)

    def _enqueue(self, message_type: str, payload: dict) -> None:
        try:
            self._queue.put_nowait((message_type, payload))
        except asyncio.QueueFull:
            self._status.dropped_events += 1
            logger.error(
                "whatsapp.baileys.inbound_dropped type=%s depth=%s: the receiver "
                "is not keeping up and this message is lost",
                message_type, self._queue.qsize(),
            )

    # -- inbound ------------------------------------------------------------

    async def _drain_inbound(self) -> None:
        """One worker, in order, for the life of the bridge.

        Serial rather than a pool, because order within a conversation is
        meaning: `STOP` followed by a request, or an answer followed by the
        next question, are not the same exchange in the other order. The reader
        is what stays free, so a send the worker is waiting on still lands.
        """
        while True:
            message_type, payload = await self._queue.get()
            try:
                await self._handle_event(message_type, payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("whatsapp.baileys.inbound_failed", exc_info=True)
            finally:
                self._queue.task_done()

    async def _handle_event(self, message_type: str, payload: dict) -> None:
        try:
            if message_type == proto.MSG_INBOUND:
                event = proto.inbound_event(payload)
            else:
                event = proto.delivery_event(payload)
        except proto.BaileysProtocolError as exc:
            # Dropped, per the spec: a line this side cannot read is not a
            # message it can attribute, and attributing one to a guessed sender
            # is a principal takeover rather than a lost message.
            self._status.malformed_lines += 1
            logger.warning(
                "whatsapp.baileys.event_unreadable type=%s reason=%s",
                message_type, exc,
            )
            return
        if event is None:
            # A receipt status this surface does not model.
            logger.debug("whatsapp.baileys.receipt_ignored")
            return
        await self._apply_with_retry(event)

    async def _apply_with_retry(self, event) -> None:
        """Apply one event, retrying a database failure in place.

        The webhook answers a failed transaction with 503 and Meta redelivers;
        nothing redelivers here, so the retry is this side's. In place and in
        order, because the worker is the ordering guarantee and moving a failed
        event to the back of the queue would reorder a conversation. A run of
        attempts that all fail is a loud terminal drop with a count, which is
        the one outcome `doctor` can act on.
        """
        from .webhook import deliver_event_responses  # noqa: PLC0415

        delay = INBOUND_RETRY_BASE_SECONDS
        for attempt in range(1, INBOUND_ATTEMPTS + 1):
            try:
                results = await asyncio.to_thread(self._apply_batch_to_db, event)
            except asyncio.CancelledError:
                raise
            except Exception:
                if attempt == INBOUND_ATTEMPTS:
                    self._status.failed_events += 1
                    logger.error(
                        "whatsapp.baileys.inbound_lost attempts=%s message=%s: the "
                        "message was never applied",
                        attempt, message_fingerprint(getattr(event, "message_id", "")),
                        exc_info=True,
                    )
                    return
                logger.warning(
                    "whatsapp.baileys.inbound_retry attempt=%s", attempt,
                    exc_info=True,
                )
                await asyncio.sleep(delay)
                delay *= 2
                continue
            self._status.inbound_applied += 1
            try:
                await deliver_event_responses(self._config, results)
            except Exception:
                # Its own contract is that it does not raise. Guarded because
                # the transaction has already committed, so a raise here would
                # retry an event the ledger has already claimed.
                logger.warning("whatsapp.baileys.responses_failed", exc_info=True)
            return

    def _apply_batch_to_db(self, event):
        """One event, one transaction, one thread.

        The whole `with db.get_db(...)` block is inside a single
        `asyncio.to_thread` callable, deliberately: a SQLite connection must
        not cross threads, and `handle_whatsapp_batch` takes `BEGIN IMMEDIATE`
        and commits on the way out of that block. Splitting it across two
        `to_thread` calls would hand an open transaction to another thread.

        **`provider` is named and is not `config.whatsapp.provider`.** The
        parameter is provenance — which adapter produced these events — and
        `_handle_inbound` compares it against the active provider to refuse an
        inactive adapter's inbound message. Reading the config here would make
        that comparison tautological and the gate inert.
        """
        from ... import db  # noqa: PLC0415
        from .webhook import handle_whatsapp_batch  # noqa: PLC0415

        with db.get_db(self._config.db_path) as conn:
            return handle_whatsapp_batch(
                conn, self._config, [event],
                provider=db.WHATSAPP_BAILEYS_PROVIDER,
            )

    # -- outbound -----------------------------------------------------------

    async def send(self, request: WhatsAppSendRequest) -> WhatsAppSendOutcome:
        """Write one `send` line and wait for its answer. Never raises.

        The adapter contract, and the ledger rests on both halves of it: a
        raise inside the claim-to-settle region settles the row `unknown`, and
        `definite` is the single bit that decides `failed` against `unknown`.

        **The definite line is `writer.write`, exactly.** Everything above it —
        no sidecar connected, a session in a fatal state, a request that would
        not encode — is provably a message that never entered the socket, and
        settles `failed`. From `write` onwards the bytes may be in the kernel
        buffer whatever `drain` then says, so every outcome is ambiguous and
        settles `unknown`.

        `CancelledError` is deliberately not caught: `deliver_whatsapp`'s
        `BaseException` backstop settles the row and re-raises, and swallowing
        a cancellation here would hide a shutdown from the path designed to
        handle it.

        **The catch-all reads the mark rather than assuming a side of the
        line.** Every exception that can reach it today is provably pre-write —
        the post-write ones are all handled inside `_send` — so a fixed
        `definite=False` here spent `unknown` on a message that never left, and
        the test that drove it pinned the wrong answer rather than catching it.
        A fixed `definite=True` would be the same mistake waiting for the next
        refactor to move a post-write failure onto this path, so the mark is
        what decides, and it is set at the one statement that matters.
        """
        mark = _WriteMark()
        try:
            return await self._send(request, mark)
        except asyncio.CancelledError:
            raise
        except Exception:
            # No `exc_info`: the frames hold the request, and the request holds
            # the destination and the message body.
            logger.warning("whatsapp.baileys.send_error written=%s", mark.written)
            if mark.written:
                return proto.local_failure(proto.REASON_LINK_LOST, definite=False)
            return proto.local_failure(proto.REASON_NOT_WRITTEN, definite=True)

    async def _send(
        self, request: WhatsAppSendRequest, mark: "_WriteMark"
    ) -> WhatsAppSendOutcome:
        if self._status.fatal_is_permanent:
            return proto.local_failure(proto.REASON_SESSION_FATAL, definite=True)
        request_id = secrets.token_hex(8)
        try:
            line = proto.encode(
                proto.MSG_SEND, **proto.send_payload(request_id, request),
            )
        except proto.BaileysProtocolError:
            logger.warning("whatsapp.baileys.send_unencodable")
            return proto.local_failure(proto.REASON_ENCODE_FAILED, definite=True)

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        try:
            # **Both waits are bounded, and neither bound is the other's.**
            # The lock covers the registration and the write together — without
            # it a `send_result` for this id could be read and dropped as late
            # before the future is in the map, since the reader runs on this
            # same loop and `drain()` is a suspension point. But a sidecar that
            # connects and stops reading never lets `drain()` return: the
            # transport's buffer stays above its high-water mark, one line near
            # `MAX_LINE_BYTES` is enough to cross it, and `deliver_whatsapp`
            # awaits `adapter.send` with no timeout of its own. Unbounded, the
            # first such send holds the lock for ever and every later one
            # queues behind it, leaving rows claimed and unsettled — which also
            # holds the parked-status gate open for the whole deployment.
            await asyncio.wait_for(
                self._write_lock.acquire(), timeout=self._send_timeout,
            )
            try:
                writer = self._writer
                if writer is None or writer.is_closing():
                    return proto.local_failure(
                        proto.REASON_NO_SIDECAR, definite=True,
                    )
                self._pending[request_id] = future
                try:
                    writer.write(line)
                except Exception:
                    # `write` raised, so nothing was buffered and nothing left.
                    self._pending.pop(request_id, None)
                    logger.warning("whatsapp.baileys.send_not_written")
                    return proto.local_failure(
                        proto.REASON_NO_SIDECAR, definite=True,
                    )
                mark.written = True
                try:
                    await asyncio.wait_for(writer.drain(), timeout=DRAIN_TIMEOUT)
                except Exception:
                    # Past `write`. The bytes may already be in the socket, so
                    # the outcome is ambiguous however this ends — including a
                    # drain that timed out, which is why this is swallowed and
                    # the answer is left to the wait below.
                    logger.warning("whatsapp.baileys.send_drain_failed")
            finally:
                self._write_lock.release()
            return await asyncio.wait_for(future, timeout=self._send_timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "whatsapp.baileys.send_timeout request=%s written=%s",
                request_id, mark.written,
            )
            if mark.written:
                return proto.local_failure(
                    proto.REASON_SEND_TIMEOUT, definite=False,
                )
            # The lock was never acquired, so this send never reached a socket.
            return proto.local_failure(proto.REASON_NOT_WRITTEN, definite=True)
        finally:
            self._pending.pop(request_id, None)


__all__ = [
    "BaileysBridge",
    "BridgeStatus",
    "ENV_SESSION_DIR",
    "ENV_SOCKET",
    "INBOUND_ATTEMPTS",
    "INBOUND_QUEUE_MAX",
    "SEND_TIMEOUT_SECONDS",
    "SESSION_DIR_NAME",
    "SOCKET_NAME",
    "active_bridge",
    "clear_active_bridge",
    "default_session_dir",
    "default_socket_path",
    "ensure_session_dir",
    "harden_session_files",
    "read_status",
    "set_active_bridge",
]
