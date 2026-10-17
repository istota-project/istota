"""A location share's map as a PNG, for the surfaces that cannot draw one.

Talk and WhatsApp get a 640 x 360 picture of the same projected observation
the share's text discloses; web draws its own interactive card and never comes
here. The picture is the `share-map` render bundle (`web/src/render/share-map/`),
which mounts the same `ShareMap` component the web card uses, so the two cannot
drift; `istota.browser.document_render` sends it to the browser service.

The payload is checked here, because this side owns what it means: the
precision and freshness enums, finite coordinates (an area's already rounded),
plain labels, and a basemap of the resolver's shape whose attribution carries
plain http(s) links only, since MapLibre inserts it as HTML.

`render_location_map` makes one attempt and says why it has no picture;
`render_location_png` is the same call for a caller that only wants the
picture. A missing picture is the documented fallback, never an error: the
text answer and its map link go out regardless. Reasons are stable strings,
and `SKIP_REASONS` are the ones a delivery records as `skipped` rather than
`failed`. Nothing logged here carries a coordinate, a URL or a path.

The PNG is checked (signature, exactly 640 x 360, at most 1 MiB) and then
re-encoded through the Pillow path WhatsApp's outbound images take, which
drops every chunk the browser wrote beside the pixels.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import secrets
import struct
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterator
from urllib.parse import urlsplit

from istota.browser.document_render import render_document
from istota.webui.map_basemap import basemap_for_user

if TYPE_CHECKING:
    from istota.config import Config

logger = logging.getLogger(__name__)

WIDTH = 640
HEIGHT = 360
MAX_PNG_BYTES = 1024 * 1024
#: Web Mercator's edge, as `shareMap.ts` draws it.
MERCATOR_MAX_LAT = 85.0511
#: How long to wait for the browser's admission lock. A share's text is
#: already out or about to be; a map is not worth queueing behind a browse.
ADMISSION_WAIT_S = 2.0
#: The whole call, admission included.
TOTAL_TIMEOUT_S = 10.0

BUNDLE = "share-map"
VIEWPORT = {"width": WIDTH, "height": HEIGHT}

#: Reasons that mean "no map was possible here", not "the map failed".
SKIP_REASONS = frozenset({"render_unavailable", "not_mercator_safe"})

_PASSED_ON = frozenset({"render_unavailable", "capacity", "render_timeout"})
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

PRECISIONS = ("exact", "area")
FRESHNESS = ("fresh", "fresh_clock_skew", "last_known")
PROVIDERS = ("openfreemap", "carto", "osm", "custom")
BASEMAP_KINDS = ("style", "raster")
_MAX_ACCURACY_M = 100_000
_MAX_LABEL = 200
_MAX_URL = 2048
_MAX_ATTRIBUTION = 1000
_OBSERVED_AT = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})?"
)
# The one markup an attribution may carry: a plain http(s) link, as the
# resolver's own provider rows write it.
_ATTRIBUTION_LINK = re.compile(r'<a href="https?://[^"<>\s]+">|</a>')
_URL_FORBIDDEN = re.compile(r"[\x00-\x20\x7f\"'<>\\`]")


@dataclass(frozen=True)
class RenderedMap:
    """A checked, re-encoded map. `data` is the PNG; `sha256` is its hex digest."""

    data: bytes
    sha256: str
    width: int
    height: int


@dataclass(frozen=True)
class MapRender:
    """One attempt: the picture, or the stable reason there is none."""

    rendered: RenderedMap | None
    reason: str | None


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _plain_label(value) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= _MAX_LABEL
            and "<" not in value and ">" not in value
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _http_url(value) -> bool:
    if not isinstance(value, str) or not value or len(value) > _MAX_URL:
        return False
    if _URL_FORBIDDEN.search(value):
        return False
    try:
        parts = urlsplit(value)
        parts.port
    except ValueError:
        return False
    return (parts.scheme in ("http", "https") and bool(parts.hostname)
            and parts.username is None and parts.password is None)


def _attribution_ok(value) -> bool:
    if not isinstance(value, str) or len(value) > _MAX_ATTRIBUTION:
        return False
    rest = _ATTRIBUTION_LINK.sub("", value)
    return "<" not in rest and ">" not in rest


def _observation_payload(observation: dict) -> dict | None:
    """The projection the page draws, from a card's stored observation, or None."""
    try:
        precision = observation["precision"]
        lat, lon = observation["lat"], observation["lon"]
        accuracy = observation.get("accuracy_m")
        observed_at = observation["observed_at"]
        owner_user_id = observation["owner_user_id"]
    except (KeyError, TypeError):
        return None
    freshness = observation.get("freshness") or "fresh"
    if precision not in PRECISIONS or freshness not in FRESHNESS:
        return None
    if not _finite(lat) or not _finite(lon) or abs(lat) > 90 or abs(lon) > 180:
        return None
    if precision == "area":
        # Rounded before it left the location module; a value that is not is
        # not drawn as a cell around it.
        accuracy = None
        if round(lat, 2) != lat or round(lon, 2) != lon:
            return None
    elif accuracy is not None and (not _finite(accuracy)
                                   or not 0 <= accuracy <= _MAX_ACCURACY_M):
        return None
    if not isinstance(observed_at, str) or not _OBSERVED_AT.fullmatch(observed_at):
        return None
    if not _plain_label(owner_user_id):
        return None
    return {
        "precision": precision,
        "lat": lat,
        "lon": lon,
        "accuracy_m": accuracy,
        "observed_at": observed_at,
        "freshness": freshness,
        "owner_user_id": owner_user_id,
    }


def _basemap_payload(config: "Config", actor_user_id: str) -> dict | None:
    """The actor's basemap as the page takes it, or None when the page should not draw it."""
    spec = basemap_for_user(config, actor_user_id)
    if spec.provider not in PROVIDERS or spec.kind not in BASEMAP_KINDS:
        return None
    urls = [spec.light] + ([spec.dark] if spec.dark else [])
    if not all(_http_url(url) for url in urls):
        return None
    if spec.kind == "raster" and not all(
            all(token in url for token in ("{z}", "{x}", "{y}")) for url in urls):
        return None
    if not _attribution_ok(spec.attribution):
        return None
    return {
        "provider": spec.provider,
        "kind": spec.kind,
        "light": spec.light,
        "dark": spec.dark,
        "attribution": spec.attribution,
    }


def png_dimensions(data: bytes) -> tuple[int, int] | None:
    """`(width, height)` from a PNG's header, or None when it is not one."""
    if len(data) < 24 or data[:8] != _PNG_SIGNATURE or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def _checked(data: bytes) -> RenderedMap | None:
    if len(data) > MAX_PNG_BYTES or png_dimensions(data) != (WIDTH, HEIGHT):
        return None
    from istota.transport.whatsapp.outbound_media import reencode_bytes  # noqa: PLC0415

    encoded = reencode_bytes(data)
    if encoded is None or encoded[1] != "image/png":
        return None
    clean = encoded[0]
    if png_dimensions(clean) != (WIDTH, HEIGHT):
        return None
    return RenderedMap(clean, hashlib.sha256(clean).hexdigest(), WIDTH, HEIGHT)


def _skip(reason: str) -> MapRender:
    logger.info("location.share_map.no_render reason=%s", reason)
    return MapRender(None, reason)


def render_location_map(
    config: "Config", *, actor_user_id: str, observation: dict,
) -> MapRender:
    """Draw one card's observation through the browser service. Never raises.

    `actor_user_id` is whose browser capacity and basemap the render uses:
    the user whose task or approved post the map rides on.
    """
    payload_observation = _observation_payload(observation)
    if payload_observation is None:
        return _skip("bad_observation")
    if abs(payload_observation["lat"]) > MERCATOR_MAX_LAT:
        return _skip("not_mercator_safe")
    owner = observation.get("owner_label") or payload_observation["owner_user_id"]
    if not _plain_label(owner):
        return _skip("bad_observation")
    basemap = _basemap_payload(config, actor_user_id)
    if basemap is None:
        # The operator's basemap is not one this page draws: no map here.
        return _skip("render_unavailable")
    payload = {
        "observation": payload_observation,
        "basemap": basemap,
        "owner": owner,
        "viewport": dict(VIEWPORT),
    }
    attempt = render_document(
        config, actor_user_id=actor_user_id, bundle=BUNDLE, payload=payload,
        viewport=dict(VIEWPORT), timeout_s=TOTAL_TIMEOUT_S, admission_wait_s=ADMISSION_WAIT_S,
    )
    if attempt.data is None:
        return _skip(attempt.reason if attempt.reason in _PASSED_ON else "render_failed")
    rendered = _checked(attempt.data)
    if rendered is None:
        return _skip("bad_png")
    return MapRender(rendered, None)


def render_location_png(
    config: "Config", *, actor_user_id: str, observation: dict,
) -> RenderedMap | None:
    """`render_location_map`'s picture, or None. The reason is logged there."""
    return render_location_map(
        config, actor_user_id=actor_user_id, observation=observation,
    ).rendered


@contextmanager
def private_file(rendered: RenderedMap, directory: Path) -> Iterator[Path]:
    """The PNG written 0600 under an opaque name in `directory`, removed on the way out.

    For a delivery that needs a path (an upload). The name says nothing about
    whose map it is or where.
    """
    path = Path(directory) / f"map-{secrets.token_hex(12)}.png"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        try:
            view = memoryview(rendered.data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
        yield path
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
