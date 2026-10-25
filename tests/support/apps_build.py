"""A fake apps build directory, as `npm run build:apps` lays it out, for the apps tests.

The kit files are tiny stand-ins; what the tests rely on is the layout and the
hash manifest `istota.apps.kit` checks them against.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

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
