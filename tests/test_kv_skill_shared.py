"""Tests for the --shared flag on the kv skill CLI."""

import json

import pytest

from istota import db
from istota.config import Config
from istota.skills import kv as kv_skill
from istota.skills.kv import main as kv_main


@pytest.fixture
def _shared_env(db_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
    return db_path


def _patch_config(monkeypatch, db_path, admin_users):
    monkeypatch.setattr(
        kv_skill, "_load_config",
        lambda: Config(db_path=db_path, admin_users=set(admin_users)),
    )


class TestSharedRead:
    def test_get_shared_open_to_any_user(self, _shared_env, db_path, capsys, monkeypatch):
        with db.get_db(db_path) as conn:
            db.shared_kv_set(conn, "ns", "k", '"hi"', "admin")
        monkeypatch.setenv("ISTOTA_USER_ID", "nonadmin")
        kv_main(["get", "ns", "k", "--shared"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["value"] == "hi"

    def test_list_shared(self, _shared_env, db_path, capsys):
        with db.get_db(db_path) as conn:
            db.shared_kv_set(conn, "ns", "a", '"1"', "admin")
            db.shared_kv_set(conn, "ns", "b", '"2"', "admin")
        kv_main(["list", "ns", "--shared"])
        out = json.loads(capsys.readouterr().out)
        assert out["count"] == 2

    def test_namespaces_shared(self, _shared_env, db_path, capsys):
        with db.get_db(db_path) as conn:
            db.shared_kv_set(conn, "world", "k", "1", "admin")
        kv_main(["namespaces", "--shared"])
        out = json.loads(capsys.readouterr().out)
        assert out["namespaces"] == ["world"]

    def test_shared_read_isolated_from_peruser(self, _shared_env, db_path, capsys):
        with db.get_db(db_path) as conn:
            db.kv_set(conn, "alice", "ns", "k", '"personal"')
        kv_main(["get", "ns", "k", "--shared"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "not_found"


class TestSharedDirectWrite:
    def test_admin_direct_set_allowed(self, _shared_env, db_path, capsys, monkeypatch):
        _patch_config(monkeypatch, db_path, {"alice"})
        kv_main(["set", "ns", "k", '{"text":"x"}', "--shared"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        with db.get_db(db_path) as conn:
            row = db.shared_kv_get(conn, "ns", "k")
        assert row["value"] == '{"text":"x"}'
        assert row["written_by"] == "alice"

    def test_non_admin_direct_set_denied(self, _shared_env, db_path, capsys, monkeypatch):
        _patch_config(monkeypatch, db_path, {"bob"})  # alice not admin
        with pytest.raises(SystemExit) as exc:
            kv_main(["set", "ns", "k", '"x"', "--shared"])
        assert exc.value.code == 1
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "admin" in out["error"]
        with db.get_db(db_path) as conn:
            assert db.shared_kv_get(conn, "ns", "k") is None

    def test_blank_allowlist_fails_closed(self, _shared_env, db_path, capsys, monkeypatch):
        _patch_config(monkeypatch, db_path, set())
        with pytest.raises(SystemExit):
            kv_main(["set", "ns", "k", '"x"', "--shared"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"

    def test_admin_direct_delete(self, _shared_env, db_path, capsys, monkeypatch):
        with db.get_db(db_path) as conn:
            db.shared_kv_set(conn, "ns", "k", '"x"', "alice")
        _patch_config(monkeypatch, db_path, {"alice"})
        kv_main(["delete", "ns", "k", "--shared"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["deleted"] is True


class TestSharedDeferredWrite:
    def test_set_shared_emits_scope(self, db_path, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(tmp_path))
        monkeypatch.setenv("ISTOTA_TASK_ID", "77")
        kv_main(["set", "ns", "k", '"v"', "--shared"])
        out = json.loads(capsys.readouterr().out)
        assert out["deferred"] is True
        ops = json.loads((tmp_path / "task_77_kv_ops.json").read_text())
        assert ops[0]["scope"] == "shared"

    def test_set_user_omits_scope(self, db_path, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(tmp_path))
        monkeypatch.setenv("ISTOTA_TASK_ID", "78")
        kv_main(["set", "ns", "k", '"v"'])
        ops = json.loads((tmp_path / "task_78_kv_ops.json").read_text())
        assert "scope" not in ops[0]


class TestSetOpsRejectShared:
    def test_set_add_shared_rejected(self, _shared_env, capsys):
        with pytest.raises(SystemExit) as exc:
            kv_main(["set-add", "ns", "k", "member", "--shared"])
        assert exc.value.code == 1
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "set-op" in out["error"] or "whole-value" in out["error"]


class TestSharedStatus:
    """`kv shared-status` reports whether this identity may write shared KV.

    The predicate is is_shared_kv_writer, NOT is_admin — a blank admins file
    authorizes nobody here (fail-closed), so the check must target the real
    gate rather than admin-ness by analogy.
    """

    def test_admin_can_write(self, _shared_env, db_path, capsys, monkeypatch):
        _patch_config(monkeypatch, db_path, {"alice"})
        kv_main(["shared-status"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["can_write_shared"] is True
        assert out["user_id"] == "alice"
        assert out["admins_configured"] is True

    def test_non_admin_cannot_write(self, _shared_env, db_path, capsys, monkeypatch):
        _patch_config(monkeypatch, db_path, {"bob"})  # alice not admin
        kv_main(["shared-status"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["can_write_shared"] is False
        assert out["admins_configured"] is True

    def test_blank_allowlist_fails_closed(self, _shared_env, db_path, capsys, monkeypatch):
        _patch_config(monkeypatch, db_path, set())
        kv_main(["shared-status"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["can_write_shared"] is False
        assert out["admins_configured"] is False

    def test_status_is_read_only_no_deferral(self, db_path, tmp_path, capsys, monkeypatch):
        # Even in sandbox mode (deferred dir set) this is a pure read — it must
        # answer directly, never write a deferred op.
        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(tmp_path))
        monkeypatch.setenv("ISTOTA_TASK_ID", "99")
        _patch_config(monkeypatch, db_path, {"alice"})
        kv_main(["shared-status"])
        out = json.loads(capsys.readouterr().out)
        assert out["can_write_shared"] is True
        assert not (tmp_path / "task_99_kv_ops.json").exists()

    def test_config_unavailable_fails_closed(self, _shared_env, db_path, capsys, monkeypatch):
        monkeypatch.setattr(kv_skill, "_load_config", lambda: None)
        kv_main(["shared-status"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["can_write_shared"] is False
