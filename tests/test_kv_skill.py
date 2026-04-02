"""Tests for istota.skills.kv — skill CLI and deferred KV operations."""

import json

from istota import db
from istota.skills.kv import main as kv_main


# ============================================================================
# Skill CLI — read operations
# ============================================================================


class TestKvSkillGet:
    def _seed(self, db_path, user, ns, key, value):
        """Seed data using a separate connection that commits."""
        with db.get_db(db_path) as conn:
            db.kv_set(conn, user, ns, key, value)

    def test_get_existing(self, db_path, capsys, monkeypatch):
        self._seed(db_path, "alice", "ns", "k", '"hello"')
        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
        kv_main(["get", "ns", "k"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["value"] == "hello"

    def test_get_missing(self, db_path, capsys, monkeypatch):
        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
        kv_main(["get", "ns", "missing"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "not_found"

    def test_get_json_object_value(self, db_path, capsys, monkeypatch):
        self._seed(db_path, "alice", "ns", "data", '{"count": 42}')
        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
        kv_main(["get", "ns", "data"])
        out = json.loads(capsys.readouterr().out)
        assert out["value"] == {"count": 42}


class TestKvSkillList:
    def test_list_entries(self, db_path, capsys, monkeypatch):
        with db.get_db(db_path) as conn:
            db.kv_set(conn, "alice", "ns", "a", '"1"')
            db.kv_set(conn, "alice", "ns", "b", '"2"')
        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
        kv_main(["list", "ns"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["count"] == 2

    def test_list_empty(self, db_path, capsys, monkeypatch):
        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
        kv_main(["list", "empty"])
        out = json.loads(capsys.readouterr().out)
        assert out["count"] == 0


class TestKvSkillNamespaces:
    def test_namespaces(self, db_path, capsys, monkeypatch):
        with db.get_db(db_path) as conn:
            db.kv_set(conn, "alice", "ns1", "a", '"1"')
            db.kv_set(conn, "alice", "ns2", "b", '"2"')
        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
        kv_main(["namespaces"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert sorted(out["namespaces"]) == ["ns1", "ns2"]


# ============================================================================
# Skill CLI — write operations (direct, no sandbox)
# ============================================================================


class TestKvSkillSetDirect:
    def test_set_direct(self, db_path, db_conn, capsys, monkeypatch):
        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
        monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
        kv_main(["set", "ns", "k", '"hello"'])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert "deferred" not in out

        result = db.kv_get(db_conn, "alice", "ns", "k")
        assert result is not None
        assert result["value"] == '"hello"'

    def test_set_invalid_json(self, db_path, capsys, monkeypatch):
        import pytest
        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
        with pytest.raises(SystemExit):
            kv_main(["set", "ns", "k", "not json"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"


class TestKvSkillDeleteDirect:
    def test_delete_existing(self, db_path, capsys, monkeypatch):
        with db.get_db(db_path) as conn:
            db.kv_set(conn, "alice", "ns", "k", '"v"')
        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
        monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
        kv_main(["delete", "ns", "k"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["deleted"] is True

    def test_delete_missing(self, db_path, capsys, monkeypatch):
        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
        monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
        kv_main(["delete", "ns", "missing"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["deleted"] is False


# ============================================================================
# Skill CLI — deferred writes (sandbox mode)
# ============================================================================


class TestKvSkillDeferred:
    def test_set_deferred(self, db_path, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(tmp_path))
        monkeypatch.setenv("ISTOTA_TASK_ID", "42")
        kv_main(["set", "ns", "k", '{"x": 1}'])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["deferred"] is True

        # Check deferred file was written
        deferred = tmp_path / "task_42_kv_ops.json"
        assert deferred.exists()
        ops = json.loads(deferred.read_text())
        assert len(ops) == 1
        assert ops[0] == {"op": "set", "namespace": "ns", "key": "k", "value": '{"x": 1}'}

    def test_delete_deferred(self, db_path, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(tmp_path))
        monkeypatch.setenv("ISTOTA_TASK_ID", "42")
        kv_main(["delete", "ns", "k"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["deferred"] is True

        ops = json.loads((tmp_path / "task_42_kv_ops.json").read_text())
        assert ops[0]["op"] == "delete"

    def test_multiple_deferred_ops_append(self, db_path, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(tmp_path))
        monkeypatch.setenv("ISTOTA_TASK_ID", "42")
        kv_main(["set", "ns", "a", '"1"'])
        capsys.readouterr()
        kv_main(["set", "ns", "b", '"2"'])
        capsys.readouterr()

        ops = json.loads((tmp_path / "task_42_kv_ops.json").read_text())
        assert len(ops) == 2
        assert ops[0]["key"] == "a"
        assert ops[1]["key"] == "b"


# ============================================================================
# Scheduler — deferred KV processing
# ============================================================================


class TestProcessDeferredKvOps:
    def _make_config(self, db_path, tmp_path):
        from istota.config import Config, NextcloudConfig, TalkConfig, EmailConfig, SchedulerConfig
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="secret"),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(),
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )

    def test_process_set_ops(self, db_path, tmp_path):
        from istota.scheduler import _process_deferred_kv_ops
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="test", user_id="alice")
            task = db.get_task(conn, task_id)

        ops = [
            {"op": "set", "namespace": "loc", "key": "state", "value": '{"place": "Home"}'},
            {"op": "set", "namespace": "loc", "key": "last", "value": '"2026-04-02"'},
        ]
        (user_temp / f"task_{task_id}_kv_ops.json").write_text(json.dumps(ops))

        count = _process_deferred_kv_ops(config, task, user_temp)
        assert count == 2

        with db.get_db(db_path) as conn:
            result = db.kv_get(conn, "alice", "loc", "state")
            assert json.loads(result["value"]) == {"place": "Home"}
            result2 = db.kv_get(conn, "alice", "loc", "last")
            assert json.loads(result2["value"]) == "2026-04-02"

        # File should be cleaned up
        assert not (user_temp / f"task_{task_id}_kv_ops.json").exists()

    def test_process_delete_ops(self, db_path, tmp_path):
        from istota.scheduler import _process_deferred_kv_ops
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="test", user_id="alice")
            task = db.get_task(conn, task_id)
            db.kv_set(conn, "alice", "ns", "old_key", '"old"')

        ops = [{"op": "delete", "namespace": "ns", "key": "old_key"}]
        (user_temp / f"task_{task_id}_kv_ops.json").write_text(json.dumps(ops))

        count = _process_deferred_kv_ops(config, task, user_temp)
        assert count == 1

        with db.get_db(db_path) as conn:
            assert db.kv_get(conn, "alice", "ns", "old_key") is None

    def test_no_file_returns_zero(self, db_path, tmp_path):
        from istota.scheduler import _process_deferred_kv_ops
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="test", user_id="alice")
            task = db.get_task(conn, task_id)

        count = _process_deferred_kv_ops(config, task, user_temp)
        assert count == 0

    def test_bad_json_cleans_up(self, db_path, tmp_path):
        from istota.scheduler import _process_deferred_kv_ops
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="test", user_id="alice")
            task = db.get_task(conn, task_id)

        (user_temp / f"task_{task_id}_kv_ops.json").write_text("not json")

        count = _process_deferred_kv_ops(config, task, user_temp)
        assert count == 0
        assert not (user_temp / f"task_{task_id}_kv_ops.json").exists()

    def test_mixed_ops(self, db_path, tmp_path):
        from istota.scheduler import _process_deferred_kv_ops
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="test", user_id="alice")
            task = db.get_task(conn, task_id)
            db.kv_set(conn, "alice", "ns", "to_delete", '"old"')

        ops = [
            {"op": "set", "namespace": "ns", "key": "new_key", "value": '"new"'},
            {"op": "delete", "namespace": "ns", "key": "to_delete"},
        ]
        (user_temp / f"task_{task_id}_kv_ops.json").write_text(json.dumps(ops))

        count = _process_deferred_kv_ops(config, task, user_temp)
        assert count == 2

        with db.get_db(db_path) as conn:
            assert db.kv_get(conn, "alice", "ns", "new_key") is not None
            assert db.kv_get(conn, "alice", "ns", "to_delete") is None
