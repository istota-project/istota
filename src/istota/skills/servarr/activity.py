"""The activity half of the skill: what the stack is doing and why.

``status`` is each app's version, start time and health checks, the first
thing to run when something seems wrong. ``queue`` merges every media app's
download queue, problems first; ``why`` explains one item from its state, its
queue rows and its history, with a plain-language ``hint`` and, on request, an
interactive release search; ``wanted`` lists monitored items still missing;
``search-now`` asks an app to search for one item. ``indexers`` is prowlarr's
indexers with a state each, and with ``--test`` the result of testing them all.

Nothing here changes anything except ``search-now``'s one ``POST command``,
which the arrs queue and de-duplicate, so it has no gate, and ``indexers
--test``'s ``POST indexer/testall``, which changes no setting. A requester may search only
for an item carrying their own ``req-`` tag (``not_your_request`` otherwise),
and nobody may search for an item that is not monitored (``not_monitored``),
since the search would find releases and grab nothing.

Queue rows from the three apps are merged and sorted problems first, then by
estimated completion. A queue is read one page of `QUEUE_PAGE` rows per app; an
app with more says ``truncated`` so a partial queue is never reported as the
whole one.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from . import access
from .apps import APP_ORDER, KIND_APP
from .client import ArrClient, ServarrError, as_rows, fence, library_id, number, selector
from .library import (
    ID_FLAGS, _id_for, _invalid, _positive_id, _stats, _tags, _text, limit_of, req_tag,
    requesters_in, tag_id_in,
)
from .run import Run, media_apps, walk_apps

_HEALTH_TYPES = frozenset({"ok", "notice", "warning", "error"})

QUEUE_PAGE = 200
MAX_STATUS_MESSAGES = 5
#: An import still pending this long after the download was added needs a look.
IMPORT_PENDING_GRACE = timedelta(hours=1)
_PROBLEM_STATES = frozenset({"importBlocked", "failedPending"})
#: The events after which nothing more happens unless someone acts.
FAILURE_EVENTS = frozenset({"downloadFailed", "importFailed", "albumImportIncomplete"})

#: Each app's queue page parameters: the item objects are included so a row
#: can name its item and `--mine` can read its tags.
QUEUE_PARAMS = {
    "sonarr": {"includeUnknownSeriesItems": "false", "includeSeries": "true",
               "includeEpisode": "true"},
    "radarr": {"includeMovie": "true"},
    "lidarr": {"includeArtist": "true", "includeAlbum": "true"},
}
WANTED_PARAMS = {
    "sonarr": {"includeSeries": "true", "sortKey": "episodes.airDateUtc",
               "sortDirection": "descending"},
    "radarr": {},
    "lidarr": {"includeArtist": "true", "sortKey": "albums.releaseDate",
               "sortDirection": "descending"},
}

#: The library collection holding each kind, and how to find one by external
#: id there without a metadata lookup: (query parameter, field it matches).
ITEM_PATHS = {"film": "movie", "show": "series", "artist": "artist", "album": "album"}
EXTERNAL_QUERY = {"film": ("tmdbId", "tmdbId"), "show": ("tvdbId", "tvdbId"),
                  "artist": ("mbId", "foreignArtistId"),
                  "album": ("foreignAlbumId", "foreignAlbumId")}
#: The queue record field naming the item each kind is.
QUEUE_ITEM_FIELDS = {"film": "movieId", "show": "seriesId", "artist": "artistId",
                     "album": "albumId"}
#: Per-item history: the path, taken from the arr API docs (confirmed live in
#: Stage 6), and the query parameter naming the item.
HISTORY = {"film": ("history/movie", "movieId"), "show": ("history/series", "seriesId"),
           "artist": ("history/artist", "artistId"), "album": ("history/artist", "artistId")}

_FRACTION = re.compile(r"(\.\d{6})\d+")


def _health_item(item: dict) -> dict:
    kind = item.get("type")
    return {
        "type": kind if kind in _HEALTH_TYPES else selector(kind),
        "source": selector(item.get("source")),
        "message": fence(item.get("message")),
    }


def _status(client: ArrClient) -> dict:
    data = client.system_status()
    health = as_rows(client.request("GET", "health"), client.app, "health")
    return {
        "version": selector(data.get("version")),
        "start_time": selector(data.get("startTime")),
        "health": [_health_item(item) for item in health],
    }


def cmd_status(run: Run, args) -> dict:
    """Every configured app, prowlarr included, or the one ``--app`` names."""
    if args.app:
        return walk_apps(run, [args.app], _status, named=True)
    names = [name for name in APP_ORDER if name in run.config.servarr.apps]
    return walk_apps(run, names, _status, named=False)


# --------------------------------------------------------------------------- #
# Shared reading helpers
# --------------------------------------------------------------------------- #


def parse_time(value) -> datetime | None:
    """An arr timestamp (``2026-10-16T07:00:00Z``) as an aware datetime, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = _FRACTION.sub(r"\1", value.strip())
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _page_records(data, app: str, what: str) -> tuple[list[dict], int | None]:
    """A paged answer's records and its ``totalRecords``."""
    if not isinstance(data, dict):
        raise ServarrError(f"{app} answered {what} with something other than a page.",
                           "bad_response", app=app)
    return as_rows(data.get("records"), app, what), number(data.get("totalRecords"))


def _episode_code(season, episode) -> str | None:
    season, episode = number(season), number(episode)
    if not isinstance(season, int) or not isinstance(episode, int):
        return None
    return f"S{season:02d}E{episode:02d}"


def _tag_ids(item: dict) -> list[int]:
    tags = item.get("tags")
    if not isinstance(tags, list):
        return []
    return [t for t in tags if isinstance(t, int) and not isinstance(t, bool)]


# --------------------------------------------------------------------------- #
# queue
# --------------------------------------------------------------------------- #


def _queue_item(app: str, record: dict) -> tuple[str, str | None, int | None, dict]:
    """``(kind, title, library_id, item)`` for a queue record: the item it is
    a download of, from the included objects, and that item's own title."""
    if app == "radarr":
        movie = _dict(record.get("movie"))
        title = movie.get("title")
        year = number(movie.get("year"))
        if isinstance(title, str) and year:
            title = f"{title} ({year})"
        return "film", title, _positive_id(record.get("movieId")), movie
    if app == "sonarr":
        series = _dict(record.get("series"))
        episode = _dict(record.get("episode"))
        title = series.get("title")
        code = _episode_code(episode.get("seasonNumber", record.get("seasonNumber")),
                             episode.get("episodeNumber"))
        if isinstance(title, str) and code:
            title = f"{title} {code}"
        return "show", title, _positive_id(record.get("seriesId")), series
    artist = _dict(record.get("artist"))
    album = _dict(record.get("album"))
    names = [n for n in (artist.get("artistName"), album.get("title")) if isinstance(n, str)]
    return "album", " - ".join(names) or None, _positive_id(record.get("albumId")), artist


def _status_messages(record: dict) -> list[str]:
    """``title: message`` lines, flattened and capped, each fenced."""
    lines: list[str] = []
    entries = record.get("statusMessages")
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        title = entry.get("title") if isinstance(entry.get("title"), str) else ""
        messages = [m for m in entry.get("messages") or [] if isinstance(m, str) and m]
        if not messages and title:
            lines.append(title)
        for message in messages:
            lines.append(f"{title}: {message}" if title else message)
    return [fence(line) for line in lines[:MAX_STATUS_MESSAGES]]


def is_problem(record: dict, now: datetime) -> bool:
    """Whether a queue row needs someone to look at it: a tracked status other
    than ``ok``, an import blocked or a failure pending, or an import still
    pending an hour after the download was added. No field says when a state
    began, so the age is measured from ``added``."""
    tracked = record.get("trackedDownloadStatus")
    if isinstance(tracked, str) and tracked.lower() != "ok":
        return True
    state = record.get("trackedDownloadState")
    if state in _PROBLEM_STATES:
        return True
    if state == "importPending":
        added = parse_time(record.get("added"))
        return added is not None and now - added > IMPORT_PENDING_GRACE
    return False


def queue_row(app: str, record: dict, now: datetime) -> dict:
    kind, title, item_id, item = _queue_item(app, record)
    if not title:
        title = record.get("title")
    size, left = number(record.get("size")), number(record.get("sizeleft"))
    progress = round((size - left) / size * 100, 1) if size and left is not None else None
    error = record.get("errorMessage")
    return {
        "app": app, "queue_id": number(record.get("id")), "kind": kind,
        "library_id": item_id, "title": _text(title),
        "problem": is_problem(record, now),
        "status": selector(record.get("status")),
        "tracked_download_status": selector(record.get("trackedDownloadStatus")),
        "tracked_download_state": selector(record.get("trackedDownloadState")),
        "progress": progress,
        "timeleft": selector(record.get("timeleft")),
        "estimated_completion_time": selector(record.get("estimatedCompletionTime")),
        "status_messages": _status_messages(record),
        "error_message": fence(error) if isinstance(error, str) and error else None,
        "protocol": selector(record.get("protocol")),
        "download_client": _text(record.get("downloadClient")),
        "tags": _tag_ids(item),
    }


def _queue_records(client: ArrClient) -> tuple[list[dict], int | None]:
    params = {"page": 1, "pageSize": QUEUE_PAGE, **QUEUE_PARAMS[client.app]}
    return _page_records(client.request("GET", "queue", params=params), client.app, "queue")


_LATEST = datetime.max.replace(tzinfo=timezone.utc)


def _row_order(row: dict) -> tuple:
    eta = parse_time(row.get("estimated_completion_time"))
    return (not row["problem"], eta or _LATEST, APP_ORDER.index(row["app"]),
            row.get("queue_id") or 0)


def cmd_queue(run: Run, args) -> dict:
    """Every media app's queue, merged: problems first, then by completion."""
    limit = limit_of(args)
    names, named = media_apps(run, args.app, "queue")
    now = run.now()
    label = req_tag(run.user_id) if args.mine else None
    rows: list[dict] = []

    def visit(client: ArrClient) -> dict:
        records, total = _queue_records(client)
        found = [queue_row(client.app, record, now) for record in records]
        if label is not None:
            own = tag_id_in(_tags(client), label)
            found = [row for row in found if own is not None and own in row["tags"]]
        rows.extend(found)
        fetched = len(records)
        return {"total_records": total, "fetched": fetched,
                "truncated": total is not None and total > fetched}

    envelope = walk_apps(run, names, visit, named=named)
    if args.problems:
        rows = [row for row in rows if row["problem"]]
    rows.sort(key=_row_order)
    envelope["total"] = len(rows)
    envelope["truncated"] = any(entry.get("truncated") for entry in envelope["apps"])
    envelope["rows"] = rows[:limit]
    return envelope


# --------------------------------------------------------------------------- #
# Finding one library item
# --------------------------------------------------------------------------- #


def item_arguments(args, verb: str) -> tuple[int | None, int | str | None]:
    """``(library_id, external_id)`` from argv, checked before anything is sent."""
    kind = args.kind
    ident = _id_for(args, kind)
    raw = getattr(args, "library_id", None)
    if raw is None and ident is None:
        raise _invalid(f"{verb} needs --library-id or the {kind}'s --{ID_FLAGS[kind]}.")
    return (library_id(raw) if raw is not None else None), ident


def find_item(client: ArrClient, kind: str, item_id: int | None, ident) -> dict:
    """The library item, by its library id or by its external id; a library
    query either way, never a metadata lookup."""
    path = ITEM_PATHS[kind]
    if item_id is not None:
        item = client.request("GET", f"{path}/{item_id}")
        if not isinstance(item, dict) or _positive_id(item.get("id")) is None:
            raise ServarrError(f"{client.app} answered {path}/{item_id} with no item.",
                               "bad_response", app=client.app)
        return item
    param, field = EXTERNAL_QUERY[kind]
    rows = as_rows(client.request("GET", path, params={param: ident}), client.app, path)
    for row in rows:
        value = row.get(field)
        same = (value.lower() == ident if isinstance(ident, str) and isinstance(value, str)
                else value == ident and not isinstance(value, bool))
        if same and _positive_id(row.get("id")) is not None:
            return row
    raise ServarrError(
        f"No {kind} with --{ID_FLAGS[kind]} {ident} is in {client.app}'s library; search "
        f"for it, and add it if the user wants it.", "not_in_library", app=client.app,
        kind=kind,
    )


def _artist_of(client: ArrClient, album: dict) -> dict:
    """An album's artist from the library, or {} when it cannot be named."""
    artist_id = _positive_id(album.get("artistId"))
    if artist_id is None:
        return _dict(album.get("artist"))
    found = client.request("GET", f"artist/{artist_id}")
    return found if isinstance(found, dict) else {}


# --------------------------------------------------------------------------- #
# why
# --------------------------------------------------------------------------- #


def _profile_name(client: ArrClient, profile_id) -> str | None:
    if _positive_id(profile_id) is None:
        return None
    for row in as_rows(client.request("GET", "qualityprofile"), client.app, "qualityprofile"):
        if row.get("id") == profile_id:
            return selector(row.get("name"))
    return None


def _why_item(client: ArrClient, kind: str, item: dict, artist: dict) -> dict:
    stats = _stats(item)
    out: dict = {"library_id": _positive_id(item.get("id")),
                 "monitored": item.get("monitored") is True}
    if kind == "film":
        out.update(title=_text(item.get("title")), year=number(item.get("year")),
                   has_file=item.get("hasFile") is True,
                   minimum_availability=selector(item.get("minimumAvailability")),
                   is_available=(item.get("isAvailable")
                                 if isinstance(item.get("isAvailable"), bool) else None))
        profile = item.get("qualityProfileId")
    elif kind == "show":
        files = number(stats.get("episodeFileCount"))
        out.update(title=_text(item.get("title")), year=number(item.get("year")),
                   has_file=bool(files), episode_files=files,
                   episodes=number(stats.get("episodeCount")))
        profile = item.get("qualityProfileId")
    else:
        files = number(stats.get("trackFileCount"))
        if kind == "artist":
            out["name"] = _text(item.get("artistName"))
        else:
            out.update(title=_text(item.get("title")), artist=_text(artist.get("artistName")),
                       release_date=selector(item.get("releaseDate")))
        out.update(has_file=bool(files), track_files=files,
                   tracks=number(stats.get("totalTrackCount")))
        profile = (item if kind == "artist" else artist).get("qualityProfileId")
    out["quality_profile"] = _profile_name(client, profile)
    return out


def _history_event(event: dict) -> dict:
    data = _dict(event.get("data"))
    message = data.get("message") or data.get("reason")
    return {
        "event_type": selector(event.get("eventType")),
        "date": selector(event.get("date")),
        "source_title": _text(event.get("sourceTitle")),
        "message": fence(message) if isinstance(message, str) and message else None,
    }


def _history(client: ArrClient, kind: str, item: dict) -> list[dict]:
    """The item's events, newest first."""
    path, param = HISTORY[kind]
    if kind == "album":
        params = {param: _positive_id(item.get("artistId")), "albumId": item["id"]}
    else:
        params = {param: item["id"]}
    events = as_rows(client.request("GET", path, params=params), client.app, path)
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    events.sort(key=lambda e: parse_time(e.get("date")) or oldest, reverse=True)
    return events


def _hint(kind: str, item: dict, state: dict, queue: list, events: list[dict]) -> str | None:
    """A plain reason, derived only from what the app reported; None when
    nothing explains a stall (or nothing is stalled)."""
    if item.get("monitored") is not True:
        return "not monitored, so nothing will grab it"
    if kind == "film" and item.get("isAvailable") is False:
        return (f"not yet available under minimum availability "
                f"{selector(item.get('minimumAvailability'))}")
    if not events and not queue and not state.get("has_file"):
        return ("never grabbed; run `why --releases` to see what the indexers offer and why "
                "each was rejected")
    if not queue and events and events[0].get("eventType") in FAILURE_EVENTS:
        last = _history_event(events[0])
        said = last["message"] or "with no message"
        return f"the last attempt ended {last['event_type']}: {said}"
    return None


def _latest_season(item: dict) -> int | None:
    """The highest monitored season above the specials, else the highest."""
    seasons = item.get("seasons")
    numbers = []
    monitored = []
    for season in seasons if isinstance(seasons, list) else []:
        if not isinstance(season, dict):
            continue
        n = number(season.get("seasonNumber"))
        if isinstance(n, int) and n > 0:
            numbers.append(n)
            if season.get("monitored") is True:
                monitored.append(n)
    if monitored:
        return max(monitored)
    return max(numbers) if numbers else None


def _rejections(release: dict) -> list[str]:
    out = []
    raw = release.get("rejections")
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, dict):
            entry = entry.get("message") or entry.get("reason")
        if isinstance(entry, str) and entry:
            out.append(fence(entry))
    return out


def _release(release: dict) -> dict:
    quality = _dict(_dict(release.get("quality")).get("quality"))
    return {
        "title": _text(release.get("title")),
        "indexer": _text(release.get("indexer")),
        "size": number(release.get("size")),
        "seeders": number(release.get("seeders")),
        "quality": selector(quality.get("name")),
        "custom_format_score": number(release.get("customFormatScore")),
        "approved": release.get("approved") is True,
        "rejections": _rejections(release),
    }


def _releases(client: ArrClient, kind: str, item: dict, limit: int) -> dict:
    """An interactive search across every indexer: approved first, then by score."""
    out: dict = {}
    if kind == "film":
        params = {"movieId": item["id"]}
    elif kind == "show":
        season = _latest_season(item)
        if season is None:
            raise ServarrError("sonarr lists no season to search for this show.",
                               "no_season", app=client.app)
        params = {"seriesId": item["id"], "seasonNumber": season}
        out["season_number"] = season
    else:
        params = {"albumId": item["id"]}
    rows = as_rows(client.request("GET", "release", params=params, kind="release"),
                   client.app, "release")
    rows.sort(key=lambda r: (r.get("approved") is not True,
                             -(number(r.get("customFormatScore")) or 0)))
    out.update(total=len(rows), approved=sum(1 for r in rows if r.get("approved") is True),
               results=[_release(row) for row in rows[:limit]])
    return out


def cmd_why(run: Run, args) -> dict:
    """One item explained: its state, its queue rows, its history and a hint."""
    kind = args.kind
    item_id, ident = item_arguments(args, "why")
    limit = limit_of(args)
    if args.releases and kind == "artist":
        raise _invalid("--releases searches one album, not a whole discography; use --kind "
                       "album with the album's id.")
    client = run.client(KIND_APP[kind])
    item = find_item(client, kind, item_id, ident)
    artist = _artist_of(client, item) if kind == "album" else {}
    state = _why_item(client, kind, item, artist)

    records, total = _queue_records(client)
    field = QUEUE_ITEM_FIELDS[kind]
    now = run.now()
    queue = [queue_row(client.app, record, now) for record in records
             if record.get(field) == item["id"]]
    events = _history(client, kind, item)
    envelope = {
        "status": "ok", "app": client.app, "kind": kind, "item": state, "queue": queue,
        "queue_truncated": total is not None and total > len(records),
        "history": [_history_event(event) for event in events[:limit]],
        "history_total": len(events),
        "hint": _hint(kind, item, state, queue, events),
    }
    if args.releases:
        envelope["releases"] = _releases(client, kind, item, limit)
    return envelope


# --------------------------------------------------------------------------- #
# wanted
# --------------------------------------------------------------------------- #


def _wanted_item(app: str, record: dict) -> dict:
    if app == "radarr":
        available = record.get("isAvailable")
        return {"library_id": _positive_id(record.get("id")),
                "tmdb_id": number(record.get("tmdbId")), "title": _text(record.get("title")),
                "year": number(record.get("year")),
                "is_available": available if isinstance(available, bool) else None,
                "minimum_availability": selector(record.get("minimumAvailability"))}
    if app == "sonarr":
        series = _dict(record.get("series"))
        return {"series_id": _positive_id(record.get("seriesId")),
                "series": _text(series.get("title")),
                "episode": _episode_code(record.get("seasonNumber"),
                                         record.get("episodeNumber")),
                "episode_id": _positive_id(record.get("id")),
                "title": _text(record.get("title")),
                "air_date": selector(record.get("airDateUtc"))}
    artist = _dict(record.get("artist"))
    return {"library_id": _positive_id(record.get("id")),
            "artist_id": _positive_id(record.get("artistId")),
            "artist": _text(artist.get("artistName")), "title": _text(record.get("title")),
            "release_date": selector(record.get("releaseDate")),
            "type": selector(record.get("albumType"))}


def cmd_wanted(run: Run, args) -> dict:
    """Monitored items still missing, or below their cutoff with ``--cutoff``."""
    limit = limit_of(args)
    names, named = media_apps(run, args.app, "wanted")
    path = "wanted/cutoff" if args.cutoff else "wanted/missing"

    def visit(client: ArrClient) -> dict:
        params = {"page": 1, "pageSize": limit, "monitored": "true",
                  **WANTED_PARAMS[client.app]}
        records, total = _page_records(client.request("GET", path, params=params),
                                       client.app, path)
        return {"total": total, "items": [_wanted_item(client.app, r) for r in records[:limit]]}

    return walk_apps(run, names, visit, named=named)


# --------------------------------------------------------------------------- #
# search-now
# --------------------------------------------------------------------------- #


def _search_command(kind: str, item_id: int) -> dict:
    if kind == "film":
        return {"name": "MoviesSearch", "movieIds": [item_id]}
    if kind == "show":
        return {"name": "SeriesSearch", "seriesId": item_id}
    if kind == "album":
        return {"name": "AlbumSearch", "albumIds": [item_id]}
    return {"name": "ArtistSearch", "artistId": item_id}


def _check_owner(run: Run, client: ArrClient, kind: str, item: dict) -> None:
    """A requester's search is refused unless the item carries their tag. An
    album's tags live on its artist."""
    carrier = _artist_of(client, item) if kind == "album" else item
    rows = _tags(client)
    own = tag_id_in(rows, req_tag(run.user_id))
    if own is not None and own in _tag_ids(carrier):
        return
    who = requesters_in(carrier, rows)
    if who:
        said = f"it was requested by {', '.join(str(w) for w in who)}"
    else:
        said = "nobody requested it through this skill"
    raise ServarrError(
        f"A requester may start a search only for their own requests, and {said}. Nothing "
        f"was sent; an admin can search for it.", "not_your_request", app=client.app,
        requested_by=who,
    )


def cmd_search_now(run: Run, args) -> dict:
    """Ask the app to search for one item now; one ``POST command``."""
    kind = args.kind
    item_id, ident = item_arguments(args, "search-now")
    client = run.client(KIND_APP[kind])
    item = find_item(client, kind, item_id, ident)
    library = item["id"]
    if run.tier.name != access.ADMIN:
        _check_owner(run, client, kind, item)
    if item.get("monitored") is not True:
        raise ServarrError(
            f"This {kind} is not monitored, so a search would find releases and grab "
            f"nothing. Nothing was sent; it has to be monitored in {client.app} first.",
            "not_monitored", app=client.app, library_id=library,
        )
    body = _search_command(kind, library)
    command = client.request("POST", "command", json=body,
                             lookup=f"why --kind {kind} --library-id {library}")
    command = command if isinstance(command, dict) else {}
    return {"status": "ok", "app": client.app, "kind": kind, "library_id": library,
            "command": body["name"], "command_id": number(command.get("id")),
            "state": selector(command.get("status"))}


# --------------------------------------------------------------------------- #
# indexers
# --------------------------------------------------------------------------- #

#: Problems first: an indexer prowlarr has backed off, then one switched off.
INDEXER_STATES = ("failing", "disabled_by_user", "ok")


def _indexer_status(rows: list[dict]) -> dict[int, dict]:
    out = {}
    for row in rows:
        indexer = _positive_id(row.get("indexerId"))
        if indexer is not None:
            out[indexer] = row
    return out


def _indexer_row(indexer: dict, status: dict, profiles: dict, now: datetime) -> dict:
    """One indexer and its state. Prowlarr backs a failing indexer off until
    ``disabledTill``; a time already past leaves it ``ok``."""
    until = status.get("disabledTill", status.get("disableUntil"))
    until_time = parse_time(until)
    failing = until_time is not None and until_time > now
    if indexer.get("enable") is False:
        state = "disabled_by_user"
    elif failing:
        state = "failing"
    else:
        state = "ok"
    profile = profiles.get(indexer.get("appProfileId"))
    return {
        "id": _positive_id(indexer.get("id")),
        "name": _text(indexer.get("name")),
        "enable": indexer.get("enable") is True,
        "protocol": selector(indexer.get("protocol")),
        "priority": number(indexer.get("priority")),
        "app_profile": selector(profile) if profile is not None else None,
        "state": state,
        "failing_until": selector(until) if failing else None,
        "most_recent_failure": selector(status.get("mostRecentFailure")),
        "initial_failure": selector(status.get("initialFailure")),
    }


def _test_result(result: dict, names: dict) -> dict:
    failures = []
    raw = result.get("validationFailures")
    for failure in raw if isinstance(raw, list) else []:
        if not isinstance(failure, dict):
            continue
        message = failure.get("errorMessage")
        if not isinstance(message, str) or not message:
            continue
        prop = failure.get("propertyName")
        failures.append(fence(f"{prop}: {message}" if isinstance(prop, str) and prop
                              else message))
    indexer = _positive_id(result.get("id"))
    return {"id": indexer, "name": names.get(indexer), "is_valid": result.get("isValid") is True,
            "failures": failures}


def _test_all(client: ArrClient, names: dict, limit: int) -> dict:
    """``POST indexer/testall``, sent once. Prowlarr answers 400 with the same
    per-indexer list when any indexer fails, so that status is an answer."""
    data = client.request("POST", "indexer/testall", kind="release", lookup="indexers",
                          answer_on=(400,))
    results = [_test_result(row, names)
               for row in as_rows(data, client.app, "indexer/testall")]
    results.sort(key=lambda r: (r["is_valid"], r["id"] or 0))
    return {"total": len(results), "invalid": sum(1 for r in results if not r["is_valid"]),
            "results": results[:limit]}


def cmd_indexers(run: Run, args) -> dict:
    """Prowlarr's indexers, problems first, with its health checks; with
    ``--test``, every indexer tested."""
    limit = limit_of(args)
    client = run.client("prowlarr")
    indexers = as_rows(client.request("GET", "indexer"), client.app, "indexer")
    status = _indexer_status(as_rows(client.request("GET", "indexerstatus"), client.app,
                                     "indexerstatus"))
    profiles = {row.get("id"): row.get("name")
                for row in as_rows(client.request("GET", "appprofile"), client.app,
                                   "appprofile")}
    health = as_rows(client.request("GET", "health"), client.app, "health")
    now = run.now()

    rows = [_indexer_row(indexer, status.get(indexer.get("id"), {}), profiles, now)
            for indexer in indexers]
    rows.sort(key=lambda r: (INDEXER_STATES.index(r["state"]),
                             r["priority"] if r["priority"] is not None else 0, r["id"] or 0))
    counts = {state: sum(1 for r in rows if r["state"] == state)
              for state in ("ok", "disabled_by_user", "failing")}
    envelope = {"status": "ok", "app": client.app, "total": len(rows), "counts": counts,
                "indexers": rows[:limit],
                "health": [_health_item(item) for item in health]}
    if args.test:
        names = {r["id"]: r["name"] for r in rows if r["id"] is not None}
        envelope["test"] = _test_all(client, names, limit)
    return envelope
