"""`!arr`: search for and request a film, show or album with no model turn.

The command builds the same argv the CLI takes, parses it with the CLI's own
parser and runs the verb through `call_verb`, so the access table, the add
defaults and every refusal are the skill's. What differs is the caller: the
user typed the command in their own turn, so nothing is withheld, and the
answer is rendered as plain text for a person rather than JSON for a model.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3

from istota.lib.untrusted import unframe_untrusted

from . import build_parser, call_verb
from .client import LABEL, ServarrError, external_id, mbid
from .library import ID_FLAGS, MAX_QUERY_CHARS, SERIES_MONITOR_MODES

log = logging.getLogger(__name__)

#: A command has no skill-proxy timeout above it, and on Talk it runs inside
#: the poll's transaction, so it gets a shorter run than the CLI.
COMMAND_BUDGET_SECONDS = 45
SEARCH_LIMIT = 8
TITLE_CHARS = 100

KIND_WORDS = {
    "film": "film", "films": "film", "movie": "film", "movies": "film",
    "show": "show", "shows": "show", "tv": "show", "series": "show",
    "album": "album", "albums": "album",
}
_MONITOR_MODES = {mode.casefold(): mode for mode in SERIES_MONITOR_MODES}
_APP_NAMES = {"film": "Radarr", "show": "Sonarr", "album": "Lidarr"}
_KIND_NOUNS = {"film": "films", "show": "shows", "album": "albums"}
_ID_FIELDS = {"film": "tmdb_id", "show": "tvdb_id", "album": "foreign_album_id"}

#: The ids the user's last search in a room listed, keyed by room, so
#: `!arr add album 2` can name a result by its number. Reserved: no task writes it.
LAST_SEARCH_NAMESPACE = "_arr_last_search"

USAGE = (
    "**Request media from the arr stack:**\n\n"
    "- `!arr search <film|show|album> <title>` -- look a title up\n"
    "- `!arr add film <tmdb-id>` -- request a film\n"
    "- `!arr add show <tvdb-id> [monitor]` -- request a show; monitor is one of "
    f"{', '.join(SERIES_MONITOR_MODES)}\n"
    "- `!arr add album <musicbrainz-id>` -- request an album\n\n"
    "Search first, then add a result by its number (`!arr add album 2`) or by its id. "
    "A number picks from your last search in this room. Adding something that is in "
    "the library but not monitored monitors it and searches for it."
)


def run_command(config, user_id: str, conversation_token: str, text: str) -> str:
    """The reply to `!arr <text>`."""
    words = text.split()
    if not words or words[0].lower() in ("help", "?"):
        return USAGE
    verb = words[0].lower()
    if verb in KIND_WORDS:
        verb, words = "search", ["search", *words]
    if verb not in ("search", "add"):
        return f"Unknown `!arr {words[0]}`.\n\n{USAGE}"
    if len(words) < 3:
        return USAGE
    kind = KIND_WORDS.get(words[1].lower())
    if kind is None:
        return f"`{words[1]}` is not a kind: use film, show or album.\n\n{USAGE}"
    rest = words[2:]

    if verb == "add" and _is_result_number(rest[0]):
        picked, refusal = _pick_result(config, user_id, conversation_token, kind, int(rest[0]))
        if refusal:
            return refusal
        if picked is not None:
            rest = [picked, *rest[1:]]

    if verb == "add" and _as_id(kind, rest[0]) is not None:
        if len(rest) > 2:
            return USAGE
        mode = None
        if len(rest) == 2:
            if kind != "show":
                return "A monitor mode applies to shows only."
            mode = _MONITOR_MODES.get(rest[1].casefold())
            if mode is None:
                return f"`{rest[1]}` is not a monitor mode: use one of " \
                       f"{', '.join(SERIES_MONITOR_MODES)}."
        argv = ["add", "--kind", kind, f"--{ID_FLAGS[kind]}={rest[0]}"]
        if mode:
            argv.append(f"--monitor={mode}")
        return _call(config, user_id, conversation_token, argv, render=_render_add)

    # `add` with a title rather than an id is a search: the skill never adds by title.
    query = " ".join(rest)
    if len(query) > MAX_QUERY_CHARS:
        return f"The title is longer than {MAX_QUERY_CHARS} characters."
    argv = ["search", "--kind", kind, f"--limit={SEARCH_LIMIT}", "--", query]

    def render(args, envelope):
        results = envelope.get("results") or []
        if results:
            _remember_search(config, user_id, conversation_token, kind, results)
        return _render_search(args, envelope)

    return _call(config, user_id, conversation_token, argv, render=render)


def _is_result_number(word: str) -> bool:
    return word.isdigit() and 1 <= int(word) <= SEARCH_LIMIT


def _pick_result(config, user_id: str, conversation_token: str, kind: str, number: int):
    """`(id, refusal)` for result `number` of the user's last search in this room.

    `(None, None)` when no search is stored and the number can stand as an id
    itself, which a TMDB or TVDB id can and a MusicBrainz id cannot.
    """
    from istota import db

    stored = None
    try:
        with db.get_db(config.db_path) as conn:
            row = db.kv_get(conn, user_id, LAST_SEARCH_NAMESPACE, conversation_token)
        if row is not None:
            stored = json.loads(row["value"])
    except (sqlite3.Error, ValueError):
        log.warning("!arr could not read the last search", exc_info=True)
    ids = stored.get("ids") if isinstance(stored, dict) else None
    if not isinstance(ids, list):
        if ID_FLAGS[kind] != "mbid":
            return None, None
        return None, (f"There is no album search in this room to take result {number} "
                      "from. Search first, or add it by its id.")
    if stored.get("kind") != kind:
        last = _KIND_NOUNS.get(stored.get("kind"), "something else")
        return None, (f"Your last search here was for {last}. Search for "
                      f"{_KIND_NOUNS[kind]} first, or add it by its id.")
    if number > len(ids) or not ids[number - 1]:
        return None, (f"Your last search here listed {len(ids)} "
                      f"result{'s' if len(ids) != 1 else ''}; there is no result {number}.")
    return str(ids[number - 1]), None


def _remember_search(config, user_id: str, conversation_token: str, kind: str,
                     results: list) -> None:
    from istota import db

    ids = [str(r.get(_ID_FIELDS[kind]) or "") if isinstance(r, dict) else ""
           for r in results[:SEARCH_LIMIT]]
    try:
        with db.get_db(config.db_path) as conn:
            db.kv_set(conn, user_id, LAST_SEARCH_NAMESPACE, conversation_token,
                      json.dumps({"kind": kind, "ids": ids}))
    except sqlite3.Error:
        log.warning("!arr could not store the search for numbered adds", exc_info=True)


def _as_id(kind: str, word: str):
    try:
        return mbid(word) if ID_FLAGS[kind] == "mbid" else external_id(word, ID_FLAGS[kind])
    except ServarrError:
        return None


def _call(config, user_id: str, conversation_token: str, argv: list[str], *, render) -> str:
    from istota.skills._loader import load_skill_index
    from istota.skills.skills import skill_context

    try:
        args = build_parser().parse_args(argv)
    except SystemExit:
        return USAGE
    skill_index = load_skill_index(config.skills_dir, bundled_dir=config.bundled_skills_dir)
    ctx = skill_context(config, user_id, skill_index, frozenset())
    ctx["conversation_token"] = conversation_token
    try:
        envelope = call_verb(args.command, args, ctx, budget=COMMAND_BUDGET_SECONDS)
    except ServarrError as exc:
        return _render_refusal(exc)
    return render(args, envelope)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _plain(value) -> str:
    """A field the app wrote, out of its fence, on one line and cut short.

    The reply goes to the person who asked, not a model; it is still posted in
    the room, so it is kept to one short line.
    """
    if value is None:
        return ""
    text = unframe_untrusted(value, LABEL)
    text = " ".join((str(value) if text is None else text).split())
    if len(text) > TITLE_CHARS:
        text = text[:TITLE_CHARS] + "…"
    return text


def _year(value) -> str:
    if isinstance(value, int) and value > 0:
        return str(value)
    if isinstance(value, str) and len(value) >= 4 and value[:4].isdigit():
        return value[:4]
    return ""


def _library_state(result: dict) -> str:
    if not result.get("in_library"):
        return ""
    if not result.get("monitored"):
        return " · in the library, not monitored"
    if result.get("has_file"):
        return " · in the library"
    return " · in the library, wanted"


def _search_line(kind: str, result: dict) -> str:
    title = _plain(result.get("title")) or "(untitled)"
    if kind == "album":
        details = [d for d in (_year(result.get("release_date")),
                               *[_plain(t) for t in result.get("secondary_types") or []]) if d]
        artist = _plain(result.get("artist"))
        head = f"**{title}**" + (f" by {artist}" if artist else "")
        if details:
            head += f" ({', '.join(details)})"
    else:
        details = [_year(result.get("year"))]
        seasons = result.get("season_count")
        if kind == "show" and isinstance(seasons, int):
            details.append(f"{seasons} season{'s' if seasons != 1 else ''}")
        details = [d for d in details if d]
        head = f"**{title}**" + (f" ({', '.join(details)})" if details else "")
    ident = result.get(_ID_FIELDS[kind])
    return f"{head} -- `{_plain(ident)}`{_library_state(result)}"


def _render_search(args: argparse.Namespace, envelope: dict) -> str:
    kind = args.kind
    results = envelope.get("results") or []
    if not results:
        return f"No {_KIND_NOUNS[kind]} matched “{args.query}”."
    total = envelope.get("total")
    shown = f" (first {len(results)} of {total})" if isinstance(total, int) and total > len(results) else ""
    lines = [f"**{_KIND_NOUNS[kind].capitalize()} matching “{args.query}”**{shown}:", ""]
    lines += [f"{n}. {_search_line(kind, r)}" for n, r in enumerate(results, 1)]
    lines += ["", f"Request one by its number, `!arr add {kind} 1`, or its id."]
    return "\n".join(lines)


def _render_add(args: argparse.Namespace, envelope: dict) -> str:
    kind = args.kind
    app = _APP_NAMES[kind]
    item = envelope.get("item") if isinstance(envelope.get("item"), dict) else {}
    title = _plain(envelope.get("title") or item.get("title")) or f"the {kind}"
    if kind == "album":
        artist = _plain(envelope.get("artist") or item.get("artist"))
        if artist:
            title = f"{title}** by **{artist}"
    year = _year(envelope.get("year") or item.get("year"))
    name = f"**{title}**" + (f" ({year})" if year else "")
    searching = "searching now" if envelope.get("searching") else "not searching yet"

    if envelope.get("added"):
        text = f"Requested {name} in {app}; {searching}."
        if envelope.get("monitor"):
            text += f" Monitoring: {envelope['monitor']}."
        if kind == "album" and envelope.get("note"):
            text += f"\n\n{envelope['note']}"
        return text
    if envelope.get("monitored_now"):
        return (f"{name} was in {app} but not monitored. It is monitored now and "
                f"requested for you; {searching}.")
    if envelope.get("already_present"):
        if not item:
            return f"{name} was added to {app} by someone else while this ran."
        who = [_plain(u) for u in envelope.get("requested_by") or []]
        by = f", requested by {', '.join(who)}" if who else ""
        return f"{name} is already in {app} and monitored{by}. Nothing changed."
    return f"{app} answered without adding {name}."


def _render_refusal(exc: ServarrError) -> str:
    if exc.reason == "not_authorized":
        return f"Not available: {exc}"
    if exc.extra.get("partial"):
        return f"Partly done: {exc}"
    return f"Not done: {exc}"
