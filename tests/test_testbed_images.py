"""A test run removes the images it built, and only those.

`docker` is a stub on PATH that records its argv, so this needs no daemon.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from testbed import images

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "scripts" / "test-image-ledger.sh"


@pytest.fixture
def docker_calls(tmp_path, monkeypatch):
    log = tmp_path / "docker-calls"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "docker"
    stub.write_text(
        f'#!/bin/bash\necho "$*" >> {log}\n'
        'case "$*" in *in-use*) echo "image is being used" >&2; exit 1 ;; esac\n'
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{Path('/usr/bin')}:{Path('/bin')}")
    monkeypatch.delenv(images.KEEP_VAR, raising=False)
    monkeypatch.delenv(images.LEDGER_VAR, raising=False)
    monkeypatch.setattr(images, "_built", set())
    return log


def _calls(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


class TestASession:
    def test_it_removes_what_it_recorded_once(self, docker_calls):
        images.record("istota-test/lean:abc")
        images.record("istota-test/lean:abc")

        assert images.remove_built() == (["istota-test/lean:abc"], [])
        assert _calls(docker_calls) == ["image rm istota-test/lean:abc"]

        assert images.remove_built() == ([], [])
        assert len(_calls(docker_calls)) == 1

    def test_the_trim_names_the_cache_and_no_builder(self, docker_calls, monkeypatch):
        monkeypatch.setenv("BUILDX_BUILDER", "from-the-shell")

        assert images.trim_build_cache() is None
        assert _calls(docker_calls) == [
            f"builder prune --all --force --filter until={images.BUILD_CACHE_MAX_AGE}",
        ]

    def test_a_failed_trim_says_why(self, tmp_path, monkeypatch):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        stub = bindir / "docker"
        stub.write_text('#!/bin/bash\necho "use another context" >&2\nexit 1\n')
        stub.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")

        assert images.trim_build_cache() == "use another context"

    def test_a_session_that_built_nothing_runs_no_docker(self, docker_calls):
        assert images.remove_built() == ([], [])
        assert _calls(docker_calls) == []

    def test_an_image_in_use_is_reported_and_the_rest_still_go(self, docker_calls):
        images.record("istota-test/in-use:a")
        images.record("istota-test/lean:b")

        assert images.remove_built() == (["istota-test/lean:b"], ["istota-test/in-use:a"])

    def test_keep_removes_nothing_and_says_what_stayed(self, docker_calls, monkeypatch):
        monkeypatch.setenv(images.KEEP_VAR, "1")
        images.record("istota-test/lean:abc")

        assert images.remove_built() == ([], ["istota-test/lean:abc"])
        assert _calls(docker_calls) == []

    def test_a_ledger_takes_the_tag_and_the_session_leaves_it(self, docker_calls, tmp_path, monkeypatch):
        ledger = tmp_path / "ledger"
        monkeypatch.setenv(images.LEDGER_VAR, str(ledger))
        images.record("istota-test/lean:abc")

        assert images.remove_built() == ([], [])
        assert _calls(docker_calls) == []
        assert ledger.read_text() == "istota-test/lean:abc\n"


def _driver(body: str, tmp_path: Path) -> subprocess.CompletedProcess:
    script = tmp_path / "driver.sh"
    script.write_text(
        f"#!/bin/bash\nset -euo pipefail\nsource {HELPER}\n"
        "trap remove_ledger_images EXIT\n" + body
    )
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=30)


class TestADriver:
    def test_it_removes_each_build_once_at_exit_including_a_sessions(self, docker_calls, tmp_path):
        result = _driver(
            "docker build -q -f a --build-arg BASE=x -t istota-test/a:control ctx >/dev/null\n"
            'tag="$(docker build -q -t istota-test/b:control ctx)"\n'
            'echo istota-test/session:1 >> "$ISTOTA_TEST_IMAGE_LEDGER"\n'
            "docker build -q -t istota-test/a:control ctx >/dev/null\n",
            tmp_path,
        )

        assert result.returncode == 0, result.stderr
        removed = [c for c in _calls(docker_calls) if c.startswith("image rm")]
        assert removed == [
            "image rm istota-test/a:control",
            "image rm istota-test/b:control",
            "image rm istota-test/session:1",
        ]
        assert _calls(docker_calls)[-1].startswith("builder prune")

    def test_a_driver_that_built_nothing_trims_nothing(self, docker_calls, tmp_path):
        result = _driver("true\n", tmp_path)

        assert result.returncode == 0, result.stderr
        assert _calls(docker_calls) == []

    def test_a_failing_driver_still_cleans_up_and_keeps_its_status(self, docker_calls, tmp_path):
        result = _driver("docker build -q -t istota-test/a:control ctx\nexit 7\n", tmp_path)

        assert result.returncode == 7
        assert "image rm istota-test/a:control" in _calls(docker_calls)

    def test_keep_leaves_them(self, docker_calls, tmp_path, monkeypatch):
        monkeypatch.setenv(images.KEEP_VAR, "1")
        result = _driver("docker build -q -t istota-test/a:control ctx\n", tmp_path)

        assert result.returncode == 0, result.stderr
        assert not [c for c in _calls(docker_calls) if c.startswith("image rm")]
        assert "istota-test/a:control" in result.stderr


class TestEveryBuilderIsCovered:
    """A build the cleanup does not know about is how the disk filled the last time."""

    def _python_builders(self) -> list[Path]:
        found = []
        for root in (REPO / "tests", REPO / "testbed"):
            for path in root.rglob("*.py"):
                # The vm tier's images are in its VM, and a default-suite test here
                # that builds does it in a disposable daemon of its own.
                if path.parts[-2] == "vm" or (path.parent == REPO / "tests" and path.name.startswith("test_")):
                    continue
                if re.search(r'"docker", "build"|\*lean_args, "build"|"--build"\)', path.read_text()):
                    found.append(path)
        return found

    def test_the_guard_finds_the_known_builders(self):
        names = {path.name for path in self._python_builders()}
        assert {"conftest.py", "stack.py", "test_web_setup_boot.py"} <= names

    def test_each_python_builder_records(self):
        for path in self._python_builders():
            assert "images.record(" in path.read_text(), path

    @pytest.mark.parametrize("script", ["test-image-negative-control.sh", "test-stack-negative-control.sh"])
    def test_each_driver_sources_the_ledger_and_clears_it(self, script):
        text = (REPO / "scripts" / script).read_text()
        assert "test-image-ledger.sh" in text
        assert re.search(r"trap '[^']*remove_ledger_images", text)

    def test_the_linux_tier_clears_its_ledger(self):
        text = (REPO / "scripts" / "test-linux.sh").read_text()
        assert "test-image-ledger.sh" in text
        assert "trap 'remove_ledger_images' EXIT" in text

    def test_both_halves_keep_the_same_cache(self):
        assert f"--filter until={images.BUILD_CACHE_MAX_AGE}" in HELPER.read_text()

    def test_the_helper_is_in_the_package(self):
        assert '"images.py"' in (REPO / "testbed" / "pyproject.toml").read_text()
