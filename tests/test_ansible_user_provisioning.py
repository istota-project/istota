"""Timezone must not be clobbered by Ansible re-provisioning (ISSUE-102 follow-up).

The ISSUE-102 fix made the *read* paths seed-only: ``hydrate_user_configs``
(Nextcloud) and ``merge_into_user_config`` (config.toml overlay) both leave an
explicit, user-set timezone alone across restarts. But the Ansible *write* path
bypassed all of it: the "Ensure user_profiles rows" task rendered
``istota user ensure ... --tz "<inventory tz>"`` on every deploy, doing an
unconditional partial UPDATE of ``user_profiles.timezone`` and then notifying a
scheduler restart. A user who picked their timezone in the web UI had it
overwritten on the next deploy.

Option B: timezone is a user-facing preference (web UI + Nextcloud), not
deployment infra. The Ansible provisioning command must not pass ``--tz`` at
all, so a redeploy can never clobber the web-set value.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from jinja2 import Environment

REPO = Path(__file__).resolve().parent.parent
TASKS_FILE = REPO / "deploy" / "ansible" / "tasks" / "main.yml"


def _ensure_profiles_command() -> str:
    """Return the ``command:`` template of the 'Ensure user_profiles rows' task."""
    tasks = yaml.safe_load(TASKS_FILE.read_text())
    for task in tasks:
        if isinstance(task, dict) and task.get("name") == "Ensure user_profiles rows":
            assert "command" in task, "task found but has no `command:` key"
            return task["command"]
    raise AssertionError("task 'Ensure user_profiles rows' not found in tasks/main.yml")


def _render(command: str, user_value: dict) -> str:
    """Render the command template the way Ansible would for one user.

    The task sets ``user_id`` via ``vars:`` from ``user_item.key``; the
    surrounding play supplies ``istota_home`` / ``istota_package`` /
    ``istota_repo_dir``. All Jinja in the command uses standard filters
    (``default``, ``is defined``), so a bare Jinja2 Environment renders it.
    """
    env = Environment()
    return env.from_string(command).render(
        istota_home="/srv/app/zorg",
        istota_package="istota",
        istota_repo_dir="/srv/app/zorg",
        user_id="alice",
        user_item={"key": "alice", "value": user_value},
    )


class TestAnsibleUserEnsureOmitsTimezone:
    def test_command_template_never_passes_tz(self):
        # An inventory timezone must not flow into the provisioning command,
        # else every deploy clobbers the web-UI-set value.
        rendered = _render(
            _ensure_profiles_command(),
            {"display_name": "Alice", "timezone": "Europe/Warsaw"},
        )
        assert "--tz" not in rendered, (
            "Ansible still passes --tz; a redeploy will overwrite the "
            "web-set timezone in user_profiles"
        )
        assert "Europe/Warsaw" not in rendered, (
            "inventory timezone leaked into the user-ensure command"
        )

    def test_command_template_still_provisions_other_fields(self):
        # Guard against an over-broad edit that drops the whole task body:
        # the non-timezone profile fields must still be provisioned.
        rendered = _render(
            _ensure_profiles_command(),
            {"display_name": "Alice", "timezone": "Europe/Warsaw"},
        )
        assert "user ensure" in rendered
        assert "--name alice" in rendered
        assert "--display-name" in rendered


class TestTimezoneSurvivesRedeploy:
    """End-to-end: web edit then redeploy preserves the user's timezone.

    Replays the lifecycle through the real CLI entrypoint with the
    post-fix invocation shape (no ``--tz``).
    """

    @pytest.fixture
    def cfg_with_db(self, tmp_path, monkeypatch):
        from istota import db

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{db_path}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
        )
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        return cfg, db_path

    def test_web_set_timezone_survives_redeploy(self, cfg_with_db):
        from istota import user_profiles
        from istota.cli import cmd_user_ensure

        from tests.test_cli_user_ensure import _FakeArgs

        cfg, db_path = cfg_with_db

        # First deploy: Ansible provisions the profile (no --tz under Option B).
        cmd_user_ensure(_FakeArgs(config=str(cfg), name="alice", display_name="Alice"))

        # User picks their timezone in the web UI.
        user_profiles.update_profile(db_path, "alice", timezone="Europe/Warsaw")

        # Redeploy: same provisioning invocation runs again.
        cmd_user_ensure(_FakeArgs(config=str(cfg), name="alice", display_name="Alice"))

        profile = user_profiles.get_profile(db_path, "alice")
        assert profile is not None
        assert profile.timezone == "Europe/Warsaw"
