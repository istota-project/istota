"""Direct tests for ``task_env.build_task_runtime``.

The block this covers used to be ~320 lines inside ``execute_task``, reachable
only by driving a whole task. The properties below are the ones the ordering
inside it exists for, and each was previously asserted (where it was asserted
at all) several hundred lines away from the line that establishes it.
"""

import os
from pathlib import Path

import pytest

from istota import executor, network_proxy, skill_proxy, task_env
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

    def test_the_one_skill_that_calls_a_model_gets_it_back(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        # `code_review` spawns the `claude` CLI of its own, so the strip above
        # left it unauthenticated (every review came back `review_failed`).
        # It comes back scoped to that skill, by copy rather than by split:
        # the model's env keeps it, because two brains authenticate with it.
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")
        config = _config(tmp_path)
        runtime_inputs = {
            **runtime_inputs,
            "selected_skills": ["code_review", "kv"],
            "skill_index": {
                "code_review": _skill("code_review"),
                "kv": _skill("kv"),
            },
        }

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        proxy = runtime.proxy_ctx
        assert proxy is not None
        assert proxy.credential_env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-test"
        assert "CLAUDE_CODE_OAUTH_TOKEN" in proxy.skill_credential_map["code_review"]
        # Still out of the shared base env and still in the model's own.
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in proxy.base_env
        assert runtime.env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-test"

    def test_no_other_skill_gets_it(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")
        config = _config(tmp_path)
        runtime_inputs = {
            **runtime_inputs,
            "selected_skills": ["code_review", "kv"],
            "skill_index": {
                "code_review": _skill("code_review"),
                "kv": _skill("kv"),
            },
        }

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        assert "CLAUDE_CODE_OAUTH_TOKEN" not in (
            runtime.proxy_ctx.skill_credential_map.get("kv", set())
        )

    def test_it_is_never_fetchable_over_the_socket(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        # `credential-fetch` is scoped by a union allowlist rather than per
        # skill, so anything in it is readable by anything holding the socket
        # — the model included. Injection is scoped; a lookup would not be.
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")
        config = _config(tmp_path)
        runtime_inputs = {
            **runtime_inputs,
            "selected_skills": ["code_review"],
            "skill_index": {"code_review": _skill("code_review")},
        }

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        assert "CLAUDE_CODE_OAUTH_TOKEN" not in runtime.proxy_ctx.allowed_credentials

    def test_a_skill_the_task_never_authorized_gets_nothing(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")
        config = _config(tmp_path)
        runtime_inputs = {
            **runtime_inputs,
            "selected_skills": [],
            "skill_index": {"code_review": _skill("code_review")},
        }

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        assert "code_review" not in runtime.proxy_ctx.skill_credential_map

    def test_an_api_key_deployment_is_authenticated_the_same_way(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        # `build_model_cli_env` runs *inside* the skill subprocess, so the
        # `os.environ` it reads is the proxy's snapshot rather than the
        # daemon's — and the API-key names are in a task env by no route at
        # all. Broken on that shape before ISSUE-390 broke the other one.
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-test")
        config = _config(tmp_path)
        runtime_inputs = {
            **runtime_inputs,
            "selected_skills": ["code_review"],
            "skill_index": {"code_review": _skill("code_review")},
        }

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        proxy = runtime.proxy_ctx
        assert proxy.credential_env["ANTHROPIC_API_KEY"] == "sk-ant-api-test"
        assert "ANTHROPIC_API_KEY" in proxy.skill_credential_map["code_review"]
        assert "ANTHROPIC_API_KEY" not in proxy.allowed_credentials
        # Never in the shared snapshot: it reaches one skill, not all of them.
        assert "ANTHROPIC_API_KEY" not in proxy.base_env

    def test_nothing_is_injected_when_the_daemon_has_no_token(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        config = _config(tmp_path)
        runtime_inputs = {
            **runtime_inputs,
            "selected_skills": ["code_review"],
            "skill_index": {"code_review": _skill("code_review")},
        }

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        # An empty string is not a credential: injecting one gives the CLI
        # something to authenticate with that cannot work.
        #
        # Asserted over the credential names rather than as `not
        # credential_env`, which is what this said until ISSUE-410 put the
        # scoped *reachability* names through the same dict. Emptiness was
        # never the claim — it was true by accident of nothing else living
        # there — and a deployment with a `NO_PROXY` set now legitimately puts
        # one in, as does every test in this suite, since
        # `tests/support/env_isolation.py` forces that name into `os.environ`.
        proxy = runtime.proxy_ctx
        for name in executor.SKILL_MODEL_CREDENTIAL_VARS:
            assert name not in proxy.credential_env, name
        assert not (
            executor.SKILL_MODEL_CREDENTIAL_VARS
            & proxy.skill_credential_map.get("code_review", set())
        )


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


def _code_review_inputs(runtime_inputs):
    return {
        **runtime_inputs,
        "selected_skills": ["code_review"],
        "skill_index": {"code_review": _skill("code_review")},
    }


class TestTheReachabilityNames:
    """How a host-side skill CLI learns to reach anything (ISSUE-410).

    The second half of ISSUE-409. ``build_model_cli_env`` tops its allowlist up
    from ``os.environ``, which is the daemon's own for every daemon-side caller
    and is ``proxy_base_env`` for the one that is not — ``code_review``, which
    runs as a subprocess the skill proxy spawned — so the loop was reading the
    wrong environment. ISSUE-409 put the credential names in reach; these are
    what was left, and without them a deployment behind an outbound proxy, a
    private CA or a gateway authenticates and then fails at connect or at the
    handshake.

    **They do not all go to the same place**, and the two halves are asserted
    apart because the axis is the whole point: a trust store path only ever
    adds a CA, so it is shared, while a proxy URL redirects traffic and can
    carry userinfo, so it is scoped to the skills that call a model.

    Each case sets a value on the daemon side and observes that same value on
    the other. Asserting only that a name is *present* passes just as well
    against a deployment that sets nothing, which is the failure this whole
    group is a second instance of.

    One ambient fact worth knowing before reading a failure here:
    ``tests/support/env_isolation.py`` deliberately *forces* ``NO_PROXY`` and
    ``no_proxy`` into ``os.environ`` for every test in the suite, so those two
    names are present unless a case deletes them.
    """

    def test_the_tls_names_reach_every_host_side_cli(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/private-ca.pem")
        monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "/etc/ssl/node-ca.pem")
        monkeypatch.setenv("CURL_CA_BUNDLE", "/etc/ssl/curl-ca.pem")
        config = _config(tmp_path)

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        base = runtime.proxy_ctx.base_env
        assert base["SSL_CERT_FILE"] == "/etc/ssl/private-ca.pem"
        assert base["NODE_EXTRA_CA_CERTS"] == "/etc/ssl/node-ca.pem"
        assert base["CURL_CA_BUNDLE"] == "/etc/ssl/curl-ca.pem"

    def test_the_proxy_triple_does_not_reach_every_host_side_cli(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        # The regression this split exists to avoid. `browse` calls
        # `BROWSER_API_URL` — `http://localhost:9223` by default — over an
        # httpx client with `trust_env=True`, which honours `HTTP_PROXY` and
        # does not exempt loopback. A shared egress proxy would send that
        # internal call at the proxy, which answers 403 or 405, on a
        # deployment where it works today.
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        monkeypatch.setenv("HTTPS_PROXY", "http://egress.example:3128")
        monkeypatch.setenv("HTTP_PROXY", "http://egress.example:3128")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example/v1")
        config = _config(tmp_path)

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        base = runtime.proxy_ctx.base_env
        assert "HTTPS_PROXY" not in base
        assert "HTTP_PROXY" not in base
        assert "ANTHROPIC_BASE_URL" not in base

    def test_the_proxy_triple_reaches_the_model_calling_skill(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        monkeypatch.setenv("HTTPS_PROXY", "http://egress.example:3128")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example/v1")
        config = _config(tmp_path)

        runtime = task_env.build_task_runtime(
            config, **_code_review_inputs(runtime_inputs)
        )

        proxy = runtime.proxy_ctx
        assert proxy.credential_env["HTTPS_PROXY"] == (
            "http://egress.example:3128"
        )
        assert proxy.credential_env["ANTHROPIC_BASE_URL"] == (
            "https://gateway.example/v1"
        )
        assert "HTTPS_PROXY" in proxy.skill_credential_map["code_review"]
        assert "ANTHROPIC_BASE_URL" in proxy.skill_credential_map["code_review"]

    def test_no_other_skill_gets_the_proxy_triple(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        monkeypatch.setenv("HTTPS_PROXY", "http://egress.example:3128")
        config = _config(tmp_path)
        runtime_inputs = {
            **runtime_inputs,
            "selected_skills": ["code_review", "kv"],
            "skill_index": {
                "code_review": _skill("code_review"),
                "kv": _skill("kv"),
            },
        }

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        assert "HTTPS_PROXY" not in (
            runtime.proxy_ctx.skill_credential_map.get("kv", set())
        )

    def test_the_proxy_triple_is_never_fetchable_over_the_socket(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        # A proxy URL can carry basic-auth userinfo, and `allowed_credentials`
        # is a union anything holding the socket can fetch by name — the model
        # included. Injection is scoped; a lookup would not be.
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        monkeypatch.setenv("HTTPS_PROXY", "http://user:pw@egress.example:3128")
        config = _config(tmp_path)

        runtime = task_env.build_task_runtime(
            config, **_code_review_inputs(runtime_inputs)
        )

        assert "HTTPS_PROXY" not in runtime.proxy_ctx.allowed_credentials
        assert "ANTHROPIC_BASE_URL" not in runtime.proxy_ctx.allowed_credentials

    def test_nothing_arrives_when_the_daemon_sets_nothing(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        # The negative control for both halves. Without it every assertion
        # above is equally true of an env carrying the name for some other
        # reason, which is how the first half of this went unnoticed.
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        names = (
            *executor.SKILL_CLI_TLS_VARS,
            *executor.SKILL_MODEL_REACHABILITY_VARS,
        )
        for name in names:
            monkeypatch.delenv(name, raising=False)
        config = _config(tmp_path)

        runtime = task_env.build_task_runtime(
            config, **_code_review_inputs(runtime_inputs)
        )

        proxy = runtime.proxy_ctx
        for name in names:
            assert name not in proxy.base_env, name
            assert name not in proxy.credential_env, name

    def test_an_empty_no_proxy_is_carried(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        # Presence, not truthiness: `NO_PROXY=` blanks an inherited exemption
        # list, so dropping it is a behaviour change rather than a no-op. This
        # is also what separates `skill_model_reachability` from
        # `skill_model_credentials`, which drops an empty value on purpose.
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        monkeypatch.setenv("NO_PROXY", "")
        config = _config(tmp_path)

        runtime = task_env.build_task_runtime(
            config, **_code_review_inputs(runtime_inputs)
        )

        assert runtime.proxy_ctx.credential_env["NO_PROXY"] == ""

    def test_a_name_the_credential_split_took_is_not_put_back(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        # The top-up fills gaps; it does not overrule a decision made
        # elsewhere. A manifest declaring one of these `sensitive` has moved
        # it to the per-skill map on purpose, and re-adding it from the
        # daemon's environment would undo that silently — putting it in front
        # of every host-side CLI instead of the one that declared it.
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/from-the-daemon.pem")
        config = _config(tmp_path)
        config.developer.enabled = True
        config.developer.gitlab_token = "glpat-xxxxxxxxxxxxxxxxxxxx"
        spec = EnvSpec(
            var="SSL_CERT_FILE",
            source="config",
            config_path="developer.gitlab_token",
            sensitive=True,
        )
        runtime_inputs = {
            **runtime_inputs,
            "skill_index": {"certskill": _skill("certskill", spec)},
            "selected_skills": ["certskill"],
        }

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        proxy = runtime.proxy_ctx
        assert proxy.credential_env["SSL_CERT_FILE"] == (
            "glpat-xxxxxxxxxxxxxxxxxxxx"
        )
        assert "SSL_CERT_FILE" not in proxy.base_env

    def test_the_three_name_lists_are_disjoint(self):
        # The scoped reachability names are merged into the same dict as the
        # credentials and injected through the same per-skill map, and the
        # shared TLS names land in `proxy_base_env` first. A name on two of
        # these lists would be handled twice under two different rules —
        # notably the empty-value rule, which differs between them.
        tls = set(executor.SKILL_CLI_TLS_VARS)
        reach = set(executor.SKILL_MODEL_REACHABILITY_VARS)
        creds = set(executor.SKILL_MODEL_CREDENTIAL_VARS)
        assert not tls & reach
        assert not tls & creds
        assert not reach & creds

    def test_the_scoped_names_are_blocked_from_lookup_by_name(self):
        # `_PROXY_LOOKUP_BLOCKED` is the endpoint-side half of the same rule
        # the `allowed_credentials` assertion above checks from the outside,
        # so the two do not depend on each other's ordering.
        assert executor.SKILL_MODEL_REACHABILITY_VARS <= (
            executor._PROXY_LOOKUP_BLOCKED
        )

    def test_the_reviewer_env_built_inside_the_subprocess_carries_them(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        # The seam the whole change exists for. `build_model_cli_env` runs
        # *inside* the skill subprocess, so its `os.environ` is what the proxy
        # handed that subprocess — the shared snapshot plus this skill's own
        # injected names. Its existing top-up loop is what carries them the
        # last hop into the `claude -p` each reviewer spawns; nothing in that
        # function changed, only what its environment has to offer.
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        monkeypatch.setenv("HTTPS_PROXY", "http://egress.example:3128")
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/etc/ssl/private-ca.pem")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example/v1")
        config = _config(tmp_path)

        runtime = task_env.build_task_runtime(
            config, **_code_review_inputs(runtime_inputs)
        )

        # Stand exactly where the skill CLI stands: `skill_proxy` builds its
        # env as the base snapshot plus the names mapped to that skill.
        proxy = runtime.proxy_ctx
        cli_env = dict(proxy.base_env)
        for var in proxy.skill_credential_map["code_review"]:
            cli_env[var] = proxy.credential_env[var]
        monkeypatch.setattr(os, "environ", cli_env)
        reviewer_env = executor.build_model_cli_env(config)

        assert reviewer_env["HTTPS_PROXY"] == "http://egress.example:3128"
        assert reviewer_env["REQUESTS_CA_BUNDLE"] == "/etc/ssl/private-ca.pem"
        assert reviewer_env["ANTHROPIC_BASE_URL"] == (
            "https://gateway.example/v1"
        )


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
    """The one property the spec names as the thing not to tidy away.

    The `ExitStack` lives in `execute_task` and must stay there: the proxies
    have to be live across the primary call, the reroute and the fallback
    call. A `with` inside the builder would close them before the brain ran.
    """

    def test_neither_proxy_was_started(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        # Records `start`/`stop` rather than probing for the socket file. The
        # socket is not a discriminating probe: `SkillProxy.stop` unlinks it,
        # so a builder "tidied" into `with SkillProxy(...)` would leave no
        # socket behind either and the absence would read as never-entered.
        calls = []
        for cls, name in (
            (skill_proxy.SkillProxy, "skill"),
            (network_proxy.NetworkProxy, "net"),
        ):
            for verb in ("start", "stop"):
                monkeypatch.setattr(
                    cls, verb,
                    (lambda n, v: lambda self, *a, **k: calls.append((n, v)))(
                        name, verb,
                    ),
                )
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        config = _config(tmp_path)
        config.security.network = NetworkConfig(enabled=True)

        runtime = task_env.build_task_runtime(config, **runtime_inputs)

        assert runtime.proxy_ctx is not None
        assert runtime.net_proxy_ctx is not None
        assert calls == []

        # The control: entering one *is* observable through the recorder, so
        # an empty list above means never-entered rather than never-recorded.
        with runtime.proxy_ctx:
            pass
        assert calls == [("skill", "start"), ("skill", "stop")]

    def test_neither_socket_exists_yet(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        # Weaker than the recorder above and kept for the plainer claim: the
        # builder has bound nothing on the filesystem.
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
