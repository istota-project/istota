"""Per-user module DBs live on local disk (off the Nextcloud mount) and use WAL.

Covers the stall fix (ISSUE-156/157 follow-up):
* ``Config.module_db_path`` resolves ``{module_data_dir}/{user}/{module}.db``
  and refuses to sit under ``nextcloud_mount_path`` (WAL -shm SIGBUSes there).
* Each module loader relocates only the ``.db`` — ``data_dir`` / workspace
  (uploads, ledgers, exports) stay on the mount.
* Each module ``init_db`` sets WAL (not DELETE) now that the file is local,
  and converts a relocated DELETE-mode DB to WAL on first touch.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from istota.config import Config, UserConfig


def _config(tmp_path: Path, *, users: dict[str, UserConfig]) -> Config:
    mount = tmp_path / "mount"
    mount.mkdir(exist_ok=True)
    return Config(
        nextcloud_mount_path=mount,
        module_data_dir=tmp_path / "local",
        users=users,
        bot_name="Istota",
        db_path=tmp_path / "istota.db",
    )


def _journal_mode(db_path: Path) -> str:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
    finally:
        conn.close()


class TestModuleDbPath:
    def test_layout(self, tmp_path):
        cfg = _config(tmp_path, users={"alice": UserConfig()})
        assert cfg.module_db_path("alice", "health") == (
            (tmp_path / "local").resolve() / "alice" / "health.db"
        )

    def test_refuses_under_mount(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        cfg = Config(
            nextcloud_mount_path=mount,
            module_data_dir=mount / "modules",  # footgun: under the mount
            users={"alice": UserConfig()},
        )
        with pytest.raises(ValueError, match="local disk"):
            cfg.module_db_path("alice", "feeds")

    def test_allows_local_when_mount_set(self, tmp_path):
        # module_data_dir sibling of the mount, not under it → allowed.
        cfg = _config(tmp_path, users={"alice": UserConfig()})
        path = cfg.module_db_path("alice", "location")
        assert not path.resolve().is_relative_to((tmp_path / "mount").resolve())

    def test_derived_default_tracks_db_path_parent(self, tmp_path):
        # Unset module_data_dir → {db_path.parent}/modules, no under-mount guard
        # even when db_path's dir coincides with the mount (a degenerate layout).
        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path,  # == db_path.parent
            users={"alice": UserConfig()},
        )
        assert cfg.module_data_dir is None
        assert cfg.module_db_path("alice", "feeds") == (
            tmp_path.resolve() / "modules" / "alice" / "feeds.db"
        )


class TestLoadersRelocateOnlyDb:
    """resolve_for_user: db_path is the local module path; data_dir stays
    on the mount."""

    def test_location(self, tmp_path):
        from istota.location import resolve_for_user

        cfg = _config(tmp_path, users={"alice": UserConfig()})
        ctx = resolve_for_user("alice", cfg)
        assert ctx.db_path == cfg.module_db_path("alice", "location")
        assert not ctx.db_path.is_relative_to((tmp_path / "mount").resolve())
        assert ctx.workspace.is_relative_to((tmp_path / "mount").resolve())

    def test_health(self, tmp_path):
        from istota.health import resolve_for_user

        cfg = _config(tmp_path, users={"alice": UserConfig()})
        ctx = resolve_for_user("alice", cfg)
        assert ctx.db_path == cfg.module_db_path("alice", "health")
        # uploads dir (user-facing) stays under the mount
        assert ctx.uploads_dir.is_relative_to((tmp_path / "mount").resolve())

    def test_feeds(self, tmp_path):
        from istota.feeds import resolve_for_user

        cfg = _config(tmp_path, users={"alice": UserConfig()})
        ctx = resolve_for_user("alice", cfg)
        assert ctx.db_path == cfg.module_db_path("alice", "feeds")
        assert ctx.data_dir.is_relative_to((tmp_path / "mount").resolve())

    def test_money(self, tmp_path):
        from istota.money import resolve_for_user

        cfg = _config(tmp_path, users={"alice": UserConfig()})
        ctx = resolve_for_user("alice", cfg)
        assert ctx.db_path == cfg.module_db_path("alice", "money")
        assert ctx.data_dir.is_relative_to((tmp_path / "mount").resolve())


class TestModuleInitDbUsesWal:
    def test_feeds_fresh_is_wal(self, tmp_path):
        from istota.feeds.db import init_db

        db_path = tmp_path / "feeds.db"
        init_db(db_path)
        assert _journal_mode(db_path) == "wal"

    def test_health_fresh_is_wal(self, tmp_path):
        from istota.health.db import init_db

        db_path = tmp_path / "health.db"
        init_db(db_path)
        assert _journal_mode(db_path) == "wal"

    def test_location_fresh_is_wal(self, tmp_path):
        from istota.location.db import init_db

        db_path = tmp_path / "location.db"
        init_db(db_path)
        assert _journal_mode(db_path) == "wal"

    def test_money_fresh_is_wal(self, tmp_path):
        from istota.money.db import init_db

        db_path = tmp_path / "money.db"
        init_db(db_path)
        assert _journal_mode(db_path) == "wal"

    def test_relocated_delete_db_converts_to_wal(self, tmp_path):
        """A DELETE-mode DB copied from the mount converts to WAL on init_db —
        this is how the relocation migration flips the journal for free."""
        from istota.health.db import init_db

        db_path = tmp_path / "health.db"
        # Simulate the old on-mount DELETE-mode file.
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("CREATE TABLE t (x)")
        conn.commit()
        conn.close()
        assert _journal_mode(db_path) == "delete"

        init_db(db_path)
        assert _journal_mode(db_path) == "wal"
