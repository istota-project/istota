"""The ``skill:<skill>.<op>`` capabilities: six read-only operations, each with typed arguments.

An app never names a command. It names one operation from :data:`SKILL_CAPS`,
whose keys are ``capabilities.SKILL_OPERATIONS``, and passes arguments that
:func:`check_args` holds to the operation's schema: no key the schema does not
name, each value of its declared kind and size. The handler then calls the
library function the skill's own CLI calls, host-side, as the user:

- ``markets.quote``: ``skills.markets.get_quotes``;
- ``health.trend``: ``health.db.find_biomarker_ref_by_alias`` and
  ``biomarker_trend``, on the user's health database;
- ``location.current`` and ``location.places``: ``location.logic``'s two, on
  the user's location database;
- ``memory_search.facts`` and ``memory_search.timeline``:
  ``memory.knowledge_graph.get_current_facts`` and ``get_entity_timeline``, on
  the framework database, scoped by user id.

A handler whose module is off for the user (health, location), or whose data
or library is not there, raises :class:`Unavailable`; bad arguments raise
:class:`BadArgs`. The bridge maps the two to ``not_found`` and ``bad_args``,
and checks the grant and the user's disabled skills before any of this runs.
Nothing here writes: a missing module database is an empty answer, not one
created for the call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Callable

#: The most symbols one ``markets.quote`` call fetches, one request each.
SYMBOLS_MAX = 20
#: The most facts one ``memory_search.facts`` or ``timeline`` call returns.
FACTS_MAX = 5000
_SYMBOL = re.compile(r"[A-Za-z0-9^=.\-]{1,20}")


class BadArgs(ValueError):
    """Arguments the operation's schema refuses, with a sentence saying why."""


class Unavailable(LookupError):
    """The operation has nothing to answer from for this user."""


@dataclass(frozen=True)
class Arg:
    """One argument: ``text`` (a one-line string), ``date`` (``YYYY-MM-DD``) or ``symbols``."""

    kind: str
    required: bool = False
    max_chars: int = 200


@dataclass(frozen=True)
class SkillCap:
    skill: str
    arg_schema: dict[str, Arg]
    handler: Callable[[object, str, dict], object]
    sensitive: bool = False


def _text(name: str, value: object, spec: Arg) -> str:
    if (
        not isinstance(value, str) or not value.strip() or len(value) > spec.max_chars
        or any(ord(c) < 0x20 for c in value)
    ):
        raise BadArgs(f"{name} must be one line of text, at most {spec.max_chars} characters.")
    return value


def _date(name: str, value: object) -> str:
    if not isinstance(value, str) or len(value) != 10:
        raise BadArgs(f"{name} must be a date written YYYY-MM-DD.")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise BadArgs(f"{name} must be a date written YYYY-MM-DD.") from None
    return value


def _symbols(name: str, value: object) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= SYMBOLS_MAX:
        raise BadArgs(f"{name} must be a list of 1 to {SYMBOLS_MAX} symbols.")
    for symbol in value:
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            raise BadArgs(f"{name} holds something that is not a market symbol.")
    return list(value)


def check_args(schema: dict[str, Arg], args: dict) -> dict:
    """``args`` held to ``schema``: the checked values by name, or :class:`BadArgs`."""
    unknown = sorted(set(args) - set(schema))
    if unknown:
        raise BadArgs(f"This operation takes no argument {unknown[0]!r}.")
    values = {}
    for name, spec in schema.items():
        if name not in args:
            if spec.required:
                raise BadArgs(f"This operation needs {name}.")
            continue
        value = args[name]
        if spec.kind == "text":
            values[name] = _text(name, value, spec)
        elif spec.kind == "date":
            values[name] = _date(name, value)
        elif spec.kind == "symbols":
            values[name] = _symbols(name, value)
        else:
            raise ValueError(f"unknown argument kind {spec.kind!r}")
    return values


# -- the handlers -----------------------------------------------------------------


def _markets_quote(config, user_id: str, values: dict) -> dict:
    try:
        import yfinance  # noqa: F401 - get_quotes answers [] without it; say why instead
    except ImportError:
        raise Unavailable("Market quotes are not available on this server.") from None
    from istota.skills.markets import get_quotes, quote_to_dict

    return {"quotes": [quote_to_dict(q) for q in get_quotes(values["symbols"])]}


def _health_trend(config, user_id: str, values: dict) -> dict:
    from istota.health import db as health_db
    from istota.health._loader import UserNotFoundError, resolve_for_user

    try:
        ctx = resolve_for_user(user_id, config)
    except UserNotFoundError:
        raise Unavailable("Health is not turned on for you.") from None
    name = values["name"]
    if not ctx.db_path.exists():
        return {"name": name, "display_name": name, "unit": None, "points": []}
    with health_db.connect(ctx.db_path) as conn:
        ref = health_db.find_biomarker_ref_by_alias(conn, name)
        canonical = ref.name if ref else name
        trend = health_db.biomarker_trend(
            conn, name=canonical, since=values.get("since"), until=values.get("until"),
        )
    return {
        "name": canonical,
        "display_name": ref.display_name if ref else canonical,
        "unit": ref.default_unit if ref else None,
        "points": [
            {"drawn_at": drawn_at, "value": b.value, "unit": b.unit, "flag": b.flag}
            for b, drawn_at in trend
        ],
    }


def _location_db(config, user_id: str):
    """The user's location database path, None when there is none yet."""
    from istota.location._loader import UserNotFoundError, resolve_for_user

    try:
        ctx = resolve_for_user(user_id, config)
    except UserNotFoundError:
        raise Unavailable("Location is not turned on for you.") from None
    return ctx.db_path if ctx.db_path.exists() else None


def _location_current(config, user_id: str, values: dict) -> dict:
    from istota.location import logic

    path = _location_db(config, user_id)
    if path is None:
        return {"last_ping": None, "current_visit": None}
    return logic.location_current(path)


def _location_places(config, user_id: str, values: dict) -> dict:
    from istota.location import logic

    path = _location_db(config, user_id)
    if path is None:
        return {"places": []}
    return logic.location_places(path)


def _memory_facts(config, user_id: str, values: dict) -> dict:
    from istota import db as framework_db
    from istota.memory.knowledge_graph import get_current_facts
    from istota.skills.memory_search import fact_to_dict

    with framework_db.get_db(config.db_path) as conn:
        facts = get_current_facts(
            conn, user_id, subject=values.get("subject"), predicate=values.get("predicate"),
        )
    return {
        "facts": [fact_to_dict(f) for f in facts[:FACTS_MAX]],
        "truncated": len(facts) > FACTS_MAX,
    }


def _memory_timeline(config, user_id: str, values: dict) -> dict:
    from istota import db as framework_db
    from istota.memory.knowledge_graph import get_entity_timeline
    from istota.skills.memory_search import timeline_entry_to_dict

    subject = values["subject"]
    with framework_db.get_db(config.db_path) as conn:
        facts = get_entity_timeline(conn, user_id, subject)
    return {
        "subject": subject,
        "facts": [timeline_entry_to_dict(f) for f in facts[:FACTS_MAX]],
        "truncated": len(facts) > FACTS_MAX,
    }


SKILL_CAPS: dict[str, SkillCap] = {
    "markets.quote": SkillCap(
        skill="markets",
        arg_schema={"symbols": Arg("symbols", required=True)},
        handler=_markets_quote,
    ),
    "health.trend": SkillCap(
        skill="health",
        arg_schema={
            "name": Arg("text", required=True),
            "since": Arg("date"),
            "until": Arg("date"),
        },
        handler=_health_trend,
        sensitive=True,
    ),
    "location.current": SkillCap(skill="location", arg_schema={}, handler=_location_current),
    "location.places": SkillCap(skill="location", arg_schema={}, handler=_location_places),
    "memory_search.facts": SkillCap(
        skill="memory_search",
        arg_schema={"subject": Arg("text"), "predicate": Arg("text")},
        handler=_memory_facts,
    ),
    "memory_search.timeline": SkillCap(
        skill="memory_search",
        arg_schema={"subject": Arg("text", required=True)},
        handler=_memory_timeline,
    ),
}
