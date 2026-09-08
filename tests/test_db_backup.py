"""Tests for the local-DB -> mount snapshot backup."""

from __future__ import annotations

import sqlite3
import stat
import sys
from datetime import date as _date
from datetime import timedelta
from pathlib import Path


from istota import db, db_backup
from istota.config import (
    Config,
    EmailConfig,
    NextcloudConfig,
    SchedulerConfig,
    TalkConfig,
    UserConfig,
)

FIXED_DAY = "2026-07-12"


def _config(tmp_path: Path, *, nextcloud_url: str = "", **sched) -> Config:
    mount = tmp_path / "mount"
    mount.mkdir(exist_ok=True)
    # Default to an explicit db_backup_dir *outside* the mount so integration
    # tests exercise the "operator-designated durable target" path and don't trip
    # the mount-liveness guard (a tmp dir isn't a real OS mountpoint). Pass
    # db_backup_dir="" to exercise the mount-derived default resolution.
    sched.setdefault("db_backup_dir", str(tmp_path / "backups"))
    # The mount-liveness gate only applies where a Nextcloud backs the workspace
    # (`storage_is_nextcloud`); a URL-less config is a standalone install whose
    # `nextcloud_mount_path` is a plain directory. TestMountLiveness passes one.
    return Config(
        db_path=tmp_path / "istota.db",
        nextcloud=NextcloudConfig(url=nextcloud_url),
        talk=TalkConfig(),
        email=EmailConfig(),
        scheduler=SchedulerConfig(**sched),
        nextcloud_mount_path=mount,
        module_data_dir=tmp_path / "local",
        users={"alice": UserConfig()},
    )


def _seed_module_db(cfg: Config, user: str, module: str) -> Path:
    from istota.location.db import init_db  # any module init works for the shape

    path = cfg.module_db_path(user, module)
    init_db(path)
    return path


def _add_place(path: Path) -> None:
    """Insert one data row into a module DB so it has non-zero data rows."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO places (name, lat, lon) VALUES ('home', 1.0, 2.0)"
        )
        conn.commit()
    finally:
        conn.close()


def _root(tmp_path: Path) -> Path:
    # Matches the explicit db_backup_dir the _config helper sets by default.
    return tmp_path / "backups"


class TestBackupDestination:
    def test_defaults_under_mount(self, tmp_path):
        cfg = _config(tmp_path, db_backup_dir="")
        assert db_backup.backup_destination(cfg) == (tmp_path / "mount" / "istota-db-backups")

    def test_explicit_dir_wins(self, tmp_path):
        cfg = _config(tmp_path, db_backup_dir=str(tmp_path / "elsewhere"))
        assert db_backup.backup_destination(cfg) == (tmp_path / "elsewhere")

    def test_none_without_mount_or_dir(self, tmp_path):
        cfg = _config(tmp_path, db_backup_dir="")
        cfg.nextcloud_mount_path = None
        assert db_backup.backup_destination(cfg) is None


class TestBackupDatabases:
    def test_snapshots_framework_and_module_dbs(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        _seed_module_db(cfg, "alice", "location")

        results = db_backup.backup_databases(cfg, today=FIXED_DAY)
        by_label = {r["label"]: r["status"] for r in results}

        assert by_label["framework"] == "ok"
        assert by_label["location:alice"] == "ok"
        # A module with no local DB is skipped, not errored.
        assert by_label["money:alice"] == "skip_missing"

        dated = _root(tmp_path) / FIXED_DAY
        assert (dated / "framework" / "istota.db").exists()
        assert (dated / "alice" / "location.db").exists()

    def test_snapshot_is_readable_and_has_data(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        with db.get_db(cfg.db_path) as conn:
            db.create_task(conn, prompt="hi", user_id="alice")

        db_backup.backup_databases(cfg, today=FIXED_DAY)

        snap = _root(tmp_path) / FIXED_DAY / "framework" / "istota.db"
        conn = sqlite3.connect(snap)
        try:
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        finally:
            conn.close()

    def test_disabled_is_noop(self, tmp_path):
        cfg = _config(tmp_path, db_backup_enabled=False)
        db.init_db(cfg.db_path)
        assert db_backup.backup_databases(cfg, today=FIXED_DAY) == []
        assert not _root(tmp_path).exists()

    def test_no_destination_is_noop(self, tmp_path):
        cfg = _config(tmp_path, db_backup_dir="")
        cfg.nextcloud_mount_path = None
        db.init_db(cfg.db_path)
        assert db_backup.backup_databases(cfg, today=FIXED_DAY) == []

    def test_defaults_to_real_date_when_today_none(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        db_backup.backup_databases(cfg)
        # Exactly one dated dir was created (today's real date).
        dated = [p for p in _root(tmp_path).iterdir() if p.is_dir()]
        assert len(dated) == 1
        assert (dated[0] / "framework" / "istota.db").exists()


class TestBackupClockPersistence:
    """The daily-backup clock must survive scheduler restarts (else frequent
    deploys defer it forever). last_backup_time reads a persisted timestamp;
    backup_databases writes it after a real attempt."""

    def test_last_backup_time_zero_when_never_run(self, tmp_path):
        cfg = _config(tmp_path)
        assert db_backup.last_backup_time(cfg) == 0.0

    def test_backup_persists_last_run(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        before = db_backup.last_backup_time(cfg)
        db_backup.backup_databases(cfg, today=FIXED_DAY)
        after = db_backup.last_backup_time(cfg)
        assert before == 0.0
        assert after > 0.0  # a real timestamp was written

    def test_noop_does_not_persist_last_run(self, tmp_path):
        cfg = _config(tmp_path, db_backup_dir="")
        cfg.nextcloud_mount_path = None
        db.init_db(cfg.db_path)
        db_backup.backup_databases(cfg, today=FIXED_DAY)
        assert db_backup.last_backup_time(cfg) == 0.0

    def test_disabled_does_not_persist_last_run(self, tmp_path):
        cfg = _config(tmp_path, db_backup_enabled=False)
        db.init_db(cfg.db_path)
        db_backup.backup_databases(cfg, today=FIXED_DAY)
        assert db_backup.last_backup_time(cfg) == 0.0


class TestColdCopyIsDeleteMode:
    def test_snapshot_is_delete_journal_mode(self, tmp_path):
        # The cold copy on the mount must not carry a WAL header (its -shm would
        # SIGBUS on FUSE if ever opened in place).
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        db_backup.backup_databases(cfg, today=FIXED_DAY)
        snap = _root(tmp_path) / FIXED_DAY / "framework" / "istota.db"
        conn = sqlite3.connect(snap)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        finally:
            conn.close()


class TestDatedSnapshotsAndRetention:
    def test_each_run_writes_its_own_dated_dir(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        db_backup.backup_databases(cfg, today="2026-07-10")
        db_backup.backup_databases(cfg, today="2026-07-11")
        dates = sorted(p.name for p in _root(tmp_path).iterdir() if p.is_dir())
        assert dates == ["2026-07-10", "2026-07-11"]

    def test_retention_prunes_oldest_beyond_keep(self, tmp_path):
        cfg = _config(tmp_path, db_backup_retention=2)
        db.init_db(cfg.db_path)
        for day in ("2026-07-08", "2026-07-09", "2026-07-10", "2026-07-11"):
            db_backup.backup_databases(cfg, today=day)
        dates = sorted(p.name for p in _root(tmp_path).iterdir() if p.is_dir())
        # Only the 2 newest remain.
        assert dates == ["2026-07-10", "2026-07-11"]

    def test_retention_zero_disables_pruning(self, tmp_path):
        cfg = _config(tmp_path, db_backup_retention=0)
        db.init_db(cfg.db_path)
        for day in ("2026-07-08", "2026-07-09", "2026-07-10"):
            db_backup.backup_databases(cfg, today=day)
        dates = {p.name for p in _root(tmp_path).iterdir() if p.is_dir()}
        assert dates == {"2026-07-08", "2026-07-09", "2026-07-10"}

    def test_same_day_rerun_refreshes_today(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        db_backup.backup_databases(cfg, today=FIXED_DAY)
        db_backup.backup_databases(cfg, today=FIXED_DAY)
        dated = [p for p in _root(tmp_path).iterdir() if p.is_dir()]
        assert len(dated) == 1


class TestCollapseGuard:
    def test_emptied_module_db_marked_suspect(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        path = _seed_module_db(cfg, "alice", "location")
        _add_place(path)  # prior snapshot has data

        db_backup.backup_databases(cfg, today="2026-07-11")

        # Now the live DB is emptied (the ISSUE-156 empty-shadow scenario).
        conn = sqlite3.connect(path)
        conn.execute("DELETE FROM places")
        conn.commit()
        conn.close()

        results = db_backup.backup_databases(cfg, today="2026-07-12")
        loc = next(r for r in results if r["label"] == "location:alice")
        assert loc["status"] == "suspect"
        assert loc["prior_rows"] == 1
        assert loc["new_rows"] == 0

        dated = _root(tmp_path) / "2026-07-12" / "alice"
        # The fresh empty snapshot is quarantined, not treated as latest-good.
        assert (dated / "location.db.suspect").exists()
        assert not (dated / "location.db").exists()

    def test_prior_good_snapshot_survives_collapse(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        path = _seed_module_db(cfg, "alice", "location")
        _add_place(path)
        db_backup.backup_databases(cfg, today="2026-07-11")

        conn = sqlite3.connect(path)
        conn.execute("DELETE FROM places")
        conn.commit()
        conn.close()
        db_backup.backup_databases(cfg, today="2026-07-12")

        good = _root(tmp_path) / "2026-07-11" / "alice" / "location.db"
        assert good.exists()
        conn = sqlite3.connect(good)
        try:
            assert conn.execute("SELECT COUNT(*) FROM places").fetchone()[0] == 1
        finally:
            conn.close()

    def test_zero_to_zero_is_not_suspect(self, tmp_path):
        # A DB that was legitimately empty and stays empty is fine, not suspect.
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        _seed_module_db(cfg, "alice", "location")  # 0 rows
        db_backup.backup_databases(cfg, today="2026-07-11")
        results = db_backup.backup_databases(cfg, today="2026-07-12")
        loc = next(r for r in results if r["label"] == "location:alice")
        assert loc["status"] == "ok"

    def test_first_ever_snapshot_never_suspect(self, tmp_path):
        # No prior snapshot to compare against -> can't be a collapse.
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        _seed_module_db(cfg, "alice", "location")  # 0 rows, no prior
        results = db_backup.backup_databases(cfg, today="2026-07-12")
        loc = next(r for r in results if r["label"] == "location:alice")
        assert loc["status"] == "ok"


class TestRetentionProtectsNewestGood:
    def test_prune_keeps_dir_holding_newest_good_copy(self, tmp_path):
        # keep=1, but the newest dir's copy is suspect -> the older good dir must
        # be protected from pruning so we don't lose the last good copy.
        cfg = _config(tmp_path, db_backup_retention=1)
        db.init_db(cfg.db_path)
        path = _seed_module_db(cfg, "alice", "location")
        _add_place(path)
        db_backup.backup_databases(cfg, today="2026-07-11")  # good

        conn = sqlite3.connect(path)
        conn.execute("DELETE FROM places")
        conn.commit()
        conn.close()
        db_backup.backup_databases(cfg, today="2026-07-12")  # collapse -> suspect

        # The 07-11 dir holds the newest *good* location copy; keep it.
        assert (_root(tmp_path) / "2026-07-11" / "alice" / "location.db").exists()


class TestBackupPermissions:
    def test_backup_tree_and_files_are_locked_down(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        db_backup.backup_databases(cfg, today=FIXED_DAY)

        dated = _root(tmp_path) / FIXED_DAY
        snap = dated / "framework" / "istota.db"
        # Dir 0700, file 0600 -> no group/other bits.
        assert stat.S_IMODE(dated.stat().st_mode) == 0o700
        assert stat.S_IMODE(snap.stat().st_mode) == 0o600


class TestMountLiveness:
    """A destination under the Nextcloud mount must not be written when the mount
    is down — otherwise the 'backup' lands on local disk under a stale mountpoint
    and silently vanishes when the mount returns (Mulder #1).

    Durability follows the resolved destination, not the config branch that
    produced it (ISSUE-480): the mount-derived default and an explicit
    ``db_backup_dir`` under the mount carry the same exposure and get the same
    gate. A destination outside the mount stays the operator's claim.
    """

    NC = "https://cloud.example.com"

    def test_mount_derived_skips_when_not_mounted(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path, db_backup_dir="", nextcloud_url=self.NC)
        db.init_db(cfg.db_path)
        monkeypatch.setattr(db_backup.os.path, "ismount", lambda p: False)

        results = db_backup.backup_databases(cfg, today=FIXED_DAY)
        assert results == []
        # Nothing written, clock not advanced.
        assert not (tmp_path / "mount" / "istota-db-backups").exists()
        assert db_backup.last_backup_time(cfg) == 0.0

    def test_mount_derived_runs_when_mounted(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path, db_backup_dir="", nextcloud_url=self.NC)
        db.init_db(cfg.db_path)
        monkeypatch.setattr(db_backup.os.path, "ismount", lambda p: True)

        results = db_backup.backup_databases(cfg, today=FIXED_DAY)
        assert any(r["status"] == "ok" for r in results)
        assert (tmp_path / "mount" / "istota-db-backups" / FIXED_DAY / "framework" / "istota.db").exists()

    def test_explicit_dir_outside_mount_is_trusted_without_ismount(self, tmp_path, monkeypatch):
        # A db_backup_dir the mount does not contain is the operator's claim
        # about a filesystem this module cannot check; don't ismount-gate it.
        cfg = _config(tmp_path, nextcloud_url=self.NC)  # explicit dir outside the mount
        db.init_db(cfg.db_path)
        monkeypatch.setattr(db_backup.os.path, "ismount", lambda p: False)
        results = db_backup.backup_databases(cfg, today=FIXED_DAY)
        assert any(r["status"] == "ok" for r in results)

    def test_explicit_dir_under_mount_skips_when_not_mounted(self, tmp_path, monkeypatch):
        # ISSUE-480: the same filesystem one directory over used to be trusted
        # purely because the operator had spelled it out.
        cfg = _config(
            tmp_path,
            db_backup_dir=str(tmp_path / "mount" / "Backups" / "snapshots"),
            nextcloud_url=self.NC,
        )
        db.init_db(cfg.db_path)
        monkeypatch.setattr(db_backup.os.path, "ismount", lambda p: False)

        results = db_backup.backup_databases(cfg, today=FIXED_DAY)
        assert results == []
        assert not (tmp_path / "mount" / "Backups").exists()
        assert db_backup.last_backup_time(cfg) == 0.0

    def test_explicit_dir_under_mount_runs_when_mounted(self, tmp_path, monkeypatch):
        cfg = _config(
            tmp_path,
            db_backup_dir=str(tmp_path / "mount" / "Backups" / "snapshots"),
            nextcloud_url=self.NC,
        )
        db.init_db(cfg.db_path)
        monkeypatch.setattr(db_backup.os.path, "ismount", lambda p: True)

        results = db_backup.backup_databases(cfg, today=FIXED_DAY)
        assert any(r["status"] == "ok" for r in results)
        snap = tmp_path / "mount" / "Backups" / "snapshots" / FIXED_DAY / "framework" / "istota.db"
        assert snap.exists()

    def test_symlinked_explicit_dir_into_mount_is_gated(self, tmp_path, monkeypatch):
        # Containment is the resolved-path comparison, so a symlink landing
        # inside the mount is caught even though the spelling is outside it.
        (tmp_path / "mount" / "Backups").mkdir(parents=True)
        link = tmp_path / "backups-link"
        link.symlink_to(tmp_path / "mount" / "Backups")
        cfg = _config(tmp_path, db_backup_dir=str(link), nextcloud_url=self.NC)
        db.init_db(cfg.db_path)
        monkeypatch.setattr(db_backup.os.path, "ismount", lambda p: False)

        assert db_backup.backup_databases(cfg, today=FIXED_DAY) == []

    def _symlinked_mount(self, tmp_path):
        """Config whose nextcloud_mount_path is a symlink to the real mount."""
        real_mount = tmp_path / "real-mount"
        (real_mount / "Backups").mkdir(parents=True)
        link_mount = tmp_path / "linked-mount"
        link_mount.symlink_to(real_mount)
        cfg = _config(
            tmp_path,
            db_backup_dir=str(real_mount / "Backups"),
            nextcloud_url=self.NC,
        )
        cfg.nextcloud_mount_path = link_mount
        db.init_db(cfg.db_path)
        return cfg, real_mount

    def test_symlinked_mount_path_still_catches_destination(self, tmp_path, monkeypatch):
        # Both sides resolve: a mount path that is itself a symlink must not
        # hide a destination spelled through the real directory.
        cfg, _real = self._symlinked_mount(tmp_path)
        monkeypatch.setattr(db_backup.os.path, "ismount", lambda p: False)

        assert db_backup.backup_databases(cfg, today=FIXED_DAY) == []

    def test_symlinked_mount_path_runs_when_the_real_mount_is_up(self, tmp_path, monkeypatch):
        # The negative control for the test above, and the one that fails if the
        # liveness test is handed the unresolved path: `posixpath.ismount`
        # short-circuits to False for a symlink, so asking it about
        # `linked-mount` would report a healthy mount as down forever.
        cfg, real_mount = self._symlinked_mount(tmp_path)
        # Answers truthfully per path, the way the real ismount does.
        monkeypatch.setattr(
            db_backup.os.path, "ismount",
            lambda p: Path(p) == real_mount and not Path(p).is_symlink(),
        )

        results = db_backup.backup_databases(cfg, today=FIXED_DAY)
        assert any(r["status"] == "ok" for r in results)
        assert (real_mount / "Backups" / FIXED_DAY / "framework" / "istota.db").exists()

    def test_unresolvable_destination_keeps_the_gate(self, tmp_path, monkeypatch):
        # "Can't tell" has to mean "still check the mount": the alternative is a
        # snapshot written to local disk under a stale mountpoint.
        cfg = _config(tmp_path, db_backup_dir=str(tmp_path / "somewhere"), nextcloud_url=self.NC)
        db.init_db(cfg.db_path)
        real_resolve = Path.resolve

        def _boom(self, *a, **kw):
            if self == Path(str(tmp_path / "somewhere")):
                raise OSError("simulated dead filesystem")
            return real_resolve(self, *a, **kw)

        monkeypatch.setattr(Path, "resolve", _boom)
        monkeypatch.setattr(db_backup.os.path, "ismount", lambda p: False)

        assert db_backup.backup_databases(cfg, today=FIXED_DAY) == []

    def test_unresolvable_mount_falls_back_to_the_raw_spelling(self, tmp_path, monkeypatch):
        # Both halves must still agree when the mount itself will not resolve.
        cfg = _config(
            tmp_path,
            db_backup_dir=str(tmp_path / "mount" / "Backups"),
            nextcloud_url=self.NC,
        )
        db.init_db(cfg.db_path)
        real_resolve = Path.resolve

        def _boom(self, *a, **kw):
            if self == Path(str(tmp_path / "mount")):
                raise OSError("simulated dead filesystem")
            return real_resolve(self, *a, **kw)

        monkeypatch.setattr(Path, "resolve", _boom)
        monkeypatch.setattr(db_backup.os.path, "ismount", lambda p: False)

        assert db_backup.backup_databases(cfg, today=FIXED_DAY) == []

    def test_textual_prefix_sibling_of_mount_is_trusted(self, tmp_path, monkeypatch):
        # ``<tmp>/mountains`` shares a prefix with ``<tmp>/mount`` and is not
        # under it; a string-prefix containment test would gate it wrongly.
        cfg = _config(tmp_path, db_backup_dir=str(tmp_path / "mountains"), nextcloud_url=self.NC)
        db.init_db(cfg.db_path)
        monkeypatch.setattr(db_backup.os.path, "ismount", lambda p: False)

        results = db_backup.backup_databases(cfg, today=FIXED_DAY)
        assert any(r["status"] == "ok" for r in results)

    def test_explicit_dir_equal_to_mount_root_is_gated(self, tmp_path, monkeypatch):
        # The mount root itself counts as within the mount.
        cfg = _config(tmp_path, db_backup_dir=str(tmp_path / "mount"), nextcloud_url=self.NC)
        db.init_db(cfg.db_path)
        monkeypatch.setattr(db_backup.os.path, "ismount", lambda p: False)

        assert db_backup.backup_databases(cfg, today=FIXED_DAY) == []


class TestStandaloneInstallIsNotGated:
    """A standalone install points ``nextcloud_mount_path`` at its plain local
    workspace, which is never a mountpoint. Gating on the resolved path alone
    would refuse every backup there — including the one ``istota setup`` writes,
    which lives inside that workspace by design."""

    def _standalone(self, tmp_path, **kw):
        # Mirrors setup_wizard.render_config_toml: no Nextcloud URL, mount path
        # is the workspace, backups sit inside it.
        return _config(
            tmp_path,
            db_backup_dir=str(tmp_path / "mount" / "db-backups"),
            **kw,
        )

    def test_backup_dir_inside_workspace_runs_without_ismount(self, tmp_path, monkeypatch):
        cfg = self._standalone(tmp_path)
        assert not cfg.storage_is_nextcloud
        db.init_db(cfg.db_path)
        monkeypatch.setattr(db_backup.os.path, "ismount", lambda p: False)

        results = db_backup.backup_databases(cfg, today=FIXED_DAY)
        assert any(r["status"] == "ok" for r in results)
        assert (tmp_path / "mount" / "db-backups" / FIXED_DAY / "framework" / "istota.db").exists()

    def test_derived_destination_runs_without_ismount(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path, db_backup_dir="")  # no URL: standalone
        db.init_db(cfg.db_path)
        monkeypatch.setattr(db_backup.os.path, "ismount", lambda p: False)

        results = db_backup.backup_databases(cfg, today=FIXED_DAY)
        assert any(r["status"] == "ok" for r in results)

    def test_adding_a_nextcloud_url_arms_the_gate(self, tmp_path, monkeypatch):
        # The accepted consequence of keying the gate on `storage_is_nextcloud`:
        # the same layout with a URL is a Nextcloud deployment as far as this
        # module can tell, so its ordinary directory gets ismount-tested and the
        # run is refused. Documented in docs/configuration/reference.md; pinned
        # here so it is a decision rather than a surprise.
        cfg = self._standalone(tmp_path, nextcloud_url="https://cloud.example.com")
        assert cfg.storage_is_nextcloud
        db.init_db(cfg.db_path)
        monkeypatch.setattr(db_backup.os.path, "ismount", lambda p: False)

        assert db_backup.backup_databases(cfg, today=FIXED_DAY) == []


class TestClockOnlyAdvancesOnOk:
    def test_clock_not_advanced_when_all_error(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)

        def _boom(src, dest, label):
            raise OSError("simulated write failure")

        monkeypatch.setattr(db_backup, "_snapshot_one", _boom)
        results = db_backup.backup_databases(cfg, today=FIXED_DAY)
        assert results and all(r["status"] == "error" for r in results)
        # Clock stays stale so the staleness alert can eventually fire.
        assert db_backup.last_backup_time(cfg) == 0.0

    def test_clock_advances_on_partial_ok(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)  # framework will be ok even if modules are missing
        db_backup.backup_databases(cfg, today=FIXED_DAY)
        assert db_backup.last_backup_time(cfg) > 0.0


class TestForceEntrypoint:
    def test_main_runs_backup_immediately(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        monkeypatch.setattr("istota.config.load_config", lambda: cfg)
        monkeypatch.setattr(sys, "argv", ["istota.db_backup"])
        rc = db_backup.main()
        assert rc == 0
        # A dated dir for today's real date exists (main() ignores the interval).
        dated = [p for p in _root(tmp_path).iterdir() if p.is_dir()]
        assert len(dated) == 1


class TestModuleList:
    def test_modules_come_from_the_registry(self):
        """ISSUE-262: the Ansible backup script kept its own hand-maintained
        bash array of module names and had already lost ``briefings``. This
        tuple was a second copy of the same list, correct by coincidence."""
        from istota.modules import MODULE_NAMES

        assert db_backup.MODULES == tuple(sorted(MODULE_NAMES))


class TestVanishedModuleDb:
    """A module DB that had a good snapshot and now has none.

    ``skip_missing`` is the right answer for a module a user never opened —
    ``money:alice`` has no ``money.db`` and never will. It is the wrong answer
    for a DB that was being snapshotted yesterday and isn't today: that is the
    shape of ISSUE-262 itself (coverage silently shrinking), on the system now
    responsible for module DBs.
    """

    def test_never_existed_stays_skip_missing(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        results = db_backup.backup_databases(cfg, today=FIXED_DAY)
        money = next(r for r in results if r["label"] == "money:alice")
        assert money["status"] == "skip_missing"

    def test_disappeared_source_is_flagged(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        path = _seed_module_db(cfg, "alice", "location")
        _add_place(path)
        db_backup.backup_databases(cfg, today="2026-07-11")

        path.unlink()

        results = db_backup.backup_databases(cfg, today="2026-07-12")
        loc = next(r for r in results if r["label"] == "location:alice")
        assert loc["status"] == "vanished"

    def test_a_suspect_prior_does_not_count_as_coverage(self, tmp_path):
        """``_prior_good_snapshot`` skips quarantined copies, so a DB whose only
        prior snapshot was quarantined and which then disappears reports
        ``skip_missing`` — the alert for it already fired as ``suspect``."""
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        path = _seed_module_db(cfg, "alice", "location")
        _add_place(path)
        db_backup.backup_databases(cfg, today="2026-07-10")

        conn = sqlite3.connect(path)
        conn.execute("DELETE FROM places")
        conn.commit()
        conn.close()
        db_backup.backup_databases(cfg, today="2026-07-11")  # -> suspect

        # Drop the one good copy, leaving only the quarantined one behind.
        (_root(tmp_path) / "2026-07-10" / "alice" / "location.db").unlink()
        path.unlink()

        results = db_backup.backup_databases(cfg, today="2026-07-12")
        loc = next(r for r in results if r["label"] == "location:alice")
        assert loc["status"] == "skip_missing"

    def test_stops_flagging_once_the_evidence_is_old(self, tmp_path):
        """Self-limiting on purpose. ``_prune_old_snapshots`` never prunes the
        dir holding the newest good copy of a DB, so the prior snapshot stays on
        disk forever and an unbounded lookback would alert once per interval for
        the rest of the deployment's life. An alert that repeats forever is one
        an operator learns to ignore."""
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        path = _seed_module_db(cfg, "alice", "location")
        _add_place(path)
        db_backup.backup_databases(cfg, today="2026-07-10")
        path.unlink()

        stale_day = (
            _date.fromisoformat("2026-07-10")
            + timedelta(days=db_backup._VANISHED_LOOKBACK_DAYS + 1)
        ).isoformat()
        results = db_backup.backup_databases(cfg, today=stale_day)

        loc = next(r for r in results if r["label"] == "location:alice")
        assert loc["status"] == "skip_missing"
        # The evidence is still there; it is the flag that ages out, not the copy.
        assert (_root(tmp_path) / "2026-07-10" / "alice" / "location.db").exists()
