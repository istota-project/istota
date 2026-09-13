"""Who starts the Baileys bridge, and who is told when its session dies.

The bridge is a mechanism with no opinion about lifecycle: it never raises,
never logs a payload, never opens a database. That leaves two jobs nobody
owned, and both are here.

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
from typing import TYPE_CHECKING

from . import baileys_bridge

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
            await asyncio.to_thread(_push_unlink_alert, config, item)
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


def _push_unlink_alert(config: "Config", raised) -> None:
    from .._alerts import push_off_surface  # noqa: PLC0415

    push_off_surface(
        config, raised,
        exclude_surface="whatsapp",
        reference_prefix="istota:whatsapp:baileys",
    )


__all__ = [
    "baileys_bridge_wanted",
    "start_baileys_bridge",
]
