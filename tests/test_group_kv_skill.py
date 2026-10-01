"""`--group` on the kv skill CLI: the membership gate, reads, writes, set-ops.

Modelled on `tests/test_kv_skill_shared.py`. The gate checks membership of
`ISTOTA_USER_ID` and refuses identically for a group that does not exist and
one the caller is not in, so the CLI is no group-existence oracle.
"""

import json

import pytest

from istota import db
from istota.skills.kv import main as kv_main

SET_OPS = {
    "set-contains": ["set-contains", "ns", "k", "a"],
    "set-size": ["set-size", "ns", "k"],
    "set-members": ["set-members", "ns", "k"],
    "set-add": ["set-add", "ns", "k", "a"],
    "set-remove": ["set-remove", "ns", "k", "a"],
    "set-trim": ["set-trim", "ns", "k", "--keep-newest", "1"],
}

VALUE_VERBS = {
    "get": ["get", "ns", "k"],
    "set": ["set", "ns", "k", '"v"'],
    "delete": ["delete", "ns", "k"],
    "list": ["list", "ns"],
    "namespaces": ["namespaces"],
}


@pytest.fixture
def env(db_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    # The task's resolved group set, which the proxy sets (D21). Naming a
    # group the caller is not in proves membership is still asked.
    monkeypatch.setenv("ISTOTA_TASK_GROUPS", "fam,other")
    monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
    monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
    with db.get_db(db_path) as conn:
        db.create_group(conn, "fam", kind="family", display_name="Fam",
                        created_by="operator")
        db.add_group_member(conn, "fam", "alice", added_by="operator")
        db.create_group(conn, "other", kind="team", display_name="Other",
                        created_by="operator")
        db.add_group_member(conn, "other", "bob", added_by="operator")
    return db_path


@pytest.fixture
def deferred(env, tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(tmp_path))
    monkeypatch.setenv("ISTOTA_TASK_ID", "41")
    return tmp_path / "task_41_kv_ops.json"


def _run_error(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        kv_main(argv)
    assert exc.value.code == 1
    return json.loads(capsys.readouterr().out)


class TestMemberReads:
    def test_get_reads_the_group_store_not_the_users(self, env, capsys):
        with db.get_db(env) as conn:
            db.group_kv_set(conn, "fam", "ns", "k", '"group"', "bob")
            db.kv_set(conn, "alice", "ns", "k", '"personal"')
        kv_main(["get", "ns", "k", "--group", "fam"])
        out = json.loads(capsys.readouterr().out)
        assert out == {"status": "ok", "value": "group", "written_by": "bob"}

    def test_list_and_namespaces(self, env, capsys):
        with db.get_db(env) as conn:
            db.group_kv_set(conn, "fam", "ns", "a", '"1"', "alice")
            db.group_kv_set(conn, "fam", "ns", "b", '"2"', "alice")
            db.group_kv_set(conn, "fam", "_framework", "x", '"3"', "alice")
            db.kv_set(conn, "alice", "mine", "k", '"p"')
        kv_main(["list", "ns", "--group", "fam"])
        out = json.loads(capsys.readouterr().out)
        assert [e["key"] for e in out["entries"]] == ["a", "b"]
        kv_main(["namespaces", "--group", "fam"])
        out = json.loads(capsys.readouterr().out)
        assert out["namespaces"] == ["ns"]

    def test_set_reads_go_to_the_group_store(self, env, capsys):
        with db.get_db(env) as conn:
            db.group_kv_set(conn, "fam", "ns", "k", '["a", "b"]', "alice")
        kv_main(["set-size", "ns", "k", "--group", "fam"])
        assert json.loads(capsys.readouterr().out)["size"] == 2
        kv_main(["set-contains", "ns", "k", "a", "--group", "fam"])
        assert json.loads(capsys.readouterr().out)["contains"] is True
        kv_main(["set-members", "ns", "k", "--group", "fam"])
        assert json.loads(capsys.readouterr().out)["members"] == ["a", "b"]


class TestTheGate:
    @pytest.mark.parametrize("verb", sorted({**VALUE_VERBS, **SET_OPS}))
    def test_a_non_member_is_refused_on_every_verb(self, env, capsys, verb):
        argv = {**VALUE_VERBS, **SET_OPS}[verb]
        out = _run_error(argv + ["--group", "other"], capsys)
        assert out["error"] == "not a member of group 'other'"

    def test_the_refusal_does_not_say_whether_the_group_exists(self, env, capsys):
        missing = _run_error(["get", "ns", "k", "--group", "nosuch"], capsys)
        not_mine = _run_error(["get", "ns", "k", "--group", "other"], capsys)
        assert missing.keys() == not_mine.keys()
        assert missing["error"].replace("nosuch", "X") == \
            not_mine["error"].replace("other", "X")

    def test_an_ended_membership_is_refused(self, env, capsys):
        with db.get_db(env) as conn:
            db.end_group_membership(conn, "fam", "alice", ended_by="operator")
        _run_error(["get", "ns", "k", "--group", "fam"], capsys)

    def test_an_archived_group_is_refused(self, env, capsys):
        with db.get_db(env) as conn:
            db.archive_group(conn, "fam")
        _run_error(["get", "ns", "k", "--group", "fam"], capsys)

    def test_a_malformed_id_is_refused_the_same_way(self, env, capsys):
        out = _run_error(["get", "ns", "k", "--group", "../x"], capsys)
        assert out["error"] == "not a member of group '../x'"

    def test_a_database_error_is_a_refusal(self, env, capsys, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("database is locked")
        monkeypatch.setattr(db, "is_group_member", boom)
        _run_error(["get", "ns", "k", "--group", "fam"], capsys)

    def test_no_user_id_is_a_refusal(self, env, capsys, monkeypatch):
        monkeypatch.delenv("ISTOTA_USER_ID")
        _run_error(["get", "ns", "k", "--group", "fam"], capsys)

    def test_a_refused_write_queues_nothing(self, deferred, capsys):
        _run_error(["set", "ns", "k", '"v"', "--group", "other"], capsys)
        assert not deferred.exists()

    def test_the_gate_reads_the_trusted_user_not_an_argument(self, env, capsys,
                                                             monkeypatch):
        monkeypatch.setenv("ISTOTA_USER_ID", "bob")
        _run_error(["get", "ns", "k", "--group", "fam"], capsys)

    @pytest.mark.parametrize("verb", sorted({**VALUE_VERBS, **SET_OPS}))
    def test_a_member_is_refused_a_group_outside_the_tasks_set(
        self, env, capsys, monkeypatch, verb,
    ):
        # D21: a member whose task did not resolve the group (a room with a
        # non-member in it, a guest's turn) is refused exactly like a stranger.
        monkeypatch.setenv("ISTOTA_TASK_GROUPS", "other")
        argv = {**VALUE_VERBS, **SET_OPS}[verb]
        out = _run_error(argv + ["--group", "fam"], capsys)
        assert out["error"] == "not a member of group 'fam'"

    def test_no_resolved_set_is_a_refusal(self, env, capsys, monkeypatch):
        monkeypatch.delenv("ISTOTA_TASK_GROUPS")
        _run_error(["get", "ns", "k", "--group", "fam"], capsys)

    def test_the_set_is_matched_whole_not_by_substring(self, env, capsys, monkeypatch):
        monkeypatch.setenv("ISTOTA_TASK_GROUPS", "family, fam2 ,xfam")
        _run_error(["get", "ns", "k", "--group", "fam"], capsys)

    def test_a_refused_write_outside_the_set_queues_nothing(
        self, deferred, capsys, monkeypatch,
    ):
        monkeypatch.setenv("ISTOTA_TASK_GROUPS", "")
        _run_error(["set", "ns", "k", '"v"', "--group", "fam"], capsys)
        assert not deferred.exists()

    def test_reserved_namespaces_are_still_refused(self, env, capsys):
        out = _run_error(["get", "_vault_sync", "k", "--group", "fam"], capsys)
        assert "reserved" in out["error"]


class TestFlagCombinations:
    def test_group_and_shared_together_is_an_error(self, env, capsys):
        out = _run_error(["get", "ns", "k", "--group", "fam", "--shared"], capsys)
        assert "--group" in out["error"] and "--shared" in out["error"]

    @pytest.mark.parametrize("verb", sorted(SET_OPS))
    def test_every_set_op_accepts_group(self, env, capsys, verb):
        kv_main(SET_OPS[verb] + ["--group", "fam"])
        assert json.loads(capsys.readouterr().out)["status"] == "ok"

    @pytest.mark.parametrize("verb", sorted(SET_OPS))
    def test_every_set_op_still_refuses_shared(self, env, capsys, verb):
        out = _run_error(SET_OPS[verb] + ["--shared"], capsys)
        assert "--shared" in out["error"]


class TestDeferredWrites:
    def test_set_and_delete_carry_the_group_scope(self, deferred, capsys):
        kv_main(["set", "ns", "k", '"v"', "--group", "fam"])
        assert json.loads(capsys.readouterr().out)["deferred"] is True
        kv_main(["delete", "ns", "k", "--group", "fam"])
        ops = json.loads(deferred.read_text())
        assert [o["scope"] for o in ops] == ["group:fam", "group:fam"]

    @pytest.mark.parametrize("verb", ["set-add", "set-remove", "set-trim"])
    def test_set_ops_carry_the_group_scope(self, deferred, capsys, verb):
        kv_main(SET_OPS[verb] + ["--group", "fam"])
        ops = json.loads(deferred.read_text())
        assert ops[0]["op"] == verb
        assert ops[0]["scope"] == "group:fam"

    def test_set_ops_without_group_carry_no_scope(self, deferred, capsys):
        kv_main(SET_OPS["set-add"])
        ops = json.loads(deferred.read_text())
        assert "scope" not in ops[0]

    def test_set_add_counts_against_the_group_value(self, deferred, capsys, env):
        with db.get_db(env) as conn:
            db.group_kv_set(conn, "fam", "ns", "k", '["a"]', "bob")
        kv_main(["set-add", "ns", "k", "a", "b", "--group", "fam"])
        assert json.loads(capsys.readouterr().out)["added"] == 1


class TestDirectWrites:
    def test_set_writes_the_group_store_with_provenance(self, env, capsys):
        kv_main(["set", "ns", "k", '{"x": 1}', "--group", "fam"])
        with db.get_db(env) as conn:
            row = db.group_kv_get(conn, "fam", "ns", "k")
            assert db.kv_get(conn, "alice", "ns", "k") is None
        assert row["value"] == '{"x": 1}'
        assert row["written_by"] == "alice"

    def test_delete(self, env, capsys):
        with db.get_db(env) as conn:
            db.group_kv_set(conn, "fam", "ns", "k", '"v"', "bob")
        kv_main(["delete", "ns", "k", "--group", "fam"])
        assert json.loads(capsys.readouterr().out)["deleted"] is True

    def test_set_ops_write_the_group_store(self, env, capsys):
        kv_main(["set-add", "ns", "k", "a", "b", "c", "--group", "fam"])
        kv_main(["set-remove", "ns", "k", "a", "--group", "fam"])
        kv_main(["set-trim", "ns", "k", "--keep-newest", "1", "--group", "fam"])
        with db.get_db(env) as conn:
            row = db.group_kv_get(conn, "fam", "ns", "k")
            assert db.kv_get(conn, "alice", "ns", "k") is None
        assert json.loads(row["value"]) == ["c"]
        assert row["written_by"] == "alice"
