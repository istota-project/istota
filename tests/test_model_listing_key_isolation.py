"""Model credentials stay where they belong (the model-catalogue spec, Stage 3).

A value test across every environment istota builds: a sentinel in the
model-listing key's file, another under every model credential name in the
process environment, and a key per provider in config. Each auth mode then
builds every env, and each sentinel may appear only where its mode carries it:
the model-listing key nowhere, the OAuth token nowhere under "api_key", a
provider's key only in its own provider and its own `model_caller_env` copy.

The parts that need discovery (the single call site of
`reveal_for_model_listing`, the fixed URL, no redirects) are Stage 4's and
join this file there.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types

import pytest

from istota import db, executor
from istota.config import Config, SchedulerConfig, SecurityConfig, CredentialBrokerConfig, UserConfig
from istota.llm.providers import (
    build_providers,
    is_model_credential_name,
    resolve_process_auth,
    reveal_inference_key,
)
from istota.skills._types import EnvSpec, SkillMeta

LISTING = "LISTING-SENTINEL"
OAUTH = "OAUTH-SENTINEL"
AMBIENT = "AMBIENT-SENTINEL"
ANTHROPIC_KEY = "ANTHROPIC-KEY-SENTINEL"
OPENROUTER_KEY = "OPENROUTER-KEY-SENTINEL"

#: Every name a model credential travels under, each holding the ambient
#: sentinel in the daemon's environment (the token holds its own).
AMBIENT_NAMES = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "ISTOTA_PROVIDER_ANTHROPIC_API_KEY", "ISTOTA_PROVIDER_ANTHROPIC_EXTRA_HEADERS",
    "ISTOTA_PROVIDER_OPENROUTER_API_KEY", "ISTOTA_PROVIDER_LOCAL_API_KEY",
    "ISTOTA_BRAIN_NATIVE_API_KEY", "ISTOTA_BRAIN_NATIVE_EXTRA_HEADERS",
)
ALL_NAMES = ("CLAUDE_CODE_OAUTH_TOKEN", *AMBIENT_NAMES)


def _values(env) -> set[str]:
    return set((env or {}).values())


def _carries(env, sentinel) -> bool:
    return any(sentinel in value for value in _values(env))


@pytest.fixture
def world(tmp_path, db_path, monkeypatch, make_task):
    listing = tmp_path / "listing-key"
    listing.write_text(LISTING)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", OAUTH)
    for name in AMBIENT_NAMES:
        monkeypatch.setenv(name, AMBIENT)
    monkeypatch.setattr(executor, "_bwrap_available", lambda: True)

    # A setup_env hook that echoes every model credential name, and a manifest
    # naming each: both are refused by name.
    hooked = types.ModuleType("istota.skills.echoer")
    hooked.setup_env = lambda ctx: {name: AMBIENT for name in ALL_NAMES} | {"HOOK_OK": "1"}
    monkeypatch.setitem(sys.modules, "istota.skills.echoer", hooked)

    mount = tmp_path / "mount"
    mount.mkdir()
    temp = tmp_path / "temp"
    (temp / "alice").mkdir(parents=True)
    (temp / ".control" / "alice" / "task_5").mkdir(parents=True)

    def config_for(mode: str) -> Config:
        config = Config(
            db_path=db_path, workspace_path=mount, temp_dir=temp,
            admin_users={"alice"}, users={"alice": UserConfig()},
            scheduler=SchedulerConfig(task_timeout_minutes=1),
            security=SecurityConfig(
                sandbox_enabled=True, skill_proxy_enabled=True,
                passthrough_env_vars=list(ALL_NAMES),
                credential_broker=CredentialBrokerConfig(enabled=False, enforce_reveal=False),
            ),
        )
        config.brain.claude_code.auth = mode
        config.providers["anthropic"].api_key = ANTHROPIC_KEY
        config.providers["openrouter"].api_key = OPENROUTER_KEY
        config.models.anthropic_listing_key_file = str(listing)
        resolve_process_auth(config, dict(os.environ))
        return config

    return config_for, make_task


def _skill_index():
    return {
        "echoer": SkillMeta(name="echoer", description="x", cli=True, skill_dir=None),
        "manifest": SkillMeta(name="manifest", description="x", cli=True, env_specs=[
            EnvSpec(var=name, source="config", config_path="nextcloud.url", sensitive=True)
            for name in ALL_NAMES
        ] + [EnvSpec(var="MANIFEST_OK", source="config", config_path="providers.anthropic.api_key")]),
        "code_review": SkillMeta(name="code_review", description="x", cli=True),
        "kv": SkillMeta(name="kv", description="x", cli=True),
    }


def _every_env(config, make_task, monkeypatch, tmp_path) -> dict[str, dict]:
    """Every environment istota builds, by name."""
    from istota import heartbeat, scheduler
    from istota.sandbox import task_env
    from istota.skills import _env as skills_env

    envs: dict[str, dict] = {}
    envs["build_clean_env"] = executor.build_clean_env(config)
    envs["build_model_cli_env"] = executor.build_model_cli_env(config)
    envs["build_stripped_env"] = executor.build_stripped_env()

    # The hook dispatcher iterates modules with a skill_dir; give the echoer one.
    index = _skill_index()
    index["echoer"] = SkillMeta(name="echoer", description="x", cli=True, skill_dir=tmp_path)
    task = make_task(id=5, user_id="alice")
    runtime = task_env.build_task_runtime(
        config, task,
        user_temp_dir=config.temp_dir / "alice",
        control_dir=config.temp_dir / ".control" / "alice" / "task_5",
        task_attempt=1,
        selected_skills=["manifest", "code_review"],
        skill_index=index,
        is_admin=True,
        user_resources=[],
        user_config=None,
    )
    envs["task_env"] = runtime.env
    envs["skill_proxy_base_env"] = runtime.proxy_ctx.base_env
    proxy = runtime.proxy_ctx
    for skill, names in proxy.skill_credential_map.items():
        envs[f"skill:{skill}"] = {n: proxy.credential_env[n] for n in names if n in proxy.credential_env}

    captured: dict[str, dict] = {}

    def fake_run(*args, **kwargs):
        captured["env"] = dict(kwargs.get("env") or {})
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("istota.scheduler._run_capture", fake_run)
    monkeypatch.setattr(
        "istota.skills._loader.load_skill_index", lambda *a, **k: index,
    )
    for skill in ("kv", "code_review"):
        scheduler._execute_skill_task(
            db.Task(id=5, status="running", source_type="scheduled", user_id="alice",
                    prompt="", skill=skill, skill_args="[]"),
            config,
        )
        envs[f"scheduler_skill_task:{skill}"] = captured.pop("env")
    envs["heartbeat"] = heartbeat._build_heartbeat_skill_env(config, "alice")

    box = executor.build_daemon_sandbox(config, "alice")
    envs["daemon_sandbox_argv"] = {str(i): part for i, part in enumerate(box.wrap(["claude"]) if box.wrap else [])}
    assert skills_env  # imported for the dispatcher the runtime used
    return envs


@pytest.mark.parametrize("mode", ["subscription", "api_key"])
def test_each_sentinel_appears_only_where_its_mode_carries_it(world, mode, monkeypatch, tmp_path):
    config_for, make_task = world
    config = config_for(mode)
    envs = _every_env(config, make_task, monkeypatch, tmp_path)

    # Positive controls: the hook and the manifest still deliver what is not a
    # model credential, and the mode's own credential reaches the CLI env.
    assert envs["task_env"].get("HOOK_OK") == "1"
    if mode == "subscription":
        assert envs["build_clean_env"]["CLAUDE_CODE_OAUTH_TOKEN"] == OAUTH
    else:
        assert envs["build_clean_env"]["ANTHROPIC_API_KEY"] == ANTHROPIC_KEY

    for name, env in envs.items():
        assert not _carries(env, LISTING), name
        # Nothing the daemon's environment held under a model credential name
        # reaches any env: no top-up, no passthrough, no hook, no manifest.
        assert not _carries(env, AMBIENT), name
        assert not _carries(env, OPENROUTER_KEY), name
        if mode == "api_key":
            assert not _carries(env, OAUTH), name

    # The anthropic key: the CLI's own env and the reviewer's copy, under
    # "api_key", and nowhere a skill other than the reviewer runs.
    allowed_anthropic = {"build_clean_env", "build_model_cli_env", "task_env",
                         "skill:code_review", "scheduler_skill_task:code_review"}
    for name, env in envs.items():
        if mode == "subscription" or name not in allowed_anthropic:
            assert not _carries(env, ANTHROPIC_KEY), name

    # The token: the CLI env and the reviewer's copy, under "subscription".
    allowed_token = {"build_clean_env", "build_model_cli_env", "task_env",
                     "skill:code_review", "scheduler_skill_task:code_review"}
    for name, env in envs.items():
        if name not in allowed_token:
            assert not _carries(env, OAUTH), name

    # No model credential name survives into an env that carries none.
    for name in ("build_stripped_env", "heartbeat", "skill_proxy_base_env",
                 "scheduler_skill_task:kv"):
        assert not [k for k in envs[name] if is_model_credential_name(k)], name


@pytest.mark.parametrize("mode", ["subscription", "api_key"])
def test_each_provider_carries_its_own_key_only(world, mode):
    config_for, _make_task = world
    specs = build_providers(config_for(mode))
    assert reveal_inference_key(specs["anthropic"].inference_key) == ANTHROPIC_KEY
    assert reveal_inference_key(specs["openrouter"].inference_key) == OPENROUTER_KEY
    for spec in specs.values():
        assert LISTING not in reveal_inference_key(spec.inference_key)


def test_the_config_editor_never_writes_the_listing_key():
    """It moves credentials only through config paths, and this file has none
    whose value is the key; a change set naming it is refused."""
    from istota.config_editor.edits import ChangeError, _read_secrets

    with pytest.raises(ChangeError):
        _read_secrets({"istota_anthropic_listing_key": LISTING})
    files, _inline = _read_secrets({"istota_provider_anthropic_api_key": "k"})
    assert files == {"istota_provider_anthropic_api_key": "k"}


def test_the_admin_view_names_the_two_ways_to_set_it(tmp_path):
    from istota.webui.admin_config_view import build_config_view

    config = Config(db_path=tmp_path / "istota.db", temp_dir=tmp_path / "temp")
    view = build_config_view(config)
    entry = next(e for e in view["secrets"] if e["name"] == "istota_anthropic_listing_key")
    assert entry["read_only_reason"]
    assert "--anthropic-model-listing-key-file" in entry["read_only_reason"]
    assert "put-files" in entry["read_only_reason"]
    assert LISTING not in str(view)


def test_a_manifest_naming_the_listing_key_or_a_provider_is_refused():
    from istota.skills._loader import _parse_env_specs

    specs = _parse_env_specs([
        {"var": "LISTING", "from": "config", "config_path": "models.anthropic_listing_key_file"},
        {"var": "PROVIDER", "from": "config", "config_path": "providers.anthropic.api_key"},
        {"var": "MODE", "from": "config", "config_path": "brain.claude_code.auth"},
        {"var": "ISTOTA_PROVIDER_X_API_KEY", "from": "config", "config_path": "nextcloud.url"},
        {"var": "KEPT", "from": "config", "config_path": "nextcloud.url"},
    ])
    assert [s.var for s in specs] == ["KEPT"]
