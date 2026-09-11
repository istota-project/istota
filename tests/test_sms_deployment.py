"""Deployment wiring for the provider-neutral SMS webhook surface."""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml
from jinja2 import Environment

from tests.test_ansible_config_template import (
    DEFAULTS_FILE,
    TASKS_FILE,
    render as render_ansible_config,
    render_secrets,
)
from tests.test_render_config import REQUIRED, render as render_docker_config


REPO = Path(__file__).resolve().parent.parent
COMPOSE = REPO / "docker" / "docker-compose.yml"
DOCKER_NGINX = REPO / "docker" / "nginx" / "default.conf.template"
DOCKER_ENTRYPOINT = REPO / "docker" / "istota" / "entrypoint.sh"
ANSIBLE = REPO / "deploy" / "ansible"

SMS_VALUES = {
    "ISTOTA_SMS_ENABLED": "true",
    "ISTOTA_SMS_PROVIDER": "twilio",
    "ISTOTA_SMS_SERVICE_NUMBERS": "+15551230000,+15551230001",
    "ISTOTA_SMS_DEFAULT_SENDER_NUMBER": "+15551230000",
    "ISTOTA_SMS_MAX_SEGMENTS": "4",
    "ISTOTA_SMS_REQUEST_TIMEOUT_SECONDS": "8",
    "ISTOTA_SMS_TWILIO_ACCOUNT_SID": "AC-placeholder",
    "ISTOTA_SMS_TWILIO_AUTH_TOKEN": "auth-placeholder",
    "ISTOTA_SMS_TWILIO_API_KEY_SID": "SK-placeholder",
    "ISTOTA_SMS_TWILIO_API_KEY_SECRET": "secret-placeholder",
    "ISTOTA_SMS_TWILIO_MESSAGING_SERVICE_SID": "MG-placeholder",
    "ISTOTA_SMS_TELNYX_API_KEY": "telnyx-api-placeholder",
    "ISTOTA_SMS_TELNYX_PUBLIC_KEY": "telnyx-public-placeholder",
    "ISTOTA_SMS_TELNYX_MESSAGING_PROFILE_ID": "telnyx-profile-placeholder",
}


def test_docker_render_writes_provider_qualified_sms_config(tmp_path):
    path = render_docker_config(tmp_path, **REQUIRED, **SMS_VALUES)
    sms = tomllib.loads(path.read_text())["sms"]

    assert sms["enabled"] is True
    assert sms["provider"] == "twilio"
    assert sms["service_numbers"] == ["+15551230000", "+15551230001"]
    assert sms["default_sender_number"] == "+15551230000"
    assert sms["max_segments"] == 4
    assert sms["request_timeout_seconds"] == 8
    assert sms["twilio"] == {
        "account_sid": "AC-placeholder",
        "auth_token": "auth-placeholder",
        "api_key_sid": "SK-placeholder",
        "api_key_secret": "secret-placeholder",
        "messaging_service_sid": "MG-placeholder",
    }
    assert sms["telnyx"] == {
        "api_key": "telnyx-api-placeholder",
        "public_key": "telnyx-public-placeholder",
        "messaging_profile_id": "telnyx-profile-placeholder",
    }


def test_compose_passes_sms_inputs_and_shares_one_webhook_service():
    compose = yaml.safe_load(COMPOSE.read_text())
    services = compose["services"]
    webhook = services["webhooks"]

    assert set(webhook["profiles"]) == {"location", "sms"}
    assert "ports" not in webhook
    assert webhook["expose"] == ["${ISTOTA_WEBHOOKS_PORT:-8765}"]
    # Deliberately no assertion on the location default. Flipping it to
    # `false` disables a module for every Docker deployment that never set it,
    # which is an unannounced behaviour change and no part of the SMS work —
    # the two subsystems share this service through `profiles`, and nothing in
    # SMS needs location off. Pinning the flip here would have made the
    # out-of-scope change the tested behaviour.
    for name in SMS_VALUES:
        assert name in services["istota"]["environment"]


def test_docker_nginx_keeps_webhooks_behind_the_shared_proxy():
    nginx = DOCKER_NGINX.read_text()

    assert "set $upstream_webhooks" in nginx
    assert "location /webhooks/" in nginx
    assert "proxy_pass http://$upstream_webhooks" in nginx


def test_location_banner_uses_the_public_nginx_address():
    entrypoint = DOCKER_ENTRYPOINT.read_text()
    banner = entrypoint.split("# --- Module activation summary", 1)[1].split(
        "# --- Application secret key", 1,
    )[0]

    assert "ISTOTA_WEB_SITE_HOSTNAME" in banner
    assert "ISTOTA_WEBHOOKS_PORT" not in banner


def test_ansible_renders_sms_and_keeps_provider_secrets_out_of_config():
    overrides = {
        "istota_sms_enabled": True,
        "istota_sms_provider": "telnyx",
        "istota_sms_service_numbers": ["+15551230000"],
        "istota_sms_default_sender_number": "+15551230000",
        "istota_sms_telnyx_api_key": "api-placeholder",
        "istota_sms_telnyx_public_key": "public-placeholder",
        "istota_sms_telnyx_messaging_profile_id": "profile-placeholder",
        "istota_sms_twilio_account_sid": "AC-placeholder",
        "istota_sms_twilio_auth_token": "auth-placeholder",
        "istota_sms_twilio_api_key_sid": "SK-placeholder",
        "istota_sms_twilio_api_key_secret": "secret-placeholder",
        "istota_sms_twilio_messaging_service_sid": "MG-placeholder",
    }
    parsed = tomllib.loads(render_ansible_config(**overrides))

    assert parsed["sms"]["provider"] == "telnyx"
    assert parsed["sms"]["service_numbers"] == ["+15551230000"]
    assert "api_key" not in parsed["sms"]["telnyx"]
    secrets = render_secrets(**overrides)
    assert "ISTOTA_SMS_TELNYX_API_KEY=api-placeholder" in secrets
    assert "ISTOTA_SMS_TELNYX_PUBLIC_KEY=public-placeholder" in secrets
    assert "ISTOTA_SMS_TELNYX_MESSAGING_PROFILE_ID=profile-placeholder" in secrets
    assert "ISTOTA_SMS_TWILIO_ACCOUNT_SID=AC-placeholder" in secrets
    assert "ISTOTA_SMS_TWILIO_AUTH_TOKEN=auth-placeholder" in secrets
    assert "ISTOTA_SMS_TWILIO_API_KEY_SID=SK-placeholder" in secrets
    assert "ISTOTA_SMS_TWILIO_API_KEY_SECRET=secret-placeholder" in secrets
    assert "ISTOTA_SMS_TWILIO_MESSAGING_SERVICE_SID=MG-placeholder" in secrets


def test_ansible_uses_one_derived_webhook_condition_everywhere():
    defaults = yaml.safe_load(DEFAULTS_FILE.read_text())
    tasks = yaml.safe_load(TASKS_FILE.read_text())

    assert defaults["istota_webhooks_enabled"] is False
    derive = next(task for task in tasks if task.get("name") == "Resolve webhook receiver need")
    expression = derive["set_fact"]["istota_webhooks_enabled"]
    assert "istota_location_enabled" in expression
    assert "istota_sms_enabled" in expression
    assert "istota_sms_twilio_auth_token" in expression
    assert "istota_sms_telnyx_public_key" in expression

    webhook_tasks = [
        task for task in tasks
        if "webhook receiver" in str(task.get("name", "")).lower()
        and task.get("name") != "Resolve webhook receiver need"
    ]
    assert webhook_tasks
    assert all("istota_webhooks_enabled" in str(task.get("when", "")) for task in webhook_tasks)

    for task_name in (
        "Deploy secrets environment file",
        "Deploy istota configuration",
    ):
        task = next(task for task in tasks if task.get("name") == task_name)
        assert "restart istota-webhooks" in task["notify"]


def test_ansible_nginx_exposes_webhooks_for_sms_without_location():
    defaults = yaml.safe_load(DEFAULTS_FILE.read_text())
    variables = {
        **defaults,
        "istota_web_enabled": False,
        "istota_location_enabled": False,
        "istota_webhooks_enabled": True,
    }
    template = (ANSIBLE / "templates" / "istota.conf.j2").read_text()
    rendered = Environment().from_string(template).render(**variables)

    assert "location /webhooks/" in rendered
