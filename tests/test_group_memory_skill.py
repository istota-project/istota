"""`--group` on the memory skill CLI: `Groups/<id>/GROUP.md` as a third target.

The gate is the one `kv --group` uses (multiplayer D21): the caller, read from
`ISTOTA_USER_ID`, must be a current member *and* the group must be in the
task's resolved set, `ISTOTA_TASK_GROUPS`. Every refusal reads the same, so the
CLI is no group-existence oracle. The directory is contained by equality under
`{mount}/Groups`, and every group write is audited into the group's own store.
"""

import json
import shutil

import pytest

from istota import db
from istota.memory.curation.audit import AUDIT_NAMESPACE
from istota.skills.memory import main as memory_main

SEED = "<!-- charter -->\n\n# Fam\n\n## Members\n\n## Conventions\n\n- Shoes off\n\n## Reference\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    mount = tmp_path / "mount"
    (mount / "Users" / "alice" / "istota" / "config").mkdir(parents=True)
    (mount / "Users" / "alice" / "istota" / "config" / "USER.md").write_text(
        "## Notes\n\n- mine\n"
    )
    group_dir = mount / "Groups" / "fam"
    group_dir.mkdir(parents=True)
    (group_dir / "GROUP.md").write_text(SEED)
    (mount / "Groups" / "other").mkdir()
    (mount / "Groups" / "other" / "GROUP.md").write_text("## Conventions\n")

    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    with db.get_db(db_path) as conn:
        db.create_group(conn, "fam", kind="family", display_name="Fam",
                        created_by="operator")
        db.add_group_member(conn, "fam", "alice", added_by="operator")
        db.create_group(conn, "other", kind="team", display_name="Other",
                        created_by="operator")
        db.add_group_member(conn, "other", "bob", added_by="operator")

    monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.setenv("ISTOTA_BOT_DIR_NAME", "istota")
    monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
    monkeypatch.setenv("ISTOTA_TASK_ID", "41")
    # Naming a group the caller is not in proves membership is still asked.
    monkeypatch.setenv("ISTOTA_TASK_GROUPS", "fam,other")
    monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
    monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)

    class Env:
        pass

    e = Env()
    e.mount, e.db_path, e.group_md = mount, db_path, group_dir / "GROUP.md"
    return e


def _refused(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        memory_main(argv)
    assert exc.value.code == 1
    return json.loads(capsys.readouterr().out)


def _group_audit(e, group_id="fam"):
    with db.get_db(e.db_path) as conn:
        rows = db.group_kv_list(conn, group_id, AUDIT_NAMESPACE)
    return [json.loads(r["value"]) for r in rows]


def _user_audit(e, user_id="alice"):
    with db.get_db(e.db_path) as conn:
        rows = db.kv_list(conn, user_id, AUDIT_NAMESPACE)
    return [json.loads(r["value"]) for r in rows]


class TestMemberWrites:
    def test_append_lands_in_group_md_and_not_user_md(self, env, capsys):
        memory_main(["append", "--group", "fam", "--heading", "Conventions",
                     "--line", "Bins go out Tuesday"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok" and out["outcome"] == "applied"
        assert "- Bins go out Tuesday" in env.group_md.read_text()
        user_md = env.mount / "Users" / "alice" / "istota" / "config" / "USER.md"
        assert "Bins" not in user_md.read_text()

    def test_the_charter_comment_survives_a_write(self, env, capsys):
        memory_main(["append", "--group", "fam", "--heading", "Reference",
                     "--line", "Plumber: Ana"])
        assert env.group_md.read_text().startswith("<!-- charter -->")

    def test_show_and_headings_read_the_group_file(self, env, capsys):
        memory_main(["show", "--group", "fam", "--heading", "Conventions"])
        assert "Shoes off" in capsys.readouterr().out
        memory_main(["headings", "--group", "fam"])
        out = json.loads(capsys.readouterr().out)
        assert out["headings"] == ["Members", "Conventions", "Reference"]


class TestTheGate:
    def test_a_non_member_is_refused_and_nothing_is_written(self, env, capsys):
        before = (env.mount / "Groups" / "other" / "GROUP.md").read_text()
        out = _refused(["append", "--group", "other", "--heading",
                        "Conventions", "--line", "x"], capsys)
        assert out == {"status": "error", "error": "not a member of group 'other'"}
        assert (env.mount / "Groups" / "other" / "GROUP.md").read_text() == before

    def test_an_unknown_group_reads_the_same_as_a_non_member(self, env, capsys):
        other = _refused(["show", "--group", "other"], capsys)
        unknown = _refused(["show", "--group", "nosuch"], capsys)
        assert unknown["error"] == "not a member of group 'nosuch'"
        assert set(other) == set(unknown) == {"status", "error"}

    def test_a_group_outside_the_tasks_resolved_set_is_refused(
        self, env, monkeypatch, capsys
    ):
        monkeypatch.setenv("ISTOTA_TASK_GROUPS", "other")
        out = _refused(["show", "--group", "fam"], capsys)
        assert out["error"] == "not a member of group 'fam'"

    def test_no_resolved_set_means_none(self, env, monkeypatch, capsys):
        monkeypatch.delenv("ISTOTA_TASK_GROUPS")
        out = _refused(["append", "--group", "fam", "--heading", "Reference",
                        "--line", "x"], capsys)
        assert out["error"] == "not a member of group 'fam'"
        assert "- x" not in env.group_md.read_text()

    def test_an_ended_membership_is_refused(self, env, capsys):
        with db.get_db(env.db_path) as conn:
            db.end_group_membership(conn, "fam", "alice", ended_by="operator")
        _refused(["show", "--group", "fam"], capsys)

    def test_an_unreachable_database_is_refused(self, env, monkeypatch, capsys):
        missing = env.db_path.parent / "missing.db"
        monkeypatch.setenv("ISTOTA_DB_PATH", str(missing))
        _refused(["show", "--group", "fam"], capsys)
        assert not missing.exists()

    @pytest.mark.parametrize("bad", ["..", ".", "a/b", "/abs", "FAM"])
    def test_an_invalid_id_is_refused_like_any_other(self, env, capsys, bad):
        out = _refused(["show", "--group", bad], capsys)
        assert out["error"] == f"not a member of group '{bad}'"

    def test_group_and_channel_together_is_an_error(self, env, monkeypatch, capsys):
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "room1")
        out = _refused(["show", "--group", "fam", "--channel", "room1"], capsys)
        assert out["error"] == "--group and --channel are mutually exclusive"


class TestContainment:
    def test_a_symlinked_group_dir_cannot_redirect_the_write(self, env, capsys):
        # Another group's directory: "under the root" would accept it.
        target = env.mount / "Groups" / "other"
        shutil.rmtree(env.group_md.parent)
        env.group_md.parent.symlink_to(target, target_is_directory=True)
        before = (target / "GROUP.md").read_text()
        out = _refused(["append", "--group", "fam", "--heading", "Conventions",
                        "--line", "planted"], capsys)
        assert out["error"] == "group_dir_outside_group_root"
        assert (target / "GROUP.md").read_text() == before

    def test_a_symlink_at_group_md_is_refused_on_read(self, env, tmp_path, capsys):
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP SECRET\n")
        env.group_md.unlink()
        env.group_md.symlink_to(secret)
        captured = None
        with pytest.raises(SystemExit):
            memory_main(["show", "--group", "fam"])
        captured = capsys.readouterr().out
        assert "TOP SECRET" not in captured


class TestAudit:
    def test_a_group_write_is_audited_into_the_groups_store(self, env, capsys):
        memory_main(["append", "--group", "fam", "--heading", "Conventions",
                     "--line", "Bins go out Tuesday"])
        entries = _group_audit(env)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["group_id"] == "fam"
        assert entry["user_id"] == "alice"
        assert entry["task_id"] == "41"
        assert entry["source"] == "runtime"
        assert entry["applied"] == [{
            "op": {"op": "append", "heading": "Conventions",
                   "line": "Bins go out Tuesday"},
            "outcome": "applied",
        }]
        assert entry["rejected"] == []

    def test_a_rejected_group_op_is_audited_with_its_reason(self, env, capsys):
        _refused(["append", "--group", "fam", "--heading", "Nope",
                  "--line", "x"], capsys)
        [entry] = _group_audit(env)
        assert entry["applied"] == []
        assert entry["rejected"][0]["reason"] == "heading_missing"

    def test_a_group_write_leaves_the_users_own_trail_alone(self, env, capsys):
        memory_main(["append", "--group", "fam", "--heading", "Conventions",
                     "--line", "x"])
        assert _user_audit(env) == []

    def test_a_refused_caller_leaves_no_entry(self, env, capsys):
        _refused(["append", "--group", "other", "--heading", "Conventions",
                  "--line", "x"], capsys)
        assert _group_audit(env, "other") == []

    def test_the_audit_namespace_is_reserved(self):
        from istota.kv_namespaces import is_reserved_namespace

        assert is_reserved_namespace(AUDIT_NAMESPACE)


class TestTheLock:
    def test_every_member_shares_one_anchor_for_group_md(
        self, env, tmp_path, monkeypatch
    ):
        # Two members' deferred dirs differ; GROUP.md's anchor must not.
        from istota.memory.curation.file_lock import lock_path_for
        from istota.skills import memory

        target = memory.Target(env.group_md, memory._GROUP, "fam")
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(tmp_path / "alice-tmp"))
        a = lock_path_for(env.group_md, lock_dir=memory._lock_dir(target))
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(tmp_path / "bob-tmp"))
        b = lock_path_for(env.group_md, lock_dir=memory._lock_dir(target))
        assert a == b
