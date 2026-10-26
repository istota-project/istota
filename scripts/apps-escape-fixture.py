"""Build an app fixture folder into the document the escape tier frames.

The escape tier (``web/tests-apps-escape/``) must frame exactly what an install
would serve, so this runs the real ``apps.manifest.parse_manifest``,
``apps.document.build_document`` and ``apps.kit.load_kit`` over a fixture
folder and writes, under ``--out``:

* ``frame.html``, the built document;
* ``csp.txt``, the Content-Security-Policy the frame route would send for it.

The folder name must equal the manifest's ``id``, as for a real draft. The kit
must be built first (``npm --prefix web run build:apps``); a missing kit is
exit 2 with the plain error, a fixture that does not build is exit 1 with the
builder's errors.

    uv run python scripts/apps-escape-fixture.py --app tests/fixtures/apps/escape-contract --out <dir>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from istota.apps.document import MANIFEST as MANIFEST_FILE
from istota.apps.document import DocumentError, build_document
from istota.apps.kit import KitMissing, load_kit
from istota.apps.manifest import ManifestError, parse_manifest
from istota.config import AppsConfig

# Any name works: it only feeds the files:read deny list, which a fixture does not use.
BOT_DIR = "istota"


def read_folder(folder: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise SystemExit(f"{path}: a fixture may not hold links")
        if path.is_file():
            files[path.relative_to(folder).as_posix()] = path.read_bytes()
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--app", required=True, type=Path, help="the fixture app folder")
    parser.add_argument("--out", required=True, type=Path, help="where to write frame.html and csp.txt")
    args = parser.parse_args(argv)

    folder = args.app.resolve()
    files = read_folder(folder)
    if MANIFEST_FILE not in files:
        print(f"{folder}: no {MANIFEST_FILE}", file=sys.stderr)
        return 1
    try:
        manifest = parse_manifest(files[MANIFEST_FILE].decode("utf-8"), folder_name=folder.name, bot_dir=BOT_DIR)
        kit = load_kit()
        built = build_document(files, manifest, kit, max_bytes=AppsConfig().bundle_max_bytes)
    except KitMissing as exc:
        print(f"{exc} ({exc.reason}: {exc.path}); run `npm --prefix web run build:apps`", file=sys.stderr)
        return 2
    except (ManifestError, DocumentError) as exc:
        for error in exc.errors:
            print(error, file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "frame.html").write_bytes(built.html)
    (args.out / "csp.txt").write_text(built.csp + "\n", encoding="utf-8")
    for warning in built.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(f"{manifest.id}: {len(built.html)} bytes, {len(built.script_hashes)} scripts -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
