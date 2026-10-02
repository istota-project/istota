"""Seam 6: the `skills` CLI honours what the calling task withholds (multiplayer Stage 16).

`istota-skill skills list|show|overlay` runs host-side in a subprocess and used
to compute "disabled" with no task at all, so a guest's turn was offered, and
could load, the bodies and the per-user overlays it may not reach. Since
ISSUE-576 only a guest's turn withholds anything; a member's turn in a shared
room sees what their private room does. The executor's seams are what enforce;
this pins the backstop.
"""
import argparse
import json

import pytest

from istota import db
from istota.config import Config, UserConfig


def _skill(bundled, name, *, shared_room="private"):
    d = bundled / name
    d.mkdir(parents=True)
    (d / "skill.md").write_text(
        f"---\nname: {name}\ndescription: the {name} skill\n"
        f"shared_room: {shared_room}\n---\n\n# {name} body\n"
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    mount = tmp_path / "mount"
    bundled = tmp_path / "bundled"
    _skill(bundled, "calendar")
    _skill(bundled, "weather", shared_room="safe")
    config = Config(
        db_path=tmp_path / "istota.db",
        temp_dir=tmp_path / "tmp",
        workspace_path=mount,
        bundled_skills_dir=bundled,
        skills_dir=tmp_path / "ops_skills",
        users={"alice": UserConfig(), "bob": UserConfig()},
        admin_users={"alice"},
    )
    db.init_db(config.db_path)
    with db.get_db(config.db_path) as conn:
        room = db.create_web_chat_room(conn, "alice", "plans")
        db.add_room_member(conn, room.token, "bob")
        solo = db.create_web_chat_room(conn, "alice", "solo")
    overlays = mount / "Users" / "alice" / config.bot_dir_name / "config" / "skills"
    overlays.mkdir(parents=True)
    (overlays / "weather.md").write_text("- alice lives on Elm Street\n")
    monkeypatch.setattr("istota.config.load_config", lambda *a, **kw: config)
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.delenv("ISTOTA_EXPERIMENTAL_FEATURES", raising=False)
    monkeypatch.delenv("ISTOTA_WITHHELD_SCOPES", raising=False)
    monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
    return config, room.token, solo.token


def _task_in(config, monkeypatch, token, **kw):
    with db.get_db(config.db_path) as conn:
        task_id = db.create_task(conn, prompt="p", user_id="alice", source_type="web",
                                 conversation_token=token, **kw)
    monkeypatch.setenv("ISTOTA_TASK_ID", str(task_id))
    return task_id


def _list(capsys):
    from istota.skills.skills import cmd_list

    cmd_list(argparse.Namespace())
    return {s["name"] for s in json.loads(capsys.readouterr().out)["skills"]}


def _show(name, capsys):
    from istota.skills.skills import cmd_show

    try:
        cmd_show(argparse.Namespace(name=name))
    except SystemExit:
        pass
    return capsys.readouterr().out


class TestTheSkillsCliReadsTheRoom:
    def test_a_members_turn_in_a_shared_room_is_offered_everything(
        self, env, monkeypatch, capsys,
    ):
        config, shared, _solo = env
        _task_in(config, monkeypatch, shared)
        assert _list(capsys) == {"calendar", "weather"}
        assert "# calendar body" in _show("calendar", capsys)

    def test_a_private_room_task_is(self, env, monkeypatch, capsys):
        config, _shared, solo = env
        _task_in(config, monkeypatch, solo)
        assert _list(capsys) == {"calendar", "weather"}
        assert "# calendar body" in _show("calendar", capsys)

    def test_a_guests_turn_is_offered_only_safe_skills(self, env, monkeypatch, capsys):
        config, shared, _solo = env
        _task_in(config, monkeypatch, shared, guest_participant_id=1)
        assert _list(capsys) == {"weather"}
        assert "disabled" in _show("calendar", capsys)

    def test_the_executors_set_is_honoured_without_a_task(self, env, monkeypatch, capsys):
        monkeypatch.setenv("ISTOTA_WITHHELD_SCOPES", "calendar")
        assert _list(capsys) == {"weather"}

    def test_an_unreadable_database_withholds_everything(self, env, monkeypatch, capsys):
        config, shared, _solo = env
        _task_in(config, monkeypatch, shared)
        monkeypatch.setattr("istota.rooms.scopes.withheld_for_task",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("gone")))
        assert _list(capsys) == {"weather"}


class TestOverlaysAreMemory:
    def test_show_leaves_the_overlay_out_on_a_guests_turn(self, env, monkeypatch, capsys):
        config, shared, _solo = env
        _task_in(config, monkeypatch, shared, guest_participant_id=1)
        out = _show("weather", capsys)
        assert "# weather body" in out
        assert "Elm Street" not in out

    def test_and_keeps_it_on_a_members_turn(self, env, monkeypatch, capsys):
        config, shared, _solo = env
        _task_in(config, monkeypatch, shared)
        assert "Elm Street" in _show("weather", capsys)

    def test_overlay_and_overlays_refuse_a_guests_turn(self, env, monkeypatch, capsys):
        from istota.skills.skills import cmd_overlay, cmd_overlays

        config, shared, _solo = env
        _task_in(config, monkeypatch, shared, guest_participant_id=1)
        for call in (lambda: cmd_overlay(argparse.Namespace(name="weather")),
                     lambda: cmd_overlays(argparse.Namespace())):
            with pytest.raises(SystemExit):
                call()
            out = capsys.readouterr().out
            assert "memory_withheld" in out and "Elm Street" not in out
