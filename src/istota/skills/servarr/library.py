"""The library half of the skill: `describe`, `search`, `add` and `requests`.

Films (radarr) and shows (sonarr) are here; artists and albums answer
``not_implemented`` until the lidarr half lands.

**Adds take an external id, never a title**, and look it up first: an item
already in the library is ``already_present``, a successful answer with
``added: false``, and is never retagged. A new item gets the caller's
``req-<user>`` tag, created if the app lacks it, so `requests` can answer
"what did I ask for". The add itself is one ``POST``; an ending after it was
sent is ``outcome_unknown`` with the `search` that tells whether it applied.

The add defaults are resolved in one place, which `describe` reports and `add`
applies. For a quality profile, a root folder and (lidarr) a metadata
profile, the order is:

1. the flag (`--profile`, `--root`, `--metadata-profile`; admin tier only),
   matched against the app's own list: profiles by exact name, then
   case-insensitively; root folders by exact path. A miss is
   ``unknown_profile`` or ``unknown_root``, naming what exists;
2. ``[servarr.defaults.<app>]``, matched the same way. A name the app does not
   have is ``stale_default``, never a quiet fall-through, because that would
   hide drift between the stack and the config;
3. the app's only option, when it has exactly one;
4. otherwise ``choice_required``, listing the options.

``minimum_availability`` (radarr) and ``monitor`` are not looked up in the app:
they come from config or their built-in default, and have no flag except
``--monitor`` for a show. A configured value outside the app's own set is
``config_invalid`` before anything is written.
"""

from __future__ import annotations

import re

from .apps import APPS, DEFAULT_FIELDS, KIND_APP, MEDIA_APPS
from .client import (
    ArrClient, ServarrError, as_rows, external_id, fence, number, selector,
)
from .run import Run, walk_apps

SERIES_MONITOR_MODES = ("all", "future", "missing", "existing", "firstSeason", "lastSeason",
                        "pilot", "none")
MINIMUM_AVAILABILITY = ("tba", "announced", "inCinemas", "released")

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
OVERVIEW_CHARS = 300
MAX_QUERY_CHARS = 200

TAG_PREFIX = "req-"
TAG_USER_CHARS = 40
_TAG_UNSAFE = re.compile(r"[^a-z0-9-]")

#: The external id each kind is added by: (argv flag, lookup term prefix).
ID_FLAGS = {"film": "tmdb", "show": "tvdb"}
#: The kinds this build adds and searches; the rest belong to lidarr's half.
VIDEO_KINDS = tuple(ID_FLAGS)
#: The `tag/detail` field that lists an app's tagged items.
TAG_DETAIL_FIELDS = {"radarr": "movieIds", "sonarr": "seriesIds", "lidarr": "artistIds"}

_FILM_STATUSES = frozenset({"tba", "announced", "inCinemas", "released", "deleted"})
_SHOW_STATUSES = frozenset({"continuing", "ended", "upcoming", "deleted"})

#: Fields resolved against a list the app returns: (option key, flag, miss reason).
LISTED_FIELDS = {
    "quality_profile": ("name", "--profile", "unknown_profile"),
    "root_folder": ("path", "--root", "unknown_root"),
    "metadata_profile": ("name", "--metadata-profile", "unknown_profile"),
}
#: Fields with a built-in default and no list to check against.
BUILTIN_DEFAULTS = {"minimum_availability": "released", "monitor": "all"}


def _match(options: list[dict], key: str, wanted: str, *, fold: bool) -> dict | None:
    for option in options:
        if option.get(key) == wanted:
            return option
    if fold:
        folded = wanted.casefold()
        for option in options:
            value = option.get(key)
            if isinstance(value, str) and value.casefold() == folded:
                return option
    return None


def _found(option: dict, key: str, source: str) -> dict:
    result = {"value": option.get(key)}
    if key == "name" and "id" in option:
        result["id"] = option["id"]
    result["source"] = source
    return result


def resolve_field(
    app: str, field: str, options: list[dict], *, flag: str | None = None,
    configured: str | None = None,
) -> dict:
    """``{"value", ["id"], "source"}`` for one listed field, or a refusal.

    The value is the server's own spelling, which is what an add sends. Names
    in a refusal's ``available`` are passed through `selector`, so a hostile
    one arrives fenced.
    """
    key, flag_name, miss = LISTED_FIELDS[field]
    fold = key == "name"
    available = [selector(option.get(key)) for option in options]

    if flag is not None:
        option = _match(options, key, flag, fold=fold)
        if option is None:
            raise ServarrError(
                f"{app} has no {field.replace('_', ' ')} matching {flag_name} {flag!r}.",
                miss, app=app, field=field, available=available,
            )
        return _found(option, key, "flag")

    if configured:
        option = _match(options, key, configured, fold=fold)
        if option is None:
            config_key = f"servarr.defaults.{app}.{field}"
            raise ServarrError(
                f"The configured default {config_key} = {configured!r} is not one {app} "
                f"has; the operator needs to correct it.",
                "stale_default", app=app, field=field, config_key=config_key,
                available=available,
            )
        return _found(option, key, "config")

    if len(options) == 1:
        return _found(options[0], key, "only_option")

    raise ServarrError(
        f"{app} has {len(options)} {field.replace('_', ' ')} options and no default is "
        f"configured, so one has to be chosen.",
        "choice_required", app=app, field=field, available=available,
    )


def describe_defaults(app: str, fields: tuple[str, ...], configured: dict,
                      catalog: dict[str, list[dict]]) -> dict:
    """What an add with no flags would use, field by field; never raises.

    A field that cannot be resolved reports its refusal (``error``, plus
    ``config_key`` and ``available`` where they apply) in place of a value, so
    `describe` shows every problem at once rather than the first.
    """
    configured = configured if isinstance(configured, dict) else {}
    out: dict[str, dict] = {}
    for field in fields:
        value = configured.get(field)
        value = value.strip() if isinstance(value, str) and value.strip() else None
        if field in BUILTIN_DEFAULTS:
            out[field] = ({"value": value, "source": "config"} if value
                          else {"value": BUILTIN_DEFAULTS[field], "source": "default"})
            continue
        try:
            out[field] = resolve_field(app, field, catalog.get(field, []), configured=value)
        except ServarrError as exc:
            entry = {"error": exc.reason}
            for name in ("config_key", "available"):
                if name in exc.extra:
                    entry[name] = exc.extra[name]
            out[field] = entry
    return out


def _named(rows: list[dict]) -> list[dict]:
    return [{"id": number(row.get("id")), "name": selector(row.get("name"))} for row in rows]


def _describe(client: ArrClient, defaults: dict) -> dict:
    app = client.app
    profiles = as_rows(client.request("GET", "qualityprofile"), app, "qualityprofile")
    roots = as_rows(client.request("GET", "rootfolder"), app, "rootfolder")
    tags = as_rows(client.request("GET", "tag"), app, "tag")
    catalog = {"quality_profile": profiles, "root_folder": roots}
    out = {
        "quality_profiles": _named(profiles),
        # `freeSpace` is reported as given: on a network mount it may mean little.
        "root_folders": [
            {"path": selector(row.get("path")), "free_space": number(row.get("freeSpace")),
             "accessible": row.get("accessible") if isinstance(row.get("accessible"), bool)
             else None}
            for row in roots
        ],
        "tags": [{"id": number(row.get("id")), "label": selector(row.get("label"))}
                 for row in tags],
    }
    if "metadata_profile" in DEFAULT_FIELDS[app]:
        metadata = as_rows(client.request("GET", "metadataprofile"), app, "metadataprofile")
        catalog["metadata_profile"] = metadata
        out["metadata_profiles"] = _named(metadata)
    out["defaults"] = describe_defaults(app, DEFAULT_FIELDS[app], defaults.get(app, {}), catalog)
    return out


def cmd_describe(run: Run, args) -> dict:
    """Profiles, root folders, tags and the add defaults of each media app."""
    defaults = run.config.servarr.defaults
    if args.app:
        if args.app not in MEDIA_APPS:
            raise ServarrError(f"{args.app} serves no media, so it has nothing to describe; "
                               f"use status for it.", "not_a_media_app", app=args.app)
        names = [args.app]
    else:
        names = [name for name in MEDIA_APPS if name in run.config.servarr.apps]
    return walk_apps(run, names, lambda client: _describe(client, defaults),
                     named=bool(args.app))


# --------------------------------------------------------------------------- #
# Arguments
# --------------------------------------------------------------------------- #


def _invalid(message: str, **extra) -> ServarrError:
    return ServarrError(message + " Nothing was sent.", "invalid_arguments", **extra)


def limit_of(args) -> int:
    """``--limit``: 10 when absent, refused outside 1 to 50."""
    value = getattr(args, "limit", None)
    if value is None:
        return DEFAULT_LIMIT
    if not 1 <= value <= MAX_LIMIT:
        raise ServarrError(f"--limit must be between 1 and {MAX_LIMIT}; nothing was sent.",
                           "invalid_limit", limit=value)
    return value


def _video_only(kind: str, verb: str) -> None:
    if kind not in VIDEO_KINDS:
        raise ServarrError(f"{verb} --kind {kind} is not available in this build yet; films "
                           f"and shows are.", "not_implemented", verb=verb, kind=kind)


def _id_for(args, kind: str) -> int | None:
    """The kind's own external id from argv, validated; None when not given.

    Another kind's id flag is refused rather than ignored, so a TVDB id passed
    for a film is not quietly dropped in favour of a title search.
    """
    flag = ID_FLAGS[kind]
    for other in ("tmdb", "tvdb", "mbid"):
        if other != flag and getattr(args, other, None) is not None:
            raise _invalid(f"A {kind} is looked up by --{flag}, not --{other}.", kind=kind)
    value = getattr(args, flag, None)
    return None if value is None else external_id(value, f"--{flag}")


def _configured(defaults: dict, field: str) -> str | None:
    value = defaults.get(field)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _enum_default(app: str, field: str, defaults: dict, allowed: tuple[str, ...]) -> str:
    """A configured enum default, or the built-in one; refused outside `allowed`."""
    value = _configured(defaults, field)
    if value is None:
        return BUILTIN_DEFAULTS[field]
    if value not in allowed:
        config_key = f"servarr.defaults.{app}.{field}"
        raise ServarrError(
            f"The configured default {config_key} = {value!r} is not one of "
            f"{', '.join(allowed)}; the operator needs to correct it. Nothing was added.",
            "config_invalid", app=app, config_key=config_key, available=list(allowed),
        )
    return value


# --------------------------------------------------------------------------- #
# Rendering an item
# --------------------------------------------------------------------------- #


def _positive_id(value) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _text(value):
    return fence(value) if isinstance(value, str) and value else None


def _overview(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if len(text) > OVERVIEW_CHARS:
        text = text[:OVERVIEW_CHARS] + "…"
    return fence(text)


def _enum(value, known: frozenset):
    if isinstance(value, str) and value in known:
        return value
    return selector(value)


def _stats(item: dict) -> dict:
    stats = item.get("statistics")
    return stats if isinstance(stats, dict) else {}


def _with_library_state(out: dict, item: dict, has_file: bool, extra: dict) -> dict:
    """``in_library``, and the library state when it is: the arrs mark an item
    a lookup found in the library by giving it its id (``id > 0``)."""
    library_id = _positive_id(item.get("id"))
    out["in_library"] = library_id is not None
    if library_id is not None:
        out.update(library_id=library_id, monitored=item.get("monitored") is True,
                   has_file=has_file, **extra)
    return out


def _film(item: dict) -> dict:
    out = {
        "tmdb_id": number(item.get("tmdbId")),
        "imdb_id": selector(item.get("imdbId")),
        "title": _text(item.get("title")),
        "year": number(item.get("year")),
        "status": _enum(item.get("status"), _FILM_STATUSES),
        "runtime": number(item.get("runtime")),
        "overview": _overview(item.get("overview")),
    }
    return _with_library_state(out, item, item.get("hasFile") is True, {})


def _season_count(item: dict):
    count = number(_stats(item).get("seasonCount"))
    if count is not None:
        return count
    seasons = item.get("seasons")
    if not isinstance(seasons, list):
        return None
    numbers = [number(s.get("seasonNumber")) for s in seasons if isinstance(s, dict)]
    return sum(1 for n in numbers if n is not None and n > 0)


def _show(item: dict) -> dict:
    out = {
        "tvdb_id": number(item.get("tvdbId")),
        "imdb_id": selector(item.get("imdbId")),
        "title": _text(item.get("title")),
        "year": number(item.get("year")),
        "status": _enum(item.get("status"), _SHOW_STATUSES),
        "season_count": _season_count(item),
        "runtime": number(item.get("runtime")),
        "overview": _overview(item.get("overview")),
    }
    stats = _stats(item)
    files = number(stats.get("episodeFileCount"))
    episodes = {"episode_files": files, "episodes": number(stats.get("episodeCount"))}
    return _with_library_state(out, item, bool(files), episodes)


RENDER = {"film": _film, "show": _show}


# --------------------------------------------------------------------------- #
# Tags
# --------------------------------------------------------------------------- #


def req_tag(user_id: str) -> str:
    """``req-<user>``: lowercased, anything outside ``[a-z0-9-]`` made a hyphen,
    the user part cut at 40 characters, since arr tag labels allow no more."""
    user = _TAG_UNSAFE.sub("-", user_id.strip().lower())
    return TAG_PREFIX + user[:TAG_USER_CHARS]


def _tags(client: ArrClient) -> list[dict]:
    return as_rows(client.request("GET", "tag"), client.app, "tag")


def _find_tag(client: ArrClient, label: str) -> int | None:
    for row in _tags(client):
        found = row.get("label")
        if isinstance(found, str) and found.casefold() == label:
            tag_id = _positive_id(row.get("id"))
            if tag_id is not None:
                return tag_id
    return None


def ensure_tag(client: ArrClient, label: str) -> int:
    """The id of the tag `label`, created when the app does not have it.

    A create that fails or ends ambiguously is followed by one re-read: another
    add may have created the same tag in between, and the tag is the same tag
    whoever made it.
    """
    found = _find_tag(client, label)
    if found is not None:
        return found
    try:
        created = client.request("POST", "tag", json={"label": label})
    except ServarrError as exc:
        if exc.reason not in ("rejected", "api_error", "outcome_unknown"):
            raise
        found = _find_tag(client, label)
        if found is None:
            raise
        return found
    tag_id = _positive_id(created.get("id")) if isinstance(created, dict) else None
    if tag_id is None:
        tag_id = _find_tag(client, label)
    if tag_id is None:
        raise ServarrError(f"{client.app} accepted the tag {label} but did not give its id; "
                           f"nothing was added.", "bad_response", app=client.app)
    return tag_id


def _requested_by(client: ArrClient, item: dict) -> list:
    """Whose ``req-`` tags an existing item carries."""
    tag_ids = item.get("tags")
    if not isinstance(tag_ids, list) or not tag_ids:
        return []
    labels = {row.get("id"): row.get("label") for row in _tags(client)}
    out = []
    for tag_id in tag_ids:
        label = labels.get(tag_id) if isinstance(tag_id, int) else None
        if isinstance(label, str) and label.startswith(TAG_PREFIX) and label != TAG_PREFIX:
            out.append(selector(label[len(TAG_PREFIX):]))
    return out


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #


def _lookup(client: ArrClient, term: str) -> list[dict]:
    data = client.request("GET", APPS[client.app].lookup, params={"term": term}, kind="lookup")
    return as_rows(data, client.app, "lookup")


def cmd_search(run: Run, args) -> dict:
    """A title, or the kind's own external id, looked up in its app."""
    kind = args.kind
    _video_only(kind, "search")
    flag = ID_FLAGS[kind]
    ident = _id_for(args, kind)
    query = (args.query or "").strip()
    if ident is None and not query:
        raise _invalid(f"search needs a title or --{flag}.")
    if ident is not None and query:
        raise _invalid(f"search takes a title or --{flag}, not both.")
    if len(query) > MAX_QUERY_CHARS:
        raise _invalid(f"The title is longer than {MAX_QUERY_CHARS} characters.")
    limit = limit_of(args)

    app = KIND_APP[kind]
    term = f"{flag}:{ident}" if ident is not None else query
    rows = _lookup(run.client(app), term)
    render = RENDER[kind]
    return {"status": "ok", "app": app, "kind": kind, "total": len(rows),
            "results": [render(row) for row in rows[:limit]]}


# --------------------------------------------------------------------------- #
# add
# --------------------------------------------------------------------------- #


def _add_settings(app: str, kind: str, args, defaults: dict) -> dict:
    """The enum settings an add sends: radarr's minimum availability (config
    only, no flag) or sonarr's monitor mode (``--monitor``, then config)."""
    if kind == "film":
        return {"minimum_availability": _enum_default(app, "minimum_availability", defaults,
                                                      MINIMUM_AVAILABILITY)}
    if args.monitor:
        return {"monitor": args.monitor}
    return {"monitor": _enum_default(app, "monitor", defaults, SERIES_MONITOR_MODES)}


def _already_present(client: ArrClient, kind: str, item: dict) -> dict:
    # Nested: the item's own `status` (released, ended) is not the envelope's.
    return {
        "status": "ok", "app": client.app, "kind": kind, "added": False,
        "already_present": True, "item": RENDER[kind](item),
        "requested_by": _requested_by(client, item),
        "note": "Already in the library; nothing was changed and it was not retagged.",
    }


def cmd_add(run: Run, args) -> dict:
    """Add a film or show by its external id, with the stack's defaults."""
    kind = args.kind
    _video_only(kind, "add")
    flag = ID_FLAGS[kind]
    ident = _id_for(args, kind)
    if ident is None:
        raise _invalid(f"add takes the {kind}'s --{flag} id, from search; never a title.")
    if kind == "film" and args.monitor:
        raise _invalid("--monitor applies to shows; a film is always added monitored.")

    app = KIND_APP[kind]
    client = run.client(app)
    lookup_hint = f"search --kind {kind} --{flag} {ident}"
    id_field = APPS[app].id_field
    matches = [row for row in _lookup(client, f"{flag}:{ident}") if row.get(id_field) == ident]
    if len(matches) != 1:
        raise ServarrError(f"{app} has no single {kind} with --{flag} {ident}; run search to "
                           f"find the right id. Nothing was added.", "not_found",
                           app=app, kind=kind)
    item = matches[0]
    if _positive_id(item.get("id")) is not None:
        return _already_present(client, kind, item)

    defaults = run.config.servarr.defaults.get(app)
    defaults = defaults if isinstance(defaults, dict) else {}
    settings = _add_settings(app, kind, args, defaults)
    profiles = as_rows(client.request("GET", "qualityprofile"), app, "qualityprofile")
    profile = resolve_field(app, "quality_profile", profiles, flag=args.profile,
                            configured=_configured(defaults, "quality_profile"))
    roots = as_rows(client.request("GET", "rootfolder"), app, "rootfolder")
    root = resolve_field(app, "root_folder", roots, flag=args.root,
                         configured=_configured(defaults, "root_folder"))
    label = req_tag(run.user_id)
    tag_id = ensure_tag(client, label)

    body = dict(item)
    body.update(qualityProfileId=profile["id"], rootFolderPath=root["value"], monitored=True,
                tags=[tag_id])
    if kind == "film":
        body["minimumAvailability"] = settings["minimum_availability"]
        body["addOptions"] = {"searchForMovie": not args.no_search}
    else:
        body["seasonFolder"] = True
        body["addOptions"] = {"monitor": settings["monitor"],
                              "searchForMissingEpisodes": not args.no_search,
                              "searchForCutoffUnmetEpisodes": False}
    try:
        created = client.request("POST", APPS[app].collection, json=body, lookup=lookup_hint)
    except ServarrError as exc:
        if exc.reason != "already_present":
            raise
        return {"status": "ok", "app": app, "kind": kind, "added": False,
                "already_present": True, "lookup": lookup_hint,
                "note": "Someone added it while this request ran; it was not retagged."}

    library_id = _positive_id(created.get("id")) if isinstance(created, dict) else None
    out = {
        "status": "ok", "app": app, "kind": kind, "added": True, "library_id": library_id,
        "title": _text(item.get("title")), "year": number(item.get("year")),
        "quality_profile": selector(profile["value"]), "root_folder": selector(root["value"]),
        "tag": label, "searching": not args.no_search, **settings,
    }
    if library_id is None:
        out["lookup"] = lookup_hint
    return out


# --------------------------------------------------------------------------- #
# requests
# --------------------------------------------------------------------------- #

_ITEM_KINDS = {"radarr": "film", "sonarr": "show", "lidarr": "artist"}


def _request_item(app: str, item: dict) -> dict:
    stats = _stats(item)
    title = item.get("artistName") if app == "lidarr" else item.get("title")
    out = {"kind": _ITEM_KINDS[app], "library_id": _positive_id(item.get("id")),
           "title": _text(title)}
    if app == "radarr":
        out.update(year=number(item.get("year")), has_file=item.get("hasFile") is True)
    elif app == "sonarr":
        files = number(stats.get("episodeFileCount"))
        out.update(year=number(item.get("year")), has_file=bool(files), episode_files=files,
                   episodes=number(stats.get("episodeCount")))
    else:
        files = number(stats.get("trackFileCount"))
        out.update(has_file=bool(files), track_files=files,
                   tracks=number(stats.get("totalTrackCount")))
    out["monitored"] = item.get("monitored") is True
    out["added"] = selector(item.get("added"))
    return out


def _requests(client: ArrClient, label: str, limit: int) -> dict:
    """The items in one app carrying `label`, newest first (highest id)."""
    app = client.app
    field = TAG_DETAIL_FIELDS[app]
    ids: set[int] = set()
    for row in as_rows(client.request("GET", "tag/detail"), app, "tag/detail"):
        found = row.get("label")
        values = row.get(field)
        if isinstance(found, str) and found.casefold() == label and isinstance(values, list):
            ids.update(v for v in values if _positive_id(v) is not None)
    ordered = sorted(ids, reverse=True)
    items = []
    for library_id in ordered[:limit]:
        try:
            item = client.request("GET", f"{APPS[app].collection}/{library_id}")
        except ServarrError as exc:
            if exc.reason == "not_found":
                continue
            raise
        if isinstance(item, dict):
            items.append(_request_item(app, item))
    return {"total": len(ordered), "truncated": len(ordered) > limit, "items": items}


def cmd_requests(run: Run, args) -> dict:
    """What one user asked for: the items carrying their ``req-`` tag.

    ``--user`` is only ever compared with tag labels here, never sent.
    """
    who = (args.user if args.user is not None else run.user_id).strip()
    if not who:
        raise _invalid("--user is empty.")
    label = req_tag(who)
    limit = limit_of(args)
    if args.kind:
        names = [KIND_APP[args.kind]]
    else:
        names = [name for name in MEDIA_APPS if name in run.config.servarr.apps]
    envelope = walk_apps(run, names, lambda client: _requests(client, label, limit),
                         named=bool(args.kind))
    return {"status": envelope.pop("status"), "tag": label, **envelope}
