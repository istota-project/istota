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
        istota_home="/srv/app/istota",
        istota_package="istota",
        istota_repo_dir="/srv/app/istota",
        user_id="alice",
        user_item={"key": "alice", "value": user_value},
    )


class TestAnsibleUserEnsureOmitsTimezone:
    def test_command_template_never_passes_tz(self):
        # An inventory timezone must not flow into the provisioning command,
        # else every deploy clobbers the web-UI-set value.
        rendered = _render(
            _ensure_profiles_command(),
            {"display_name": "Alice", "timezone": "Europe/Lisbon"},
        )
        assert "--tz" not in rendered, (
            "Ansible still passes --tz; a redeploy will overwrite the "
            "web-set timezone in user_profiles"
        )
        assert "Europe/Lisbon" not in rendered, (
            "inventory timezone leaked into the user-ensure command"
        )

    def test_command_template_still_provisions_other_fields(self):
        # Guard against an over-broad edit that drops the whole task body:
        # the non-timezone profile fields must still be provisioned.
        rendered = _render(
            _ensure_profiles_command(),
            {"display_name": "Alice", "timezone": "Europe/Lisbon"},
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
        user_profiles.update_profile(db_path, "alice", timezone="Europe/Lisbon")

        # Redeploy: same provisioning invocation runs again.
        cmd_user_ensure(_FakeArgs(config=str(cfg), name="alice", display_name="Alice"))

        profile = user_profiles.get_profile(db_path, "alice")
        assert profile is not None
        assert profile.timezone == "Europe/Lisbon"


class TestAnsibleOutboundApprovalSurface:
    """The outbound approval gate must be operable from the inventory.

    Carried out of the Stage 4 review of the outbound-email-approval spec:
    ``[email] outbound_approval_floor`` defaults to ``untrusted`` in the
    dataclass, so the gate switches itself on for every existing deployment at
    upgrade — and with no Ansible surface there was no supported way to turn it
    back off, since the role overwrites hand edits to ``config.toml`` on the
    next play. These pin the three pieces that make it operable, each of which
    is silently inert without the other two.
    """

    @staticmethod
    def _defaults() -> dict:
        return yaml.safe_load(
            (REPO / "deploy" / "ansible" / "defaults" / "main.yml").read_text()
        )

    @staticmethod
    def _template() -> str:
        return (
            REPO / "deploy" / "ansible" / "templates" / "config.toml.j2"
        ).read_text()

    def test_the_floor_has_a_default_matching_the_dataclass(self):
        from istota.config import EmailConfig

        value = self._defaults()["istota_email_outbound_approval_floor"]
        # Running the role must not re-decide the policy on its own. A default
        # here that disagrees with the code changes behaviour for every
        # deployment that never set the variable.
        assert value == EmailConfig().outbound_approval_floor

    def test_the_template_renders_the_floor(self):
        assert "istota_email_outbound_approval_floor" in self._template(), (
            "the variable exists but nothing renders it into config.toml, so "
            "setting it in the inventory would do nothing"
        )

    def test_the_rendered_floor_survives_a_config_load(self, tmp_path):
        """End to end on the value that matters: an operator turning it off.

        An invalid floor raises at config load rather than falling back, so a
        template rendering (say) an unquoted bareword takes the daemon down on
        the next deploy instead of degrading.
        """
        from istota.config import load_config

        # The template as a whole uses Ansible-only filters (`to_json`), so a
        # bare Jinja2 Environment cannot compile it. Render the one line under
        # test, which is what this is about anyway.
        source = next(
            ln for ln in self._template().splitlines()
            if ln.startswith("outbound_approval_floor")
        )
        line = Environment().from_string(source).render(
            istota_email_outbound_approval_floor="off",
        )
        cfg = tmp_path / "config.toml"
        cfg.write_text(f"[email]\n{line}\n")
        assert load_config(cfg).email.outbound_approval_floor == "off"

    def test_user_ensure_threads_the_per_user_policy(self):
        rendered = _render(
            _ensure_profiles_command(),
            {"display_name": "Alice", "outbound_approval": "all"},
        )
        assert '--outbound-approval "all"' in rendered

    def test_an_empty_per_user_policy_is_still_passed(self):
        """`""` is a value, not an omission — it clears the user back to
        following the operator floor. A truthiness test on this key would put
        that out of reach from the inventory, which is why the template asks
        ``is defined`` here and truthiness elsewhere."""
        rendered = _render(
            _ensure_profiles_command(),
            {"display_name": "Alice", "outbound_approval": ""},
        )
        assert '--outbound-approval ""' in rendered

    def test_an_absent_per_user_policy_passes_nothing(self):
        # Omitting the key leaves a web- or CLI-set value alone, the same
        # non-clobber rule timezone has.
        rendered = _render(
            _ensure_profiles_command(), {"display_name": "Alice"},
        )
        assert "--outbound-approval" not in rendered

    def test_user_ensure_threads_external_turn_display(self):
        rendered = _render(
            _ensure_profiles_command(),
            {"display_name": "Alice", "external_turn_display": "hidden"},
        )
        assert '--external-turn-display "hidden"' in rendered

    def test_an_absent_external_turn_display_passes_nothing(self):
        rendered = _render(
            _ensure_profiles_command(), {"display_name": "Alice"},
        )
        assert "--external-turn-display" not in rendered

    @pytest.mark.parametrize(
        "flag,value",
        [("--outbound-approval", "off"), ("--external-turn-display", "full")],
    )
    def test_the_cli_parser_accepts_the_flags_the_role_renders(
        self, flag, value, monkeypatch,
    ):
        """The role and the parser live in different files, and a rendered flag
        argparse does not know fails the play at deploy time — inside a task
        whose ``changed_when: false`` makes it easy to skim past."""
        import istota.cli as cli

        seen = {}
        monkeypatch.setattr(cli, "cmd_user_ensure", lambda args: seen.update(vars(args)))
        monkeypatch.setattr(
            "sys.argv",
            ["istota", "user", "ensure", "--name", "alice", flag, value],
        )
        cli.main()

        assert seen[flag.lstrip("-").replace("-", "_")] == value
