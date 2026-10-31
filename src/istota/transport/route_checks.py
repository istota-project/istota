"""Save-time checks on a delivery route a user picked: which rooms and surfaces
a descriptor or a briefing's ``conversation_token`` may name.

Delivery-time routing is `transport.routing` (`phone_room`,
`refuse_shared_rooms`); this module answers the earlier question, whether a
user may save a route at all. The web profile PUT and the briefing POST reach
it through thin wrappers in `webui.app`, and the briefings_config skill calls
it directly, so the two cannot drift. Each function takes ``config`` rather
than reading a module global; a ``None`` config, or one with no database,
checks nothing, as the web app did before its config loaded.

`validate_briefing_schedule` raises `RouteRefused` for a room or surface
refusal and a plain `ValueError` for a malformed schedule, so a caller can
tell the two apart. `RouteRefused` is a `ValueError`, so a caller that does
not care sees one exception.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class RouteRefused(ValueError):
    """A route refused because of the room or surface it names."""


BUILTIN_DELIVERY_SURFACES = frozenset({
    "talk", "email", "ntfy", "istota_file", "stream",
})


def user_talk_channels(config, user_id: str) -> list[tuple[str, str]]:
    """The user's two provisioned Talk channels as ``(label, token)``, in the
    order the picker lists them.

    The profile row first: it is what a save writes, so reading ``config.users``
    alone would offer a channel one save out of date. The config user is the
    fallback for a user who has one from config.toml and no row yet.

    A caveat that belongs to the whole pair rather than to this function.
    ``config.users`` is a snapshot that each process re-applies from
    ``user_profiles`` when the table changes (the scheduler each loop tick, the
    web app at most once a second), so a channel changed through the web API is
    live here and briefly behind in ``notifications.resolve_destinations``.
    Reading the row is still the right answer for a picker, which must show
    what the user just saved. A pinned ``talk:<token>`` goes through the
    descriptor path and is unaffected.
    """
    from istota import user_profiles

    uc = config.users.get(user_id) if config else None
    profile = None
    if config and config.db_path:
        try:
            profile = user_profiles.get_profile(config.db_path, user_id)
        except Exception as e:  # pragma: no cover - defensive, mirrors the list
            logger.warning("profile lookup failed for user %s: %s", user_id, e)

    def pick(field: str) -> str:
        return (getattr(profile, field, "") or getattr(uc, field, "") or "").strip()

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, field in (
        ("Alerts channel", "alerts_channel"),
        ("Logs channel", "log_channel"),
    ):
        token = pick(field)
        if token and token not in seen:
            out.append((label, token))
            seen.add(token)
    return out


def talk_member_refs(conn, user_id: str) -> dict[str, str]:
    """Talk conversation ref -> display name, for the Talk-bound rooms
    ``user_id`` is a member of.

    **Raises rather than swallowing.** This is the query the authorization check
    reads, and a lookup that fails has not established that a conversation is
    somebody else's — returning a short list there would refuse a legitimate
    save with a 400 blaming the user for a database problem. The rendering
    wrapper `webui.app._user_talk_rooms` is the one that degrades, which is the
    opposite policy and deliberately so.

    Keyed by the ``talk`` binding's ``surface_ref`` rather than by the canonical
    room token, because that ref is the Nextcloud conversation id a ``talk:``
    leaf carries; on a promoted room the two differ.

    An archived room is dropped, mirroring the web list — `archive_orphaned_talk_rooms`
    sets that flag when the bot leaves a conversation, so its binding names a
    room that is dead on Talk and every delivery to it would 404.
    """
    from istota import db

    out: dict[str, str] = {}
    for room_token, talk_ref in db.talk_refs_for_member(conn, user_id).items():
        if not talk_ref:
            continue
        room = db.get_room(conn, room_token)
        if room is not None and room.archived:
            continue
        out.setdefault(talk_ref, db.room_display_name(room, None) or talk_ref)
    return out


def talk_route_tokens(config, user_id: str) -> set[str]:
    """The Talk conversations a ``talk:<token>`` route may name — the
    authorization set behind `validate_descriptor_rooms`.

    The user's own provisioned channels plus `talk_member_refs`. Including the
    channels is only safe because writing them through the web API is itself
    checked against the *registry* set alone (`webui.app._validate_talk_channel`):
    were it not, a caller could `PUT {"alerts_channel": "<somebody else's token>"}`
    and then pin a route to it, which is the whole check bypassed in two
    requests. That is also why the channel write does not consult this
    function — the two would authorize each other.
    """
    from istota import db

    tokens = {token for _, token in user_talk_channels(config, user_id)}
    if config is None or not config.db_path:
        return tokens
    with db.get_db(config.db_path) as conn:
        tokens |= set(talk_member_refs(conn, user_id))
    return tokens


def validate_descriptor_rooms(config, descriptor: str, user_id: str) -> None:
    """Raise ValueError if a ``web:`` or ``talk:`` leaf names a room ``user_id``
    is not a member of.

    A room token is not a secret — the room settings pane offers a copy button —
    and `WebTransport.deliver` checks only that the room exists, so without this
    a saved route is a standing write into any transcript whose token the caller
    has seen, on every alert (ISSUE-473). Membership rather than ownership; but
    a room another human reads is refused all the same
    (`refuse_shared_destination`), since delivery drops personal content there
    and a pin onto one would save and then deliver nothing.

    **This is an authorization gate, and it is deliberately looser than the
    offered list** (ISSUE-478). `webui.app._user_web_rooms` also drops a room
    that is archived, deleted or dismissed; that is a liveness question rather
    than a permission one, and a 400 out of this function would report it as
    the latter. Nothing is lost by leaving it out: the picker can no longer
    offer such a room, so a save cannot newly pin one from the UI, and an
    existing pin is not re-judged either way — `coerce_profile_field` validates
    a descriptor only when it differs from the stored one, per purpose. What the
    user gets instead is the `(unavailable)` mark on the pinned option, driven
    by `_unavailable_web_room_pins`, which says the thing a 400 here would have
    said at the point where it is true and where it can name the room.

    Talk is checked on the same footing since ISSUE-475, because promoting
    ``talk:<token>`` to a dropdown is what promoted it to a setting a user can
    reach. The bot is a participant in every user's conversations, so a Talk
    token names a transcript the bot can post to whether or not the caller has
    any part in it — the same standing write, on the surface with more rooms in
    it. What differs is the registry consulted: a ``web`` token is a row in the
    room table, a ``talk`` one is a Nextcloud conversation id reached through a
    room's ``talk`` binding, which is why the two ask different questions rather
    than sharing one. ``ntfy`` / ``email`` carry no room at all.

    The Talk set comes from `talk_route_tokens`, which `_user_talk_rooms`
    renders, so the offered list and the accepted set cannot drift.

    The check is on the *web API* rather than in the descriptor grammar: an
    operator setting a token through ``istota user ensure`` or config.toml is
    trusted, and is also the one who would be repairing a room the user cannot
    reach. That trust is preserved on the way through by the caller rather than
    here — `coerce_profile_field` skips a descriptor identical to the stored
    one, so a value only an operator could have set is never re-judged by a save
    that did not touch it. It has to be the caller's job because ``routing`` is
    one field carrying three descriptors: the settings page diffs top-level
    keys, so editing the alert route ships the log route with it, and without
    that skip an operator-set log route would refuse the user's own unrelated
    edit with no way to clear it from the UI.
    """
    from istota import db
    from istota.transport import parse_output_target

    dests = parse_output_target(descriptor)
    web_tokens = [d.channel for d in dests if d.surface == "web" and d.channel]
    talk_tokens = [d.channel for d in dests if d.surface == "talk" and d.channel]
    if config is None or not config.db_path:
        return

    for token in talk_tokens:
        validate_talk_route_token(config, token, user_id)

    if not web_tokens:
        return
    with db.get_db(config.db_path) as conn:
        for token in web_tokens:
            if not db.is_room_member(conn, token, user_id):
                raise ValueError(f"web room {token!r} is not one of your rooms")
            refuse_shared_destination(conn, token, f"web room {token!r}")


def refuse_shared_destination(conn, room_token: str | None, what: str) -> None:
    """Raise ValueError when ``room_token`` names a room more than one human reads.

    Delivery refuses such a room for personal content (speech-gate B4,
    `routing.refuse_shared_rooms`), so a setting naming one would save and then
    deliver nothing. Refused at save instead, where the user can see why. A
    stored pin that later becomes shared is not re-judged, because every caller
    skips a value identical to the stored one (the profile PUT and the briefing
    upsert both); the picker marks it and delivery drops it.
    """
    from istota import db

    if room_token and db.room_is_incognito(conn, room_token):
        raise ValueError(f"{what} is incognito, and deliveries never go there")
    if room_token and db.room_is_shared(conn, room_token):
        raise ValueError(
            f"{what} is shared with other people, and personal deliveries never go there"
        )


def talk_ref_room(conn, talk_ref: str) -> str | None:
    """The room a Talk conversation ref is bound to, or None for one the
    registry does not hold (a provisioned channel the bot made on Nextcloud)."""
    from istota import db

    return db.resolve_room_token(conn, "talk", talk_ref) or (
        talk_ref if db.get_room(conn, talk_ref) is not None else None
    )


def validate_talk_route_token(config, token: str, user_id: str) -> None:
    """Raise ValueError if ``token`` is not a Talk conversation ``user_id`` may
    deliver to.

    The check behind a ``talk:<token>`` descriptor leaf and behind a briefing's
    ``conversation_token``, which are two spellings of one question — where on
    Talk this delivery lands — and so read one set, `talk_route_tokens`.
    """
    if config is None or not config.db_path or not token:
        return
    if token not in talk_route_tokens(config, user_id):
        raise ValueError(
            f"Talk conversation {token!r} is not one of your conversations"
        )
    from istota import db

    with db.get_db(config.db_path) as conn:
        refuse_shared_destination(
            conn, talk_ref_room(conn, token), f"Talk conversation {token!r}",
        )


def validate_descriptor_surfaces(config, descriptor: str) -> None:
    """Raise ValueError if any leaf surface in a descriptor is neither a builtin
    surface nor a registered transport.

    Builtin surfaces (talk/email/ntfy/istota_file/stream) are always accepted
    even when disabled at the instance level — a user may route to email before
    the operator enables it. Only genuinely-unknown surfaces (typos, an
    unregistered Matrix) are rejected."""
    from istota.transport import make_registry, parse_output_target
    known = set(BUILTIN_DELIVERY_SURFACES)
    if config is not None:
        known |= set(make_registry(config).names())
    for dest in parse_output_target(descriptor):
        if dest.surface not in known:
            raise ValueError(f"unknown delivery surface: {dest.surface}")


def validate_briefing_schedule(
    config, payload: dict, *, user_id: str, stored=None, name_required: bool,
) -> dict:
    """Common shape check for a briefing schedule upsert, from the web POST and
    the briefings_config skill alike.

    Returns the cleaned dict. Raises `RouteRefused` when the output or token
    names a room or surface the user may not deliver to, and `ValueError` for
    any other bad input.

    ``user_id`` is here for the room checks. A briefing is a delivery route on a
    cron schedule, so an unchecked ``output`` or ``conversation_token`` is the
    same standing write ISSUE-473 closed for the alert route, repeating on a
    timer — and it sits one row below it on the same settings page, which is
    where the asymmetry would have been most visible (ISSUE-475).
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")

    name = (payload.get("name") or "").strip()
    if name_required and not name:
        raise ValueError("name is required")

    cron = (payload.get("cron") or "").strip()
    if not cron:
        raise ValueError("cron is required")

    # Blank is meaningful: it means "derive the title from the name".
    title = (payload.get("title") or "").strip()
    if len(title) > 200:
        raise ValueError("title must be 200 characters or fewer")
    if any(ord(c) < 32 for c in title):
        raise ValueError("title must not contain control characters")

    output = (payload.get("output") or "talk").strip()
    # Validate every leaf surface is known (rejects typos like "sms"); the
    # grammar stays permissive so legacy ``both`` / comma lists still parse,
    # while the UI offers only ``_registered_delivery_surfaces()``.
    # ``stored`` is the briefing this upsert replaces, if any. A room value it
    # already held is not re-judged, the rule `coerce_profile_field` follows:
    # a pin onto a room that became shared since must not stop the user
    # disabling or rescheduling the briefing it sits on.
    try:
        validate_descriptor_surfaces(config, output)
        if stored is None or output != stored.output:
            validate_descriptor_rooms(config, output, user_id)
    except ValueError as exc:
        raise RouteRefused(str(exc)) from exc

    from istota.transport import parse_output_target
    token = (payload.get("conversation_token") or "").strip()
    talk_leaf = any(d.surface == "talk" for d in parse_output_target(output))
    if talk_leaf and not token:
        raise ValueError(f"conversation_token is required when output is {output!r}")
    # The token is a second way to name the room, so it takes the same check the
    # descriptor's own `talk:<token>` leaf takes — otherwise a bare `talk`
    # output plus a token is the unguarded path around it.
    try:
        if stored is None or token != stored.conversation_token:
            validate_talk_route_token(config, token, user_id)
    except ValueError as exc:
        raise RouteRefused(str(exc)) from exc

    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")

    return {
        "name": name,
        "cron": cron,
        "title": title,
        "conversation_token": token,
        "output": output,
        "enabled": enabled,
    }
