"""Deployment wiring for the WhatsApp Cloud API webhook surface.

Written against `tests/test_sms_deployment.py`, which did this job one spec
earlier. The two surfaces share one webhook process, one nginx prefix and one
`secrets.env`, so the assertions that matter here are the ones about them
*sharing* rather than each having its own: a deployment with location, SMS and
WhatsApp all on runs one receiver, and every gate that decides whether it runs
has to name all three.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
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
ENV_EXAMPLE = REPO / "docker" / ".env.example"
ANSIBLE = REPO / "deploy" / "ansible"
ANSIBLE_NGINX = ANSIBLE / "templates" / "istota.conf.j2"

WHATSAPP_VALUES = {
    "ISTOTA_WHATSAPP_ENABLED": "true",
    "ISTOTA_WHATSAPP_WABA_ID": "100000000000001",
    "ISTOTA_WHATSAPP_PHONE_NUMBER_ID": "100000000000002",
    "ISTOTA_WHATSAPP_BUSINESS_PHONE_NUMBER": "+15551230000",
    "ISTOTA_WHATSAPP_ACCESS_TOKEN": "token-placeholder",
    "ISTOTA_WHATSAPP_APP_SECRET": "app-secret-placeholder",
    "ISTOTA_WHATSAPP_VERIFY_TOKEN": "verify-placeholder",
    "ISTOTA_WHATSAPP_GRAPH_API_VERSION": "v25.0",
    "ISTOTA_WHATSAPP_BUSINESS_TIMEZONE": "Europe/Warsaw",
    "ISTOTA_WHATSAPP_REQUEST_TIMEOUT_SECONDS": "8",
    "ISTOTA_WHATSAPP_BILLING_POLICY": "allow_paid",
    "ISTOTA_WHATSAPP_MONTHLY_SERVICE_ATTEMPT_LIMIT": "400",
    "ISTOTA_WHATSAPP_TEMPLATE_ENABLED": "true",
    "ISTOTA_WHATSAPP_TEMPLATE_NAME": "istota_result",
    "ISTOTA_WHATSAPP_TEMPLATE_LANGUAGE": "en_GB",
}


class TestTheDockerRender:
    def test_it_writes_every_whatsapp_field_the_operator_can_set(self, tmp_path):
        path = render_docker_config(tmp_path, **REQUIRED, **WHATSAPP_VALUES)
        whatsapp = tomllib.loads(path.read_text())["whatsapp"]

        assert whatsapp["enabled"] is True
        assert whatsapp["waba_id"] == "100000000000001"
        assert whatsapp["phone_number_id"] == "100000000000002"
        assert whatsapp["business_phone_number"] == "+15551230000"
        assert whatsapp["access_token"] == "token-placeholder"
        assert whatsapp["app_secret"] == "app-secret-placeholder"
        assert whatsapp["verify_token"] == "verify-placeholder"
        assert whatsapp["graph_api_version"] == "v25.0"
        assert whatsapp["business_timezone"] == "Europe/Warsaw"
        assert whatsapp["request_timeout_seconds"] == 8
        assert whatsapp["billing_policy"] == "allow_paid"
        assert whatsapp["monthly_service_attempt_limit"] == 400
        assert whatsapp["proactive_template"] == {
            "enabled": True,
            "name": "istota_result",
            "language": "en_GB",
        }

    def test_the_defaults_render_a_disabled_free_guard_block(self, tmp_path):
        """An operator who sets nothing gets the free-biased shape.

        `billing_policy` in particular: a render defaulting to `allow_paid`
        would make every Docker deployment able to send a paid template the
        moment somebody named one, which is the opposite of what
        `free_guard` is for.
        """
        path = render_docker_config(tmp_path, **REQUIRED)
        whatsapp = tomllib.loads(path.read_text())["whatsapp"]

        assert whatsapp["enabled"] is False
        assert whatsapp["billing_policy"] == "free_guard"
        assert whatsapp["monthly_service_attempt_limit"] == 900
        assert whatsapp["proactive_template"]["enabled"] is False

    def test_a_capitalised_boolean_renders_invalid_toml(self, tmp_path):
        """Recorded rather than fixed, so the next reader knows it was decided.

        The four numeric and boolean fields are interpolated bare, so
        `ISTOTA_WHATSAPP_ENABLED=True` writes `enabled = True`, which is not a
        TOML boolean and fails the load of the *whole* config rather than the
        WhatsApp block. The Ansible template normalizes the same field with
        `| lower` and the shell has no equivalent.

        Not fixed here, and the reason is that it is the file's own pattern:
        roughly fifty booleans and integers are interpolated this way, so a
        `toml_bool` helper on these four alone makes one file hold two
        conventions while leaving the class open everywhere else. Normalizing
        all of them is its own change with its own blast radius.
        `docker/.env.example` shows every one of these as lowercase, which is
        the documentation. No injection either way: the heredoc expands and
        does not re-evaluate.
        """
        path = render_docker_config(
            tmp_path, **REQUIRED, ISTOTA_WHATSAPP_ENABLED="True",
        )

        assert "enabled = True" in path.read_text()
        with pytest.raises(tomllib.TOMLDecodeError):
            tomllib.loads(path.read_text())

    def test_the_rendered_config_loads(self, tmp_path):
        """The render is only correct if `load_config` accepts what it wrote.

        A `[whatsapp]` block that parses as TOML and then fails validation is
        a container that crash-loops on boot with the operator's own values in
        it, which no assertion above would catch.
        """
        from istota.config import load_config

        path = render_docker_config(tmp_path, **REQUIRED, **WHATSAPP_VALUES)
        config = load_config(path)

        assert config.whatsapp.enabled is True
        assert config.whatsapp.proactive_template.name == "istota_result"


class TestTheComposeStack:
    def test_the_webhook_service_carries_the_whatsapp_profile(self):
        compose = yaml.safe_load(COMPOSE.read_text())
        webhook = compose["services"]["webhooks"]

        assert set(webhook["profiles"]) == {"location", "sms", "whatsapp"}
        # One receiver for all three, on one internal port. `ports` here would
        # publish the raw receiver beside nginx, which is what the signed
        # webhook contract is behind.
        assert "ports" not in webhook
        assert webhook["expose"] == ["${ISTOTA_WEBHOOKS_PORT:-8765}"]

    def test_compose_passes_every_value_the_render_reads(self):
        compose = yaml.safe_load(COMPOSE.read_text())
        environment = compose["services"]["istota"]["environment"]

        for name in WHATSAPP_VALUES:
            assert name in environment, f"compose withholds {name}"

    def test_the_env_example_documents_the_profile_and_every_setting(self):
        text = ENV_EXAMPLE.read_text()

        assert re.search(r"^#\s+whatsapp\s", text, re.M), (
            "the optional-services list at the top of .env.example does not "
            "name the whatsapp profile"
        )
        for name in WHATSAPP_VALUES:
            assert re.search(rf"^{name}=", text, re.M), f"undocumented: {name}"


class TestTheDockerNginx:
    def test_the_whatsapp_route_gets_its_own_bounded_location(self):
        """Above istota's own 256 KiB cap and far below the shared 10m.

        The point of the route-specific limit is that the *application*
        answers an oversized body — 413 through its shared bounded-body
        reader, the same status the SMS routes return — rather than nginx
        answering with an HTML page Meta will retry against for ever.
        """
        nginx = DOCKER_NGINX.read_text()
        block = _nginx_location(nginx, "/webhooks/whatsapp")

        assert "proxy_pass http://$upstream_webhooks" in block
        limit = re.search(r"client_max_body_size\s+(\S+);", block)
        assert limit, "no route-specific body limit"
        assert _bytes(limit.group(1)) > 256 * 1024
        assert _bytes(limit.group(1)) <= 1024 * 1024

    def test_the_whatsapp_route_logs_no_query_string(self):
        """The verify token arrives as a query value on the GET handshake.

        Nothing in the application can enforce this: by the time the route
        runs, nginx has already decided what to write. `$request` in the
        default `combined` format carries the whole request line, query
        included, so the access log for this one path has to go.
        """
        block = _nginx_location(DOCKER_NGINX.read_text(), "/webhooks/whatsapp")

        assert re.search(r"^\s*access_log\s+off;", block, re.M)

    def test_the_shared_webhook_prefix_still_serves_everything_else(self):
        nginx = DOCKER_NGINX.read_text()

        assert "location /webhooks/ {" in nginx


class TestTheReceiverLogsNoVerifyToken:
    """nginx is only half of it, and the half that was missing is louder.

    Meta sends the verify token as a query value on the GET handshake. Turning
    the nginx access log off for that path stops the front end writing it, and
    then uvicorn writes it anyway: its default access formatter renders
    `get_path_with_query_string(scope)`, so the line reaching the container log
    and the systemd journal is
    `GET /webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=<secret>...`.
    Measured against a live uvicorn, not read off the source.

    `serve.build_uvicorn_server` already passes `access_log=False`, so the
    combined process was the only shape holding the property the docs claim.
    These two bring the standalone shapes into line with it.
    """

    def test_the_compose_receiver_disables_the_uvicorn_access_log(self):
        compose = COMPOSE.read_text()
        command = compose.split("  webhooks:", 1)[1]
        line = next(
            ln for ln in command.splitlines()
            if "uvicorn istota.webhook_receiver:app" in ln
        )

        assert "--no-access-log" in line

    def test_the_systemd_receiver_disables_the_uvicorn_access_log(self):
        unit = (ANSIBLE / "templates" / "istota-webhooks.service.j2").read_text()
        line = next(ln for ln in unit.splitlines() if ln.startswith("ExecStart="))

        assert "--no-access-log" in line

    def test_the_combined_process_already_disabled_it(self):
        """The control for the two above: if `serve` ever turns it back on,
        every shape leaks and these assertions would be the only ones left
        describing a property nothing holds."""
        from istota import serve

        source = (Path(serve.__file__)).read_text()

        assert "access_log=False" in source


class TestTheAnsibleRole:
    def test_the_config_template_renders_whatsapp_without_the_secrets(self):
        overrides = {
            "istota_whatsapp_enabled": True,
            "istota_whatsapp_waba_id": "100000000000001",
            "istota_whatsapp_phone_number_id": "100000000000002",
            "istota_whatsapp_business_phone_number": "+15551230000",
            "istota_whatsapp_business_timezone": "Europe/Warsaw",
            "istota_whatsapp_access_token": "token-placeholder",
            "istota_whatsapp_app_secret": "app-secret-placeholder",
            "istota_whatsapp_verify_token": "verify-placeholder",
        }
        parsed = tomllib.loads(render_ansible_config(**overrides))["whatsapp"]

        assert parsed["enabled"] is True
        assert parsed["waba_id"] == "100000000000001"
        assert parsed["business_timezone"] == "Europe/Warsaw"
        # `istota_use_environment_file` defaults on, so the three credentials
        # travel in secrets.env and this file must not name them at all.
        assert "access_token" not in parsed
        assert "app_secret" not in parsed
        assert "verify_token" not in parsed

    def test_the_rendered_config_loads_in_both_credential_shapes(self, tmp_path):
        """The Docker case next door states the rule; bare metal needs it more.

        `tests/test_ansible_config_template.py` runs `load_config` over the
        *default* render, which is `enabled = false` with every value empty —
        so none of `billing_policy`, `business_timezone`,
        `request_timeout_seconds` or `monthly_service_attempt_limit` has been
        through the validator on the shape AGENTS.md calls the only canonical
        deployment. Both credential shapes, because the env-file one renders
        no secrets at all and that has to load too (ISSUE-058).
        """
        from istota.config import load_config

        base = {
            "istota_whatsapp_enabled": True,
            "istota_whatsapp_waba_id": "100000000000001",
            "istota_whatsapp_phone_number_id": "100000000000002",
            "istota_whatsapp_business_phone_number": "+15551230000",
            "istota_whatsapp_business_timezone": "Europe/Warsaw",
            "istota_whatsapp_request_timeout_seconds": 8,
            "istota_whatsapp_monthly_service_attempt_limit": 400,
            "istota_whatsapp_access_token": "token-placeholder",
            "istota_whatsapp_app_secret": "app-secret-placeholder",
            "istota_whatsapp_verify_token": "verify-placeholder",
        }
        for index, env_file in enumerate((True, False)):
            path = tmp_path / f"config-{index}.toml"
            path.write_text(
                render_ansible_config(**base, istota_use_environment_file=env_file)
            )

            config = load_config(path)

            assert config.whatsapp.enabled is True
            assert config.whatsapp.business_timezone == "Europe/Warsaw"
            assert config.whatsapp.request_timeout_seconds == 8
            assert config.whatsapp.monthly_service_attempt_limit == 400
            assert config.whatsapp.billing_policy == "free_guard"
            assert bool(config.whatsapp.app_secret) is not env_file

    def test_a_paid_render_with_a_template_loads(self, tmp_path):
        """`proactive_template.enabled` is refused outside `allow_paid`, and
        the role can express both halves independently — so the combination is
        a config the loader raises on that only a render exercises."""
        from istota.config import load_config

        path = tmp_path / "config.toml"
        path.write_text(render_ansible_config(
            istota_whatsapp_enabled=True,
            istota_whatsapp_waba_id="100000000000001",
            istota_whatsapp_phone_number_id="100000000000002",
            istota_whatsapp_business_phone_number="+15551230000",
            istota_whatsapp_billing_policy="allow_paid",
            istota_whatsapp_template_enabled=True,
            istota_whatsapp_template_name="istota_result",
            istota_whatsapp_template_language="en_GB",
        ))

        config = load_config(path)

        assert config.whatsapp.billing_policy == "allow_paid"
        assert config.whatsapp.proactive_template.enabled is True
        assert config.whatsapp.proactive_template.name == "istota_result"
        assert config.whatsapp.proactive_template.language == "en_GB"

    def test_the_config_template_inlines_the_secrets_without_the_env_file(self):
        overrides = {
            "istota_use_environment_file": False,
            "istota_whatsapp_enabled": True,
            "istota_whatsapp_waba_id": "100000000000001",
            "istota_whatsapp_phone_number_id": "100000000000002",
            "istota_whatsapp_business_phone_number": "+15551230000",
            "istota_whatsapp_access_token": "token-placeholder",
            "istota_whatsapp_app_secret": "app-secret-placeholder",
            "istota_whatsapp_verify_token": "verify-placeholder",
        }
        parsed = tomllib.loads(render_ansible_config(**overrides))["whatsapp"]

        assert parsed["access_token"] == "token-placeholder"
        assert parsed["app_secret"] == "app-secret-placeholder"
        assert parsed["verify_token"] == "verify-placeholder"

    def test_the_three_credentials_reach_secrets_env(self):
        overrides = {
            "istota_whatsapp_access_token": "token-placeholder",
            "istota_whatsapp_app_secret": "app-secret-placeholder",
            "istota_whatsapp_verify_token": "verify-placeholder",
        }
        secrets = render_secrets(**overrides)

        assert "ISTOTA_WHATSAPP_ACCESS_TOKEN=token-placeholder" in secrets
        assert "ISTOTA_WHATSAPP_APP_SECRET=app-secret-placeholder" in secrets
        assert "ISTOTA_WHATSAPP_VERIFY_TOKEN=verify-placeholder" in secrets

    def test_whatsapp_alone_starts_the_shared_webhook_receiver(self):
        defaults = yaml.safe_load(DEFAULTS_FILE.read_text())
        tasks = yaml.safe_load(TASKS_FILE.read_text())

        assert defaults["istota_whatsapp_enabled"] is False
        derive = next(
            task for task in tasks
            if task.get("name") == "Resolve webhook receiver need"
        )
        expression = derive["set_fact"]["istota_webhooks_enabled"]
        assert "istota_whatsapp_enabled" in expression

        base = {
            **defaults,
            "istota_location_enabled": False,
            "istota_sms_enabled": False,
        }
        template = _ansible_ish_environment().from_string(expression)
        assert template.render(
            {**base, "istota_whatsapp_enabled": True},
        ).strip() == "True"
        # The control for the arm above: with all three off the same
        # expression has to answer False, or it is not reading any of them.
        assert template.render(
            {**base, "istota_whatsapp_enabled": False},
        ).strip() == "False"

    def test_the_role_refuses_the_two_configs_the_loader_raises_on(self):
        """A pre-flight assert, because the alternative fails half-way through.

        `Deploy istota configuration` writes the file and a later task runs
        the CLI against it, so an inventory value `load_config` rejects makes
        the play fail at `Ensure user_profiles rows` having already replaced
        the running deployment's config. Two WhatsApp values are reachable
        that way — an unknown `billing_policy` (which raises even while the
        block is disabled) and a template enabled under `free_guard`. The role
        already guards this class for the developer and google_workspace
        toggles, and its comment there asks for the same treatment.
        """
        tasks = yaml.safe_load(TASKS_FILE.read_text())
        asserts = [
            task for task in tasks
            if "assert" in task and "whatsapp" in str(task.get("name", "")).lower()
        ]
        assert asserts, "the role has no WhatsApp pre-flight assert"

        conditions = " ".join(
            str(clause)
            for task in asserts
            for clause in task["assert"]["that"]
        )
        assert "istota_whatsapp_billing_policy" in conditions
        assert "istota_whatsapp_template_enabled" in conditions

        # The assert has to run whatever `enabled` says, because an unknown
        # billing policy fails the load on a disabled block too.
        for task in asserts:
            assert "istota_whatsapp_enabled" not in str(task.get("when", ""))

    def test_the_whatsapp_asserts_pass_on_the_defaults(self):
        """Otherwise every deploy that never touched WhatsApp fails."""
        defaults = yaml.safe_load(DEFAULTS_FILE.read_text())
        tasks = yaml.safe_load(TASKS_FILE.read_text())
        env = _ansible_ish_environment()

        for task in tasks:
            if "assert" not in task or "whatsapp" not in str(task.get("name", "")).lower():
                continue
            for clause in task["assert"]["that"]:
                rendered = env.from_string("{{ " + clause + " }}").render(defaults)
                assert rendered.strip() == "True", f"{clause!r} -> {rendered!r}"

    def test_the_whatsapp_asserts_catch_both_bad_shapes(self):
        defaults = yaml.safe_load(DEFAULTS_FILE.read_text())
        tasks = yaml.safe_load(TASKS_FILE.read_text())
        env = _ansible_ish_environment()
        clauses = [
            clause
            for task in tasks
            if "assert" in task and "whatsapp" in str(task.get("name", "")).lower()
            for clause in task["assert"]["that"]
        ]

        for label, bad in (
            ("typo in the policy", {"istota_whatsapp_billing_policy": "free-guard"}),
            ("template under free_guard", {"istota_whatsapp_template_enabled": True}),
        ):
            verdicts = [
                env.from_string("{{ " + clause + " }}").render({**defaults, **bad}).strip()
                for clause in clauses
            ]
            assert "False" in verdicts, f"nothing refuses {label}"

    def test_the_nginx_template_bounds_and_unlogs_the_whatsapp_route(self):
        defaults = yaml.safe_load(DEFAULTS_FILE.read_text())
        rendered = Environment().from_string(ANSIBLE_NGINX.read_text()).render(
            **{
                **defaults,
                "istota_web_enabled": False,
                "istota_location_enabled": False,
                "istota_webhooks_enabled": True,
            }
        )
        block = _nginx_location(rendered, "/webhooks/whatsapp")

        limit = re.search(r"client_max_body_size\s+(\S+);", block)
        assert limit and _bytes(limit.group(1)) > 256 * 1024
        assert _bytes(limit.group(1)) <= 1024 * 1024
        assert re.search(r"^\s*access_log\s+off;", block, re.M)
        assert "location /webhooks/ {" in rendered

    def test_the_nginx_whatsapp_block_disappears_with_the_receiver(self):
        """Otherwise the assertion above is satisfied by a template that
        always emits the block, including on a deployment with no receiver
        listening behind it."""
        defaults = yaml.safe_load(DEFAULTS_FILE.read_text())
        rendered = Environment().from_string(ANSIBLE_NGINX.read_text()).render(
            **{
                **defaults,
                "istota_web_enabled": True,
                "istota_webhooks_enabled": False,
            }
        )

        assert "/webhooks/whatsapp" not in rendered

    def test_user_ensure_carries_the_whatsapp_enrollment_flags(self):
        tasks = yaml.safe_load(TASKS_FILE.read_text())
        task = next(
            t for t in tasks if t.get("name") == "Ensure user_profiles rows"
        )
        command = task["command"]

        assert "whatsapp_number" in command
        assert "--whatsapp-number" in command
        assert "--clear-whatsapp" in command


def _ansible_ish_environment() -> Environment:
    """Plain jinja2 plus the one Ansible filter the webhook condition uses.

    Ansible is not in the dependency set — `tests/test_ansible_config_template`
    makes the same concession and shims `to_json` and `ternary` for the same
    reason. `bool` is shimmed to Ansible's documented string handling rather
    than to Python truthiness, because `"false" | bool` is False there and
    True in Python, and an operator writing `istota_whatsapp_enabled: "false"`
    in inventory is exactly the case a laxer shim would get backwards.
    """
    env = Environment()
    env.filters["bool"] = _ansible_bool
    return env


def _ansible_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"yes", "on", "1", "true", "t", "y"}
    return bool(value)


def _nginx_location(text: str, path: str) -> str:
    """The body of the first `location [=] <path> {` block, brace-matched."""
    match = re.search(
        rf"location\s+(?:=\s+)?{re.escape(path)}\s*\{{", text
    )
    assert match, f"no location block for {path}"
    depth = 0
    for index in range(match.end() - 1, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[match.end():index]
    raise AssertionError(f"unbalanced braces after location {path}")


def _bytes(size: str) -> int:
    units = {"k": 1024, "m": 1024 * 1024, "g": 1024 ** 3}
    if size[-1].lower() in units:
        return int(size[:-1]) * units[size[-1].lower()]
    return int(size)
