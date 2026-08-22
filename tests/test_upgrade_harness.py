"""The upgrade tier's wiring, checked from the side that needs no Docker.

`tests/image/test_upgrade.py` cannot run here: it boots the shipped image over
artifacts captured from an older release. What can be checked without a daemon
is everything that decides *which* release the tier upgrades from and *what*
the old deployment looked like — and every failure mode in that wiring is
silent in the same way the rest of this spec's tiers were:

  * a floor tag that has drifted past the release which shipped the forge
    binaries makes the drift assertion vacuous — the floor's config would name
    the same path the new image installs, so `forge_config_drift` reports `ok`
    and the tier passes while asserting nothing;
  * an anchor that resolves to the wrong commit tests an upgrade nobody
    performs;
  * a captured config with the developer skill switched off leaves every forge
    check `SKIP`ped, which is the "no FAIL" the tier is looking for and means
    nothing (the spec's own "a doctor assertion must name the environment that
    makes its checks run");
  * a cache key that ignores the anchor serves one release's config for
    another, which reads as an upgrade that mysteriously stopped drifting.

The same shape as `tests/test_image_tier.py` and `tests/test_linux_runner.py`,
which guard the tiers below this one.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

from .support import upgrade

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout.strip()


def _floor_resolves() -> bool:
    """Whether the committed floor tag exists in this clone.

    These tests run in the *default* suite — they carry no `image` marker,
    because the wiring they check needs no Docker. But several of them resolve
    the floor tag, and `resolve_anchor` raises rather than skipping when it is
    absent. A CI checkout with `fetch-depth: 1` or `--no-tags` would go red on
    a handful of tests for a reason that has nothing to do with the change
    under test. `scripts/test-upgrade.sh` already guards this for the shell
    half; this is the Python half.
    """
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--verify", "--quiet",
         f"{upgrade.read_floor(upgrade.FLOOR_FILE)}^{{commit}}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode == 0


needs_floor_tag = pytest.mark.skipif(
    not _floor_resolves(),
    reason=(
        "the floor tag named by scripts/upgrade-floor does not resolve in this "
        "clone; `git fetch --tags --unshallow` makes these runnable"
    ),
)


def _grep_paths(pattern: str, treeish: str) -> list[str]:
    """Files under `docker/istota` matching `pattern` at `treeish`.

    `git grep` exits 1 for "no matches", which is a normal answer here rather
    than a failure — hence not routed through `_git`. Anything above 1 is a
    real error and is raised, because a grep that could not run must not read
    as a clean tree.
    """
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "grep", "-l", pattern, treeish, "--", "docker/istota"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode > 1:
        raise AssertionError(
            f"git grep {pattern} {treeish} failed (exit {result.returncode}): "
            f"{result.stderr}"
        )
    return [line for line in result.stdout.splitlines() if line.strip()]


class TestTheFloorFile:
    """`scripts/upgrade-floor` is the committed statement of the supported span."""

    def test_it_exists_and_names_one_ref(self):
        assert upgrade.FLOOR_FILE.exists(), (
            f"{upgrade.FLOOR_FILE} is the supported upgrade span; the tier has no "
            f"far anchor without it"
        )
        assert upgrade.read_floor(upgrade.FLOOR_FILE)

    def test_comments_and_blank_lines_are_ignored(self, tmp_path):
        path = tmp_path / "floor"
        path.write_text("# why this tag\n\n  v1.2.3  \n\n")
        assert upgrade.read_floor(path) == "v1.2.3"

    def test_an_empty_file_is_an_error_not_an_empty_tag(self, tmp_path):
        path = tmp_path / "floor"
        path.write_text("# nothing but a comment\n")
        with pytest.raises(upgrade.UpgradeHarnessError, match="names no ref"):
            upgrade.read_floor(path)

    def test_two_refs_are_an_error(self, tmp_path):
        """Silently taking the first would pin a floor nobody chose."""
        path = tmp_path / "floor"
        path.write_text("v1.2.3\nv1.2.4\n")
        with pytest.raises(upgrade.UpgradeHarnessError, match="one ref"):
            upgrade.read_floor(path)

    @needs_floor_tag
    def test_the_committed_floor_resolves_in_this_repo(self):
        commit = _git("rev-parse", "--verify", f"{upgrade.read_floor(upgrade.FLOOR_FILE)}^{{commit}}")
        assert len(commit) == 40

    @needs_floor_tag
    def test_the_floor_predates_the_release_that_shipped_the_forge_binaries(self):
        """The guard that keeps the drift assertion from going vacuous.

        `developer.forge_config_drift` reports `WARN` only because the floor's
        rendered `config.toml` names a `gh_bin_path` that the new image does not
        install at — in practice because the floor renders no such key at all
        and the dataclass default stands. Bump the floor past the release that
        added `gh_bin_path` to the render and the captured config names the very
        path the image ships, drift reports `ok`, and `tests/image/test_upgrade.py`
        passes while asserting nothing.

        Asserted as the property rather than as "predates commit e8b2fe9b", so
        the check still means something after a rebase or a squash.
        """
        floor = upgrade.read_floor(upgrade.FLOOR_FILE)

        # The control, first. `git grep` exits 1 on no matches, so a broken
        # invocation — a bad pathspec, a tree-ish this git cannot read — is
        # indistinguishable from the clean answer, and this assertion would be
        # green against every floor ever chosen. HEAD renders the key; if the
        # same call cannot see it there, the call is what is wrong.
        assert _grep_paths("gh_bin_path", "HEAD"), (
            "the control failed: `git grep gh_bin_path` finds nothing under "
            "docker/istota at HEAD, where render-config.sh renders it. This "
            "test cannot distinguish a good floor from a broken grep."
        )

        rendered = _grep_paths("gh_bin_path", floor)
        assert not rendered, (
            f"the floor tag {floor} already renders gh_bin_path "
            f"({rendered}), so its config names the path the new image "
            f"installs and developer.forge_config_drift will report ok. "
            f"The volume shape would then pass while asserting nothing — pick an "
            f"older floor, or replace the drift assertion with one that still "
            f"has a subject."
        )


class TestTheDriver:
    """`scripts/test-upgrade.sh` and the tier must agree on the vocabulary.

    The driver translates `--shape` into `ISTOTA_UPGRADE_SHAPES` and the tier
    skips the shapes that value leaves out. A name in one and not the other is
    silent in the worst direction: `--shape volume` would set a value no fixture
    recognises, every shape would skip, and the run would report success having
    executed nothing. That is the same silent non-execution `test-linux.sh`
    grew a post-condition for.
    """

    DRIVER = REPO_ROOT / "scripts" / "test-upgrade.sh"

    def test_the_driver_is_executable(self):
        assert self.DRIVER.exists()
        assert self.DRIVER.stat().st_mode & 0o111, f"{self.DRIVER} is not executable"

    def test_the_shape_vocabularies_match(self):
        from .image.test_upgrade import SHAPES

        driver = self.DRIVER.read_text()
        match = re.search(r"^\s*(code\|volume\|both)\)\s*;;", driver, re.M)
        assert match, (
            "could not find the `--shape` validation case in "
            f"{self.DRIVER}; if it moved, this guard is no longer watching it"
        )
        accepted = set(match.group(1).split("|")) - {"both"}
        assert accepted == set(SHAPES), (
            f"the driver accepts {sorted(accepted)} but tests/image/test_upgrade.py "
            f"knows {sorted(SHAPES)}. A shape in one and not the other makes the "
            f"run skip everything and report success."
        )

    def test_it_keeps_the_tier_off_xdist(self):
        """Session-scoped image fixtures under N workers race to build one tag."""
        assert "-n0" in self.DRIVER.read_text()

    def test_it_selects_the_image_marker(self):
        """Without `-m image` the addopts expression deselects the whole file."""
        assert "-m image" in self.DRIVER.read_text()


class TestAnchorResolution:
    def test_an_explicit_ref_wins(self):
        anchor = upgrade.resolve_anchor(REPO_ROOT, ref="HEAD")
        assert anchor.commit == _git("rev-parse", "HEAD")
        assert anchor.ref == "HEAD"

    @needs_floor_tag
    def test_the_floor_flag_reads_the_committed_file(self):
        anchor = upgrade.resolve_anchor(REPO_ROOT, floor=True)
        assert anchor.ref == upgrade.read_floor(upgrade.FLOOR_FILE)

    def test_the_default_is_the_merge_base_with_the_default_branch(self):
        anchor = upgrade.resolve_anchor(REPO_ROOT)
        expected = _git("merge-base", "HEAD", upgrade.default_branch(REPO_ROOT))
        assert anchor.commit == expected

    def test_an_explicit_ref_and_the_floor_flag_together_are_refused(self):
        """Two anchors named at once is a driver bug, not a preference."""
        with pytest.raises(upgrade.UpgradeHarnessError, match="both"):
            upgrade.resolve_anchor(REPO_ROOT, ref="HEAD", floor=True)

    def test_an_unknown_ref_says_so(self):
        with pytest.raises(upgrade.UpgradeHarnessError, match="no-such-ref"):
            upgrade.resolve_anchor(REPO_ROOT, ref="no-such-ref")


class TestTheCaptureCacheKey:
    """Capturing a release's config costs a container; serving the wrong one costs a day."""

    def test_two_anchors_do_not_share_a_directory(self, tmp_path):
        one = upgrade.capture_dir(tmp_path, "a" * 40, upgrade.capture_digest({"A": "1"}))
        two = upgrade.capture_dir(tmp_path, "b" * 40, upgrade.capture_digest({"A": "1"}))
        assert one != two

    def test_two_environments_do_not_share_a_directory(self, tmp_path):
        """The captured config is a function of the render environment too.

        The forge stack turns the `[developer]` block on through these
        variables. A cache keyed on the commit alone would serve a
        developer-less config to the run whose whole subject is the forge
        block, and every forge check would SKIP.
        """
        one = upgrade.capture_dir(tmp_path, "a" * 40, upgrade.capture_digest({"A": "1"}))
        two = upgrade.capture_dir(tmp_path, "a" * 40, upgrade.capture_digest({"A": "2"}))
        assert one != two

    def test_the_digest_does_not_depend_on_key_order(self):
        assert upgrade.capture_digest({"A": "1", "B": "2"}) == upgrade.capture_digest(
            {"B": "2", "A": "1"}
        )


class TestTheRenderEnvironment:
    """What makes the forge checks run at all."""

    def test_the_developer_block_is_switched_on(self):
        env = upgrade.render_env(nextcloud_url="http://stub:1")
        assert env["ISTOTA_DEVELOPER_ENABLED"] == "true"
        assert env["ISTOTA_DEVELOPER_REPOS_DIR"]

    def test_a_forge_token_is_configured(self):
        """Without one, `_forge_token_gate` SKIPs every developer.* check.

        A tier asserting "no FAIL" over a set of SKIPs is the vacuous shape this
        spec has found at every layer.
        """
        env = upgrade.render_env(nextcloud_url="http://stub:1")
        assert env["ISTOTA_DEVELOPER_GITLAB_TOKEN"]

    def test_it_does_not_inherit_the_ambient_environment(self, monkeypatch):
        """The captured config must not depend on the terminal that ran pytest."""
        monkeypatch.setenv("ISTOTA_DEVELOPER_GITHUB_TOKEN", "leaked-from-the-shell")
        env = upgrade.render_env(nextcloud_url="http://stub:1")
        assert "leaked-from-the-shell" not in env.values()

    def test_no_forge_binary_path_is_pinned(self):
        """Pinning one would manufacture the drift the tier is meant to observe.

        The whole subject of the volume shape is that an old release rendered no
        `gh_bin_path` and the dataclass default stood. Setting
        `ISTOTA_DEVELOPER_GH_BIN_PATH` here would produce the same WARN from a
        value the harness chose, which proves nothing about the release.
        """
        env = upgrade.render_env(nextcloud_url="http://stub:1")
        assert "ISTOTA_DEVELOPER_GH_BIN_PATH" not in env
        assert "ISTOTA_DEVELOPER_GLAB_BIN_PATH" not in env


class TestTheProvisioningFlag:
    """The entrypoint `source`s this file, so every value is shell input."""

    def test_it_carries_the_keys_the_entrypoint_sources(self):
        flag = upgrade._provisioning_flag(upgrade.render_env(nextcloud_url="http://s:1"))
        assert "USER_NAME=" in flag
        assert "BOT_USER=" in flag

    def test_a_value_with_shell_metacharacters_is_quoted(self):
        """The reason this is built in Python rather than by a heredoc.

        A quoted heredoc writes `${USER_NAME}` literally and leaves the
        expansion to the entrypoint's own `source`; an unquoted one expands `$`
        inside the values. Either way a value carrying metacharacters is
        *evaluated* at source time rather than read. `shlex.quote` settles it.
        """
        hostile = "a'b $(touch /tmp/pwned) `id`"
        flag = upgrade._provisioning_flag({"USER_NAME": hostile})
        # `sh` sourcing the file is the real reader; ask it, rather than
        # asserting on the quoting we happened to produce.
        result = subprocess.run(
            ["sh", "-c", '. /dev/stdin; printf %s "$USER_NAME"'],
            input=flag,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == hostile, (
            f"sourcing the flag did not round-trip the value: {result.stdout!r}"
        )

    def test_the_oauth_keys_are_written_empty(self):
        """The capture has no Nextcloud to register a client against.

        The entrypoint gates its `[web]` block on these being non-empty, so a
        fabricated client id would render a config section describing an OAuth
        client that does not exist.
        """
        flag = upgrade._provisioning_flag(upgrade.render_env(nextcloud_url="http://s:1"))
        assert "OAUTH_CLIENT_ID=''" in flag


class TestTheNextcloudStub:
    """Two endpoints, so the entrypoint's 120-second poll exits at once."""

    @pytest.fixture
    def stub(self):
        served = upgrade.serve_nextcloud_stub()
        try:
            yield served
        finally:
            served.close()

    def test_status_php_carries_the_literal_the_entrypoint_greps_for(self, stub):
        """No normalization, because the entrypoint does none.

        The probe is `grep -q '"installed":true'` over the raw body. An earlier
        version of this test compared `body.replace(" ", "")`, which is green
        against `json.dumps`'s default `"installed": true` — and that spacing is
        exactly what made a real capture spin for 121.9 seconds instead of
        eight. The assertion has to be the byte sequence.
        """
        import urllib.request

        with urllib.request.urlopen(f"{stub.url}/status.php", timeout=10) as response:
            body = response.read().decode()
        assert '"installed":true' in body

    def test_the_probes_own_grep_matches_the_stub(self):
        """The contract read out of the entrypoint, not restated by hand.

        A copy of the pattern in this file could drift from the shipped
        entrypoint and neither side would notice. This reads the pattern the
        entrypoint actually greps for and runs it against the stub's body.
        """
        entrypoint = (REPO_ROOT / "docker" / "istota" / "entrypoint.sh").read_text()
        match = re.search(r"""grep -q '("installed":[^']*)'""", entrypoint)
        assert match, (
            "no `grep -q '\"installed\":…'` in docker/istota/entrypoint.sh — the "
            "readiness probe moved, and this stub is now guessing at a contract "
            "that no longer exists"
        )
        assert match.group(1) in upgrade.STATUS_PHP_BODY.decode()

    def test_the_room_endpoint_answers_200_with_a_token(self, stub):
        """The entrypoint's readiness probe requires exactly this pair.

        It greps `status.php` for `"installed":true` *and* requires HTTP 200
        from the OCS room endpoint. One without the other leaves the poll
        spinning for its full two minutes.
        """
        import json
        import urllib.request

        request = urllib.request.Request(
            f"{stub.url}/ocs/v2.php/apps/spreed/api/v4/room?format=json",
            method="POST",
            data=b"{}",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == 200
            payload = json.loads(response.read().decode())
        assert payload["ocs"]["data"]["token"]

    def test_an_unknown_path_is_refused_rather_than_faked(self, stub):
        """A 501 naming the path, as the forge stub does.

        An entrypoint reaching an endpoint this stub does not implement is the
        signal that a newer release wants more provisioning than two endpoints,
        and it should say so rather than hang.
        """
        import urllib.error
        import urllib.request

        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"{stub.url}/ocs/v2.php/cloud/users", timeout=10)
        assert caught.value.code == 501
        assert "/ocs/v2.php/cloud/users" in caught.value.read().decode()


class TestTheFloorDatabase:
    """Built from the anchor's own `schema.sql`, with rows for migrations to touch."""

    @pytest.fixture(scope="class")
    def floor_db(self, tmp_path_factory):
        # The skip lives inside the fixture, not as a mark on it: pytest does
        # not apply `skipif` to a fixture function, so a mark here would be
        # silently ignored and the fixture would raise in a tagless clone.
        if not _floor_resolves():
            pytest.skip(
                "the floor tag named by scripts/upgrade-floor does not resolve "
                "in this clone; `git fetch --tags --unshallow`"
            )
        anchor = upgrade.resolve_anchor(REPO_ROOT, floor=True)
        return upgrade.build_anchor_db(
            REPO_ROOT, anchor.commit, tmp_path_factory.mktemp("floordb") / "istota.db"
        )

    def test_it_carries_the_anchors_schema(self, floor_db):
        with sqlite3.connect(floor_db) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "tasks" in tables

    def test_it_is_not_empty(self, floor_db):
        """Migrations that rewrite rows need rows.

        An empty database exercises the DDL half of a migration and nothing
        else, which is the half least likely to be wrong.
        """
        with sqlite3.connect(floor_db) as conn:
            assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] > 0

    def test_the_schema_is_the_anchors_and_not_the_working_trees(self, floor_db):
        """The property that makes the migration assertion mean anything.

        Building it from the checked-out `schema.sql` would test HEAD migrating
        HEAD, which is a no-op by construction and green forever.
        """
        anchor = upgrade.resolve_anchor(REPO_ROOT, floor=True)
        recorded = upgrade.anchor_schema_digest(REPO_ROOT, anchor.commit)
        assert recorded != upgrade.schema_digest((REPO_ROOT / "schema.sql").read_text()), (
            "the floor tag's schema.sql is identical to the working tree's, so "
            "the volume shape migrates nothing. Either the floor is too recent "
            "to be a useful anchor, or schema.sql has genuinely not moved in a "
            "month and the migration assertion should be retired."
        )
