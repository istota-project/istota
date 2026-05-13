"""SQLite self-healing helpers.

Per-user module DBs (feeds, health, location, money) live on the
Nextcloud mount, where ungraceful shutdowns and FUSE/network hiccups can
leave SQLite's index pages out of sync with table pages. ``PRAGMA
quick_check`` catches this cheaply; ``REINDEX`` repairs it when the
table itself is still intact (which is the common failure mode we see
in practice — see the deathcults-tumblr ghost-unread incident).

Usage::

    from istota.db_health import check_and_repair

    report = check_and_repair(db_path, label="feeds:stefan")
    # report.ok is True after a clean check or a successful repair;
    # report.issues_after is non-empty only on unrepairable damage.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


logger = logging.getLogger(__name__)


@dataclass
class CheckReport:
    db_path: Path
    label: str
    issues_before: list[str] = field(default_factory=list)
    issues_after: list[str] = field(default_factory=list)
    repair_attempted: bool = False
    repaired: bool = False

    @property
    def ok(self) -> bool:
        return not self.issues_after


def quick_check(conn: sqlite3.Connection) -> list[str]:
    """Return ``PRAGMA quick_check`` findings; empty list means clean.

    ``quick_check`` is a subset of ``integrity_check`` that catches the
    forms of corruption we actually see (index/table mismatches) at
    roughly O(table size) instead of O(table size * index count). A clean
    DB returns a single ``"ok"`` row.
    """
    rows = conn.execute("PRAGMA quick_check").fetchall()
    if len(rows) == 1 and rows[0][0] == "ok":
        return []
    return [row[0] for row in rows]


def reindex(conn: sqlite3.Connection) -> None:
    """Rebuild every index in the database from its underlying table data."""
    conn.execute("REINDEX")
    conn.commit()


def check_and_repair(db_path: Path, *, label: str) -> CheckReport:
    """Run ``quick_check``; if dirty, attempt ``REINDEX`` and re-check.

    Safe against in-use WAL DBs: readers keep going and ``REINDEX``
    grabs the write lock only briefly per index. If ``quick_check``
    still reports issues afterwards, the table itself is suspect and
    the caller should escalate (the report will have ``ok=False`` and
    a non-empty ``issues_after``).

    Missing files are reported as clean (``ok=True``, no issues) so
    callers can blindly enumerate optional per-user paths.
    """
    report = CheckReport(db_path=db_path, label=label)
    if not db_path.exists():
        return report

    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.DatabaseError as exc:
        report.issues_before = [f"open failed: {exc}"]
        report.issues_after = list(report.issues_before)
        logger.error(
            "db_health_open_failed label=%s db=%s err=%s",
            label, db_path, exc,
        )
        return report

    try:
        # sqlite3.connect() is happy with anything (it lazily creates a DB
        # if the file isn't one). A real header check only happens on the
        # first query — so wrap quick_check and treat hard failures as
        # unrepairable.
        try:
            report.issues_before = quick_check(conn)
        except sqlite3.DatabaseError as exc:
            report.issues_before = [f"quick_check failed: {exc}"]
            report.issues_after = list(report.issues_before)
            logger.error(
                "db_health_check_failed label=%s db=%s err=%s",
                label, db_path, exc,
            )
            return report
        if not report.issues_before:
            return report

        logger.warning(
            "db_health_dirty label=%s db=%s issues=%s",
            label, db_path, "; ".join(report.issues_before),
        )
        report.repair_attempted = True
        try:
            reindex(conn)
        except sqlite3.DatabaseError as exc:
            logger.error(
                "db_health_reindex_failed label=%s db=%s err=%s",
                label, db_path, exc,
            )
        report.issues_after = quick_check(conn)
        # ``repaired`` means the repair actually worked — quick_check is
        # clean after the REINDEX. A successful REINDEX call that doesn't
        # clear the issues (table-level damage, not just stale indexes)
        # stays at ``repaired=False`` so callers can escalate.
        report.repaired = report.repair_attempted and not report.issues_after
    finally:
        conn.close()

    if report.ok:
        logger.info(
            "db_health_repaired label=%s db=%s issues_before=%d",
            label, db_path, len(report.issues_before),
        )
    else:
        logger.error(
            "db_health_unrepaired label=%s db=%s issues=%s",
            label, db_path, "; ".join(report.issues_after),
        )
    return report
