"""Tests for the `skills` core CLI skill (Part A, Stage 2)."""

import argparse
import json

import pytest

from istota.config import Config, UserConfig


def _write_skill(bundled, name, body, *, admin_only=False, experimental=False, cli=True):
    d = bundled / name
    d.mkdir(parents=True, exist_ok=True)
    fm = ["---", f"name: {name}", "description: the {0} skill".format(name), f"cli: {'true' if cli else 'false'}"]
    if admin_only:
        fm.append("admin_only: true")
    if experimental:
        fm.append("experimental: true")
    fm.append("---")
    (d / "skill.md").write_text("\n".join(fm) + "\n" + body)
    return d


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    _write_skill(bundled, "developer", "# Developer\n\nUse {scripts_dir} for {user_id}'s scripts.\n")
    _write_skill(bundled, "secret_admin", "# Admin only\n", admin_only=True)
    _write_skill(bundled, "labs", "# Experimental\n", experimental=True)

    config = Config(
        db_path=tmp_path / "istota.db",
        temp_dir=tmp_path / "tmp",
        nextcloud_mount_path=tmp_path,
        bundled_skills_dir=bundled,
        skills_dir=tmp_path / "ops_skills",
        users={"alice": UserConfig()},
        admin_users={"boss"},  # alice is NOT admin
    )
    monkeypatch.setattr("istota.config.load_config", lambda *a, **kw: config)
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.delenv("ISTOTA_EXPERIMENTAL_FEATURES", raising=False)
    return config


class TestShow:
    def test_show_renders_body_with_substitution(self, ctx, capsys):
        from istota.skills.skills import cmd_show

        cmd_show(argparse.Namespace(name="developer"))
        out = capsys.readouterr().out
        assert "# Developer" in out
        # {scripts_dir} and {user_id} substituted.
        assert "{scripts_dir}" not in out
        assert "{user_id}" not in out
        assert "alice" in out

    def test_show_unknown_skill_errors(self, ctx, capsys):
        from istota.skills.skills import cmd_show

        with pytest.raises(SystemExit) as e:
            cmd_show(argparse.Namespace(name="nope"))
        assert e.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert "unknown skill" in payload["error"]

    def test_show_admin_only_refused_for_non_admin(self, ctx, capsys):
        from istota.skills.skills import cmd_show

        with pytest.raises(SystemExit) as e:
            cmd_show(argparse.Namespace(name="secret_admin"))
        assert e.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert "admin" in payload["error"].lower()

    def test_show_experimental_refused_when_flag_off(self, ctx, capsys):
        from istota.skills.skills import cmd_show

        with pytest.raises(SystemExit) as e:
            cmd_show(argparse.Namespace(name="labs"))
        assert e.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"

    def test_show_disabled_skill_refused(self, ctx, capsys, monkeypatch):
        ctx.users["alice"].disabled_skills = ["developer"]
        from istota.skills.skills import cmd_show

        with pytest.raises(SystemExit) as e:
            cmd_show(argparse.Namespace(name="developer"))
        assert e.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert "disabled" in payload["error"]


class TestList:
    def test_list_excludes_restricted_skills(self, ctx, capsys):
        from istota.skills.skills import cmd_list

        cmd_list(argparse.Namespace())
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        names = {s["name"] for s in payload["skills"]}
        assert "developer" in names
        # admin_only + experimental skills are filtered for this non-admin caller.
        assert "secret_admin" not in names
        assert "labs" not in names

    def test_admin_sees_admin_only(self, ctx, capsys, monkeypatch):
        ctx.admin_users = {"alice"}
        from istota.skills.skills import cmd_list

        cmd_list(argparse.Namespace())
        payload = json.loads(capsys.readouterr().out)
        names = {s["name"] for s in payload["skills"]}
        assert "secret_admin" in names
