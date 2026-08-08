"""Data types for the location module.

The :class:`LocationContext` is the per-user runtime handle (workspace +
db path). Built by :func:`istota.location.workspace.synthesize_location_context`
or :func:`istota.location._loader.resolve_for_user`. Every other function
in the module takes a context (or a connection rooted at its db_path) and
operates on it.

The ``Place``, ``Visit``, ``LocationState`` and ``Cluster`` dataclasses
mirror the rows in the per-user ``location.db``. They lost their
``user_id`` field with the per-user split — the file is the user scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LocationContext:
    user_id: str
    workspace: Path
    db_path: Path


@dataclass
class Place:
    id: int
    name: str
    lat: float
    lon: float
    radius_meters: int
    category: str | None
    created_at: str = ""
    notes: str | None = None


@dataclass
class Visit:
    id: int
    place_id: int | None
    place_name: str
    entered_at: str
    exited_at: str | None
    duration_sec: int | None
    ping_count: int


@dataclass
class LocationState:
    current_place_id: int | None
    current_visit_id: int | None
    consecutive_count: int
    last_ping_place_id: int | None
    exit_started_at: str | None = None


@dataclass
class Cluster:
    id: int
    lat: float
    lon: float
    radius_meters: int
    dismissed_at: str


@dataclass
class LocationPing:
    id: int
    timestamp: str
    received_at: str
    lat: float
    lon: float
    altitude: float | None
    accuracy: float | None
    speed: float | None
    course: float | None
    battery: float | None
    activity_type: str | None
    wifi: str | None
    place_id: int | None
    visit_id: int | None
    # Defaulted so a caller constructing one by hand (and the Garmin path,
    # which mints none) needs no change. NULL for every ping that predates
    # the native client.
    client_id: str | None = None
    # Device-reported accuracy of the vertical fix, in metres. Negative is
    # iOS's "the altitude is invalid" signal, which the ingest acts on.
    vertical_accuracy: float | None = None
    # True when the point is a coordinate the client *declared* (its
    # wifi-zone feature) rather than a fix it measured. Such a point has no
    # altitude at all — see ISSUE-229.
    wifi_zone: bool = False
