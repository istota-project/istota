"""The house kit: the four files inlined at the start of every installed app document.

``web/vite.apps.config.ts`` builds them from ``web/src/apps/kit/`` into
``<apps dir>/kit/`` and writes ``<apps dir>/manifest.json``, which maps each
output file's path relative to the apps dir to its sha256. :func:`load_kit`
reads the four, refuses any the manifest does not vouch for, and keeps the
result for the life of the process.

A missing or mismatched kit only stops new documents being built. A version
already installed carries its own copy of the kit inside its document, so the
frame route never reads this.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path

from istota.webui.static_dir import resolve_apps_dir

#: Manifest key -> the `Kit` field it fills, in the order the document inlines them.
KIT_FILES = {
    "kit/guard.js": "guard_js",
    "kit/istota-app.js": "client_js",
    "kit/components.js": "components_js",
    "kit/istota.css": "css",
}


@dataclass(frozen=True)
class Kit:
    guard_js: str
    client_js: str
    components_js: str
    css: str


class KitMissing(Exception):
    """The kit is not built, or not the build its manifest describes.

    ``str()`` is the plain sentence a user is shown; ``reason`` is for the log:
    ``no_manifest``, ``not_in_manifest``, ``no_file`` or ``digest_mismatch``.
    """

    def __init__(self, reason: str, path: str = ""):
        super().__init__("the apps kit is not built")
        self.reason = reason
        self.path = path


_lock = threading.Lock()
_cached: tuple[Path, Kit] | None = None


def reset_cache() -> None:
    """Forget the loaded kit (tests, and a rebuilt checkout)."""
    global _cached
    with _lock:
        _cached = None


def _read_manifest(directory: Path) -> dict:
    try:
        manifest = json.loads((directory / "manifest.json").read_bytes())
    except (OSError, ValueError) as exc:
        raise KitMissing("no_manifest", str(directory / "manifest.json")) from exc
    if not isinstance(manifest, dict):
        raise KitMissing("no_manifest", str(directory / "manifest.json"))
    return manifest


def _read_checked_bytes(directory: Path, relative: str, manifest: dict) -> bytes:
    expected = manifest.get(relative)
    if not isinstance(expected, str):
        raise KitMissing("not_in_manifest", relative)
    try:
        data = (directory / relative).read_bytes()
    except OSError as exc:
        raise KitMissing("no_file", relative) from exc
    if hashlib.sha256(data).hexdigest() != expected:
        raise KitMissing("digest_mismatch", relative)
    return data


def _read_checked(directory: Path, relative: str, manifest: dict) -> str:
    data = _read_checked_bytes(directory, relative, manifest)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KitMissing("digest_mismatch", relative) from exc


def load_starter_files(app_id: str) -> dict[str, bytes]:
    """A starter app's folder from the apps build, as bytes by path relative to it.

    Every file is one the build manifest lists under ``starter/<app_id>/`` and
    is checked against its digest. Raises :class:`KitMissing` when the build
    holds no such app or a file does not match. Not cached: it is read only
    when starter apps are seeded.
    """
    directory = resolve_apps_dir()
    manifest = _read_manifest(directory)
    prefix = f"starter/{app_id}/"
    files = {}
    for relative in sorted(manifest):
        if not relative.startswith(prefix):
            continue
        files[relative[len(prefix):]] = _read_checked_bytes(directory, relative, manifest)
    if not files:
        raise KitMissing("not_in_manifest", prefix)
    return files


def load_kit() -> Kit:
    """The kit from the apps build, checked against its manifest. Raises :class:`KitMissing`."""
    global _cached
    directory = resolve_apps_dir()
    with _lock:
        if _cached is not None and _cached[0] == directory:
            return _cached[1]
    manifest = _read_manifest(directory)
    fields = {
        field: _read_checked(directory, relative, manifest)
        for relative, field in KIT_FILES.items()
    }
    kit = Kit(**fields)
    with _lock:
        _cached = (directory, kit)
    return kit
