"""Task usage reads keep the caller's scope and expose list-price data."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from istota import db
from istota.config import Config
from tests.support.skill_cli import run_skill_main
from istota.usage.telemetry import BrainUsage, ModelUsage


@pytest.fixture
def usage_env(tmp_path, monkeypatch):
    config = Config(db_path=tmp_path / "istota.db", admin_users={"admin"})
    db.init_db(config.db_path)
    monkeypatch.setattr("istota.config.load_config", lambda: config)
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.setenv("ISTOTA_DB_PATH", str(config.db_path))
    monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
    monkeypatch.delenv("ISTOTA_WITHHELD_SCOPES", raising=False)
    with db.get_db(config.db_path) as conn:
        for user, cost in (("alice", 12.5), ("bob", 80.0)):
            usage = BrainUsage(
                billed_input_tokens=100, output_tokens=20, cost_usd=cost,
                cost_basis="subscription", has_totals=True,
                models=[ModelUsage(model="model-a", billed_input_tokens=100,
                                   output_tokens=20, cost_usd=cost)],
            )
            row = db.insert_task_usage(
                conn, usage=usage, user_id=user, brain_kind="claude_code",
                origin="task", success=True, model="model-a", source_type="web",
            )
            conn.execute("UPDATE task_usage SET created_at = ? WHERE id = ?",
                         ("2026-08-20T12:00:00.000Z", row))
            task = db.create_task(conn, user_id=user, source_type="web", prompt="hello")
            conn.execute("UPDATE tasks SET created_at = ?, status = 'completed' WHERE id = ?",
                         ("2026-08-20 12:00:00", task))
    return config


def _run(*argv):
    from istota.skills.usage import main

    result = run_skill_main(main, ["--since", "2026-08-20", "--until", "2026-08-20", *argv])
    return result.exit_code, result.envelope


def test_member_reads_own_model_cost_and_unmeasured_tasks(usage_env):
    code, result = _run("--by", "model", "--json")
    assert code == 0
    assert result["unmeasured_tasks"] == 1
    assert result["since"] == "2026-08-20T00:00:00.000Z"
    assert result["until"] == "2026-08-21T00:00:00.000Z"
    assert len(result["groups"]) == 1
    group = result["groups"][0]
    assert group["key"] == "model-a"
    assert group["rows"] == 1
    assert group["cost_by_basis"] == {"subscription": 12.5}


@pytest.mark.parametrize("argv", [("--user", "bob"), ("--user", "alice"),
                                  ("--user", ""), ("--by", "user")])
def test_member_cannot_request_admin_scope(usage_env, argv):
    code, result = _run(*argv)
    assert code == 1
    assert result["status"] == "error"
    assert "admin" in result["error"]
    assert "groups" not in result


def test_admin_fleet_and_user_filter(usage_env, monkeypatch):
    monkeypatch.setenv("ISTOTA_USER_ID", "admin")
    code, result = _run("--by", "user")
    assert code == 0
    assert {g["key"] for g in result["groups"]} == {"alice", "bob"}
    assert result["unmeasured_tasks"] == 2
    code, result = _run("--user", "bob")
    assert code == 0
    assert result["groups"][0]["cost_by_basis"] == {"subscription": 80.0}
    assert result["unmeasured_tasks"] == 1


@pytest.mark.parametrize("admins", [set(), {"admin"}])
def test_missing_identity_never_reads_fleet(usage_env, monkeypatch, admins):
    usage_env.admin_users = admins
    monkeypatch.delenv("ISTOTA_USER_ID")
    code, result = _run()
    assert code == 1
    assert "groups" not in result


def test_withheld_scope_refuses_even_admin(usage_env, monkeypatch):
    monkeypatch.setenv("ISTOTA_USER_ID", "admin")
    monkeypatch.setenv("ISTOTA_WITHHELD_SCOPES", "memory")
    code, result = _run()
    assert code == 1
    assert "withheld" in result["error"]


@pytest.mark.parametrize("source,guest,allowed", [("web", False, True),
                                                 ("web", True, False),
                                                 ("subtask", False, False)])
def test_room_reach_derived_from_task(usage_env, monkeypatch, source, guest, allowed):
    with db.get_db(usage_env.db_path) as conn:
        room = db.create_web_chat_room(conn, "alice", "Shared")
        db.add_room_member(conn, room.token, "bob")
        task = db.create_task(conn, user_id="alice", source_type=source, prompt="usage",
                              conversation_token=room.token)
        if guest:
            conn.execute("UPDATE tasks SET guest_participant_id = 7 WHERE id = ?", (task,))
    monkeypatch.setenv("ISTOTA_TASK_ID", str(task))
    code, result = _run()
    assert code == (0 if allowed else 1)
    assert ("groups" in result) == allowed


def test_empty_window_reports_unmeasured_without_usage(usage_env):
    with db.get_db(usage_env.db_path) as conn:
        conn.execute("DELETE FROM task_usage_models")
        conn.execute("DELETE FROM task_usage")
    code, result = _run()
    assert code == 0
    assert result["groups"][0]["rows"] == 0
    assert result["unmeasured_tasks"] == 1


@pytest.mark.parametrize("argv", [("--since", "bad"), ("--until", "2026-08-19")])
def test_invalid_window_refused(usage_env, argv):
    code, result = _run(*argv)
    assert code == 1
    assert result["status"] == "error"


def test_usage_is_discoverable_for_members(usage_env):
    from istota.skills._loader import advertised_cli_skills, eligible_skill_names, load_skill_index
    from istota.rooms.scopes import scope_names

    index = load_skill_index(usage_env.skills_dir, bundled_dir=usage_env.bundled_skills_dir)
    assert "usage" in eligible_skill_names(index, exclude=set(), is_admin=False)
    assert "usage" in advertised_cli_skills(index, is_admin=False, disabled_skills=set())
    assert "usage" in scope_names(index)


def test_console_entrypoint_reads_through_real_config(usage_env, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(f'db_path = "{usage_env.db_path}"\n')
    admins = tmp_path / "admins"
    admins.write_text("admin\n")
    env = dict(os.environ, ISTOTA_CONFIG_PATH=str(config_path), ISTOTA_ADMINS_FILE=str(admins))
    env.pop("ISTOTA_SKILL_PROXY_SOCK", None)
    env.pop("ISTOTA_SANDBOXED", None)
    result = subprocess.run(
        [str(Path(sys.executable).with_name("istota-skill")),
         "usage", "--since", "2026-08-20", "--until", "2026-08-20", "--by", "model"],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["groups"][0]["cost_by_basis"] == {"subscription": 12.5}
    assert payload["unmeasured_tasks"] == 1


def test_days_must_be_positive(usage_env):
    from istota.skills.usage import main

    result = run_skill_main(main, ["--days", "0"])
    assert result.exit_code == 1
    assert result.envelope["error"] == "--days must be at least 1"
