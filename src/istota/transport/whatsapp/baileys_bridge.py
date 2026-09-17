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
another uid. The sidecar sets `umask 0o077` **on itself** so its own files land
0600 — the daemon's spawn passes the same umask, but that reaches only the
`istota serve` shape, and the systemd unit and the compose service were both
writing 0644 until the program took it over (`applyPrivateUmask`) — and its
stdio is discarded so it cannot print into the daemon's log; the directory is
bound into no sandbox at any path; and neither its contents nor a `qr` payload
is ever logged, a QR being the pairing credential itself.

**A pairing code is relayed only inside an armed window, and never through
this module's log.** `_handle_qr` discards the payload with a note unless an
admin-initiated `PairingWindow` is open, which is what every deployment has
always done — relaying every QR the bridge sees would publish a full-account
credential to anyone who can reach `/admin` on each unpaired boot. Inside a
window it goes to a 0600 file (`pairing_relay`), which is unlinked the moment
the window closes by any route, and every touch of that file happens on a
thread: the read loop dispatches and never works, and `_call_back` is not a
thread hop however its name reads.

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
import math
import os
import secrets
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ...config import Config
from . import baileys_protocol as proto, message_fingerprint, pairing_relay
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

#: How long `reset_session` waits for the supervisor loop to finish before it
#: gives up rather than moving the session directory anyway. The loop is
#: already returning by then — a permanent fatal is what got us here — so this
#: bounds `_reap_process`'s own two grace periods and nothing else.
RESET_SETTLE_SECONDS = SHUTDOWN_GRACE_SECONDS + TERMINATE_GRACE_SECONDS + 2.0

#: How long an admin-initiated pairing window stays armed. Matching
#: `cli.WHATSAPP_PAIR_TIMEOUT_SECONDS`, which is about fifteen QR rotations —
#: long enough to find a phone, short enough that a credential is not
#: relayable for an afternoon.
PAIRING_WINDOW_SECONDS = 300.0

#: How long the pairing sequence waits for the link to drop after it has sent
#: `shutdown`. A **constant**, because the sidecar's own path from that frame to
#: `exit(0)` is a socket teardown and a process exit with no dependence on any
#: supervisor.
SIDECAR_STOP_TIMEOUT = 15.0

#: How long after a re-pair another one is refused. It stands in for
#: `reset_session`'s once-per-bridge `_reset_used` on a bridge that lives as
#: long as the daemon, where that flag would allow one re-pair per daemon
#: restart on a surface whose whole purpose is recovery. Longer than
#: `PAIRING_WINDOW_SECONDS`, which is the property that matters: it makes a
#: retry loop against a link WhatsApp watches for churn pointless without
#: obstructing somebody who mis-scanned.
#:
#: **Stamped when the `shutdown` frame is written, not when the move
#: succeeds.** What is being rationed is the induced restart — ISSUE-497's
#: churn against a link WhatsApp watches — and a sequence that aborted at
#: `SIDECAR_STOP_TIMEOUT` spent one just as surely as one that paired.
RESET_COOLDOWN = 600.0

#: How often the pairing sequence re-reads `connected` while it waits for a
#: sidecar to appear. Only the *appear* wait polls; the wait for the link to
#: drop is event-driven, because that one is where the rename's safety margin
#: is measured in Docker's 100ms restart backoff rather than in systemd's 30s.
_SIDECAR_POLL_INTERVAL = 0.05

#: The fixed half of `sidecar_return_timeout` — node boot, the Baileys load, a
#: TLS and Noise handshake, and the 500ms the sidecar waits for its own frame
#: to leave the socket. ISSUE-496 measured 42 restarts in about 25 minutes at
#: `RestartSec=30`, i.e. a ~35.7s cycle: roughly 30s down and about 5.7s up.
SIDECAR_RETURN_FIXED_SECONDS = 30.0

#: How long the watchdog will sleep in one go. The deadlines drive the sleeps,
#: so this is only the ceiling that keeps a window whose clock was read wrongly
#: from parking for its whole TTL.
PAIRING_WATCHDOG_MAX_SLEEP = 1.0

#: The relay file's name beside the state directory. See `pairing_relay`.
PAIRING_RELAY_NAME = "whatsapp-pairing.json"

#: How many names `_reset_destination` will probe before giving up. The stamp
#: is one second wide, so a collision means two resets inside one second and a
#: handful of suffixes covers it; the ceiling is there because an unbounded
#: walk is a hang wherever a stat is answering wrongly.
_RESET_NAME_ATTEMPTS = 1000

#: A `fatal` naming one of these is not retried by respawning. The session is
#: gone and only `istota whatsapp pair --reset` brings it back, so a respawn
#: loop would burn a process every few seconds while changing nothing. An explicit
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


def sidecar_return_timeout(restart_interval_seconds) -> float:
    """How long to wait for a sidecar to *appear*, from a declared interval.

    It bounds **both** waits for one: the one before the `shutdown` frame,
    where the pairing sequence needs a connected peer to send it to, and the
    one after the rename, where a window is waiting for a code. Those are the
    same quantity measured at two points, so they share one answer rather than
    acquiring two names.

    **Additive rather than a multiple, because only one of its terms scales.**
    A whole restart interval can elapse before the boot begins, but the boot
    itself is the same ~6s whatever the interval. At `RestartSec=5` a `3x`
    multiple gives 15s against that fixed cost and would report a healthy
    restart as a missing sidecar on a slow host; additive gives 35s, correctly
    dominated by the fixed allowance.

    The declaration **gates nothing** — it lives in a unit file the daemon
    never reads, so a wrong value costs only an inaccurate `sidecar_absent`
    deadline rather than permission to act. Which is why anything unusable
    (undeclared, negative, not a number at all) falls back to the fixed
    allowance instead of raising: 0 is what compose renders, since Docker's
    sub-second backoff is not an interval.
    """
    try:
        declared = float(restart_interval_seconds)
    except (TypeError, ValueError):
        declared = 0.0
    if not math.isfinite(declared) or declared <= 0:
        declared = 0.0
    return declared + SIDECAR_RETURN_FIXED_SECONDS


#: The same function under a name the constructor's own keyword argument does
#: not shadow. The keyword keeps the public spelling because that is what a
#: caller overriding the wait is naming.
_return_timeout_for = sidecar_return_timeout


def default_pairing_relay_path(config: Config) -> Path:
    """Where the QR relay file goes unless the operator named somewhere else.

    `{db_path.parent}` on both server shapes: the deployment's state root,
    which the socket and — unless the operator pointed `session_dir`
    elsewhere — the paired session itself already sit in, and which the
    sandbox binds at no path.

    **The standalone install is the exception and the path moves off the state
    root there.** `setup_wizard` puts `db_path` at `{workspace}/istota.db`, so
    `db_path.parent`, `workspace_path` and the state root are one directory:
    the shape where `_mask_dir` refuses outright, and where a `user_resources`
    row is bounded by `workspace_path` alone. The credential *class* is no
    different from `creds.json` already sitting in that unmasked tree, but a
    QR is a scan-and-done artifact with no keystore to exfiltrate, live for
    300s and rotating every 20, so it is worth keeping out of the way.

    **What the move buys is narrower than it looks, and is stated rather than
    implied**: `{workspace}/tmp` is still inside `workspace_path`, so a
    resource naming the workspace *root* reaches it either way. What it does
    buy is that the file is not in the directory such a row most plausibly
    names, and is a sibling of the per-user temp directories the sandbox binds
    rather than inside one. The refusal for a shape where the resolved relay
    would still land inside a bound root belongs to the sequence that opens a
    window, which is where the task's own bind list is known.
    """
    db_parent = Path(config.db_path).parent
    workspace = config.workspace_path
    if workspace is not None and _same_directory(workspace, db_parent):
        return Path(config.temp_dir) / PAIRING_RELAY_NAME
    return db_parent / PAIRING_RELAY_NAME


def _same_directory(left, right) -> bool:
    """Whether two paths name one directory, comparing resolved where it can.

    Resolved, because the collapse this decides is a fact about the directory
    rather than about how it was spelled — a symlinked workspace root and a
    `db_path` written through it are the same place. Falls back to a lexical
    comparison rather than raising: `resolve()` answers `ValueError` for an
    embedded null byte, and the caller is a constructor on a boot path.
    """
    try:
        return Path(left).resolve() == Path(right).resolve()
    except (OSError, ValueError, RuntimeError):
        return Path(left) == Path(right)


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
    than an omission.** `in_tree_sidecar_argv` below is that fallback and
    `cli.cmd_whatsapp_pair` is its one caller, where a developer's checkout is
    the case it serves. Here it would fire on exactly
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
    if not entry.is_file():
        return ()
    # Bound once. Two `PATH` walks with a gap between them can answer
    # differently, and the second answering `None` renders the string "None"
    # into the argv.
    node = shutil.which("node")
    if node is None:
        return ()
    if not (_SIDECAR_IN_TREE / "node_modules" / "@whiskeysockets" / "baileys").is_dir():
        # The library is required lazily inside the sidecar, so without this
        # the program starts and fails at its first import. That used to reach
        # the daemon as a permanent `bad_session`, which pages every admin
        # about an unlinked device on a deployment that has never paired; the
        # sidecar classifies it now, and declining to name the argv is the
        # earlier and quieter half of the same answer.
        logger.warning(
            "whatsapp.baileys.dependencies_missing: the sidecar is in this "
            "tree at %s and its dependencies are not installed (`npm ci`)",
            _SIDECAR_IN_TREE,
        )
        return ()
    return (node, str(entry))


def shipped_library_version() -> str:
    """The Baileys version this checkout's sidecar pins, or ``""``.

    Read out of `package.json` rather than imported, since nothing in Python
    imports the sidecar. `""` on any failure — a wheel install has no
    `docker/` directory at all, and a diagnostic that cannot read the file has
    not learned that two versions disagree.
    """
    import json  # noqa: PLC0415

    try:
        manifest = json.loads((_SIDECAR_IN_TREE / "package.json").read_text())
        pinned = manifest["dependencies"]["@whiskeysockets/baileys"]
    except Exception:
        return ""
    return pinned if isinstance(pinned, str) else ""


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


def _wide_session_files(path: Path):
    """Every regular file in the session directory wider than 0600.

    The walk both the narrowing pass and the read-only survey run, written
    once so a diagnostic and a repair cannot disagree about what "wide" means
    — `.claude/rules/doctor.md`'s rule that a check asks the owning module's
    own predicate rather than keeping a copy.

    `lstat`, so a symlink is judged as the link rather than as its target, and
    an entry that vanished between the listing and the stat is skipped: it is
    not a widened session file, and counting it as one is what an earlier
    version of the narrowing pass got wrong.

    Never raises. An unreadable directory yields nothing.
    """
    try:
        entries = sorted(path.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            info = entry.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) == 0o600:
            continue
        yield entry


def survey_session_files(path: Path) -> int:
    """How many session files are wider than 0600, changing nothing.

    `harden_session_files`' read-only twin, and it exists because `doctor` may
    not be the second writer of a full-account credential's permissions. It
    was: the session check called the narrowing pass, so the exposure was
    repaired from whichever of four processes happened to run a diagnostic
    first — and then *self-cleared*, so the hourly sweep's transition alerting
    could observe it once and an operator who reran the command to confirm saw
    a clean tree. A check that repairs is a check that cannot report.

    Never raises, for the reason every `doctor` helper does not.
    """
    return sum(1 for _ in _wide_session_files(path))


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
    for entry in _wide_session_files(path):
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


class SessionResetRefused(Exception):
    """`reset_session` declined, and the session directory is untouched.

    Raised rather than reported through a return value, because the caller's
    next step differs by reason and none of the reasons lets it carry on: the
    directory holds a full WhatsApp account, and each refusal below is a way
    that moving it would either destroy a credential that was coming back on
    its own or move one out from under a live Baileys client.
    """


class SessionResetIncomplete(Exception):
    """The old session moved and could be neither replaced nor put back.

    Its own class rather than a `SessionResetRefused`, because the two ask
    opposite things of whoever catches them: a refusal means nothing happened
    and the operator can retry, while this means a full-account credential is
    sitting at a path nothing else names. `moved_to` carries that path so the
    caller can print it — the one thing that has to reach a human here.
    """

    def __init__(self, message: str, *, moved_to: Path) -> None:
        super().__init__(message)
        self.moved_to = moved_to


@dataclass
class PairingWindow:
    """One admin-initiated, TTL-bounded permission to relay a pairing code.

    **The relay is never ambient.** Without a window, `_handle_qr` keeps its
    original behaviour and discards the payload — which is what every Baileys
    deployment has always done. Relaying every QR the bridge sees would publish
    a full-account credential to anyone who can reach `/admin` on every
    unpaired boot, and that is not a trade worth making.

    Two clocks, deliberately. `opened_at` and `expires_at` are the event loop's
    monotonic clock, because they drive the watchdog and a backwards NTP step
    must not extend or cut a window somebody is mid-scan on.
    `expires_at_wall` is the same instant in epoch seconds, because it is what
    crosses to another process — the relay file and the durable request row —
    where a monotonic reading means nothing.
    """

    window_id: str
    opened_at: float
    expires_at: float
    expires_at_wall: float
    requested_by: str
    destructive: bool
    #: The last state published to the relay. `awaiting_sidecar` at open.
    state: str = pairing_relay.STATE_AWAITING_SIDECAR
    #: Incremented per QR rotation, so a reader can tell a new code from a
    #: re-read of the same one without holding the payload to compare.
    qr_seq: int = 0
    message: str = ""


@dataclass(frozen=True)
class PairingOutcome:
    """How the last window ended, for whoever owns the durable request row.

    The bridge writes no database row — this module never raises and never
    opens a connection, and the owner that started it does that work. But a
    closed window is indistinguishable from a process that died holding one
    unless the terminal state is readable from here, so this is what lets a
    successful pairing be recorded as `paired` rather than swept up as an
    orphan.
    """

    window_id: str
    state: str
    message: str


#: `PairingResult.reason` codes. Machine-readable, because the web route maps
#: them onto status codes and the durable row records them; the prose an
#: operator reads is `message`, which is built per case.
PAIRING_OK = "pairing"
PAIRING_ALREADY = "already_pairing"
PAIRING_COOLDOWN = "cooldown"
PAIRING_SESSION_LIVE = "session_live"
PAIRING_SIDECAR_ABSENT = "sidecar_absent"
PAIRING_STOP_TIMEOUT = "stop_timeout"
PAIRING_FRAME_UNSENT = "frame_unsent"
PAIRING_SIDECAR_RETURNED = "sidecar_returned"
PAIRING_SESSION_DIR_UNUSABLE = "session_dir_unusable"
PAIRING_STOPPING = "stopping"
PAIRING_SHAPE_UNSUPPORTED = "shape_unsupported"
PAIRING_RESET_REFUSED = "reset_refused"
PAIRING_RESET_INCOMPLETE = "reset_incomplete"
PAIRING_NO_WINDOW = "no_window"
PAIRING_FAILED = "failed"


@dataclass(frozen=True)
class PairingResult:
    """What one `repair_session` did, as a value rather than an exception.

    `repair_session` never raises — it runs from a scheduler tick and from a
    web route, and every one of its refusals is a state an operator has to be
    told about rather than a fault. `reason` is one of the codes above and
    `message` is the prose that names the unit, the remedy or the path.

    **`moved_to` is on the result even when `ok` is False**, and that is the
    field to be careful with: the sequence can move a full-account credential
    aside and then fail to open a window, and a result that dropped the path
    would leave an operator with two directories and no way to tell which
    holds their session. Stage 3's admin notification reads it for the same
    reason.
    """

    ok: bool
    reason: str
    window_id: str | None = None
    message: str = ""
    moved_to: Path | None = None
    #: Whether this attempt cost a sidecar restart. True from the moment the
    #: `shutdown` frame reaches `writer.write` on the external-unit shape, and
    #: from a returning `reset_session` on the spawned one — so an abort can
    #: say whether it was a clean no-op, which only `sidecar_absent` and
    #: `frame_unsent` are. It is not "a frame was written", because the
    #: delegated path spends a restart without writing one.
    restart_spent: bool = False


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
    #: Whether the sidecar's logged-out backoff is running on a guess because
    #: it could not record the run (ISSUE-501). The condition's only other
    #: signal is a warning the sidecar appends to `sidecar.log` *inside* the
    #: directory it cannot write, so without this it reaches no operator at
    #: all — and a deployment retrying every 30s looks from here exactly like
    #: one backing off correctly.
    fatal_run_unrecorded: bool = False
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
    #: The open pairing window's state, or `None` when none is open — derived
    #: in the `status` property from the live window rather than stamped at
    #: each transition, so the two cannot drift and a closed window reads as
    #: closed. **Neither of these may ever become the payload**: `read_status`
    #: returns this dataclass to `doctor`, which renders it into the boot log
    #: and the admin dashboard.
    pairing_state: str | None = None
    #: Wall clock, so it means something to a reader in another process.
    pairing_expires_at: float | None = None


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
        pairing_relay_path: Path | None = None,
        pairing_window_seconds: float = PAIRING_WINDOW_SECONDS,
        restart_interval_seconds: float = 0.0,
        sidecar_return_timeout: float | None = None,
        sidecar_stop_timeout: float = SIDECAR_STOP_TIMEOUT,
        reset_cooldown: float = RESET_COOLDOWN,
        on_qr=None,
        on_fatal=None,
    ) -> None:
        self._config = config
        self._sidecar_argv = tuple(sidecar_argv)
        self._socket_path = Path(socket_path or default_socket_path(config))
        self._session_dir = Path(session_dir or default_session_dir(config))
        self._send_timeout = send_timeout
        self._on_qr = on_qr
        # The pairing window and its relay. Every one of these is a
        # constructor parameter rather than a config read, because the four
        # `[whatsapp.baileys]` keys behind them land with the web API; this is
        # the seam they fill.
        self._pairing_relay_path = Path(
            pairing_relay_path or default_pairing_relay_path(config)
        )
        self._pairing_window_seconds = float(pairing_window_seconds)
        self._sidecar_return_timeout = (
            float(sidecar_return_timeout)
            if sidecar_return_timeout is not None
            else _return_timeout_for(restart_interval_seconds)
        )
        self._sidecar_stop_timeout = float(sidecar_stop_timeout)
        self._reset_cooldown = float(reset_cooldown)
        self._pairing_window: PairingWindow | None = None
        self._pairing_watchdog: asyncio.Task | None = None
        self._last_pairing_outcome: PairingOutcome | None = None
        # Single-flight for `repair_session`, which is not the same thing as
        # "a window is open": the window is not opened until the last step,
        # after up to `sidecar_return_timeout + sidecar_stop_timeout` of
        # waiting, so the window alone leaves the whole destructive half
        # re-entrant. Set synchronously before the first await and cleared in a
        # `finally`.
        self._repairing = False
        # Loop-monotonic, and `None` until a restart has been spent on this
        # bridge. See `RESET_COOLDOWN`.
        self._last_reset_at: float | None = None
        # Serializes every relay touch, and it is the ordering *and* the
        # anti-resurrection guard: each job re-reads the live window while
        # holding this lock, so a write scheduled before a window closed finds
        # a stale window and skips rather than re-creating a credential file
        # behind the unlink. Nothing in this stage sweeps that path.
        #
        # **A `threading.Lock`, taken inside the worker thread, rather than an
        # `asyncio.Lock` taken around the thread hop.** The asyncio form looks
        # equivalent and is not: cancelling the task that awaits
        # `asyncio.to_thread` does not stop a thread that has already started
        # — `run_in_executor` can only cancel a future whose work has not
        # begun — so the `async with` unwinds and *releases the lock* with the
        # write still live. The unlink then takes the free lock, removes the
        # file, and the orphaned thread's `os.replace` puts a pairing code
        # back on disk after its window has closed. That cancel is not
        # hypothetical: `AsyncRuntime._shutdown` cancels every pending task
        # before it runs the cleanup hook that calls `stop()`. Held inside the
        # thread, a cancelled awaiter cannot release it early, and the guard
        # below runs after any pending unlink rather than before it.
        self._relay_lock = threading.Lock()
        self._relay_tasks: set[asyncio.Task] = set()
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
        # One reset per bridge. ISSUE-497 established that a sidecar restart
        # drops and rebuilds a link WhatsApp watches for churn, and that the
        # cost is invisible from our side because the credential is on disk
        # rather than in the process. Once per pairing incident is fine; a
        # caller free to ask again on the next fatal is a retry loop against
        # exactly that, so the bound sits on the primitive rather than on
        # whoever happens to call it.
        self._reset_used = False
        self._write_lock = asyncio.Lock()
        # Set by a permanent `fatal`, and waited on beside the child's exit.
        # Checking it only at the top of the respawn loop would leave a
        # logged-out sidecar running until it exited on its own, which for a
        # long-lived process is never.
        self._permanent_fatal = asyncio.Event()
        # Set by `_close_link`, cleared when `_on_connect` adopts a writer and
        # again by `repair_session` immediately before it writes its
        # `shutdown` frame. **Those two clears are how the sequence tells a
        # drop its own frame caused from any other drop it happens to
        # observe**, and without them the latch is "has any disconnect ever
        # happened", which is the passive draft this design replaced: a
        # disconnect from minutes earlier would satisfy the wait and move the
        # session directory with no frame behind it.
        #
        # **Measured, either clear carries the property alone** — removing one
        # turns no test red and removing both turns three red — so the pair is
        # defence in depth rather than two halves of one mechanism. Both stay:
        # `_on_connect`'s maintains the invariant that an adopted writer
        # implies a clear latch, and `repair_session`'s re-asserts it at the
        # point of use, which is `reset_session`'s own rule for a gate that
        # everything after depends on.
        #
        # Event-driven rather than polled because the margin it is measured
        # against is Docker's 100ms restart backoff: the wait has to wake on
        # the callback that closed the link, with nothing between it and the
        # rename.
        self._link_dropped = asyncio.Event()

    # -- lifecycle ----------------------------------------------------------

    @property
    def status(self) -> BridgeStatus:
        self._status.queue_depth = self._queue.qsize()
        self._status.pending_sends = len(self._pending)
        # Derived here rather than stamped at each transition, like
        # `queue_depth` above: one source, no drift, and `None` the moment the
        # window closes so `doctor`'s "an open window is reported" stays true.
        window = self._pairing_window
        self._status.pairing_state = None if window is None else window.state
        self._status.pairing_expires_at = (
            None if window is None else window.expires_at_wall
        )
        return self._status

    # -- pairing window -----------------------------------------------------

    @property
    def pairing_relay_path(self) -> Path:
        return self._pairing_relay_path

    @property
    def pairing_window(self) -> PairingWindow | None:
        return self._pairing_window

    @property
    def last_pairing_outcome(self) -> PairingOutcome | None:
        """How the last window ended, for the owner of the durable row."""
        return self._last_pairing_outcome

    async def open_pairing_window(
        self, requested_by: str, *, destructive: bool = False,
    ) -> PairingWindow | None:
        """Arm the relay for `PAIRING_WINDOW_SECONDS`, or refuse.

        **`None` has three producers and a caller cannot tell them apart**:
        a window is already open (the single-flight rule behind the route's
        409), the bridge is stopping, or it began stopping during the two
        awaits below. A discriminated refusal belongs with the `PairingResult`
        the sequence around this returns, not here, so the note is that all
        three mean "no window was opened and nothing was touched".

        Awaits the initial relay publish, so a caller returns with the state
        already readable by the web process; the window is installed *before*
        that await, or a QR arriving during it would be discarded by a handler
        that sees no window. Both gates are then re-read, which is
        `reset_session`'s own rule for the same reason: they were answered
        before two awaits, and returning a window `stop()` has since closed
        would have the caller write a durable row for a window that does not
        exist.

        Never raises. A relay that cannot be written still leaves a window
        open — the code is then invisible to the UI and the durable row says
        so, which is a worse pairing rather than a failed daemon.
        """
        if self._pairing_window is not None:
            return None
        if self._stopping:
            return None
        loop = asyncio.get_running_loop()
        opened_at = loop.time()
        window = PairingWindow(
            window_id=uuid.uuid4().hex,
            opened_at=opened_at,
            expires_at=opened_at + self._pairing_window_seconds,
            expires_at_wall=time.time() + self._pairing_window_seconds,
            requested_by=str(requested_by),
            destructive=bool(destructive),
        )
        self._pairing_window = window
        self._pairing_watchdog = asyncio.create_task(self._watch_pairing(window))
        # `mkstemp` needs the directory to exist, and on the shape where the
        # relay resolves to the temp root that directory is the daemon's own to
        # make. Off the loop like every other relay touch. The mode is left to
        # the umask deliberately: what carries the credential is the 0600 file,
        # and narrowing a directory the rest of the deployment shares would be
        # this module writing somebody else's permissions.
        await self._guarded_relay_job(
            asyncio.to_thread(self._ensure_relay_parent),
        )
        logger.info(
            "whatsapp.pairing.window_opened window=%s by=%s destructive=%s "
            "ttl=%.0fs",
            window.window_id, window.requested_by, window.destructive,
            self._pairing_window_seconds,
        )
        await self._guarded_relay_job(self._publish_relay(window))
        if self._pairing_window is not window or self._stopping:
            return None
        return window

    async def cancel_pairing(self) -> bool:
        """Close an open window and unlink the relay. `False` if none was open.

        Does **not** restore a session directory an earlier step moved aside:
        the sidecar may already have written a partial new session into the
        recreated one, and choosing between the two is an operator's call
        rather than a heuristic's.
        """
        window = self._pairing_window
        if window is None:
            return False
        await self._close_pairing_window(
            window,
            pairing_relay.STATE_FAILED,
            "the pairing window was cancelled before a code was scanned",
        )
        return True

    def _end_pairing_window(
        self, window: PairingWindow, state: str, message: str,
    ) -> bool:
        """Drop the window and record how it ended, synchronously.

        The synchronous half of a close, because one of the three things that
        race to close a window is a `ready` frame arriving on the read loop,
        where `_dispatch` cannot await — and a window that is dropped a tick
        later is a window that relays one more QR after the session came up.
        The unlink is the asynchronous half; `_relay_lock` is what keeps it
        ordered against any write already queued.

        `False` where this is no longer the live window, so a close is
        idempotent however many of the three reach it.
        """
        if self._pairing_window is not window:
            return False
        self._pairing_window = None
        self._last_pairing_outcome = PairingOutcome(
            window_id=window.window_id, state=state, message=message,
        )
        watchdog, self._pairing_watchdog = self._pairing_watchdog, None
        if watchdog is not None and watchdog is not asyncio.current_task():
            watchdog.cancel()
        logger.info(
            "whatsapp.pairing.window_closed window=%s state=%s",
            window.window_id, state,
        )
        return True

    async def _close_pairing_window(
        self, window: PairingWindow, state: str, message: str,
    ) -> None:
        if self._end_pairing_window(window, state, message):
            await self._clear_relay()

    async def _publish_relay(self, window: PairingWindow) -> None:
        """Write the window's state with no payload.

        The payload is snapshotted here, on the loop, so the worker thread
        reads nothing mutable but the two guard values — and so a rotation
        that changed the state between this call and the thread running is
        what the guard refuses rather than something this payload silently
        carries.
        """
        await asyncio.to_thread(
            self._write_relay_under_lock,
            window,
            pairing_relay.build_payload(
                window_id=window.window_id,
                state=window.state,
                expires_at=window.expires_at_wall,
                qr_seq=window.qr_seq,
                message=window.message,
            ),
            window.state,
            None,
        )

    async def _publish_relay_qr(
        self, window: PairingWindow, qr_seq: int, value: str,
    ) -> None:
        """Publish one rotation's payload.

        The payload is an **argument** rather than a field on the window: the
        window lives for five minutes and a code lives for twenty seconds, so
        keeping it would leave the credential in the daemon's memory long
        after it had stopped being scannable, and every later `_publish_relay`
        would have to decide whether to re-publish it.
        """
        await asyncio.to_thread(
            self._write_relay_under_lock,
            window,
            pairing_relay.build_payload(
                window_id=window.window_id,
                state=window.state,
                expires_at=window.expires_at_wall,
                qr=value,
                qr_seq=qr_seq,
                message=window.message,
            ),
            window.state,
            qr_seq,
        )

    def _write_relay_under_lock(
        self,
        window: PairingWindow,
        payload: dict,
        expected_state: str,
        expected_seq: int | None,
    ) -> None:
        """Publish, in a worker thread, unless the window has moved on.

        Three guards, and each refuses a write that would publish something
        untrue by the time it reached the disk:

        - the window is no longer the live one, so the file would be a
          credential re-created behind its own unlink;
        - its state has moved on, so a `sidecar_absent` notice queued a moment
          before a code arrived would clobber that code with a payload-free
          record of a wait that is over;
        - a later rotation already landed, so this one is stale.

        All three are read **here**, inside the lock and inside the thread,
        rather than around the thread hop — see `_relay_lock`.
        """
        with self._relay_lock:
            if self._pairing_window is not window:
                return
            if window.state != expected_state:
                return
            if expected_seq is not None and window.qr_seq != expected_seq:
                return
            pairing_relay.write_relay(self._pairing_relay_path, payload)

    def _ensure_relay_parent(self) -> None:
        self._pairing_relay_path.parent.mkdir(parents=True, exist_ok=True)

    async def _clear_relay(self) -> None:
        await asyncio.to_thread(self._clear_relay_under_lock)

    def _clear_relay_under_lock(self) -> None:
        with self._relay_lock:
            pairing_relay.clear_relay(self._pairing_relay_path)

    def _spawn_relay_job(self, coro) -> None:
        """Run a relay coroutine off the read loop's own call stack.

        **`_call_back` is not what does this**, and its name invites the
        mistake: it runs its callback inline and hands only an awaitable
        *result* to `asyncio.ensure_future`, which schedules it on this same
        loop thread. What transfers from it is the part that is a rule rather
        than a mechanism — hold the task so the loop cannot collect it
        mid-flight, and log without `exc_info`, because a traceback frame here
        holds the pairing credential.
        """
        task = asyncio.ensure_future(self._guarded_relay_job(coro))
        self._relay_tasks.add(task)
        task.add_done_callback(self._relay_tasks.discard)

    async def _guarded_relay_job(self, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the contract is never-raises
            logger.warning(
                "whatsapp.pairing.relay_job_failed reason=%s",
                type(exc).__name__,
            )

    async def _watch_pairing(self, window: PairingWindow) -> None:
        """Close the window at its TTL, and name a sidecar that never came.

        Two deadlines on one task. The **shorter** one is ISSUE-497's residual
        made visible: a failing `npm ci` in the update cron stops the sidecar's
        unit and aborts before either restart arm, so it can be left down
        indefinitely with the reason only in the update log. Past
        `sidecar_return_timeout` the window says `sidecar_absent` and names
        where to look, rather than sitting on an empty wait until the TTL —
        and it **stays open**, because a late sidecar still pairs.

        The demotion applies only to a window still `awaiting_sidecar`. A
        window that already has a code in hand must not be clobbered back.
        """
        absent_at = window.opened_at + self._sidecar_return_timeout
        loop = asyncio.get_running_loop()
        try:
            while True:
                now = loop.time()
                if self._pairing_window is not window:
                    return
                if now >= window.expires_at:
                    await self._close_pairing_window(
                        window,
                        pairing_relay.STATE_EXPIRED,
                        "the pairing window expired with no code scanned",
                    )
                    return
                if (
                    now >= absent_at
                    and window.state == pairing_relay.STATE_AWAITING_SIDECAR
                ):
                    # **Two faults reach this deadline and the message says
                    # which.** The state is the same either way, because what
                    # it is for is stopping the UI from implying a code is on
                    # its way — but a sidecar that is *connected* and has not
                    # offered one is not a stopped unit, and telling an
                    # operator to go and look at the unit and the update log
                    # is a false alarm that `doctor` will repeat as a WARN.
                    # `connected` is the bridge's own answer and is right
                    # here; it is not enough to *gate* the demotion on,
                    # which would restore the empty wait to the full TTL.
                    window.state = pairing_relay.STATE_SIDECAR_ABSENT
                    if self._status.connected:
                        window.message = (
                            "a WhatsApp sidecar is connected and has not "
                            f"offered a pairing code in "
                            f"{self._sidecar_return_timeout:.0f}s, which is "
                            "what a session starting from a credential that "
                            "is still there looks like. Check `sidecar.log` "
                            "inside the session directory. A code that "
                            "arrives later still pairs."
                        )
                    else:
                        window.message = (
                            "no WhatsApp sidecar has connected in "
                            f"{self._sidecar_return_timeout:.0f}s. Check its "
                            "own unit or compose service and the deploy "
                            "update log — a failed dependency install can "
                            "leave it stopped. A late sidecar still pairs."
                        )
                    logger.warning(
                        "whatsapp.pairing.sidecar_absent window=%s connected=%s",
                        window.window_id, self._status.connected,
                    )
                    await self._publish_relay(window)
                    continue
                deadline = window.expires_at
                if window.state == pairing_relay.STATE_AWAITING_SIDECAR:
                    deadline = min(deadline, absent_at)
                await asyncio.sleep(
                    max(0.0, min(deadline - now, PAIRING_WATCHDOG_MAX_SLEEP)),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the contract is never-raises
            # **Fail closed.** Logging and falling off the end of this
            # coroutine leaves the window armed with nothing left to enforce
            # its TTL and the credential on disk until the process stops —
            # which is the opposite of what a relay whose whole contract is
            # "unlinked the moment the window closes by any route" wants from
            # its last line of defence. The realistic trigger is
            # `asyncio.to_thread` answering `RuntimeError` as the default
            # executor shuts down, i.e. exactly the shutdown case.
            logger.warning(
                "whatsapp.pairing.watchdog_failed window=%s reason=%s: "
                "closing the window rather than leaving it armed",
                window.window_id, type(exc).__name__,
            )
            with contextlib.suppress(Exception):
                await self._close_pairing_window(
                    window,
                    pairing_relay.STATE_FAILED,
                    "the pairing window was closed because its watchdog "
                    "could not run",
                )

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
        # **Before anything else**, because a graceful shutdown mid-window
        # would otherwise leave a live pairing code on disk indefinitely:
        # nothing in this process sweeps that path, and the durable request row
        # that would notice an orphaned window belongs to a later stage. This
        # runs on a task rather than the read loop, so it can await the unlink.
        window = self._pairing_window
        if window is not None and self._end_pairing_window(
            window,
            pairing_relay.STATE_FAILED,
            "the bridge stopped before a code was scanned",
        ):
            # **The drop is synchronous and only the unlink can be skipped.**
            # Every other await in this method is already inside a
            # `contextlib.suppress`, because the docstring's "never raises" is
            # what keeps the socket inode from outliving the process — and
            # `_guarded_relay_job` deliberately re-raises `CancelledError`, so
            # an unguarded await here would abort `stop()` before the sidecar
            # was even asked to leave. `AsyncRuntime._shutdown` cancels
            # pending tasks before it runs the cleanup hook that calls this,
            # so that cancel is the ordinary case rather than a corner. The
            # cost of a skipped unlink is a relay file nothing sweeps, which
            # is the accepted residual named in `pairing_relay`.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self._clear_relay()
        # Cancelled rather than left pending, like the supervisor and the
        # worker below: a relay job still holding an executor thread at loop
        # close is a destroyed-pending-task warning on the `istota serve`
        # shape. It cannot resurrect the file — `_write_relay_under_lock`
        # refuses a window that is no longer live, and the window is already
        # gone by here.
        for task in list(self._relay_tasks):
            task.cancel()
        for task in list(self._relay_tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
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

    def _reset_destination(self) -> Path:
        """Where a dead session directory goes: a timestamped sibling.

        A **sibling**, because the move has to be one `rename(2)` inside one
        directory — a copy-then-delete has a window where the credential
        exists twice and a crash where it exists nowhere, and a destination on
        another filesystem turns the rename into exactly that. Timestamped
        rather than a fixed `.old`, so the second logout of a deployment's
        life does not overwrite the record of the first.

        The stamp is one second wide, so the name is settled by probing rather
        than by trusting the clock to be distinct: what stands at the
        destination is the only copy of what was in the session directory, and
        `rename(2)` onto an empty one would take it silently. **The probe is
        the whole of that guarantee** — there is no `RENAME_NOREPLACE` behind
        it, so a directory created at the chosen name between the `lexists`
        and the rename is still taken. Not a live hazard on either shipped
        shape, where the parent is the operator's own state directory, but it
        is the probe rather than the syscall that holds.

        Bounded at `_RESET_NAME_ATTEMPTS`, matching `session_log`'s solution
        to the identical problem: a walk with no ceiling is a hang where a
        stat is answering wrongly, and both callers would rather fail.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base = self._session_dir.parent / f"{self._session_dir.name}.{stamp}"
        candidate = base
        suffix = 1
        # `lexists`, not `exists`: a broken symlink standing at the name is
        # something, and `rename(2)` would replace it.
        while os.path.lexists(candidate) and suffix < _RESET_NAME_ATTEMPTS:
            candidate = base.with_name(f"{base.name}-{suffix}")
            suffix += 1
        return candidate

    async def reset_session(self) -> Path | None:
        """Move a dead session aside so the sidecar can offer a code again.

        The recovery ISSUE-496 found there was no path to. A `logged_out`
        session leaves `creds.json` naming a device WhatsApp has unlinked, and
        `useMultiFileAuthState` reads a registered account there and attempts
        a login rather than emitting a QR — so the one thing that could
        resolve the state is the one thing that cannot happen while the file
        is there, and every restart, `istota whatsapp pair` included, met the
        same refusal. The remedy an operator was left with was composing an
        `rm -rf` against a full-account credential directory, as root, on a
        host where WhatsApp was already down.

        **The move is the easy part; the guards are the change.** Four
        refusals stand in front of it, and each is a way the rename would be
        worse than the state it fixes:

        - *A live session is never reset.* Without the `fatal_is_permanent`
          gate this destroys a credential that was merely unreachable and
          about to reconnect on its own.
        - *A bridge that is stopping refuses.* `stop()` cancels the supervisor
          and reaps concurrently, so the two would race for the same child and
          the same directory.
        - *A bridge that does not run the sidecar refuses outright.* On the
          Ansible and compose shapes the sidecar is its own unit and this
          process supervises nothing, so it can establish nothing about
          whether that unit still holds the directory — and `Restart=always`
          means it is coming back whatever we observed. Moving a directory a
          second Baileys client is writing is the auth-state corruption every
          other guard in this module exists to prevent. The operator stops
          that unit and runs `istota whatsapp pair --reset`, which supervises
          its own.
        - *Once per bridge.* See `_reset_used`.

        **Then the child goes before the directory does, and that ordering is
        the property to keep.** A permanent fatal has already ended the
        supervisor loop, which reaps on its way out, so this waits for that
        task rather than racing it — shielded, so a timeout here does not
        cancel a reap in flight. Both waits carry their own bound: neither the
        supervisor nor `_reap_process` is bounded from the inside, and without
        one the refusals below are unreachable rather than merely slow. A
        supervisor or a process that survives all of it is a refusal rather
        than a rename, and so is a latch that cleared while we waited.

        Returns where the old directory went, or `None` where there was
        nothing to move — a `bad_session` fatal on a deployment whose
        directory somebody has already removed by hand reaches here, and
        recreating it is the whole of what that needs. Never partially
        applied: every refusal leaves the directory, the latch and the
        supervisor exactly as they were.
        """
        if self._reset_used:
            raise SessionResetRefused(
                "this bridge has already moved its session aside once; asking "
                "again is a retry loop against a link WhatsApp watches for "
                "churn. Run `istota whatsapp pair --reset` afresh."
            )
        if self._stopping:
            raise SessionResetRefused("the bridge is stopping")
        if not self._sidecar_argv:
            raise SessionResetRefused(
                "this process does not run the WhatsApp sidecar, so it cannot "
                "establish that nothing else holds the session directory. "
                "Stop the sidecar's own unit or compose service and run "
                "`istota whatsapp pair --reset`."
            )
        if not self._status.fatal_is_permanent:
            raise SessionResetRefused(
                "the session has reported no permanent fault, so there is "
                "nothing to move aside"
            )

        supervisor = self._supervisor
        if supervisor is not None and not supervisor.done():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    asyncio.shield(supervisor), timeout=RESET_SETTLE_SECONDS,
                )
        # **A supervisor that outlived the wait is a refusal, and the process
        # check below cannot stand in for it.** `_await_child_or_fatal` sets
        # `self._process = None` itself on the exit arm, so an abandoned loop
        # sleeping between respawns presents no process at all. It is reachable
        # rather than theoretical: that arm is taken whenever the child's exit
        # and the fatal complete in one iteration of the event loop, since
        # `exited.done()` is tested first — and the loop then sleeps its
        # backoff *before* re-reading the latch, which from the fifth
        # consecutive fast crash is longer than this wait. Falling through
        # would clear the latch and create a second supervisor while the first
        # is still sleeping; it would wake, read the cleared latch and spawn a
        # sidecar into the directory the new one is already pairing into. Two
        # Baileys clients on one auth state is the corruption this whole
        # method is arranged to avoid.
        if supervisor is not None and not supervisor.done():
            raise SessionResetRefused(
                "the WhatsApp sidecar supervisor did not stop, so a second "
                "sidecar could be spawned into the session directory"
            )
        # **Bounded here rather than inside `_reap_process`.** That one's last
        # phase is `kill()` and then an unbounded `wait()`, which is right for
        # `stop()` and wrong for this caller: a child that will not reap — an
        # uninterruptible sleep, a descendant holding the transport — would
        # hang `istota whatsapp pair --reset` for ever instead of producing
        # the refusal below, which is the only thing that makes that refusal
        # reachable at all. The bound is this call's, so `stop()`'s shutdown
        # semantics are untouched.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                self._reap_process(), timeout=RESET_SETTLE_SECONDS,
            )
        process = self._process
        if process is not None and process.returncode is None:
            raise SessionResetRefused(
                "the WhatsApp sidecar process could not be stopped, so the "
                "session directory still has a writer"
            )
        # **Both gates are re-read, because this is the last statement before
        # the rename and everything after it is synchronous.** They were
        # answered before two awaits, and during those the read loop can
        # dispatch a `ready` — which clears the latch — or another caller can
        # start `stop()`. Renaming on the strength of a stale answer moves the
        # directory of a session that has just come back.
        if self._stopping:
            raise SessionResetRefused("the bridge is stopping")
        if not self._status.fatal_is_permanent:
            raise SessionResetRefused(
                "the WhatsApp session recovered while the sidecar was being "
                "stopped, so there is nothing to move aside"
            )
        self._process = None
        self._supervisor = None
        # The peer is provably dead, and `_on_connect` refuses a second
        # connection while a writer is live — so a link left standing here
        # would have the respawned sidecar counted as a rejected connection
        # and the reset would wedge with nothing saying why.
        self._close_link(proto.REASON_LINK_LOST)

        destination = self._move_session_aside()
        self._reset_used = True
        self.resume()
        return destination

    def _move_session_aside(self) -> Path | None:
        """Archive the session directory, recreate it, and clear the latch.

        **The valuable half of `reset_session`, shared rather than copied.**
        Two callers reach it with two different kinds of evidence that nothing
        holds the directory, and that axis is the only one they differ on:
        `reset_session` supervised the child itself and awaited its reap, while
        `repair_session` sent a `shutdown` frame to a named peer and observed
        the drop that frame caused. Every guard producing that evidence stays
        with its own caller. What is here is the part that moves a full-account
        credential, which went through two reviewers and ten negative controls
        under ISSUE-496 and must not acquire a second implementation: a copy is
        cheap to write, passes its own tests, and drifts the first time either
        one is fixed.

        **Synchronous, and deliberately so.** There is no await in it, which is
        what lets `repair_session` run it in the same loop iteration as the
        drop it observed — on the compose shape the supervisor's restart
        backoff starts at 100ms, and a suspension point in here is a window for
        a respawned sidecar to be adopted mid-rename.

        Returns where the old directory went, or `None` where there was nothing
        at the name to move. Raises `SessionResetIncomplete` when the archive
        could be neither replaced nor restored, and re-raises anything
        `ensure_session_dir` refused after putting the archive back.
        """
        destination: Path | None = self._reset_destination()
        try:
            os.rename(self._session_dir, destination)
        except FileNotFoundError:
            destination = None
        try:
            ensure_session_dir(self._session_dir)
            (
                self._status.session_files_hardened,
                self._status.session_files_unfixed,
            ) = harden_session_files(self._session_dir)
        except Exception:
            # **Put it back, or say where it went.** `ensure_session_dir`
            # raises rather than degrading, by its own docstring, and by here
            # the credential has already moved — so an unguarded raise leaves
            # the reset half-applied and the caller reporting that the session
            # "could not be moved aside", which is the opposite of what
            # happened. Restoring returns the bridge to the state every
            # refusal above leaves it in; where even that fails, the operator
            # is told the one thing they need, which is the path the only copy
            # of their credential is now at.
            if destination is not None:
                try:
                    os.rename(destination, self._session_dir)
                except OSError as restore_error:
                    raise SessionResetIncomplete(
                        f"the old WhatsApp session was moved to {destination} "
                        "and neither a new session directory nor a restore "
                        f"could be made: {type(restore_error).__name__}",
                        moved_to=destination,
                    ) from restore_error
            raise

        self._clear_fatal_latch()
        logger.warning(
            "whatsapp.baileys.session_reset moved_to=%s: the sidecar will "
            "offer a new pairing code",
            destination if destination is not None else "nothing to move",
        )
        return destination

    def _clear_fatal_latch(self) -> None:
        """Forget a permanent fault, so a supervisor can be started again.

        **Before any supervisor is recreated**, or the new loop reads the latch
        at its own first statement and returns without spawning. Its own method
        because `repair_session` reaches it on a path that moves nothing: a
        window orphaned by a scheduler restart has already emptied the session
        directory, and archiving an empty one a second time would accumulate
        directories nothing sweeps while telling the operator two of them
        matter.
        """
        self._status.ready = False
        self._status.fatal_reason = None
        self._status.fatal_is_permanent = False
        self._status.fatal_run_unrecorded = False
        self._permanent_fatal.clear()

    def _session_dir_holds_a_session(self) -> bool:
        """Whether there is anything at the session directory worth archiving.

        **Fails toward archiving.** A directory this cannot read answers True,
        because the cost of a needless archive is a directory an operator
        deletes and the cost of a wrong `False` is a credential overwritten in
        place.
        """
        try:
            with os.scandir(self._session_dir) as entries:
                for _ in entries:
                    return True
            return False
        except FileNotFoundError:
            return False
        except OSError:
            return True

    def resume(self) -> bool:
        """Start a supervisor a permanent fatal ended. `False` where there is
        none to start.

        Both permanent-fatal arms in `_supervise` `return`, and clearing the
        latch does not resurrect a coroutine that has already ended — so on the
        spawned shape a cleared latch with no fresh supervisor is a bridge that
        reports itself healthy and will never run a sidecar again. That
        distinction is the difference between a working re-pair and a green
        test over a dead deployment, which is why the test for this asserts a
        spawn rather than a flag.

        Answers `False` on the `sidecar_argv=()` shape, where there is no
        supervisor to resume because the sidecar is its own unit and its
        `Restart=always` has already scheduled the start — which is the shape
        the web flow runs on, and the reason this method matters to the CLI
        rather than to that surface.
        """
        if not self._sidecar_argv:
            return False
        if self._stopping:
            return False
        supervisor = self._supervisor
        if supervisor is not None and not supervisor.done():
            return False
        self._supervisor = asyncio.create_task(self._supervise())
        return True

    # -- the pairing sequence -----------------------------------------------

    async def repair_session(
        self, requested_by: str, *, force: bool = False,
    ) -> PairingResult:
        """Re-pair from a running bridge, and open a window for the code.

        **The frame is the evidence.** There is no way to ask whether anything
        holds the session directory — that is what an advisory lock would buy
        and this deployment has none — so the sequence *causes* the answer
        instead of observing it: it waits for a connected sidecar, writes
        `shutdown` to that peer, and moves the directory only after the link
        drop its own frame produced. A drop it merely observed is not evidence,
        because `index.js` starts the Baileys session without waiting for the
        daemon link and retries the link on its own timer — so a live sidecar
        holds the directory for a window at every boot while `connected` reads
        false. Renaming there leaves the survivor writing the old dead
        credential into the freshly created directory: no code, an orphaned
        archive, and a window that waits out its TTL for a QR that cannot come.

        **`force` selects no mechanism.** The frame is sent either way; it means
        "I accept disconnecting a session that is working", and without it a
        bridge with no latched permanent fault is refused. The default is the
        safe one, so a caller who forgets the argument cannot disconnect a live
        link.

        **On the spawned shape it delegates to `reset_session` outright**, and
        that is the one place the shape matters. `_supervise` returns its
        respawn delay to `RESPAWN_BASE_SECONDS` for any child that ran a
        while, so the sidecar this sequence asks to exit is back about a second
        later — into the directory the rename is about to move, with the drop to
        observe, schedule and act on inside that second. `reset_session`
        already settles with a supervisor rather than with a frame, by awaiting
        the task and refusing if it outlived the wait, and reproducing that
        reasoning here would be the second copy `_move_session_aside` exists to
        avoid. The cost is that the forced case is unavailable there, which is
        the developer checkout `istota whatsapp pair` already owns, and it is
        said in the message rather than left as a silent refusal.

        Never raises: every refusal is a `PairingResult` carrying a code and
        the prose for it, because this runs from a scheduler tick and from a
        web route and neither can act on an exception.
        """
        # Single-flight, and it is not the same question as "is a window open":
        # the window opens at the last step, so between here and there the
        # whole destructive half would otherwise be re-entrant. Both checks are
        # synchronous and both are before the first await.
        if self._repairing:
            return PairingResult(
                False,
                PAIRING_ALREADY,
                message="a WhatsApp re-pair is already in progress",
            )
        window = self._pairing_window
        if window is not None:
            return PairingResult(
                False,
                PAIRING_ALREADY,
                window_id=window.window_id,
                message=(
                    "a WhatsApp pairing window is already open. Cancel it or "
                    "wait for it to expire."
                ),
            )
        self._repairing = True
        try:
            return await self._repair(str(requested_by), force=force)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the contract is never-raises
            # No `exc_info`: this path can be reached with a pairing code live
            # in a frame, and the module's rule is that no traceback carrying
            # one is printed.
            logger.warning(
                "whatsapp.pairing.repair_failed reason=%s", type(exc).__name__,
            )
            return PairingResult(
                False,
                PAIRING_FAILED,
                message=(
                    "the WhatsApp re-pair could not be completed "
                    f"({type(exc).__name__}). Check `doctor`'s "
                    "`whatsapp.baileys_session` for where the session "
                    "directory stands."
                ),
            )
        finally:
            self._repairing = False

    async def _repair(self, requested_by: str, *, force: bool) -> PairingResult:
        """The sequence, with the cheap refusals first.

        The order is the design: nothing that can refuse on what it already
        knows may run after the step that spends a sidecar restart.
        """
        if self._stopping:
            return PairingResult(
                False, PAIRING_STOPPING, message="the bridge is stopping",
            )

        # 1. Validate before anything is destroyed. `ensure_session_dir`
        #    refuses a directory owned by another uid and a non-directory at
        #    the name, and this is the moment to hear that — after the sidecar
        #    is down it is a restart spent on a sequence that was never going
        #    to finish.
        try:
            ensure_session_dir(self._session_dir)
        except Exception as exc:  # noqa: BLE001 — reported, never raised on
            logger.warning(
                "whatsapp.pairing.session_dir_unusable reason=%s",
                type(exc).__name__,
            )
            return PairingResult(
                False,
                PAIRING_SESSION_DIR_UNUSABLE,
                message=(
                    "the WhatsApp session directory could not be opened as "
                    "this process's own private directory "
                    f"({type(exc).__name__}). Nothing was stopped and nothing "
                    "was moved."
                ),
            )

        # 2. The cooldown, which stands in for `reset_session`'s
        #    once-per-bridge flag on a bridge that lives as long as the daemon.
        remaining = self._cooldown_remaining()
        if remaining > 0:
            minutes = max(1, int(remaining // 60) + (1 if remaining % 60 else 0))
            return PairingResult(
                False,
                PAIRING_COOLDOWN,
                message=(
                    "a WhatsApp re-pair has already spent a sidecar restart "
                    f"recently. Wait about {minutes} minute"
                    f"{'' if minutes == 1 else 's'} before asking again — "
                    "repeating it is churn against a link WhatsApp watches."
                ),
            )

        # 3. The confirmation gate. Not a mechanism switch: the frame is sent
        #    either way, and this asks whether the caller accepts that it
        #    disconnects something that works.
        if not self._status.fatal_is_permanent and not force:
            return PairingResult(
                False,
                PAIRING_SESSION_LIVE,
                message=(
                    "the WhatsApp session has reported no permanent fault, so "
                    "re-pairing it would disconnect a link that may be "
                    "working or about to come back on its own. Confirm the "
                    "disconnect to proceed."
                ),
            )

        if self._sidecar_argv:
            return await self._repair_by_reset(requested_by)
        return await self._repair_by_frame(requested_by)

    async def _repair_by_reset(self, requested_by: str) -> PairingResult:
        """The spawned shape: settle with the supervisor, not with the frame.

        Reached only with `sidecar_argv` non-empty. `force` is not a parameter
        because the gate above has already answered it: a session with no
        permanent fault only arrives here when the caller forced it, and
        `reset_session` requires that fault, so this is where that combination
        is named.
        """
        if not self._status.fatal_is_permanent:
            return PairingResult(
                False,
                PAIRING_SHAPE_UNSUPPORTED,
                message=(
                    "this process runs the WhatsApp sidecar itself, and on "
                    "that shape a re-pair settles with the supervisor rather "
                    "than with a shutdown frame — which only moves a session "
                    "that has reported a permanent fault. To re-pair a "
                    "working session here, stop this process and run `istota "
                    "whatsapp pair --reset`."
                ),
            )
        try:
            moved = await self.reset_session()
        except SessionResetIncomplete as exc:
            logger.error("whatsapp.pairing.reset_incomplete moved_to=%s", exc.moved_to)
            return PairingResult(
                False,
                PAIRING_RESET_INCOMPLETE,
                message=str(exc),
                moved_to=exc.moved_to,
                restart_spent=True,
            )
        except SessionResetRefused as exc:
            return PairingResult(
                False, PAIRING_RESET_REFUSED, message=str(exc),
            )
        # A reset that got as far as returning has stopped and respawned the
        # child, so the restart this cooldown rations is spent here just as it
        # is at the frame on the other shape.
        self._last_reset_at = asyncio.get_running_loop().time()
        return await self._open_window_after(
            requested_by, moved, restart_spent=True,
        )

    async def _repair_by_frame(self, requested_by: str) -> PairingResult:
        """The external-unit shape: cause the drop, then move.

        Two waits, each with its own bound and its own meaning. The first is
        for a sidecar to *appear*, because the evidence needs a peer to be
        caused on; timing out there is the ISSUE-497 residual — a failing `npm
        ci` in the update cron leaves the unit stopped with the reason only in
        the update log — and it is the one abort in this flow that really costs
        nothing. The second is for the link to drop after the frame, and past
        its bound the directory is left alone: moving it out from under a live
        writer is the corruption every guard in this module exists to prevent.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._sidecar_return_timeout
        while True:
            if self._stopping:
                return PairingResult(
                    False, PAIRING_STOPPING, message="the bridge is stopping",
                )
            writer = self._writer
            if (
                writer is not None
                and self._status.connected
                and not writer.is_closing()
            ):
                # **Clear the latch, then re-read the writer, with nothing
                # awaited in between.** The clear is what makes the wait below
                # mean "gone *since* the frame" rather than "gone at some
                # point"; the re-read is what catches a link that dropped on
                # its own during the clear, which must send this back to
                # waiting rather than into a rename. From the re-read to
                # `writer.write` there is no suspension point, so the loop
                # cannot run `_close_link` in between.
                self._link_dropped.clear()
                if self._writer is writer and not writer.is_closing():
                    if await self._write_shutdown_frame(writer):
                        break
                    # The write itself failed, so nothing left this process and
                    # no restart was spent. A clean abort, and the only one
                    # here that is.
                    logger.warning("whatsapp.pairing.frame_unsent")
                    return PairingResult(
                        False,
                        PAIRING_FRAME_UNSENT,
                        message=(
                            "the shutdown frame could not be written to the "
                            "WhatsApp sidecar, so nothing was stopped and "
                            "nothing was moved. Try again."
                        ),
                    )
            if loop.time() >= deadline:
                logger.warning("whatsapp.pairing.sidecar_absent_at_start")
                return PairingResult(
                    False,
                    PAIRING_SIDECAR_ABSENT,
                    message=(
                        "no WhatsApp sidecar connected in "
                        f"{self._sidecar_return_timeout:.0f}s, so there was "
                        "nothing to ask to stop. Check its own unit or compose "
                        "service and the deploy update log — a failed "
                        "dependency install can leave it stopped. Nothing was "
                        "moved and no restart was spent."
                    ),
                )
            await asyncio.sleep(_SIDECAR_POLL_INTERVAL)

        # **The restart is spent at the write, not at success.** A sidecar
        # slow past the bound below still exits, so an abort there has cost a
        # reconnect on a re-pair that did not happen — which is what the
        # cooldown rations and what the confirmation copy promises either way.
        self._last_reset_at = loop.time()
        try:
            await asyncio.wait_for(
                self._link_dropped.wait(), timeout=self._sidecar_stop_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("whatsapp.pairing.stop_timeout")
            return PairingResult(
                False,
                PAIRING_STOP_TIMEOUT,
                message=(
                    "the WhatsApp sidecar's link did not drop within "
                    f"{self._sidecar_stop_timeout:.0f}s of being asked to "
                    "stop, so the session directory was left alone — moving "
                    "it out from under a live writer is what corrupts an auth "
                    "state. The shutdown frame has already gone, so the "
                    "sidecar may still be stopping and its restart is spent."
                ),
                restart_spent=True,
            )

        # **Nothing awaits from here to the end of the move.** On the compose
        # shape the supervisor's restart backoff starts at 100ms, and the two
        # syscalls below are four orders of magnitude inside that — but only if
        # the loop is not given a chance to adopt a respawned sidecar in
        # between. `_move_session_aside` is synchronous for this reason.
        if self._stopping:
            return PairingResult(
                False,
                PAIRING_STOPPING,
                message=(
                    "the bridge began stopping while the WhatsApp sidecar was "
                    "shutting down, so the session directory was left alone. "
                    "Its restart is spent."
                ),
                restart_spent=True,
            )
        # **The second line of defence, and it is the one that keeps the
        # corruption out of reach.** Measured: with both latch clears removed
        # the wait is satisfied by a stale drop, and it is this check that
        # still refuses rather than renaming out from under the sidecar that
        # is connected right now. Removing it as well is what finally lets the
        # directory move with a live writer holding it.
        if self._writer is not None:
            logger.warning("whatsapp.pairing.sidecar_returned_before_move")
            return PairingResult(
                False,
                PAIRING_SIDECAR_RETURNED,
                message=(
                    "a WhatsApp sidecar reconnected before the session "
                    "directory could be moved, so it was left alone rather "
                    "than moved out from under a live writer. Its restart is "
                    "spent; try again."
                ),
                restart_spent=True,
            )

        moved: Path | None = None
        if self._session_dir_holds_a_session():
            try:
                moved = self._move_session_aside()
            except SessionResetIncomplete as exc:
                logger.error(
                    "whatsapp.pairing.reset_incomplete moved_to=%s", exc.moved_to,
                )
                return PairingResult(
                    False,
                    PAIRING_RESET_INCOMPLETE,
                    message=str(exc),
                    moved_to=exc.moved_to,
                    restart_spent=True,
                )
            except Exception as exc:  # noqa: BLE001 — reported, never raised on
                logger.warning(
                    "whatsapp.pairing.move_aside_failed reason=%s",
                    type(exc).__name__,
                )
                return PairingResult(
                    False,
                    PAIRING_FAILED,
                    message=(
                        "the WhatsApp session directory could not be moved "
                        f"aside ({type(exc).__name__}); it is untouched and "
                        "the sidecar's restart is spent."
                    ),
                    restart_spent=True,
                )
        else:
            # **An empty directory is not archived a second time.** The routine
            # case is a window a process restart orphaned: the credential is
            # already at a `.old-` sibling, so the recovery is another window
            # against the directory that move emptied, not another archive of
            # nothing. The reference deployment restarts its units on every
            # commit, so this arm is ordinary rather than exceptional.
            self._clear_fatal_latch()
            logger.info(
                "whatsapp.pairing.nothing_to_move: the session directory is "
                "already empty, so only the fault latch was cleared",
            )
        # A no-op on this shape by construction — there is no supervisor to
        # resume when `sidecar_argv` is empty — and called anyway, so the two
        # entry points end the same way rather than one of them relying on a
        # property of its caller.
        self.resume()
        return await self._open_window_after(
            requested_by, moved, restart_spent=True,
        )

    async def _open_window_after(
        self, requested_by: str, moved: Path | None, *, restart_spent: bool,
    ) -> PairingResult:
        """Arm the relay, and say where the old session went either way.

        `open_pairing_window` can answer `None` — the bridge began stopping
        during its own two awaits — and by here a full-account credential has
        already moved. A result that dropped `moved_to` on that path would
        leave an operator with two directories and nothing saying which holds
        their session.
        """
        window = await self.open_pairing_window(
            requested_by, destructive=moved is not None,
        )
        if window is None:
            return PairingResult(
                False,
                PAIRING_NO_WINDOW,
                message=(
                    "the WhatsApp session was reset but no pairing window "
                    "could be opened, so no code will be relayed"
                    + (
                        f". The old session is at {moved}"
                        if moved is not None
                        else ""
                    )
                ),
                moved_to=moved,
                restart_spent=restart_spent,
            )
        return PairingResult(
            True,
            PAIRING_OK,
            window_id=window.window_id,
            message=(
                "waiting for the WhatsApp sidecar to restart and offer a "
                "pairing code"
                + (f". The old session is at {moved}" if moved is not None else "")
            ),
            moved_to=moved,
            restart_spent=restart_spent,
        )

    def _cooldown_remaining(self) -> float:
        last = self._last_reset_at
        if last is None:
            return 0.0
        elapsed = asyncio.get_running_loop().time() - last
        return max(0.0, self._reset_cooldown - elapsed)

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

        **On this shape the way out is `resume()`.** With an argv, ending the
        loop means no child, so nothing can send the `ready` that `_dispatch`
        clears the latch on — the clearing arm is unreachable from in here. The
        way back is to clear the latch and start this loop again, and both
        callers do exactly that after the dead session has been moved aside:
        `reset_session` (behind `istota whatsapp pair --reset`) and
        `repair_session`, which delegates to it on this shape. On the
        `sidecar_argv=()` shape there is no gap and nothing to resume: systemd
        restarts the unit, it reconnects, and its `ready` clears the latch —
        but its session directory is the unit's to hold, not this process's to
        move.
        """
        delay = RESPAWN_BASE_SECONDS
        while not self._stopping:
            if self._status.fatal_is_permanent:
                logger.error(
                    "whatsapp.baileys.not_respawning reason=%s: re-pair with "
                    "`istota whatsapp pair --reset`",
                    self._status.fatal_reason,
                )
                return
            started = asyncio.get_running_loop().time()
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *self._sidecar_argv,
                    env=self._child_env(),
                    # The sidecar creates the session files, so its umask is
                    # the only thing that decides their mode at birth. It sets
                    # the same one on itself, which is what covers the two
                    # shapes this spawn does not reach — the systemd unit and
                    # the compose service. Kept here because it binds the child
                    # from `execve` rather than from its first statement, and
                    # because nothing outside the program should have to trust
                    # the program for this. `harden_session_files` corrects a
                    # stray one afterwards and is the backstop, not either
                    # mechanism.
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
                    #
                    # Asked before it is reaped, which used to be academic and
                    # is not since ISSUE-498: a logged-out sidecar now waits
                    # before exiting, so `_reap_process` would spend its whole
                    # `SHUTDOWN_GRACE_SECONDS` and then SIGTERM a process that
                    # answers the frame in milliseconds. The frame's handler is
                    # not gated on the sidecar's own stopping flag, so it
                    # reaches one mid-wait.
                    await self._request_shutdown()
                    await self._reap_process()
                    self._process = None
                    logger.error(
                        "whatsapp.baileys.not_respawning reason=%s: re-pair "
                        "with `istota whatsapp pair --reset`",
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
        """Ask whichever sidecar is connected to stop. Never raises."""
        await self._write_shutdown_frame(self._writer)

    async def _write_shutdown_frame(self, writer) -> bool:
        """Ask **this** peer to stop, and say whether the frame was written.

        Split out of `_request_shutdown` because `repair_session` needs both
        halves that method does not give it. It has to write to the writer it
        already established was live rather than to whatever `self._writer`
        holds a statement later — the drop it then observes has to belong to a
        process it named — and it has to know whether the frame reached
        `writer.write`, because that is what decides whether a restart was
        spent and therefore whether an abort is a clean no-op.

        Never raises, and the mark is set between the write and the drain for
        `_send`'s reason: past `write` the bytes may be in the kernel buffer
        whatever `drain` then says. `_request_shutdown`'s own behaviour is
        unchanged — it still suppresses everything and returns `None`, which
        `stop()` and the supervisor's permanent-fatal arm both depend on.
        """
        if writer is None:
            return False
        written = False
        with contextlib.suppress(Exception):
            writer.write(proto.encode(proto.MSG_SHUTDOWN))
            written = True
            await asyncio.wait_for(writer.drain(), timeout=1.0)
        return written

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
            # Cleared where the writer is adopted rather than where the
            # connection is accepted, so the latch tracks exactly the link
            # `_close_link` will set it for.
            self._link_dropped.clear()
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
        # Set unconditionally, including where there was no writer: the
        # question `repair_session` asks of this latch is "has the link gone
        # since I cleared it", and it clears it while holding a live writer.
        self._link_dropped.set()
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
            self._status.fatal_run_unrecorded = False
            self._permanent_fatal.clear()
            # A scan is what closes a pairing window, and it closes it from
            # this side rather than from a reader's — so a code scanned while
            # no browser is watching still pairs. The window is dropped
            # synchronously and the credential unlinked on a task, because
            # `_dispatch` runs on the read loop and may not await.
            #
            # **A `ready` frame is not evidence that a code was scanned**, and
            # the terminal state must not claim it was: the sidecar
            # re-announces its verdict whenever the daemon link reconnects, so
            # a link blip inside a window produces one with no pairing behind
            # it. `qr_seq` is the evidence — it counts codes this window
            # actually relayed — and the distinction matters because the
            # durable row records this state as the outcome of somebody's
            # re-pair request. The window closes either way; there is nothing
            # left to relay to a session that is up.
            window = self._pairing_window
            if window is not None:
                paired = window.qr_seq > 0
                if self._end_pairing_window(
                    window,
                    (
                        pairing_relay.STATE_PAIRED
                        if paired
                        else pairing_relay.STATE_FAILED
                    ),
                    (
                        "the session was paired"
                        if paired
                        else "the session reported itself open without having "
                        "offered a pairing code, which is what a reconnect of "
                        "a working session looks like; nothing was re-paired"
                    ),
                ):
                    self._spawn_relay_job(self._clear_relay())
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
        linked as a device. The callback is `istota whatsapp pair`'s
        own-sidecar mode; with neither an armed window nor a callback the
        daemon is not pairing and the frame is noted without its contents.

        **The window branch is an addition at the top, not a rewrite.** With no
        open window — including one already past its deadline that the watchdog
        has not yet closed — this behaves exactly as it did before there was a
        window at all, which is what every Baileys deployment in existence
        runs. An armed window relays and returns rather than also calling the
        callback: one code, one channel.
        """
        value = payload.get("qr")
        if not isinstance(value, str) or not value:
            self._status.malformed_lines += 1
            return
        window = self._pairing_window
        if window is not None and asyncio.get_running_loop().time() < window.expires_at:
            # Stamped on the loop, in order, so the sequence a queued write
            # checks against is the one this rotation was given. A late
            # sidecar's first code also lifts `sidecar_absent` — the window
            # stayed open precisely so it could.
            window.qr_seq += 1
            window.state = pairing_relay.STATE_AWAITING_SCAN
            window.message = ""
            self._spawn_relay_job(
                self._publish_relay_qr(window, window.qr_seq, value),
            )
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
        # Overwritten per frame, exactly as `fatal_reason` is, so the two
        # always describe the same frame. The sidecar reports this on a
        # *second* `fatal` rather than on the first — the first one's position
        # is load-bearing, since it has to leave before any filesystem work —
        # so the pair arrives as False then True, and a `ready` clears both.
        self._status.fatal_run_unrecorded = payload.get("run_unrecorded") is True
        self._status.ready = False
        # **Only `ready` clears a permanent latch.** Assigning `permanent`
        # here unconditionally meant a *transient* fatal arriving after a
        # permanent one re-opened the send gate against a session that is
        # gone, left `_permanent_fatal` set so the supervisor still refused to
        # respawn, and re-armed the once-per-outage alert. Nothing the shipped
        # sidecar sends reaches that today — both its fatals are permanent —
        # but the branch is written as though a transient one exists, and any
        # other sidecar has one.
        if permanent:
            self._status.fatal_is_permanent = True
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
        an open pairing window, no sidecar connected, a session in a fatal
        state, a request that would not encode — is provably a message that
        never entered the socket, and settles `failed`. From `write` onwards the bytes may be in the kernel
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
        # **Ahead of the fatal latch, and not optional.** `repair_session`
        # clears that latch — it has to, or `resume()`'s fresh supervisor reads
        # it at its first statement and returns without spawning — so for the
        # whole window this bridge is attached to a sidecar that is restarting
        # into an unpaired session. With only the latch below, `writer.write`
        # succeeds, the mark is set, and the answer either never comes
        # (`send_timeout`) or comes back as a link failure: both settle
        # **`unknown`**, the one state `.claude/rules/whatsapp.md` says an
        # operator can never resolve. `logical_key` is UNIQUE and nothing
        # deletes from `sent_whatsapp`, so a task result, a confirmation prompt
        # or an admin alert caught inside a window would be unsendable for
        # good. Before the latch clear that same send settled `failed`,
        # honestly, and this is what keeps it doing so.
        if self._pairing_window is not None:
            return proto.local_failure(proto.REASON_PAIRING, definite=True)
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
    "PAIRING_ALREADY",
    "PAIRING_COOLDOWN",
    "PAIRING_FAILED",
    "PAIRING_FRAME_UNSENT",
    "PAIRING_NO_WINDOW",
    "PAIRING_OK",
    "PAIRING_RESET_INCOMPLETE",
    "PAIRING_RESET_REFUSED",
    "PAIRING_SESSION_DIR_UNUSABLE",
    "PAIRING_SESSION_LIVE",
    "PAIRING_SHAPE_UNSUPPORTED",
    "PAIRING_SIDECAR_ABSENT",
    "PAIRING_SIDECAR_RETURNED",
    "PAIRING_STOPPING",
    "PAIRING_STOP_TIMEOUT",
    "PAIRING_WINDOW_SECONDS",
    "PairingOutcome",
    "PairingResult",
    "PairingWindow",
    "RESET_COOLDOWN",
    "SEND_TIMEOUT_SECONDS",
    "SESSION_DIR_NAME",
    "SIDECAR_STOP_TIMEOUT",
    "SOCKET_NAME",
    "SessionResetIncomplete",
    "SessionResetRefused",
    "active_bridge",
    "clear_active_bridge",
    "default_session_dir",
    "default_socket_path",
    "ensure_session_dir",
    "harden_session_files",
    "in_tree_sidecar_argv",
    "read_status",
    "set_active_bridge",
    "shipped_library_version",
    "survey_session_files",
]
