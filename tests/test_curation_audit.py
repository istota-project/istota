"""Tests for the curation audit log."""

import json
from unittest.mock import patch

from istota.config import Config, NextcloudConfig
from istota.memory.curation.audit import get_curation_audit_path, write_audit_log


class TestAuditPath:
    def test_audit_path_is_sibling_of_user_md(self, tmp_path):
        from istota.storage import _get_mount_path, get_user_memory_path

        config = Config(
            db_path=tmp_path / "x.db",
            nextcloud=NextcloudConfig(),
            nextcloud_mount_path=tmp_path / "mount",
            bot_name="Istota",
        )
        user_md = _get_mount_path(config, get_user_memory_path("alice", config.bot_dir_name))
        path = get_curation_audit_path(config, "alice")
        assert path.name == "USER.md.audit.jsonl"
        assert path.parent == user_md.parent


class TestWriteAuditLog:
    def _config(self, tmp_path):
        return Config(
            db_path=tmp_path / "x.db",
            nextcloud=NextcloudConfig(),
            nextcloud_mount_path=tmp_path / "mount",
            bot_name="Istota",
        )

    def test_writes_jsonl_line_when_ops_applied(self, tmp_path):
        config = self._config(tmp_path)
        applied = [{"op": {"op": "append", "heading": "A", "line": "- x"}, "outcome": "applied"}]
        write_audit_log(config, "alice", applied=applied, rejected=[])
        path = get_curation_audit_path(config, "alice")
        assert path.exists()
        text = path.read_text()
        # One JSON line
        assert text.count("\n") == 1
        entry = json.loads(text.strip())
        assert entry["user_id"] == "alice"
        assert entry["applied"] == applied
        assert entry["rejected"] == []
        assert "ts" in entry

    def test_writes_nothing_when_applied_and_rejected_both_empty(self, tmp_path):
        config = self._config(tmp_path)
        write_audit_log(config, "alice", applied=[], rejected=[])
        path = get_curation_audit_path(config, "alice")
        assert not path.exists()

    def test_writes_when_only_rejected_ops_present(self, tmp_path):
        config = self._config(tmp_path)
        rejected = [{"op": {"op": "append", "heading": "X"}, "reason": "missing_field"}]
        write_audit_log(config, "alice", applied=[], rejected=rejected)
        path = get_curation_audit_path(config, "alice")
        assert path.exists()

    def test_creates_directory_if_missing(self, tmp_path):
        config = self._config(tmp_path)
        applied = [{"op": {"op": "append", "heading": "A", "line": "- x"}, "outcome": "applied"}]
        # Parent dir doesn't exist yet
        write_audit_log(config, "alice", applied=applied, rejected=[])
        path = get_curation_audit_path(config, "alice")
        assert path.exists()
        assert path.parent.exists()

    def test_appends_subsequent_runs(self, tmp_path):
        config = self._config(tmp_path)
        applied1 = [{"op": {"op": "append", "heading": "A", "line": "- x"}, "outcome": "applied"}]
        applied2 = [{"op": {"op": "append", "heading": "A", "line": "- y"}, "outcome": "applied"}]
        write_audit_log(config, "alice", applied=applied1, rejected=[])
        write_audit_log(config, "alice", applied=applied2, rejected=[])
        path = get_curation_audit_path(config, "alice")
        lines = [line for line in path.read_text().splitlines() if line]
        assert len(lines) == 2
