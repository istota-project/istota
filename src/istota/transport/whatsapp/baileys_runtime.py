"""Who starts the Baileys bridge, drives its pairing, and reports its faults.

The bridge is a mechanism with no opinion about lifecycle: it never raises,
never logs a payload, never opens a database. That leaves three jobs nobody
owned, and all three are here.

**Starting it, on the right loop.** `deliver_whatsapp` runs on the process
global `AsyncRuntime` loop — every WhatsApp send reaches it through `run_coro`
— and the bridge's asyncio primitives bind to whichever loop first awaits
them. A bridge started on uvicorn's loop and sent to from the runtime's is a
cross-loop await: at best a `RuntimeError`, at worst a future nothing ever
resolves inside the claim-to-settle region. So the bridge is started **on the
runtime loop**, from the scheduler's boot, exactly where the Talk signaling
supervisor is started and for a stricter reason.

That is a **deviation from the spec's Affected-files line**, which names
`serve.py` ("start the Baileys sidecar when selected") beside the webhook
mount. Two things make the mount point the wrong one. The mount decision runs
in the *web* process, and on the canonical Ansible deployment the web app and
the scheduler are separate units — so a sidecar started there would be running
in the one process that never sends. And `istota serve` runs the scheduler in
a thread of its own inside the same process, so starting it from the scheduler
gives the combined shape a bridge anyway, through the loop the sends actually
use. The webhook-mount predicate is still split per provider, in
`config.whatsapp_webhooks_enabled`, which is where that half already lives.

**Driving a re-pair from a durable request row.** The bridge owns the pairing
window and the relay file; it does not own the trigger, because the trigger is
written by another process. `poll_pairing_request` is the other end of that
channel, called from the scheduler's `whatsapp-pairing` gate in whichever
process holds the bridge: it expires stale rows, claims a fresh request and
hands it to `BaileysBridge.repair_session` on the runtime loop. The expiry is
load-bearing rather than housekeeping — see that function's docstring.

**Telling somebody the device was unlinked.** WhatsApp drops a linked device
after long inactivity or a protocol change; the sidecar reports `fatal`, the
bridge latches it, and from that moment every send is refused — correctly, and
silently. The spec's edge case asks for a task alert *and* a doctor failure;
this is the first half. It is deployment-wide and its remedy is an operator
action, so it is addressed the way the billing circuit's alert is: to the
admins, once per transition, pushed on every route but WhatsApp's own.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import baileys_bridge, pairing_relay

if TYPE_CHECKING:
    from ...config import Config

logger = logging.getLogger(__name__)

_ALERT_TITLE = "WhatsApp is unlinked — the paired session needs re-pairing"

#: Namespaced under the surface like every other WhatsApp dedup key, and fixed
#: rather than carrying the reason: the row is about the session being gone,
#: which is one condition however the sidecar spelled it, and a reason-keyed
#: axis would be one durable row and one push per spelling.
_ALERT_DEDUP_KEY = "whatsapp:baileys-unlinked"

#: The replaced-connection alerts (ISSUE-553). Keys and titles of their own,
#: since the condition is not an unlinked device: the device is linked and
#: another client is using it. Two keys rather than one, because the latch
#: row is usually still open when the sidecar gives up, and a write under an
#: open row's key bumps it without delivering.
_REPLACED_DEDUP_KEY = "whatsapp:baileys-replaced"
_REPLACED_GAVE_UP_DEDUP_KEY = "whatsapp:baileys-replaced-gave-up"
_REPLACED_TITLES = {
    1: "Another client is using this WhatsApp session",
    2: "WhatsApp stopped retrying: another client holds the session",
}

#: What an operator does once the sidecar has given up. Shared with doctor's
#: remedy for the same state, so the two cannot give different instructions.
#: The unlink comes first because a re-pair makes a new device and leaves the
#: copied credential valid: whoever holds it keeps a working session.
REPLACED_GIVE_UP_REMEDY = (
    "Re-pair from Admin, Connections. If you do not know what the other "
    "client is, first remove the old device under Linked Devices in WhatsApp "
    "on the phone: a re-pair does not revoke the copied credential, so "
    "whoever holds it can keep using the session."
)


def baileys_bridge_wanted(config: "Config") -> bool:
    """Whether this deployment's WhatsApp surface runs on a paired session.

    The same two questions `config.whatsapp_webhooks_enabled` asks of the
    other adapter, answered for this one — and deliberately *not* "are the
    credentials present", which for this provider is a directory on disk that
    the pairing flow creates. A deployment that has not paired yet still wants
    the bridge: the listener is what a sidecar dials into, and the refusal for
    an unpaired session belongs at the send, which is the ISSUE-058 rule this
    surface already follows for Meta's secrets.
    """
    return bool(config.whatsapp.enabled and config.whatsapp.provider == "baileys")


def start_baileys_bridge(config: "Config") -> bool:
    """Start the bridge on the runtime loop and publish it. Never raises.

    Returns whether a bridge is now running. `False` covers a deployment that
    does not want one and a start that failed, and the difference is in the
    log rather than in the value, because no caller can act on it differently:
    a scheduler whose WhatsApp bridge would not start still has to run.

    `run_coro` rather than `spawn_task`: `start()` returns as soon as the
    socket is listening and the supervisor is spawned, and the boot sequence
    wants to know it happened — an `EADDRINUSE` or a refused session directory
    is a condition an operator has to see at startup rather than as the first
    failed send.

    The bridge is published **after** `start()` returns, which is the ordering
    the adapter's refusal depends on: a send resolved through a bridge whose
    listener is not up would be written to nothing, where the adapter's answer
    with no bridge at all is a definite local failure and a clean ledger row.
    """
    if not baileys_bridge_wanted(config):
        return False
    from ...async_runtime import get_async_runtime, run_coro  # noqa: PLC0415

    argv = baileys_bridge.resolve_sidecar_argv(config)
    # The pairing keys are read here rather than inside the constructor so the
    # bridge keeps taking them as parameters — which is what lets a test drive
    # a five-second window without writing a config file. `pairing_relay_path`
    # is the exception and is resolved by `default_pairing_relay_path`, because
    # the poll's no-bridge relay clear has no bridge to ask.
    bridge = baileys_bridge.BaileysBridge(
        config,
        sidecar_argv=argv,
        pairing_window_seconds=baileys_bridge.configured_pairing_window_seconds(
            config
        ),
        restart_interval_seconds=config.whatsapp.baileys.restart_interval_seconds,
        on_fatal=lambda reason: _announce_unlink(config, reason),
        on_replaced=lambda level, count: _announce_replaced(config, level, count),
    )
    try:
        run_coro(bridge.start())
    except Exception:
        logger.error(
            "whatsapp.baileys.start_failed: the WhatsApp surface is set to "
            "baileys and its bridge did not start; every send will be "
            "recorded `unconfigured` until this resolves",
            exc_info=True,
        )
        return False
    baileys_bridge.set_active_bridge(bridge)
    get_async_runtime().add_cleanup_hook(lambda: _stop(bridge))
    logger.info(
        "whatsapp.baileys.started sidecar=%s",
        "spawned" if argv else "external",
    )
    return True


async def _stop(bridge) -> None:
    """The runtime's cleanup hook. Unpublish first, then stop.

    In that order: once the bridge is stopping, a send resolved through it
    would sit on a socket being closed and time out into `unknown`, where the
    adapter's answer with nothing published is a definite `failed`.
    """
    baileys_bridge.clear_active_bridge()
    await bridge.stop()


async def _announce_unlink(config: "Config", reason: str) -> None:
    """Raise the durable rows behind an unlinked device, and push them.

    Runs as its own task on the read loop, so every database step goes to a
    worker thread — the loop thread taking the WAL write lock synchronously is
    the stall `.claude/rules/transport.md` records at several other seams.

    The write and the push are **two transactions**, per
    `.claude/rules/notifications.md`: a producer that delivers from inside its
    own open write transaction opens a second connection against the lock it
    is holding, waits out the busy timeout and raises into a never-raises
    contract.

    Never raises. The caller is `_call_back`, which already swallows — but it
    swallows without `exc_info` because a `qr` frame is a credential, and this
    failure has a cause an operator needs.
    """
    try:
        raised = await asyncio.to_thread(_write_unlink_alerts, config, reason)
        for item in raised:
            await asyncio.to_thread(_push_baileys_alert, config, item)
    except Exception:
        logger.warning("whatsapp.baileys.unlink_alert_failed", exc_info=True)


async def _announce_replaced(config: "Config", level: int, count: int) -> None:
    """Raise and push the replaced-connection alert for `level`.

    The sidecar asks for this once per outage per level and persists that it
    asked, so this keeps no once-flag of its own. Never raises, for
    `_announce_unlink`'s reason.
    """
    try:
        dedup_key = (
            _REPLACED_GAVE_UP_DEDUP_KEY if level == 2 else _REPLACED_DEDUP_KEY
        )
        raised = await asyncio.to_thread(
            _write_baileys_alerts, config,
            dedup_key, _REPLACED_TITLES.get(level, _REPLACED_TITLES[1]),
            _replaced_alert_body(level, count), "session_replaced",
        )
        for item in raised:
            await asyncio.to_thread(_push_baileys_alert, config, item)
    except Exception:
        logger.warning("whatsapp.baileys.replaced_alert_failed", exc_info=True)


def _replaced_alert_body(level: int, count: int) -> str:
    """What an operator is told when another client holds the session."""
    times = max(int(count), 0)
    if level == 2:
        return (
            f"Another client kept replacing this deployment's WhatsApp "
            f"connection ({times} times) and was still there on every retry, "
            "so the sidecar has stopped trying and every WhatsApp send is "
            "refused. " + REPLACED_GIVE_UP_REMEDY
        )
    return (
        f"WhatsApp closed this deployment's connection {times} times in a few "
        "minutes because another client logged in with the same session, "
        "often a copy of the session directory running on another host. The "
        "sidecar has stopped reconnecting and every WhatsApp send is refused. "
        "It will try again after 15 minutes, then after an hour twice more; "
        "stop the other client and the next try succeeds. If the other client "
        "is still there after the last try, a second notice says how to "
        "re-pair."
    )


def _unlink_readers(config: "Config") -> list[str]:
    """Who is told. Admins, or everybody where that means everybody.

    `load_admin_users()` returning an empty set is `Config.is_admin`'s "every
    user is an admin", which is the single-user install's ordinary state — so
    an empty set has to fan out rather than reach nobody. This is the fan-out
    `_write_billing_alerts` declines to make, and the difference is that that
    one has a recipient to fall back on: a charged message names the user who
    received it, and a dead session names nobody at all.
    """
    from ...config import load_admin_users  # noqa: PLC0415

    try:
        admins = load_admin_users()
    except Exception:
        logger.warning("whatsapp.baileys.admins_unreadable", exc_info=True)
        admins = set()
    if admins:
        # The intersection, falling back to **every configured user** rather
        # than to the raw admin list: an admins file naming somebody who is
        # not in `config.users` — a stale entry, a renamed account — would
        # otherwise write a row and attempt a push for a user with no
        # configuration at all. Same answer as the empty-admins arm below, and
        # for the same reason.
        return sorted(admins & set(config.users)) or sorted(config.users)
    return sorted(config.users)


def _alert_body(reason: str) -> str:
    """What the operator is told, with the sidecar's own words left out.

    `reason` is a small vocabulary this side defines (`logged_out`,
    `unpaired`, `bad_session`, `credential_unreadable`) plus whatever else a
    sidecar sends, so it is
    reported as a bounded label and never as prose: Baileys' error text is one
    of the places a JID or a message body turns up, and this string reaches a
    notification panel and every alert route the user has configured.
    `task_alert._slug` is that bound, borrowed rather than re-spelled — it is
    the same rule this source already applies to every other caller-supplied
    key component, for the same reason.
    """
    from ...notification_resolvers.task_alert import _slug  # noqa: PLC0415

    if reason == "credential_unreadable":
        # The device is still linked on WhatsApp's side; it is the local copy
        # of the credential that cannot be read (ISSUE-552). A permission or
        # ownership error reads the same from here, so the check comes first.
        return (
            "The saved WhatsApp credential cannot be read, so every WhatsApp "
            "send is refused and nothing is opened until it is repaired. "
            "Check `sidecar.log` in the session directory: if the file's "
            "owner or mode is wrong, fix that and restart the sidecar. "
            "Otherwise stop the istota daemon and any sidecar running as a "
            "unit of its own, run `istota whatsapp pair --reset`, scan the "
            "code, then start them again, and remove the old entry from "
            "WhatsApp's Linked Devices screen. The unreadable session is kept "
            "as a timestamped sibling directory, not deleted."
        )
    return (
        f"The WhatsApp device link ended ({_slug(reason, fallback='unknown')}), so every "
        "WhatsApp send is refused until the session is paired again. Stop the "
        "istota daemon and any sidecar running as a unit of its own, run "
        "`istota whatsapp pair --reset`, scan the code from WhatsApp's Linked "
        "Devices screen, then start them again. The old session is kept as a "
        "timestamped sibling directory, not deleted."
    )


def _write_unlink_alerts(config: "Config", reason: str) -> tuple[object, ...]:
    return _write_baileys_alerts(
        config, _ALERT_DEDUP_KEY, _ALERT_TITLE, _alert_body(reason),
        "session_unlinked",
    )


def _write_baileys_alerts(
    config: "Config", dedup_key: str, title: str, body: str, status: str,
) -> tuple[object, ...]:
    from ... import db  # noqa: PLC0415
    from ...notification_resolvers import task_alert  # noqa: PLC0415

    raised = []
    with db.get_db(config.db_path) as conn:
        for reader in _unlink_readers(config):
            raised.append(task_alert.write(
                conn, reader,
                dedup_key=dedup_key,
                title=title,
                body=body,
                params={"task_id": None, "status": status},
            ))
    return tuple(item for item in raised if item is not None)


def _push_baileys_alert(config: "Config", raised) -> None:
    """Push one raised row off the WhatsApp surface, for both alert producers."""
    from .._alerts import push_off_surface  # noqa: PLC0415

    push_off_surface(
        config, raised,
        exclude_surface="whatsapp",
        reference_prefix="istota:whatsapp:baileys",
    )


# --- The pairing request channel ------------------------------------------
#
# The web process (or the CLI's attach mode) writes a request row; this is the
# other end, polled from the scheduler's `whatsapp-pairing` interval gate in
# whichever process holds the bridge. Three jobs, in this order: expire stale
# rows, mirror a live window's state, claim a fresh request and hand it to the
# bridge.


#: A bounded lock wait, because this runs on the scheduler's **dispatch**
#: thread. The default 30s would stall the loop behind any other writer and
#: trip the stall watchdog; a lost tick costs nothing here, since the next one
#: is a `poll_interval` away and every deadline in this flow has minutes of
#: slack. Same reasoning as the dispatch loop's own read scans.
_POLL_BUSY_TIMEOUT_MS = 2000

_PAIRING_ALERT_TITLES = {
    "paired": "WhatsApp is paired again",
    "expired": "The WhatsApp pairing window expired with no code scanned",
    "failed": "The WhatsApp re-pair did not complete",
}

_PAIRING_ALERT_SEVERITY = {
    "paired": "success",
    "expired": "warning",
    "failed": "warning",
}


@dataclass(frozen=True)
class _PairingClosure:
    """A request row this poll moved to a terminal state, for the announcement.

    Carried out of the write transaction rather than acted on inside it: a
    notification producer that delivers from inside its own open write
    transaction opens a second connection against the lock it is holding and
    waits out the busy timeout, which is `.claude/rules/notifications.md`'s
    two-call rule. The relay unlink is out here for the plainer reason that it
    is disk I/O under a held write lock.
    """

    window_id: str
    state: str
    message: str
    requested_by: str
    #: Whether the row was in a window-implying state, so a relay file for it
    #: may still be on disk. A `requested` or `servicing` row never had one.
    unlink_relay: bool


@dataclass(frozen=True)
class _PairingAdoption:
    """A window this process should re-arm from the row (ISSUE-504).

    The third outcome of `_expire_stale_pairing`, beside a closure and nothing.
    Decided inside the `BEGIN IMMEDIATE` and acted on after it, the way the
    claim already is, because `adopt_pairing_window` is a coroutine on the
    runtime loop and the transaction runs on the dispatch thread.

    **Nothing here is written back.** Every field is copied off the row, which
    is what makes adoption a read: the row is already the durable record of the
    window, so there is nothing to clobber and no column to add.
    """

    window_id: str
    requested_by: str
    #: Epoch seconds. The row stores `sql_datetime_now`'s text so the deadline
    #: arm can compare it in SQL; the bridge needs the absolute instant.
    expires_at_wall: float
    #: Off the row's message, which `_window_open_message` writes as the
    #: archive path and nothing else — so a non-empty one is exactly the
    #: durable record that this re-pair moved a credential aside.
    destructive: bool
    #: Whether a code had already reached the relay, which the row records by
    #: being in `awaiting_scan`. The bridge reads it as the evidence that a
    #: `ready` frame is a real pairing rather than a reconnect, so without it a
    #: scan the previous process relayed closes the re-adopted window `failed`.
    codes_relayed: bool


def poll_pairing_request(config: "Config") -> None:
    """Service the durable pairing request row. Never raises.

    Called from the scheduler's `whatsapp-pairing` gate on **every tick**, so
    the common case — nothing pending, which is every tick on every Baileys
    deployment for ever — must cost one indexed read and nothing else. That is
    why the gate is not `background`: a thread per tick is about 17,000 a day
    to answer a primary-key lookup.

    **It expires rows before it services them, and that is not housekeeping.**
    The window and its watchdog live in bridge memory; the request row is in
    the database. A scheduler restart mid-window leaves the two disagreeing in
    the worst direction — the new bridge has no window so `_handle_qr`
    discards codes again, nothing will ever close the row, the relay keeps
    rendering a frozen code, and `request_whatsapp_pairing` is guarded on
    there being no open request, so **every later pairing request is refused
    for the life of the deployment.** That is the stuck-row class this repo
    already carries on `sent_sms` and `sent_whatsapp`, arriving on a surface
    whose whole purpose is recovery.

    Never raises, because a raise here takes the scheduler's dispatch loop.
    `exc_info` is safe on this path and wanted: no pairing payload reaches
    this module, so nothing a traceback frame here holds is a credential.
    """
    try:
        _poll_pairing_request(config)
    except sqlite3.OperationalError as exc:
        # A contended write lock, which `_POLL_BUSY_TIMEOUT_MS` turns into this
        # rather than a 30s block. A lost tick costs nothing — the next is a
        # poll interval away — so it is a skipped tick and not a fault, and at
        # `fixed_interval=0` a WARNING with a traceback would be one per tick
        # for as long as the other writer holds its transaction.
        logger.debug("whatsapp.pairing.poll_skipped reason=%s", exc)
    except Exception:
        logger.warning("whatsapp.pairing.poll_failed", exc_info=True)


def _poll_pairing_request(config: "Config") -> None:
    from ... import db  # noqa: PLC0415

    # Imported at function scope, which is this module's convention and also a
    # test constraint: a module-scope binding of `spawn_task` or
    # `active_bridge` would make `monkeypatch.setattr` on the defining module
    # inert while the tests still passed.
    from . import baileys_bridge  # noqa: PLC0415

    bridge = baileys_bridge.active_bridge()

    # The cheap read, outside any transaction. `BEGIN IMMEDIATE` takes the
    # write lock, and taking it on every tick for ever to discover there is
    # nothing to do is the cost the SMS delivery branch already measured and
    # refused. A SELECT on the singleton's primary key opens no transaction.
    with db.get_db(
        config.db_path, busy_timeout_ms=_POLL_BUSY_TIMEOUT_MS
    ) as conn:
        row = db.read_whatsapp_pairing(conn)
    # **An orphan is a window or a relay file the durable row no longer owns**,
    # and clearing one is the only route a cancel has on the split shape: the
    # web process holds no bridge on the Ansible deployment, so `DELETE` can
    # only stamp the row and leave both to whoever does. Run *before* the
    # terminal early return, because a fresh request written over a closed one
    # is non-terminal and would otherwise skip it entirely — see
    # `_pairing_row_owns_no_window`.
    _close_orphaned_pairing(config, bridge, row)
    if row is None or row["state"] in db.WHATSAPP_PAIRING_TERMINAL_STATES:
        return

    closure: _PairingClosure | None = None
    adoption: _PairingAdoption | None = None
    claim: tuple[str, str, bool] | None = None
    with db.get_db(
        config.db_path, busy_timeout_ms=_POLL_BUSY_TIMEOUT_MS
    ) as conn:
        # One transaction for the read, both expiry arms and the claim. The
        # claim has to be in the *same* `BEGIN IMMEDIATE` as the read that
        # found the row unclaimed, or two polls both read `requested` and both
        # spawn — `AsyncRuntime.spawn` does not deduplicate by name, so this
        # claim is the only guard there is. It is also strictly better than an
        # in-memory one: it survives the restart the orphan arm exists for and
        # every reader can see it.
        conn.execute("BEGIN IMMEDIATE")
        row = db.read_whatsapp_pairing(conn)
        if row is None or row["state"] in db.WHATSAPP_PAIRING_TERMINAL_STATES:
            return
        decision = _expire_stale_pairing(conn, bridge, row)
        if isinstance(decision, _PairingAdoption):
            adoption = decision
        elif decision is not None:
            closure = decision
        else:
            _mirror_pairing_window(conn, bridge, row)
            if (
                row["state"] == db.WHATSAPP_PAIRING_REQUESTED
                and bridge is not None
                and db.record_whatsapp_pairing_state(
                    conn, row["window_id"], db.WHATSAPP_PAIRING_SERVICING,
                )
            ):
                # `force` rides the claim rather than being re-read later: the
                # row is what carries the operator's confirmation, and by the
                # time the coroutine runs the deadline arm could have replaced
                # it with a fresh, unforced request.
                claim = (row["window_id"], row["requested_by"], row["force"])

    if adoption is not None:
        if _spawn_pairing_adopt(bridge, adoption):
            return
        # Nothing will re-arm it, so the row falls to the close adoption
        # replaced. Its own transaction rather than the one above: that one has
        # committed, and holding a write lock across a `spawn_task` is the
        # stall `_POLL_BUSY_TIMEOUT_MS` exists to keep off this thread.
        closure = _close_after_refused_adoption(config, row)
        if closure is None:
            return

    if closure is not None:
        _clean_up_after_closure(config, bridge, closure)
        # Off the dispatch thread, like every other blocking thing here. The
        # announce opens its own connection and then makes a network send per
        # admin — `send_notification`'s Talk leg is a blocking `run_coro` and
        # its email leg is synchronous SMTP — which is exactly the stall
        # `_POLL_BUSY_TIMEOUT_MS` exists to keep off this loop. The sibling
        # producer `_announce_unlink` already does both halves in threads; this
        # one had been left synchronous.
        _spawn_pairing_announce(config, closure)
        return
    if claim is not None:
        _spawn_pairing_service(
            config, bridge, claim[0], claim[1], force=claim[2],
        )


def _pairing_row_owns_no_window(row: dict | None) -> bool:
    """Whether a live pairing window would provably be an orphan of this row.

    **Three states, and the third is the one that is easy to miss.** No row at
    all and a terminal row are the obvious two. `requested` is the third: a
    fresh request is only ever written over a terminal predecessor, so a window
    alive while the row reads `requested` belongs to the request *before* this
    one — and without that arm it survives, `repair_session` refuses the new
    request `already_pairing` against it, and every retry fails for the rest of
    its TTL with the row recording a failure the operator did not cause.
    `_revert_pairing_claim` puts a row back to `requested` too, before the
    coroutine has run, where the same reasoning holds.

    **`servicing` is deliberately excluded, and an id comparison in place of a
    state test would be a defect rather than a tightening.** Between
    `open_pairing_window` and `_write_pairing_outcome`'s adopt write the row
    carries the *request* id while the live window carries its own, so "the two
    ids differ" is the ordinary state of a healthy re-pair — a tick landing in
    that gap would cancel the very window it is servicing. The state is what
    separates a leftover from work in flight; the id is not.
    """
    from ... import db  # noqa: PLC0415

    if row is None:
        return True
    return (
        row["state"] in db.WHATSAPP_PAIRING_TERMINAL_STATES
        or row["state"] == db.WHATSAPP_PAIRING_REQUESTED
    )


def _close_orphaned_pairing(config: "Config", bridge, row: dict | None) -> None:
    """Clear a window and a relay file the durable row no longer owns.

    The row is the authority on whether a pairing is over — it is what the web
    process can write and what survives a restart — so either leftover behind
    a row that does not own it is a full-account credential with nothing
    owning it.

    **Two leftovers, two remedies, and the second is not reachable through the
    first.** A live window is cancelled, which drops it, cancels its watchdog
    and unlinks the relay under the bridge's own lock. But a scheduler restart
    loses the window while the relay file survives — so after a `DELETE` there
    is nothing left to cancel and the last code sits at 0600 for the life of
    the deployment. `_clean_up_after_closure` is the sibling that would have
    swept it, and it never runs again: its closure is computed past the
    terminal early return, and the deadline arm only ever looks at non-terminal
    rows.

    **The existence test comes first, and that is about cost rather than
    tidiness.** This runs on the dispatch thread on every tick, and
    `clear_relay` globs the directory for staging files — so on a deployment
    whose pairing row has been closed for months that would be a directory
    listing per tick for ever. A `Path.exists()` on a file that is not there is
    one `stat`.

    Never raises: it is a poll tick.
    """
    if not _pairing_row_owns_no_window(row):
        return
    window = None if bridge is None else bridge.pairing_window
    if window is not None:
        _spawn_orphan_cancel(bridge, window)
        return
    try:
        relay = (
            baileys_bridge.default_pairing_relay_path(config)
            if bridge is None
            else bridge.pairing_relay_path
        )
        if not relay.exists():
            return
    except OSError:
        return
    logger.warning(
        "whatsapp.pairing.orphan_relay_swept path=%s: a pairing code outlived "
        "the request row that owned it", relay,
    )
    if bridge is not None:
        # Through the bridge, so the unlink is serialized against a write it
        # may still have in flight on a worker thread — the precondition
        # `pairing_relay.clear_relay` states for itself.
        bridge.clear_relay_file()
        return
    pairing_relay.clear_relay(relay)


def _spawn_orphan_cancel(bridge, window) -> None:
    """Cancel one orphaned window on the runtime loop. Never raises.

    Scheduled rather than awaited, because the bridge's asyncio primitives are
    bound to that loop and this runs on the scheduler's dispatch thread. Scoped
    to the id read on this thread, since by the time it runs the window it
    meant could have closed and a fresh one opened — and that scoping is also
    what covers Stage 3's stated residual, a window whose adopt write was
    refused and whose id therefore never reached the row at all.

    A refusal to schedule is logged and nothing else: the window's own watchdog
    still closes it at its TTL, and a runtime refusing a task means the process
    is going away with it. It is re-attempted on the next tick, which is
    harmless — `cancel_pairing` answers False once there is no window and
    `_end_pairing_window` re-checks identity — and costs one scheduled task per
    tick until one of them lands.
    """
    from ...async_runtime import spawn_task  # noqa: PLC0415

    coro = bridge.cancel_pairing(window_id=window.window_id)
    try:
        spawn_task(coro, name="whatsapp-pairing-cancel")
    except Exception as exc:  # noqa: BLE001 — a poll tick must not raise
        coro.close()
        logger.warning(
            "whatsapp.pairing.orphan_cancel_unscheduled window=%s reason=%s",
            window.window_id, exc,
        )


def _clean_up_after_closure(
    config: "Config", bridge, closure: "_PairingClosure",
) -> None:
    """Remove what a closed row leaves behind: the window, then the relay.

    **A row closing while its window is still live is the case this exists
    for.** The row's deadline is wall-clock and the window's is the loop's
    monotonic clock, and both are sized from the same TTL — so a row picked up
    late reaches its deadline while `repair_session` is still running or moments
    after it armed a window. With the window left alone the next QR rotation
    (about twenty seconds) republishes the relay file this function just
    removed, and nothing closes the row a second time, so a live pairing code
    sits on disk for the rest of the window's TTL after the operator has been
    told it expired. `cancel_pairing` is the primitive for it: it drops the
    window, cancels the watchdog and unlinks under `_relay_lock`, all three.

    **Scheduled on the runtime loop rather than awaited**, because the bridge's
    asyncio primitives are bound to it and this runs on the scheduler's
    dispatch thread. Scoped to the window id, since by the time it runs the
    window could have closed and a fresh one opened.

    The relay path is taken from the **config** when no bridge is in hand, not
    skipped: the deadline arm runs in a process whose bridge failed to start
    too, and there the file is a pairing code with no window and no durable row
    left to drive a later sweep.
    """
    if not closure.unlink_relay:
        return
    live = None if bridge is None else bridge.pairing_window
    if live is not None and live.window_id == closure.window_id:
        from ...async_runtime import spawn_task  # noqa: PLC0415

        try:
            spawn_task(
                bridge.cancel_pairing(window_id=closure.window_id),
                name="whatsapp-pairing-cancel",
            )
            return
        except Exception:
            # The runtime is stopping, so nothing will run the cancel. Fall
            # through to the unlink: the credential matters more than the
            # window, which is going away with the process anyway.
            logger.warning(
                "whatsapp.pairing.cancel_unscheduled window=%s",
                closure.window_id,
            )
    if bridge is not None:
        # Through the bridge, so the unlink is serialized against a write this
        # bridge may still have in flight on a worker thread — the
        # precondition `pairing_relay.clear_relay` states for itself.
        bridge.clear_relay_file()
        return
    pairing_relay.clear_relay(
        baileys_bridge.default_pairing_relay_path(config)
    )


def _spawn_pairing_announce(config: "Config", closure: "_PairingClosure") -> None:
    from ...async_runtime import spawn_task  # noqa: PLC0415

    try:
        spawn_task(
            asyncio.to_thread(_announce_pairing, config, closure),
            name="whatsapp-pairing-announce",
        )
    except Exception:
        # Nothing to fall back to that would not be the stall this avoids. The
        # durable row already carries the outcome, which is the record that
        # matters; the notification is the convenience on top.
        logger.warning(
            "whatsapp.pairing.announce_unscheduled window=%s state=%s",
            closure.window_id, closure.state,
        )


def _expire_stale_pairing(
    conn, bridge, row: dict,
) -> "_PairingClosure | _PairingAdoption | None":
    """Close a stale request row, or say it should be adopted instead.

    Three outcomes. A `_PairingClosure` is a row this moved to a terminal state;
    a `_PairingAdoption` is a window the caller should re-arm on the runtime
    loop, written back nowhere; `None` is a row to leave alone.

    **The two arms must not be collapsed into one**, and the obvious single
    sentence — "any non-terminal row with no in-memory window behind it" —
    kills the request it is meant to service. `request_whatsapp_pairing`
    writes the row `requested`, and no window exists until the last step of
    `repair_session`, up to a sidecar-return plus a stop timeout later. A fresh
    `requested` row therefore satisfies "no window behind it" *by
    construction*, so a poll running that predicate would stamp it `expired`
    and then find nothing pending to service — unconditionally, inside a single
    invocation, with no race involved. Pairing would never start once.

    The **deadline** arm applies to every non-terminal state, `requested` and
    `servicing` included. It is what closes a row nobody ever picked up (a
    scheduler that was down, a bridge that failed to start) and a row claimed
    by a process that died before its task ran.

    The **orphan** arm applies only to the states that imply a window was
    opened, and only in a process that holds the bridge. No live window behind
    such a row means the process that owned it is gone — which since ISSUE-504
    is an **adoption** rather than a close: the sidecar is its own unit and
    survives a scheduler restart, so it is still offering codes against the
    directory the sequence emptied, and the only thing lost was this daemon's
    record that somebody was waiting. The close survives it for the one row
    adoption cannot bound, a deadline this cannot read.

    **The two closures are not the same outcome and do not share a message.**
    A row that reached its deadline is `expired`: nobody scanned, and the
    destructive confirmation covered that. An orphaned row is `failed`, because
    the old credential is already moved aside and an operator has to be told
    which of two directories to trust.
    """
    from ... import db  # noqa: PLC0415

    state = row["state"]
    window_id = row["window_id"]
    in_window = state in db.WHATSAPP_PAIRING_WINDOW_STATES

    if not window_id:
        # **A row with no id is addressable by nothing else here**, and it is a
        # stuck row rather than a curiosity: every write in this module is
        # guarded on `pairing_window_id`, SQL equality never matches NULL, and
        # `request_whatsapp_pairing`'s own guard is on the *state* — so such a
        # row would refuse every later pairing request for the life of the
        # deployment with nothing able to close it. No writer produces one
        # today; Stage 4 adds routes that write this table directly, which is
        # the moment a defensive note stops being enough. `clear_whatsapp_
        # pairing` carries no id guard and is the one thing that can reach it.
        if db.clear_whatsapp_pairing(conn):
            logger.warning(
                "whatsapp.pairing.row_without_id_cleared state=%s — a pairing "
                "row with no window id can be addressed by nothing else",
                state,
            )
        return None

    deadline = row["expires_at"]
    if deadline and deadline <= db.sql_datetime_now():
        # Both sides are `sql_datetime_now`'s fixed-width UTC format, where a
        # lexical comparison *is* a chronological one.
        message = _deadline_message(row)
        if db.record_whatsapp_pairing_state(
            conn, window_id, db.WHATSAPP_PAIRING_EXPIRED, message,
        ):
            logger.info(
                "whatsapp.pairing.row_expired window=%s from=%s",
                window_id, state,
            )
            return _PairingClosure(
                window_id=window_id,
                state=db.WHATSAPP_PAIRING_EXPIRED,
                message=message,
                requested_by=row["requested_by"],
                unlink_relay=in_window,
            )
        return None

    if bridge is None or not in_window:
        return None
    live = bridge.pairing_window
    if live is not None and live.window_id == window_id:
        return None

    # A window this same process closed itself is not an orphan, and telling
    # the two apart is the whole reason `last_pairing_outcome` exists: a
    # *paired* close and a process that died holding a window both present as
    # `pairing_window is None`, so without it a successful pairing would be
    # recorded `failed`.
    outcome = bridge.last_pairing_outcome
    if outcome is None or outcome.window_id != window_id:
        # **Nothing closed this window here: a process holding it went away.**
        # Before ISSUE-504 that was the orphan close below, which is wrong in
        # the case it is most often reached in — the credential has already
        # been archived and the sidecar is still offering codes, so the row is
        # the only thing that was lost. Re-arm from it instead. The deadline
        # arm above is what still bounds the result, which is why this sits
        # behind it; the outcome branch below is what still records a pairing
        # this process completed, which is why this sits behind that too.
        adoption = _pairing_adoption(row)
        if adoption is not None:
            return adoption
        # A deadline this cannot read is a window that could not be bounded, so
        # the old close is the right answer rather than an armed relay nothing
        # would expire.
        return _record_pairing_closure(
            conn, row, db.WHATSAPP_PAIRING_FAILED, _orphan_message(row),
        )

    state_out, message = outcome.state, outcome.message
    # The bridge's five close sites all pass a terminal state today, so this
    # coercion is latent — and it is the difference between a latent defect and
    # a live one. A non-terminal value would land the row non-terminal with no
    # window behind it, and since every write also stamps `updated_at` the
    # `rowcount` is 1 every time: this arm would then return a fresh closure on
    # every tick, one relay clear, one log line and one alert per poll interval
    # until the deadline.
    if state_out not in db.WHATSAPP_PAIRING_TERMINAL_STATES:
        logger.warning(
            "whatsapp.pairing.outcome_not_terminal window=%s state=%s — "
            "recorded as failed",
            window_id, state_out,
        )
        state_out = db.WHATSAPP_PAIRING_FAILED
    return _record_pairing_closure(conn, row, state_out, message)


def _pairing_adoption(row: dict) -> "_PairingAdoption | None":
    """The row read back as a window to re-arm, or `None` where it cannot be.

    `None` on a deadline this cannot parse, which is the one thing adoption
    genuinely needs: the bridge's watchdog fires on that instant, so a window
    armed without it would hold a published credential until the process
    stopped. The caller closes such a row instead.

    `sql_epoch_from_datetime` rather than a fourth parser — `db` already names
    it the authoritative epoch-direction reader of this column's format, and a
    copy here would be a fifth.
    """
    from ... import db  # noqa: PLC0415

    expires_at_wall = db.sql_epoch_from_datetime(row["expires_at"])
    if expires_at_wall is None:
        return None
    return _PairingAdoption(
        window_id=row["window_id"],
        requested_by=row["requested_by"],
        expires_at_wall=expires_at_wall,
        destructive=bool(row["message"]),
        # **`awaiting_scan` and not "anything but `awaiting_sidecar`"**:
        # `sidecar_absent` is a demotion the watchdog applies only to a window
        # still waiting for its first code, so it means nothing was ever
        # relayed either. Reading it as evidence would have a `ready` on a
        # window nobody scanned recorded as a successful pairing.
        codes_relayed=row["state"] == db.WHATSAPP_PAIRING_AWAITING_SCAN,
    )


def _record_pairing_closure(
    conn, row: dict, state: str, message: str,
) -> "_PairingClosure | None":
    """Move a window-state row to `state` and describe it for the announcement.

    One spelling of the write and the closure construction, because three
    callers reach it now: the outcome branch, the unbounded-deadline arm, and
    `_close_after_refused_adoption`, which runs in its own transaction after
    the poll's has committed. `None` where the write did not apply — the row
    went terminal underneath, which `record_whatsapp_pairing_state`'s own
    guards answer.
    """
    from ... import db  # noqa: PLC0415

    window_id = row["window_id"]
    if not db.record_whatsapp_pairing_state(conn, window_id, state, message):
        return None
    logger.info(
        "whatsapp.pairing.row_reconciled window=%s from=%s to=%s",
        window_id, row["state"], state,
    )
    return _PairingClosure(
        window_id=window_id,
        state=state,
        message=message,
        requested_by=row["requested_by"],
        unlink_relay=True,
    )


def _mirror_pairing_window(conn, bridge, row: dict) -> None:
    """Copy a live window's state onto the row, when it moved.

    The bridge writes the relay file and no database row, so without this the
    durable row would sit at `awaiting_sidecar` for the whole window and the
    orphan arm's other two states would be unreachable. Guarded on the state
    having actually changed, so an open window costs one UPDATE per transition
    rather than one per tick.
    """
    from ... import db  # noqa: PLC0415

    if bridge is None:
        return
    live = bridge.pairing_window
    if live is None or live.window_id != row["window_id"]:
        return
    if (
        live.state == row["state"]
        or live.state not in db.WHATSAPP_PAIRING_WINDOW_STATES
    ):
        return
    # **The message is preserved verbatim and this function never writes one.**
    # The row's message is the durable archive record — the only place a later
    # process can learn which `.old-<timestamp>` sibling this attempt made —
    # and the bridge sets `window.message` to its own prose on the
    # `sidecar_absent` demotion, so taking the live one destroyed the path on
    # the ordinary `awaiting_sidecar` -> `sidecar_absent` -> restart -> orphan
    # sequence. The live prose is not lost to anybody who needs it: the relay
    # file carries it, and the relay file is what the admin UI reads for a
    # window that is still open.
    db.record_whatsapp_pairing_state(
        conn, row["window_id"], live.state, row["message"] or None,
    )


def _spawn_pairing_service(
    config: "Config", bridge, request_id: str, requested_by: str,
    *, force: bool = False,
) -> None:
    """Hand the claimed request to the bridge, on the runtime loop.

    `spawn_task` rather than a worker thread, because the bridge's asyncio
    primitives are bound to that loop and `repair_session`'s own docstring
    requires it: `_link_dropped` is set from the loop with no
    `call_soon_threadsafe`, so a waiter created elsewhere would run to the stop
    timeout and report a failure for a sidecar that did exit. `spawn` schedules
    and returns, where `run_coro` would block the dispatch thread for the whole
    sequence — so no thread is created on any tick, and none on a pairing.

    A `RuntimeError` means the runtime is stopping or was never started, and a
    shutdown landing between the claim and the spawn must not strand the row:
    the claim is reverted to `requested` so the next poll can service it.
    """
    from ...async_runtime import spawn_task  # noqa: PLC0415

    # The coroutine is built outside the `try` so the failure path can close
    # it. `spawn` closes it itself on the `RuntimeError` it raises, but not on
    # anything else, and an un-awaited coroutine is a `RuntimeWarning` in a
    # place nobody is watching for one.
    coro = _service_pairing_request(
        config, bridge, request_id, requested_by, force=force,
    )
    try:
        spawn_task(coro, name="whatsapp-pairing")
    except Exception as exc:  # noqa: BLE001 — the claim must not be stranded
        # `RuntimeError` is the documented case (the runtime is stopping or was
        # never started) and deliberately not the only one caught: anything at
        # all out of the spawn leaves a row claimed with nothing running, which
        # only the deadline arm would close, minutes later and with a message
        # about a bridge that never picked it up.
        coro.close()
        logger.warning(
            "whatsapp.pairing.spawn_refused window=%s reason=%s — the claim is "
            "reverted and the next poll will retry",
            request_id, exc,
        )
        _revert_pairing_claim(config, request_id)


def _spawn_pairing_adopt(bridge, adoption: "_PairingAdoption") -> bool:
    """Re-arm an orphaned window on the runtime loop. `False` if it could not.

    `spawn_task` rather than a worker thread, for `_spawn_pairing_service`'s
    reason: the bridge's asyncio primitives are bound to that loop, and a
    `run_coro` here would block the dispatch thread for the length of a relay
    publish.

    **A refusal is reported rather than swallowed**, because it is the one path
    that still reaches the close adoption replaced — and the caller has to know
    which of the two happened. `adopt_pairing_window` answering `None` is a
    different thing and is deliberately not reported, so nothing here needs
    retry bookkeeping — but the four refusals do not all converge the same way.
    Three reach an arm that already exists within a tick or two: a matching
    live window is arm 4, a passed deadline is arm 2, and a bridge that is
    stopping takes the process with it. The fourth, a session already `ready`,
    converges through **nothing** until the deadline arm fires, so until then
    each tick spawns a coroutine that refuses on sight. That is bounded by the
    row's own TTL and costs one scheduled no-op per poll; it is written down
    rather than fixed because the alternative is a close, and a row whose
    session came up on its own is not a failure to record.
    """
    from ...async_runtime import spawn_task  # noqa: PLC0415

    coro = bridge.adopt_pairing_window(
        adoption.window_id,
        adoption.requested_by,
        adoption.expires_at_wall,
        destructive=adoption.destructive,
        codes_relayed=adoption.codes_relayed,
    )
    try:
        spawn_task(coro, name="whatsapp-pairing-adopt")
        return True
    except Exception as exc:  # noqa: BLE001 — a poll tick must not raise
        coro.close()
        logger.warning(
            "whatsapp.pairing.adopt_unscheduled window=%s reason=%s",
            adoption.window_id, exc,
        )
        return False


def _close_after_refused_adoption(
    config: "Config", row: dict,
) -> "_PairingClosure | None":
    """Close a row nothing could re-arm. `None` where the write did not apply.

    The pre-ISSUE-504 orphan close, now reached only when the runtime refused
    the adoption — which means it is stopping, so this is a row the next
    process would adopt anyway. It is still written, because the archive path
    has to reach an operator somehow and the row is the only thing carrying it.
    """
    from ... import db  # noqa: PLC0415

    try:
        with db.get_db(
            config.db_path, busy_timeout_ms=_POLL_BUSY_TIMEOUT_MS
        ) as conn:
            return _record_pairing_closure(
                conn, row, db.WHATSAPP_PAIRING_FAILED, _orphan_message(row),
            )
    except Exception:
        logger.warning("whatsapp.pairing.orphan_close_failed", exc_info=True)
        return None


def _revert_pairing_claim(config: "Config", request_id: str) -> None:
    from ... import db  # noqa: PLC0415

    try:
        with db.get_db(
            config.db_path, busy_timeout_ms=_POLL_BUSY_TIMEOUT_MS
        ) as conn:
            db.record_whatsapp_pairing_state(
                conn, request_id, db.WHATSAPP_PAIRING_REQUESTED,
            )
    except Exception:
        logger.warning("whatsapp.pairing.claim_revert_failed", exc_info=True)


async def _service_pairing_request(
    config: "Config", bridge, request_id: str, requested_by: str,
    *, force: bool = False,
) -> None:
    """Run one re-pair and write its outcome onto the request row.

    Every database touch goes through `asyncio.to_thread`: this runs on the
    runtime loop, where a `get_db` taking the write lock synchronously is the
    stall the rest of this surface already avoids at several seams.

    **`force` comes off the row and from nowhere else.** It is the operator's
    confirmation that disconnecting a working session is acceptable, collected
    by the surface they spoke to and carried across processes in
    `pairing_force`; this function is not that surface and must not manufacture
    one. Deriving it from the bridge's live state was the obvious alternative
    and is wrong in the one direction that matters: a request written while a
    fatal was latched, serviced after the session recovered, would disconnect a
    session nobody agreed to disconnect. The default is therefore the safe one,
    so a caller that forgets the argument gets the refusal rather than the
    destructive path.
    """
    try:
        result = await bridge.repair_session(requested_by, force=force)
    except asyncio.CancelledError:
        # `repair_session` re-raises cancellation, and `AsyncRuntime._shutdown`
        # cancels pending tasks — so a daemon stopping mid-repair would leave
        # the row `servicing` with nothing to close it but the deadline arm,
        # minutes later and under a message about a bridge that never picked it
        # up. The attempt may already have moved the credential aside, so the
        # row is the only place that could say so. Recorded synchronously and
        # with the bounded timeout, because there is no thread hop left during
        # cancellation; then re-raised, per the runtime's own contract.
        _record_pairing_interrupted(config, request_id)
        raise
    expires_at: float | None = None
    window = bridge.pairing_window
    if window is not None and window.window_id == result.window_id:
        expires_at = window.expires_at_wall
    try:
        closure = await asyncio.to_thread(
            _write_pairing_outcome,
            config, request_id, result, expires_at, requested_by,
        )
    except Exception:
        logger.warning("whatsapp.pairing.outcome_write_failed", exc_info=True)
        return
    if closure is None:
        return
    try:
        await asyncio.to_thread(_announce_pairing, config, closure)
    except Exception:
        logger.warning("whatsapp.pairing.announce_failed", exc_info=True)


def _record_pairing_interrupted(config: "Config", request_id: str) -> None:
    from ... import db  # noqa: PLC0415

    try:
        with db.get_db(
            config.db_path, busy_timeout_ms=_POLL_BUSY_TIMEOUT_MS
        ) as conn:
            db.record_whatsapp_pairing_state(
                conn, request_id, db.WHATSAPP_PAIRING_FAILED,
                "the daemon stopped while pairing WhatsApp, so the attempt "
                "did not finish. Check `doctor`'s `whatsapp.baileys_session` "
                "for where the session directory stands, then re-pair.",
            )
    except Exception:
        logger.warning("whatsapp.pairing.interrupt_record_failed", exc_info=True)


def _write_pairing_outcome(
    config: "Config",
    request_id: str,
    result,
    expires_at: float | None,
    requested_by: str = "",
) -> "_PairingClosure | None":
    """Record what `repair_session` did. A closure to announce, or `None`.

    **A refusal never lands in a window-implying state**, whatever it was
    called. `sidecar_absent` is a `PairingResult` reason *and* a window state,
    and writing the reason into the row would have the orphan arm fire on a row
    with no window behind it — so every refusal is `failed`, carrying its own
    prose, and the window states are reached only by a window that exists.

    A window that opened is not an outcome yet, so it returns `None`: the
    announcement belongs to the close, which the poll's arms record.
    """
    from ... import db  # noqa: PLC0415

    if result.ok and result.window_id:
        with db.get_db(
            config.db_path, busy_timeout_ms=_POLL_BUSY_TIMEOUT_MS
        ) as conn:
            adopted = db.record_whatsapp_pairing_state(
                conn, request_id,
                db.WHATSAPP_PAIRING_AWAITING_SIDECAR,
                _window_open_message(result),
                adopt_window_id=result.window_id,
                expires_at=expires_at,
            )
        if not adopted:
            # The row went terminal while the sequence ran — the deadline arm
            # is the case `record_whatsapp_pairing_state`'s terminal guard
            # exists for. A live window with no durable row behind it is why
            # this is said out loud rather than dropped: the next request
            # dead-ends on `repair_session`'s `already_pairing` while the
            # operator has been told this one expired. The poll's own closure
            # cleanup cancels the window on that path, so the recovery is one
            # tick away rather than a TTL.
            logger.warning(
                "whatsapp.pairing.window_untracked request=%s window=%s — the "
                "row closed while the re-pair ran",
                request_id, result.window_id,
            )
        return None

    message = result.message or (
        "the WhatsApp re-pair was refused and the session is unchanged"
    )
    if result.moved_to is not None:
        message = f"{message} The previous session is at {result.moved_to}."
    with db.get_db(
        config.db_path, busy_timeout_ms=_POLL_BUSY_TIMEOUT_MS
    ) as conn:
        applied = db.record_whatsapp_pairing_state(
            conn, request_id, db.WHATSAPP_PAIRING_FAILED, message,
        )
    if not applied:
        return None
    logger.info(
        "whatsapp.pairing.request_failed window=%s reason=%s",
        request_id, result.reason,
    )
    return _PairingClosure(
        window_id=request_id,
        state=db.WHATSAPP_PAIRING_FAILED,
        message=message,
        # Carried from the caller rather than left empty: the poll's own
        # closures pass it, and `_write_pairing_alerts` appends "Requested by
        # …" only when it is set — so the same notification read differently
        # depending on which path closed the row.
        requested_by=requested_by,
        unlink_relay=False,
    )


def _window_open_message(result) -> str:
    """What the row carries while a window is open: the archive path, alone.

    **Durable state rather than prose, which is why it is only the path.** The
    orphan arm runs in a *later* process, which has no `PairingResult` and no
    way to know which of the `.old-<timestamp>` siblings this re-pair made, so
    the path is written down here while it is known and carried forward by
    whichever arm closes the row. The remedy that belongs beside it — scan the
    code — is live-window copy and lives on the relay file, which is what the
    admin UI reads; carrying it on the row put it in front of an operator
    reading a *terminal* row, telling them to scan a code from a window that
    had just closed.
    """
    if result.moved_to is None:
        return ""
    return f"The previous session was moved aside to {result.moved_to}."


def _deadline_message(row: dict) -> str:
    from ... import db  # noqa: PLC0415

    state = row["state"]
    if state == db.WHATSAPP_PAIRING_REQUESTED:
        return (
            "no bridge picked this pairing request up before it expired. "
            "Check that the istota scheduler is running and that the WhatsApp "
            "surface is set to the baileys provider, then request it again."
        )
    if state == db.WHATSAPP_PAIRING_SERVICING:
        # Its own prose, because the `requested` message above is false on
        # every count for a row a bridge *did* claim: the scheduler was
        # running and the provider was right, and the process that took the
        # row went away before it could report.
        return (
            "a bridge claimed this pairing request and did not report back "
            "before it expired — the process it was running in most likely "
            "restarted. Request a re-pair again."
        )
    base = (
        "the pairing window expired with no code scanned, so WhatsApp is "
        "still unpaired."
    )
    return _with_archive(base, row)


def _orphan_message(row: dict) -> str:
    """Why an orphaned row closed, and a remedy that actually works.

    **Not "request a re-pair again".** The session directory here is the empty
    one the re-pair created: a sidecar sits in it in pairing mode, so nothing
    re-latches a permanent fatal, and `repair_session`'s confirmation gate then
    refuses the next request as `session_live` — the durable channel carries no
    `force`, so the unforced default is all the poll can ask for. The remedy has
    to be one of the two surfaces that *can* confirm.

    **Since ISSUE-504 this has two producers and neither is the restart it used
    to be about**: a row whose deadline `_pairing_adoption` could not parse,
    where the daemon is perfectly healthy and the window simply could not be
    bounded, and a `spawn_task` the runtime refused, which means the daemon is
    stopping. The prose fits both, because what it says is that nothing took
    the window over rather than why. Either way the credential is at the archive the row is
    carrying, so the remedy names `restore-session` beside the two re-pair
    routes: an operator whose session was working before the re-pair usually
    wants it back rather than a new one.
    """
    base = (
        "nothing could take over the WhatsApp pairing window this deployment "
        "had open, so it closed with no code scanned and the session directory "
        "is empty. Re-pair from the admin Connections pane, confirming the "
        "unlink, or stop the istota daemon and any sidecar unit and run "
        "`istota whatsapp pair`. To put the previous session back instead, "
        "stop both and run `istota whatsapp restore-session`."
    )
    return _with_archive(base, row)


def _with_archive(base: str, row: dict) -> str:
    """`base`, plus the archive clause the row is carrying, if any.

    Only ever the clause — `_window_open_message` is what keeps the row's
    message down to that, so appending it cannot contradict the sentence in
    front of it.
    """
    carried = row["message"]
    return f"{base} {carried}" if carried else base


def _announce_pairing(config: "Config", outcome: "_PairingClosure") -> None:
    """Tell the admins how a pairing ended. Never raises.

    `_announce_unlink`'s shape and its `task_alert` route, with one difference
    that matters: the dedup key carries the window id. That source is
    fire-and-forget and an upsert onto an open row *bumps* rather than
    delivers, so a fixed key would have the second pairing outcome of a
    deployment's life reach nobody. The id is our own uuid4 hex and is bounded
    besides, since `_slug` is what the key is built through.

    Announced on every terminal state, `expired` included: a window that closed
    unscanned leaves WhatsApp unpaired with the old credential moved aside,
    which is exactly as much an operator's business as a failure is.
    """
    try:
        raised = _write_pairing_alerts(config, outcome)
        for item in raised:
            _push_baileys_alert(config, item)
    except Exception:
        logger.warning("whatsapp.pairing.alert_failed", exc_info=True)


def _write_pairing_alerts(
    config: "Config", outcome: "_PairingClosure"
) -> tuple[object, ...]:
    from ... import db  # noqa: PLC0415
    from ...notification_resolvers import task_alert  # noqa: PLC0415
    from ...notification_resolvers.task_alert import _slug  # noqa: PLC0415

    title = _PAIRING_ALERT_TITLES.get(
        outcome.state, "The WhatsApp pairing request closed"
    )
    severity = _PAIRING_ALERT_SEVERITY.get(outcome.state, "warning")
    body = outcome.message or title
    if outcome.requested_by:
        body = f"{body} Requested by {_slug(outcome.requested_by)}."
    key = f"whatsapp:baileys-pairing:{_slug(outcome.window_id, fallback='unknown')}"
    raised = []
    with db.get_db(
        config.db_path, busy_timeout_ms=_POLL_BUSY_TIMEOUT_MS
    ) as conn:
        for reader in _unlink_readers(config):
            raised.append(task_alert.write(
                conn, reader,
                dedup_key=key,
                title=title,
                body=body,
                severity=severity,
                params={"task_id": None, "status": f"pairing_{outcome.state}"},
            ))
    return tuple(item for item in raised if item is not None)


__all__ = [
    "baileys_bridge_wanted",
    "poll_pairing_request",
    "start_baileys_bridge",
]
