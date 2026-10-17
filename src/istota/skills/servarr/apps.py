"""The arr apps the skill speaks to: API prefixes, paths and the media they serve.

The app names, their order and the add defaults each takes are
`istota.config`'s (`SERVARR_APPS`, `SERVARR_DEFAULT_FIELDS`), so the loader can
validate `[servarr]` without importing this package; this table adds what only
the skill needs. Nothing here holds a URL or a key: a connection is passed to
the client by the caller, which is what lets a later spec add named instances
as a resolution layer rather than a rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass

from istota.config import SERVARR_APPS, SERVARR_DEFAULT_FIELDS
from istota.lib.url_redact import redact_userinfo


@dataclass(frozen=True)
class AppKind:
    app: str             # "sonarr"
    api: str             # "/api/v3"
    kinds: tuple         # media kinds it serves: ("show",)
    lookup: str          # "series/lookup"
    collection: str      # "series"
    id_field: str        # "tvdbId"
    search_command: str  # "SeriesSearch"
    min_major: int       # the oldest major version the paths and payloads fit


# Sonarr v4 still serves /api/v3; Lidarr and Prowlarr are /api/v1. Sonarr v3
# needs `languageProfileId` on add, which this skill does not send, hence 4.
APPS: dict[str, AppKind] = {
    "sonarr": AppKind("sonarr", "/api/v3", ("show",), "series/lookup", "series", "tvdbId",
                      "SeriesSearch", 4),
    "radarr": AppKind("radarr", "/api/v3", ("film",), "movie/lookup", "movie", "tmdbId",
                      "MoviesSearch", 5),
    "lidarr": AppKind("lidarr", "/api/v1", ("artist", "album"), "artist/lookup", "artist",
                      "foreignArtistId", "AlbumSearch", 2),
    "prowlarr": AppKind("prowlarr", "/api/v1", (), "", "", "", "", 1),
}

KIND_APP = {"film": "radarr", "show": "sonarr", "artist": "lidarr", "album": "lidarr"}

#: Every multi-app verb visits apps in this order.
APP_ORDER: tuple[str, ...] = SERVARR_APPS
MEDIA_APPS: tuple[str, ...] = tuple(name for name in APP_ORDER if APPS[name].kinds)
DEFAULT_FIELDS = SERVARR_DEFAULT_FIELDS


def display_url(url: str) -> str:
    """The configured URL with any user name or password removed, for output."""
    return redact_userinfo(url, remove=True)


def configured(config) -> list[str]:
    """The known apps that have a connection, in the fixed order."""
    return [name for name in APP_ORDER if name in config.servarr.apps]


def missing(config) -> list[str]:
    return [name for name in APP_ORDER if name not in config.servarr.apps]


def ignored(config) -> list[str]:
    """App tables the skill does not know, which it never uses."""
    return sorted(name for name in config.servarr.apps if name not in APPS)
