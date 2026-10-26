"""CLI auth and keeping model credentials where they belong (the model-catalogue
spec, Stage 3).

`[brain.claude_code] auth` decides whether the `claude` CLI gets the OAuth
token or the `anthropic` provider's key, and the other is never passed. The CLI
env is built from config by allowlist; nothing credential- or endpoint-shaped is
topped up from a process environment. These tests hold the mode's resolution,
`cli_auth_env`, the env builders that use it, the strip seams, the passthrough,
hook and manifest refusals, and the per-skill copy for a skill that calls a
model.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from istota import config as config_module
from istota.brain import brain_unconfigured_reason, room_selectable_kinds
from istota.config import Config, SecurityConfig, config_from_document, load_config
from istota.llm import providers
from istota.llm.providers import (
    cli_auth_env,
    is_model_credential_name,
    model_caller_env,
    resolve_cli_auth,
    resolve_process_auth,
    strip_model_credentials,
    strip_unselected_cli_credentials,
)

OAUTH = "OAUTH-SENTINEL"
KEY = "APIKEY-SENTINEL"
OTHER = "OTHER-SENTINEL"

#: Every name a model credential can travel under, one per shape.
MODEL_NAMES = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ISTOTA_PROVIDER_ANTHROPIC_API_KEY",
    "ISTOTA_PROVIDER_OPENROUTER_EXTRA_HEADERS",
    "ISTOTA_PROVIDER_LOCAL_API_KEY",
    "ISTOTA_BRAIN_NATIVE_API_KEY",
    "ISTOTA_BRAIN_NATIVE_EXTRA_HEADERS",
)


@pytest.fixture(autouse=True)
def _no_model_variables(monkeypatch):
    for name in (*MODEL_NAMES, "ISTOTA_CLI_AUTH", "ISTOTA_PROVIDER_OPENROUTER_API_KEY",
                 "ISTOTA_PROVIDER_ANTHROPIC_EXTRA_HEADERS"):
        monkeypatch.delenv(name, raising=False)


def _doc(auth: str | None = None, **sections) -> Config:
    if auth is not None:
        sections.setdefault("brain", {}).setdefault("claude_code", {})["auth"] = auth
    return config_from_document(sections)


def _with_key(auth: str | None = "api_key", base_url: str | None = None, **sections) -> Config:
    table = {"api_key": KEY}
    if base_url:
        table["base_url"] = base_url
    sections.setdefault("providers", {})["anthropic"] = table
    return _doc(auth, **sections)


class TestThePredicate:
    @pytest.mark.parametrize("name", MODEL_NAMES)
    def test_every_model_credential_name_is_one(self, name):
        assert is_model_credential_name(name)

    @pytest.mark.parametrize("name", [
        "PATH", "HOME", "ISTOTA_CLI_AUTH", "ISTOTA_CONFIG_PATH", "NC_PASS",
        "ISTOTA_SECRET_KEY", "ANTHROPIC_MODEL",
    ])
    def test_other_names_are_not(self, name):
        assert not is_model_credential_name(name)


class TestTheMode:
    def test_an_explicit_setting_wins(self):
        assert resolve_cli_auth(_doc("api_key"), {"CLAUDE_CODE_OAUTH_TOKEN": OAUTH}) == "api_key"
        assert resolve_cli_auth(_doc("subscription"), {"ANTHROPIC_API_KEY": KEY}) == "subscription"

    def test_a_token_derives_subscription(self):
        assert resolve_cli_auth(_doc(), {"CLAUDE_CODE_OAUTH_TOKEN": OAUTH}) == "subscription"

    def test_a_provider_key_alone_derives_nothing(self):
        """D15: a key alone must not move an install onto API billing."""
        assert resolve_cli_auth(_with_key(auth=None), {}) is None

    def test_the_legacy_variable_derives_api_key(self):
        """The transition row: what such an install meant before."""
        assert resolve_cli_auth(_doc(), {"ANTHROPIC_API_KEY": KEY}) == "api_key"

    def test_a_token_beats_the_legacy_variable(self):
        env = {"CLAUDE_CODE_OAUTH_TOKEN": OAUTH, "ANTHROPIC_API_KEY": KEY}
        assert resolve_cli_auth(_doc(), env) == "subscription"

    def test_a_child_takes_the_parents_mode(self):
        assert resolve_cli_auth(_doc(), {"ISTOTA_CLI_AUTH": "api_key"}) == "api_key"

    def test_a_bogus_inherited_mode_is_ignored(self):
        assert resolve_cli_auth(_doc(), {"ISTOTA_CLI_AUTH": "root"}) is None

    def test_no_credential_is_none(self):
        assert resolve_cli_auth(_doc(), {}) is None

    def test_a_stale_credentials_file_derives_nothing(self, tmp_path, monkeypatch):
        """The derivation reads the env token only, never a file or a keychain."""
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / ".credentials.json").write_text('{"claudeAiOauth": {"accessToken": "x"}}')
        monkeypatch.setenv("HOME", str(tmp_path))
        assert resolve_cli_auth(_doc(), {"HOME": str(tmp_path)}) is None

    def test_load_config_never_resolves_it(self, tmp_path, monkeypatch):
        path = tmp_path / "config.toml"
        path.write_text('[brain]\nkind = "claude_code"\n')
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", OAUTH)
        with patch.object(providers, "resolve_cli_auth", side_effect=AssertionError("called")), \
                patch.object(providers, "resolve_process_auth", side_effect=AssertionError("called")):
            config = load_config(path)
        assert dict(config.brain.unconfigured) == {}
        assert config._cli_mode_resolved is False

    def test_the_mode_is_cached_once_per_process(self):
        config = _doc()
        assert resolve_process_auth(config, {"CLAUDE_CODE_OAUTH_TOKEN": OAUTH}) == "subscription"
        assert providers.process_cli_auth(config) == "subscription"
        with patch.object(providers, "resolve_cli_auth", side_effect=AssertionError("again")):
            assert providers.process_cli_auth(config) == "subscription"

    def test_an_unknown_auth_value_is_refused_at_load(self):
        from istota.config_mapper import collect_problems

        with collect_problems() as problems:
            config = _doc("oauth")
        assert config.brain.claude_code.auth == ""
        assert any(p["path"] == ("brain", "claude_code", "auth") and p["kind"] == "discarded"
                   for p in problems)


class TestTheCliEnv:
    def test_subscription_carries_the_token_and_nothing_else(self):
        env = {"CLAUDE_CODE_OAUTH_TOKEN": OAUTH, "ANTHROPIC_API_KEY": OTHER,
               "ANTHROPIC_BASE_URL": "http://gateway", "ANTHROPIC_AUTH_TOKEN": OTHER}
        out = cli_auth_env(_with_key("subscription"), "subscription", env=env)
        assert out == {"CLAUDE_CODE_OAUTH_TOKEN": OAUTH}

    def test_subscription_without_a_token_is_empty(self):
        assert cli_auth_env(_doc("subscription"), "subscription", env={}) == {}

    def test_api_key_carries_the_providers_key_and_url_and_no_token(self):
        config = _with_key("api_key", base_url="https://gateway.example")
        out = cli_auth_env(config, "api_key", env={"CLAUDE_CODE_OAUTH_TOKEN": OAUTH})
        assert out == {"ANTHROPIC_API_KEY": KEY, "ANTHROPIC_BASE_URL": "https://gateway.example"}

    def test_api_key_carries_the_users_own_key_in_their_task(self):
        config = _with_key("api_key")
        with patch.object(providers, "user_provider_key", return_value="USER-KEY") as lookup:
            out = cli_auth_env(config, "api_key", user="alice", env={})
        lookup.assert_called_once_with(config, "alice", "anthropic")
        assert out["ANTHROPIC_API_KEY"] == "USER-KEY"

    def test_no_mode_is_empty(self):
        assert cli_auth_env(_with_key(None), None, env={"CLAUDE_CODE_OAUTH_TOKEN": OAUTH}) == {}

    @pytest.mark.parametrize("mode", ["subscription", "api_key", None])
    def test_the_bearer_name_is_never_set(self, mode):
        out = cli_auth_env(_with_key(mode), mode, env={"CLAUDE_CODE_OAUTH_TOKEN": OAUTH,
                                                       "ANTHROPIC_AUTH_TOKEN": OTHER})
        assert "ANTHROPIC_AUTH_TOKEN" not in out


class TestTheEnvBuilders:
    """`build_clean_env` and `build_model_cli_env` agree with `cli_auth_env`
    and top up no credential or endpoint name from the process environment."""

    def _env(self, config, environ):
        from istota.executor import build_clean_env, build_model_cli_env

        with patch.dict(os.environ, {"PATH": "/usr/bin", "HOME": "/home/t", **environ}, clear=True):
            return build_clean_env(config), build_model_cli_env(config)

    def test_subscription_ignores_every_api_key_in_the_daemon_env(self):
        config = _doc("subscription")
        for env in self._env(config, {"CLAUDE_CODE_OAUTH_TOKEN": OAUTH, "ANTHROPIC_API_KEY": OTHER,
                                      "ANTHROPIC_BASE_URL": "http://elsewhere",
                                      "ANTHROPIC_AUTH_TOKEN": OTHER}):
            assert env["CLAUDE_CODE_OAUTH_TOKEN"] == OAUTH
            assert env["ISTOTA_CLI_AUTH"] == "subscription"
            for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
                assert name not in env

    def test_api_key_carries_the_config_key_never_the_token(self):
        config = _with_key("api_key")
        for env in self._env(config, {"CLAUDE_CODE_OAUTH_TOKEN": OAUTH, "ANTHROPIC_API_KEY": OTHER}):
            assert env["ANTHROPIC_API_KEY"] == KEY
            assert env["ANTHROPIC_BASE_URL"] == "https://api.anthropic.com"
            assert "CLAUDE_CODE_OAUTH_TOKEN" not in env

    def test_no_credential_still_builds(self):
        config = _doc(brain={"kind": "native", "native": {"provider": "openrouter"}})
        clean, model = self._env(config, {})
        assert clean["PATH"].endswith("/usr/bin")
        assert not any(is_model_credential_name(k) for k in clean)
        assert not any(is_model_credential_name(k) for k in model)
        assert "ISTOTA_CLI_AUTH" not in clean

    def test_reachability_names_still_come_through(self):
        config = _doc("subscription")
        _clean, model = self._env(config, {"HTTPS_PROXY": "http://proxy:3128",
                                            "SSL_CERT_FILE": "/etc/ca.pem"})
        assert model["HTTPS_PROXY"] == "http://proxy:3128"
        assert model["SSL_CERT_FILE"] == "/etc/ca.pem"

    @pytest.mark.parametrize("mode", ["subscription", "api_key"])
    def test_passthrough_refuses_every_model_credential_name(self, mode, caplog):
        config = _with_key(mode)
        config.security = SecurityConfig(passthrough_env_vars=list(MODEL_NAMES) + ["MY_VAR"])
        with caplog.at_level(logging.WARNING, logger="istota.executor"):
            clean, _model = self._env(config, {name: OTHER for name in MODEL_NAMES} | {"MY_VAR": "kept"})
        assert clean["MY_VAR"] == "kept"
        # Under "subscription" the token is the mode's own credential, read
        # from the environment by `cli_auth_env`; nothing else came through.
        carried = {k for k, v in clean.items() if v == OTHER}
        assert carried == ({"CLAUDE_CODE_OAUTH_TOKEN"} if mode == "subscription" else set())
        assert "passthrough_env_vars" in caplog.text


class TestTheStrips:
    def test_the_runtime_set_names_everything_api_key_adds(self):
        from istota.sandbox.claude_runtime_env import CLAUDE_RUNTIME_ENV_VARS

        assert {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_BASE_URL"} <= CLAUDE_RUNTIME_ENV_VARS

    def test_unselected_names_go_and_the_modes_value_is_restored(self):
        env = {
            "PATH": "/bin",
            "CLAUDE_CODE_OAUTH_TOKEN": OTHER,         # a hook added it under api_key
            "ANTHROPIC_API_KEY": OTHER,               # a merge replaced the mode's value
            "ISTOTA_PROVIDER_OPENROUTER_API_KEY": OTHER,
        }
        out = strip_unselected_cli_credentials(env, "api_key", {"ANTHROPIC_API_KEY": KEY})
        assert out == {"PATH": "/bin", "ANTHROPIC_API_KEY": KEY}

    def test_subscription_keeps_only_the_token(self):
        env = {"ANTHROPIC_API_KEY": OTHER, "CLAUDE_CODE_OAUTH_TOKEN": OTHER, "X": "1"}
        out = strip_unselected_cli_credentials(env, "subscription", {"CLAUDE_CODE_OAUTH_TOKEN": OAUTH})
        assert out == {"CLAUDE_CODE_OAUTH_TOKEN": OAUTH, "X": "1"}

    def test_strip_model_credentials_adds_nothing(self):
        env = {name: OTHER for name in MODEL_NAMES} | {"KEEP": "1"}
        assert strip_model_credentials(env) == {"KEEP": "1"}

    def test_build_stripped_env_carries_no_model_credential(self):
        from istota.executor import build_stripped_env

        with patch.dict(os.environ, {name: OTHER for name in MODEL_NAMES} | {"PATH": "/bin"}, clear=True):
            env = build_stripped_env()
        assert not any(is_model_credential_name(k) for k in env)


class TestAvailability:
    def test_no_cli_credential_makes_the_cli_brains_unavailable(self):
        config = _doc(brain={"kind": "native", "native": {"provider": "openrouter"},
                             "room_selectable": ["native", "claude_code", "tmux_claude"]})
        resolve_process_auth(config, {})
        for kind in ("claude_code", "tmux_claude"):
            reason = brain_unconfigured_reason(kind, config)
            assert reason and "[brain.claude_code] auth" in reason
        assert brain_unconfigured_reason("native", config) is None
        assert room_selectable_kinds(config.brain) == frozenset({"native"})

    def test_explicit_subscription_without_a_token_counts_as_configured(self):
        """A developer's Mac, where `claude` is logged in through the keychain."""
        config = _doc("subscription")
        resolve_process_auth(config, {})
        assert brain_unconfigured_reason("claude_code", config) is None

    def test_a_gateway_under_subscription_is_unavailable(self):
        config = _with_key("subscription", base_url="https://gateway.example")
        resolve_process_auth(config, {"CLAUDE_CODE_OAUTH_TOKEN": OAUTH})
        reason = brain_unconfigured_reason("claude_code", config)
        assert reason and "base_url" in reason

    def test_natives_reason_moved_here(self):
        config = _doc(brain={"kind": "native"})
        assert brain_unconfigured_reason("native", config) is None  # nothing filled at load
        resolve_process_auth(config, {})
        assert "[brain.native] provider" in brain_unconfigured_reason("native", config)


class TestTheModelCallerCopy:
    def test_a_cli_reviewer_under_subscription_gets_the_mode_and_the_token(self):
        config = _doc("subscription")
        out = model_caller_env(config, "subscription", "anthropic", cli_reviewer=True,
                               env={"CLAUDE_CODE_OAUTH_TOKEN": OAUTH})
        assert out == {"ISTOTA_CLI_AUTH": "subscription", "CLAUDE_CODE_OAUTH_TOKEN": OAUTH}

    def test_a_cli_reviewer_under_api_key_gets_the_providers_key(self):
        config = _with_key("api_key")
        out = model_caller_env(config, "api_key", "anthropic", cli_reviewer=True,
                               env={"CLAUDE_CODE_OAUTH_TOKEN": OAUTH})
        assert out == {"ISTOTA_CLI_AUTH": "api_key", "ISTOTA_PROVIDER_ANTHROPIC_API_KEY": KEY}

    def test_a_native_reviewer_gets_its_providers_key_and_headers(self):
        config = _doc("subscription", providers={"openrouter": {
            "api_key": "OR-KEY", "extra_headers": {"X-Test": "1"}}})
        out = model_caller_env(config, "subscription", "openrouter", cli_reviewer=False,
                               env={"CLAUDE_CODE_OAUTH_TOKEN": OAUTH})
        assert out["ISTOTA_PROVIDER_OPENROUTER_API_KEY"] == "OR-KEY"
        assert out["ISTOTA_PROVIDER_OPENROUTER_EXTRA_HEADERS"] == '{"X-Test": "1"}'
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in out

    def test_the_users_own_key_is_used(self):
        config = _with_key("api_key")
        with patch.object(providers, "user_provider_key", return_value="USER-KEY"):
            out = model_caller_env(config, "api_key", "anthropic", user="alice",
                                   cli_reviewer=True, env={})
        assert out["ISTOTA_PROVIDER_ANTHROPIC_API_KEY"] == "USER-KEY"

    def test_the_child_builds_its_cli_env_from_the_copy(self, tmp_path, monkeypatch):
        """What the copy is for: the child's own `load_config` reads it through
        the ordinary override and its `cli_auth_env` builds the CLI env."""
        path = tmp_path / "config.toml"
        path.write_text("")
        parent = _with_key("api_key")
        copy = model_caller_env(parent, "api_key", "anthropic", cli_reviewer=True, env={})
        for name, value in copy.items():
            monkeypatch.setenv(name, value)
        child = load_config(path)
        mode = providers.process_cli_auth(child)
        assert mode == "api_key"
        assert cli_auth_env(child, mode, env=dict(os.environ))["ANTHROPIC_API_KEY"] == KEY


class TestTheTransitionRow:
    def test_the_legacy_variable_is_the_anthropic_providers_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
        path = tmp_path / "config.toml"
        path.write_text("")
        config = load_config(path)
        assert config.providers["anthropic"].api_key == KEY

    def test_a_current_variable_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", OTHER)
        monkeypatch.setenv("ISTOTA_PROVIDER_ANTHROPIC_API_KEY", KEY)
        path = tmp_path / "config.toml"
        path.write_text("")
        assert load_config(path).providers["anthropic"].api_key == KEY

    def test_two_different_legacy_keys_each_keep_their_job(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", OTHER)
        path = tmp_path / "config.toml"
        path.write_text('[brain.native]\nbase_url = "https://api.anthropic.com/v1"\n')
        config = load_config(path)
        assert config.brain.native.provider == "native"
        assert config.providers["native"].kind == "anthropic"
        assert config.providers["native"].base_url == "https://api.anthropic.com"
        assert config.providers["native"].api_key == OTHER
        assert config.providers["anthropic"].api_key == KEY

    def test_the_same_key_twice_is_one_provider(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", KEY)
        path = tmp_path / "config.toml"
        path.write_text('[brain.native]\nbase_url = "https://api.anthropic.com/v1"\n')
        config = load_config(path)
        assert config.brain.native.provider == "anthropic"
        assert "native" not in config.providers

    def test_it_is_a_warning_and_nothing_is_logged(self, tmp_path, monkeypatch, caplog):
        from istota.config_mapper import collect_problems

        monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
        path = tmp_path / "config.toml"
        path.write_text("")
        with caplog.at_level(logging.DEBUG), collect_problems() as problems:
            load_config(path)
        notes = [p for p in problems if "ANTHROPIC_API_KEY" in p["message"]]
        assert [p["kind"] for p in notes] == ["warning"]
        assert KEY not in caplog.text
        assert not any("ANTHROPIC_API_KEY" in r.getMessage() for r in caplog.records)


class TestTheCredentialsFile:
    """`istota cli-auth sync-credentials-file` writes, keeps and removes the
    file for each mode and token state."""

    def _run(self, tmp_path, monkeypatch, config_text: str, **env):
        from istota import cli

        path = tmp_path / "config.toml"
        path.write_text(config_text)
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(home))
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        rc = cli.cmd_cli_auth(type("A", (), {"config": str(path)})())
        return rc, home / ".claude" / ".credentials.json"

    def test_subscription_with_a_token_writes_a_private_file_the_cli_accepts(self, tmp_path, monkeypatch, capsys):
        import json
        import stat

        rc, path = self._run(tmp_path, monkeypatch, "", CLAUDE_CODE_OAUTH_TOKEN=OAUTH)
        assert rc == 0
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        stored = json.loads(path.read_text())["claudeAiOauth"]
        assert stored["accessToken"] == OAUTH
        # The probe's finding: without the scope the CLI reads "Not logged in".
        assert stored["scopes"] == ["user:inference"]
        assert OAUTH not in capsys.readouterr().out

    def test_a_second_run_keeps_it(self, tmp_path, monkeypatch, capsys):
        self._run(tmp_path, monkeypatch, "", CLAUDE_CODE_OAUTH_TOKEN=OAUTH)
        self._run(tmp_path, monkeypatch, "", CLAUDE_CODE_OAUTH_TOKEN=OAUTH)
        assert "unchanged" in capsys.readouterr().out

    def test_api_key_removes_it(self, tmp_path, monkeypatch):
        _rc, path = self._run(tmp_path, monkeypatch, "", CLAUDE_CODE_OAUTH_TOKEN=OAUTH)
        assert path.exists()
        rc, path = self._run(tmp_path, monkeypatch, '[brain.claude_code]\nauth = "api_key"\n')
        assert rc == 0 and not path.exists()

    def test_no_token_removes_it(self, tmp_path, monkeypatch):
        _rc, path = self._run(tmp_path, monkeypatch, "", CLAUDE_CODE_OAUTH_TOKEN=OAUTH)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
        rc, path = self._run(tmp_path, monkeypatch, '[brain.claude_code]\nauth = "subscription"\n')
        assert rc == 0 and not path.exists()

    def test_the_daemon_removes_a_stale_file_under_api_key(self, tmp_path):
        from istota.sandbox.claude_runtime_env import (
            remove_stale_credentials_file,
            sync_credentials_file,
        )

        assert sync_credentials_file("subscription", OAUTH, home=str(tmp_path)) == "written"
        assert remove_stale_credentials_file(str(tmp_path)) is True
        assert remove_stale_credentials_file(str(tmp_path)) is False


class TestTheAllowlist:
    def test_api_key_adds_the_providers_host(self):
        from istota.executor import _build_network_allowlist

        config = _with_key("api_key", base_url="https://gateway.example:8443")
        resolve_process_auth(config, {})
        assert "gateway.example:8443" in _build_network_allowlist(config, [])

    def test_subscription_adds_nothing(self):
        from istota.executor import _build_network_allowlist

        config = _with_key("subscription", base_url="https://gateway.example")
        resolve_process_auth(config, {"CLAUDE_CODE_OAUTH_TOKEN": OAUTH})
        assert not any("gateway.example" in h for h in _build_network_allowlist(config, []))


def test_the_config_field_is_a_closed_vocabulary():
    assert config_module.FIELD_CHOICES[("brain", "claude_code", "auth")] == ("", "subscription", "api_key")
    assert Path  # keep the import used under every collection
