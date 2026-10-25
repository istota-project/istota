"""A fake apps build directory, as `npm run build:apps` lays it out, for the apps tests.

The kit files are tiny stand-ins; what the tests rely on is the layout and the
hash manifest `istota.apps.kit` checks them against.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

#: The starter apps' sources in the checkout, which the apps build copies.
STARTER_SOURCE = Path(__file__).resolve().parents[2] / "web" / "src" / "apps" / "starter"

# The same rule as `shipped()` in web/src/apps/build.ts, which is authoritative:
# a starter folder's tests are not copied into the build.
_TEST_FILE = re.compile(r"\.test\.[cm]?[jt]s$")

KIT_FILES = {
    "kit/guard.js": "(() => { delete window.RTCPeerConnection; })();\n",
    "kit/istota-app.js": "(() => { globalThis.istota = {}; })();\n",
    "kit/components.js": "(() => {})();\n",
    "kit/istota.css": ":root { --space-1: 0.25rem; }\n",
}


def write_build(directory: Path, starter: dict[str, dict[str, str]] | None = None) -> None:
    """Write the kit, any starter apps and ``manifest.json`` into ``directory``."""
    files = dict(KIT_FILES)
    for app_id, app_files in (starter or {}).items():
        for relative, text in app_files.items():
            files[f"starter/{app_id}/{relative}"] = text
    for relative, text in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    manifest = {relative: hashlib.sha256(text.encode()).hexdigest() for relative, text in files.items()}
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def shipped_starter(app_id: str) -> dict[str, str]:
    """The files the apps build ships for starter app ``app_id``, by path relative to its folder."""
    folder = STARTER_SOURCE / app_id
    files = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file() and not _TEST_FILE.search(path.name):
            files[path.relative_to(folder).as_posix()] = path.read_text(encoding="utf-8")
    if not files:
        raise FileNotFoundError(folder)
    return files
