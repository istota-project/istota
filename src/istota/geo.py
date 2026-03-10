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


def cluster_pings(pings: list[dict], radius_m: float = 200) -> list[dict]:
    """Cluster pings into stops based on spatial proximity.

    Pings must be sorted by timestamp ascending.
    Returns list of clusters with centroid, time range, and ping count.
    """
    if not pings:
        return []

    clusters = []
    current = {
        "pings": [pings[0]],
        "lat_sum": pings[0]["lat"],
        "lon_sum": pings[0]["lon"],
    }

    for ping in pings[1:]:
        centroid_lat = current["lat_sum"] / len(current["pings"])
        centroid_lon = current["lon_sum"] / len(current["pings"])
        dist = haversine(centroid_lat, centroid_lon, ping["lat"], ping["lon"])

        if dist <= radius_m:
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

    clusters.append(_finalize_cluster(current))
    return clusters


def _finalize_cluster(cluster: dict) -> dict:
    n = len(cluster["pings"])
    lat = cluster["lat_sum"] / n
    lon = cluster["lon_sum"] / n
    return {
        "lat": lat,
        "lon": lon,
        "ping_count": n,
        "first_ts": cluster["pings"][0]["timestamp"],
        "last_ts": cluster["pings"][-1]["timestamp"],
        "place_id": cluster["pings"][0].get("place_id"),
        "place_name": cluster["pings"][0].get("place_name"),
    }
