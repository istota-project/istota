"""Tests for the curation audit log."""

import json
from unittest.mock import patch

from istota.config import Config, NextcloudConfig
from istota.memory.curation.audit import (
    detect_bypass_write,
    get_curation_audit_path,
    get_user_md_last_seen_path,
    read_last_seen,
    write_audit_log,
    write_last_seen,
)


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


class TestSourceAndEntryKind:
    def _config(self, tmp_path):
        return Config(
            db_path=tmp_path / "x.db",
            nextcloud=NextcloudConfig(),
            nextcloud_mount_path=tmp_path / "mount",
            bot_name="Istota",
        )

    def test_default_source_is_nightly(self, tmp_path):
        config = self._config(tmp_path)
        applied = [{"op": {"op": "append", "heading": "A", "line": "- x"}, "outcome": "applied"}]
        write_audit_log(config, "alice", applied=applied, rejected=[])
        entry = json.loads(get_curation_audit_path(config, "alice").read_text().strip())
        assert entry["source"] == "nightly"
        assert entry["entry_kind"] == "batch"

    def test_runtime_source_round_trips(self, tmp_path):
        config = self._config(tmp_path)
        applied = [{"op": {"op": "append", "heading": "A", "line": "- x"}, "outcome": "applied"}]
        write_audit_log(config, "alice", applied=applied, rejected=[], source="runtime")
        entry = json.loads(get_curation_audit_path(config, "alice").read_text().strip())
        assert entry["source"] == "runtime"

    def test_lint_candidate_entry_with_extra(self, tmp_path):
        config = self._config(tmp_path)
        write_audit_log(
            config, "alice",
            applied=[], rejected=[],
            source="nightly", entry_kind="lint_candidate",
            extra={"lint_candidates": [{"heading": "Notes", "bullet_text": "bought X on 2026-01-01"}]},
        )
        entry = json.loads(get_curation_audit_path(config, "alice").read_text().strip())
        assert entry["entry_kind"] == "lint_candidate"
        assert entry["lint_candidates"][0]["heading"] == "Notes"


class TestBypassDetection:
    def _config(self, tmp_path):
        return Config(
            db_path=tmp_path / "x.db",
            nextcloud=NextcloudConfig(),
            nextcloud_mount_path=tmp_path / "mount",
            bot_name="Istota",
        )

    def test_first_sight_returns_none(self, tmp_path):
        config = self._config(tmp_path)
        signal = detect_bypass_write(config, "alice", "first contents\n")
        assert signal is None

    def test_changed_since_last_seen_returns_signal(self, tmp_path):
        config = self._config(tmp_path)
        write_last_seen(config, "alice", size_bytes=10, sha256="deadbeef")
        signal = detect_bypass_write(config, "alice", "different\n")
        assert signal is not None
        assert signal["previous_sha256"] == "deadbeef"

    def test_unchanged_returns_none(self, tmp_path):
        import hashlib
        config = self._config(tmp_path)
        text = "stable\n"
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        write_last_seen(config, "alice", size_bytes=len(text), sha256=sha)
        assert detect_bypass_write(config, "alice", text) is None

    def test_runtime_write_updates_last_seen(self, tmp_path):
        # After a runtime write updates last_seen, the next bypass check
        # against the same content should NOT flag.
        import hashlib
        config = self._config(tmp_path)
        text = "v1\n"
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        write_last_seen(config, "alice", size_bytes=len(text), sha256=sha)
        assert detect_bypass_write(config, "alice", text) is None
        # Now a runtime write happens — caller updates last_seen — and
        # bypass detection on the new contents should pass.
        text2 = "v2\n"
        sha2 = hashlib.sha256(text2.encode("utf-8")).hexdigest()
        write_last_seen(config, "alice", size_bytes=len(text2), sha256=sha2)
        assert detect_bypass_write(config, "alice", text2) is None
        # An out-of-band edit that did NOT update last_seen flags.
        signal = detect_bypass_write(config, "alice", "v3\n")
        assert signal is not None
