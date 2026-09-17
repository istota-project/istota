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
    bridge = baileys_bridge.BaileysBridge(
        config,
        sidecar_argv=argv,
        on_fatal=lambda reason: _announce_unlink(config, reason),
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
    `unpaired`, `bad_session`) plus whatever else a sidecar sends, so it is
    reported as a bounded label and never as prose: Baileys' error text is one
    of the places a JID or a message body turns up, and this string reaches a
    notification panel and every alert route the user has configured.
    `task_alert._slug` is that bound, borrowed rather than re-spelled — it is
    the same rule this source already applies to every other caller-supplied
    key component, for the same reason.
    """
    from ...notification_resolvers.task_alert import _slug  # noqa: PLC0415

    return (
        f"The WhatsApp device link ended ({_slug(reason, fallback='unknown')}), so every "
        "WhatsApp send is refused until the session is paired again. Stop the "
        "istota daemon and any sidecar running as a unit of its own, run "
        "`istota whatsapp pair --reset`, scan the code from WhatsApp's Linked "
        "Devices screen, then start them again. The old session is kept as a "
        "timestamped sibling directory, not deleted."
    )


def _write_unlink_alerts(config: "Config", reason: str) -> tuple[object, ...]:
    from ... import db  # noqa: PLC0415
    from ...notification_resolvers import task_alert  # noqa: PLC0415

    body = _alert_body(reason)
    raised = []
    with db.get_db(config.db_path) as conn:
        for reader in _unlink_readers(config):
            raised.append(task_alert.write(
                conn, reader,
                dedup_key=_ALERT_DEDUP_KEY,
                title=_ALERT_TITLE,
                body=body,
                params={"task_id": None, "status": "session_unlinked"},
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
    if row is None or row["state"] in db.WHATSAPP_PAIRING_TERMINAL_STATES:
        return

    closure: _PairingClosure | None = None
    claim: tuple[str, str] | None = None
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
        closure = _expire_stale_pairing(conn, bridge, row)
        if closure is None:
            _mirror_pairing_window(conn, bridge, row)
            if (
                row["state"] == db.WHATSAPP_PAIRING_REQUESTED
                and bridge is not None
                and db.record_whatsapp_pairing_state(
                    conn, row["window_id"], db.WHATSAPP_PAIRING_SERVICING,
                )
            ):
                claim = (row["window_id"], row["requested_by"])

    if closure is not None:
        if closure.unlink_relay and bridge is not None:
            pairing_relay.clear_relay(bridge.pairing_relay_path)
        _announce_pairing(config, closure)
        return
    if claim is not None:
        _spawn_pairing_service(config, bridge, claim[0], claim[1])


def _expire_stale_pairing(conn, bridge, row: dict) -> "_PairingClosure | None":
    """Close a stale request row, by whichever of **two** arms applies.

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
    such a row means the process that owned it is gone.

    **The two closures are not the same outcome and do not share a message.**
    A row that reached its deadline is `expired`: nobody scanned, and the
    destructive confirmation covered that. An orphaned row is `failed`, because
    the old credential is already moved aside and an operator has to be told
    which of two directories to trust. On a deployment whose update cron
    restarts the units on every commit, the orphan arm is routine rather than
    exceptional.
    """
    from ... import db  # noqa: PLC0415

    state = row["state"]
    window_id = row["window_id"]
    in_window = state in db.WHATSAPP_PAIRING_WINDOW_STATES

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
    if outcome is not None and outcome.window_id == window_id:
        state_out, message = outcome.state, outcome.message
    else:
        state_out, message = db.WHATSAPP_PAIRING_FAILED, _orphan_message(row)
    if not db.record_whatsapp_pairing_state(
        conn, window_id, state_out, message,
    ):
        return None
    logger.info(
        "whatsapp.pairing.row_reconciled window=%s from=%s to=%s",
        window_id, state, state_out,
    )
    return _PairingClosure(
        window_id=window_id,
        state=state_out,
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
    db.record_whatsapp_pairing_state(
        conn, row["window_id"], live.state, live.message or row["message"],
    )


def _spawn_pairing_service(
    config: "Config", bridge, request_id: str, requested_by: str,
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

    try:
        spawn_task(
            _service_pairing_request(config, bridge, request_id, requested_by),
            name="whatsapp-pairing",
        )
    except RuntimeError as exc:
        logger.warning(
            "whatsapp.pairing.spawn_refused window=%s reason=%s — the claim is "
            "reverted and the next poll will retry",
            request_id, exc,
        )
        _revert_pairing_claim(config, request_id)


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
) -> None:
    """Run one re-pair and write its outcome onto the request row.

    Every database touch goes through `asyncio.to_thread`: this runs on the
    runtime loop, where a `get_db` taking the write lock synchronously is the
    stall the rest of this surface already avoids at several seams.

    `force` is deliberately not passed. The durable row has no column for it,
    so the only way to derive one here would be to read the bridge's live
    state — and a request written while a fatal was latched, serviced after the
    session recovered, would then disconnect a working session. The unforced
    default is the spec's safe one and the latched-permanent-fatal case is what
    this flow is for; carrying an operator's confirmed `force` across the row
    belongs with the routes that collect it.
    """
    result = await bridge.repair_session(requested_by)
    expires_at: float | None = None
    window = bridge.pairing_window
    if window is not None and window.window_id == result.window_id:
        expires_at = window.expires_at_wall
    try:
        closure = await asyncio.to_thread(
            _write_pairing_outcome, config, request_id, result, expires_at,
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


def _write_pairing_outcome(
    config: "Config", request_id: str, result, expires_at: float | None,
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
        with db.get_db(config.db_path) as conn:
            db.record_whatsapp_pairing_state(
                conn, request_id,
                db.WHATSAPP_PAIRING_AWAITING_SIDECAR,
                _window_open_message(result),
                adopt_window_id=result.window_id,
                expires_at=expires_at,
            )
        return None

    message = result.message or (
        "the WhatsApp re-pair was refused and the session is unchanged"
    )
    if result.moved_to is not None:
        message = f"{message} The previous session is at {result.moved_to}."
    with db.get_db(config.db_path) as conn:
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
        requested_by="",
        unlink_relay=False,
    )


def _window_open_message(result) -> str:
    """What the row says while a window is open.

    It names the archive, and that is durable state rather than prose: the
    orphan arm runs in a *later* process, which has no `PairingResult` and no
    way to know which of the `.old-<timestamp>` siblings this re-pair made. So
    the path is written down here, while it is known, and carried forward by
    `_orphan_message`.
    """
    base = (
        "a pairing window is open. Scan the code from WhatsApp's Linked "
        "Devices screen."
    )
    if result.moved_to is None:
        return base
    return f"{base} The previous session was moved aside to {result.moved_to}."


def _deadline_message(row: dict) -> str:
    from ... import db  # noqa: PLC0415

    if row["state"] in (
        db.WHATSAPP_PAIRING_REQUESTED, db.WHATSAPP_PAIRING_SERVICING,
    ):
        return (
            "no bridge picked this pairing request up before it expired. "
            "Check that the istota scheduler is running and that the WhatsApp "
            "surface is set to the baileys provider, then request it again."
        )
    carried = row["message"]
    base = (
        "the pairing window expired with no code scanned, so WhatsApp is "
        "still unpaired."
    )
    return f"{base} {carried}" if carried else base


def _orphan_message(row: dict) -> str:
    base = (
        "the process that was pairing WhatsApp restarted before a code was "
        "scanned, so the window is gone. Request a re-pair again — it reuses "
        "the session directory this attempt already emptied rather than "
        "archiving a second time."
    )
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
    with db.get_db(config.db_path) as conn:
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
