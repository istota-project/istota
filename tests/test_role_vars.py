"""`istota migrate role-vars`: the old bare-metal role's vault, as the management role's variables.

The values are credentials, so every test that runs the CLI also checks that no
value reaches stdout or stderr; only names may.
"""

from __future__ import annotations

import re
import stat
import tomllib
from pathlib import Path

import pytest
import yaml

from istota import setup_wizard
from istota.maintenance import role_vars
from istota.maintenance.bare_metal_import import Refusal
from tests.test_management_role import _defaults, _render

REPO = Path(__file__).resolve().parent.parent
OLD_SECRETS_TEMPLATE = REPO / "deploy" / "ansible" / "templates" / "secrets.env.j2"

CONFIG_TOML = """\
bot_name = "Helper"
db_path = "/data/db/istota.db"

[nextcloud]
url = "https://cloud.example.com"
username = "helper"

[speech_gate]
mode = "classifier"

[[briefing_shared_blocks]]
name = "world-headlines"
kind = "headlines"
"""

HOST_ENV = """\
STORAGE=nextcloud
INGRESS=proxied
DOMAIN=bot.example.com
TLS_CERT_SOURCE=self-signed
UPSTREAM_PROXY=192.0.2.10
LISTEN_ADDR=10.0.0.5
LISTEN_PORT=8080
RELEASE_SIGNING_KEY=ssh-ed25519 AAAAexample releases
CLAUDE_CODE_VERSION=
ACME_EMAIL=
ACME_SERVER=
RCLONE_REMOTE=nextcloud
ZRAM_ENABLED=1
ISTOTA_DEVBOX_SUBNET=172.30.0.0/24
"""

STACK_ENV = """\
COMPOSE_PROJECT_NAME=istota
COMPOSE_PROFILES=browser,whatsapp-baileys
DOMAIN=bot.example.com
ISTOTA_TAG=v0.45.0
ISTOTA_PREVIOUS_TAG=v0.44.0
"""


def _old() -> dict:
    return {
        "istota_nextcloud_url": "https://cloud.example.com",
        "istota_nextcloud_username": "helper",
        "istota_nextcloud_app_password": "nc-pass-0001",
        "istota_email_imap_password": "imap-pass-0002",
        "istota_web_secret_key": "session-0003",
        "istota_claude_code_oauth_token": "oauth-0004",
        "istota_developer_gitlab_token": "",
        "istota_caldav_password": "caldav-0005",
        "istota_secret_key": "master-0006",
        "istota_web_token_key": "webtoken-0007",
        "istota_rclone_password_obscured": "obscured-0008",
        "istota_repo_clone_token": "clone-0009",
        "istota_talk_enabled": True,
        "istota_admin_users": ["alice"],
        "istota_user_profile_mode": "enforce",
        "istota_users": {
            "alice": {
                "display_name": "Alice",
                "email_addresses": ["alice@example.com", "a@example.org"],
                "sms_phone_number": "",
                "whatsapp_number": "+15550100",
                "trusted_email_senders": ["bob@example.com"],
                "routing": {"briefing": "email"},
                "default_briefings": False,
                "outbound_approval": "untrusted",
                "resources": [
                    {"type": "folder", "path": "/data/shared/papers", "permissions": "read", "label": "Papers"},
                    {"type": "calendar", "path": "/Calendars/alice"},
                ],
                "briefings": [
                    {"name": "morning", "cron": "0 7 * * 1-5", "title": "Morning", "output": "email"},
                    {"name": "evening", "cron": "0 19 * * *", "enabled": False},
                ],
            },
        },
        "istota_user_secrets": {
            "alice": [{"service": "karakeep", "key": "api_key", "value": "kk-0010"}],
        },
    }


def _convert(old=None, **inputs):
    inputs.setdefault("config", tomllib.loads(CONFIG_TOML))
    inputs.setdefault("host_env", role_vars.parse_env(HOST_ENV, "host.env"))
    inputs.setdefault("stack_env", role_vars.parse_env(STACK_ENV, ".env"))
    return role_vars.convert(_old() if old is None else old, **inputs)


# --- secrets ---------------------------------------------------------------


def test_each_old_secret_lands_under_its_stack_name():
    secrets = _convert().vars["istota_secrets"]
    assert secrets == {
        "istota_nextcloud_app_password": "nc-pass-0001",
        "istota_email_imap_password": "imap-pass-0002",
        # The old role defaulted the SMTP password to the IMAP one.
        "istota_email_smtp_password": "imap-pass-0002",
        "istota_web_session_secret_key": "session-0003",
        "claude_code_oauth_token": "oauth-0004",
    }
    assert set(secrets) <= set(setup_wizard.SECRET_NAMES)


def test_a_caldav_password_without_its_url_is_left_out_as_the_old_role_did():
    result = _convert()
    assert "istota_caldav_password" not in result.vars["istota_secrets"]
    assert any("istota_caldav_password" in note for note in result.notes)

    old = _old() | {"istota_caldav_url": "https://dav.example.com"}
    assert _convert(old).vars["istota_secrets"]["istota_caldav_password"] == "caldav-0005"


def test_a_credential_with_no_stack_file_refuses_and_names_it_without_its_value():
    old = _old() | {"istota_sms_twilio_auth_token": "twilio-0011", "istota_whatsapp_app_secret": "wa-0012"}
    with pytest.raises(Refusal) as refused:
        _convert(old)
    message = str(refused.value)
    assert "istota_sms_twilio_auth_token" in message and "istota_whatsapp_app_secret" in message
    assert "twilio-0011" not in message and "wa-0012" not in message


def test_the_master_key_and_web_token_key_are_kept_in_the_vault_for_recovery():
    result = _convert()
    assert result.vars["vault_istota_secret_key"] == "master-0006"
    assert result.vars["vault_istota_web_token_key"] == "webtoken-0007"


def test_a_plain_reference_to_another_variable_is_resolved():
    old = _old() | {"istota_email_smtp_password": "{{ istota_email_imap_password }}"}
    assert _convert(old).vars["istota_secrets"]["istota_email_smtp_password"] == "imap-pass-0002"


def test_the_old_secrets_template_and_the_table_agree():
    """Every credential secrets.env.j2 rendered is mapped to a stack file or named as having none."""
    lines = re.findall(r"^([A-Z_]+)=\{\{ (istota_\w+)", OLD_SECRETS_TEMPLATE.read_text(), re.M)
    assert lines
    for env_name, variable in lines:
        if variable in role_vars.KEY_VARS:
            continue
        if variable in role_vars.NO_STACK_FILE:
            assert env_name.lower() not in setup_wizard.SECRET_NAMES
            continue
        assert role_vars.SECRET_VARS[variable] == env_name.lower(), variable
    assert set(role_vars.SECRET_VARS.values()) <= set(setup_wizard.SECRET_NAMES)


# --- rclone ----------------------------------------------------------------


def test_rclone_conf_is_rendered_from_the_old_variables_and_the_live_remote():
    conf = _convert().vars["istota_rclone_conf"]
    assert conf == (
        "[nextcloud]\n"
        "type = webdav\n"
        "url = https://cloud.example.com/remote.php/dav/files/helper/\n"
        "vendor = nextcloud\n"
        "user = helper\n"
        "pass = obscured-0008\n"
    )


def test_a_given_rclone_conf_wins():
    conf = "[nextcloud]\ntype = webdav\npass = live\n"
    assert _convert(rclone_conf=conf).vars["istota_rclone_conf"] == conf


def test_an_unobscured_password_is_obscured_through_the_given_function():
    old = _old()
    del old["istota_rclone_password_obscured"]
    seen = []

    def obscure(value):
        seen.append(value)
        return "freshly-obscured"

    conf = _convert(old, obscure=obscure).vars["istota_rclone_conf"]
    assert seen == ["nc-pass-0001"]
    assert "pass = freshly-obscured\n" in conf


# --- users -----------------------------------------------------------------


def test_users_become_the_ensure_flags_the_new_role_passes():
    result = _convert()
    assert result.vars["istota_admins"] == ["alice"]
    assert result.vars["istota_users"] == {
        "alice": {
            "managed": True,
            "display-name": "Alice",
            "email": ["alice@example.com", "a@example.org"],
            "clear-sms-number": True,
            "whatsapp-number": "+15550100",
            "trusted-sender": ["bob@example.com"],
            "route": ["briefing=email"],
            "no-default-briefings": True,
            "outbound-approval": "untrusted",
        },
    }


def test_resources_keep_folders_only_with_their_extras():
    result = _convert()
    assert result.vars["istota_user_resources"] == {
        "alice": [{"type": "folder", "path": "/data/shared/papers", "permissions": "read",
                   "extras-json": '{"label": "Papers"}'}],
    }
    assert any("calendar" in note for note in result.notes)


def test_briefings_map_to_briefing_ensure_and_a_title_is_reported():
    result = _convert()
    assert result.vars["istota_user_briefings"] == {
        "alice": [
            {"name": "morning", "cron": "0 7 * * 1-5", "output": "email"},
            {"name": "evening", "cron": "0 19 * * *", "output": "talk", "disabled": True},
        ],
    }
    assert any("title" in note and "morning" in note for note in result.notes)


def test_user_secrets_carry_over_as_they_are():
    assert _convert().vars["istota_user_secrets"] == {
        "alice": [{"service": "karakeep", "key": "api_key", "value": "kk-0010"}],
    }


def test_seed_mode_passes_seed():
    old = _old() | {"istota_user_profile_mode": "seed"}
    user = _convert(old).vars["istota_users"]["alice"]
    assert user["seed"] is True and "managed" not in user


def test_an_unknown_user_key_is_reported():
    old = _old()
    old["istota_users"]["alice"]["favourite_colour"] = "green"
    assert any("favourite_colour" in note for note in _convert(old).notes)


# --- the VM's own files ----------------------------------------------------


def test_the_live_config_becomes_istota_config():
    assert _convert().vars["istota_config"] == tomllib.loads(CONFIG_TOML)


def test_host_env_round_trips_through_the_roles_own_derivation():
    """What the role would write back from our variables is the host.env we read."""
    result = _convert()
    variables = _defaults() | result.vars
    rendered = _render(variables)
    live = role_vars.parse_env(HOST_ENV, "host.env")
    written = {k: str(v) for k, v in (rendered["istota_host_env"] | rendered["istota_host_env_extra"]).items()}
    assert written == live
    assert rendered["istota_stack_env"]["COMPOSE_PROFILES"] == "browser,whatsapp-baileys"
    assert result.vars["istota_release"] == "v0.45.0"


def test_an_unsigned_running_tag_is_reported():
    stack = STACK_ENV + "ISTOTA_TAG_UNSIGNED=1\n"
    result = _convert(stack_env=role_vars.parse_env(stack, ".env"))
    assert result.vars["istota_update_mode"] == "release"
    assert any("unsigned" in note for note in result.notes)


def test_a_vm_following_main_is_latest_mode_and_needs_no_key():
    stack = STACK_ENV.replace("ISTOTA_TAG=v0.45.0", "ISTOTA_TAG=main-0123456789ab") + "ISTOTA_TAG_UNSIGNED=1\n"
    host = HOST_ENV.replace("RELEASE_SIGNING_KEY=ssh-ed25519 AAAAexample releases", "RELEASE_SIGNING_KEY=")
    result = _convert(stack_env=role_vars.parse_env(stack, ".env"), host_env=role_vars.parse_env(host, "host.env"))
    assert result.vars["istota_update_mode"] == "latest"
    assert "istota_release" not in result.vars
    assert not any("signing_key" in note for note in result.notes)


def test_dropped_old_variables_are_named():
    notes = _convert().notes
    assert any("istota_repo_clone_token" in note for note in notes)
    assert any("istota_talk_enabled" in note for note in notes)


# --- the CLI ---------------------------------------------------------------


def _write_inputs(tmp_path: Path, old: dict) -> list[str]:
    (tmp_path / "old.yml").write_text(yaml.safe_dump(old))
    (tmp_path / "config.toml").write_text(CONFIG_TOML)
    (tmp_path / "host.env").write_text(HOST_ENV)
    (tmp_path / ".env").write_text(STACK_ENV)
    return [
        "--old-vars", str(tmp_path / "old.yml"),
        "--config", str(tmp_path / "config.toml"),
        "--host-env", str(tmp_path / "host.env"),
        "--stack-env", str(tmp_path / ".env"),
    ]


def _values(old: dict):
    yield from (v for v in yaml.safe_dump(old).split() if re.search(r"-\d{4}$", v))


def test_the_cli_writes_a_private_vars_file_and_prints_no_value(tmp_path, capsys):
    out = tmp_path / "vars.yml"
    code = role_vars.main(_write_inputs(tmp_path, _old()) + ["--output", str(out)])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    written = yaml.safe_load(out.read_text())
    assert written == _convert().vars
    for value in _values(_old()):
        assert value not in captured.out and value not in captured.err


def test_the_cli_refuses_and_writes_nothing(tmp_path, capsys):
    old = _old() | {"istota_whatsapp_access_token": "wa-0013"}
    out = tmp_path / "vars.yml"
    code = role_vars.main(_write_inputs(tmp_path, old) + ["--output", str(out)])
    captured = capsys.readouterr()
    assert code == 1
    assert not out.exists()
    assert "istota_whatsapp_access_token" in captured.err
    assert "wa-0013" not in captured.err and "wa-0013" not in captured.out


def test_a_yaml_error_names_the_position_and_not_the_line(tmp_path, capsys):
    args = _write_inputs(tmp_path, _old())
    (tmp_path / "old.yml").write_text('istota_nextcloud_app_password: "bad-0015\n  : [\n')
    assert role_vars.main(args + ["--output", str(tmp_path / "vars.yml")]) == 1
    captured = capsys.readouterr()
    assert "line" in captured.err
    assert "bad-0015" not in captured.err and "bad-0015" not in captured.out


def test_later_old_vars_files_win(tmp_path, capsys):
    args = _write_inputs(tmp_path, _old())
    (tmp_path / "host.yml").write_text(yaml.safe_dump({"istota_nextcloud_app_password": "host-0014"}))
    out = tmp_path / "vars.yml"
    assert role_vars.main(args + ["--old-vars", str(tmp_path / "host.yml"), "--output", str(out)]) == 0
    written = yaml.safe_load(out.read_text())
    assert written["istota_secrets"]["istota_nextcloud_app_password"] == "host-0014"


def test_the_cli_is_reached_through_istota_migrate(tmp_path, monkeypatch):
    from istota import cli

    out = tmp_path / "vars.yml"
    argv = ["istota", "migrate", "role-vars"] + _write_inputs(tmp_path, _old()) + ["--output", str(out)]
    monkeypatch.setattr("sys.argv", argv)
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 0
    assert out.exists()
