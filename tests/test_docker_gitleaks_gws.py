"""#695 — the istota image shipped neither `gitleaks` nor `gws`.

The Ansible role installed both. The image did not, so on the containerized
shape the pre-commit hook refused every unattended commit and every
google_workspace call died at `execvp`. These pin the image's install: both
binaries are fetched at a pinned version, checksum-verified per architecture,
land on PATH, and gitleaks stays in step with the role's pin.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO / "docker" / "istota" / "Dockerfile"
ROLE_DEFAULTS = REPO / "deploy" / "ansible" / "defaults" / "main.yml"

# `gitleaks git` replaced `detect`/`protect` in 8.19; the hook calls the new one.
GIT_SUBCOMMAND_FLOOR = (8, 19, 0)

PINNED = (
    "GITLEAKS_VERSION",
    "GITLEAKS_SHA256_AMD64",
    "GITLEAKS_SHA256_ARM64",
    "GWS_VERSION",
    "GWS_SHA256_AMD64",
    "GWS_SHA256_ARM64",
)


@pytest.fixture(scope="module")
def body() -> str:
    return DOCKERFILE.read_text()


@pytest.fixture(scope="module")
def args(body) -> dict[str, str]:
    return dict(re.findall(r"^ARG\s+([A-Z0-9_]+)=(\S+)", body, re.M))


@pytest.fixture(scope="module")
def layer(body) -> str:
    """The RUN layer that installs both, comments stripped."""
    start = body.index("gitleaks_${GITLEAKS_VERSION}")
    start = body.rindex("RUN ", 0, start)
    end = body.index("\n\n", start)
    return "\n".join(
        line for line in body[start:end].splitlines() if not line.lstrip().startswith("#")
    )


def test_every_pin_is_declared(args):
    for name in PINNED:
        assert name in args, f"Dockerfile declares no ARG {name}"


@pytest.mark.parametrize("name", [n for n in PINNED if "SHA256" in n])
def test_checksums_are_literal_sha256(args, name):
    assert re.fullmatch(r"[0-9a-f]{64}", args[name]), name


def test_both_downloads_are_verified(layer):
    assert layer.count("sha256sum -c -") == 2


def test_an_unpinned_architecture_fails_the_build(layer):
    assert "exit 1" in layer


def test_gws_is_not_fetched_from_latest(layer):
    assert "releases/latest" not in layer
    assert "${GWS_VERSION}" in layer


def test_both_land_on_path(layer):
    assert "/usr/local/bin/gitleaks" in layer
    assert "/usr/local/bin/gws" in layer


def test_the_build_runs_the_hooks_probe(layer):
    """A truncated or wrong-arch binary fails here rather than at first commit."""
    assert "gitleaks git --help" in layer
    assert "gws --version" in layer


def test_gitleaks_clears_the_git_subcommand_floor(args):
    version = tuple(int(p) for p in args["GITLEAKS_VERSION"].split("."))
    assert version >= GIT_SUBCOMMAND_FLOOR


def test_gitleaks_matches_the_role(args):
    """Same binary on both shapes until the role is deleted (Stage 9)."""
    defaults = ROLE_DEFAULTS.read_text()
    version = re.search(r'^istota_developer_gitleaks_version:\s*"([^"]+)"', defaults, re.M)
    checksums = defaults[defaults.index("istota_developer_gitleaks_checksums:"):]
    amd64 = re.search(r'^\s+amd64:\s*"([0-9a-f]{64})"', checksums, re.M)
    arm64 = re.search(r'^\s+arm64:\s*"([0-9a-f]{64})"', checksums, re.M)
    assert version and amd64 and arm64
    assert args["GITLEAKS_VERSION"] == version.group(1)
    assert args["GITLEAKS_SHA256_AMD64"] == amd64.group(1)
    assert args["GITLEAKS_SHA256_ARM64"] == arm64.group(1)
