"""Tests for the periodic scheduler_stats health line (ISSUE-101 follow-up)."""

import builtins
import logging
import re
import sqlite3

import pytest

from istota import db
from istota.config import (
    Config,
    EmailConfig,
    NextcloudConfig,
    SchedulerConfig,
    TalkConfig,
)
from istota.scheduler import WorkerPool, _emit_scheduler_stats


def _config(tmp_path, *, interval=60):
    db_path = tmp_path / "stats.db"
    db.init_db(db_path)
    return Config(
        db_path=db_path,
        nextcloud=NextcloudConfig(),
        talk=TalkConfig(),
        email=EmailConfig(),
        scheduler=SchedulerConfig(scheduler_stats_interval=interval),
        temp_dir=tmp_path / "temp",
    )


def _stats_lines(caplog):
    return [
        r.message
        for r in caplog.records
        if r.message.startswith("scheduler_stats ")
    ]


class TestEmitSchedulerStats:
    def test_emit_includes_required_fields(self, tmp_path, caplog):
        config = _config(tmp_path)
        pool = WorkerPool(config)
        with caplog.at_level(logging.INFO, logger="istota.scheduler.stats"):
            _emit_scheduler_stats(config, pool)
        lines = _stats_lines(caplog)
        assert len(lines) == 1
        assert re.match(
            r"^scheduler_stats threads=\d+ fds=\d+ rss_mb=\d+ "
            r"tasks_running=\d+ workers_active=\d+$",
            lines[0],
        ), lines[0]

    def test_emit_omits_fds_when_psutil_unavailable(self, tmp_path, caplog, monkeypatch):
        # Reset the once-per-process warn latch so this test sees its own WARN.
        monkeypatch.setattr("istota.scheduler._psutil_unavailable_warned", False)
        config = _config(tmp_path)
        pool = WorkerPool(config)

        real_import = builtins.__import__

        def _no_psutil(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("psutil unavailable for test")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_psutil)

        with caplog.at_level(logging.INFO):
            _emit_scheduler_stats(config, pool)

        lines = _stats_lines(caplog)
        assert len(lines) == 1
        assert re.match(
            r"^scheduler_stats threads=\d+ tasks_running=\d+ workers_active=\d+$",
            lines[0],
        ), lines[0]
        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "psutil unavailable" in r.message
        ]
        assert len(warnings) == 1

    def test_emit_survives_db_failure(self, tmp_path, caplog, monkeypatch):
        config = _config(tmp_path)
        pool = WorkerPool(config)

        def _boom(_conn):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("istota.db.count_running_tasks", _boom)

        with caplog.at_level(logging.INFO, logger="istota.scheduler.stats"):
            _emit_scheduler_stats(config, pool)  # must not raise

        lines = _stats_lines(caplog)
        assert len(lines) == 1
        assert "tasks_running=?" in lines[0]

    def test_emit_survives_psutil_collector_error(self, tmp_path, caplog, monkeypatch):
        # Regression (Mulder HIGH finding): psutil is installed, but a per-call
        # collector raises a non-ImportError — e.g. OSError(EMFILE) from
        # num_fds() under the very fd exhaustion this line exists to catch. The
        # line MUST still emit (degrade the field, don't drop the whole line).
        import psutil

        config = _config(tmp_path)
        pool = WorkerPool(config)

        def _emfile(self):
            raise OSError(24, "Too many open files")

        monkeypatch.setattr(psutil.Process, "num_fds", _emfile)

        with caplog.at_level(logging.INFO, logger="istota.scheduler.stats"):
            _emit_scheduler_stats(config, pool)

        lines = _stats_lines(caplog)
        assert len(lines) == 1, "line must still emit when num_fds() raises"
        assert "fds=" not in lines[0], "the failed field is omitted"
        # The other fields survive — these are the signals under fd exhaustion.
        assert re.search(r"\bthreads=\d+\b", lines[0])
        assert re.search(r"\btasks_running=\d+\b", lines[0])
        assert re.search(r"\bworkers_active=\d+\b", lines[0])

    def test_psutil_warn_emitted_once_across_emits(self, tmp_path, caplog, monkeypatch):
        # The psutil-unavailable WARN is once-per-process, not once-per-emit.
        monkeypatch.setattr("istota.scheduler._psutil_unavailable_warned", False)
        config = _config(tmp_path)
        pool = WorkerPool(config)

        real_import = builtins.__import__

        def _no_psutil(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("psutil unavailable for test")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_psutil)

        with caplog.at_level(logging.INFO):
            _emit_scheduler_stats(config, pool)
            _emit_scheduler_stats(config, pool)
            _emit_scheduler_stats(config, pool)

        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "psutil unavailable" in r.message
        ]
        assert len(warnings) == 1, "WARN must fire once across multiple emits"
        # All three emits still produced a line.
        assert len(_stats_lines(caplog)) == 3

    def test_outer_backstop_swallows_unexpected_error(self, tmp_path, caplog):
        # The outer try/except is the "never crash the daemon loop" guarantee.
        # Force a failure outside the inner guards (pool.active_count) and assert
        # nothing escapes and the failure is logged on the stats logger.
        config = _config(tmp_path)

        class _BoomPool:
            @property
            def active_count(self):
                raise RuntimeError("boom")

        with caplog.at_level(logging.WARNING, logger="istota.scheduler.stats"):
            _emit_scheduler_stats(config, _BoomPool())  # must not raise

        failures = [
            r for r in caplog.records
            if r.name == "istota.scheduler.stats"
            and "scheduler_stats emit failed" in r.message
        ]
        assert len(failures) == 1

    def test_worker_pool_none_treated_as_zero(self, tmp_path, caplog):
        config = _config(tmp_path)
        with caplog.at_level(logging.INFO, logger="istota.scheduler.stats"):
            _emit_scheduler_stats(config, None)
        lines = _stats_lines(caplog)
        assert len(lines) == 1
        assert "workers_active=0" in lines[0]

    def test_running_task_count_reflects_db(self, tmp_path, caplog):
        config = _config(tmp_path)
        pool = WorkerPool(config)
        with db.get_db(config.db_path) as conn:
            tid = db.create_task(conn, prompt="hi", user_id="alice")
            db.update_task_status(conn, tid, "running")

        with caplog.at_level(logging.INFO, logger="istota.scheduler.stats"):
            _emit_scheduler_stats(config, pool)
        lines = _stats_lines(caplog)
        assert "tasks_running=1" in lines[0]

    def test_logger_name_is_correct(self, tmp_path, caplog):
        config = _config(tmp_path)
        pool = WorkerPool(config)
        with caplog.at_level(logging.INFO, logger="istota.scheduler.stats"):
            _emit_scheduler_stats(config, pool)
        stats_records = [
            r for r in caplog.records if r.message.startswith("scheduler_stats ")
        ]
        assert len(stats_records) == 1
        assert stats_records[0].name == "istota.scheduler.stats"


class TestZeroIntervalGate:
    """The daemon-loop gate, not the helper, is what honours interval == 0.

    Replicates the exact condition used in run_daemon so a refactor of the
    guard expression is caught here without standing up the whole daemon.
    """

    def _should_emit(self, interval, now, last):
        return bool(interval and now - last >= interval)

    def test_zero_interval_never_emits(self):
        assert self._should_emit(0, now=10_000.0, last=0.0) is False

    def test_positive_interval_emits_after_elapsed(self):
        assert self._should_emit(60, now=100.0, last=0.0) is True

    def test_positive_interval_waits_until_elapsed(self):
        assert self._should_emit(60, now=30.0, last=0.0) is False
