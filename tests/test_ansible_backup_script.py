"""The host backup script: what it owns, and retention that does not depend on it.

ISSUE-262. The script used to discover per-user module DBs by scanning
``{mount}/Users/<user>/<bot_dir>/<module>/data/<module>.db``. ISSUE-157 moved
those DBs to local disk and nothing updated the finder, so for 39 days the run
backed up the framework DB, matched nothing else, and logged "Database backup
complete". Two things follow from that, and both are tested here:

  * **Ownership.** Per-user module DBs belong to ``db_backup.py``, which derives
    its list from ``config.users`` and its paths from ``config.module_db_path``
    and therefore followed the relocation without an edit. The script keeps the
    framework DB only.
  * **Rotation must not sit in the per-DB success path.** It did, behind the
    early return for a missing source, so a prefix that stopped being discovered
    also stopped being pruned and its backups were retained forever — 375M of
    July files on a root filesystem that had hit the monit threshold.

These tests render the script the way Ansible would and then *run* it against a
fixture tree, following ``TestClaudeVersionPrune`` in
``test_ansible_host_log_retention.py``. Grepping the template for a ``find``
line cannot tell a sweep that prunes from one that is unreachable, and
unreachable is exactly what the bug was.

``mountpoint`` is Linux-only, so the mount-available paths (remote copy,
catch-up, remote rotation) are reached through a stub earlier on ``PATH`` rather
than by rewriting the script under test.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment

# The script shells out to these; the Python sqlite3 module does not imply the
# CLI. Same posture as tests/test_forge_cli_exec.py, which skips without `gh`.
pytestmark = pytest.mark.skipif(
    not all(shutil.which(b) for b in ("bash", "sqlite3", "gzip")),
    reason="needs the bash, sqlite3 and gzip binaries the rendered script calls",
)

REPO = Path(__file__).resolve().parent.parent
ANSIBLE = REPO / "deploy" / "ansible"
DEFAULTS_FILE = ANSIBLE / "defaults" / "main.yml"
TEMPLATES = ANSIBLE / "templates"

DAY = 86400


def defaults() -> dict:
    return yaml.safe_load(DEFAULTS_FILE.read_text())


def render(**overrides) -> str:
    """Render the backup script the way Ansible would, with the role defaults."""
    variables = {**defaults(), **overrides}
    env = Environment(keep_trailing_newline=True)
    source = (TEMPLATES / "istota-backup.sh.j2").read_text()
    return env.from_string(source).render(**variables)


def _sqlite_with_a_row(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
        conn.execute("INSERT INTO t (v) VALUES ('x')")
        conn.commit()
    finally:
        conn.close()


def _aged_backup(path: Path, days_old: float) -> Path:
    """A backup artifact with a backdated mtime.

    Really gzipped: the catch-up pass checks with ``gzip -t`` before promoting a
    file to the durable side, so a placeholder that only looked like a .gz would
    quietly exercise the reject path in tests that are about something else.
    """
    import gzip

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(path.name.encode()))
    when = time.time() - days_old * DAY
    os.utime(path, (when, when))
    return path


class Fixture:
    """A rendered script plus the tree it runs against."""

    def __init__(self, root: Path, mounted: bool):
        self.root = root
        self.home = root / "srv" / "app" / "istota"
        self.db = self.home / "data" / "istota.db"
        self.local = root / "backups"
        self.mount = root / "mount"
        self.remote = self.mount / "Backups"
        self.alert_file = root / "alert.txt"
        self.bin = root / "fakebin"

        for d in (
            self.local / "daily",
            self.local / "weekly",
            self.remote / "db" / "daily",
            self.remote / "db" / "weekly",
            self.bin,
        ):
            d.mkdir(parents=True, exist_ok=True)

        # `mountpoint -q <path>` succeeds only when the marker exists, so a test
        # chooses whether the mount is up without touching the script.
        stub = self.bin / "mountpoint"
        stub.write_text('#!/bin/sh\n[ -e "$2/.is-mounted" ]\n')
        stub.chmod(0o755)
        if mounted:
            (self.mount / ".is-mounted").touch()

        self.script = root / "istota-backup.sh"

    def write_script(self, **overrides):
        # `env | grep` rather than an echo of the message: the alert text
        # contains "(s)", which the shell would try to parse.
        alert = f"env | grep ISTOTA_BACKUP_ALERT_MESSAGE > {self.alert_file}"
        rendered = render(
            istota_namespace="istota",
            istota_home=str(self.home),
            istota_backup_local_dir=str(self.local),
            istota_backup_remote_dir=str(self.remote),
            istota_nextcloud_mount_path=str(self.mount),
            istota_backup_alert_command=alert,
            **overrides,
        )
        self.script.write_text(rendered)
        self.script.chmod(0o755)
        return rendered

    def run(self, mode: str = "db") -> subprocess.CompletedProcess:
        env = {**os.environ, "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"}
        return subprocess.run(
            ["bash", str(self.script), mode],
            capture_output=True,
            text=True,
            env=env,
        )

    def local_daily(self) -> set[str]:
        return {p.name for p in (self.local / "daily").iterdir()}

    def remote_daily(self) -> set[str]:
        return {p.name for p in (self.remote / "db" / "daily").iterdir()}


@pytest.fixture
def mounted(tmp_path):
    fx = Fixture(tmp_path, mounted=True)
    _sqlite_with_a_row(fx.db)
    fx.write_script()
    return fx


@pytest.fixture
def unmounted(tmp_path):
    fx = Fixture(tmp_path, mounted=False)
    _sqlite_with_a_row(fx.db)
    fx.write_script()
    return fx


# ---------------------------------------------------------------------------
# Ownership: the framework DB, and nothing else
# ---------------------------------------------------------------------------


class TestOwnership:
    def test_backs_up_the_framework_db(self, mounted):
        result = mounted.run()
        assert result.returncode == 0, result.stdout
        assert len([n for n in mounted.local_daily() if n.startswith("istota-")]) == 1

    def test_does_not_scan_the_mount_for_module_dbs(self, mounted):
        """The retired layout. A module DB sitting exactly where the old finder
        looked must not produce a backup — db_backup.py owns those now, and two
        systems half-covering the same files is how this went unnoticed."""
        _sqlite_with_a_row(
            mounted.mount / "Users" / "alice" / "istota" / "feeds" / "data" / "feeds.db"
        )

        result = mounted.run()

        assert result.returncode == 0, result.stdout
        assert not [n for n in mounted.local_daily() if n.startswith("alice-")]

    def test_the_hand_maintained_module_list_is_gone(self):
        """A bash array of module names cannot be kept in step with
        ``modules.MODULE_NAMES`` and nothing fails when the two disagree — it
        had already lost ``briefings``."""
        rendered = render()
        assert "MODULE_NAMES" not in rendered
        assert "_discover_module_dbs" not in rendered


# ---------------------------------------------------------------------------
# Rotation: one sweep per run, independent of any source DB still existing
# ---------------------------------------------------------------------------


class TestRotation:
    def test_prunes_backups_whose_source_db_no_longer_exists(self, mounted):
        """The 375M. These prefixes stopped being discovered in July; rotation
        lived behind the per-DB early return, so they were never pruned again."""
        stranded = _aged_backup(mounted.local / "daily" / "alice-feeds-old.db.gz", 30)
        stranded_weekly = _aged_backup(
            mounted.local / "weekly" / "alice-feeds-old.db.gz", 30
        )

        mounted.run()

        assert not stranded.exists()
        assert not stranded_weekly.exists()

    def test_prunes_the_remote_copies_too(self, mounted):
        stranded = _aged_backup(
            mounted.remote / "db" / "daily" / "alice-feeds-old.db.gz", 40
        )
        stranded_weekly = _aged_backup(
            mounted.remote / "db" / "weekly" / "alice-feeds-old.db.gz", 40
        )

        mounted.run()

        assert not stranded.exists()
        assert not stranded_weekly.exists()

    def test_keeps_backups_inside_the_window(self, mounted):
        """A sweep that deletes everything would also pass the test above."""
        fresh = _aged_backup(mounted.local / "daily" / "alice-feeds-fresh.db.gz", 1)
        fresh_weekly = _aged_backup(mounted.local / "weekly" / "alice-feeds-fresh.db.gz", 1)

        mounted.run()

        assert fresh.exists()
        assert fresh_weekly.exists()

    def test_local_and_remote_keep_separate_windows(self, mounted):
        """Remote is the off-host tail and outlives the local working set. A
        file older than the local window but inside the remote one survives on
        the mount.

        Uses a retired prefix so the newest-own-backups floor doesn't answer for
        the sweep — that floor is tested on its own below.
        """
        d = defaults()
        assert d["istota_backup_db_daily_retention"] < d["istota_backup_db_daily_retention_remote"]
        age = (
            d["istota_backup_db_daily_retention"] + d["istota_backup_db_daily_retention_remote"]
        ) / 2
        local = _aged_backup(mounted.local / "daily" / "alice-feeds-midwindow.db.gz", age)
        remote = _aged_backup(
            mounted.remote / "db" / "daily" / "alice-feeds-midwindow.db.gz", age
        )

        mounted.run()

        assert not local.exists()
        assert remote.exists()

    def test_runs_even_when_the_backup_itself_failed(self, mounted):
        """Retention that only prunes items that still succeed is backwards:
        the reclaim is needed most in exactly the runs that are failing."""
        mounted.db.unlink()
        stranded = _aged_backup(mounted.local / "daily" / "alice-feeds-old.db.gz", 30)

        result = mounted.run()

        assert result.returncode != 0
        assert not stranded.exists()

    def test_leaves_unrelated_files_alone(self, mounted):
        """The sweep is bounded to the artifacts the script writes."""
        note = mounted.local / "daily" / "README.txt"
        note.write_text("hands off")
        os.utime(note, (time.time() - 90 * DAY, time.time() - 90 * DAY))

        mounted.run()

        assert note.exists()


# ---------------------------------------------------------------------------
# A missing source DB is a failure, not a shrug
# ---------------------------------------------------------------------------


class TestMissingSource:
    def test_fails_the_run(self, mounted):
        mounted.db.unlink()
        result = mounted.run()
        assert result.returncode != 0

    def test_fires_the_alert(self, mounted):
        mounted.db.unlink()
        mounted.run()
        assert mounted.alert_file.exists(), "a backup that covered nothing must alert"

    def test_says_which_db_in_the_log(self, mounted):
        mounted.db.unlink()
        result = mounted.run()
        assert str(mounted.db) in result.stdout


# ---------------------------------------------------------------------------
# Remote catch-up: a snapshot taken while the mount was down still gets up
# ---------------------------------------------------------------------------


class TestRemoteCatchUp:
    def test_copies_local_backups_the_mount_missed(self, mounted):
        """Without this the local window is the entire recovery margin for a
        mount outage: the remote copy is gated on the mount being up and there
        was no second chance."""
        missed = _aged_backup(mounted.local / "daily" / "istota-missed.db.gz", 2)
        missed_weekly = _aged_backup(mounted.local / "weekly" / "istota-missed.db.gz", 2)

        mounted.run()

        assert (mounted.remote / "db" / "daily" / missed.name).exists()
        assert (mounted.remote / "db" / "weekly" / missed_weekly.name).exists()

    def test_does_not_overwrite_what_is_already_there(self, mounted):
        local = _aged_backup(mounted.local / "daily" / "istota-present.db.gz", 2)
        local.write_bytes(b"local-copy")
        remote = mounted.remote / "db" / "daily" / "istota-present.db.gz"
        remote.write_bytes(b"remote-copy")

        mounted.run()

        assert remote.read_bytes() == b"remote-copy"

    def test_does_not_resurrect_a_file_past_the_remote_window(self, mounted):
        """Catch-up runs before the sweep, so an ancient local leftover must not
        be copied up and kept: the copy carries its original mtime, so the sweep
        that follows still sees it as expired. A plain ``cp`` would restart the
        clock and give it a full fresh window on the durable side."""
        _aged_backup(mounted.local / "daily" / "alice-feeds-ancient.db.gz", 60)

        mounted.run()

        assert not (mounted.remote / "db" / "daily" / "alice-feeds-ancient.db.gz").exists()

    def test_is_skipped_when_the_mount_is_down(self, unmounted):
        _aged_backup(unmounted.local / "daily" / "istota-missed.db.gz", 2)
        stranded = _aged_backup(
            unmounted.remote / "db" / "daily" / "alice-feeds-old.db.gz", 40
        )

        result = unmounted.run()

        assert result.returncode == 0, result.stdout
        assert not (unmounted.remote / "db" / "daily" / "istota-missed.db.gz").exists()
        assert stranded.exists(), "an unreachable mount must not be pruned"


# ---------------------------------------------------------------------------
# Whole-run behaviour on a healthy host
# ---------------------------------------------------------------------------


class TestHealthyRun:
    def test_writes_local_and_remote_copies_of_todays_backup(self, mounted):
        result = mounted.run()

        assert result.returncode == 0, result.stdout
        (name,) = [n for n in mounted.local_daily() if n.startswith("istota-")]
        assert name in mounted.remote_daily()

    def test_the_backup_is_a_readable_database(self, mounted, tmp_path):
        import gzip

        mounted.run()

        (name,) = [n for n in mounted.local_daily() if n.startswith("istota-")]
        restored = tmp_path / "restored.db"
        restored.write_bytes(gzip.decompress((mounted.local / "daily" / name).read_bytes()))
        conn = sqlite3.connect(restored)
        try:
            assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 1
        finally:
            conn.close()

    def test_leaves_no_temp_files_behind(self, mounted):
        mounted.run()
        assert not [p for p in mounted.local.iterdir() if p.is_file()]


# ---------------------------------------------------------------------------
# Review-driven hardening
# ---------------------------------------------------------------------------


class TestRetentionValues:
    """`find -mtime +N` with a bad N is destructive in one direction and fatal
    in the other, and the value comes from operator-set inventory.

    An empty variable is the dangerous one: bash arithmetic reads it as 0, so
    `-mtime +$((WEEKLY_RETENTION * 7))` becomes `-mtime +0` and the weekly sweep
    deletes everything over a day old on both sides. A non-numeric one exits the
    shell mid-sweep under `set -u`, which `|| true` does not contain.
    """

    def test_empty_window_deletes_nothing(self, mounted):
        keep = _aged_backup(mounted.local / "weekly" / "istota-old.db.gz", 30)
        mounted.write_script(istota_backup_db_weekly_retention="")

        result = mounted.run()

        assert keep.exists(), "an unusable retention window must delete nothing"
        assert result.returncode != 0

    def test_non_numeric_window_deletes_nothing(self, mounted):
        keep = _aged_backup(mounted.local / "daily" / "istota-old.db.gz", 30)
        mounted.write_script(istota_backup_db_daily_retention="seven")

        result = mounted.run()

        assert keep.exists()
        assert result.returncode != 0

    def test_says_which_variable_is_wrong(self, mounted):
        mounted.write_script(istota_backup_db_daily_retention_remote="")
        result = mounted.run()
        assert "DAILY_RETENTION_REMOTE" in result.stdout


class TestOwnPrefixFloor:
    """The sweep is unfloored for retired prefixes and floored for the one this
    script still writes.

    Rotation now runs on failing runs too, so without a floor a framework DB
    that stays missing for longer than the window would have every copy of it
    swept away — by the retention that exists to protect it. db_backup.py takes
    the same position on the same DB.
    """

    def test_keeps_the_newest_own_backups_through_a_sustained_failure(self, mounted):
        recent = _aged_backup(mounted.local / "daily" / "istota-2026-08-19.db.gz", 30)
        older = _aged_backup(mounted.local / "daily" / "istota-2026-08-18.db.gz", 31)
        oldest = _aged_backup(mounted.local / "daily" / "istota-2026-08-17.db.gz", 32)
        mounted.db.unlink()

        result = mounted.run()

        assert result.returncode != 0
        assert recent.exists() and older.exists(), "swept the last copies of the DB it protects"
        assert not oldest.exists(), "the floor is a floor, not a second retention policy"

    def test_the_floor_does_not_cover_a_retired_prefix(self, mounted):
        """The 375M reclaim. A per-prefix floor would have kept these forever,
        which is why the sweep is unfloored for everything but the live prefix."""
        stranded = _aged_backup(mounted.local / "daily" / "alice-feeds-old.db.gz", 30)
        mounted.db.unlink()

        mounted.run()

        assert not stranded.exists()

    def test_the_floor_applies_on_the_remote_too(self, mounted):
        recent = _aged_backup(mounted.remote / "db" / "daily" / "istota-2026-08-19.db.gz", 40)
        mounted.db.unlink()

        mounted.run()

        assert recent.exists()


class TestCatchUpIntegrity:
    def test_does_not_promote_a_truncated_local_file(self, mounted):
        """A run killed mid-gzip leaves a partial .db.gz that no later run
        cleans up. Copying it to the durable side would make it the copy you
        find when you go looking."""
        bad = mounted.local / "daily" / "istota-truncated.db.gz"
        bad.write_bytes(b"\x1f\x8b\x08\x00truncated-garbage")
        os.utime(bad, (time.time() - DAY, time.time() - DAY))

        result = mounted.run()

        assert not (mounted.remote / "db" / "daily" / bad.name).exists()
        assert "not a valid gzip" in result.stdout

    def test_leaves_no_part_files_behind(self, mounted):
        import gzip

        good = mounted.local / "daily" / "istota-good.db.gz"
        good.write_bytes(gzip.compress(b"anything"))
        os.utime(good, (time.time() - DAY, time.time() - DAY))

        mounted.run()

        assert (mounted.remote / "db" / "daily" / good.name).exists()
        assert not [p for p in (mounted.remote / "db" / "daily").iterdir()
                    if p.name.endswith(".part")]

    def test_creates_the_remote_tier_directories(self, mounted):
        """An Ansible run that landed while the mount was down leaves these
        shadowed once rclone mounts over the top."""
        import gzip

        shutil.rmtree(mounted.remote / "db")
        good = mounted.local / "daily" / "istota-good.db.gz"
        good.write_bytes(gzip.compress(b"anything"))

        result = mounted.run()

        assert result.returncode == 0, result.stdout
        assert (mounted.remote / "db" / "daily" / good.name).exists()


class TestTempFileSweep:
    def test_removes_an_orphaned_working_copy(self, mounted):
        """mktemp writes an uncompressed copy of the source DB into LOCAL_DIR
        itself. Every rm of it is on a path where a command returned an error,
        so a SIGKILL or a reboot mid-backup leaves a full-size file that nothing
        removed — on the disk-pressure issue that motivated all of this."""
        orphan = mounted.local / "istota-backup-AbCdEf.db"
        orphan.write_bytes(b"x" * 4096)
        os.utime(orphan, (time.time() - DAY, time.time() - DAY))

        mounted.run()

        assert not orphan.exists()

    def test_leaves_a_working_copy_from_a_concurrent_run(self, mounted):
        """The backup cron runs every 6h and a large DB takes a while. A sweep
        that deleted a temp file out from under a running backup would break the
        thing it is tidying up after."""
        live = mounted.local / "istota-backup-ZyXwVu.db"
        live.write_bytes(b"x" * 4096)

        mounted.run()

        assert live.exists()
