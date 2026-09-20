"""Integration tests for the browse skill against a live browser container.

Runs scripts/test_browse_sites.py on the remote host via SSH, since the
browser container is only reachable from there (Docker internal network).

Setup:
    1. Add to .env (see .env.example):
       BROWSER_HOST=your-server
    2. Run:
       uv run pytest -m integration tests/test_browse_integration.py -v
"""

import os
import subprocess
from pathlib import Path

import pytest

_ssh_host = os.environ.get("BROWSER_HOST", "")

_skip_reason = None
if not _ssh_host:
    _skip_reason = "BROWSER_HOST not set"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(_skip_reason is not None, reason=_skip_reason or ""),
]

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


def _run_remote_script(script_path, timeout=300, env=None):
    """Run a Python script on the remote host via SSH and return the result.

    `env` is passed as a `NAME=value` prefix on the remote command rather than
    through `subprocess`'s own `env`, which would set it here and not there.
    """
    assert script_path.exists(), f"Script not found: {script_path}"

    prefix = [f"{name}={value}" for name, value in sorted((env or {}).items())]
    result = subprocess.run(
        ["ssh", _ssh_host, *prefix, "python3", "-"],
        stdin=open(script_path),
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    # Print output for visibility in pytest -v
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)

    return result


class TestBrowseIntegration:
    def test_browse_sites(self):
        """Run the browse integration test suite on the remote host."""
        result = _run_remote_script(_SCRIPTS_DIR / "test_browse_sites.py")
        assert result.returncode == 0, (
            f"Browse integration tests failed:\n{result.stdout}\n{result.stderr}"
        )

    def test_visual_coordinates(self):
        """A point read off the delivered picture lands on the right cell.

        The positive proof the arithmetic cannot give. Every other test of
        visual mode drives a stubbed page object, so the conversion is pinned
        against a recorded frame and nothing in the default suite has ever
        clicked a page — which is exactly how the proof of concept found that
        `window.screenX` reports `0,0` under Xvfb with every unit test passing
        around the wrong constant.

        The remote script builds its own grid page and asserts on the cell's
        own click handler, so it cannot pass by hitting something else. It also
        drives a half-size picture, which is the leg that shows the envelope
        scaling doing real work at a screen size where the full-size conversion
        is the identity, and the scroll and full-page refusals.

        Run it at both screen sizes: the shipped 1440x900, and
        `SCREEN_WIDTH=1920 SCREEN_HEIGHT=1080` where the envelope scale is
        about 0.74 rather than about 0.99.
        """
        result = _run_remote_script(
            _SCRIPTS_DIR / "test_browse_sites.py",
            timeout=180,
            env={"BROWSE_TEST_ONLY": "visual"},
        )
        assert result.returncode == 0, (
            f"Visual coordinate test failed:\n{result.stdout}\n{result.stderr}"
        )

    def test_bot_detection(self):
        """Run bot detection checks on the remote host."""
        result = _run_remote_script(_SCRIPTS_DIR / "test_bot_detection.py", timeout=120)
        assert result.returncode == 0, (
            f"Bot detection tests failed:\n{result.stdout}\n{result.stderr}"
        )

    def test_nytimes(self):
        """Test NYTimes index + article navigation (DataDome-protected)."""
        result = _run_remote_script(_SCRIPTS_DIR / "test_nytimes.py", timeout=120)
        assert result.returncode == 0, (
            f"NYTimes test failed:\n{result.stdout}\n{result.stderr}"
        )
