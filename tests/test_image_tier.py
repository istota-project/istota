"""The image tier's own wiring, checked from the side that needs no Docker.

`tests/image/` cannot run here — it builds and runs the shipped image, and the
whole point of the tier is that the ordinary suite does not pay for that. What
can be checked without a daemon is the wiring that decides *whether* the tier
runs and *how*, because every failure mode in that wiring is silent:

  * a marker that stops being deselected turns every ordinary `uv run pytest`
    into a Docker build;
  * a guard that fires on the default run breaks the suite for everyone;
  * a guard that stops firing lets N xdist workers race to build one tag, which
    surfaces as an unreproducible mess rather than as an error;
  * a `--platform` that silently means "native" makes the amd64 opt-in — the
    whole reason the flag exists — a no-op that reports success.

The same shape as `tests/test_linux_runner.py`, which guards the tier below it.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
IMAGE_TESTS = REPO_ROOT / "tests" / "image"

# Every module in the image tier, enumerated at import so the checks below
# cover files added after they were written. Guarded by
# `test_the_enumeration_below_found_the_tier`, because an empty list would make
# a parametrized check collapse into one green skip.
_IMAGE_TIER_MODULES = sorted(
    p.name for p in IMAGE_TESTS.glob("*.py") if p.name != "__init__.py"
)


def _ini() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["tool"]["pytest"][
        "ini_options"
    ]


def _addopts_marker_expression() -> str:
    match = re.search(r"-m '([^']+)'", _ini()["addopts"])
    assert match, f"could not find a -m expression in addopts: {_ini()['addopts']!r}"
    return match.group(1)


class TestTheMarkerIsRegisteredAndOffByDefault:
    def test_image_is_a_registered_marker(self):
        # An unregistered marker is a warning, not an error, so a typo would
        # deselect nothing and run the tier on every commit.
        assert any(m.startswith("image:") for m in _ini()["markers"])

    def test_addopts_deselects_image(self):
        # `uv run pytest` must not build a Docker image. This is the line that
        # keeps the default suite free.
        assert re.search(r"\bnot image\b", _addopts_marker_expression())

    def test_the_linux_driver_also_deselects_image(self):
        # The Linux tier runs the suite with its own `-m`, which replaces rather
        # than composes with addopts. A marker deselected in one and not the
        # other runs inside the runner and nowhere else.
        driver = (REPO_ROOT / "scripts" / "test-linux.sh").read_text()
        match = re.search(r"^default_markers='([^']+)'", driver, re.MULTILINE)

        assert match, "could not find default_markers= in scripts/test-linux.sh"
        assert re.search(r"\bnot image\b", match.group(1))


class TestTheGuardDoesNotFireOnTheDefaultRun:
    """The regression this guard nearly caused.

    `pytest_collection_modifyitems` is also where pytest's own `-m` deselection
    happens, so a conftest hook without `trylast` sees the *unfiltered* item
    list. The first draft did exactly that: `uv run pytest` exited 4 with a
    usage error about `-n0`, on a run that had already deselected every image
    test and was never going to build anything.
    """

    def test_an_ordinary_collection_over_the_image_dir_succeeds(self):
        result = _collect(["tests/image"])

        assert result.returncode in (0, 5), (
            "the default run tripped the image tier's xdist guard\n"
            f"{result.stdout}\n{result.stderr}"
        )
        assert "deselected" in result.stdout

    def test_the_collection_hook_rejects_the_collect_only_spelling(self):
        # `--collect-only -n 2` disables xdist and leaves `numprocesses` set, so
        # this is the one shape the collection hook can see. Kept because it
        # gives an error before anything is built — but it is NOT the real
        # scenario, which the next test covers.
        result = _collect(["-m", "image", "-n", "2"])

        assert result.returncode == 4, (
            "selecting the image tier under xdist did not fail the session\n"
            f"{result.stdout}\n{result.stderr}"
        )
        assert "-n0" in (result.stdout + result.stderr)

    def test_the_tier_collects_cleanly_with_n0(self):
        result = _collect(["-m", "image", "-n", "0"])

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "collected" in result.stdout

    def test_a_real_xdist_run_is_refused_before_anything_is_built(self):
        """The scenario the collection hook structurally cannot see.

        Under a real `-n 2` the controller never calls
        `pytest_collection_modifyitems` — it holds no items — and xdist clears
        `numprocesses` and `dist` in the workers so they do not re-fan-out. Every
        reading available to that hook therefore says "not parallel", and a
        measured `-m image -n 2` ran the entire tier ungated.

        So the binding check is `_require_no_xdist`, in the image fixtures,
        keyed on `config.workerinput`. This drives a real parallel session to
        prove it. It costs a fraction of a second because the refusal happens at
        fixture setup, before `require_docker()` and before any build — which
        also means this test needs no Docker daemon.
        """
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "-q",
                "--no-header",
                "-m",
                "image",
                "-n",
                "2",
                "tests/image/test_istota_image.py::TestGroupBTheRuntime",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
            env={**os.environ, "ISTOTA_IMAGE_TAG": "guard-test-should-never-be-pulled"},
        )
        output = result.stdout + result.stderr

        assert result.returncode != 0, f"a real xdist run was not refused\n{output}"
        assert "must run with -n0" in output, output
        assert "xdist worker" in output, output


def _collect(args: list[str]) -> subprocess.CompletedProcess:
    """A nested `--collect-only` pytest, from the repo root.

    A subprocess rather than `pytester`: the thing under test is this repo's own
    `addopts` and conftest, and `pytester` gives a synthetic project with
    neither.
    """
    return subprocess.run(
        # `-p no:cacheprovider`: the cacheprovider writes .pytest_cache/v/cache/
        # nodeids during collection, and three of these run concurrently with an
        # outer `-n auto` session writing the same file. Benign today (a
        # clobbered `--lf` set, not a failure), but shared mutable state across
        # processes is exactly what the order-independence rule in AGENTS.md
        # rules out.
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--collect-only",
            "-q",
            *args,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )


class TestCollectionNeverRequiresDocker:
    """A developer with no daemon running must still collect a clean session.

    So the skip has to happen at fixture *setup*. Import-time detection is the
    tempting version and it is wrong: it makes collection itself depend on
    Docker, which breaks the default suite on a machine that was never going to
    run this tier.
    """

    def test_the_conftest_imports_without_a_daemon(self):
        # Importing is the whole assertion: a module-level `docker info` would
        # raise or hang here.
        sys.path.insert(0, str(REPO_ROOT))
        try:
            import tests.image.conftest as image_conftest
        finally:
            sys.path.pop(0)

        assert callable(image_conftest.build_image)

    def test_the_enumeration_below_found_the_tier(self):
        """An empty parametrize list is a *skipped placeholder*, not a failure.

        The check below enumerates `tests/image/*.py` rather than listing them,
        because a hardcoded list silently stops covering the next file added —
        `test_upgrade.py` arrived after the original list was written and was
        not in it. But a glob that matches nothing (a moved directory, a renamed
        tier) turns the whole check into one green skip, which is the same
        silent non-coverage by another route. So the enumeration is asserted.
        """
        assert _IMAGE_TIER_MODULES, (
            f"no modules found under {IMAGE_TESTS}; the check below is "
            f"enumerating an empty directory and asserting nothing"
        )
        assert "conftest.py" in _IMAGE_TIER_MODULES

    @pytest.mark.parametrize("module", _IMAGE_TIER_MODULES)
    def test_no_module_level_subprocess_call(self, module):
        """Nothing in the image tier shells out at import time.

        Asserted on the AST rather than by grepping for the word "docker",
        which the first version did and which matched the docstring explaining
        why the rule exists. And asserted here rather than relying on the import
        test above: a module-level `docker info` would not *raise* on a machine
        that has Docker, so importing successfully proves nothing on the only
        machines that can run this tier.
        """
        tree = ast.parse((IMAGE_TESTS / module).read_text())

        offenders = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    name = ast.unparse(inner.func)
                    if "subprocess" in name or "run" == name:
                        offenders.append(f"line {inner.lineno}: {name}")

        assert not offenders, f"{module} shells out at import time: {offenders}"


class TestPlatformResolution:
    """`--platform amd64` must not silently mean "native".

    The amd64 run is the one that witnesses the architecture that actually
    ships. A flag that resolved to an empty string would build natively, tag the
    result as native, and report a pass — asserting nothing about amd64 while
    looking exactly like a successful amd64 run.
    """

    @pytest.mark.parametrize(
        "given,expected",
        [
            ("amd64", "linux/amd64"),
            ("linux/amd64", "linux/amd64"),
            ("arm64", "linux/arm64"),
            ("  amd64  ", "linux/amd64"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_a_bare_architecture_is_normalized(self, given, expected, monkeypatch):
        monkeypatch.delenv("ISTOTA_TEST_PLATFORM", raising=False)
        resolve = _image_conftest().resolve_platform

        assert resolve(_FakeConfig(given)) == expected

    def test_the_environment_variable_is_the_fallback(self, monkeypatch):
        monkeypatch.setenv("ISTOTA_TEST_PLATFORM", "amd64")
        resolve = _image_conftest().resolve_platform

        assert resolve(_FakeConfig(None)) == "linux/amd64"

    def test_the_flag_wins_over_the_environment_variable(self, monkeypatch):
        monkeypatch.setenv("ISTOTA_TEST_PLATFORM", "amd64")
        resolve = _image_conftest().resolve_platform

        assert resolve(_FakeConfig("arm64")) == "linux/arm64"

    def test_the_platform_is_in_the_tag(self):
        # A cached arm64 image and an amd64 one are different artifacts. Sharing
        # a tag means the second run tests the first run's image.
        tag_for = _image_conftest()._tag_for
        dockerfile = REPO_ROOT / "docker" / "istota" / "Dockerfile"

        native = tag_for(dockerfile, "", "istota")
        amd64 = tag_for(dockerfile, "linux/amd64", "istota")

        assert native != amd64
        assert "linux-amd64" in amd64

    def test_the_dockerfile_hash_is_in_the_tag(self, tmp_path):
        # Covers the uncommitted case: a dirty working tree at the same HEAD
        # would otherwise reuse an image built from the previous text.
        tag_for = _image_conftest()._tag_for
        one = tmp_path / "a"
        one.write_text("FROM scratch\n")
        two = tmp_path / "b"
        two.write_text("FROM scratch\nRUN true\n")

        assert tag_for(one, "", "x") != tag_for(two, "", "x")


class TestCredentialScrubbing:
    """A failing container assertion renders its output into the pytest report.

    Under a live run that output can carry a real token, and a pytest report is
    something people paste into chat.
    """

    def test_a_credential_shaped_variable_is_replaced_by_its_name(self):
        scrub = _image_conftest().scrub
        env = {"ISTOTA_DEVELOPER_GITLAB_TOKEN": "glpat-secret-value"}

        out = scrub("failed: glpat-secret-value rejected", env)

        assert "glpat-secret-value" not in out
        assert "<ISTOTA_DEVELOPER_GITLAB_TOKEN>" in out

    @pytest.mark.parametrize(
        "name",
        ["A_TOKEN", "MY_PASSWORD", "SESSION_SECRET", "SOME_KEY", "X_CREDENTIAL", "API_X"],
    )
    def test_every_credential_shaped_name_is_covered(self, name):
        scrub = _image_conftest().scrub

        assert "sensitive" not in scrub("sensitive", {name: "sensitive"})

    def test_an_ordinary_variable_is_left_alone(self):
        # Scrubbing everything would make a failure unreadable, which is its own
        # way of hiding the bug.
        scrub = _image_conftest().scrub

        assert scrub("user=testuser", {"USER_NAME": "testuser"}) == "user=testuser"

    def test_an_empty_value_does_not_blank_the_output(self):
        # `"".replace("", x)` inserts x between every character.
        scrub = _image_conftest().scrub

        assert scrub("hello", {"SOME_TOKEN": ""}) == "hello"

    def test_a_credential_is_never_placed_in_the_docker_argv(self):
        """The leak that scrubbing stdout alone does not close.

        `docker run -e NAME=value` puts the value in argv, where any other user
        on the host reads it out of `ps` — and pytest renders
        `CompletedProcess.args` into the assertion message, so it reaches the
        report too. Credential-shaped names go as a bare `-e NAME` with the
        value handed to docker through our own environment instead.
        """
        conftest = _image_conftest()
        image = conftest.BuiltImage(tag="x", dockerfile=REPO_ROOT, platform="")
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env") or {}
            return subprocess.CompletedProcess(cmd, 0, "", "")

        env = {"A_TOKEN": "secret-value", "USER_NAME": "alice"}
        original_run = conftest.subprocess.run
        original_docker = conftest.docker_available
        conftest.subprocess.run = fake_run
        conftest.docker_available = lambda: True
        try:
            result = conftest.run_in(image, ["-c", "true"], env=env)
        finally:
            conftest.subprocess.run = original_run
            conftest.docker_available = original_docker

        assert "secret-value" not in " ".join(captured["cmd"]), captured["cmd"]
        assert "-e" in captured["cmd"] and "A_TOKEN" in captured["cmd"]
        # It still has to reach the container, just by the other route.
        assert captured["env"]["A_TOKEN"] == "secret-value"
        # A non-credential keeps the inline form, which keeps failures readable.
        assert "USER_NAME=alice" in captured["cmd"]
        # And the returned args, which pytest renders, carry nothing either.
        assert "secret-value" not in " ".join(result.args)


class _FakeConfig:
    """Just enough of pytest's Config for `resolve_platform`."""

    def __init__(self, platform):
        self._platform = platform

    def getoption(self, name):
        assert name == "--platform"
        return self._platform


def _image_conftest():
    sys.path.insert(0, str(REPO_ROOT))
    try:
        import tests.image.conftest as mod
    finally:
        sys.path.pop(0)
    return mod
