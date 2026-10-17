"""The desktop tier: the Mac app (`desktop/`) built and driven on this Mac.

Run it with `scripts/test-desktop.sh`, which builds `Istota.app` with
`desktop/build-app.sh` and runs this directory with `-m desktop -n0`; its
`--control m9` builds the row M9 negative control and requires it to go red.
The app under test is `ISTOTA_DESKTOP_APP` (a path to `Istota.app`), else the
release bundle under `desktop/src-tauri/target`. Each run gives the app a
state directory of its own (`ISTOTA_DESKTOP_STATE_DIR`), so nothing here reads
or writes `~/Library/Application Support/Istota` or any Lima instance.

macOS on Apple silicon only; it skips elsewhere, and fails when the app has
not been built rather than skipping, so a run that tested nothing says so.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DEFAULT_APP = REPO / "desktop" / "src-tauri" / "target" / "release" / "bundle" / "macos" / "Istota.app"


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    if not any(item.get_closest_marker("desktop") for item in items):
        return
    workers = getattr(config.option, "numprocesses", None)
    if workers:
        raise pytest.UsageError(
            "the desktop tier must run with -n0: each test launches the one app bundle with a window"
        )


@pytest.fixture(scope="session")
def app_bundle() -> Path:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        pytest.skip("the desktop tier runs on an Apple silicon Mac")
    bundle = Path(os.environ.get("ISTOTA_DESKTOP_APP") or DEFAULT_APP)
    if not (bundle / "Contents" / "Info.plist").is_file():
        pytest.fail(f"no app at {bundle}; build it with desktop/build-app.sh or scripts/test-desktop.sh")
    return bundle


@pytest.fixture(scope="session")
def app_executable(app_bundle: Path) -> Path:
    macos = app_bundle / "Contents" / "MacOS"
    executables = [p for p in macos.iterdir() if p.is_file() and os.access(p, os.X_OK)]
    assert len(executables) == 1, executables
    return executables[0]
