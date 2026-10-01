"""The `scope: "group:<id>"` branch of `_process_deferred_kv_ops`.

The op file is model-written, so membership is decided against `task.user_id`
at apply time and never against anything in the JSON, and every refusal still
consumes the file.
"""

import json
import logging

import pytest

from istota import db
from istota.config import Config
from istota.scheduler import _process_deferred_kv_ops


@pytest.fixture
def setup(db_path, tmp_path):
    config = Config(db_path=db_path, temp_dir=tmp_path / "temp")
    with db.get_db(db_path) as conn:
        db.create_group(conn, "fam", kind="family", display_name="Fam",
                        created_by="operator")
        db.add_group_member(conn, "fam", "alice", added_by="operator")
        db.add_group_member(conn, "fam", "bob", added_by="operator")
    return config


def _task(db_path, user_id):
    with db.get_db(db_path) as conn:
        return db.get_task(conn, db.create_task(conn, prompt="t", user_id=user_id))


def _apply(config, user_id, ops, tmp_path):
    task = _task(config.db_path, user_id)
    user_temp = tmp_path / "temp" / user_id
    user_temp.mkdir(parents=True, exist_ok=True)
    path = user_temp / f"task_{task.id}_kv_ops.json"
    path.write_text(json.dumps(ops))
    count = _process_deferred_kv_ops(config, task, user_temp)
    assert not path.exists(), "the op file is consumed whatever was refused"
    return count


def _group_value(db_path, ns="ns", key="k", group="fam"):
    with db.get_db(db_path) as conn:
        return db.group_kv_get(conn, group, ns, key)


class TestMemberOps:
    def test_set_applies_with_the_task_identity(self, setup, db_path, tmp_path):
        ops = [{"op": "set", "namespace": "ns", "key": "k", "value": '"v"',
                "scope": "group:fam", "user_id": "mallory", "written_by": "x"}]
        assert _apply(setup, "alice", ops, tmp_path) == 1
        row = _group_value(db_path)
        assert row["value"] == '"v"'
        assert row["written_by"] == "alice"
        with db.get_db(db_path) as conn:
            assert db.kv_get(conn, "alice", "ns", "k") is None

    def test_delete(self, setup, db_path, tmp_path):
        with db.get_db(db_path) as conn:
            db.group_kv_set(conn, "fam", "ns", "k", '"v"', "bob")
        ops = [{"op": "delete", "namespace": "ns", "key": "k", "scope": "group:fam"}]
        assert _apply(setup, "alice", ops, tmp_path) == 1
        assert _group_value(db_path) is None

    def test_set_ops_replay_against_group_kv_not_istota_kv(
        self, setup, db_path, tmp_path,
    ):
        with db.get_db(db_path) as conn:
            db.kv_set(conn, "alice", "ns", "k", '["personal"]')
            db.group_kv_set(conn, "fam", "ns", "k", '["a"]', "bob")
        ops = [
            {"op": "set-add", "namespace": "ns", "key": "k",
             "members": ["b", "c"], "scope": "group:fam"},
            {"op": "set-remove", "namespace": "ns", "key": "k",
             "members": ["a"], "scope": "group:fam"},
            {"op": "set-trim", "namespace": "ns", "key": "k",
             "keep_newest": 1, "scope": "group:fam"},
        ]
        assert _apply(setup, "alice", ops, tmp_path) == 3
        row = _group_value(db_path)
        assert json.loads(row["value"]) == ["c"]
        assert row["written_by"] == "alice"
        with db.get_db(db_path) as conn:
            assert db.kv_get(conn, "alice", "ns", "k")["value"] == '["personal"]'

    def test_set_trim_does_not_create_an_absent_group_key(
        self, setup, db_path, tmp_path,
    ):
        ops = [{"op": "set-trim", "namespace": "ns", "key": "k",
                "keep_newest": 2, "scope": "group:fam"}]
        assert _apply(setup, "alice", ops, tmp_path) == 0
        assert _group_value(db_path) is None

    def test_two_tasks_compose(self, setup, db_path, tmp_path):
        add = {"op": "set-add", "namespace": "ns", "key": "k", "scope": "group:fam"}
        _apply(setup, "alice", [{**add, "members": ["a"]}], tmp_path)
        _apply(setup, "bob", [{**add, "members": ["b"]}], tmp_path)
        row = _group_value(db_path)
        assert json.loads(row["value"]) == ["a", "b"]
        assert row["written_by"] == "bob"

    def test_the_first_set_op_reads_under_the_write_lock(
        self, setup, db_path, tmp_path, monkeypatch,
    ):
        # A read outside a transaction lets a second worker's write land
        # between this read and its write, and one of the two adds is lost.
        seen = []
        real = db.group_kv_get
        monkeypatch.setattr(
            db, "group_kv_get",
            lambda conn, *a: seen.append(conn.in_transaction) or real(conn, *a),
        )
        ops = [{"op": "set-add", "namespace": "ns", "key": "k",
                "members": ["a"], "scope": "group:fam"}]
        assert _apply(setup, "alice", ops, tmp_path) == 1
        assert seen == [True]

    def test_per_user_set_ops_are_unchanged(self, setup, db_path, tmp_path):
        ops = [{"op": "set-add", "namespace": "ns", "key": "k", "members": ["a"]}]
        assert _apply(setup, "alice", ops, tmp_path) == 1
        with db.get_db(db_path) as conn:
            assert json.loads(db.kv_get(conn, "alice", "ns", "k")["value"]) == ["a"]
        assert _group_value(db_path) is None


class TestRefusals:
    def test_a_non_member_is_refused_and_logged(
        self, setup, db_path, tmp_path, caplog,
    ):
        ops = [{"op": "set", "namespace": "ns", "key": "k", "value": '"evil"',
                "scope": "group:fam"}]
        with caplog.at_level(logging.WARNING, logger="istota.scheduler"):
            assert _apply(setup, "mallory", ops, tmp_path) == 0
        assert _group_value(db_path) is None
        assert any("group KV write denied" in r.getMessage()
                   and "mallory" in r.getMessage() for r in caplog.records)

    def test_a_json_user_id_does_not_authorize(self, setup, db_path, tmp_path):
        ops = [{"op": "set", "namespace": "ns", "key": "k", "value": '"v"',
                "scope": "group:fam", "user_id": "alice"}]
        assert _apply(setup, "mallory", ops, tmp_path) == 0
        assert _group_value(db_path) is None
        with db.get_db(db_path) as conn:
            assert db.kv_get(conn, "mallory", "ns", "k") is None

    def test_an_ended_membership_is_refused(self, setup, db_path, tmp_path):
        with db.get_db(db_path) as conn:
            db.end_group_membership(conn, "fam", "alice", ended_by="operator")
        ops = [{"op": "set-add", "namespace": "ns", "key": "k",
                "members": ["a"], "scope": "group:fam"}]
        assert _apply(setup, "alice", ops, tmp_path) == 0
        assert _group_value(db_path) is None

    def test_an_unknown_group_is_refused(self, setup, db_path, tmp_path):
        ops = [{"op": "set", "namespace": "ns", "key": "k", "value": '"v"',
                "scope": "group:nosuch"}]
        assert _apply(setup, "alice", ops, tmp_path) == 0
        with db.get_db(db_path) as conn:
            assert db.group_kv_namespaces(conn, "nosuch") == []
            assert db.kv_get(conn, "alice", "ns", "k") is None

    @pytest.mark.parametrize("scope", [
        "group:", "group", "group:../x", "group:fam/../other", "GROUP:fam",
        "", 7, ["group:fam"], {"group": "fam"},
    ])
    def test_a_malformed_scope_is_refused(self, setup, db_path, tmp_path, scope):
        ops = [{"op": "set", "namespace": "ns", "key": "k", "value": '"v"',
                "scope": scope}]
        assert _apply(setup, "alice", ops, tmp_path) == 0
        assert _group_value(db_path) is None
        with db.get_db(db_path) as conn:
            assert db.kv_get(conn, "alice", "ns", "k") is None

    def test_a_reserved_namespace_is_refused_before_the_scope_branch(
        self, setup, db_path, tmp_path, monkeypatch,
    ):
        calls = []
        real = db.is_group_member
        monkeypatch.setattr(
            db, "is_group_member",
            lambda *a, **k: calls.append(a) or real(*a, **k),
        )
        ops = [{"op": "set", "namespace": "_vault_sync", "key": "k",
                "value": '"v"', "scope": "group:fam"}]
        assert _apply(setup, "alice", ops, tmp_path) == 0
        assert calls == []
        assert _group_value(db_path, ns="_vault_sync") is None

    def test_a_database_error_in_the_gate_is_a_refusal(
        self, setup, db_path, tmp_path, monkeypatch,
    ):
        def boom(*a, **k):
            raise RuntimeError("database is locked")
        monkeypatch.setattr(db, "is_group_member", boom)
        ops = [{"op": "set", "namespace": "ns", "key": "k", "value": '"v"',
                "scope": "group:fam"},
               {"op": "set", "namespace": "ns", "key": "mine", "value": '"p"'}]
        assert _apply(setup, "alice", ops, tmp_path) == 1
        assert _group_value(db_path) is None
        with db.get_db(db_path) as conn:
            assert db.kv_get(conn, "alice", "ns", "mine")["value"] == '"p"'

    def test_a_refused_group_op_leaves_the_rest_of_the_file(
        self, setup, db_path, tmp_path,
    ):
        ops = [{"op": "set", "namespace": "ns", "key": "k", "value": '"g"',
                "scope": "group:nosuch"},
               {"op": "set", "namespace": "ns", "key": "k", "value": '"g"',
                "scope": "group:fam"}]
        assert _apply(setup, "alice", ops, tmp_path) == 1
        assert _group_value(db_path)["value"] == '"g"'


class TestTheResolvedSet:
    """D21: the apply asks the task's resolved group set as well as membership,
    re-derived from the task row, because the op file cannot carry it."""

    def _room_task(self, db_path, token, user_id="alice", **fields):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="t", user_id=user_id,
                                     conversation_token=token, **fields)
            return db.get_task(conn, task_id)

    def _apply_task(self, config, task, ops, tmp_path):
        user_temp = tmp_path / "temp" / task.user_id
        user_temp.mkdir(parents=True, exist_ok=True)
        path = user_temp / f"task_{task.id}_kv_ops.json"
        path.write_text(json.dumps(ops))
        count = _process_deferred_kv_ops(config, task, user_temp)
        assert not path.exists()
        return count

    OPS = [{"op": "set", "namespace": "ns", "key": "k", "value": '"v"',
            "scope": "group:fam"}]

    def test_a_room_of_members_applies(self, setup, db_path, tmp_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "r1", "alice", origin="web", name="r1")
            db.add_web_room_member(conn, "r1", "bob")
        task = self._room_task(db_path, "r1")
        assert self._apply_task(setup, task, self.OPS, tmp_path) == 1

    def test_a_room_with_a_non_member_refuses_a_member(
        self, setup, db_path, tmp_path, caplog,
    ):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "r2", "alice", origin="web", name="r2")
            db.add_web_room_member(conn, "r2", "carol")
        task = self._room_task(db_path, "r2")
        with caplog.at_level(logging.WARNING, logger="istota.scheduler"):
            assert self._apply_task(setup, task, self.OPS, tmp_path) == 0
        assert _group_value(db_path) is None
        assert any("group KV write denied" in r.getMessage()
                   for r in caplog.records)

    def test_a_guest_turn_refuses(self, setup, db_path, tmp_path):
        task = self._room_task(db_path, None, guest_participant_id=5)
        assert self._apply_task(setup, task, self.OPS, tmp_path) == 0
        assert _group_value(db_path) is None

    def test_a_mixed_turn_refuses(self, setup, db_path, tmp_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "r3", "alice", origin="web", name="r3")
        task = self._room_task(db_path, "r3", audience="mixed")
        assert self._apply_task(setup, task, self.OPS, tmp_path) == 0


class TestEndToEnd:
    """A sandboxed task writes through the CLI, the scheduler applies the op
    file, and another member's task reads the value back through the CLI."""

    def _cli(self, monkeypatch, db_path, user_id, deferred_dir, task_id, argv):
        from istota.skills.kv import main as kv_main

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", user_id)
        monkeypatch.setenv("ISTOTA_TASK_GROUPS", "fam")
        if deferred_dir is None:
            monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
            monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
        else:
            monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(deferred_dir))
            monkeypatch.setenv("ISTOTA_TASK_ID", str(task_id))
        kv_main(argv)

    def test_write_apply_read_back(self, setup, db_path, tmp_path, capsys,
                                   monkeypatch):
        task = _task(db_path, "alice")
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)
        self._cli(monkeypatch, db_path, "alice", user_temp, task.id,
                  ["set-add", "ns", "plumbers", "Ana", "--group", "fam"])
        assert json.loads(capsys.readouterr().out)["deferred"] is True
        assert _group_value(db_path, key="plumbers") is None

        assert _process_deferred_kv_ops(setup, task, user_temp) == 1

        self._cli(monkeypatch, db_path, "bob", None, None,
                  ["set-members", "ns", "plumbers", "--group", "fam"])
        assert json.loads(capsys.readouterr().out)["members"] == ["Ana"]

    def test_a_hand_written_op_file_from_a_non_member(
        self, setup, db_path, tmp_path, caplog,
    ):
        # The CLI refuses a non-member before queueing, so this op file is what
        # a task writing the JSON directly would leave behind.
        ops = [{"op": "set-add", "namespace": "ns", "key": "plumbers",
                "members": ["Eve"], "scope": "group:fam"}]
        with caplog.at_level(logging.WARNING, logger="istota.scheduler"):
            assert _apply(setup, "mallory", ops, tmp_path) == 0
        assert _group_value(db_path, key="plumbers") is None
        assert any("group KV write denied" in r.getMessage()
                   for r in caplog.records)
