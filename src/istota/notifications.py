"""Centralized notification dispatcher for Talk, Email, and ntfy.

Dispatch is driven through the transport routing helpers: a ``surface`` string
is an ``output_target`` descriptor (``talk`` / ``email`` / ``ntfy`` / ``both``
/ ``all`` / ``talk:<token>`` / comma lists), parsed into destinations and
looped — there is no per-surface ``if surface == "both"`` chain. The actual
ntfy POST lives in ``transport.ntfy`` (the single ntfy delivery path); the
``_send_ntfy`` shim here just adapts the sync notification signature to it.
"""

import logging
import threading
from typing import TYPE_CHECKING

# Re-exported so existing references (and the is_channel_configured probe) keep
# working; the canonical home is the ntfy transport.
from .transport.ntfy import _NTFY_DEFAULT_PRIORITY  # noqa: F401
from .transport.ntfy import ntfy_settings as _ntfy_settings
from .transport.registry import make_registry

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger("istota.notifications")


# Purpose names for the per-user routing table.
PURPOSES = ("reply", "alert", "log", "briefing", "notification")


def resolve_destinations(config: "Config", user_id: str, purpose: str):
    """Resolve the ordered list of delivery ``Destination``s for a user + purpose.

    Precedence:
      1. the user's ``routing[purpose]`` descriptor (a full comma-list — every
         destination, not just the first),
      2. legacy per-purpose fields (alerts_channel → ``alert``;
         log_channel → ``log``; first briefing token → ``briefing``),
      3. the user's ``default_destination`` descriptor,
      4. instance fallback ``[talk]`` (channel ``None`` → resolve at delivery / DM).

    A returned Talk destination may carry ``channel=None``, meaning "resolve the
    user's Talk channel at delivery time." Always returns at least one entry.
    """
    from .transport import Destination, parse_output_target

    uc = config.users.get(user_id)

    if uc and uc.routing:
        spec = uc.routing.get(purpose)
        if spec:
            dests = parse_output_target(spec)
            if dests:
                return dests

    if uc:
        if purpose == "alert" and uc.alerts_channel:
            return [Destination("talk", uc.alerts_channel)]
        if purpose == "log" and uc.log_channel:
            return [Destination("talk", uc.log_channel)]
        if purpose == "briefing":
            for briefing in uc.briefings:
                if briefing.conversation_token:
                    return [Destination("talk", briefing.conversation_token)]

    if uc and uc.default_destination:
        dests = parse_output_target(uc.default_destination)
        if dests:
            return dests

    return [Destination("talk", None)]


def resolve_destination(config: "Config", user_id: str, purpose: str):
    """The primary (first) delivery ``Destination`` for a user + purpose.

    Thin wrapper over :func:`resolve_destinations` for callers that only need a
    single channel. See that function for the precedence rules.
    """
    return resolve_destinations(config, user_id, purpose)[0]


def _descriptor_for_destination(dest) -> str:
    """Render a ``Destination`` back into an ``output_target`` descriptor leaf."""
    return dest.surface if dest.channel is None else f"{dest.surface}:{dest.channel}"


def surface_for_purpose(config: "Config", user_id: str, purpose: str) -> str:
    """The ``output_target`` descriptor string the routing table resolves a
    purpose to (a comma list when the user routes a purpose to several surfaces).

    Used where a surface *string* is needed rather than ``Destination``s — e.g.
    heartbeat's :func:`is_channel_configured` probe.
    """
    dests = resolve_destinations(config, user_id, purpose)
    return ",".join(_descriptor_for_destination(d) for d in dests) or "talk"


def effective_log_destinations(config: "Config", user_id: str):
    """Resolve where a user's verbose execution log goes — and whether it is
    enabled at all.

    The log channel is **opt-in**, so this deliberately does *not* reuse
    :func:`resolve_destinations` (whose generic fall-through to
    ``default_destination`` / bare ``talk`` would silently turn the verbose log
    on for every user). Sources, in precedence order:

      1. the user's ``routing["log"]`` descriptor,
      2. the legacy ``log_channel`` Talk token (→ ``talk:<token>``),
      3. otherwise ``[]`` — the log channel is disabled for this user.

    Resolved destinations are filtered to surfaces that are registered **and**
    ``user_routable`` (each drop logged at WARNING). A bare ``talk`` destination
    (no explicit ``:token``) has its channel resolved via
    :func:`resolve_conversation_token`; if that yields nothing the destination is
    dropped. Returns a deduplicated list; never raises into the caller.
    """
    from .transport import Destination, parse_output_target

    try:
        uc = config.users.get(user_id)
        if not uc:
            return []

        if uc.routing and uc.routing.get("log"):
            dests = parse_output_target(uc.routing["log"])
        elif uc.log_channel:
            dests = [Destination("talk", uc.log_channel)]
        else:
            return []

        registry = make_registry(config)
        resolved: list[Destination] = []
        seen: set[tuple[str, str | None]] = set()
        for dest in dests:
            transport = registry.get(dest.surface)
            if transport is None:
                logger.warning(
                    "Dropping log destination %r for user %s: surface not registered",
                    dest.surface, user_id,
                )
                continue
            if not getattr(transport.capabilities, "user_routable", True):
                logger.warning(
                    "Dropping log destination %r for user %s: surface not user-routable",
                    dest.surface, user_id,
                )
                continue
            channel = dest.channel
            if dest.surface == "talk" and channel is None:
                # Bare `talk` for the log purpose means "the user's logs room":
                # prefer the provisioned log_channel, fall back to the default
                # Talk channel / DM only if no logs room is set.
                channel = uc.log_channel or resolve_conversation_token(config, user_id)
                if not channel:
                    logger.warning(
                        "Dropping bare talk log destination for user %s: no "
                        "resolvable Talk channel", user_id,
                    )
                    continue
            elif dest.surface == "web" and channel is None:
                # Bare `web` log route lands in the user's default room.
                from .transport.web import default_web_room_token
                channel = default_web_room_token(config, user_id)
                if not channel:
                    logger.warning(
                        "Dropping bare web log destination for user %s: no "
                        "resolvable web room", user_id,
                    )
                    continue
            key = (dest.surface, channel)
            if key in seen:
                continue
            seen.add(key)
            resolved.append(Destination(dest.surface, channel))
        return resolved
    except Exception:
        logger.warning(
            "effective_log_destinations failed for user %s", user_id, exc_info=True,
        )
        return []


def resolve_conversation_token(config: "Config", user_id: str) -> str | None:
    """Resolve Talk conversation token for a user.

    Priority: an explicit Talk route (``routing["alert"]`` / ``routing["reply"]``)
    > alerts_channel > briefing token > auto-detected 1:1 DM. Retains the
    unconditional Talk auto-DM fallback so routing a purpose off-Talk does not
    make this report Talk as unconfigured (which would corrupt heartbeat's
    ``consecutive_errors`` accounting).
    """
    user_config = config.users.get(user_id)
    if not user_config:
        return None

    # Honour an explicit Talk route for the alert/reply purposes first.
    if user_config.routing:
        from .transport import parse_output_target
        for purpose in ("alert", "reply"):
            spec = user_config.routing.get(purpose)
            if not spec:
                continue
            for dest in parse_output_target(spec):
                if dest.surface == "talk" and dest.channel:
                    return dest.channel

    if user_config.alerts_channel:
        return user_config.alerts_channel

    for briefing in user_config.briefings:
        if briefing.conversation_token:
            return briefing.conversation_token

    # Fall back to auto-detected 1:1 DM from talk poller
    try:
        from .transport.talk import get_dm_token
        dm_token = get_dm_token(user_id)
        if dm_token:
            return dm_token
    except ImportError:
        pass

    return None


async def _send_talk(
    config: "Config", user_id: str, message: str,
    conversation_token: str | None = None,
) -> int | None:
    """Send a notification via Talk. Returns message_id on success, None on failure."""
    token = conversation_token or resolve_conversation_token(config, user_id)
    if not token:
        logger.warning("No conversation token for notification (user: %s)", user_id)
        return None

    if not config.nextcloud.url:
        logger.warning("Nextcloud not configured for notifications")
        return None

    from .transport.talk import TalkTransport
    return await TalkTransport(config).deliver(token, message)


def send_confirmation_prompt(
    config: "Config",
    user_id: str,
    message: str,
    *,
    conversation_token: str | None = None,
) -> tuple[bool, int | None]:
    """Ask the user to approve a held task, on whatever surface they read.

    Returns ``(delivered, talk_message_id)``. The Talk id is what
    ``handle_confirmation_reply``'s Path A matches a *reply* against, so it is
    handed back even though every other surface has no equivalent; it is None
    when Talk was not among the destinations or the post failed.

    Routed through the ``alert`` purpose rather than a hardwired Talk send
    (ISSUE-241). A confirmation is the one notification the user *must* see —
    the task is cancelled unanswered at ``confirmation_timeout_minutes`` and,
    for an inbound email, the message was marked processed in the same
    transaction, so a prompt nobody reads is silent mail loss. Routing it means
    a user who has pointed alerts at web or ntfy is actually asked;
    ``resolve_destinations`` still falls back to Talk, so a deployment that has
    configured nothing behaves exactly as before.

    ``conversation_token`` overrides the channel of a *bare* talk destination
    only (the caller's ``alerts_channel``), matching ``send_notification``.
    """
    dests = resolve_destinations(config, user_id, "alert")
    return _dispatch(config, user_id, message, dests,
                     conversation_token=conversation_token)


def _send_email(
    config: "Config", user_id: str, subject: str, body: str,
) -> bool:
    """Send a notification via email. Returns True on success."""
    user_config = config.users.get(user_id)
    if not user_config or not user_config.email_addresses:
        logger.warning("No email address for notification (user: %s)", user_id)
        return False

    if not config.email.enabled:
        logger.warning("Email not configured for notifications")
        return False

    try:
        from .email_support import get_email_config
        from .skills.email import send_email
        email_config = get_email_config(config)
        send_email(
            to=user_config.email_addresses[0],
            subject=subject,
            body=body,
            config=email_config,
            from_addr=config.email.bot_email,
            content_type="plain",
        )
        return True
    except Exception as e:
        logger.error("Failed to send email notification (user: %s): %s", user_id, e)
        return False


def _send_ntfy(
    config: "Config", user_id: str, message: str,
    title: str | None = None,
    priority: int | None = None,
    tags: str | None = None,
) -> bool:
    """Send a notification via the user's own ntfy server. Returns True on success.

    Thin sync shim: builds ``DeliveryOptions`` and calls the ntfy transport
    (the single ntfy delivery path) on the persistent loop via ``run_coro``.
    """
    from .async_runtime import run_coro
    from .transport._types import DeliveryOptions
    from .transport.ntfy import send_ntfy_async

    return bool(run_coro(send_ntfy_async(
        config, user_id, message,
        DeliveryOptions(title=title, priority=priority, tags=tags),
    )))


def _send_web(
    config: "Config", user_id: str, message: str,
    conversation_token: str | None = None,
    title: str | None = None,
) -> bool:
    """Post a notification into the user's web chat room. Returns True on success.

    The room token is the explicit ``web:<token>`` channel if given, else the
    user's default room. The message is appended via ``WebTransport`` as a
    ``role='system'`` row in the canonical ``messages`` store, rendered as a
    system message in the room and pushed live by the room stream.
    """
    from .async_runtime import run_coro
    from .transport._types import DeliveryOptions
    from .transport.web import WebTransport, default_web_room_token

    token = conversation_token or default_web_room_token(config, user_id)
    if not token:
        logger.warning("No web room for notification (user: %s)", user_id)
        return False
    msg_id = run_coro(WebTransport(config).deliver(
        token, message, options=DeliveryOptions(title=title),
    ))
    return msg_id is not None


# How long the transcript mirror waits for the write lock before giving up. Well
# under the 30s default, because a caller holding a transaction is a stall on
# whatever thread it runs on rather than an error anyone sees. See the note in
# `mirror_talk_to_room`.
_MIRROR_LOCK_WAIT_MS = 250


def _default_web_room(config: "Config", user_id: str) -> str | None:
    """The room a bare ``web`` destination lands on. None if the user has none."""
    from .transport.web import default_web_room_token

    try:
        return default_web_room_token(config, user_id)
    except Exception:
        return None


def _canonical_room_token(config: "Config", token: str) -> str:
    """The canonical room a Talk token belongs to, or the token unchanged.

    A promoted web room keeps its own token and binds the Talk one to it, so
    the two names for one room only compare equal after this.
    """
    from . import db

    if not config.db_path:
        return token
    try:
        with db.get_db(config.db_path, busy_timeout_ms=_MIRROR_LOCK_WAIT_MS) as conn:
            return db.resolve_room_token(conn, "talk", token) or token
    except Exception:
        return token


def strip_leading_title(message: str, title: str | None) -> str:
    """Drop a leading copy of ``title`` from a message body.

    :func:`istota.notification_store._delivery_text` composes the delivered text
    as the title, a blank line, then the body, and it has to: Talk takes no title
    argument, so the first line of the message is the only place a Talk reader
    ever sees the label. Every *other* surface in :func:`_dispatch` takes ``title``
    of its own — an email subject, an ntfy header, the ``messages.title`` column
    the web transcript renders above the body — so the composed string reaches
    them with the label twice, once as a heading and once as the opening line
    (ISSUE-311).

    Anchored on the whole title followed by the blank line, so a body that merely
    opens with similar words is left alone. The message is returned unchanged
    when what is left would be empty or blank — ``_delivery_text`` returns the
    bare title when the body is empty, and an empty notification is worse than a
    repeated label.

    **Two prefix spellings, and deliberately only two.** Producers that compose
    their own delivery text rather than going through ``_delivery_text`` bold the
    label for Talk, which renders markdown — ``heartbeat.send_heartbeat_alert``
    is the one in the tree. Its alert fires on a schedule, so missing it would
    leave the highest-volume producer showing the defect this function exists to
    remove. This is a closed list of exact comparisons, not markdown parsing: if
    a third spelling ever appears, add it here rather than reaching for a regex,
    and prefer changing the producer to compose the plain form.
    """
    if not title or not message:
        return message
    for prefix in (f"{title}\n\n", f"**{title}**\n\n"):
        if message.startswith(prefix):
            remainder = message[len(prefix):]
            return remainder if remainder.strip() else message
    return message


def mirror_talk_to_room(
    config: "Config", token: str, message: str,
    *, title: str | None = None, talk_message_id: int | None = None,
) -> None:
    """Record a Talk-delivered notification in that room's web transcript.

    A Talk room and its web view are one room with one token, so a post made
    only to Talk left the web view of the same conversation blank (ISSUE-242).
    Task turns already cross over (``scheduler._store_room_turn``); this closes
    the same gap for everything that isn't a task turn — alerts, heartbeat
    notices, reminders, and the inbound-email confirmation prompt, where the
    gap is silent mail loss because the task auto-cancels at
    ``confirmation_timeout_minutes`` unanswered.

    ``role='system'`` is what the ``web`` leg already writes, so it renders
    identically, rides the live room stream and counts toward unread — no new
    render path. ``origin_surface='talk'`` is provenance only:
    ``list_system_messages`` filters on ``role`` alone, so the value does not
    gate visibility, but it makes the row's origin legible where ``'web'``
    would not.

    The gate is **room existence**, not surface config — the same rule
    ``_store_room_turn`` uses. A synthetic email-thread token, or a Talk room
    never registered, no-ops rather than minting a room. Best-effort
    throughout: the Talk delivery has already happened, and a failed transcript
    row must not report the notification as undelivered.

    **The short busy timeout is the load-bearing part.** This opens a *second*
    connection to a database a caller may already hold a write transaction on —
    the hazard the ``web`` leg has always had, and which ``_deliver_dmarc_alerts``
    / ``_deliver_confirmation_prompts`` / ``run_cleanup_checks`` are structured
    around. Making the ``talk`` leg a writer too extends that hazard to every
    remaining ``send_notification`` call site, and the ones reached from inside a
    transaction are not all worth restructuring (the sleep cycle holds one write
    transaction for a whole nightly pass and calls this from deep inside it). At
    the default 30s lock wait, such a caller stalls for 30s per notification —
    on the dispatch thread, where six of them trip the loop-stall watchdog. Failing
    fast instead turns the worst case into a *dropped mirror*, which is the right
    trade: the notification itself has already been delivered to Talk, and the
    mirror is a convenience for the web reader. The confirmation prompt, the one
    case where the mirror really matters, is delivered outside any transaction.
    """
    from . import db

    if not config.db_path:
        return
    try:
        with db.get_db(config.db_path, busy_timeout_ms=_MIRROR_LOCK_WAIT_MS) as conn:
            # A per-surface ref maps to the canonical room: a *promoted* web
            # room's Talk token is not its own token, so writing the raw one
            # would silently find no room and drop the mirror.
            room_token = db.resolve_room_token(conn, "talk", token) or token
            if db.get_room(conn, room_token) is None:
                return
            db.add_message(
                conn, room_token, role="system", body=message,
                origin_surface="talk", title=title,
                external_ids=(
                    {"talk": str(talk_message_id)} if talk_message_id else None
                ),
            )
            conn.commit()
    except Exception:
        logger.warning(
            "Talk→room transcript mirror failed for %r", token, exc_info=True,
        )


def is_channel_configured(
    config: "Config",
    user_id: str,
    surface: str,
    *,
    conversation_token: str | None = None,
) -> bool:
    """Probe: does the user have this notification channel set up?

    Distinguishes "user hasn't configured this channel" from "tried to send
    and the network/server failed". Heartbeat uses this to avoid bumping
    ``consecutive_errors`` when a check is misconfigured (e.g. ``channel =
    "ntfy"`` but the user never set their ntfy topic).

    Compound surfaces (``both``, ``all``) are configured if **any** of
    their leaf channels are.
    """
    from .transport import parse_output_target

    user_config = config.users.get(user_id)

    def _talk_ok() -> bool:
        if not config.nextcloud.url:
            return False
        if conversation_token:
            return True
        return resolve_conversation_token(config, user_id) is not None

    def _email_ok() -> bool:
        return bool(
            config.email.enabled
            and user_config
            and user_config.email_addresses
        )

    def _ntfy_ok() -> bool:
        return _ntfy_settings(config, user_id) is not None

    def _web_ok() -> bool:
        # Web chat is always-on; a user always has (or auto-provisions) a room.
        from .transport.web import default_web_room_token
        return default_web_room_token(config, user_id) is not None

    probes = {"talk": _talk_ok, "email": _email_ok, "ntfy": _ntfy_ok, "web": _web_ok}
    dests = parse_output_target(surface)
    if not dests:
        return False
    # Compound surfaces (both/all) are configured if ANY leaf is.
    return any(probes.get(d.surface, lambda: False)() for d in dests)


def _dispatch(
    config: "Config",
    user_id: str,
    message: str,
    dests,
    *,
    conversation_token: str | None = None,
    title: str | None = None,
    priority: int | None = None,
    tags: str | None = None,
) -> tuple[bool, int | None]:
    """Deliver ``message`` to every resolved destination.

    Returns ``(sent, talk_message_id)`` — the second value only for callers that
    need to address the posted message later (the confirmation prompt, whose
    Talk id is how a *reply* to it is matched). One loop, shared by
    :func:`send_notification` and :func:`send_confirmation_prompt`, so the two
    cannot disagree about what a destination list means.
    """
    from .async_runtime import run_coro

    sent = False
    talk_message_id: int | None = None
    # Rooms a `web` destination will write into anyway — a route naming both
    # legs of one room must not produce two rows for one message. Both sides are
    # resolved to the *canonical* room before comparing, because neither raw
    # token is the room: a bare `web` leaf carries no channel at all and lands
    # on the user's default room, and a promoted room's Talk token differs from
    # its own. Comparing the raw values missed both.
    web_rooms = {
        _canonical_room_token(config, t)
        for t in (
            d.channel or _default_web_room(config, user_id)
            for d in dests if d.surface == "web"
        )
        if t
    }
    # `message` keeps the title prefix for Talk, which has no title field of its
    # own; `body` is the same text for the surfaces that render `title`
    # separately and would otherwise print the label twice (ISSUE-311).
    body = strip_leading_title(message, title)
    for dest in dests:
        if dest.surface == "talk":
            # Resolved here, not left to `_send_talk`, because the mirror needs
            # the token a bare `talk` destination actually lands on.
            token = (
                dest.channel or conversation_token
                or resolve_conversation_token(config, user_id)
            )
            msg_id = run_coro(_send_talk(config, user_id, message, token))
            if msg_id:
                sent = True
                if talk_message_id is None:
                    talk_message_id = msg_id
                if token and _canonical_room_token(config, token) not in web_rooms:
                    # The mirror writes `title` into its own column, so it takes
                    # the body — even though what it is mirroring is the Talk
                    # message, which kept the prefix.
                    mirror_talk_to_room(
                        config, token, body,
                        title=title, talk_message_id=msg_id,
                    )
        elif dest.surface == "email":
            if _send_email(config, user_id, title or "Notification", body):
                sent = True
        elif dest.surface == "ntfy":
            if _send_ntfy(config, user_id, body, title=title, priority=priority, tags=tags):
                sent = True
        elif dest.surface == "web":
            # A bare `web` route carries no channel; the explicit conversation_token
            # override only applies to bare `talk`, so pass the descriptor channel.
            if _send_web(config, user_id, body, dest.channel, title=title):
                sent = True
        else:
            logger.warning(
                "Unsupported notification surface %r (user: %s)",
                dest.surface, user_id,
            )
    return sent, talk_message_id


def send_notification(
    config: "Config",
    user_id: str,
    message: str,
    *,
    surface: str | None = None,
    purpose: str | None = None,
    conversation_token: str | None = None,
    title: str | None = None,
    priority: int | None = None,
    tags: str | None = None,
) -> bool:
    """Send a notification via an explicit surface or the user's routing table.

    Destination resolution:
      1. ``surface`` if given — an ``output_target`` descriptor ("talk", "email",
         "ntfy", "all", "talk:<token>", or a comma list). An explicit surface
         always wins (e.g. a heartbeat check's own channel).
      2. else ``purpose`` (one of :data:`PURPOSES`) resolved through the user's
         per-user routing table via :func:`resolve_destinations` — this is what
         makes ``routing={"alert": "ntfy"}`` actually route alerts to ntfy.
      3. else bare ``talk``.

    Args:
        conversation_token: Talk room override for any *bare* talk destination
            (``talk`` with no explicit ``:token``); an explicit ``talk:<token>``
            in the descriptor (or a routed channel) keeps its own channel.
    """
    from .transport import parse_output_target

    if surface is not None:
        dests = parse_output_target(surface)
    elif purpose is not None:
        dests = resolve_destinations(config, user_id, purpose)
    else:
        dests = parse_output_target("talk")

    sent, _talk_msg_id = _dispatch(
        config, user_id, message, dests,
        conversation_token=conversation_token,
        title=title, priority=priority, tags=tags,
    )

    if not sent:
        logger.warning(
            "Notification not delivered (user: %s, surface: %s, purpose: %s)",
            user_id, surface, purpose,
        )

    return sent


# ---------------------------------------------------------------------------
# Operator alerts
# ---------------------------------------------------------------------------
# Operator-level alerts go to a single recipient (first admin, else first
# configured user) over the ``alert`` routing purpose. ``send_operator_alert``
# runs the send on a short-lived daemon thread with a join timeout so a wedged
# Talk/Nextcloud can't stall the caller — the scheduler main loop, the sleep
# cycle, shared-block generation, and the backup/loop watchdogs all share this.
# ISSUE-143 class: a degraded brain is exactly when Talk is likely also down.


def operator_alert_user(config: "Config") -> str | None:
    """Pick the user to receive operator-level alerts.

    Prefers the first admin user (sorted for determinism); falls back to the
    first configured user. ``None`` when no users are configured.
    """
    admins = getattr(config, "admin_users", None)
    if admins:
        return sorted(admins)[0]
    users = getattr(config, "users", None)
    if users:
        return sorted(users)[0]
    return None


def send_operator_alert(
    config: "Config", message: str, *, timeout: float = 30.0
) -> None:
    """Send an operator alert without letting a hung delivery stall the caller.

    Picks the recipient via :func:`operator_alert_user` and dispatches through
    :func:`send_notification` (``purpose="alert"``) on a short-lived daemon
    thread, waiting at most ``timeout``. If the send is still running after the
    timeout it is left to finish (or die) in the background — a wedged Talk must
    never block the scheduler main loop or a sleep-cycle pass (ISSUE-143 class).
    """
    user_id = operator_alert_user(config)
    if not user_id:
        logger.warning("operator_alert_no_recipient — message not sent: %s", message)
        return

    def _do() -> None:
        try:
            send_notification(config, user_id, message, purpose="alert")
        except Exception as exc:  # noqa: BLE001
            logger.error("operator_alert_failed err=%s", exc)

    t = threading.Thread(target=_do, name="operator-alert", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        logger.error(
            "operator_alert_timed_out after %ss — send still running in background",
            timeout,
        )
