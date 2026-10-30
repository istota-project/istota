"""Model credentials stay where they belong (the model-catalogue spec, Stage 3).

A value test across every environment istota builds: a sentinel in the
model-listing key's file, another under every model credential name in the
process environment, and a key per provider in config. Each auth mode then
builds every env, and each sentinel may appear only where its mode carries it:
the model-listing key nowhere, the OAuth token nowhere under "api_key", a
provider's key only in its own provider and its own `model_caller_env` copy.

Stage 4 adds the discovery fetch's guards: `reveal_for_model_listing` has one
call site and `ModelListingKey` is constructed in one place (a scan of
`src/istota/`), the list goes to the fixed URL whatever the provider's base
URL says, and a redirect is reported rather than followed.
"""

from __future__ import annotations

import ast
import http.server
import os
import subprocess
import sys
import threading
import types
from pathlib import Path

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

SRC = Path(__file__).resolve().parents[1] / "src" / "istota"

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
    from istota import scheduler
    from istota.sandbox import task_env
    from istota.skills import _env as skills_env

    envs: dict[str, dict] = {}
    envs["build_clean_env"] = executor.build_clean_env(config)
    envs["build_model_cli_env"] = executor.build_model_cli_env(config)
    envs["build_harness_cli_env"] = executor.build_harness_cli_env(
        config, "alice", timeout_seconds=60,
    )
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

    box = executor.build_daemon_sandbox(
        config, "alice", profile=executor.SandboxProfile.NATIVE,
    )
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
    for cli_env in ("build_model_cli_env", "build_harness_cli_env"):
        if mode == "subscription":
            assert envs[cli_env]["CLAUDE_CODE_OAUTH_TOKEN"] == OAUTH
        else:
            assert envs[cli_env]["ANTHROPIC_API_KEY"] == ANTHROPIC_KEY

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
    allowed_anthropic = {"build_model_cli_env", "build_harness_cli_env",
                         "skill:code_review", "scheduler_skill_task:code_review"}
    for name, env in envs.items():
        if mode == "subscription" or name not in allowed_anthropic:
            assert not _carries(env, ANTHROPIC_KEY), name

    # The token: the CLI env and the reviewer's copy, under "subscription".
    # Neither a task env nor the clean env it starts from carries either
    # credential since the CLI left the sandbox (cli-brain-tools part 2,
    # Stage 5).
    allowed_token = {"build_model_cli_env", "build_harness_cli_env",
                     "skill:code_review", "scheduler_skill_task:code_review"}
    for name, env in envs.items():
        if name not in allowed_token:
            assert not _carries(env, OAUTH), name

    # No model credential name survives into an env that carries none.
    for name in ("build_clean_env", "task_env", "build_stripped_env",
                 "skill_proxy_base_env", "scheduler_skill_task:kv"):
        assert not [k for k in envs[name] if is_model_credential_name(k)], name


@pytest.mark.parametrize("mode", ["subscription", "api_key"])
def test_each_provider_carries_its_own_key_only(world, mode):
    config_for, _make_task = world
    specs = build_providers(config_for(mode))
    assert reveal_inference_key(specs["anthropic"].inference_key) == ANTHROPIC_KEY
    assert reveal_inference_key(specs["openrouter"].inference_key) == OPENROUTER_KEY
    for spec in specs.values():
        assert LISTING not in reveal_inference_key(spec.inference_key)


def test_the_config_editor_writes_the_listing_key_as_a_file_secret():
    """#859: a change set naming it goes to its secret file through the host
    handoff, like every other file credential; no config path holds it."""
    from istota.config_editor.edits import _read_secrets

    files, inline = _read_secrets({"istota_anthropic_listing_key": LISTING})
    assert files == {"istota_anthropic_listing_key": LISTING}
    assert inline == {}


def _listing_entry(config, tmp_path):
    from istota.webui.admin_config_view import build_config_view

    view = build_config_view(config, secrets_dir=tmp_path / "secrets")
    assert LISTING not in str(view)
    return next(e for e in view["secrets"] if e["name"] == "istota_anthropic_listing_key")


@pytest.fixture
def daemon_config(tmp_path):
    config = Config(db_path=tmp_path / "istota.db", temp_dir=tmp_path / "temp")
    db.init_db(config.db_path)
    return config


def test_the_admin_view_says_unknown_until_the_daemon_has_answered(daemon_config, tmp_path):
    """The web process never sees the file, so it shows no guess."""
    entry = _listing_entry(daemon_config, tmp_path)
    assert entry["read_only_reason"] is None
    assert entry["source"] == "unknown"
    assert entry["set"] is False
    assert "list" in entry["label"].lower()


@pytest.mark.parametrize("held, source", [(LISTING, "file"), ("", "none")])
def test_the_admin_view_shows_the_daemons_answer(daemon_config, tmp_path, held, source):
    from istota import doctor

    key_file = tmp_path / "daemon-listing"
    key_file.write_text(held)
    key_file.chmod(0o400)
    daemon_config.models.anthropic_listing_key_file = str(key_file)
    doctor.record_report(daemon_config, [])
    # The stack's shape: the web process's config names the default secret
    # path, which only the daemon mounts.
    daemon_config.models.anthropic_listing_key_file = ""
    entry = _listing_entry(daemon_config, tmp_path)
    assert entry["source"] == source
    assert entry["set"] is bool(held)
    assert entry["read_only_reason"] is None


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


def test_a_manifest_naming_native_s_legacy_key_fields_is_refused():
    # `[brain.native] api_key` and `extra_headers` still hold a provider key
    # through the legacy transition table.
    from istota.skills._loader import _parse_env_specs

    specs = _parse_env_specs([
        {"var": "LEGACY_KEY", "from": "config", "config_path": "brain.native.api_key"},
        {"var": "LEGACY_HEADERS", "from": "config", "config_path": "brain.native.extra_headers"},
        {"var": "ONE_HEADER", "from": "config", "config_path": "brain.native.extra_headers.Authorization"},
        {"var": "KEPT", "from": "config", "config_path": "brain.native.model"},
    ])
    assert [s.var for s in specs] == ["KEPT"]


# --- the discovery fetch (Stage 4) ---------------------------------------------


def _calls_named(name: str) -> list[tuple[str, str]]:
    """Every call of `name` (bare or attribute) under src/istota, as (file, enclosing function)."""
    found = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                target = node.func
                called = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
                if called == name:
                    found.append((path.relative_to(SRC).as_posix(), func.name))
    return found


def test_the_listing_key_is_revealed_at_exactly_one_call_site():
    assert _calls_named("reveal_for_model_listing") == [("llm/discovery.py", "fetch_anthropic_models")]


def test_a_listing_key_is_constructed_only_in_the_discovery_fetch():
    assert _calls_named("ModelListingKey") == [("llm/discovery.py", "_read_listing_key")]


def test_the_anthropic_list_goes_to_the_fixed_url_whatever_the_base_url(tmp_path):
    """A base URL pointed at a stub or a gateway never receives the key."""
    from dataclasses import replace

    from istota.llm import discovery
    from istota.llm.providers import ProviderSpec

    key_file = tmp_path / "listing"
    key_file.write_text(LISTING)
    seen = []

    def transport(url, headers, timeout):
        seen.append((url, headers.get("x-api-key")))
        return 200, b'{"data": [{"id": "claude-x-1", "line": "opus"}], "has_more": false}', {}

    spec = ProviderSpec(id="anthropic", kind="anthropic", base_url="http://127.0.0.1:9/stub")
    for base in ("http://127.0.0.1:9/stub", "https://gateway.example.com"):
        result = discovery.refresh_provider(
            replace(spec, base_url=base), tmp_path / "cache", 1.0,
            listing_key_path=key_file, transport=transport,
        )
        assert result.outcome == "refreshed"
    assert discovery.ANTHROPIC_MODELS_URL == "https://api.anthropic.com/v1/models"
    assert seen and all(url.startswith(f"{discovery.ANTHROPIC_MODELS_URL}?") for url, _key in seen)
    assert all(key == LISTING for _url, key in seen)


def test_the_default_transport_is_the_no_redirect_no_proxy_one():
    from istota.lib import fixed_get
    from istota.llm import discovery

    # The conftest swaps the module attribute out for every test, so the
    # function as written is what is checked.
    source = Path(discovery.__file__).read_text(encoding="utf-8")
    assert "    return fixed_get.get(url, headers, timeout)\n" in source
    opener = fixed_get.build_opener()
    assert any(isinstance(h, fixed_get.NoRedirect) for h in opener.handlers)
    assert fixed_get.NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://x/") is None


def test_a_redirect_is_reported_and_its_target_never_reached():
    """Against a real local server: the 302 comes back as a status, and the
    request carrying the key is never re-sent to where it points."""
    from istota.lib import fixed_get

    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            if self.path.startswith("/models"):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/elsewhere")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _body, _headers = fixed_get.get(
            f"http://127.0.0.1:{server.server_port}/models", {"x-api-key": LISTING}, 5.0,
        )
    finally:
        server.shutdown()
        server.server_close()
    assert status == 302
    assert hits == ["/models"]


def test_a_configured_key_path_makes_the_field_read_only(daemon_config, tmp_path):
    """The host applier writes the stack's secret file, which the daemon does
    not read when the config names another one."""
    daemon_config.models.anthropic_listing_key_file = str(tmp_path / "elsewhere")
    entry = _listing_entry(daemon_config, tmp_path)
    assert "anthropic_listing_key_file" in entry["read_only_reason"]


def test_a_nul_in_the_key_path_does_not_raise_from_the_report(daemon_config):
    """`record_report` runs outside the start-up run's `try`."""
    from istota import doctor

    daemon_config.models.anthropic_listing_key_file = "x\x00"
    doctor.record_report(daemon_config, [])
    assert doctor.daemon_listing_key_present(daemon_config) is False
