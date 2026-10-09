"""Native login configuration and token-log contracts across deployments."""

import re
import tomllib
from pathlib import Path
from string import Template

import pytest
import yaml
from jinja2 import Environment

from istota import db, doctor, user_profiles
from istota.webui import auth as web_auth
from istota.config import Config, WebConfig
from testbed import profiles
from testbed.stack import render_config
from tests.test_render_config import REQUIRED, render
from tests.test_entrypoint_config_stage import boot
from tests.test_ansible_config_template import render as render_ansible

REPO = Path(__file__).resolve().parent.parent
# Email sign-in is a typed code (ISSUE-574), so set-password is the one route
# left that carries a credential in its query string.
TOKEN_PATHS = ("/istota/auth/set-password",)
NGINX = ("docker/nginx/default.conf.template", "deploy/ansible/templates/istota.conf.j2")
LAUNCHERS = ("docker/docker-compose.yml", "deploy/ansible/templates/istota-web.service.j2")


@pytest.mark.parametrize("nc_url", ["", "http://nextcloud:80"])
@pytest.mark.parametrize("auth", [None, "email", "nextcloud,email"])
def test_web_render_without_oauth(tmp_path, nc_url, auth):
    env = {**REQUIRED, "NC_URL": nc_url}
    if auth:
        env["ISTOTA_WEB_AUTH"] = auth
    rendered = tomllib.loads(render(tmp_path, **env).read_text())
    assert rendered["web"]["auth"] == (auth.split(",") if auth else ["nextcloud"])
    assert rendered["web"]["enabled"] is True
    assert len(rendered["web"]["session_secret_key"]) >= 32
    assert "oauth2_client_id" not in rendered["web"]


def test_email_secret_survives_entrypoint_rerender(tmp_path):
    first = boot(tmp_path, NC_URL="", ISTOTA_WEB_AUTH="email")
    second = boot(tmp_path, NC_URL="", ISTOTA_WEB_AUTH="email")
    assert first["web"]["session_secret_key"] == second["web"]["session_secret_key"]
    assert (tmp_path / ".web_session_secret").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("profile", [p for p in profiles.ALL if p.shape == "lean"], ids=lambda p: p.name)
def test_lean_profiles_keep_nextcloud_auth(tmp_path, profile):
    path = render_config(REPO / "docker/istota/render-config.sh", tmp_path, {}, extra=profile.config)
    rendered = tomllib.loads(path.read_text())
    assert rendered["web"]["auth"] == ["nextcloud"]
    config = Config(web=WebConfig(**{k: rendered["web"][k] for k in ("auth", "session_secret_key")}))
    assert all(r.status == doctor.SKIP for r in doctor.check_web_auth(config, False)[1:])


@pytest.mark.parametrize("auth", [["nextcloud"], ["email"], ["nextcloud", "email"]])
def test_ansible_auth(auth):
    assert tomllib.loads(render_ansible(istota_web_auth=auth))["web"]["auth"] == auth


def nginx_render(path):
    text = (REPO / path).read_text()
    if path.endswith(".j2"):
        text = Environment().from_string(text).render(istota_web_enabled=True, istota_web_port=8766)
    else:
        text = Template(text).safe_substitute(DOMAIN="bot.example.com", WEB_PORT="8766", WEBHOOKS_PORT="8765", NGINX_CLIENT_MAX_BODY_SIZE="512m")
    return text


def assert_nginx_suppression(text):
    for path in TOKEN_PATHS:
        match = re.search(r"location\s+=\s+" + re.escape(path) + r"\s*\{([^}]+)\}", text)
        assert match, f"missing exact token location: {path}"
        block = match.group(1)
        assert "access_log off;" in block, f"token access logging enabled: {path}"
        assert "proxy_pass http://" in block
        for header in ("Host $http_host", "X-Real-IP $remote_addr", "X-Forwarded-For $proxy_add_x_forwarded_for", "X-Forwarded-Proto $scheme"):
            assert f"proxy_set_header {header};" in block


@pytest.mark.parametrize("path", NGINX)
def test_nginx_token_logs_suppressed(path):
    assert_nginx_suppression(nginx_render(path))


@pytest.mark.parametrize("path", NGINX)
@pytest.mark.parametrize("token_path", TOKEN_PATHS)
def test_nginx_negative_control(path, token_path):
    text = nginx_render(path)
    assert_nginx_suppression(text)
    start = text.index("location = " + token_path)
    end = text.index("}", start)
    broken = text[:start] + text[start:end].replace("access_log off;", "access_log on;") + text[end:]
    with pytest.raises(AssertionError, match="token access logging enabled"):
        assert_nginx_suppression(broken)


def launcher_command(path):
    text = (REPO / path).read_text()
    if path.endswith(".yml"):
        return yaml.safe_load(text)["services"]["web"]["entrypoint"][-1].split("/app/.venv/bin/uvicorn", 1)[1]
    text = Environment().from_string(text).render(istota_web_port=8766)
    return next(line for line in text.splitlines() if line.startswith("ExecStart="))


def assert_launcher_suppression(command):
    assert "--no-access-log" in command.split(), "uvicorn token access logging enabled"


@pytest.mark.parametrize("path", LAUNCHERS)
def test_uvicorn_token_logs_suppressed(path):
    command = launcher_command(path)
    assert_launcher_suppression(command)
    with pytest.raises(AssertionError, match="uvicorn token access logging enabled"):
        assert_launcher_suppression(command.replace("--no-access-log", ""))


@pytest.fixture
def auth_config(tmp_path, monkeypatch):
    monkeypatch.delenv("ISTOTA_WEB_AUTH", raising=False)
    monkeypatch.delenv("ISTOTA_WEB_SESSION_SECRET_KEY", raising=False)
    monkeypatch.delenv("ISTOTA_WEB_ALLOW_INSECURE_SESSION", raising=False)
    config = Config(db_path=tmp_path / "auth.db", web=WebConfig(auth="email", session_secret_key="test-session-secret"))
    config.site.hostname = "bot.example.com"
    config.web.trusted_proxy_hops = 1
    config.email.enabled = True
    config.admin_users = {"alice"}
    db.init_db(config.db_path)
    user_profiles.ensure_profile(config.db_path, "alice")
    web_auth.upsert_identity(config.db_path, "alice", "alice@example.com")
    return config


def results(config):
    return {r.name.removeprefix("web.auth."): r for r in doctor.check_web_auth(config, False)}


def test_doctor_healthy_passwordless_and_registry(auth_config):
    found = results(auth_config)
    assert set(found) == {"methods", "site_hostname", "mail", "identities", "admins", "proxy_ip", "session_secret"}
    assert all(r.status == doctor.OK and r.scope == doctor.DEPLOYMENT for r in found.values())
    assert doctor.CHECK_SCOPES["web.auth"] == doctor.DEPLOYMENT
    assert ("web.auth", doctor.check_web_auth) in doctor.CHECKS
    assert "config/default" in found["methods"].detail


def test_doctor_missing_login_requirements(auth_config):
    auth_config.site.hostname = ""
    auth_config.web.session_secret_key = ""
    auth_config.email.enabled = False
    found = results(auth_config)
    assert found["site_hostname"].status == doctor.FAIL
    assert found["session_secret"].status == doctor.FAIL
    assert found["mail"].status == doctor.WARN
    assert "1" in found["mail"].detail
    assert found["identities"].status == doctor.WARN


def test_doctor_profile_identity_counts_and_admins(auth_config):
    user_profiles.ensure_profile(auth_config.db_path, "bob")
    web_auth.upsert_identity(auth_config.db_path, "orphan", "orphan@example.com", create_profile=True)
    with db.get_db(auth_config.db_path) as conn:
        conn.execute("DELETE FROM user_profiles WHERE user_id = ?", ("orphan",))
    auth_config.admin_users = set()
    found = results(auth_config)
    assert found["identities"].status == doctor.WARN
    assert "1 without" in found["identities"].detail
    assert "1 orphan" in found["identities"].detail
    assert found["admins"].status == doctor.FAIL


def test_doctor_method_source_proxy_and_secret_override(auth_config, monkeypatch):
    monkeypatch.setenv("ISTOTA_WEB_AUTH", "email")
    monkeypatch.setenv("ISTOTA_WEB_SESSION_SECRET_KEY", "override-session-secret")
    auth_config.web.session_secret_key = ""
    auth_config.web.trusted_proxy_hops = 0
    found = results(auth_config)
    assert "ISTOTA_WEB_AUTH" in found["methods"].detail
    assert found["proxy_ip"].status == doctor.WARN
    assert found["session_secret"].status == doctor.OK
    assert "override-session-secret" not in repr(found)


def test_doctor_empty_methods_and_database_failure(auth_config):
    auth_config.web.auth = []
    assert results(auth_config)["methods"].status == doctor.FAIL
    auth_config.web.auth = ["email"]
    auth_config.db_path = auth_config.db_path.parent / "missing" / "auth.db"
    found = results(auth_config)
    assert found["identities"].status == doctor.FAIL
    assert found["admins"].status == doctor.FAIL


def test_auth_override_reaches_both_compose_services():
    services = yaml.safe_load((REPO / "docker/docker-compose.yml").read_text())["services"]
    for name in ("istota", "web"):
        assert services[name]["environment"]["ISTOTA_WEB_AUTH"] == "${ISTOTA_WEB_AUTH:-nextcloud}"
