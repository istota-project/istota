"""The role has to put a *usable* gitleaks on the deployment (ISSUE-291).

The hook refuses an unattended agent commit when gitleaks cannot run, so from
here on the install is not a nicety — it is what keeps the developer skill able
to commit at all. Two ways it can be wrong, and the second is the one that
looks right:

  * absent, which is the state ISSUE-291 was filed about; and
  * installed from the Debian archive, which on trixie means 8.16 — a binary
    with no `git` subcommand, so the hook's capability probe rejects it and
    every agent commit is refused by the fix rather than by a secret.

That makes the *source* of the binary part of the contract, not an
implementation detail, which is why this test reads the task file rather than
only checking that some task mentions gitleaks.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TASKS_FILE = REPO_ROOT / "deploy" / "ansible" / "tasks" / "main.yml"
DEFAULTS = REPO_ROOT / "deploy" / "ansible" / "defaults" / "main.yml"

# `gitleaks git` replaced `detect`/`protect` here. Below it the hook's probe
# fails, so this is the floor the deployment has to clear.
GIT_SUBCOMMAND_FLOOR = (8, 19, 0)


@pytest.fixture(scope="module")
def tasks() -> str:
    return TASKS_FILE.read_text()


@pytest.fixture(scope="module")
def defaults() -> str:
    return DEFAULTS.read_text()


def _default(name: str, defaults: str) -> str:
    match = re.search(rf'^{name}:\s*"?([^"\n#]+)"?', defaults, re.MULTILINE)
    assert match, f"defaults/main.yml defines no {name}"
    return match.group(1).strip()


class TestTheRoleInstallsIt:
    def test_a_task_installs_gitleaks(self, tasks):
        assert "Install gitleaks" in tasks

    def test_the_install_is_gated_on_the_developer_skill(self, tasks):
        """Nothing else on the host runs the hook, so a deployment with the
        skill off should not grow a binary it will never call."""
        block = _gitleaks_block(tasks)
        assert "istota_developer_enabled" in block

    def test_the_binary_is_not_taken_from_the_debian_archive(self, tasks):
        """The trap. `apt: name=gitleaks` reads as the obvious thing to do and
        installs a version the hook cannot use."""
        block = _gitleaks_block(tasks)
        # Both spellings. Every task in this section uses the FQCN form, so a
        # pattern matching only the short `apt:` could not catch the regression
        # it exists for — somebody replacing this block with
        # `ansible.builtin.apt: name=gitleaks`, which is the obvious thing to
        # reach for and the thing that breaks the hook.
        assert not re.search(
            r"^\s+(ansible\.builtin\.)?apt(_repository)?:", block, re.MULTILINE
        ), (
            "the gitleaks install uses apt, which on Debian 13 lands 8.16 — "
            "below the floor the hook's `gitleaks git` invocation needs"
        )

    def test_the_download_is_checksum_pinned(self, tasks):
        """A release tarball fetched without a pinned digest is whatever the
        host served that day, and this particular binary is the thing deciding
        whether a credential reaches a public repository."""
        block = _gitleaks_block(tasks)
        checksum = re.search(r"^\s+checksum:\s*(.+)$", block, re.MULTILINE)
        assert checksum, "the gitleaks download pins no checksum"
        # Naming the variable, not merely carrying the key: `checksum: ""` and
        # `checksum: "sha256:{{ typo }}"` both satisfy a substring check while
        # pinning nothing.
        assert "istota_developer_gitleaks_checksums" in checksum.group(1), (
            f"the checksum expression {checksum.group(1)!r} does not read the "
            "pinned digest map"
        )

    def test_the_download_does_not_land_in_a_shared_directory(self, tasks):
        """The download runs as root. A predictable name under /tmp is a
        destination an unprivileged local account can prepare in advance, and
        the digest authenticates the payload rather than the path."""
        block = _gitleaks_block(tasks)
        dests = re.findall(r"^\s+dest:\s*(.+)$", block, re.MULTILINE)
        assert dests, "no download destination found"
        assert not any(d.strip().strip('"').startswith("/tmp/") for d in dests), dests


BANNER = "# ============================================================"


def _gitleaks_block(tasks: str) -> str:
    """The tasks between the gitleaks section banner and the next one.

    The role separates its sections with a banner line above and below the
    title, so a slice has to skip the section's *own* closing banner before
    looking for the next section — otherwise it returns the title line alone
    and every `assert ... in block` below fails while every `assert ... not in
    block` passes for the wrong reason.
    """
    sections = tasks.split(BANNER)
    for title, body in zip(sections[1::2], sections[2::2]):
        if "gitleaks" in title:
            # The last task in the section. Without this the negative
            # assertions below would pass against a slice that ended early.
            assert "subcommand the pre-commit hook calls" in body, (
                "the gitleaks section slice is truncated; the assertions "
                "against it would not mean what they say"
            )
            return body
    raise AssertionError("no gitleaks section in the role's task file")


class TestThePinnedVersionClearsTheFloor:
    def test_a_version_is_pinned(self, defaults):
        version = _default("istota_developer_gitleaks_version", defaults)
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), version

    def test_the_pinned_version_has_the_git_subcommand(self, defaults):
        version = _default("istota_developer_gitleaks_version", defaults)
        parts = tuple(int(p) for p in version.split("."))
        assert parts >= GIT_SUBCOMMAND_FLOOR, (
            f"gitleaks {version} predates the `git` subcommand, so the hook's "
            "probe would reject the binary this role installs and every agent "
            "commit would be refused"
        )

    def test_the_tasks_file_reads_the_name_the_defaults_define(self, tasks):
        """The rename from the old `istota_developer_cli_floors['gitleaks']` had
        to land in two files. Miss one and Ansible raises "undefined variable" on
        the operator's host, on the play that installs the credential scanner —
        while the Python suite stays green, because nothing else here reads the
        tasks file for this name."""
        assert "istota_developer_gitleaks_min_version" in _gitleaks_block(tasks)
        assert "istota_developer_cli_floors" not in tasks

    def test_the_floor_is_asserted_at_deploy_time(self, defaults):
        """A floor stated in the defaults and checked against the binary the
        hook will actually resolve, rather than assumed from the version this
        file happens to pin today. gh and glab carry no equivalent: any floor
        low enough to be true of the verbs the developer skill uses is also
        cleared by the archive versions it would be meant to catch. Below this
        one, `gitleaks git` does not exist and the hook cannot run at all."""
        floor = _default("istota_developer_gitleaks_min_version", defaults)
        parts = tuple(int(p) for p in floor.split("."))
        assert parts >= GIT_SUBCOMMAND_FLOOR

    def test_a_checksum_is_pinned_for_every_architecture_the_role_maps(self, defaults):
        block = re.search(
            r"^istota_developer_gitleaks_checksums:\n((?:\s+\S+:.*\n)+)",
            defaults,
            re.MULTILINE,
        )
        assert block, "defaults/main.yml pins no gitleaks checksums"
        digests = re.findall(r"^\s+(\S+):\s*\"?([0-9a-f]{64})", block.group(1), re.MULTILINE)
        names = {name.rstrip(":") for name, _ in digests}
        assert {"amd64", "arm64"} <= names, (
            f"checksums pinned for {sorted(names)}; the role maps both "
            "amd64 and arm64"
        )


class TestThePlaySurvivesCheckMode:
    """`command` is skipped under `--check`, so a later task dereferencing its
    registered result raises "dict object has no attribute 'stdout'" and takes
    the whole play down. The role already hit this twice elsewhere and carries
    `check_mode: false` at those sites."""

    def test_every_command_read_runs_in_check_mode(self, tasks):
        block = _gitleaks_block(tasks)
        commands = re.findall(
            r"^- name: (.+)\n(?:.*\n)*?", block, re.MULTILINE
        )
        # Each registered command in the section must opt out of check mode.
        for name, body in _tasks_with_register(block):
            assert "check_mode: false" in body, (
                f"task {name!r} registers a result but is skipped under "
                "--check, so whatever reads it raises on a missing attribute"
            )
        assert commands, "no tasks parsed out of the gitleaks section"


def _tasks_with_register(block: str):
    """(name, body) for each task in the section that registers a result."""
    chunks = re.split(r"^\s*- name: ", block, flags=re.MULTILINE)[1:]
    for chunk in chunks:
        name = chunk.split("\n", 1)[0].strip()
        if re.search(r"^\s+register:", chunk, re.MULTILINE) and re.search(
            r"^\s+ansible\.builtin\.command:", chunk, re.MULTILINE
        ):
            yield name, chunk


class TestTheDocumentationNamesTheLinuxRoute:
    """The install hint being macOS-only is the stated reason the deployment —
    a Linux host — never had the binary."""

    def test_the_secret_scanning_doc_covers_linux(self):
        text = (REPO_ROOT / "docs" / "development" / "secret-scanning.md").read_text()
        assert "github.com/gitleaks/gitleaks/releases" in text

    def test_the_doc_warns_off_the_debian_package(self):
        text = (REPO_ROOT / "docs" / "development" / "secret-scanning.md").read_text()
        assert "8.19" in text
