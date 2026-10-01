"""Operator auth and credential settings survive the Ansible render and loader."""

import tomllib

import pytest
from starlette.requests import Request

from istota import db, doctor, web_app, web_auth
from istota.config import Config
from tests.test_ansible_config_template import load_config_from, render


# Non-default overrides prove that the loader did not just keep its fallback.
SETTINGS = [
    ("web", "auth_enrol_ttl_hours", 48),
    ("web", "auth_reset_ttl_hours", 2),
    ("web", "auth_login_link_ttl_minutes", 5),
    ("web", "auth_min_password_length", 16),
    ("web", "auth_throttle_window_seconds", 300),
    ("web", "auth_throttle_max_email", 4),
    ("web", "auth_throttle_max_ip", 8),
    ("web", "auth_mail_link_max_email", 2),
    ("web", "trusted_proxy_hops", 1),
    ("security", "vault_fetch_limit_per_task", 0),
    ("security", "vault_writes_per_task", 0),
    ("security.credential_broker", "enabled", True),
    ("security.credential_broker", "enforce_reveal", True),
    ("security.credential_broker", "scan_max_bytes", 4096),
    ("security.credential_broker", "leaf_validity_hours", 12),
]


@pytest.fixture(scope="module")
def rendered_defaults():
    return tomllib.loads(render())


@pytest.mark.parametrize("section,field,override", SETTINGS)
def test_role_defaults_preserve_application_policy(rendered_defaults, section, field, override):
    rendered = rendered_defaults
    expected = Config()
    for part in section.split("."):
        rendered = rendered[part]
        expected = getattr(expected, part)
    assert rendered[field] == getattr(expected, field)


@pytest.mark.parametrize("section,field,override", SETTINGS)
def test_inventory_overrides_reach_loaded_config(section, field, override):
    variable = "istota_" + section.replace(".", "_") + "_" + field
    loaded = load_config_from(render(**{variable: override}))
    for part in section.split("."):
        loaded = getattr(loaded, part)
    actual = getattr(loaded, field)
    assert actual == override
    assert type(actual) is type(override)


@pytest.mark.parametrize("hops", [0, 1, 2])
def test_rendered_proxy_policy_controls_real_ip_throttle(tmp_path, monkeypatch, hops):
    config = load_config_from(render(
        istota_hostname="bot.example.com",
        istota_web_auth=["email"],
        istota_web_trusted_proxy_hops=hops,
        istota_web_auth_throttle_max_ip=1,
    ))
    config.db_path = tmp_path / "auth.db"
    db.init_db(config.db_path)
    monkeypatch.setattr(web_app, "_config", config)
    request = Request({
        "type": "http",
        "headers": [(b"x-forwarded-for", b"192.0.2.10, 192.0.2.20")],
    })
    ip = web_app._client_ip(request)
    assert ip == {0: None, 1: "192.0.2.20", 2: "192.0.2.10"}[hops]

    policy = web_auth.policy_from_config(config)
    assert web_auth.check_and_record(config.db_path, policy, email="alice@example.com", ip=ip)
    assert web_auth.check_and_record(config.db_path, policy, email="bob@example.com", ip=ip) == (hops == 0)
    checks = {check.name: check for check in doctor.check_web_auth(config, False)}
    assert checks["web.auth.proxy_ip"].status == (doctor.WARN if hops == 0 else doctor.OK)


def test_broker_table_preserves_sibling_security_settings():
    config = load_config_from(render(
        istota_web_enabled=False,
        istota_security_credential_broker_enabled=True,
        istota_security_credential_broker_enforce_reveal=True,
        istota_security_vault_fetch_limit_per_task=2,
        istota_security_vault_writes_per_task=0,
        istota_security_skill_proxy_timeouts={"browse": 90},
        istota_security_sandbox_cache_dir="/srv/cache",
        istota_security_sandbox_cache_max_gb=8.0,
        istota_security_network_extra_hosts=["api.example.com:443"],
    ))
    security = config.security
    assert security.credential_broker.enabled is True
    assert security.credential_broker.enforce_reveal is True
    assert security.vault_fetch_limit_per_task == 2
    assert security.vault_writes_per_task == 0
    assert security.skill_proxy_timeouts == {"browse": 90}
    assert security.sandbox_cache_dir == "/srv/cache"
    assert security.sandbox_cache_max_gb == 8.0
    assert security.network.extra_hosts == ["api.example.com:443"]
