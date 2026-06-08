"""Centralized notification dispatcher for Talk, Email, and ntfy.

Dispatch is driven through the transport routing helpers: a ``surface`` string
is an ``output_target`` descriptor (``talk`` / ``email`` / ``ntfy`` / ``both``
/ ``all`` / ``talk:<token>`` / comma lists), parsed into destinations and
looped — there is no per-surface ``if surface == "both"`` chain. The actual
ntfy POST lives in ``transport.ntfy`` (the single ntfy delivery path); the
``_send_ntfy`` shim here just adapts the sync notification signature to it.
"""

import logging
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


def send_talk_confirmation(
    config: "Config",
    user_id: str,
    message: str,
    conversation_token: str | None = None,
) -> int | None:
    """Send a confirmation prompt via Talk (sync). Returns message_id or None."""
    from .async_runtime import run_coro
    return run_coro(_send_talk(config, user_id, message, conversation_token))


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
    user's default room. The message is appended via ``WebTransport`` (a
    ``web_chat_messages`` row) and rendered as a system message in the room.
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
    from .async_runtime import run_coro
    from .transport import parse_output_target

    if surface is not None:
        dests = parse_output_target(surface)
    elif purpose is not None:
        dests = resolve_destinations(config, user_id, purpose)
    else:
        dests = parse_output_target("talk")

    sent = False
    for dest in dests:
        if dest.surface == "talk":
            token = dest.channel or conversation_token
            if run_coro(_send_talk(config, user_id, message, token)):
                sent = True
        elif dest.surface == "email":
            if _send_email(config, user_id, title or "Notification", message):
                sent = True
        elif dest.surface == "ntfy":
            if _send_ntfy(config, user_id, message, title=title, priority=priority, tags=tags):
                sent = True
        elif dest.surface == "web":
            # A bare `web` route carries no channel; the explicit conversation_token
            # override only applies to bare `talk`, so pass the descriptor channel.
            if _send_web(config, user_id, message, dest.channel, title=title):
                sent = True
        else:
            logger.warning(
                "Unsupported notification surface %r (user: %s)",
                dest.surface, user_id,
            )

    if not sent:
        logger.warning(
            "Notification not delivered (user: %s, surface: %s, purpose: %s)",
            user_id, surface, purpose,
        )

    return sent
