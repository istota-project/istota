"""The env-var deselection must stay in step with the one `addopts` performs.

There are now two ways to say "not the discretionary tiers": the `-m` expression
in `addopts`, used by every ordinary run, and `ISTOTA_DESELECT_TIERS`, used by
the incremental (testmon) runs that cannot pass `-m` at all. They are written
out twice because pytest offers no way to compose a marker expression, the same
reason `scripts/test-linux.sh` restates the set a third time.

The dangerous direction is a marker deselected by `addopts` and missing from
`DISCRETIONARY_MARKERS`: an incremental run would then collect a tier meant to
be off by default and start building Docker images on a developer's laptop.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from .conftest import DISCRETIONARY_MARKERS

REPO_ROOT = Path(__file__).resolve().parent.parent


def _addopts_marker_expression() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    addopts = data["tool"]["pytest"]["ini_options"]["addopts"]
    match = re.search(r"-m '([^']+)'", addopts)
    assert match, f"could not find a -m expression in addopts: {addopts!r}"
    return match.group(1)


class TestTheTwoDeselectionsAgree:
    def test_every_marker_addopts_deselects_is_in_the_tuple(self):
        deselected = set(re.findall(r"not (\w+)", _addopts_marker_expression()))
        missing = sorted(deselected - set(DISCRETIONARY_MARKERS))
        assert not missing, (
            f"addopts deselects {missing}, which ISTOTA_DESELECT_TIERS does not "
            f"— an incremental run would collect them"
        )

    def test_the_tuple_claims_nothing_addopts_does_not(self):
        """The other direction is merely wrong, but it is still wrong."""
        deselected = set(re.findall(r"not (\w+)", _addopts_marker_expression()))
        extra = sorted(set(DISCRETIONARY_MARKERS) - deselected)
        assert not extra, f"{extra} is deselected by the env var but not by addopts"

    @pytest.mark.parametrize("marker", DISCRETIONARY_MARKERS)
    def test_each_one_is_a_registered_marker(self, marker):
        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        markers = data["tool"]["pytest"]["ini_options"]["markers"]
        assert any(m.startswith(f"{marker}:") for m in markers), marker


class TestTheEnvVarActuallyDeselects:
    """A subprocess, because the thing under test is this repo's own conftest."""

    def _collect(self, env_value: str | None) -> str:
        import os

        env = dict(os.environ)
        env.pop("ISTOTA_DESELECT_TIERS", None)
        if env_value is not None:
            env["ISTOTA_DESELECT_TIERS"] = env_value
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q",
             "-o", "addopts=", "-p", "no:randomly", "tests/image/"],
            cwd=REPO_ROOT, capture_output=True, text=True, env=env,
        )
        return result.stdout + result.stderr

    def test_the_image_tier_is_collected_without_it(self):
        """The control: without the variable those tests are collectable."""
        out = self._collect(None)
        assert "tests collected" in out or "test collected" in out, out[-2000:]

    def test_the_image_tier_is_deselected_with_it(self):
        out = self._collect("1")
        assert "deselected" in out, out[-2000:]
        assert re.search(r"\b0/\d+ tests collected|no tests ran|deselected", out), out[-2000:]

    def test_an_unset_value_does_not_deselect(self):
        """`ISTOTA_DESELECT_TIERS=0` left in a shell must not silently disarm."""
        out = self._collect("0")
        assert "tests collected" in out or "test collected" in out, out[-2000:]
