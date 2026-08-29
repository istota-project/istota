"""The `_`-prefixed KV namespaces are framework state the model cannot reach.

Two enforcement points, and neither substitutes for the other: the `kv` skill
covers a host-side CLI call, `_process_deferred_kv_ops` covers a **sandboxed**
task, whose `kv set` never runs the skill's check at all — it writes a JSON op
file that the scheduler replays afterwards.
"""

import json

import pytest

from istota import db
from istota.config import (
    Config,
    EmailConfig,
    NextcloudConfig,
    SchedulerConfig,
    TalkConfig,
)
from istota.kv_namespaces import RESERVED_NAMESPACE_PREFIX, is_reserved_namespace
from istota.memory.curation.audit import AUDIT_NAMESPACE, CURATION_NAMESPACE
from istota.scheduler import _process_deferred_kv_ops
from istota.skills.kv import main as kv_main


class TestPredicate:
    def test_prefixed_names_are_reserved(self):
        assert is_reserved_namespace("_memory_audit")
        assert is_reserved_namespace(RESERVED_NAMESPACE_PREFIX)

    def test_ordinary_names_are_not(self):
        assert not is_reserved_namespace("briefings")
        assert not is_reserved_namespace("warsaw")
        # The prefix is a *leading* underscore, not one anywhere in the name.
        assert not is_reserved_namespace("my_namespace")

    def test_a_non_string_is_not_reserved_and_does_not_raise(self):
        """Both call sites pass a value off an argparse namespace or out of
        model-written JSON, so a non-string is a case to answer."""
        assert not is_reserved_namespace(None)
        assert not is_reserved_namespace(42)
        assert not is_reserved_namespace({"namespace": "_memory_audit"})


class TestSkillCliRefuses:
    """Every verb taking a namespace, not just the writers: `kv list
    _memory_audit` would put the whole curation trail in the model's context.
    """

    @pytest.fixture(autouse=True)
    def _env(self, db_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)

    @pytest.mark.parametrize("argv", [
        ["get", "_memory_audit", "k"],
        ["set", "_memory_audit", "k", '"v"'],
        ["delete", "_memory_curation", "last_seen"],
        ["list", "_memory_audit"],
        ["set-contains", "_x", "k", "m"],
        ["set-size", "_x", "k"],
        ["set-members", "_x", "k"],
        ["set-add", "_x", "k", "m"],
        ["set-remove", "_x", "k", "m"],
        ["set-trim", "_x", "k", "--keep-newest", "1"],
    ])
    def test_verb_is_refused(self, argv, capsys):
        with pytest.raises(SystemExit) as exc:
            kv_main(argv)
        assert exc.value.code == 1
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "reserved" in out["error"]

    def test_a_refused_read_returns_no_value(self, db_path, capsys):
        """The refusal has to come before the read, not after it."""
        with db.get_db(db_path) as conn:
            db.kv_set(conn, "alice", CURATION_NAMESPACE, "last_seen", '{"sha256":"secret"}')
        with pytest.raises(SystemExit):
            kv_main(["get", CURATION_NAMESPACE, "last_seen"])
        assert "secret" not in capsys.readouterr().out

    def test_a_refused_write_leaves_the_row_alone(self, db_path, capsys):
        with db.get_db(db_path) as conn:
            db.kv_set(conn, "alice", CURATION_NAMESPACE, "last_seen", '{"sha256":"real"}')
        with pytest.raises(SystemExit):
            kv_main(["set", CURATION_NAMESPACE, "last_seen", '{"sha256":"forged"}'])
        with db.get_db(db_path) as conn:
            row = db.kv_get(conn, "alice", CURATION_NAMESPACE, "last_seen")
        assert json.loads(row["value"])["sha256"] == "real"

    def test_a_refused_delete_leaves_the_row_alone(self, db_path, capsys):
        with db.get_db(db_path) as conn:
            db.kv_set(conn, "alice", AUDIT_NAMESPACE, "2026-01-01T00:00:00Z-000", "{}")
        with pytest.raises(SystemExit):
            kv_main(["delete", AUDIT_NAMESPACE, "2026-01-01T00:00:00Z-000"])
        with db.get_db(db_path) as conn:
            assert db.kv_get(conn, "alice", AUDIT_NAMESPACE, "2026-01-01T00:00:00Z-000") is not None

    def test_ordinary_namespaces_still_work(self, capsys):
        kv_main(["set", "warsaw", "k", '"v"'])
        capsys.readouterr()
        kv_main(["get", "warsaw", "k"])
        out = json.loads(capsys.readouterr().out)
        assert out["value"] == "v"

    def test_namespaces_listing_hides_them(self, db_path, capsys):
        with db.get_db(db_path) as conn:
            db.kv_set(conn, "alice", "warsaw", "k", '"v"')
            db.kv_set(conn, "alice", AUDIT_NAMESPACE, "k", "{}")
            db.kv_set(conn, "alice", CURATION_NAMESPACE, "last_seen", "{}")
        kv_main(["namespaces"])
        out = json.loads(capsys.readouterr().out)
        assert out["namespaces"] == ["warsaw"]

    def test_the_deferred_path_is_refused_too(self, db_path, tmp_path, capsys, monkeypatch):
        """A sandboxed `kv set` writes an op file rather than touching the DB.
        The refusal must land before that file is written, or the scheduler
        would be the only thing standing between the model and the row."""
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(deferred))
        monkeypatch.setenv("ISTOTA_TASK_ID", "7")
        with pytest.raises(SystemExit):
            kv_main(["set", AUDIT_NAMESPACE, "k", '"v"'])
        assert list(deferred.iterdir()) == []


def _make_config(db_path, tmp_path):
    mount = tmp_path / "mount"
    mount.mkdir(exist_ok=True)
    return Config(
        db_path=db_path,
        nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="s"),
        talk=TalkConfig(enabled=True, bot_username="istota"),
        email=EmailConfig(enabled=False),
        scheduler=SchedulerConfig(),
        nextcloud_mount_path=mount,
        temp_dir=tmp_path / "temp",
        admin_users={"alice"},
    )


class TestDeferredApplierRefuses:
    """The op file is model-written, so the namespace in it is untrusted even
    though the skill would have refused the same name at the CLI."""

    def _run(self, db_path, tmp_path, ops):
        config = _make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="t", user_id="alice")
            task = db.get_task(conn, task_id)
        (user_temp / f"task_{task.id}_kv_ops.json").write_text(json.dumps(ops))
        return _process_deferred_kv_ops(config, task, user_temp)

    def test_set_into_a_reserved_namespace_is_dropped(self, db_path, tmp_path):
        count = self._run(db_path, tmp_path, [
            {"op": "set", "namespace": AUDIT_NAMESPACE, "key": "k", "value": '"v"'},
        ])
        assert count == 0
        with db.get_db(db_path) as conn:
            assert db.kv_get(conn, "alice", AUDIT_NAMESPACE, "k") is None

    def test_delete_of_a_reserved_row_is_dropped(self, db_path, tmp_path):
        with db.get_db(db_path) as conn:
            db.kv_set(conn, "alice", AUDIT_NAMESPACE, "k", "{}")
        count = self._run(db_path, tmp_path, [
            {"op": "delete", "namespace": AUDIT_NAMESPACE, "key": "k"},
        ])
        assert count == 0
        with db.get_db(db_path) as conn:
            assert db.kv_get(conn, "alice", AUDIT_NAMESPACE, "k") is not None

    def test_shared_scope_is_refused_before_the_admin_gate(self, db_path, tmp_path):
        """`alice` is an admin here, so the shared-write gate would let this
        through. The namespace check has to sit above it."""
        count = self._run(db_path, tmp_path, [
            {"op": "set", "namespace": "_secret", "key": "k",
             "value": '"v"', "scope": "shared"},
        ])
        assert count == 0
        with db.get_db(db_path) as conn:
            assert db.shared_kv_get(conn, "_secret", "k") is None

    def test_an_ordinary_op_in_the_same_file_still_applies(self, db_path, tmp_path):
        """One refused op must not abandon the rest of the batch."""
        count = self._run(db_path, tmp_path, [
            {"op": "set", "namespace": AUDIT_NAMESPACE, "key": "k", "value": '"v"'},
            {"op": "set", "namespace": "warsaw", "key": "k", "value": '"v"'},
        ])
        assert count == 1
        with db.get_db(db_path) as conn:
            assert db.kv_get(conn, "alice", "warsaw", "k") is not None
