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
    @pytest.mark.parametrize("array", ["gitdir_bind", "progress_args"])
    def test_an_empty_optional_array_survives_set_u(self, array):
        """The exact expansions the driver uses, against the shell that breaks.

        `gitdir_bind` is empty in an ordinary clone and non-empty in a linked
        worktree, so the plain `"${a[@]}"` form worked in the checkout this was
        developed in and would have died in everyone else's. `progress_args`
        has the same shape and the same trap: it is empty on exactly the
        legacy-builder host the conditional flag exists to support, so the
        naive form would break the case it was written to fix.
        """
        form = re.search(rf'(\$\{{{array}\[@\][^\n]*?)\s*\\\n', DRIVER.read_text())
        assert form, f"could not find the {array} expansion in the driver"

        script = f'set -euo pipefail; a=(); printf "[%s]" {form.group(1).replace(array, "a")}; echo ok'
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


class TestTheBuildFlagsMatchTheBuilder:
    """`--progress` is a BuildKit flag, and the driver must not assume BuildKit.

    The legacy builder rejects it outright — `unknown flag: --progress`, before
    the Dockerfile is read. That matters because `DOCKER_BUILDKIT=0` is the
    escape from a `docker-container` default buildx builder that cannot reach
    the daemon, so the one host that needs the legacy path was the one the
    driver refused to build on (ISSUE-293).
    """

    def _run_selector(self, stub_help: str, env: dict[str, str] | None = None) -> list[str]:
        """The driver's own flag selector, against a stubbed `docker`.

        Returns the arguments it selected. `/bin/bash` rather than whatever
        `bash` resolves to, for the same reason as the rest of this class:
        3.2 is the shell this driver has to work on, and Homebrew's 5.x is
        what a developer's PATH would supply instead.

        The environment is built explicitly rather than inherited.
        `ISTOTA_TEST_BUILD_PROGRESS` is the value under test and the driver's
        own header advertises exporting it, so passing `os.environ` through
        would let a developer's debugging setting decide the assertion.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "docker"
            stub.write_text("#!/bin/sh\ncat <<'STUB_EOF'\n" + stub_help + "\nSTUB_EOF\n")
            stub.chmod(0o755)

            script = (
                'set -euo pipefail; '
                f'eval "$(sed -n \'/^build_progress_args()/,/^}}/p\' {DRIVER})"; '
                'build_progress_args'
            )
            result = subprocess.run(
                ["/bin/bash", "-c", script],
                capture_output=True,
                text=True,
                env={"PATH": f"{tmp}:/usr/bin:/bin", **(env or {})},
            )
            assert result.returncode == 0, result.stderr
            # NUL-delimited, so a value containing a space or a newline stays
            # one argument — which is the point of the delimiter.
            return [arg for arg in result.stdout.split("\0") if arg]

    def test_the_flag_is_dropped_when_the_builder_does_not_take_it(self):
        selected = self._run_selector("Usage: docker build [OPTIONS] PATH\n  -t, --tag list")
        assert selected == [], (
            "the legacy builder refuses --progress; the driver must not pass it"
        )

    def test_the_flag_is_passed_when_the_builder_takes_it(self):
        selected = self._run_selector("Usage: docker buildx build\n      --progress string")
        assert selected == ["--progress", "quiet"]

    def test_the_progress_override_still_reaches_a_buildkit_build(self):
        selected = self._run_selector(
            "      --progress string", env={"ISTOTA_TEST_BUILD_PROGRESS": "plain"},
        )
        assert selected == ["--progress", "plain"]

    def test_a_progress_value_containing_a_newline_stays_one_argument(self):
        """Otherwise it arrives as a second build context.

        `docker build --progress pl ain -f … "$REPO_ROOT"` names two contexts
        and fails on something unrelated to what was set.
        """
        selected = self._run_selector(
            "      --progress string",
            env={"ISTOTA_TEST_BUILD_PROGRESS": "pl\nain"},
        )
        assert selected == ["--progress", "pl\nain"]

    def test_the_driver_does_not_pass_progress_unconditionally(self):
        """The shape of the bug: a literal `--progress` on the build line."""
        body = DRIVER.read_text()
        build_line = re.search(r"^docker build .*$", body, re.MULTILINE)
        assert build_line, "could not find the docker build invocation"
        assert "--progress" not in build_line.group(0), (
            f"--progress is hardcoded on the build line: {build_line.group(0)!r}"
        )
