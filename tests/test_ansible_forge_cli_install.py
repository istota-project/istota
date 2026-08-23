"""The host and the two images have to ship the *same* `gh` and `glab`.

The role took both from the Debian archive, which on trixie means gh 2.46 and
glab 1.53, while `docker/istota/Dockerfile` and `docker/devbox/Dockerfile` pin
2.98.0 and 1.114.0 — so a bare-metal deployment ran ~50 gh releases behind the
container it is meant to be interchangeable with.

Two properties, and the second is the one a version bump breaks quietly:

  * the binaries come from the vendors' own releases, pinned and
    sha256-verified, rather than from apt; and
  * the pinned version and digests are the *same* ones both Dockerfiles pin.

The second is why this file reads all three sources rather than only the role.
Nothing can share a literal across an Ansible role and two Docker builds, so a
bump that moves the Dockerfiles and forgets the role puts the drift straight
back — and the symptom is a wrong binary on one deployment shape only.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TASKS_FILE = REPO_ROOT / "deploy" / "ansible" / "tasks" / "main.yml"
DEFAULTS = REPO_ROOT / "deploy" / "ansible" / "defaults" / "main.yml"
DOCKERFILE = REPO_ROOT / "docker" / "istota" / "Dockerfile"
DEVBOX_DOCKERFILE = REPO_ROOT / "docker" / "devbox" / "Dockerfile"

BANNER = "# ============================================================"

# Where the role now puts them. Ahead of /usr/bin on the default PATH, so this
# binary wins over an apt-installed one an older run of this same role left
# behind — the reason the section does not have to remove the package.
INSTALL_DIR = "/usr/local/bin"


@pytest.fixture(scope="module")
def tasks() -> str:
    return TASKS_FILE.read_text()


@pytest.fixture(scope="module")
def defaults() -> str:
    return DEFAULTS.read_text()


@pytest.fixture(scope="module")
def block(tasks: str) -> str:
    return _forge_section(tasks)


@pytest.fixture(scope="module")
def yaml_only(block: str) -> str:
    """The section with its comment lines dropped.

    Every `not in` assertion below is about what the section *does*, so it has
    to be scoped to the YAML. Run over the prose too they read the section's
    own explanation of why it avoids a thing — and this role writes comments
    that name what they are warning against, so the sentence "`dpkg-deb -x`
    rather than `dpkg -i`" fails a test whose message says the install uses
    `dpkg -i`. `tests/test_docker_forge_clis._forge_run_block` learned the
    same lesson on the same two binaries.
    """
    return "\n".join(
        line for line in block.split("\n") if not line.lstrip().startswith("#")
    )


def _forge_section(tasks: str) -> str:
    """The tasks between the forge-CLI section banner and the next one.

    Same shape as `test_ansible_gitleaks_install._gitleaks_block`, and for the
    same reason: the role writes a banner line above *and* below each section
    title, so a naive split returns the title line alone and every
    `assert ... in block` fails while every `assert ... not in block` passes
    for the wrong reason.
    """
    sections = tasks.split(BANNER)
    for title, body in zip(sections[1::2], sections[2::2]):
        if "Forge CLIs" in title:
            # The last task in the section. Without this the negative
            # assertions below would pass against a slice that ended early.
            assert "Remove the forge CLI download directory" in body, (
                "the forge CLI section slice is truncated; the assertions "
                "against it would not mean what they say"
            )
            return body
    raise AssertionError("no forge CLI section in the role's task file")


def _default(name: str, defaults: str) -> str:
    match = re.search(rf'^{name}:\s*"?([^"\n#]+)"?', defaults, re.MULTILINE)
    assert match, f"defaults/main.yml defines no {name}"
    return match.group(1).strip()


def _checksum_map(name: str, defaults: str) -> dict[str, str]:
    body = re.search(rf"^{name}:\n((?:\s+\S+:.*\n)+)", defaults, re.MULTILINE)
    assert body, f"defaults/main.yml pins no {name}"
    return {
        arch: digest
        for arch, digest in re.findall(
            r"^\s+(\S+):\s*\"?([0-9a-f]{64})", body.group(1), re.MULTILINE
        )
    }


def _build_args(body: str) -> dict[str, str]:
    """The `ARG NAME=value` pairs declared in a Dockerfile."""
    return dict(re.findall(r"^ARG\s+([A-Z0-9_]+)=(\S+)", body, re.M))


class TestTheBinariesComeFromTheVendors:
    def test_the_section_does_not_use_apt(self, yaml_only):
        """The regression in one line. `apt: name=[gh, glab]` is what the
        section used to say, and it reads as the obvious thing to do."""
        assert not re.search(
            r"^\s+(ansible\.builtin\.)?apt(_repository)?:", yaml_only, re.MULTILINE
        ), (
            "the forge CLI install uses apt, which on Debian 13 lands gh 2.46 "
            "and glab 1.53 — decades of releases behind what both Dockerfiles "
            "pin and what the known-good marks name"
        )

    @pytest.mark.parametrize("name", ["gh", "glab"])
    def test_the_download_names_the_vendor_release(self, block, name):
        hosts = {
            "gh": "https://github.com/cli/cli/releases/download/",
            "glab": "https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/packages/",
        }
        assert hosts[name] in block, f"{name} is not fetched from its own project"

    @pytest.mark.parametrize(
        "var",
        ["istota_developer_gh_checksums", "istota_developer_glab_checksums"],
    )
    def test_the_download_is_checksum_pinned(self, yaml_only, var):
        """A release fetched without a pinned digest is whatever the host
        served that day, and this one runs as root and then runs as the agent.

        Followed as a chain rather than as one substring, because the download
        is a loop: `get_url` names `item.sha256`, and `item.sha256` is what has
        to come from the pinned map. Checking only that the section mentions
        the map somewhere would pass against a `checksum:` key deleted outright
        while the `vars:` entry that feeds it stayed behind.
        """
        checksums = re.findall(r"^\s+checksum:\s*(.+)$", yaml_only, re.MULTILINE)
        assert checksums, "the forge CLI downloads pin no checksum"
        fields = {
            expr.strip().strip('"').removeprefix("sha256:").strip("{} ")
            for expr in checksums
        }
        assert fields == {"item.sha256"}, (
            f"the checksum expressions {checksums!r} do not all read a per-item "
            "digest; a literal or an empty value pins nothing"
        )
        assigned = re.search(
            r"^\s+sha256:\s*\"\{\{\s*(\S+)\[", yaml_only, re.MULTILINE | re.DOTALL
        )
        assert assigned, "no `sha256:` field is assigned from a pinned digest map"
        assert re.search(rf'sha256:\s*"\{{\{{\s*{var}\[', yaml_only), (
            f"no release entry takes its digest from {var}"
        )

    def test_an_unpinned_architecture_fails_the_play(self, block):
        """The one thing worse than an old binary is an unverified one. Both
        Dockerfiles fail the build on an unmapped arch; this is the same guard
        on the same two artifacts, so it has to fail rather than skip the pin."""
        assert "No forge CLI digest is pinned for" in block

    def test_the_download_does_not_land_in_a_shared_directory(self, block):
        """The download runs as root on a host with a real unprivileged bot
        account. A predictable name under /tmp is a destination somebody else
        can prepare, and the digest authenticates the payload, not the path."""
        dests = re.findall(r"^\s+dest:\s*(.+)$", block, re.MULTILINE)
        assert dests, "no download destination found"
        assert not any(d.strip().strip('"').startswith("/tmp/") for d in dests), dests

    def test_the_download_directory_is_removed_even_on_failure(self, block):
        """Otherwise a failed download leaves one behind on every run."""
        assert "always:" in block
        assert "state: absent" in block

    def test_dpkg_is_not_used_to_install_the_package(self, yaml_only):
        """`dpkg -i` would put the binary back at /usr/bin/gh, under apt's
        ownership, fighting the package the archive still has. The binary is
        extracted from the .deb instead and installed under /usr/local, which
        is the half of the filesystem the distro leaves alone.

        Paired with the positive: without it, a section that installed nothing
        at all would satisfy the `not in`.
        """
        assert "dpkg-deb -x" in yaml_only
        assert not re.search(r"dpkg\s+-i", yaml_only)

    @pytest.mark.parametrize(
        "var", ["istota_developer_gh_bin_path", "istota_developer_glab_bin_path"]
    )
    def test_the_binary_lands_where_the_rendered_config_points(self, block, var):
        """The install destination is the same variable `config.toml.j2`
        renders into `[developer]`, rather than a directory stated a second
        time in the task file. Two spellings of the same path is how a
        deployment ends up with a working binary and a config naming a
        different one, and `os.execve` exits 6 mid-task on the difference.
        """
        assert var in block, (
            f"the install does not derive its destination from {var}, so the "
            "path it writes to and the path the wrapper execs can drift"
        )


class TestTheInstallIsGatedAndIdempotent:
    def test_the_install_is_gated_on_the_developer_skill(self, block):
        """Nothing else on the host runs a forge command, so a deployment with
        the skill off should not grow two binaries it will never call."""
        assert "istota_developer_enabled" in block

    def test_the_install_is_skipped_when_the_pinned_version_is_already_there(
        self, block
    ):
        """Without this the role re-downloads ~30 MB on every play, and every
        play reports changed."""
        assert "istota_developer_gh_version" in block
        assert "istota_developer_glab_version" in block

    def test_every_command_read_runs_in_check_mode(self, block):
        """`command` is skipped under `--check`, so a later task dereferencing
        its registered result raises "dict object has no attribute 'stdout'"
        and takes the whole play down. The role has hit this twice elsewhere
        and carries `check_mode: false` at those sites."""
        for name, body in _tasks_with_register(block):
            assert "check_mode: false" in body, (
                f"task {name!r} registers a command result but is skipped "
                "under --check, so whatever reads it raises on a missing "
                "attribute"
            )


class TestThePinsMatchEverywhereElse:
    """The parity contract. Three files name these two versions — this role and
    the two Dockerfiles — and a bump that moves some of them is the drift."""

    @pytest.mark.parametrize(
        "role_var,docker_arg",
        [
            ("istota_developer_gh_version", "GH_VERSION"),
            ("istota_developer_glab_version", "GLAB_VERSION"),
        ],
    )
    def test_the_role_pins_what_both_images_pin(self, defaults, role_var, docker_arg):
        pinned = _default(role_var, defaults)
        assert pinned == _build_args(DOCKERFILE.read_text())[docker_arg], (
            f"{role_var} differs from docker/istota/Dockerfile's {docker_arg}"
        )
        assert pinned == _build_args(DEVBOX_DOCKERFILE.read_text())[docker_arg], (
            f"{role_var} differs from docker/devbox/Dockerfile's {docker_arg}"
        )

    @pytest.mark.parametrize(
        "role_var,docker_prefix",
        [
            ("istota_developer_gh_checksums", "GH_DEB_SHA256"),
            ("istota_developer_glab_checksums", "GLAB_DEB_SHA256"),
        ],
    )
    def test_the_role_pins_the_same_digests(self, defaults, role_var, docker_prefix):
        """Same version, same asset, so the same bytes. A digest that differs
        means one of the two is fetching something else under the same name —
        which is precisely what a supply-chain pin exists to make visible.
        """
        mine = _checksum_map(role_var, defaults)
        theirs = _build_args(DOCKERFILE.read_text())
        for arch in ("amd64", "arm64"):
            assert mine[arch] == theirs[f"{docker_prefix}_{arch.upper()}"], (
                f"{role_var}[{arch}] differs from the image's pinned digest"
            )

    @pytest.mark.parametrize(
        "role_var",
        ["istota_developer_gh_checksums", "istota_developer_glab_checksums"],
    )
    def test_a_digest_is_pinned_for_every_architecture_the_role_maps(
        self, defaults, role_var
    ):
        """arm64 is not hypothetical here — the images build on it, and an
        arm64 digest guarded by nothing is how a bump passes every test and
        fails at `get_url` on the one host that runs it."""
        names = set(_checksum_map(role_var, defaults))
        assert {"amd64", "arm64"} <= names, (
            f"{role_var} pins {sorted(names)}; the role maps both architectures"
        )


class TestTheRenderedConfigPointsAtTheNewLocation:
    @pytest.mark.parametrize(
        "var,name",
        [("istota_developer_gh_bin_path", "gh"),
         ("istota_developer_glab_bin_path", "glab")],
    )
    def test_the_bin_path_default_is_where_the_role_installs(self, defaults, var, name):
        """The half of this change that breaks silently: the section installs
        into /usr/local/bin, and a default still naming /usr/bin points the
        wrapper's `os.execve` at the stale apt binary — or at nothing, on a
        host that never had the package."""
        assert _default(var, defaults) == f"{INSTALL_DIR}/{name}"


def _tasks_with_register(block: str):
    """(name, body) for each task in the section that registers a command result."""
    chunks = re.split(r"^\s*- name: ", block, flags=re.MULTILINE)[1:]
    for chunk in chunks:
        name = chunk.split("\n", 1)[0].strip()
        if re.search(r"^\s+register:", chunk, re.MULTILINE) and re.search(
            r"^\s+ansible\.builtin\.command:", chunk, re.MULTILINE
        ):
            yield name, chunk
