"""APT_MIRROR and the apt build cache.

deb.debian.org's CDN stalls some connections (measured 2026-10-13: the same
edge address served one fetch at 5 MB/s and another at 21 KB/s), and a first
build runs hundreds of fetches through it. `APT_MIRROR` in host.env names one
Debian mirror for both halves: provision.sh points the host's apt at it, and
istota-stack passes it as a build arg to every image that runs apt. The cache
mounts keep downloaded packages between builds, so an update whose apt layer
changed fetches only what changed.

The istota-stack half (what `compose` exports) is in test_istota_stack.py, and
the devbox build's args in test_devbox_compose_file.py.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
PROVISION = REPO / "host" / "provision.sh"
STACK = REPO / "host" / "istota-stack"
COMPOSE = REPO / "docker" / "docker-compose.yml"

APT_DOCKERFILES = {
    "istota": REPO / "docker" / "istota" / "Dockerfile",
    "devbox": REPO / "docker" / "devbox" / "Dockerfile",
    "browser": REPO / "docker" / "browser" / "Dockerfile",
}

TRIXIE_SOURCES = """\
Types: deb
URIs: http://deb.debian.org/debian
Suites: trixie trixie-updates
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: http://deb.debian.org/debian-security
Suites: trixie-security
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
"""


def _instructions(dockerfile: Path) -> list[str]:
    """Each instruction as one line: continuations joined, the comment lines
    Docker drops inside a continuation dropped too."""
    found: list[str] = []
    current = ""
    for line in dockerfile.read_text().splitlines():
        if line.lstrip().startswith("#") or (not current and not line.strip()):
            continue
        current += line.rstrip("\\").strip() + " "
        if not line.rstrip().endswith("\\"):
            found.append(current.strip())
            current = ""
    return found


def _apt_runs(dockerfile: Path) -> list[str]:
    """The steps that fetch from Debian. The browser's chrome stage runs only
    `dpkg -i` and `apt-get check`, which read dpkg's status and fetch nothing."""
    return [i for i in _instructions(dockerfile)
            if i.startswith("RUN ") and re.search(r"apt-get (update|install)", i)]


def _mirror_line(dockerfile: Path) -> str:
    lines = [line.strip() for line in dockerfile.read_text().splitlines() if '"$APT_MIRROR"' in line]
    assert len(lines) == 1, f"{dockerfile} rewrites its sources in one line"
    return lines[0]


class TestTheDockerfiles:
    @pytest.mark.parametrize("name", sorted(APT_DOCKERFILES))
    def test_it_takes_the_mirror_as_a_build_arg(self, name):
        instructions = _instructions(APT_DOCKERFILES[name])
        assert "ARG APT_MIRROR=" in instructions
        arg = instructions.index("ARG APT_MIRROR=")
        first_apt = instructions.index(_apt_runs(APT_DOCKERFILES[name])[0])
        assert arg < first_apt
        assert '"$APT_MIRROR"' in instructions[first_apt], "the first apt step rewrites the sources"

    @pytest.mark.parametrize("name", sorted(APT_DOCKERFILES))
    def test_every_apt_step_keeps_its_downloads(self, name):
        runs = _apt_runs(APT_DOCKERFILES[name])
        assert runs
        for run in runs:
            assert f"--mount=type=cache,id={name}-apt-archives,target=/var/cache/apt-build,sharing=locked" in run, run
            assert f"--mount=type=cache,id={name}-apt-lists,target=/var/lib/apt/lists,sharing=locked" in run, run
            # The base image's docker-clean deletes /var/cache/apt/archives after
            # every install; the build's own archive directory is out of its way.
            assert """echo 'Dir::Cache::Archives "/var/cache/apt-build";' > /etc/apt/apt.conf.d/zz-build-cache""" in run
            assert run.rstrip().endswith("rm -f /etc/apt/apt.conf.d/zz-build-cache"), run
            # The lists are the cache mount; emptying them empties the cache.
            assert "rm -rf /var/lib/apt/lists" not in run, run

    def test_the_three_rewrite_the_sources_alike(self):
        lines = {name: _mirror_line(path) for name, path in APT_DOCKERFILES.items()}
        assert len(set(lines.values())) == 1, lines

    @pytest.mark.parametrize("mirror,expected", [
        ("", TRIXIE_SOURCES),
        ("http://mirror.example.net/debian",
         TRIXIE_SOURCES.replace("URIs: http://deb.debian.org/debian\n", "URIs: http://mirror.example.net/debian\n")),
    ])
    def test_only_the_main_archive_moves(self, tmp_path, mirror, expected):
        sources = tmp_path / "debian.sources"
        sources.write_text(TRIXIE_SOURCES)
        line = _mirror_line(APT_DOCKERFILES["istota"]).rstrip("\\").strip()
        line = line.replace("/etc/apt/sources.list.d/debian.sources", str(sources))
        result = subprocess.run(["sh", "-c", f"set -eu; {line}"], capture_output=True, text=True,
                                env={**os.environ, "APT_MIRROR": mirror})
        assert result.returncode == 0, result.stderr
        assert sources.read_text() == expected


class TestTheComposeFiles:
    def test_the_istota_and_browser_builds_pass_it(self):
        services = yaml.safe_load(COMPOSE.read_text())["services"]
        assert "APT_MIRROR" in services["daemon"]["build"]["args"]
        assert "APT_MIRROR" in services["browser"]["build"]["args"]


def _function(text: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{.*?^\}}", text, re.M | re.S)
    assert match, f"{name} is defined"
    return match.group(0)


class TestTheHost:
    TEXT = PROVISION.read_text()

    def test_both_scripts_check_a_mirror_alike(self):
        assert _function(self.TEXT, "valid_apt_mirror") == _function(STACK.read_text(), "valid_apt_mirror")

    @pytest.mark.parametrize("value,ok", [
        ("http://mirror.example.net/debian", True),
        ("https://mirror.example.net:8443/pub/linux/debian", True),
        ("http://mirror.example.net/debian/", False),
        ("ftp://mirror.example.net/debian", False),
        ("http://mirror.example.net/debian trixie", False),
        ("http://mirror.example.net/de#bian", False),
        ("http://mirror.example.net/debian|x", False),
        ("", False),
    ])
    def test_what_counts_as_a_mirror(self, value, ok):
        script = f"{_function(self.TEXT, 'valid_apt_mirror')}\nvalid_apt_mirror \"$1\""
        result = subprocess.run(["bash", "-c", script, "_", value], capture_output=True, text=True)
        assert (result.returncode == 0) is ok, value

    def _run(self, tmp_path: Path, mirror: str) -> subprocess.CompletedProcess:
        script = "\n".join([
            "set -euo pipefail",
            "log() { echo \"[provision] $*\"; }",
            _function(self.TEXT, "valid_apt_mirror"),
            _function(self.TEXT, "use_apt_mirror"),
            'use_apt_mirror "$1" "$2" "$3"',
        ])
        return subprocess.run(["bash", "-c", script, "_", mirror, str(tmp_path / "state"), str(tmp_path / "root")],
                              capture_output=True, text=True)

    @pytest.fixture
    def root(self, tmp_path) -> Path:
        apt = tmp_path / "root" / "etc" / "apt"
        (apt / "mirrors").mkdir(parents=True)
        (apt / "sources.list.d").mkdir()
        (apt / "mirrors" / "debian.list").write_text("https://deb.debian.org/debian\n")
        (apt / "mirrors" / "debian-security.list").write_text("https://deb.debian.org/debian-security\n")
        (apt / "sources.list.d" / "debian.sources").write_text(
            TRIXIE_SOURCES.replace("http://deb.debian.org/debian\n", "mirror+file:///etc/apt/mirrors/debian.list\n"))
        (apt / "sources.list").write_text(
            "deb http://deb.debian.org/debian trixie main\n"
            "deb http://repo.example.com/debian stable main\n")
        return apt

    def test_a_mirror_replaces_debians_and_nothing_else(self, tmp_path, root):
        before_sources = (root / "sources.list.d" / "debian.sources").read_text()
        result = self._run(tmp_path, "http://mirror.example.net/debian")
        assert result.returncode == 0, result.stderr
        assert (root / "mirrors" / "debian.list").read_text() == "http://mirror.example.net/debian\n"
        assert (root / "mirrors" / "debian-security.list").read_text() == "https://deb.debian.org/debian-security\n"
        assert (root / "sources.list.d" / "debian.sources").read_text() == before_sources
        assert (root / "sources.list").read_text() == (
            "deb http://mirror.example.net/debian trixie main\n"
            "deb http://repo.example.com/debian stable main\n")
        assert (tmp_path / "state").read_text() == "http://mirror.example.net/debian\n"

    def test_a_second_mirror_replaces_the_first(self, tmp_path, root):
        assert self._run(tmp_path, "http://mirror.example.net/debian").returncode == 0
        assert self._run(tmp_path, "https://other.example.org/debian").returncode == 0
        assert (root / "mirrors" / "debian.list").read_text() == "https://other.example.org/debian\n"
        assert "deb https://other.example.org/debian trixie main\n" in (root / "sources.list").read_text()

    def test_removing_the_key_goes_back_to_debians(self, tmp_path, root):
        assert self._run(tmp_path, "http://mirror.example.net/debian").returncode == 0
        result = self._run(tmp_path, "")
        assert result.returncode == 0, result.stderr
        assert (root / "mirrors" / "debian.list").read_text() == "https://deb.debian.org/debian\n"
        assert (root / "sources.list").read_text() == (
            "deb https://deb.debian.org/debian trixie main\n"
            "deb http://repo.example.com/debian stable main\n")
        assert not (tmp_path / "state").exists()

    def test_no_key_and_no_record_touches_nothing(self, tmp_path, root):
        before = {p: p.read_text() for p in root.rglob("*") if p.is_file()}
        assert self._run(tmp_path, "").returncode == 0
        assert {p: p.read_text() for p in root.rglob("*") if p.is_file()} == before

    def test_an_invalid_mirror_is_a_warning_and_leaves_apt_alone(self, tmp_path, root):
        before = {p: p.read_text() for p in root.rglob("*") if p.is_file()}
        result = self._run(tmp_path, "http://mirror.example.net/debian|x")
        assert result.returncode == 0, result.stderr
        assert "WARNING" in result.stdout
        assert {p: p.read_text() for p in root.rglob("*") if p.is_file()} == before

    def test_provision_sets_the_mirror_before_its_first_apt_call(self):
        call = self.TEXT.index('use_apt_mirror "$(host_env APT_MIRROR)" "${STACK}/.apt-mirror" ""')
        assert call < self.TEXT.index("apt-get update -qq")
