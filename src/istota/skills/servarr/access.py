"""Who may use the servarr skill, and with which verbs and flags.

The caller is ``ISTOTA_USER_ID`` from the CLI's environment, which the daemon
builds for the skill proxy and the model cannot set (on the supported shape:
Linux, bubblewrap, the skill proxy on). This module does not read config or the
user id itself: it starts from `skills._load_context()` and
`_guard_skill("servarr", ctx)`, the context every gated skill CLI uses, so an
operator's ``disabled_skills``, a per-user disable and the capability gate
refuse the CLI too.

Three tiers. ``admin`` (`config.is_admin`) gets every verb and flag;
``requester`` (listed in ``[servarr] requesters``) may ask for things and see
what is happening to them; ``none`` gets nothing. The room caps the tier: a
turn with any withheld scope gets ``none``, and a turn in an email-thread room
gets ``requester`` at most and may not pass ``--confirmed``, because an
admitted correspondent's mail runs as the host.

The table lives here and nowhere else: `VERB_TIERS` and `FLAG_TIERS`, plus
`KIND_TIERS` for the one value-shaped rule (a whole-discography add). The
dispatcher checks it after argv parses and before any app is touched.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .client import ServarrError

ADMIN = "admin"
REQUESTER = "requester"
NONE = "none"
_RANK = {NONE: 0, REQUESTER: 1, ADMIN: 2}

#: The minimum tier for each verb.
VERB_TIERS: dict[str, str] = {
    "apps": REQUESTER,
    "status": REQUESTER,
    "describe": ADMIN,
    "search": REQUESTER,
    "add": REQUESTER,
    "queue": REQUESTER,
    "why": REQUESTER,
    "wanted": ADMIN,
    "search-now": REQUESTER,
    "requests": REQUESTER,
    "indexers": ADMIN,
}

#: The minimum tier for a flag on a verb, beyond the verb's own. A requester
#: adds with the stack's defaults: the profile and root folder are the stack's.
FLAG_TIERS: dict[tuple[str, str], str] = {
    ("add", "--profile"): ADMIN,
    ("add", "--root"): ADMIN,
    ("add", "--metadata-profile"): ADMIN,
    ("indexers", "--test"): ADMIN,
}

#: A value of `--kind` that needs more than the verb does: a whole discography.
KIND_TIERS: dict[tuple[str, str], str] = {
    ("add", "artist"): ADMIN,
}


@dataclass(frozen=True)
class Tier:
    name: str
    message: str = ""
    email_thread: bool = False


class _InstallConnection:
    """The one connection v1 has. A user's own connection, in the follow-up
    spec, gets its own rule in `resolve_tier` without moving the call sites."""


INSTALL = _InstallConnection()


def load_context():
    """`skills._load_context()`, refusing first when there is no user id.

    `_load_context` reports a missing id with its own envelope; the skill's
    answer to "who is calling" being nobody is ``not_authorized``, never a
    default user.
    """
    if not os.environ.get("ISTOTA_USER_ID", "").strip():
        raise ServarrError(
            "No user is attached to this call (ISTOTA_USER_ID is unset), so nothing "
            "was sent.", "not_authorized",
        )
    from istota.skills.skills import _load_context

    return _load_context()


def resolve_tier(ctx: dict, connection=INSTALL) -> Tier:
    """``admin``, ``requester`` or ``none`` for this caller on `connection`."""
    from istota.skills.skills import _guard_skill

    refusal = _guard_skill("servarr", ctx)
    if refusal:
        return Tier(NONE, f"The servarr skill is not available here: {refusal}.")
    if ctx["withheld"]:
        return Tier(NONE, "The arr stack is not reachable from this turn: the room "
                          "withholds the caller's scopes (a guest's turn, or a task "
                          "nobody asked in a shared room).")

    config = ctx["config"]
    user_id = ctx["user_id"].strip()
    if ctx["is_admin"]:
        base = ADMIN
    elif user_id.casefold() in {str(r).strip().casefold() for r in config.servarr.requesters}:
        base = REQUESTER
    else:
        return Tier(NONE, "The operator has not given this user access to the arr stack "
                          "(they are neither an admin nor in [servarr] requesters).")

    # A `!command` names its room in the context; a CLI run has it in its env.
    token = ctx.get("conversation_token")
    if token is None:
        token = os.environ.get("ISTOTA_CONVERSATION_TOKEN", "")
    if _in_email_thread(config, token.strip()):
        return Tier(REQUESTER if base == ADMIN else base, email_thread=True)
    return Tier(base)


def _in_email_thread(config, token: str) -> bool:
    """Whether the room `token` names is an email thread; any failure says yes."""
    if not token:
        return False
    try:
        from pathlib import Path

        from istota import db
        from istota.rooms import scopes

        if not Path(config.db_path).exists():
            return True
        with db.get_db(config.db_path) as conn:
            return bool(scopes.is_email_thread_room(conn, token))
    except Exception:  # noqa: BLE001 — fail closed: treat the room as a thread
        return True


def allows(tier: str, minimum: str) -> bool:
    return _RANK[tier] >= _RANK[minimum]


def check(verb: str, args, tier: Tier) -> None:
    """Refuse the call when the tier does not reach the verb, a flag or a kind."""
    if tier.name == NONE:
        raise ServarrError(tier.message or "Not authorized; nothing was sent.",
                           "not_authorized")
    if tier.email_thread and getattr(args, "confirmed", False):
        raise ServarrError(
            "--confirmed is not accepted in an email thread: a correspondent's mail "
            "runs as the host there, so the agreement could be theirs. Nothing was sent.",
            "confirmation_not_allowed_here",
        )
    if not allows(tier.name, VERB_TIERS[verb]):
        raise ServarrError(
            f"{verb} is for admins only; this user has the {tier.name} tier. Nothing was "
            f"sent.", "verb_requires_admin", verb=verb,
        )
    for flag in passed_flags(verb, args):
        if not allows(tier.name, FLAG_TIERS[(verb, flag)]):
            raise ServarrError(
                f"{flag} is for admins only; requests use the stack's own defaults. "
                f"Nothing was sent.", "flag_requires_admin", flag=flag,
            )
    kind = getattr(args, "kind", None)
    minimum = KIND_TIERS.get((verb, kind))
    if minimum is not None and not allows(tier.name, minimum):
        raise ServarrError(
            f"{verb} --kind {kind} is for admins only. Nothing was sent.",
            "flag_requires_admin", flag=f"--kind {kind}",
        )


def passed_flags(verb: str, args) -> list[str]:
    """The tiered flags of `verb` this call actually set."""
    out = []
    for (flag_verb, flag) in FLAG_TIERS:
        if flag_verb != verb:
            continue
        value = getattr(args, flag.lstrip("-").replace("-", "_"), None)
        if value not in (None, False):
            out.append(flag)
    return out


def refused_flags(tier: str) -> list[str]:
    """What `apps` lists as closed to this tier, as ``verb --flag``."""
    out = [f"{verb} {flag}" for (verb, flag), minimum in FLAG_TIERS.items()
           if allows(tier, VERB_TIERS[verb]) and not allows(tier, minimum)]
    out += [f"{verb} --kind {kind}" for (verb, kind), minimum in KIND_TIERS.items()
            if allows(tier, VERB_TIERS[verb]) and not allows(tier, minimum)]
    return out


def allowed_verbs(tier: str) -> list[str]:
    return [verb for verb, minimum in VERB_TIERS.items() if allows(tier, minimum)]
