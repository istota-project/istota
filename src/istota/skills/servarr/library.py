"""The library half of the skill: `describe`, `search`, `add` and `requests`.

Films (radarr), shows (sonarr), and artists and albums (lidarr).

**Adds take an external id, never a title**, and look it up first: a
monitored item already in the library is ``already_present``, a successful
answer with ``added: false``, and is never retagged. A film or show in the
library but unmonitored is a stack holding something nobody asked for, so the
add requests it in place (`_monitor_existing`): given the caller's tag beside
any it has, monitored, and searched. ``--profile`` and ``--root`` are refused
there, since a request keeps the item's own settings. A new item gets the caller's
``req-<user>`` tag, created if the app lacks it, so `requests` can answer
"what did I ask for". A film, show or artist add is one ``POST``; an ending
after it was sent is ``outcome_unknown`` with the `search` that tells whether
it applied. An artist add is a whole discography, so it is gated: without
``--confirmed`` it answers ``confirmation_required`` with a ``would`` line.

**An album add is up to four writes.** Lidarr holds albums under artists. An
album the library does not hold yet is added with ``POST album`` carrying the
lookup's album object, as Lidarr's own UI adds one: under its artist when the
artist is in the library, whether or not the artist's album list (never
complete for Various Artists, and filtered by the metadata profile) includes
it, and with the artist when it is not. A new artist is tagged and sent with
``monitor: none`` and ``albumsToMonitor`` naming this album, and the add waits
for the artist's refresh (``GET command``), bounded by `REFRESH_POLL_SECONDS`
and the run's deadline, because that refresh stores the artist unmonitored and
would undo an earlier write. Then the artist is monitored if it is not, the
album if it is not (``PUT album/monitor``), and it is searched
(``AlbumSearch``). An album under an artist already in the library carries no
tag, because tags live on the artist and an existing artist is never retagged.
A step that fails after a write reports ``partial`` with what exists now, so
the model does not repeat the add blind; running the same add again finishes it.

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
    ArrClient, ServarrError, as_rows, external_id, fence, mbid, number, selector,
)
from .run import Run, media_apps, walk_apps

SERIES_MONITOR_MODES = ("all", "future", "missing", "existing", "firstSeason", "lastSeason",
                        "pilot", "none")
#: Lidarr's modes for which of an added artist's albums are monitored.
ARTIST_MONITOR_MODES = ("all", "future", "missing", "existing", "latest", "first", "none")
MINIMUM_AVAILABILITY = ("tba", "announced", "inCinemas", "released")

REFRESH_POLL_INTERVAL = 1.0
#: How long an album add waits for a new artist's refresh to finish.
REFRESH_POLL_SECONDS = 60.0
#: Kept back from the run's deadline for the monitors and search after the poll.
REFRESH_POLL_RESERVE_SECONDS = 10.0
#: Lidarr's command states after which a command changes nothing more.
_COMMAND_DONE = frozenset({"completed", "failed", "aborted", "cancelled", "orphaned"})

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
OVERVIEW_CHARS = 300
MAX_QUERY_CHARS = 200

TAG_PREFIX = "req-"
TAG_USER_CHARS = 40
_TAG_UNSAFE = re.compile(r"[^a-z0-9-]")

#: The argv flag each kind's external id is given by.
ID_FLAGS = {"film": "tmdb", "show": "tvdb", "artist": "mbid", "album": "mbid"}
#: The arrs' own id-term prefix for each flag: ``lidarr:<MBID>``.
TERM_PREFIX = {"tmdb": "tmdb", "tvdb": "tvdb", "mbid": "lidarr"}
#: The lookup each kind searches, and the field its external id is in: the
#: app's own for the kind its collection holds, plus lidarr's albums.
LOOKUP_PATHS = {"film": APPS["radarr"].lookup, "show": APPS["sonarr"].lookup,
                "artist": APPS["lidarr"].lookup, "album": "album/lookup"}
ID_FIELDS = {"film": APPS["radarr"].id_field, "show": APPS["sonarr"].id_field,
             "artist": APPS["lidarr"].id_field, "album": "foreignAlbumId"}
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
    names, named = media_apps(run, args.app, "describe")
    return walk_apps(run, names, lambda client: _describe(client, defaults), named=named)


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


def _id_for(args, kind: str) -> int | str | None:
    """The kind's own external id from argv, validated; None when not given.

    Another kind's id flag is refused rather than ignored, so a TVDB id passed
    for a film is not quietly dropped in favour of a title search. A
    MusicBrainz id comes back lowercased.
    """
    flag = ID_FLAGS[kind]
    for other in ("tmdb", "tvdb", "mbid"):
        if other != flag and getattr(args, other, None) is not None:
            raise _invalid(f"A {kind} is looked up by --{flag}, not --{other}.", kind=kind)
    value = getattr(args, flag, None)
    if value is None:
        return None
    return mbid(value) if flag == "mbid" else external_id(value, f"--{flag}")


def _term(kind: str, ident) -> str:
    """The lookup term for an external id: ``tmdb:655``, ``lidarr:<MBID>``."""
    return f"{TERM_PREFIX[ID_FLAGS[kind]]}:{ident}"


def _same_id(value, ident) -> bool:
    if isinstance(ident, str):
        return isinstance(value, str) and value.lower() == ident
    return value == ident and not isinstance(value, bool)


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


_ARTIST_STATUSES = frozenset({"continuing", "ended", "deleted"})


def _track_state(item: dict) -> tuple[bool, dict]:
    stats = _stats(item)
    files = number(stats.get("trackFileCount"))
    return bool(files), {"track_files": files, "tracks": number(stats.get("totalTrackCount"))}


def album_count(item: dict):
    """How many albums an artist has, when the answer says: its statistics
    for a library artist, else the length of an ``albums`` list; None otherwise."""
    count = number(_stats(item).get("albumCount"))
    if count is not None:
        return count
    albums = item.get("albums")
    return len(albums) if isinstance(albums, list) else None


def _artist(item: dict) -> dict:
    out = {
        "foreign_artist_id": selector(item.get("foreignArtistId")),
        "name": _text(item.get("artistName")),
        "disambiguation": _text(item.get("disambiguation")),
        "type": selector(item.get("artistType")),
        "status": _enum(item.get("status"), _ARTIST_STATUSES),
        "album_count": album_count(item),
        "overview": _overview(item.get("overview")),
    }
    has_file, counts = _track_state(item)
    return _with_library_state(out, item, has_file, counts)


def _track_count(item: dict):
    """An album's track count: its statistics in the library, else its
    monitored release's (the release lidarr would grab), else the largest."""
    count = number(_stats(item).get("totalTrackCount"))
    if count:
        return count
    releases = item.get("releases")
    releases = [r for r in releases if isinstance(r, dict)] if isinstance(releases, list) else []
    chosen = [number(r.get("trackCount")) for r in releases if r.get("monitored") is True]
    counts = [c for c in chosen if c] or [c for c in (number(r.get("trackCount"))
                                                      for r in releases) if c]
    return max(counts) if counts else count


def _album_artist(item: dict) -> dict:
    artist = item.get("artist")
    return artist if isinstance(artist, dict) else {}


def _album(item: dict) -> dict:
    """One album row. ``secondary_types`` (Soundtrack, Compilation, Live) and
    ``release_count`` are what Lidarr's own search shows to tell a soundtrack
    from a band's album of the same name."""
    artist = _album_artist(item)
    types = item.get("secondaryTypes")
    releases = item.get("releases")
    out = {
        "foreign_album_id": selector(item.get("foreignAlbumId")),
        "title": _text(item.get("title")),
        "artist": _text(artist.get("artistName")),
        "foreign_artist_id": selector(artist.get("foreignArtistId")),
        "release_date": selector(item.get("releaseDate")),
        "type": selector(item.get("albumType")),
        "secondary_types": ([selector(t) for t in types if isinstance(t, str)]
                            if isinstance(types, list) else []),
        "release_count": len(releases) if isinstance(releases, list) else None,
        "track_count": _track_count(item),
    }
    has_file, counts = _track_state(item)
    return _with_library_state(out, item, has_file, counts)


RENDER = {"film": _film, "show": _show, "artist": _artist, "album": _album}


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


def tag_id_in(rows: list[dict], label: str) -> int | None:
    """The id of the tag `label` among an app's tag rows, or None."""
    for row in rows:
        found = row.get("label")
        if isinstance(found, str) and found.casefold() == label:
            tag_id = _positive_id(row.get("id"))
            if tag_id is not None:
                return tag_id
    return None


def _find_tag(client: ArrClient, label: str) -> int | None:
    return tag_id_in(_tags(client), label)


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
    return requesters_in(item, _tags(client))


def requesters_in(item: dict, rows: list[dict]) -> list:
    """Whose ``req-`` tags `item` carries, read against an app's tag rows."""
    tag_ids = item.get("tags")
    if not isinstance(tag_ids, list):
        return []
    labels = {row.get("id"): row.get("label") for row in rows}
    out = []
    for tag_id in tag_ids:
        label = labels.get(tag_id) if isinstance(tag_id, int) else None
        if isinstance(label, str) and label.startswith(TAG_PREFIX) and label != TAG_PREFIX:
            out.append(selector(label[len(TAG_PREFIX):]))
    return out


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #


def _lookup(client: ArrClient, kind: str, term: str) -> list[dict]:
    path = LOOKUP_PATHS[kind]
    data = client.request("GET", path, params={"term": term}, kind="lookup")
    return as_rows(data, client.app, path)


def _single_lookup(client: ArrClient, kind: str, ident) -> dict:
    """The one lookup row carrying `ident`, or ``not_found``."""
    flag = ID_FLAGS[kind]
    rows = _lookup(client, kind, _term(kind, ident))
    matches = [row for row in rows if _same_id(row.get(ID_FIELDS[kind]), ident)]
    if len(matches) != 1:
        raise ServarrError(f"{client.app} has no single {kind} with --{flag} {ident}; run "
                           f"search to find the right id. Nothing was added.", "not_found",
                           app=client.app, kind=kind)
    return matches[0]


def _artist_brief(item: dict) -> dict:
    return {"foreign_artist_id": selector(item.get("foreignArtistId")),
            "name": _text(item.get("artistName")),
            "disambiguation": _text(item.get("disambiguation")),
            "in_library": _positive_id(item.get("id")) is not None}


def _albums_by_title(client: ArrClient, query: str, limit: int) -> dict:
    """``search --kind album TITLE``: Lidarr's own search, as its UI runs it.

    ``search?term=`` answers artists and albums mixed, each row holding one of
    them. ``album/lookup`` is not used: it leaves out albums the search finds,
    a Various Artists soundtrack among them (checked against Lidarr 3.1). The
    album rows are the results, whoever they are by; the artist rows are
    listed apart, since a title can also be a band's name.
    """
    data = client.request("GET", "search", params={"term": query}, kind="lookup")
    rows = as_rows(data, client.app, "search")
    albums = [row["album"] for row in rows if isinstance(row.get("album"), dict)]
    artists = [row["artist"] for row in rows if isinstance(row.get("artist"), dict)]
    out = {"status": "ok", "app": client.app, "kind": "album", "total": len(albums),
           "results": [_album(row) for row in albums[:limit]],
           "artists": [_artist_brief(a) for a in artists[:5]]}
    if not albums:
        out["note"] = ("No album matched that title. To list one artist's albums, pass "
                       "--artist NAME.")
    return out


def _albums_by_artist(client: ArrClient, name: str, title: str, limit: int) -> dict:
    """``search --kind album --artist NAME [TITLE]``: one artist's albums.

    The first artist the lookup returns is taken, and the others are listed so
    the model can see when that was the wrong one. A library artist's albums
    are read from the library; otherwise the album lookup is searched with the
    artist's name and kept to that artist's albums. A title keeps the albums
    whose title contains it.
    """
    out = {"status": "ok", "app": client.app, "kind": "album"}
    artists = _lookup(client, "artist", name)
    if not artists:
        return {**out, "artist": None, "other_artists": [], "total": 0, "results": [],
                "note": "No artist matched --artist."}
    artist = artists[0]
    artist_id = _positive_id(artist.get("id"))
    if artist_id is not None:
        data = client.request("GET", "album", params={"artistId": artist_id})
        rows = as_rows(data, client.app, "album")
        source = "library"
    else:
        wanted = artist.get("foreignArtistId")
        rows = [row for row in _lookup(client, "album", name)
                if _album_artist(row).get("foreignArtistId") == wanted]
        source = "lookup"
    if title:
        folded = title.casefold()
        rows = [row for row in rows
                if isinstance(row.get("title"), str) and folded in row["title"].casefold()]
    return {**out, "artist": _artist_brief(artist),
            "other_artists": [_artist_brief(a) for a in artists[1:5]],
            "albums_from": source, "total": len(rows),
            "results": [_album(row) for row in rows[:limit]]}


def cmd_search(run: Run, args) -> dict:
    """A title, or the kind's own external id, looked up in its app."""
    kind = args.kind
    flag = ID_FLAGS[kind]
    ident = _id_for(args, kind)
    query = (args.query or "").strip()
    artist = (getattr(args, "artist", None) or "").strip()
    if artist and kind != "album":
        raise _invalid("--artist applies to --kind album.")
    if artist and ident is not None:
        raise _invalid(f"search takes --artist or --{flag}, not both.")
    if ident is None and not query and not artist:
        raise _invalid(f"search needs a title or --{flag}.")
    if ident is not None and query:
        raise _invalid(f"search takes a title or --{flag}, not both.")
    if len(query) > MAX_QUERY_CHARS or len(artist) > MAX_QUERY_CHARS:
        raise _invalid(f"The title or artist is longer than {MAX_QUERY_CHARS} characters.")
    limit = limit_of(args)

    app = KIND_APP[kind]
    client = run.client(app)
    if artist:
        return _albums_by_artist(client, artist, query, limit)
    if kind == "album" and ident is None:
        return _albums_by_title(client, query, limit)
    term = _term(kind, ident) if ident is not None else query
    rows = _lookup(client, kind, term)
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


def _app_defaults(run: Run, app: str) -> dict:
    defaults = run.config.servarr.defaults.get(app)
    return defaults if isinstance(defaults, dict) else {}


#: The flag that overrides each listed field on `add`.
_FIELD_ARGS = {"quality_profile": "profile", "root_folder": "root",
               "metadata_profile": "metadata_profile"}
_FIELD_PATHS = {"quality_profile": "qualityprofile", "root_folder": "rootfolder",
                "metadata_profile": "metadataprofile"}


def _resolve_listed(client: ArrClient, args, defaults: dict) -> dict[str, dict]:
    """Each listed field an add sends (profile, root, lidarr's metadata
    profile), resolved against the app's own lists; read-only."""
    app = client.app
    out = {}
    for field in ("quality_profile", "root_folder", "metadata_profile"):
        if field not in DEFAULT_FIELDS[app]:
            continue
        path = _FIELD_PATHS[field]
        options = as_rows(client.request("GET", path), app, path)
        out[field] = resolve_field(app, field, options, flag=getattr(args, _FIELD_ARGS[field]),
                                   configured=_configured(defaults, field))
    return out


def _listed_names(listed: dict[str, dict]) -> dict:
    return {field: selector(value["value"]) for field, value in listed.items()}


def _already_present(client: ArrClient, kind: str, item: dict) -> dict:
    # Nested: the item's own `status` (released, ended) is not the envelope's.
    return {
        "status": "ok", "app": client.app, "kind": kind, "added": False,
        "already_present": True, "item": RENDER[kind](item),
        "requested_by": _requested_by(client, item),
        "note": "Already in the library; nothing was changed and it was not retagged.",
    }


def _raced(app: str, kind: str, lookup_hint: str) -> dict:
    return {"status": "ok", "app": app, "kind": kind, "added": False,
            "already_present": True, "lookup": lookup_hint,
            "note": "Someone added it while this request ran; it was not retagged."}


def cmd_add(run: Run, args) -> dict:
    """Add an item by its external id, with the stack's defaults."""
    kind = args.kind
    flag = ID_FLAGS[kind]
    ident = _id_for(args, kind)
    if ident is None:
        raise _invalid(f"add takes the {kind}'s --{flag} id, from search; never a title.")
    if kind == "film" and args.monitor:
        raise _invalid("--monitor applies to shows; a film is always added monitored.")
    if kind in ("artist", "album") and args.monitor:
        raise _invalid("--monitor applies to shows. An album add monitors that one album; "
                       "an artist add takes its mode from [servarr.defaults.lidarr] monitor.")
    if kind == "album":
        return _add_album(run, args, ident)
    if kind == "artist":
        return _add_artist(run, args, ident)
    return _add_video(run, args, kind, ident)


def _add_video(run: Run, args, kind: str, ident: int) -> dict:
    """A film or a show: one ``POST`` carrying the lookup object."""
    app = KIND_APP[kind]
    client = run.client(app)
    lookup_hint = f"search --kind {kind} --{ID_FLAGS[kind]} {ident}"
    item = _single_lookup(client, kind, ident)
    library_id = _positive_id(item.get("id"))
    if library_id is not None:
        if item.get("monitored") is True:
            return _already_present(client, kind, item)
        return _monitor_existing(run, args, client, kind, item, library_id)

    defaults = _app_defaults(run, app)
    settings = _add_settings(app, kind, args, defaults)
    listed = _resolve_listed(client, args, defaults)
    profile, root = listed["quality_profile"], listed["root_folder"]
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
        return _raced(app, kind, lookup_hint)

    library_id = _positive_id(created.get("id")) if isinstance(created, dict) else None
    out = {
        "status": "ok", "app": app, "kind": kind, "added": True, "library_id": library_id,
        "title": _text(item.get("title")), "year": number(item.get("year")),
        **_listed_names(listed), "tag": label, "searching": not args.no_search, **settings,
    }
    if library_id is None:
        out["lookup"] = lookup_hint
    return out


#: Show monitor modes that also mean seasons announced later; an add sends
#: the lookup's ``monitorNewItems: all``, so the in-place request sets it too.
_NEW_SEASON_MODES = frozenset({"all", "future"})


def _monitor_existing(run: Run, args, client: ArrClient, kind: str, item: dict,
                      library_id: int) -> dict:
    """A film or show in the library but unmonitored: tag it as the caller's
    request, monitor it, and search.

    The tag is written first, or with the monitor in one write for a film,
    because the monitor is what turns a repeat into ``already_present``: a
    failure before it leaves the item unmonitored, and the same add finishes
    the request. Only the search can fail after it, and `search-now` passes
    for an item carrying the caller's tag.
    """
    app = client.app
    lookup_hint = f"search --kind {kind} --{ID_FLAGS[kind]} {item.get(APPS[app].id_field)}"
    for flag in ("profile", "root"):
        if getattr(args, flag, None):
            raise _invalid(f"--{flag} applies to a new item. This {kind} is already in the "
                           f"library, unmonitored, and a request keeps its own settings; "
                           f"run the add without --{flag}.")
    mode = None
    if kind == "show":
        mode = args.monitor or _enum_default(app, "monitor", _app_defaults(run, app),
                                             SERIES_MONITOR_MODES)
        if mode == "none":
            raise _invalid("This show is in the library but unmonitored; --monitor none would "
                           "leave it that way. Pick the episodes to monitor, or leave --monitor "
                           "out for the stack's default.")
    label = req_tag(run.user_id)
    tag_id = ensure_tag(client, label)
    if kind == "film":
        client.request("PUT", "movie/editor", lookup=lookup_hint,
                       json={"movieIds": [library_id], "monitored": True, "tags": [tag_id],
                             "applyTags": "add"})
    else:
        editor = {"seriesIds": [library_id], "tags": [tag_id], "applyTags": "add"}
        if mode in _NEW_SEASON_MODES:
            editor["monitorNewItems"] = "all"
        client.request("PUT", "series/editor", lookup=lookup_hint, json=editor)
        # The series flag alone leaves every episode unmonitored; seasonpass
        # applies the monitor mode to the episodes as an add would.
        try:
            client.request("POST", "seasonpass", lookup=lookup_hint,
                           json={"series": [{"id": library_id, "monitored": True}],
                                 "monitoringOptions": {"monitor": mode}})
        except ServarrError as exc:
            if exc.reason == "outcome_unknown":
                raise
            raise ServarrError(
                f"{exc} The show carries this user's request tag but is not monitored yet; "
                f"running the same add again finishes the request.",
                exc.reason, **exc.extra,
            ) from None

    command_id = None
    if not args.no_search:
        body = search_command(kind, library_id)
        try:
            command = client.request("POST", "command", json=body)
        except ServarrError as exc:
            raise _monitored_partial(exc, kind, library_id) from None
        command_id = number(command.get("id")) if isinstance(command, dict) else None

    out = {
        "status": "ok", "app": app, "kind": kind, "added": False, "already_present": True,
        "monitored_now": True, "library_id": library_id,
        "title": _text(item.get("title")), "year": number(item.get("year")),
        "tag": label, "searching": not args.no_search, "search_command_id": command_id,
        "note": "It was in the library unmonitored; it is now monitored and tagged as this "
                "user's request.",
    }
    if mode is not None:
        out["monitor"] = mode
    return out


def _monitored_partial(exc: ServarrError, kind: str, library_id: int) -> ServarrError:
    """The search failed after the item was tagged and monitored."""
    unknown = exc.reason == "outcome_unknown"
    extra = {k: v for k, v in exc.extra.items() if k != "app"}
    extra.update(partial=True, monitored_now=True, search_sent=None if unknown else False,
                 library_id=library_id)
    sent = "the search may or may not have been sent" if unknown else "no search was sent"
    return ServarrError(
        f"{exc} The request is partly done: the {kind} is now monitored and tagged as this "
        f"user's request, but {sent}. search-now --kind {kind} --library-id {library_id} "
        f"searches at once.",
        exc.reason, app=KIND_APP[kind], **extra,
    )


def search_command(kind: str, item_id: int) -> dict:
    """The app command that searches for one library item."""
    if kind == "film":
        return {"name": "MoviesSearch", "movieIds": [item_id]}
    if kind == "show":
        return {"name": "SeriesSearch", "seriesId": item_id}
    if kind == "album":
        return {"name": "AlbumSearch", "albumIds": [item_id]}
    return {"name": "ArtistSearch", "artistId": item_id}


# -- artists -------------------------------------------------------------------


def _artist_body(item: dict, listed: dict[str, dict], tag_id: int, *, monitor: str,
                 new_items: str, search: bool) -> dict:
    body = dict(item)
    body.update(
        qualityProfileId=listed["quality_profile"]["id"],
        metadataProfileId=listed["metadata_profile"]["id"],
        rootFolderPath=listed["root_folder"]["value"],
        monitored=True, monitorNewItems=new_items, tags=[tag_id],
        addOptions={"monitor": monitor, "searchForMissingAlbums": search},
    )
    return body


def _gate_artist(args, item: dict, mode: str) -> None:
    """A whole discography needs the user's agreement first (wordpress's gate)."""
    if args.confirmed:
        return
    count = album_count(item)
    name = _text(item.get("artistName")) or "this artist"
    if mode == "all":
        what = (f"with all {count} albums monitored" if count is not None
                else "with all its albums monitored (the lookup did not say how many)")
    else:
        albums = (f"{count} in the discography" if count is not None
                  else "the lookup did not say how many there are")
        what = f"with albums monitored as '{mode}' ({albums})"
    then = ", without searching now" if args.no_search else " and search for them"
    would = [f"would add {name} {what}{then}"]
    raise ServarrError(
        "Not done: this needs the user's agreement first. " + "; ".join(would) + ". "
        "Show the user exactly this, and pass --confirmed only after they agree in the "
        "conversation.",
        "confirmation_required", app="lidarr", would=would, album_count=count,
    )


def _add_artist(run: Run, args, ident: str) -> dict:
    """A whole discography: admin only, gated, one ``POST artist``."""
    client = run.client("lidarr")
    lookup_hint = f"search --kind artist --mbid {ident}"
    item = _single_lookup(client, "artist", ident)
    if _positive_id(item.get("id")) is not None:
        return _already_present(client, "artist", item)

    defaults = _app_defaults(run, "lidarr")
    mode = _enum_default("lidarr", "monitor", defaults, ARTIST_MONITOR_MODES)
    _gate_artist(args, item, mode)
    listed = _resolve_listed(client, args, defaults)
    label = req_tag(run.user_id)
    tag_id = ensure_tag(client, label)
    body = _artist_body(item, listed, tag_id, monitor=mode, new_items="all",
                        search=not args.no_search)
    try:
        created = client.request("POST", "artist", json=body, lookup=lookup_hint)
    except ServarrError as exc:
        if exc.reason != "already_present":
            raise
        return _raced("lidarr", "artist", lookup_hint)

    library_id = _positive_id(created.get("id")) if isinstance(created, dict) else None
    out = {
        "status": "ok", "app": "lidarr", "kind": "artist", "added": True,
        "library_id": library_id, "name": _text(item.get("artistName")),
        "album_count": album_count(item), **_listed_names(listed), "tag": label,
        "monitor": mode, "searching": not args.no_search,
    }
    if library_id is None:
        out["lookup"] = lookup_hint
    return out


# -- albums --------------------------------------------------------------------


def _library_album(client: ArrClient, ident: str) -> dict | None:
    """The album's row in the library, or None when the library lacks it."""
    try:
        data = client.request("GET", "album", params={"foreignAlbumId": ident})
    except ServarrError as exc:
        if exc.reason == "not_found":
            return None
        raise
    for row in as_rows(data, client.app, "album"):
        if _same_id(row.get("foreignAlbumId"), ident) and _positive_id(row.get("id")):
            return row
    return None


def _refreshes_of(client: ArrClient, artist_id: int) -> list[str]:
    """The states of the ``RefreshArtist`` commands Lidarr holds for one artist."""
    rows = as_rows(client.request("GET", "command"), client.app, "command")
    states = []
    for row in rows:
        body = row.get("body")
        body = body if isinstance(body, dict) else {}
        ids = body.get("artistIds")
        ids = ids if isinstance(ids, list) else [body.get("artistId")]
        if row.get("name") == "RefreshArtist" and artist_id in ids:
            states.append(row.get("status"))
    return states


def _wait_for_refresh(run: Run, client: ArrClient, artist_id: int) -> bool:
    """Whether the new artist's refresh finished, polled every
    `REFRESH_POLL_INTERVAL` seconds until the poll's own bound or the run's
    deadline (less a reserve for the writes after it) would be passed.

    The refresh is what applies ``monitor: none`` and stores the artist
    unmonitored, so an ``artist/editor`` sent while it runs is undone; that was
    seen against Lidarr 3.1. A refresh seen and then gone from the list has
    finished; one never seen is waited for.
    """
    give_up = min(run.clock() + REFRESH_POLL_SECONDS,
                  run.deadline - REFRESH_POLL_RESERVE_SECONDS)
    seen = False
    while True:
        states = _refreshes_of(client, artist_id)
        if states and all(state in _COMMAND_DONE for state in states):
            return True
        if seen and not states:
            return True
        seen = seen or bool(states)
        if run.clock() + REFRESH_POLL_INTERVAL > give_up:
            return False
        run.sleep(REFRESH_POLL_INTERVAL)


def _partial(exc: ServarrError, progress: dict) -> ServarrError:
    """A failure after part of an album add applied, saying what exists now."""
    done = []
    if progress.get("artist_added"):
        done.append("the album was added to lidarr with its artist, and no other album of "
                    "the artist's is monitored")
    elif progress.get("album_added"):
        done.append("the album was added under its artist")
    if progress.get("artist_monitored_now"):
        done.append("the artist is monitored" if progress.get("artist_added") else
                    "the artist is monitored now, so any of its albums that were already "
                    "monitored are wanted too")
    if progress.get("album_monitored"):
        done.append("the album is monitored")
    if exc.reason == "outcome_unknown" and progress.get("album_monitored"):
        done.append("the search may or may not have been sent")
        progress = {**progress, "search_sent": None}
    elif not progress.get("search_sent"):
        done.append("no search was sent")
    if progress.get("album_monitored"):
        album_id = progress.get("album_library_id")
        then = ("lidarr will grab it on its next RSS sync, or search-now --kind album "
                f"--library-id {album_id} searches at once.")
    else:
        then = "Running the same add again finishes it without adding anything twice."
    extra = {k: v for k, v in exc.extra.items() if k != "app"}
    extra.update(progress, partial=True)
    return ServarrError(f"{exc} The request is partly done: {'; '.join(done)}. {then}",
                        exc.reason, app="lidarr", **extra)


def _library_artist(client: ArrClient, artist_id: int | None) -> dict:
    """The artist's library row, or ``{}`` when there is none."""
    if artist_id is None:
        return {}
    try:
        found = client.request("GET", f"artist/{artist_id}")
    except ServarrError as exc:
        if exc.reason != "not_found":
            raise
        return {}
    return found if isinstance(found, dict) else {}


def _album_already_present(client: ArrClient, album: dict, listed: dict,
                           artist: dict) -> dict:
    """A monitored album under a monitored artist; who requested it is read
    off the artist."""
    item = {**album, **listed}
    return {
        "status": "ok", "app": "lidarr", "kind": "album", "added": False,
        "already_present": True, "item": _album(item),
        "requested_by": _requested_by(client, artist),
        "note": "Already in the library and monitored; nothing was changed and it was not "
                "retagged.",
    }


def _still_refreshing() -> ServarrError:
    return ServarrError(
        "lidarr has not finished refreshing the new artist, and monitoring it before then "
        "would be undone. Wait a minute, then run the same add again.", "artist_refreshing",
        app="lidarr")


def _post_album(run: Run, args, client: ArrClient, album: dict, ident: str,
                progress: dict) -> tuple[dict, str | None, dict]:
    """Add an album the library does not hold: ``(its row, tag, defaults)``.

    Lidarr's UI adds an album from search with ``POST album`` and the lookup's
    album object, and so does this: under an artist already in the library
    whether or not the artist's own album list includes it, and with the
    artist when it is new. A new artist is tagged and sent with ``monitor:
    none`` plus ``albumsToMonitor``, which keeps this album monitored through
    the refresh that leaves the rest alone, and the refresh is waited for.
    """
    foreign = _album_artist(album).get("foreignArtistId")
    try:
        artist_mbid = mbid(foreign)
    except ServarrError:
        raise ServarrError("lidarr's lookup did not say which artist this album is by; "
                           "nothing was added.", "bad_response", app="lidarr") from None
    artist = _single_lookup(client, "artist", artist_mbid)
    new_artist = _positive_id(artist.get("id")) is None
    label = None
    names: dict = {}
    if new_artist:
        resolved = _resolve_listed(client, args, _app_defaults(run, "lidarr"))
        names = _listed_names(resolved)
        label = req_tag(run.user_id)
        artist = _artist_body(artist, resolved, ensure_tag(client, label), monitor="none",
                              new_items="none", search=False)
        artist["addOptions"]["albumsToMonitor"] = [ident]
    body = dict(album)
    body.update(artist=artist, monitored=True, addOptions={"searchForNewAlbum": False})
    try:
        created = client.request("POST", "album", json=body,
                                 lookup=f"search --kind album --mbid {ident}")
    except ServarrError as exc:
        if exc.reason != "already_present":
            raise
        # Someone added it meanwhile; it was not tagged here.
        listed = _library_album(client, ident)
        if listed is None:
            raise
        return listed, None, {}

    created = created if isinstance(created, dict) else {}
    progress["album_added"] = True
    progress["artist_added"] = new_artist
    progress["album_library_id"] = _positive_id(created.get("id"))
    progress["artist_library_id"] = (_positive_id(created.get("artistId"))
                                     or _positive_id(_album_artist(created).get("id")))
    if not new_artist:
        listed = created if progress["album_library_id"] else _library_album(client, ident)
    else:
        try:
            if progress["artist_library_id"] is None:
                raise ServarrError("lidarr accepted the album but did not say which artist "
                                   "holds it.", "bad_response", app="lidarr")
            if not _wait_for_refresh(run, client, progress["artist_library_id"]):
                raise _still_refreshing()
            # Re-read: the refresh is what decides the album's monitored flag.
            listed = _library_album(client, ident)
        except ServarrError as exc:
            raise _partial(exc, progress) from None
    if listed is None:
        raise _partial(ServarrError("lidarr accepted the album but does not list it.",
                                    "bad_response", app="lidarr"), progress)
    return listed, label, names


def _add_album(run: Run, args, ident: str) -> dict:
    """One album: add it if the library lacks it, then monitor and search it.

    Every check and read happens before the first write. An album the library
    lacks is added by `_post_album`, with its artist when the artist is new;
    a new artist is tagged. An artist already in the library is not tagged (an
    existing artist is never retagged), so such an album does not show in
    `requests`, and the envelope says so.

    Lidarr's Wanted lists an album only when its artist is monitored too, and
    it stores a new artist unmonitored under ``monitor: none`` whatever the
    body's ``monitored`` says. So the artist is monitored with ``artist/editor``
    before the album is: a new one with new albums off, an existing one with
    its own new-albums setting left as it is (ISSUE-773).
    """
    client = run.client("lidarr")
    album = _single_lookup(client, "album", ident)
    progress = {"album_added": False, "artist_added": False, "artist_monitored": False,
                "artist_monitored_now": False, "album_monitored": False,
                "search_sent": False}
    listed = _library_album(client, ident)
    label = None
    names: dict = {}
    if listed is None:
        listed, label, names = _post_album(run, args, client, album, ident, progress)

    album_id = _positive_id(listed.get("id"))
    artist_id = (progress.get("artist_library_id")
                 or _positive_id(listed.get("artistId")))
    progress["artist_library_id"] = artist_id
    editor = None
    if artist_id is None:
        pass  # Nothing names the artist; the album is still monitored and searched.
    elif progress["artist_added"]:
        editor = {"artistIds": [artist_id], "monitored": True, "monitorNewItems": "none"}
    else:
        if not progress["album_added"]:
            # An earlier add's refresh may still be running (`artist_refreshing`); it
            # would undo the editor below, or unmonitor the artist after it.
            states = _refreshes_of(client, artist_id)
            if any(state not in _COMMAND_DONE for state in states):
                if not _wait_for_refresh(run, client, artist_id):
                    raise _still_refreshing()
        library_artist = _library_artist(client, artist_id)
        if label is None and library_artist.get("tags"):
            own = req_tag(run.user_id)
            if run.user_id and own[len(TAG_PREFIX):] in requesters_in(library_artist,
                                                                        _tags(client)):
                label = own
        if library_artist.get("monitored") is True:
            if listed.get("monitored") is True and not progress["album_added"]:
                return _album_already_present(client, album, listed, library_artist)
            progress["artist_monitored"] = True
        else:
            editor = {"artistIds": [artist_id], "monitored": True}
    if editor is not None:
        foreign = _album_artist(album).get("foreignArtistId")
        try:
            client.request("PUT", "artist/editor", json=editor,
                           lookup=(f"search --kind artist --mbid {foreign}" if foreign
                                   else f"search --kind album --mbid {ident}"))
        except ServarrError as exc:
            if progress["album_added"]:
                raise _partial(exc, progress) from None
            raise
        progress["artist_monitored"] = progress["artist_monitored_now"] = True

    if listed.get("monitored") is not True:
        try:
            client.request("PUT", "album/monitor", json={"albumIds": [album_id],
                                                         "monitored": True},
                           lookup=f"search --kind album --mbid {ident}")
        except ServarrError as exc:
            if progress["album_added"] or progress["artist_monitored_now"]:
                raise _partial(exc, progress) from None
            raise
    progress["album_monitored"] = True

    command_id = None
    if not args.no_search:
        try:
            command = client.request("POST", "command",
                                     json=search_command("album", album_id))
        except ServarrError as exc:
            raise _partial(exc, {**progress, "album_library_id": album_id}) from None
        progress["search_sent"] = True
        command_id = number(command.get("id")) if isinstance(command, dict) else None

    out = {
        "status": "ok", "app": "lidarr", "kind": "album", "added": True,
        "title": _text(album.get("title")),
        "artist": _text(_album_artist(album).get("artistName")),
        "album_library_id": album_id, **progress, "searching": not args.no_search,
        "search_command_id": command_id, "tag": label, **names,
    }
    notes = []
    if label is None:
        notes.append("The artist was already in the library, so nothing was tagged and this "
                     "request will not show in requests.")
    if progress["artist_monitored_now"] and not progress["artist_added"]:
        notes.append("The artist was not monitored, so it is now: lidarr wants an album only "
                     "when its artist is monitored, and any of this artist's albums that were "
                     "already monitored are wanted now too.")
    if notes:
        out["note"] = " ".join(notes)
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
