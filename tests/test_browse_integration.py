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


def _run_remote_script(script_path, timeout=300):
    """Run a Python script on the remote host via SSH and return the result."""
    assert script_path.exists(), f"Script not found: {script_path}"

    result = subprocess.run(
        ["ssh", _ssh_host, "python3", "-"],
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
