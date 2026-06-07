"""Tests for transport.istota_file — TASKS.md write-back surface."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from istota import db
from istota.config import Config, NextcloudConfig, SchedulerConfig, UserConfig
from istota.transport.istota_file import IstotaFileTransport


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


def _config(db_path, tmp_path):
    return Config(
        db_path=db_path,
        nextcloud=NextcloudConfig(),
        scheduler=SchedulerConfig(),
        temp_dir=tmp_path / "temp",
        users={"alice": UserConfig()},
    )


def _make_task_with_file(db_path, status="completed"):
    with db.get_db(db_path) as conn:
        task_id = db.create_task(
            conn, prompt="do thing", user_id="alice",
            source_type="istota_file",
        )
        db.track_istota_file_task(
            conn, user_id="alice", content_hash="h1",
            original_line="- [ ] do thing", normalized_content="do thing",
            file_path="/Users/alice/inbox/TASKS.md", task_id=task_id,
        )
        db.update_task_status(conn, task_id, status, result="result text")
        task = db.get_task(conn, task_id)
    return task


class TestIstotaFileTransport:
    def test_capabilities_push_no_io_on_init(self, db_path, tmp_path):
        t = IstotaFileTransport(_config(db_path, tmp_path))
        assert t.name == "istota_file"
        assert t.capabilities.surface_class == "push"

    def test_poll_returns_empty(self, db_path, tmp_path):
        t = IstotaFileTransport(_config(db_path, tmp_path))
        assert asyncio.run(t.poll()) == []

    def test_resolve_target_returns_file_path(self, db_path, tmp_path):
        task = _make_task_with_file(db_path)
        t = IstotaFileTransport(_config(db_path, tmp_path))
        assert t.resolve_target(task) == "/Users/alice/inbox/TASKS.md"

    def test_resolve_target_none_without_record(self, db_path, tmp_path):
        with db.get_db(db_path) as conn:
            tid = db.create_task(conn, prompt="x", user_id="alice", source_type="talk")
            task = db.get_task(conn, tid)
        t = IstotaFileTransport(_config(db_path, tmp_path))
        assert t.resolve_target(task) is None

    def test_deliver_derives_success_from_completed_status(self, db_path, tmp_path):
        task = _make_task_with_file(db_path, status="completed")
        t = IstotaFileTransport(_config(db_path, tmp_path))
        with patch(
            "istota.tasks_file_poller.handle_tasks_file_completion",
        ) as mock_handle:
            asyncio.run(t.deliver("", "result text", task=task))
        mock_handle.assert_called_once()
        # success arg (3rd positional) is True for a completed task.
        assert mock_handle.call_args[0][2] is True

    def test_deliver_derives_failure_from_failed_status(self, db_path, tmp_path):
        task = _make_task_with_file(db_path, status="failed")
        t = IstotaFileTransport(_config(db_path, tmp_path))
        with patch(
            "istota.tasks_file_poller.handle_tasks_file_completion",
        ) as mock_handle:
            asyncio.run(t.deliver("", "result text", task=task))
        assert mock_handle.call_args[0][2] is False

    def test_deliver_none_without_task(self, db_path, tmp_path):
        t = IstotaFileTransport(_config(db_path, tmp_path))
        assert asyncio.run(t.deliver("", "x")) is None
