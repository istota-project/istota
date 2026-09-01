"""Layer 4 — the shipped image over an older release's state.

New code against an older `config.toml` is how ISSUE-263 reached production: the
auto-update cron `git reset --hard`s to main without running Ansible, and a
Docker rebuild over a retained volume used to keep the config the entrypoint
wrote on that volume's first boot. ISSUE-368 made that render happen on every
boot, which narrows the Docker case to the window between the image changing and
the container restarting — the assertions below still have their subject, since
this tier runs the image with `--entrypoint /bin/sh` and never re-renders the
planted anchor config. Every other tier renders a fresh config, and a fresh
config is current by definition — so none of them can see this.

Two shapes, two anchors, because one anchor is not enough:

  * **code** — the near anchor, the merge-base with the default branch. That is
    about three days at the current release cadence, which on its own is close
    to a no-op as a regression detector. It is the default because it is cheap
    and it is the span the auto-update cron actually crosses.
  * **volume** — the far anchor, `scripts/upgrade-floor`, roughly a month back.
    This is the one worth running before a release, and it is the one that
    carries the drift assertion.

**What is asserted, and what is deliberately not.** Not "reproduces ISSUE-263 as
a doctor FAIL": `resolve_real_bin`'s `IMAGE_BIN` fallback is specifically what
makes the upgrade clean, so that criterion is unreachable against current code,
and that is the fix working rather than a gap in the test. What is asserted is:

  * no `FAIL` on any check, in either shape — the regression assertion, and the
    one that would go red if a future migration or config rename broke the old
    file;
  * `developer.forge_config_drift` reports `WARN` naming both paths on the
    volume shape — the *positive* assertion, and the signal that would have
    named ISSUE-263's condition out loud;
  * on the volume shape, that migrations applied over the old database and that
    `db_relocate` is idempotent when re-run.

And because "no FAIL" is trivially satisfied by a run where every interesting
check `SKIP`ped — the spec's own "a doctor assertion must name the environment
that makes its checks run" — every shape asserts first that the developer
checks actually ran.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from ..support import upgrade
from ..support.upgrade import Anchor
from .conftest import REPO, BuiltImage, require_docker

pytestmark = pytest.mark.image

# Long enough for `istota init` to run every migration over a month-old
# database on an emulated build, and no longer.
RUN_TIMEOUT = 600

# Which shapes this run covers, and where each starts. Both come from the
# environment so `scripts/test-upgrade.sh` can set them without this file
# growing a second argument-parsing surface — the same way `ISTOTA_IMAGE_TAG`
# already lets the driver hand the tier a pre-built image.
SHAPES = ("code", "volume")

# Scripts bind-mounted into the container. A directory rather than inline
# `python -c`, because the relocation seed has to import istota and a one-liner
# carrying that through two levels of shell quoting is unreadable and easy to
# get subtly wrong.
SEED_DIR = Path(__file__).parent / "seed"

# What the drift WARN must name: (the stale configured path, the path the
# wrapper will actually exec). **Literals, not imports.** Reading these from
# `forge_bin.FALLBACK_BIN` / `IMAGE_BIN` would take the oracle from the same
# constants `check_forge_config_drift` computes the message from, so moving the
# shipped location and the constant together keeps this green while the warning
# names a path no old `config.toml` ever contained. That is the spec's
# independent-witness rule — "a witness whose oracle vanishes along with its
# subject is not a witness" — and it is the shape that made Stage 5's tier green
# on the literal ISSUE-263 configuration.
#
# The left column is the dataclass default an old release left standing; the
# right is where `docker/istota/Dockerfile` installs the real binaries.
EXPECTED_DRIFT_PATHS = {
    "gh": ("/usr/local/bin/gh", "/usr/local/lib/istota_forge/gh"),
    "glab": ("/usr/local/bin/glab", "/usr/local/lib/istota_forge/glab"),
}


def selected_shapes() -> tuple[str, ...]:
    raw = os.environ.get("ISTOTA_UPGRADE_SHAPES", "").strip()
    if not raw:
        return SHAPES
    chosen = tuple(part.strip() for part in raw.split(",") if part.strip())
    unknown = [shape for shape in chosen if shape not in SHAPES]
    assert not unknown, f"unknown upgrade shape(s) {unknown}; known: {list(SHAPES)}"
    return chosen


def anchor_for(shape: str) -> Anchor:
    """Where this shape upgrades from.

    The shape and the anchor are separate inputs with a default pairing, not one
    input wearing two hats. `--shape volume` from the floor is the pairing worth
    running before a release, but reproducing a specific report means pointing
    either shape at a specific ref — which is what `ISTOTA_UPGRADE_FROM`
    (`--from`) is for, and it overrides the default for whichever shapes run.
    """
    override = os.environ.get("ISTOTA_UPGRADE_FROM", "").strip()
    if override:
        return upgrade.resolve_anchor(REPO, ref=override)
    return upgrade.resolve_anchor(REPO, floor=(shape == "volume"))


@dataclass(frozen=True)
class UpgradeResult:
    """One release's artifacts, and what the new image said about them."""

    shape: str
    anchor: Anchor
    config_dir: Path
    db_dir: Path
    init: subprocess.CompletedProcess
    doctor: list[dict]
    # Rows in `tasks` immediately after `istota init`, captured here because
    # later tests in the same class mutate this database.
    task_rows: int

    def named(self, prefix: str) -> list[dict]:
        return [row for row in self.doctor if row["name"].startswith(prefix)]

    def statuses(self, prefix: str) -> set[str]:
        return {row["status"] for row in self.named(prefix)}

    def report(self) -> str:
        """Every non-ok line, for an assertion message worth reading."""
        lines = [
            f"{row['name']}: {row['status']} — {row.get('detail', '')}"
            for row in self.doctor
            if row["status"] != "ok"
        ]
        return "\n".join(lines) or "(every check reported ok)"


def _docker_run(
    image: BuiltImage,
    config_dir: Path,
    db_dir: Path,
    argv: list[str],
    *,
    timeout: int = RUN_TIMEOUT,
    mounts: list[tuple[Path, str]] | None = None,
    shared_dir: Path | None = None,
) -> subprocess.CompletedProcess:
    """The new image over the old release's directories.

    `/mnt/shared` and `/data/repos` are tmpfs rather than absent, because
    `check_mount_liveness` reports a configured-but-unmounted workspace as
    `FAIL` — correctly: a dropped rclone mount leaves exactly an empty
    directory. A tmpfs is a real mount point, which is the same accommodation
    `docker-compose.test.yml` makes for the smoke tier. `shared_dir` swaps that
    tmpfs for a host directory where a test needs state to survive between two
    runs, which the relocation test does.

    Output is scrubbed. `/data/config/config.toml` in this container carries the
    forge token and two passwords from `render_env`, every assertion below
    interpolates stdout and stderr into its message, and pytest renders
    `CompletedProcess.args` into the report on failure — the same three paths
    `tests/image/conftest.py:run_in` was fixed for. Fabricated values today; the
    rule is what keeps that true.
    """
    require_docker()
    cmd = [
        "docker", "run", "--rm",
        "--entrypoint", "/bin/sh",
        "-v", f"{config_dir}:/data/config",
        "-v", f"{db_dir}:/data/db",
        "--tmpfs", "/data/repos",
    ]
    if shared_dir is not None:
        cmd += ["-v", f"{shared_dir}:/mnt/shared"]
    else:
        cmd += ["--tmpfs", "/mnt/shared"]
    for host, container in mounts or []:
        cmd += ["-v", f"{host}:{container}:ro"]
    if image.platform:
        cmd += ["--platform", image.platform]
    cmd.append(image.tag)
    cmd += argv

    secrets = upgrade.render_env(nextcloud_url="placeholder")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # `TimeoutExpired.__str__` embeds the raw argv in the traceback.
        pytest.fail(
            f"`docker run` timed out after {timeout}s: "
            f"{upgrade._scrub(' '.join(cmd), secrets)}",
            pytrace=False,
        )
    return subprocess.CompletedProcess(
        args=[upgrade._scrub(part, secrets) for part in cmd],
        returncode=result.returncode,
        stdout=upgrade._scrub(result.stdout or "", secrets),
        stderr=upgrade._scrub(result.stderr or "", secrets),
    )


def _istota(*args: str) -> str:
    """The shipped console script, against the old config, as one shell word."""
    quoted = " ".join(args)
    return f"istota -c {upgrade.CONTAINER_CONFIG} {quoted}"


def _perform_upgrade(
    shape: str, image: BuiltImage, tmp_path: Path, platform: str
) -> UpgradeResult:
    if shape not in selected_shapes():
        pytest.skip(f"shape {shape!r} not selected (ISTOTA_UPGRADE_SHAPES)")
    anchor = anchor_for(shape)

    # The near anchor's own emptiness guard, the counterpart to the volume
    # shape's `test_the_schema_moved_at_all`.
    #
    # On a checkout with no local commits, `merge-base(HEAD, origin/main)` *is*
    # HEAD — which is the normal state of this repo, and `scripts/test-upgrade.sh`
    # with no arguments is the documented default. The capture then runs HEAD's
    # own entrypoint to produce the config HEAD's image is tested against, and
    # every code-shape assertion is trivially true. Skipped rather than passed,
    # because a green run that compared a tree with itself is worse than no run:
    # it is the same silent non-execution `scripts/test-linux.sh` grew a
    # post-condition for, and this driver has one too.
    head = upgrade.resolve_anchor(REPO, ref="HEAD")
    if anchor.commit == head.commit:
        pytest.skip(
            f"the {shape} shape's anchor is HEAD itself ({anchor.short}), so "
            f"this run would upgrade a tree from itself and assert nothing. "
            f"The near anchor is the merge-base with the default branch, which "
            f"equals HEAD until this checkout has a commit of its own; commit "
            f"first, or pass --from/--from-floor to name an older release."
        )

    env = upgrade.render_env(nextcloud_url="placeholder")

    captured = upgrade.capture_config(
        repo=REPO,
        anchor=anchor,
        image=image.tag,
        env=env,
        platform=platform,
        refresh=bool(os.environ.get("ISTOTA_UPGRADE_REFRESH")),
    )

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    shutil.copy2(captured, config_dir / "config.toml")
    # The entrypoint seeds this on first boot and `_user_is_web_admin` fails
    # closed without it. Part of what a retained volume carries forward.
    (config_dir / "admins").write_text(f"{env['USER_NAME']}\n")

    db_dir = tmp_path / "db"
    db_dir.mkdir()
    if shape == "volume":
        # A database as the floor release created it, so `istota init` has a
        # month of migrations to apply rather than an empty file to populate.
        upgrade.build_anchor_db(REPO, anchor.commit, db_dir / "istota.db")

    init = _docker_run(image, config_dir, db_dir, ["-c", _istota("init")])
    assert init.returncode == 0, (
        f"[{shape}] `istota init` failed over {anchor.ref}'s state (exit "
        f"{init.returncode}). This is the upgrade itself failing, before any "
        f"check ran.\n--- stdout ---\n{init.stdout}\n--- stderr ---\n{init.stderr}"
    )

    probe = _docker_run(image, config_dir, db_dir, ["-c", _istota("doctor", "--json")])
    # The exit code is deliberately not asserted here: `doctor.exit_code` is
    # non-zero on any FAIL, and naming *which* check failed is far more useful
    # than "it exited 1". `render_json` emits valid JSON either way.
    try:
        results = json.loads(probe.stdout or "[]")
    except ValueError:
        pytest.fail(
            f"[{shape}] `istota doctor --json` printed no JSON (exit "
            f"{probe.returncode})\n--- stdout ---\n{probe.stdout}\n"
            f"--- stderr ---\n{probe.stderr}",
            pytrace=False,
        )

    # Read now, while the database is still exactly what `istota init` left.
    counted = _docker_run(
        image,
        config_dir,
        db_dir,
        [
            "-c",
            "python -c \"import sqlite3;"
            "print(sqlite3.connect('/data/db/istota.db')"
            ".execute('SELECT count(*) FROM tasks').fetchone()[0])\"",
        ],
    )
    assert counted.returncode == 0, (
        f"[{shape}] could not read the upgraded database back\n"
        f"--- stderr ---\n{counted.stderr}"
    )

    # The receipt the driver's post-condition reads. Written only once a shape
    # has actually captured a release, booted the image over it and run doctor
    # — so a session where every shape skipped leaves it empty, and
    # `scripts/test-upgrade.sh` refuses to call that a clean run.
    #
    # A file rather than an exit code because skipping is a *legitimate* answer
    # here (a near anchor equal to HEAD, a shape not selected), and pytest's own
    # counts cannot tell "skipped because there was nothing to do" from
    # "skipped because the harness broke".
    receipt = os.environ.get("ISTOTA_UPGRADE_RECEIPT")
    if receipt:
        with open(receipt, "a", encoding="utf-8") as handle:
            handle.write(f"{shape} {anchor.ref} {anchor.short}\n")

    return UpgradeResult(
        shape=shape,
        anchor=anchor,
        config_dir=config_dir,
        db_dir=db_dir,
        init=init,
        doctor=results,
        task_rows=int(counted.stdout.strip()),
    )


@pytest.fixture(scope="session")
def code_upgrade(istota_image, platform, tmp_path_factory) -> UpgradeResult:
    return _perform_upgrade(
        "code", istota_image, tmp_path_factory.mktemp("upgrade-code"), platform
    )


@pytest.fixture(scope="session")
def current_config_doctor(istota_image, tmp_path_factory) -> list[dict]:
    """`doctor --json` over a config rendered from the working tree.

    The drift control. Depends on no shape and no anchor, so it runs whichever
    shapes were selected.
    """
    root = tmp_path_factory.mktemp("upgrade-current")
    config_dir = root / "config"
    upgrade.render_current_config(config_dir)
    db_dir = root / "db"
    db_dir.mkdir()

    probe = _docker_run(
        istota_image, config_dir, db_dir, ["-c", _istota("doctor", "--json")]
    )
    try:
        return json.loads(probe.stdout or "[]")
    except ValueError:
        pytest.fail(
            f"`istota doctor --json` printed no JSON for the control config "
            f"(exit {probe.returncode})\n--- stdout ---\n{probe.stdout}\n"
            f"--- stderr ---\n{probe.stderr}",
            pytrace=False,
        )


@pytest.fixture(scope="session")
def volume_upgrade(istota_image, platform, tmp_path_factory) -> UpgradeResult:
    return _perform_upgrade(
        "volume", istota_image, tmp_path_factory.mktemp("upgrade-volume"), platform
    )


class TestTheAssertionsHaveASubject:
    """Before anything else: the checks this tier cares about actually ran.

    `_dev_gate` and `_forge_token_gate` turn every `developer.*` result into a
    `SKIP` when the skill is off or has no token, and a tier asserting "no FAIL"
    over a set of SKIPs is green against any image at all. This spec has found
    that shape at every layer it built; here it gets its own test rather than a
    comment.
    """

    @pytest.mark.parametrize("shape", ["code", "volume"])
    def test_the_doctor_run_produced_results(self, shape, request):
        result = request.getfixturevalue(f"{shape}_upgrade")
        assert result.doctor, "`istota doctor --json` returned an empty list"

    @pytest.mark.parametrize("shape", ["code", "volume"])
    def test_the_forge_checks_were_not_skipped(self, shape, request):
        result = request.getfixturevalue(f"{shape}_upgrade")
        drift = result.named("developer.forge_config_drift")
        assert drift, "developer.forge_config_drift produced no results at all"
        assert result.statuses("developer.forge_config_drift") != {"skip"}, (
            f"[{shape}] every forge check SKIPped, so 'no FAIL' below asserts "
            f"nothing. The captured config from {result.anchor.ref} has the "
            f"developer skill off or no token:\n"
            + "\n".join(f"  {row['name']}: {row.get('detail', '')}" for row in drift)
        )


class TestTheUpgradeStaysClean:
    """The regression assertion, in both shapes."""

    @pytest.mark.parametrize("shape", ["code", "volume"])
    def test_no_check_fails(self, shape, request):
        result = request.getfixturevalue(f"{shape}_upgrade")
        failed = [row for row in result.doctor if row["status"] == "fail"]
        assert not failed, (
            f"[{shape}] upgrading from {result.anchor.ref} "
            f"({result.anchor.short}) left {len(failed)} failing check(s). "
            f"A config key renamed between releases surfaces here, and the fix "
            f"is a migration in config.py rather than a change to this test.\n"
            + result.report()
        )

    @pytest.mark.parametrize("shape", ["code", "volume"])
    def test_the_forge_binaries_resolve(self, shape, request):
        """The ISSUE-263 shape itself: the wrapper's exec target exists.

        Distinct from the drift check below, and that split is the point —
        resolution succeeds *because* of the `IMAGE_BIN` fallback, which is
        exactly what hides the stale config from a single combined check.
        """
        result = request.getfixturevalue(f"{shape}_upgrade")
        binaries = result.named("developer.forge_binaries")
        assert binaries, "developer.forge_binaries produced no results"
        assert result.statuses("developer.forge_binaries") <= {"ok"}, (
            f"[{shape}] the wrapper would exec a path that does not exist:\n"
            + "\n".join(
                f"  {row['name']}: {row['status']} — {row.get('detail', '')}"
                for row in binaries
            )
        )


class TestTheRetainedVolumeStillReportsItsDrift:
    """The positive assertion — the signal ISSUE-263 never had.

    A retained volume keeps a `config.toml` written before the image shipped the
    forge binaries. The deployment *works*, because resolution falls back to
    `IMAGE_BIN`. What it has lost is the property that its config file describes
    it, and this is the check that says so out loud.
    """

    def test_drift_is_warned_about(self, volume_upgrade):
        statuses = volume_upgrade.statuses("developer.forge_config_drift")
        assert "warn" in statuses, (
            f"upgrading from {volume_upgrade.anchor.ref} reported no drift. "
            f"Either the floor now renders gh_bin_path — in which case the "
            f"assertion has no subject and the floor needs to move back — or "
            f"the drift check stopped seeing a stale config, which is the "
            f"signal ISSUE-263 was missing.\n" + volume_upgrade.report()
        )

    def test_it_names_both_paths(self, volume_upgrade):
        """A WARN that does not name where to look is not a remedy.

        Both halves matter: the configured path is what an operator greps for in
        `config.toml`, and the resolved one is what the wrapper actually execs.
        """
        warned = [
            row
            for row in volume_upgrade.named("developer.forge_config_drift")
            if row["status"] == "warn"
        ]
        assert warned, "no drift WARN to inspect"
        for row in warned:
            name = row["name"].rsplit(".", 1)[-1]
            detail = row.get("detail", "")
            configured, resolved = EXPECTED_DRIFT_PATHS[name]
            assert configured in detail, (
                f"{row['name']} does not name the configured path "
                f"{configured}: {detail!r}"
            )
            assert resolved in detail, (
                f"{row['name']} does not name the path the wrapper will exec "
                f"{resolved}: {detail!r}"
            )
            assert row.get("remedy"), f"{row['name']} warns with no remedy"

    def test_a_current_config_does_not_drift(self, current_config_doctor):
        """The control for the assertion above.

        Without it, a drift check that warned unconditionally would satisfy the
        test above just as well.

        Deliberately not tied to a shape or an anchor. The first version used
        the code shape, which meant the control skipped on exactly the run the
        spec names for verifying this stage — `--from-floor --shape volume`
        selects only the volume shape — so the positive assertion ran with its
        control silently absent. It also made `--from-floor --shape both` red on
        a healthy tree, because `--from` overrides the anchor for *every*
        selected shape and the floor drifts by design.

        This renders today's config with the shipped `render-config.sh` and
        asserts the same check reports `ok` on it. Always runs, whatever was
        selected.
        """
        statuses = {
            row["status"]
            for row in current_config_doctor
            if row["name"].startswith("developer.forge_config_drift")
        }
        assert statuses, "developer.forge_config_drift produced no results"
        assert statuses == {"ok"}, (
            "a freshly rendered config drifted, which it cannot legitimately "
            "do: render-config.sh writes gh_bin_path at the path this image "
            "installs. Either the image moved the binaries without moving the "
            "render, or the drift check warns unconditionally — in which case "
            "the assertion above proves nothing.\n"
            + "\n".join(
                f"  {row['name']}: {row['status']} — {row.get('detail', '')}"
                for row in current_config_doctor
                if row["name"].startswith("developer.forge_config_drift")
            )
        )


class TestTheOldDatabaseSurvives:
    """Migrations over a month-old schema, and a replayable relocation."""

    def test_the_schema_moved_at_all(self, volume_upgrade):
        """Otherwise the migration assertions below have nothing to observe."""
        recorded = upgrade.anchor_schema_digest(REPO, volume_upgrade.anchor.commit)
        current = upgrade.schema_digest((REPO / "schema.sql").read_text())
        assert recorded != current, (
            f"{volume_upgrade.anchor.ref}'s schema.sql is identical to the "
            f"working tree's, so `istota init` migrated nothing and the "
            f"assertions below are vacuous."
        )

    def test_the_migrated_database_gained_the_tables_head_declares(
        self, volume_upgrade, istota_image
    ):
        """The migration witness — asserted against the container's file.

        The test above compares two git blobs and never touches the database;
        the row count below counts rows the harness itself seeded. Both stay
        green if `istota init` migrated nothing at all — if it opened a
        different `db_path`, or if the migration step silently no-opped. What
        makes "migrations applied over the old database" an assertion is naming
        something that must be in the upgraded file and cannot have come from
        the seed: a table HEAD's schema declares and the anchor's does not.
        """
        expected = upgrade.tables_added_since(REPO, volume_upgrade.anchor.commit)
        if not expected:
            pytest.skip(
                f"{volume_upgrade.anchor.ref}'s schema declares the same tables "
                f"as HEAD, so there is no new table to witness"
            )

        listed = _docker_run(
            istota_image,
            volume_upgrade.config_dir,
            volume_upgrade.db_dir,
            [
                "-c",
                "python -c \"import sqlite3;"
                "print(' '.join(r[0] for r in sqlite3.connect('/data/db/istota.db')"
                ".execute(\\\"SELECT name FROM sqlite_master WHERE type='table'\\\")))\"",
            ],
        )
        assert listed.returncode == 0, (
            f"could not list the migrated database's tables\n"
            f"--- stderr ---\n{listed.stderr}"
        )
        present = set(listed.stdout.split())
        missing = expected - present
        assert not missing, (
            f"`istota init` over {volume_upgrade.anchor.ref}'s database did not "
            f"create {sorted(missing)}, which HEAD's schema.sql declares and "
            f"{volume_upgrade.anchor.ref}'s does not. The upgrade ran and the "
            f"schema did not move with it."
        )

    def test_the_seeded_rows_survived_the_migration(self, volume_upgrade):
        """A migration that drops the table it is migrating also reports success.

        `istota init` exiting 0 is asserted in the fixture; that it left the old
        rows in place is a different question, and it is the one an operator
        cares about.

        The count is taken in the fixture, immediately after `istota init`, not
        here. Three tests in this class mutate the same session-scoped
        `db_dir` — a second `init`, and two `db_relocate` runs — so reading it
        at test time made the assertion depend on declaration order, which
        `-p randomly`, `--ff` or a `-k` selection all reorder. The repo's rule
        is that new tests must be order-independent.
        """
        assert volume_upgrade.task_rows >= upgrade._SEED_TASKS, (
            f"the floor database held {upgrade._SEED_TASKS} tasks before the "
            f"upgrade and holds {volume_upgrade.task_rows} after it"
        )

    def test_init_is_idempotent(self, volume_upgrade, istota_image):
        """The auto-update cron reruns it on every restart."""
        again = _docker_run(
            istota_image,
            volume_upgrade.config_dir,
            volume_upgrade.db_dir,
            ["-c", _istota("init")],
        )
        assert again.returncode == 0, (
            f"a second `istota init` over the migrated database failed (exit "
            f"{again.returncode})\n--- stdout ---\n{again.stdout}\n"
            f"--- stderr ---\n{again.stderr}"
        )

    def test_db_relocate_actually_relocates_and_is_idempotent(
        self, volume_upgrade, istota_image, tmp_path
    ):
        """It runs on the upgrade path and must survive being replayed.

        `db_relocate` moved the per-user module databases off the Nextcloud
        mount. It is a one-time migrator that any restart can re-enter, and a
        second run that reported failure would look like a broken upgrade.

        **A legacy database has to exist for this to assert anything.** The
        first version of this test ran the migrator twice against a container
        whose `/mnt/shared` was an empty tmpfs, so `relocate_module` returned
        `no_source` for every user and module and both runs exited 0 having
        done nothing — green whether or not relocation works at all. That is
        the "control that cannot fail" shape this spec has found at every
        layer.

        The source is placed through `db_relocate.legacy_db_path` rather than
        by a path spelled out here, so what the test seeds is by construction
        what the product considers legacy.
        """
        module = "feeds"
        shared = tmp_path / "shared"
        shared.mkdir()

        seeded = _docker_run(
            istota_image,
            volume_upgrade.config_dir,
            volume_upgrade.db_dir,
            ["-c", f"ISTOTA_CONFIG_PATH={upgrade.CONTAINER_CONFIG} python /seed/seed.py"],
            mounts=[(SEED_DIR, "/seed")],
            shared_dir=shared,
        )
        assert seeded.returncode == 0, (
            f"could not seed a legacy {module} database\n"
            f"--- stdout ---\n{seeded.stdout}\n--- stderr ---\n{seeded.stderr}"
        )
        legacy = seeded.stdout.strip().splitlines()[-1]
        assert legacy, "the seed script printed no path"

        script = (
            # `db_relocate.main` takes no `-c`; it calls `load_config()` bare,
            # which reads `ISTOTA_CONFIG_PATH`. Passing `-c` would be an
            # argparse error that reads as a failed relocation.
            f"ISTOTA_CONFIG_PATH={upgrade.CONTAINER_CONFIG} "
            "python -m istota.db_relocate"
        )
        first = _docker_run(
            istota_image,
            volume_upgrade.config_dir,
            volume_upgrade.db_dir,
            ["-c", script],
            shared_dir=shared,
        )
        assert first.returncode == 0, (
            f"the first db_relocate failed\n--- stdout ---\n{first.stdout}\n"
            f"--- stderr ---\n{first.stderr}"
        )
        assert "relocated" in first.stderr, first.stderr
        assert "0 relocated" not in first.stderr, (
            "db_relocate reported nothing to move over a seeded legacy database. "
            "Either the seed did not land where the product looks, or relocation "
            "is not happening — and this test cannot tell the difference from a "
            "clean run unless it checks.\n"
            f"seeded at: {legacy}\n--- stdout ---\n{first.stdout}\n"
            f"--- stderr ---\n{first.stderr}"
        )

        second = _docker_run(
            istota_image,
            volume_upgrade.config_dir,
            volume_upgrade.db_dir,
            ["-c", script],
            shared_dir=shared,
        )
        assert second.returncode == 0, (
            "db_relocate is not replayable: the second run failed over the state "
            f"the first one left\n--- stdout ---\n{second.stdout}\n"
            f"--- stderr ---\n{second.stderr}"
        )
        assert "0 relocated" in second.stderr, (
            "the second db_relocate moved something again. It is a one-time "
            "migrator that any restart re-enters; moving twice means the first "
            "run left a source behind.\n"
            f"--- stderr ---\n{second.stderr}"
        )
