"""The website data stores the app makes for remote profiles, found and removed.

WebKit keeps a `WKWebsiteDataStore(forIdentifier:)` outside the app's state
directory, under `~/Library/WebKit/<bundle id>/WebsiteDataStore/<uuid>/`, so a
tier test that opens a remote profile removes the store it made.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

BUNDLE_ID = "org.istota.desktop"


def data_store_id(instance: str, profile: str) -> uuid.UUID:
    """`profiles::data_store_id`: FNV-1a 64 twice, as a version 8 UUID."""
    name = f"istota-desktop:{instance}:{profile}".encode()

    def fnv(offset: int) -> int:
        h = offset
        for byte in name:
            h = ((h ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
        return h

    raw = bytearray(fnv(0xCBF29CE484222325).to_bytes(8, "big") + fnv(0x6C62272E07BB0142).to_bytes(8, "big"))
    raw[6] = (raw[6] & 0x0F) | 0x80
    raw[8] = (raw[8] & 0x3F) | 0x80
    return uuid.UUID(bytes=bytes(raw))


def on_disk(*ids: uuid.UUID) -> list[Path]:
    root = Path.home() / "Library" / "WebKit" / BUNDLE_ID / "WebsiteDataStore"
    names = {str(i).lower() for i in ids}
    if not root.is_dir():
        return []
    return [p for p in root.iterdir() if p.name.lower() in names]


def remove(*ids: uuid.UUID) -> None:
    for store in on_disk(*ids):
        shutil.rmtree(store, ignore_errors=True)
