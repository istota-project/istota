"""Shared geographic utility functions."""

import math
import sqlite3

_EARTH_RADIUS_M = 6_371_000  # meters


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in meters between two lat/lon points."""
    lat1_r, lon1_r = math.radians(lat1), math.radians(lon1)
    lat2_r, lon2_r = math.radians(lat2), math.radians(lon2)

    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return _EARTH_RADIUS_M * c


def reverse_geocode(lat: float, lon: float, conn: sqlite3.Connection) -> dict:
    """Reverse geocode coordinates, using DB cache when available."""
    from istota.db import get_reverse_geocode, cache_reverse_geocode

    cached = get_reverse_geocode(conn, lat, lon)
    if cached:
        cached["source"] = "cache"
        return cached

    try:
        from geopy.geocoders import Nominatim

        geolocator = Nominatim(user_agent="istota")
        result = geolocator.reverse(
            f"{lat}, {lon}", exactly_one=True, language="en", timeout=10,
        )
        if result:
            addr = result.raw.get("address", {})
            out = {
                "display_name": result.address,
                "neighborhood": addr.get("quarter") or addr.get("neighbourhood"),
                "suburb": addr.get("suburb"),
                "road": addr.get("road"),
                "city": addr.get("city"),
                "raw": addr,
                "source": "nominatim",
            }
            cache_reverse_geocode(conn, lat, lon, out)
            conn.commit()
            return out
    except Exception as e:
        return {"error": str(e), "source": "error"}

    return {"error": "no result", "source": "error"}


def _parse_ts(ts: str):
    """Parse an ISO-8601 timestamp to a tz-aware datetime (assumes UTC if naive)."""
    from datetime import datetime, timezone

    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def cluster_dwell_seconds(cluster: dict) -> float:
    """Duration of a cluster in seconds (first_ts to last_ts)."""
    return (_parse_ts(cluster["last_ts"]) - _parse_ts(cluster["first_ts"])).total_seconds()


def _timestamp_gap_seconds(ts_a: str, ts_b: str) -> float:
    """Seconds between two ISO-8601 timestamps."""
    return abs((_parse_ts(ts_b) - _parse_ts(ts_a)).total_seconds())


def dedupe_near_duplicate_pings(
    pings: list[dict],
    window_seconds: float = 5.0,
) -> list[dict]:
    """Drop dual-source artifacts where the phone reports two fixes within seconds.

    Overland on iOS occasionally emits one high-accuracy GPS fix plus one
    low-accuracy cell/Wi-Fi fix within a few seconds. The cell/Wi-Fi fix is
    typically anchored away from the real position (e.g., near home/cell tower)
    and can carry ``activity_type=None``. Left in, these create zigzag patterns
    that fragment clusters and break trip detection.

    For each pair of consecutive pings within ``window_seconds``:
      1. If exactly one has ``activity_type`` set → keep it.
      2. Else if one has smaller ``accuracy`` → keep it.
      3. Else (ambiguous) → keep both. We'd rather preserve a real fix than
         drop one based on a guess.

    Pings must be sorted by timestamp ascending. Input is not mutated.
    See ISSUE-059 for the data analysis behind these rules.
    """
    if not pings:
        return []

    kept: list[dict] = [pings[0]]
    for current in pings[1:]:
        prev = kept[-1]
        try:
            dt = _timestamp_gap_seconds(prev["timestamp"], current["timestamp"])
        except Exception:
            kept.append(current)
            continue

        if dt > window_seconds:
            kept.append(current)
            continue

        prev_act = prev.get("activity_type")
        curr_act = current.get("activity_type")
        if prev_act and not curr_act:
            continue  # drop current
        if curr_act and not prev_act:
            kept[-1] = current
            continue

        prev_acc = prev.get("accuracy")
        curr_acc = current.get("accuracy")
        if prev_acc is not None and curr_acc is not None:
            if prev_acc < curr_acc:
                continue  # prev is better, drop current
            if curr_acc < prev_acc:
                kept[-1] = current
                continue

        kept.append(current)

    return kept


def cluster_pings(
    pings: list[dict],
    radius_m: float = 200,
    max_gap_seconds: float = 300,
) -> list[dict]:
    """Cluster pings into stops based on spatial proximity.

    Pings must be sorted by timestamp ascending.
    Returns list of clusters with centroid, time range, and ping count.

    Split conditions (any triggers a new cluster):
    - Ping is farther than ``radius_m`` from the running centroid.
    - Ping is farther than ``radius_m * 1.5`` from the cluster's first ping
      (origin anchor — prevents centroid drift along a route).
    - Time gap between consecutive pings exceeds ``max_gap_seconds``.
    """
    if not pings:
        return []

    clusters: list[dict] = []
    current = {
        "pings": [pings[0]],
        "lat_sum": pings[0]["lat"],
        "lon_sum": pings[0]["lon"],
    }
    origin_lat = pings[0]["lat"]
    origin_lon = pings[0]["lon"]
    origin_limit = radius_m * 1.5

    for ping in pings[1:]:
        centroid_lat = current["lat_sum"] / len(current["pings"])
        centroid_lon = current["lon_sum"] / len(current["pings"])
        dist_centroid = haversine(centroid_lat, centroid_lon, ping["lat"], ping["lon"])
        dist_origin = haversine(origin_lat, origin_lon, ping["lat"], ping["lon"])

        # Time gap between this ping and the previous one
        prev_ts = current["pings"][-1]["timestamp"]
        gap = _timestamp_gap_seconds(prev_ts, ping["timestamp"])

        if dist_centroid <= radius_m and dist_origin <= origin_limit and gap <= max_gap_seconds:
            current["pings"].append(ping)
            current["lat_sum"] += ping["lat"]
            current["lon_sum"] += ping["lon"]
        else:
            clusters.append(_finalize_cluster(current))
            current = {
                "pings": [ping],
                "lat_sum": ping["lat"],
                "lon_sum": ping["lon"],
            }
            origin_lat = ping["lat"]
            origin_lon = ping["lon"]

    clusters.append(_finalize_cluster(current))
    return clusters


def _finalize_cluster(cluster: dict) -> dict:
    n = len(cluster["pings"])
    lat = cluster["lat_sum"] / n
    lon = cluster["lon_sum"] / n
    place_id = None
    place_name = None
    for p in cluster["pings"]:
        pid = p.get("place_id")
        if pid is not None:
            place_id = pid
            place_name = p.get("place_name")
            break
    return {
        "lat": lat,
        "lon": lon,
        "ping_count": n,
        "first_ts": cluster["pings"][0]["timestamp"],
        "last_ts": cluster["pings"][-1]["timestamp"],
        "place_id": place_id,
        "place_name": place_name,
    }


def validate_cluster_places(
    clusters: list[dict],
    places_by_id: dict,
) -> list[dict]:
    """Strip place tags from clusters whose centroid is outside the place radius.

    With ``_finalize_cluster`` propagating ``place_id`` from any member ping,
    a cluster can inherit a place tag from a single grazing ping during a
    drive-by — promoting transit into a phantom stop downstream because
    ``filter_transit_clusters`` bypasses min-pings/min-dwell filters for
    place-matched clusters. This guard requires the cluster centroid itself
    to be within the place's radius, not just one stray ping. See ISSUE-059.

    ``places_by_id`` maps place_id → dict with at least ``lat``, ``lon``,
    ``radius_meters``. Clusters referencing unknown place_ids are left alone
    (we only invalidate when we can prove the centroid is outside).
    Mutates clusters in place and returns them.
    """
    for c in clusters:
        pid = c.get("place_id")
        if pid is None or pid not in places_by_id:
            continue
        place = places_by_id[pid]
        if haversine(c["lat"], c["lon"], place["lat"], place["lon"]) > place["radius_meters"]:
            c["place_id"] = None
            c["place_name"] = None
    return clusters


# Max transit pings between same-location stops before they're treated as
# separate visits.  1-3 stray pings are GPS glitches; 4+ indicates a real trip.
MERGE_TRANSIT_THRESHOLD = 3


def filter_transit_clusters(
    clusters: list[dict],
    min_pings: int = 3,
    min_dwell_seconds: float = 300,
    merge_radius_m: float = 100,
) -> tuple[list[dict], int]:
    """Separate stop clusters from transit clusters.

    Returns (stops, total_transit_pings).  Each stop dict gets a
    ``_transit_pings_before`` key recording how many transit pings were
    filtered since the previous stop.

    Small clusters within *merge_radius_m* of the previous surviving stop
    are absorbed into it rather than discarded.  This handles indoor GPS
    gaps where the phone is stationary but pings are sparse.
    """
    stops: list[dict] = []
    transit_pings = 0
    transit_pings_since_last_stop = 0
    for c in clusters:
        has_place = bool(c["place_name"])
        few_pings = c["ping_count"] < min_pings
        short_dwell = cluster_dwell_seconds(c) < min_dwell_seconds
        if not has_place and (few_pings or short_dwell):
            # Before discarding, check if this fragment is at the same
            # location as the previous stop (indoor GPS gap scenario)
            if stops and haversine(
                stops[-1]["lat"], stops[-1]["lon"], c["lat"], c["lon"]
            ) <= merge_radius_m:
                stops[-1]["last_ts"] = c["last_ts"]
                stops[-1]["ping_count"] += c["ping_count"]
                continue
            transit_pings += c["ping_count"]
            transit_pings_since_last_stop += c["ping_count"]
            continue
        c["_transit_pings_before"] = transit_pings_since_last_stop
        transit_pings_since_last_stop = 0
        stops.append(c)
    return stops, transit_pings


# Default radius (meters) for proximity-based merge of unnamed stops.
MERGE_PROXIMITY_RADIUS_M = 150


def _stop_dwell_seconds(stop: dict) -> float:
    """Dwell time of a stop in seconds (first_ts to last_ts)."""
    return (_parse_ts(stop["last_ts"]) - _parse_ts(stop["first_ts"])).total_seconds()


def merge_consecutive_stops(stops: list[dict], proximity_radius_m: float = MERGE_PROXIMITY_RADIUS_M) -> list[dict]:
    """Merge consecutive stops at the same location.

    Stops separated by significant transit (> ``MERGE_TRANSIT_THRESHOLD``
    filtered pings) are kept separate even if they share a location name.

    For non-saved-place stops, also merges by spatial proximity when
    coordinates are within ``proximity_radius_m`` — handles GPS drift
    causing different reverse-geocoded names for the same physical location.
    When merging by proximity, keeps the name from the longer stop.
    """
    merged: list[dict] = []
    for stop in stops:
        if not merged or stop.get("_transit_pings_before", 0) > MERGE_TRANSIT_THRESHOLD:
            merged.append(stop)
            continue

        prev = merged[-1]
        same_name = prev["location"] == stop["location"]
        # Proximity merge: both stops must be non-saved-place (reverse-geocoded)
        both_unnamed = (
            prev.get("location_source") not in ("saved_place", "saved_place_proximity")
            and stop.get("location_source") not in ("saved_place", "saved_place_proximity")
        )
        nearby = False
        if both_unnamed and not same_name:
            nearby = haversine(prev["lat"], prev["lon"], stop["lat"], stop["lon"]) <= proximity_radius_m

        if same_name or nearby:
            # When merging by proximity, keep the longer stop's name
            if nearby and not same_name:
                if _stop_dwell_seconds(stop) > _stop_dwell_seconds(prev):
                    prev["location"] = stop["location"]
                    prev["location_source"] = stop.get("location_source")
                    prev["road"] = stop.get("road")
                    prev["neighborhood"] = stop.get("neighborhood")
                    prev["suburb"] = stop.get("suburb")
            prev["last_ts"] = stop["last_ts"]
            prev["last_ts_local"] = stop.get("last_ts_local")
            prev["ping_count"] += stop["ping_count"]
        else:
            merged.append(stop)
    return merged
