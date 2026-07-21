"""Tests for the shared-scope branch of _process_deferred_kv_ops."""

import json

from istota import db
from istota.config import (
    Config,
    EmailConfig,
    NextcloudConfig,
    SchedulerConfig,
    TalkConfig,
)
from istota.scheduler import _process_deferred_kv_ops


def _make_config(db_path, tmp_path, admin_users):
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
        admin_users=set(admin_users),
    )


def _seed_task(db_path, user_id):
    with db.get_db(db_path) as conn:
        task_id = db.create_task(conn, prompt="t", user_id=user_id)
        return db.get_task(conn, task_id)


class TestSharedDeferredWrites:
    def test_admin_task_applies_shared_set(self, db_path, tmp_path):
        config = _make_config(db_path, tmp_path, {"alice"})
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)
        task = _seed_task(db_path, "alice")

        ops = [{"op": "set", "namespace": "briefings", "key": "digest",
                "value": '{"text":"hi"}', "scope": "shared"}]
        (user_temp / f"task_{task.id}_kv_ops.json").write_text(json.dumps(ops))

        count = _process_deferred_kv_ops(config, task, user_temp)
        assert count == 1
        with db.get_db(db_path) as conn:
            row = db.shared_kv_get(conn, "briefings", "digest")
        assert row is not None
        assert row["value"] == '{"text":"hi"}'
        assert row["written_by"] == "alice"  # trusted identity, from the task

    def test_non_admin_shared_op_skipped(self, db_path, tmp_path, caplog):
        config = _make_config(db_path, tmp_path, {"alice"})
        user_temp = tmp_path / "temp" / "mallory"
        user_temp.mkdir(parents=True)
        task = _seed_task(db_path, "mallory")

        ops = [{"op": "set", "namespace": "briefings", "key": "digest",
                "value": '{"text":"evil"}', "scope": "shared"}]
        (user_temp / f"task_{task.id}_kv_ops.json").write_text(json.dumps(ops))

        count = _process_deferred_kv_ops(config, task, user_temp)
        assert count == 0
        with db.get_db(db_path) as conn:
            assert db.shared_kv_get(conn, "briefings", "digest") is None

    def test_identity_always_from_task_not_json(self, db_path, tmp_path):
        # A crafted user_id in the JSON must not influence the written_by /
        # authorization — identity comes from task.user_id.
        config = _make_config(db_path, tmp_path, {"alice"})
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)
        task = _seed_task(db_path, "alice")

        ops = [{"op": "set", "namespace": "ns", "key": "k",
                "value": '"v"', "scope": "shared", "user_id": "mallory"}]
        (user_temp / f"task_{task.id}_kv_ops.json").write_text(json.dumps(ops))

        _process_deferred_kv_ops(config, task, user_temp)
        with db.get_db(db_path) as conn:
            row = db.shared_kv_get(conn, "ns", "k")
        assert row["written_by"] == "alice"

    def test_user_ops_apply_alongside_denied_shared_op(self, db_path, tmp_path):
        # A non-admin's file mixing a user-scope op and a shared op: the shared
        # one is skipped, the user-scope one still applies.
        config = _make_config(db_path, tmp_path, {"alice"})
        user_temp = tmp_path / "temp" / "mallory"
        user_temp.mkdir(parents=True)
        task = _seed_task(db_path, "mallory")

        ops = [
            {"op": "set", "namespace": "ns", "key": "shared_k",
             "value": '"x"', "scope": "shared"},
            {"op": "set", "namespace": "ns", "key": "own_k", "value": '"y"'},
        ]
        (user_temp / f"task_{task.id}_kv_ops.json").write_text(json.dumps(ops))

        _process_deferred_kv_ops(config, task, user_temp)
        with db.get_db(db_path) as conn:
            assert db.shared_kv_get(conn, "ns", "shared_k") is None
            assert db.kv_get(conn, "mallory", "ns", "own_k")["value"] == '"y"'

    def test_shared_delete(self, db_path, tmp_path):
        config = _make_config(db_path, tmp_path, {"alice"})
        with db.get_db(db_path) as conn:
            db.shared_kv_set(conn, "ns", "k", '"v"', "alice")
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)
        task = _seed_task(db_path, "alice")

        ops = [{"op": "delete", "namespace": "ns", "key": "k", "scope": "shared"}]
        (user_temp / f"task_{task.id}_kv_ops.json").write_text(json.dumps(ops))

        _process_deferred_kv_ops(config, task, user_temp)
        with db.get_db(db_path) as conn:
            assert db.shared_kv_get(conn, "ns", "k") is None
