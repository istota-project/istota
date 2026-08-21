"""The Linux tier's driver, checked from the side that does not need Docker.

`scripts/test-linux.sh` cannot be exercised here — it needs a Docker daemon,
and the whole point of the tier is that the ordinary suite does not. What can
be checked without one is the two places the driver duplicates something that
lives elsewhere in the repo, because a copy that drifts is how a runner starts
quietly running the wrong set of tests.
"""

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DRIVER = REPO_ROOT / "scripts" / "test-linux.sh"
DOCKERFILE = REPO_ROOT / "docker" / "test" / "Dockerfile"


def _addopts_marker_expression() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    addopts = data["tool"]["pytest"]["ini_options"]["addopts"]
    match = re.search(r"-m '([^']+)'", addopts)
    assert match, f"could not find a -m expression in addopts: {addopts!r}"
    return match.group(1)


def _driver_marker_expression() -> str:
    body = DRIVER.read_text()
    match = re.search(r"^default_markers='([^']+)'", body, re.MULTILINE)
    assert match, "could not find default_markers= in scripts/test-linux.sh"
    return match.group(1)


class TestDriverIsExecutableBash:
    def test_it_is_executable(self):
        import os

        assert DRIVER.exists()
        assert os.access(DRIVER, os.X_OK), "the driver is invoked as ./scripts/test-linux.sh"

    def test_it_parses_as_bash(self):
        result = subprocess.run(
            ["bash", "-n", str(DRIVER)], capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr

    @pytest.mark.skipif(
        not Path("/bin/bash").exists(), reason="no /bin/bash to check the old shell against",
    )
    def test_it_parses_under_the_system_bash(self):
        """macOS ships bash 3.2, and the driver's whole audience is on macOS.

        Not a formality: `"${empty_array[@]}"` under `set -u` is fatal there
        and fine on bash 5, and the driver has such an array. `-n` catches
        syntax, not that, so the real guard is the expansion form used at the
        call site — but a construct bash 3.2 cannot even parse would be worse.
        """
        result = subprocess.run(
            ["/bin/bash", "-n", str(DRIVER)], capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr

    @pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")
    def test_an_empty_optional_bind_survives_set_u(self):
        """The exact expansion the driver uses, against the shell that breaks.

        `gitdir_bind` is empty in an ordinary clone and non-empty in a linked
        worktree, so the plain `"${a[@]}"` form worked in the checkout this was
        developed in and would have died in everyone else's.
        """
        form = re.search(r'(\$\{gitdir_bind\[@\][^\n]*?)\s*\\\n', DRIVER.read_text())
        assert form, "could not find the gitdir_bind expansion in the driver"

        script = f'set -euo pipefail; a=(); printf "[%s]" {form.group(1).replace("gitdir_bind", "a")}; echo ok'
        result = subprocess.run(
            ["/bin/bash", "-c", script], capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"the driver's array expansion is fatal under set -u on "
            f"{subprocess.run(['/bin/bash', '--version'], capture_output=True, text=True).stdout.splitlines()[0]}: "
            f"{result.stderr}"
        )
        assert "ok" in result.stdout


class TestMarkerExpressionStaysInStepWithAddopts:
    """The driver restates pyproject's deselection set, and must not fall behind.

    There is no pytest syntax for "the default expression, plus linux" — `-m`
    replaces rather than composes — so the set is written out twice. The
    dangerous direction is a *new* default-deselected marker: addopts turns it
    off for everyone, the driver's stale copy does not mention it, and the
    Linux tier starts running tests nothing else runs.
    """

    def test_every_marker_addopts_deselects_is_deselected_by_the_driver(self):
        deselected = set(re.findall(r"not (\w+)", _addopts_marker_expression()))
        driver = _driver_marker_expression()

        missing = sorted(
            m for m in deselected
            if m != "linux" and not re.search(rf"\bnot {m}\b", driver)
        )
        assert not missing, (
            f"scripts/test-linux.sh does not deselect {missing}, which "
            f"pyproject's addopts does — the Linux tier would run them"
        )

    def test_the_driver_selects_linux(self):
        assert "linux" in _driver_marker_expression()

    def test_addopts_deselects_linux(self):
        """The user-facing half of the contract: Docker is never required.

        `uv run pytest` must not try to build a namespace, so the marker has to
        be off by default. This is the assertion that keeps the tier
        discretionary.
        """
        assert re.search(r"\bnot linux\b", _addopts_marker_expression())

    def test_linux_is_a_registered_marker(self):
        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        markers = data["tool"]["pytest"]["ini_options"]["markers"]
        assert any(m.startswith("linux:") for m in markers)


class TestTheLinuxTierCannotSilentlySkip:
    """The tier exists to end silent non-execution, so it must not skip itself.

    The driver checks bwrap with its own probes (`--unshare-user`,
    `--unshare-net`) and the tests guard on `_bwrap_available()`, which probes
    neither — two predicates that can disagree. If they did, every linux test
    would skip, pytest would exit 0, and the driver would report a clean run
    having executed none of them. `ISTOTA_LINUX_TIER=1` collapses that: inside
    the driver, the skip path is a failure.
    """

    def test_the_driver_sets_the_sentinel(self):
        assert "-e ISTOTA_LINUX_TIER=1" in DRIVER.read_text(), (
            "the sentinel must reach the container, or the guard below never fires"
        )

    def test_unavailable_skips_outside_the_runner(self, monkeypatch):
        from tests.linux.test_sandbox_real import _unavailable

        monkeypatch.delenv("ISTOTA_LINUX_TIER", raising=False)

        # BaseException, not Exception: pytest's Skipped and Failed both
        # derive from OutcomeException, which derives from BaseException so
        # a bare `except Exception` in test code cannot swallow an outcome.
        with pytest.raises(BaseException) as excinfo:
            _unavailable("no bwrap here")
        assert excinfo.typename == "Skipped"

    def test_unavailable_fails_inside_the_runner(self, monkeypatch):
        from tests.linux.test_sandbox_real import _unavailable

        monkeypatch.setenv("ISTOTA_LINUX_TIER", "1")

        with pytest.raises(BaseException) as excinfo:
            _unavailable("no bwrap here")
        assert excinfo.typename == "Failed"
        assert "no bwrap here" in str(excinfo.value)


class TestRunnerImagePinsItsToolchain:
    """An unpinned tool in the image decides, on its own schedule, that the
    tier is broken. Both of these gate the run: `uv` resolves the dependencies
    and `ruff check` runs before pytest is reached, so a new default rule in a
    ruff release fails a build that changed no source."""

    def test_uv_is_pinned(self):
        body = DOCKERFILE.read_text()
        assert "astral-sh/uv:latest" not in body, "pin uv, or a rebuild is not reproducible"
        assert re.search(r"astral-sh/uv:\d+\.\d+\.\d+", body)

    def test_ruff_is_pinned(self):
        body = DOCKERFILE.read_text()
        assert re.search(r"uv tool install ruff==\d+\.\d+\.\d+", body), (
            "pin ruff: it gates the whole run and is not a project dependency, "
            "so nothing else constrains its version"
        )
