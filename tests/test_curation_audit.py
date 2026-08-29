"""Tests for the curation audit log and fingerprints, stored in the KV store."""

import hashlib
import json

import pytest

from istota import db
from istota.config import Config, NextcloudConfig
from istota.kv_namespaces import is_reserved_namespace
from istota.memory.curation.audit import (
    AUDIT_NAMESPACE,
    CURATION_NAMESPACE,
    LAST_SEEN_KEY,
    LINT_SEEN_KEY,
    detect_bypass_write,
    legacy_audit_sidecar_path,
    legacy_last_seen_sidecar_path,
    legacy_lint_seen_sidecar_path,
    migrate_user_md_sidecars,
    read_audit_entries,
    read_last_seen,
    read_lint_seen,
    write_audit_log,
    write_last_seen,
    write_lint_seen,
)


@pytest.fixture
def config(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    return Config(
        db_path=db_path,
        nextcloud=NextcloudConfig(),
        nextcloud_mount_path=tmp_path / "mount",
        bot_name="Istota",
    )


def _rows(config, namespace, user_id="alice"):
    with db.get_db(config.db_path) as conn:
        return db.kv_list(conn, user_id, namespace)


APPLIED = [{"op": {"op": "append", "heading": "A", "line": "- x"}, "outcome": "applied"}]


class TestNamespacesAreReserved:
    """The whole point of the `_` prefix: the model-facing kv skill and the
    deferred applier both refuse these, and they do it by prefix. A literal
    renamed without the prefix would silently expose the audit trail."""

    def test_both_namespaces_carry_the_reserved_prefix(self):
        assert is_reserved_namespace(AUDIT_NAMESPACE)
        assert is_reserved_namespace(CURATION_NAMESPACE)


class TestWriteAuditLog:
    def test_writes_one_row_when_ops_applied(self, config):
        write_audit_log(config, "alice", applied=APPLIED, rejected=[])
        rows = _rows(config, AUDIT_NAMESPACE)
        assert len(rows) == 1
        entry = json.loads(rows[0]["value"])
        assert entry["user_id"] == "alice"
        assert entry["applied"] == APPLIED
        assert entry["rejected"] == []
        assert "ts" in entry

    def test_writes_nothing_when_applied_and_rejected_both_empty(self, config):
        write_audit_log(config, "alice", applied=[], rejected=[])
        assert _rows(config, AUDIT_NAMESPACE) == []

    def test_writes_when_only_rejected_ops_present(self, config):
        rejected = [{"op": {"op": "append", "heading": "X"}, "reason": "missing_field"}]
        write_audit_log(config, "alice", applied=[], rejected=rejected)
        assert len(_rows(config, AUDIT_NAMESPACE)) == 1

    def test_appends_subsequent_runs_within_one_second(self, config):
        """Two writes in the same second must not collide.

        `kv_set` upserts, and the timestamp has one-second resolution, so
        without the per-second counter in the key the second write would
        overwrite the first. A nightly run emits up to three entries back to
        back, so this is the ordinary case rather than a race.
        """
        write_audit_log(config, "alice", applied=APPLIED, rejected=[])
        write_audit_log(config, "alice", applied=APPLIED, rejected=[])
        write_audit_log(config, "alice", applied=APPLIED, rejected=[])
        assert len(_rows(config, AUDIT_NAMESPACE)) == 3

    def test_entries_read_back_oldest_first(self, config):
        for i in range(3):
            write_audit_log(
                config, "alice",
                applied=[{"op": {"n": i}, "outcome": "applied"}], rejected=[],
            )
        entries = read_audit_entries(config, "alice")
        assert [e["applied"][0]["op"]["n"] for e in entries] == [0, 1, 2]

    def test_limit_keeps_the_newest(self, config):
        for i in range(4):
            write_audit_log(
                config, "alice",
                applied=[{"op": {"n": i}, "outcome": "applied"}], rejected=[],
            )
        entries = read_audit_entries(config, "alice", limit=2)
        assert [e["applied"][0]["op"]["n"] for e in entries] == [2, 3]

    def test_scoped_to_the_user(self, config):
        write_audit_log(config, "alice", applied=APPLIED, rejected=[])
        assert read_audit_entries(config, "bob") == []

    def test_unparseable_row_is_skipped_not_raised(self, config):
        write_audit_log(config, "alice", applied=APPLIED, rejected=[])
        with db.get_db(config.db_path) as conn:
            db.kv_set(conn, "alice", AUDIT_NAMESPACE, "9999-01-01T00:00:00Z-000", "{not json")
        assert len(read_audit_entries(config, "alice")) == 1

    def test_no_db_path_is_silent(self, tmp_path):
        class _Shim:
            db_path = None
        write_audit_log(_Shim(), "alice", applied=APPLIED, rejected=[])
        assert read_audit_entries(_Shim(), "alice") == []


class TestSourceAndEntryKind:
    def test_default_source_is_nightly(self, config):
        write_audit_log(config, "alice", applied=APPLIED, rejected=[])
        entry = read_audit_entries(config, "alice")[0]
        assert entry["source"] == "nightly"
        assert entry["entry_kind"] == "batch"

    def test_runtime_source_round_trips(self, config):
        write_audit_log(config, "alice", applied=APPLIED, rejected=[], source="runtime")
        assert read_audit_entries(config, "alice")[0]["source"] == "runtime"

    def test_lint_candidate_entry_with_extra(self, config):
        write_audit_log(
            config, "alice",
            applied=[], rejected=[],
            source="nightly", entry_kind="lint_candidate",
            extra={"lint_candidates": [{"heading": "Notes", "bullet_text": "bought X on 2026-01-01"}]},
        )
        entry = read_audit_entries(config, "alice")[0]
        assert entry["entry_kind"] == "lint_candidate"
        assert entry["lint_candidates"][0]["heading"] == "Notes"


class TestBypassDetection:
    def test_first_sight_returns_none(self, config):
        assert detect_bypass_write(config, "alice", "first contents\n") is None

    def test_changed_since_last_seen_returns_signal(self, config):
        write_last_seen(config, "alice", size_bytes=10, sha256="deadbeef")
        signal = detect_bypass_write(config, "alice", "different\n")
        assert signal is not None
        assert signal["previous_sha256"] == "deadbeef"

    def test_unchanged_returns_none(self, config):
        text = "stable\n"
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        write_last_seen(config, "alice", size_bytes=len(text), sha256=sha)
        assert detect_bypass_write(config, "alice", text) is None

    def test_runtime_write_updates_last_seen(self, config):
        # After a runtime write updates last_seen, the next bypass check
        # against the same content should NOT flag.
        text = "v1\n"
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        write_last_seen(config, "alice", size_bytes=len(text), sha256=sha)
        assert detect_bypass_write(config, "alice", text) is None
        text2 = "v2\n"
        sha2 = hashlib.sha256(text2.encode("utf-8")).hexdigest()
        write_last_seen(config, "alice", size_bytes=len(text2), sha256=sha2)
        assert detect_bypass_write(config, "alice", text2) is None
        # An out-of-band edit that did NOT update last_seen flags.
        assert detect_bypass_write(config, "alice", "v3\n") is not None

    def test_last_seen_is_scoped_to_the_user(self, config):
        write_last_seen(config, "alice", size_bytes=10, sha256="deadbeef")
        assert read_last_seen(config, "bob") is None


class TestLintSeen:
    def test_round_trips(self, config):
        write_lint_seen(config, "alice", {"abc123": "2026-08-29"})
        assert read_lint_seen(config, "alice") == {"abc123": "2026-08-29"}

    def test_absent_reads_as_empty(self, config):
        assert read_lint_seen(config, "alice") == {}

    def test_corrupt_value_reads_as_empty(self, config):
        with db.get_db(config.db_path) as conn:
            db.kv_set(conn, "alice", CURATION_NAMESPACE, LINT_SEEN_KEY, "{not json")
        assert read_lint_seen(config, "alice") == {}


class TestSidecarMigration:
    def _sidecar_dir(self, config):
        path = legacy_audit_sidecar_path(config, "alice")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.parent

    def test_absent_sidecars_are_a_noop(self, config):
        self._sidecar_dir(config)
        outcomes = migrate_user_md_sidecars(config, "alice")
        assert set(outcomes.values()) == {"absent"}
        assert _rows(config, AUDIT_NAMESPACE) == []

    def test_audit_jsonl_is_imported_and_removed(self, config):
        self._sidecar_dir(config)
        path = legacy_audit_sidecar_path(config, "alice")
        path.write_text(
            '{"ts": "2026-08-01T00:00:00Z", "applied": [1], "rejected": []}\n'
            '{"ts": "2026-08-02T00:00:00Z", "applied": [2], "rejected": []}\n'
        )
        outcomes = migrate_user_md_sidecars(config, "alice")
        assert outcomes["audit"] == "migrated"
        assert not path.exists()
        entries = read_audit_entries(config, "alice")
        assert [e["applied"] for e in entries] == [[1], [2]]

    def test_import_preserves_original_order_within_one_second(self, config):
        """Every entry in the live file shares a handful of timestamps, and
        several share one exactly. The counter has to keep them apart on the
        way in, not just on a fresh write."""
        self._sidecar_dir(config)
        path = legacy_audit_sidecar_path(config, "alice")
        path.write_text("".join(
            '{"ts": "2026-08-01T00:00:00Z", "applied": [%d], "rejected": []}\n' % i
            for i in range(5)
        ))
        migrate_user_md_sidecars(config, "alice")
        entries = read_audit_entries(config, "alice")
        assert [e["applied"][0] for e in entries] == [0, 1, 2, 3, 4]

    def test_a_malformed_line_keeps_the_file(self, config):
        """Unlinking would destroy the only copy of a line we could not read."""
        self._sidecar_dir(config)
        path = legacy_audit_sidecar_path(config, "alice")
        path.write_text(
            '{"ts": "2026-08-01T00:00:00Z", "applied": [1], "rejected": []}\n'
            'not json at all\n'
        )
        outcomes = migrate_user_md_sidecars(config, "alice")
        assert outcomes["audit"] == "kept"
        assert path.exists()
        assert len(read_audit_entries(config, "alice")) == 1

    def test_last_seen_sidecar_is_imported(self, config):
        self._sidecar_dir(config)
        path = legacy_last_seen_sidecar_path(config, "alice")
        path.write_text(json.dumps({"ts": "x", "size_bytes": 42, "sha256": "abc"}))
        outcomes = migrate_user_md_sidecars(config, "alice")
        assert outcomes["last_seen"] == "migrated"
        assert not path.exists()
        assert read_last_seen(config, "alice")["sha256"] == "abc"

    def test_an_existing_row_wins_over_the_sidecar(self, config):
        """A runtime CLI write that landed before the first nightly pass is
        newer than the file. Overwriting it with the stale fingerprint would
        re-arm the bypass detector against a change already accounted for."""
        self._sidecar_dir(config)
        write_last_seen(config, "alice", size_bytes=99, sha256="newer")
        path = legacy_last_seen_sidecar_path(config, "alice")
        path.write_text(json.dumps({"ts": "x", "size_bytes": 42, "sha256": "older"}))
        migrate_user_md_sidecars(config, "alice")
        assert read_last_seen(config, "alice")["sha256"] == "newer"
        assert not path.exists()

    def test_lint_seen_sidecar_is_imported(self, config):
        self._sidecar_dir(config)
        path = legacy_lint_seen_sidecar_path(config, "alice")
        path.write_text(json.dumps({"hashes": {"abc123": "2026-08-01"}}))
        outcomes = migrate_user_md_sidecars(config, "alice")
        assert outcomes["lint_seen"] == "migrated"
        assert read_lint_seen(config, "alice") == {"abc123": "2026-08-01"}

    def test_second_run_is_a_noop(self, config):
        self._sidecar_dir(config)
        legacy_audit_sidecar_path(config, "alice").write_text(
            '{"ts": "2026-08-01T00:00:00Z", "applied": [1], "rejected": []}\n'
        )
        migrate_user_md_sidecars(config, "alice")
        migrate_user_md_sidecars(config, "alice")
        assert len(read_audit_entries(config, "alice")) == 1

    def test_a_failing_database_keeps_the_file_and_writes_nothing(self, config, tmp_path):
        """The import must be all-or-nothing. A partial import that committed
        its first N rows and kept the file would duplicate them next pass."""
        self._sidecar_dir(config)
        path = legacy_audit_sidecar_path(config, "alice")
        path.write_text(
            '{"ts": "2026-08-01T00:00:00Z", "applied": [1], "rejected": []}\n'
        )
        config.db_path = tmp_path / "does" / "not" / "exist.db"
        outcomes = migrate_user_md_sidecars(config, "alice")
        assert outcomes["audit"] == "kept"
        assert path.exists()

    def test_skipped_without_a_mount(self, config):
        config.nextcloud_mount_path = None
        assert migrate_user_md_sidecars(config, "alice") == {}


class TestKeysUsedByTheStore:
    """The key names are part of the on-disk contract — an operator reads them
    with `sqlite3` and the migration writes them by name."""

    def test_last_seen_key(self, config):
        write_last_seen(config, "alice", size_bytes=1, sha256="a")
        assert [r["key"] for r in _rows(config, CURATION_NAMESPACE)] == [LAST_SEEN_KEY]

    def test_lint_seen_key(self, config):
        write_lint_seen(config, "alice", {})
        assert [r["key"] for r in _rows(config, CURATION_NAMESPACE)] == [LINT_SEEN_KEY]
