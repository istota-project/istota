"""Location query helpers shared between the web API and the location skill.

These functions are pure SQL + lightweight math — no FastAPI/HTTP/auth dependencies —
so they can be called from both `web_app.py` and skill subprocesses.
"""

import sqlite3
from datetime import datetime

from .db import (
    delete_dismissed_cluster,
    get_place_by_id,
    insert_dismissed_cluster,
    list_dismissed_clusters,
)
from .geo import haversine


def _location_place_stats(db_path: str, user_id: str, place_id: int) -> dict | None:
    """Visit statistics for a place, derived from ping data.

    Groups pings into visits by checking whether the user was seen elsewhere
    during gaps. A gap only splits a visit if there are pings at a different
    place (or unassigned pings far away) in between — GPS dropout while
    stationary indoors doesn't break a visit. Walk-bys (< 3 pings) are
    filtered out.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        place = get_place_by_id(conn, place_id)
        if not place or place.user_id != user_id:
            return None

        rows = conn.execute(
            """
            SELECT timestamp FROM location_pings
            WHERE user_id = ? AND place_id = ?
            ORDER BY timestamp ASC
            """,
            (user_id, place_id),
        ).fetchall()

        if not rows:
            return {
                "place_id": place_id,
                "total_visits": 0,
                "first_visit": None,
                "last_visit": None,
                "avg_duration_min": None,
                "total_duration_min": None,
                "longest_visit_min": None,
            }

        min_pings = 3  # filter out walk-bys
        segments: list[tuple[str, str, int]] = []  # (first_ts, last_ts, ping_count)
        visit_start = rows[0]["timestamp"]
        prev_ts = visit_start
        ping_count = 1

        for row in rows[1:]:
            ts = row["timestamp"]
            elsewhere = conn.execute(
                """
                SELECT 1 FROM location_pings
                WHERE user_id = ? AND place_id IS NOT ? AND place_id IS NOT NULL
                  AND timestamp > ? AND timestamp < ?
                LIMIT 1
                """,
                (user_id, place_id, prev_ts, ts),
            ).fetchone()
            if elsewhere:
                segments.append((visit_start, prev_ts, ping_count))
                visit_start = ts
                ping_count = 1
            else:
                ping_count += 1
            prev_ts = ts
        segments.append((visit_start, prev_ts, ping_count))

        visits = [(s, e) for s, e, c in segments if c >= min_pings]

        if not visits:
            return {
                "place_id": place_id,
                "total_visits": 0,
                "first_visit": None,
                "last_visit": None,
                "avg_duration_min": None,
                "total_duration_min": None,
                "longest_visit_min": None,
            }

        durations_sec = []
        for start, end in visits:
            try:
                dur = (
                    datetime.fromisoformat(end) - datetime.fromisoformat(start)
                ).total_seconds()
                durations_sec.append(dur)
            except (ValueError, TypeError):
                durations_sec.append(0)

        total_sec = sum(durations_sec)
        avg_sec = total_sec / len(durations_sec) if durations_sec else 0
        longest_sec = max(durations_sec) if durations_sec else 0

        return {
            "place_id": place_id,
            "total_visits": len(visits),
            "first_visit": visits[0][0],
            "last_visit": visits[-1][0],
            "avg_duration_min": round(avg_sec / 60),
            "total_duration_min": round(total_sec / 60),
            "longest_visit_min": round(longest_sec / 60),
        }
    finally:
        conn.close()


def _location_list_dismissed(db_path: str, user_id: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = list_dismissed_clusters(conn, user_id)
        return {
            "dismissed": [
                {
                    "id": r["id"],
                    "lat": r["lat"],
                    "lon": r["lon"],
                    "radius_meters": r["radius_meters"],
                    "dismissed_at": r["dismissed_at"],
                }
                for r in rows
            ]
        }
    finally:
        conn.close()


def _location_dismiss_cluster(db_path: str, user_id: str, data: dict) -> dict:
    radius = int(data.get("radius_meters", 100))
    conn = sqlite3.connect(db_path)
    try:
        cluster_id = insert_dismissed_cluster(
            conn, user_id, float(data["lat"]), float(data["lon"]), radius,
        )
        conn.commit()
        return {
            "id": cluster_id,
            "lat": float(data["lat"]),
            "lon": float(data["lon"]),
            "radius_meters": radius,
        }
    finally:
        conn.close()


def _location_restore_dismissed(db_path: str, user_id: str, cluster_id: int) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        deleted = delete_dismissed_cluster(conn, user_id, cluster_id)
        conn.commit()
        return deleted
    finally:
        conn.close()


def _location_discover_places(db_path: str, user_id: str, min_pings: int = 10) -> dict:
    """Find clusters of stationary pings not assigned to any place."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT ROUND(lat, 4) as rlat, ROUND(lon, 4) as rlon,
                   AVG(lat) as avg_lat, AVG(lon) as avg_lon,
                   COUNT(*) as cnt,
                   MIN(timestamp) as first_seen, MAX(timestamp) as last_seen
            FROM location_pings
            WHERE user_id = ? AND place_id IS NULL
              AND (activity_type IS NULL OR activity_type = 'stationary')
            GROUP BY rlat, rlon
            HAVING cnt >= ?
            ORDER BY cnt DESC
            """,
            (user_id, max(3, min_pings // 3)),
        ).fetchall()

        points = [
            {"lat": r["avg_lat"], "lon": r["avg_lon"], "count": r["cnt"],
             "first_seen": r["first_seen"], "last_seen": r["last_seen"]}
            for r in rows
        ]

        clusters: list[dict] = []
        used = [False] * len(points)
        for i, p in enumerate(points):
            if used[i]:
                continue
            cluster_lat = p["lat"] * p["count"]
            cluster_lon = p["lon"] * p["count"]
            cluster_count = p["count"]
            first = p["first_seen"]
            last = p["last_seen"]
            members = [(p["lat"], p["lon"])]
            used[i] = True

            for j in range(i + 1, len(points)):
                if used[j]:
                    continue
                if haversine(p["lat"], p["lon"], points[j]["lat"], points[j]["lon"]) <= 200:
                    cluster_lat += points[j]["lat"] * points[j]["count"]
                    cluster_lon += points[j]["lon"] * points[j]["count"]
                    cluster_count += points[j]["count"]
                    members.append((points[j]["lat"], points[j]["lon"]))
                    if points[j]["first_seen"] < first:
                        first = points[j]["first_seen"]
                    if points[j]["last_seen"] > last:
                        last = points[j]["last_seen"]
                    used[j] = True

            if cluster_count >= min_pings:
                center_lat = cluster_lat / cluster_count
                center_lon = cluster_lon / cluster_count
                spread = max(
                    (haversine(center_lat, center_lon, mlat, mlon) for mlat, mlon in members),
                    default=0.0,
                )
                radius_meters = int(min(300, max(50, round(spread + 25))))
                clusters.append({
                    "lat": center_lat,
                    "lon": center_lon,
                    "total_pings": cluster_count,
                    "first_seen": first,
                    "last_seen": last,
                    "radius_meters": radius_meters,
                })

        existing = conn.execute(
            "SELECT lat, lon, radius_meters FROM places WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        dismissed = conn.execute(
            "SELECT lat, lon, radius_meters FROM dismissed_clusters WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        filtered = []
        for c in clusters:
            too_close = False
            for ep in existing:
                dist = haversine(c["lat"], c["lon"], ep["lat"], ep["lon"])
                if dist <= max(ep["radius_meters"], 200):
                    too_close = True
                    break
            if too_close:
                continue
            for dz in dismissed:
                if haversine(c["lat"], c["lon"], dz["lat"], dz["lon"]) <= dz["radius_meters"]:
                    too_close = True
                    break
            if not too_close:
                filtered.append(c)

        return {"clusters": filtered}
    finally:
        conn.close()
