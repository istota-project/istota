"""Direct tests for ``task_env.build_task_runtime``.

The block this covers used to be ~320 lines inside ``execute_task``, reachable
only by driving a whole task. The properties below are the ones the ordering
inside it exists for, and each was previously asserted (where it was asserted
at all) several hundred lines away from the line that establishes it.
"""

from pathlib import Path

import pytest

from istota import executor, task_env
from istota.config import Config, DevboxConfig, NetworkConfig, SecurityConfig
from istota.skills._types import EnvSpec, SkillMeta


@pytest.fixture
def runtime_inputs(tmp_path, make_task):
    """The non-config half of ``build_task_runtime``'s arguments."""
    user_temp_dir = tmp_path / "temp" / "testuser"
    user_temp_dir.mkdir(parents=True)
    control_dir = tmp_path / "temp" / ".control" / "testuser" / "task_1"
    control_dir.mkdir(parents=True)
    return {
        "task": make_task(id=1, user_id="testuser"),
        "user_temp_dir": user_temp_dir,
        "control_dir": control_dir,
        "task_attempt": 1,
        "selected_skills": [],
        "skill_index": {},
        "is_admin": True,
        "user_resources": [],
        "user_config": None,
        "discovered_calendars": [],
    }


def _config(tmp_path, **security):
    sec = {"sandbox_enabled": True, "skill_proxy_enabled": True}
    sec.update(security)
    # The database directory is a subdirectory rather than `tmp_path` itself:
    # `resolve_sandbox_cache_dir` refuses a cache root under it, and the cache
    # test below puts one beside it.
    (tmp_path / "db").mkdir(exist_ok=True)
    return Config(
        db_path=tmp_path / "db" / "test.db",
        temp_dir=tmp_path / "temp",
        nextcloud_mount_path=tmp_path / "mount",
        security=SecurityConfig(**sec),
        devbox=DevboxConfig(enabled=False),
    )


def _skill(name, *specs, cli=True):
    return SkillMeta(name=name, description="x", cli=cli, env_specs=list(specs))


class TestTheSandboxedMarker:
    """``ISTOTA_SANDBOXED`` reaches the model and not the host-side CLIs."""

    def test_the_marker_is_in_env_and_not_in_the_proxy_snapshot(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        # `proxy_base_env` is snapshotted before the marker is set, because the
        # proxy runs skills host-side where the marker would be a lie.
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        config = _config(tmp_path)

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        assert runtime.env["ISTOTA_SANDBOXED"] == "1"
        assert runtime.proxy_ctx is not None
        assert "ISTOTA_SANDBOXED" not in runtime.proxy_ctx.base_env

    def test_no_marker_when_bwrap_is_unavailable(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        monkeypatch.setattr(executor, "_bwrap_available", lambda: False)
        config = _config(tmp_path)

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        assert "ISTOTA_SANDBOXED" not in runtime.env

    def test_no_marker_when_the_proxy_is_off(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        # The marker means "the socket is how you run a skill"; with no socket
        # setting it would fail every skill CLI.
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        config = _config(tmp_path, skill_proxy_enabled=False)

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        assert "ISTOTA_SANDBOXED" not in runtime.env
        assert runtime.proxy_ctx is None
        assert runtime.proxy_sock is None


class TestTheClaudeRuntimeCredential:
    def test_neither_env_carries_the_oauth_token(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        # ISSUE-390: the token is declared in no manifest, so neither
        # credential split removes it; `without_claude_runtime_env` does.
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")
        config = _config(tmp_path)

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        assert runtime.proxy_ctx is not None
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in runtime.proxy_ctx.base_env
        # It *is* in the model's env — every brain that authenticates with it
        # needs it there; `NativeBrain` strips it on its own paths.
        assert runtime.env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-test"


class TestTheOrderOfTheTwoCredentialSplits:
    def test_a_var_declared_both_ways_goes_to_the_proxy_only_bucket(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        # `_split_credential_env` is called proxy-only first and the second
        # call operates on the residue, so a var declared both `proxy_only`
        # and `sensitive` never reaches `credential_env`.
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        config = _config(tmp_path)
        config.developer.enabled = True
        config.developer.gitlab_token = "glpat-xxxxxxxxxxxxxxxxxxxx"
        spec = EnvSpec(
            var="BOTH_WAYS",
            source="config",
            config_path="developer.gitlab_token",
            sensitive=True,
            proxy_only=True,
        )
        runtime_inputs = {
            **runtime_inputs,
            "skill_index": {"bothways": _skill("bothways", spec)},
            "selected_skills": ["bothways"],
        }

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        assert runtime.proxy_ctx is not None
        assert "BOTH_WAYS" not in runtime.env
        assert "BOTH_WAYS" not in runtime.proxy_ctx.credential_env
        assert runtime.proxy_ctx.base_env["BOTH_WAYS"] == (
            "glpat-xxxxxxxxxxxxxxxxxxxx"
        )

    def test_a_sensitive_only_var_goes_to_the_credential_bucket(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        # The control for the case above: without `proxy_only` the same var
        # survives the first split and is taken by the second.
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        config = _config(tmp_path)
        config.developer.enabled = True
        config.developer.gitlab_token = "glpat-xxxxxxxxxxxxxxxxxxxx"
        spec = EnvSpec(
            var="SECRET_ONLY",
            source="config",
            config_path="developer.gitlab_token",
            sensitive=True,
        )
        runtime_inputs = {
            **runtime_inputs,
            "skill_index": {"secretonly": _skill("secretonly", spec)},
            "selected_skills": ["secretonly"],
        }

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        assert runtime.proxy_ctx is not None
        assert "SECRET_ONLY" not in runtime.env
        assert runtime.proxy_ctx.credential_env["SECRET_ONLY"] == (
            "glpat-xxxxxxxxxxxxxxxxxxxx"
        )
        assert "SECRET_ONLY" not in runtime.proxy_ctx.base_env


class TestThePathPrepend:
    def test_the_hook_key_is_consumed_and_never_reaches_the_model(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        # A task-temp directory on `proxy_base_env`'s PATH would be a
        # host-side code-execution path; the reserved key is applied to `env`
        # after the snapshot and merged into neither.
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        prepend = str(runtime_inputs["user_temp_dir"] / ".developer")
        monkeypatch.setattr(
            "istota.skills._env.dispatch_setup_env_hooks",
            lambda selected, index, ctx: {
                executor.HOOK_PATH_PREPEND_KEY: prepend,
                "PLAIN_HOOK_VAR": "value",
            },
        )
        config = _config(tmp_path)

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        assert executor.HOOK_PATH_PREPEND_KEY not in runtime.env
        assert runtime.proxy_ctx is not None
        assert executor.HOOK_PATH_PREPEND_KEY not in runtime.proxy_ctx.base_env
        assert runtime.env["PATH"].startswith(prepend + ":")
        # The snapshot was taken before the prepend was applied.
        assert not runtime.proxy_ctx.base_env["PATH"].startswith(prepend)
        # An ordinary hook var still merges into both.
        assert runtime.env["PLAIN_HOOK_VAR"] == "value"


class TestTheNetworkProxyGate:
    def test_no_proxy_without_the_sandbox(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        # The network gate reads `security.sandbox_enabled` and the sandboxed
        # marker reads `effective_sandboxing`. Two different questions.
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        config = _config(tmp_path, sandbox_enabled=False)
        config.security.network = NetworkConfig(enabled=True)

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        assert runtime.net_proxy_ctx is None
        assert runtime.net_proxy_sock is None

    def test_a_proxy_when_both_are_on(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        config = _config(tmp_path)
        config.security.network = NetworkConfig(enabled=True)

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        assert runtime.net_proxy_ctx is not None
        assert runtime.net_proxy_sock is not None

    def test_the_network_gate_ignores_the_bwrap_probe(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        # `sandbox_enabled` on with no bwrap: no `ISTOTA_SANDBOXED`, but the
        # network proxy is still constructed, because the gate is the flag.
        monkeypatch.setattr(executor, "_bwrap_available", lambda: False)
        config = _config(tmp_path)
        config.security.network = NetworkConfig(enabled=True)

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        assert "ISTOTA_SANDBOXED" not in runtime.env
        assert runtime.net_proxy_ctx is not None


class TestBothManagersAreReturnedUnentered:
    def test_neither_socket_exists_yet(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        # The `ExitStack` lives in `execute_task` and must stay there: the
        # proxies have to be live across the primary call, the reroute and the
        # fallback call.
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        config = _config(tmp_path)
        config.security.network = NetworkConfig(enabled=True)

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        assert runtime.proxy_sock is not None
        assert not runtime.proxy_sock.exists()
        assert runtime.net_proxy_sock is not None
        assert not runtime.net_proxy_sock.exists()


class TestTheRestOfTheReturnedRuntime:
    def test_the_control_directory_is_the_read_only_bind(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        config = _config(tmp_path)

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        assert runtime.extra_ro_binds == [runtime_inputs["control_dir"]]

    def test_authorized_skills_is_a_frozenset_including_the_selected(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        config = _config(tmp_path)
        runtime_inputs = {
            **runtime_inputs,
            "skill_index": {"plain": _skill("plain")},
            "selected_skills": ["plain"],
        }

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        assert isinstance(runtime.authorized_skills, frozenset)
        assert "plain" in runtime.authorized_skills

    def test_the_task_identity_vars_are_set(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        config = _config(tmp_path)
        runtime_inputs = {**runtime_inputs, "task_attempt": 3}

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        assert runtime.env["ISTOTA_TASK_ID"] == "1"
        assert runtime.env["ISTOTA_USER_ID"] == "testuser"
        assert runtime.env["ISTOTA_DEFERRED_DIR"] == str(
            runtime_inputs["user_temp_dir"]
        )
        # Withheld from the model, handed to the proxy: `tasks transcript`
        # treats it as the floor that hides the live transcript.
        assert "ISTOTA_TASK_ATTEMPT" not in runtime.env
        assert runtime.proxy_ctx is not None
        assert runtime.proxy_ctx.base_env["ISTOTA_TASK_ATTEMPT"] == "3"

    def test_the_database_path_never_reaches_the_model(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        # Split outside the proxy branch: an operator who turned the proxy off
        # has not made it acceptable to hand the model a path to every user's
        # data.
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        config = _config(tmp_path, skill_proxy_enabled=False)

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        assert "ISTOTA_DB_PATH" not in runtime.env


class TestTheCacheRedirect:
    def test_the_caches_move_off_the_root_tmpfs_under_confinement(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        config = _config(tmp_path)
        (tmp_path / "caches").mkdir()
        config.security.sandbox_cache_dir = str(tmp_path / "caches")

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        assert runtime.env["UV_CACHE_DIR"].endswith("/uv")
        assert runtime.env["npm_config_cache"].endswith("/npm")
        assert Path(runtime.env["XDG_CACHE_HOME"]).name == "testuser"
        # HF_HOME is pinned back to the read-only bind rather than following
        # XDG, which would orphan a pre-warmed model cache.
        assert runtime.env["HF_HOME"].endswith(".cache/huggingface")

    def test_no_cache_vars_without_confinement(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        monkeypatch.setattr(executor, "_bwrap_available", lambda: False)
        config = _config(tmp_path)
        config.security.sandbox_cache_dir = str(tmp_path / "caches")

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        assert "UV_CACHE_DIR" not in runtime.env
        assert "npm_config_cache" not in runtime.env
