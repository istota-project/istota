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

#: The two adapter-level variables, kept out of `WHATSAPP_VALUES` on purpose.
#: That set is the Cloud account, and the mount-gate class below depends on it
#: naming no provider — a Cloud render that says `whatsapp_cloud` in so many
#: words stops exercising the compatibility path those tests are about.
WHATSAPP_ADAPTER_VALUES = {
    "ISTOTA_WHATSAPP_PROVIDER": "baileys",
    "ISTOTA_WHATSAPP_BAILEYS_SIDECAR_COMMAND": "/usr/bin/node /opt/sidecar/index.js",
}


class TestTheDockerRender:
    def test_it_writes_every_whatsapp_field_the_operator_can_set(self, tmp_path):
        path = render_docker_config(tmp_path, **REQUIRED, **WHATSAPP_VALUES)
        whatsapp = tomllib.loads(path.read_text())["whatsapp"]

        cloud = whatsapp["cloud"]

        assert whatsapp["enabled"] is True
        assert whatsapp["business_phone_number"] == "+15551230000"
        assert cloud["waba_id"] == "100000000000001"
        assert cloud["phone_number_id"] == "100000000000002"
        assert cloud["access_token"] == "token-placeholder"
        assert cloud["app_secret"] == "app-secret-placeholder"
        assert cloud["verify_token"] == "verify-placeholder"
        assert cloud["graph_api_version"] == "v25.0"
        assert cloud["business_timezone"] == "Europe/Warsaw"
        assert cloud["request_timeout_seconds"] == 8
        assert cloud["billing_policy"] == "allow_paid"
        assert cloud["monthly_service_attempt_limit"] == 400
        assert cloud["proactive_template"] == {
            "enabled": True,
            "name": "istota_result",
            "language": "en_GB",
        }

    def test_the_meta_keys_are_nested_rather_than_flat(self, tmp_path):
        """The generator writes the shape the loader has.

        They were rendered flat on `[whatsapp]` and read as `[whatsapp.cloud]`
        by the legacy-flat migration. `config.example.toml` asks operators to
        move them, so the file istota writes for itself moves them too — and
        the assertion is about *absence*, since the migration would make a
        render carrying both spellings look identical from the loaded config.
        """
        path = render_docker_config(tmp_path, **REQUIRED, **WHATSAPP_VALUES)
        whatsapp = tomllib.loads(path.read_text())["whatsapp"]

        assert set(whatsapp) == {"enabled", "business_phone_number", "cloud", "baileys"}

    def test_the_adapter_keys_render_when_the_operator_names_them(self, tmp_path):
        from istota.config import load_config

        path = render_docker_config(
            tmp_path, **REQUIRED, **WHATSAPP_ADAPTER_VALUES,
        )
        config = load_config(path)

        assert config.whatsapp.provider == "baileys"
        assert config.whatsapp.baileys.sidecar_command == (
            "/usr/bin/node /opt/sidecar/index.js"
        )

    def test_an_unset_provider_renders_no_key_at_all(self, tmp_path):
        """Never `provider = ""`, which no istota process can load.

        `whatsapp_structural_config_errors` refuses a provider outside the
        tuple whether or not the block is enabled, and `load_config` raises on
        it — so on this shape an empty rendered value is a container that will
        not boot.
        """
        path = render_docker_config(tmp_path, **REQUIRED, ISTOTA_WHATSAPP_PROVIDER="")

        assert "provider" not in _whatsapp_section(path.read_text())

    def test_a_provider_the_loader_refuses_fails_the_render(self, tmp_path):
        """The Ansible side asserts this and the render had nothing.

        An unrecognised provider fails `load_config` in every istota process
        and does so whether or not the block is enabled, so `cloud` for
        `whatsapp_cloud` — or a value with a stray space, which `-n` calls
        non-empty and the loader's membership test does not — would write a
        config that crash-loops the scheduler, the web app and the webhook
        receiver alike, on a traceback naming neither the variable nor this
        file. A failed render is survivable: the entrypoint keeps the previous
        config and re-renders next boot.
        """
        for bad in ("cloud", "Baileys", "whatsapp cloud"):
            proc = _render_docker(tmp_path / f"bad-{bad.replace(' ', '-')}", bad)

            assert proc.returncode != 0, f"{bad!r} rendered without complaint"
            assert "ISTOTA_WHATSAPP_PROVIDER" in proc.stderr

    def test_a_provider_with_surrounding_space_still_renders(self, tmp_path):
        """The refusal above must not catch an operator's trailing newline.

        `.env` files and shell exports pick up whitespace routinely, and the
        value is one of three fixed words rather than anything meaningful —
        so it is trimmed and accepted rather than refused.
        """
        from istota.config import load_config

        proc = _render_docker(tmp_path / "spaced", "  baileys\t")
        assert proc.returncode == 0, proc.stderr

        config = load_config(tmp_path / "spaced" / "config.toml")
        assert config.whatsapp.provider == "baileys"

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
        assert whatsapp["cloud"]["billing_policy"] == "free_guard"
        assert whatsapp["cloud"]["monthly_service_attempt_limit"] == 900
        assert whatsapp["cloud"]["proactive_template"]["enabled"] is False

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
        assert config.whatsapp.cloud.proactive_template.name == "istota_result"


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

        for name in {**WHATSAPP_VALUES, **WHATSAPP_ADAPTER_VALUES}:
            assert name in environment, f"compose withholds {name}"

    def test_the_env_example_documents_the_profile_and_every_setting(self):
        text = ENV_EXAMPLE.read_text()

        assert re.search(r"^#\s+whatsapp\s", text, re.M), (
            "the optional-services list at the top of .env.example does not "
            "name the whatsapp profile"
        )
        assert re.search(r"^#\s+whatsapp-baileys\s", text, re.M), (
            "the optional-services list at the top of .env.example does not "
            "name the whatsapp-baileys profile"
        )
        for name in {**WHATSAPP_VALUES, **WHATSAPP_ADAPTER_VALUES}:
            assert re.search(rf"^{name}=", text, re.M), f"undocumented: {name}"


class TestTheBaileysSidecarService:
    """The compose half of the profile split.

    A profile cannot read `provider` out of a config rendered inside the
    container, so the two adapters get two profiles rather than one that means
    different things: `whatsapp` is Meta's receiver and `whatsapp-baileys` is
    the sidecar. Nothing here builds the image — that is the `image` tier's
    kind of work and this service is in no tier — so these assert the shape a
    wrong service would get wrong silently.
    """

    def _service(self):
        compose = yaml.safe_load(COMPOSE.read_text())
        assert "whatsapp-baileys" in compose["services"], (
            "the whatsapp-baileys service is gone; a Baileys deployment on "
            "this shape then has a daemon listening on a socket nothing dials"
        )
        return compose["services"]["whatsapp-baileys"]

    def test_it_carries_its_own_profile_and_never_the_receivers(self):
        service = self._service()
        webhooks = yaml.safe_load(COMPOSE.read_text())["services"]["webhooks"]

        assert service["profiles"] == ["whatsapp-baileys"]
        # The control for the split: the two must not share a profile, or
        # selecting either adapter starts both halves and the receiver's two
        # handlers 404 everything a Baileys deployment has.
        assert "whatsapp-baileys" not in webhooks["profiles"]
        assert "whatsapp" not in service["profiles"]

    def test_it_is_given_both_variables_the_program_requires(self):
        """With either missing the sidecar exits 2 and logs nowhere.

        Its only log destination is a file inside the session directory, which
        is one of the two values — so a service that withholds them produces a
        container that restarts for ever with an empty `docker logs`.
        """
        environment = self._service()["environment"]

        assert environment["ISTOTA_BAILEYS_SOCKET"]
        assert environment["ISTOTA_BAILEYS_SESSION_DIR"]

    def test_the_paths_it_is_given_are_where_the_daemon_puts_them(self, tmp_path):
        """Asked of the product rather than restated.

        The socket has no config override at all and the session directory
        resolves itself when `session_dir` is empty, which is what the render
        leaves it as — so both paths are derived from `db_path`, and a compose
        literal that drifts from that derivation is a sidecar dialling a socket
        nobody is listening on, with no error on either side.
        """
        from istota.config import load_config
        from istota.transport.whatsapp.baileys_bridge import (
            default_session_dir, default_socket_path,
        )

        config = load_config(render_docker_config(tmp_path, **REQUIRED))
        environment = self._service()["environment"]

        assert environment["ISTOTA_BAILEYS_SOCKET"] == str(default_socket_path(config))
        assert environment["ISTOTA_BAILEYS_SESSION_DIR"] == str(
            default_session_dir(config)
        )

    def test_it_shares_the_data_volume_and_publishes_nothing(self):
        service = self._service()

        assert "istota_data:/data" in service["volumes"]
        # Nothing ever dials it: it speaks to the daemon over the volume's
        # socket and to WhatsApp outbound.
        assert "ports" not in service
        assert "expose" not in service

    def test_the_service_builds_the_image_beside_the_program(self):
        """The Dockerfile itself is pinned one file over.

        `tests/test_whatsapp_sidecar_vendoring.py` owns every entry under
        `docker/whatsapp-baileys/`, including what the Dockerfile copies, how
        it installs and that it declares no USER. What belongs here is only
        that this service is the thing that builds it, and from the small
        context rather than the repository root.
        """
        build = self._service()["build"]

        assert build["context"] == "whatsapp-baileys"
        assert build["dockerfile"] == "Dockerfile"


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
        assert parsed["cloud"]["waba_id"] == "100000000000001"
        assert parsed["cloud"]["business_timezone"] == "Europe/Warsaw"
        # The Meta keys are nested, matching where the loader has them; the
        # role used to render them flat and lean on the legacy-flat migration.
        assert set(parsed) == {
            "enabled", "business_phone_number", "cloud", "baileys",
        }
        # `istota_use_environment_file` defaults on, so the three credentials
        # travel in secrets.env and this file must not name them at all.
        assert "access_token" not in parsed["cloud"]
        assert "app_secret" not in parsed["cloud"]
        assert "verify_token" not in parsed["cloud"]

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
            assert config.whatsapp.cloud.business_timezone == "Europe/Warsaw"
            assert config.whatsapp.cloud.request_timeout_seconds == 8
            assert config.whatsapp.cloud.monthly_service_attempt_limit == 400
            assert config.whatsapp.cloud.billing_policy == "free_guard"
            assert bool(config.whatsapp.cloud.app_secret) is not env_file

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

        assert config.whatsapp.cloud.billing_policy == "allow_paid"
        assert config.whatsapp.cloud.proactive_template.enabled is True
        assert config.whatsapp.cloud.proactive_template.name == "istota_result"
        assert config.whatsapp.cloud.proactive_template.language == "en_GB"

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

        assert parsed["cloud"]["access_token"] == "token-placeholder"
        assert parsed["cloud"]["app_secret"] == "app-secret-placeholder"
        assert parsed["cloud"]["verify_token"] == "verify-placeholder"

    def test_the_adapter_keys_render_when_the_inventory_names_them(self, tmp_path):
        from istota.config import load_config

        path = tmp_path / "config.toml"
        path.write_text(render_ansible_config(
            istota_whatsapp_enabled=True,
            istota_whatsapp_provider="baileys",
            istota_whatsapp_baileys_sidecar_command="/usr/bin/node /opt/sidecar/index.js",
        ))
        config = load_config(path)

        assert config.whatsapp.provider == "baileys"
        assert config.whatsapp.baileys.sidecar_command == (
            "/usr/bin/node /opt/sidecar/index.js"
        )

    def test_an_unset_provider_renders_no_key_at_all(self, tmp_path):
        """Never `provider = ""`, which fails the load in every process.

        On this shape that is a play failing at its first CLI task, having
        already replaced the running deployment's config — the failure the
        pre-flight assert two tests down exists to prevent.
        """
        rendered = render_ansible_config(istota_whatsapp_provider="")

        assert "provider" not in _whatsapp_section(rendered)

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

    def test_a_baileys_deployment_starts_no_webhook_receiver(self):
        """The adapter half of the same arm.

        Baileys takes its inbound over a Unix socket, so `whatsapp_
        webhooks_enabled` is False for it and a receiver provisioned here
        would be a unit whose two handlers 404 everything. The `whatsapp_cloud`
        and empty cases both provision one — empty because the role cannot
        read the Meta values without copying the loader's signal rule, and
        provisioning a receiver nothing calls is the harmless direction.
        """
        defaults = yaml.safe_load(DEFAULTS_FILE.read_text())
        tasks = yaml.safe_load(TASKS_FILE.read_text())
        derive = next(
            task for task in tasks
            if task.get("name") == "Resolve webhook receiver need"
        )
        template = _ansible_ish_environment().from_string(
            derive["set_fact"]["istota_webhooks_enabled"]
        )
        base = {
            **defaults,
            "istota_location_enabled": False,
            "istota_sms_enabled": False,
            "istota_whatsapp_enabled": True,
        }

        for provider, expected in (
            ("baileys", "False"),
            ("whatsapp_cloud", "True"),
            ("", "True"),
        ):
            rendered = template.render(
                {**base, "istota_whatsapp_provider": provider},
            ).strip()
            assert rendered == expected, f"{provider!r} -> {rendered!r}"

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
        conditions = " ".join(_preflight_clauses(tasks))

        assert "istota_whatsapp_billing_policy" in conditions
        assert "istota_whatsapp_template_enabled" in conditions
        # A third value reaches the loader the same way: an unknown provider
        # is refused whether or not the block is enabled, so a typo in the
        # inventory is a config no istota process can read.
        assert "istota_whatsapp_provider" in conditions

        # The assert has to run whatever `enabled` says, because an unknown
        # billing policy fails the load on a disabled block too.
        task = next(t for t in tasks if t.get("name") == PREFLIGHT_ASSERT)
        assert "istota_whatsapp_enabled" not in str(task.get("when", ""))

    def test_the_whatsapp_asserts_pass_on_the_defaults(self):
        """Otherwise every deploy that never touched WhatsApp fails."""
        defaults = yaml.safe_load(DEFAULTS_FILE.read_text())
        tasks = yaml.safe_load(TASKS_FILE.read_text())
        env = _ansible_ish_environment()

        for clause in _preflight_clauses(tasks):
            rendered = env.from_string("{{ " + clause + " }}").render(defaults)
            assert rendered.strip() == "True", f"{clause!r} -> {rendered!r}"

    def test_the_whatsapp_asserts_catch_both_bad_shapes(self):
        defaults = yaml.safe_load(DEFAULTS_FILE.read_text())
        tasks = yaml.safe_load(TASKS_FILE.read_text())
        env = _ansible_ish_environment()
        clauses = _preflight_clauses(tasks)

        for label, bad in (
            ("typo in the policy", {"istota_whatsapp_billing_policy": "free-guard"}),
            ("template under free_guard", {"istota_whatsapp_template_enabled": True}),
            ("typo in the provider", {"istota_whatsapp_provider": "whatsapp-cloud"}),
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


class TestTheAnsibleSidecarUnit:
    """The bare-metal half of the adapter split.

    Nothing here runs the role — that is the `deploy` tier's job and it needs
    Docker — so these assert the parse, which is what the fourteen
    `test_ansible_*.py` files do and what they cannot see past.
    """

    UNIT = ANSIBLE / "templates" / "istota-whatsapp-baileys.service.j2"

    def _tasks(self):
        return yaml.safe_load(TASKS_FILE.read_text())

    def _named(self, name: str):
        task = next((t for t in self._tasks() if t.get("name") == name), None)
        assert task is not None, f"the role has no {name!r} task"
        return task

    def _unit(self, **overrides) -> str:
        defaults = yaml.safe_load(DEFAULTS_FILE.read_text())
        return _ansible_ish_environment().from_string(self.UNIT.read_text()).render(
            **{
                **defaults,
                "istota_namespace": "istota",
                "istota_user": "istota",
                "istota_group": "istota",
                "istota_home": "/srv/app/istota",
                "istota_repo_dir": "/srv/app/istota/src",
                **overrides,
            }
        )

    def test_the_unit_is_wanted_only_by_a_baileys_deployment(self):
        defaults = yaml.safe_load(DEFAULTS_FILE.read_text())
        expression = self._named("Resolve WhatsApp sidecar need")["set_fact"][
            "istota_whatsapp_baileys_unit_wanted"
        ]
        template = _ansible_ish_environment().from_string(expression)

        for label, overrides, expected in (
            ("the defaults", {}, "False"),
            (
                "cloud",
                {"istota_whatsapp_enabled": True,
                 "istota_whatsapp_provider": "whatsapp_cloud"},
                "False",
            ),
            (
                "an unnamed provider",
                {"istota_whatsapp_enabled": True},
                "False",
            ),
            (
                "baileys",
                {"istota_whatsapp_enabled": True,
                 "istota_whatsapp_provider": "baileys"},
                "True",
            ),
            (
                "baileys with the unit turned off",
                {"istota_whatsapp_enabled": True,
                 "istota_whatsapp_provider": "baileys",
                 "istota_whatsapp_baileys_sidecar_unit": False},
                "False",
            ),
        ):
            rendered = template.render({**defaults, **overrides}).strip()
            assert rendered == expected, f"{label} -> {rendered!r}"

    def test_the_unit_is_given_both_variables_the_program_requires(self):
        rendered = self._unit()

        assert (
            "Environment=ISTOTA_BAILEYS_SOCKET="
            "/srv/app/istota/data/whatsapp-baileys.sock" in rendered
        )
        assert (
            "Environment=ISTOTA_BAILEYS_SESSION_DIR="
            "/srv/app/istota/data/whatsapp-baileys-session" in rendered
        )

    def test_the_paths_it_is_given_are_where_the_daemon_puts_them(self, tmp_path):
        """Asked of the product, against this role's own rendered config.

        The socket name has no config override and `session_dir` is left unset
        in the render, so both resolve from db_path. A unit that drifts from
        that derivation is a sidecar dialling a socket nobody is listening on,
        with no error on either side.
        """
        from istota.config import load_config
        from istota.transport.whatsapp.baileys_bridge import (
            default_session_dir, default_socket_path,
        )

        home = "/srv/app/istota"
        path = tmp_path / "config.toml"
        path.write_text(render_ansible_config(istota_home=home))
        config = load_config(path)
        rendered = self._unit(istota_home=home)

        assert f"Environment=ISTOTA_BAILEYS_SOCKET={default_socket_path(config)}" in rendered
        assert (
            f"Environment=ISTOTA_BAILEYS_SESSION_DIR={default_session_dir(config)}"
            in rendered
        )

    def test_it_restarts_a_sidecar_that_exited_on_a_dead_session(self):
        """`Restart=always` is the recovery path, not a default copied across.

        A logged-out sidecar exits 1 deliberately so systemd brings it back;
        the daemon's in-process supervisor refuses to respawn a permanent
        fatal, so on this shape the unit is what lets a re-pair take effect
        without restarting the scheduler.
        """
        rendered = self._unit()

        assert re.search(r"^Restart=always$", rendered, re.M)
        assert re.search(r"^RestartSec=\d+$", rendered, re.M)

    def test_a_scheduler_restart_does_not_take_the_session_down(self):
        """Wants, never Requires.

        `Requires=` propagates a stop, so a scheduler restart would drop the
        WhatsApp link on every deploy. The sidecar retries a missing socket
        every two seconds, so the ordering is a courtesy and the dependency is
        not one.
        """
        rendered = self._unit()

        assert "Wants=istota-scheduler.service" in rendered
        assert "Requires=" not in rendered

    def test_it_discards_the_programs_own_output(self):
        """stdout is the channel Baileys would be chatty on.

        The program writes nothing there by contract; stderr is kept because
        what reaches it is Node's rather than the program's — a module that
        would not load — and a unit that flaps with no message anywhere is the
        worse failure.
        """
        rendered = self._unit()

        assert "StandardOutput=null" in rendered
        assert "StandardError=journal" in rendered

    def test_the_session_directory_is_created_private_and_owned(self):
        """0700 and the daemon's, or the bridge refuses to start.

        `ensure_session_dir` refuses a directory owned by another uid rather
        than adopting it, so a root-owned one left by a hand-run command is a
        bridge that will not start with the credential unreadable.
        """
        task = self._named("Ensure the WhatsApp Baileys session directory")

        assert task["file"]["mode"] == "0700"
        assert task["file"]["owner"] == "{{ istota_user }}"
        assert task["file"]["path"].endswith("/data/whatsapp-baileys-session")

    def test_the_dependency_install_is_gated_on_the_lockfile(self):
        """`npm ci` deletes node_modules before it installs.

        Unconditional, it tears the tree out from under a running sidecar on
        every deploy; on `creates:` it would never reinstall after a version
        bump. The gate is the lockfile's own checksum against a marker written
        after a successful install.
        """
        install = self._named("Install the WhatsApp sidecar's dependencies")
        record = self._named("Record the installed WhatsApp sidecar lockfile")
        resolve = self._named(
            "Resolve whether the WhatsApp sidecar needs a dependency install"
        )
        fact = resolve["set_fact"]["istota_whatsapp_baileys_install_needed"]

        assert "npm ci" in install["command"]
        assert "baileys_lockfile.stat.checksum" in fact
        assert "baileys_installed_marker.content" in fact
        assert "restart istota-whatsapp-baileys" in install["notify"]
        # The marker is written after the install and inside node_modules, so a
        # failed `npm ci` records nothing and `rm -rf node_modules` forces one.
        assert "/node_modules/.istota-lockfile-sha256" in record["copy"]["dest"]
        assert self._tasks().index(record) > self._tasks().index(install)

    def test_a_missing_lockfile_refuses_rather_than_skips(self):
        """A skip here is a flapping unit and a green play.

        The install is gated on the lockfile existing, so a repo directory
        that is not a checkout skipped it silently — and the unit is deployed,
        enabled and started on a condition that never mentions node_modules.
        `index.js` exits 3 on a missing module under `Restart=always`, so the
        result is one exit every restart interval for the life of the host
        while `ansible-playbook` reports ok.
        """
        task = self._named("Assert the WhatsApp sidecar lockfile is present")

        assert "baileys_lockfile.stat.exists" in str(task["assert"]["that"])
        assert "istota_whatsapp_baileys_unit_wanted" in str(task["when"])
        # Ahead of the install and therefore ahead of the unit.
        install = self._named("Install the WhatsApp sidecar's dependencies")
        assert self._tasks().index(task) < self._tasks().index(install)

    def test_the_sidecar_is_stopped_for_its_own_reinstall(self):
        """`npm ci` deletes node_modules before it installs.

        The running process survives on modules it has already required, but
        `Restart=always` means a crash inside that window re-execs against a
        half-populated tree and loops on exit 3 until the install finishes.
        The three tasks share one fact rather than one condition spelled three
        times, because the stop, the install and the marker have to agree.
        """
        stop = self._named("Stop the WhatsApp sidecar for its dependency install")
        install = self._named("Install the WhatsApp sidecar's dependencies")
        record = self._named("Record the installed WhatsApp sidecar lockfile")

        assert stop["systemd"]["state"] == "stopped"
        for task in (stop, install, record):
            assert "istota_whatsapp_baileys_install_needed" in str(task["when"])
        assert self._tasks().index(stop) < self._tasks().index(install)

    def test_the_install_takes_the_roles_own_update_lock(self):
        """Every other mutating build command in the role takes it.

        The auto-update cron reinstalls this same directory, so without the
        lock a play landing inside a cron run can be part-way through its own
        `npm ci` against the tree this one is deleting.
        """
        install = self._named("Install the WhatsApp sidecar's dependencies")

        assert "flock -w" in install["command"]
        assert "-update.lock" in install["command"]

    def test_the_unit_backs_off_between_restarts(self):
        """The steady state of the documented recovery path is a restart loop.

        A de-paired session is start-log-exit until a person scans a code, and
        the only place the sidecar writes is a file inside the 0700 credential
        directory that nothing else surfaces. At five seconds that is twelve
        entries a minute for as long as the account stays unlinked.
        """
        rendered = self._unit()
        seconds = re.search(r"^RestartSec=(\d+)$", rendered, re.M)

        assert seconds and int(seconds.group(1)) >= 30

    def test_the_sidecar_log_is_rotated(self):
        """It is not under /var/log, so the existing glob does not reach it."""
        defaults = yaml.safe_load(DEFAULTS_FILE.read_text())
        rendered = _ansible_ish_environment().from_string(
            (ANSIBLE / "templates" / "istota-logrotate.j2").read_text()
        ).render(
            **{
                **defaults,
                "istota_namespace": "istota",
                "istota_user": "istota",
                "istota_group": "istota",
                "istota_home": "/srv/app/istota",
                "istota_whatsapp_baileys_unit_wanted": True,
            }
        )

        assert "/srv/app/istota/data/whatsapp-baileys-session/sidecar.log" in rendered
        assert "create 0600 istota istota" in rendered
        # The writer appends and never reopens, so a rename would leave it
        # writing to an unlinked inode. Counted as directives rather than as
        # occurrences, since the stanza's comment names the keyword too.
        directives = [
            line.strip() for line in rendered.splitlines()
            if line.strip() == "copytruncate"
        ]
        assert len(directives) == 2

    def test_the_sidecar_log_stanza_goes_with_the_unit(self):
        """A rotate entry for a file no deployment has is harmless and
        misleading; logrotate would report a missing file on every host."""
        defaults = yaml.safe_load(DEFAULTS_FILE.read_text())
        rendered = _ansible_ish_environment().from_string(
            (ANSIBLE / "templates" / "istota-logrotate.j2").read_text()
        ).render(**{**defaults, "istota_namespace": "istota",
                    "istota_user": "istota", "istota_group": "istota",
                    "istota_home": "/srv/app/istota"})

        assert "whatsapp-baileys-session" not in rendered

    def test_the_unit_writes_no_wider_than_it_needs(self):
        """Narrower than the sibling units', deliberately.

        Those run istota's own code; this one runs a third-party dependency
        tree while holding a full WhatsApp account, and everything it writes is
        under the data directory. Naming the parent would hand it the framework
        database, every module database and the checkout it executes from.
        """
        rendered = self._unit()

        assert "ReadWritePaths=/srv/app/istota/data" in rendered
        assert "ReadWritePaths=/srv/app/istota\n" not in rendered

    def test_the_unit_creates_nothing_wider_than_the_credential(self):
        """systemd's default `UMask` is 0022, so every session file Baileys
        wrote under this unit landed 0644 — a full-account WhatsApp credential
        readable by every account on the host.

        Defence in depth rather than the mechanism: the program sets its own
        umask, which is the only thing that reaches the compose shape as well,
        since a compose service cannot express one. This is the half a reader
        of the unit can see.
        """
        assert "UMask=0077" in self._unit()

    def test_the_unit_carries_a_memory_ceiling(self):
        """As on the web and webhook units, and omitted when set empty."""
        assert "MemoryHigh=512M" in self._unit()
        assert "MemoryHigh" not in self._unit(istota_whatsapp_baileys_memory_high="")

    def test_the_teardown_says_the_credential_is_still_there(self):
        """Said rather than done: deleting a credential on an operator's
        behalf during a routine converge is the wrong default, and leaving it
        unnamed is how it stays on disk unnoticed."""
        task = self._named("Note the WhatsApp session left behind")

        assert "whatsapp-baileys-session" in task["debug"]["msg"]
        assert "not (istota_whatsapp_baileys_unit_wanted | bool)" in str(task["when"])

    def test_the_node_install_reaches_a_baileys_host(self):
        """Otherwise the unit fails at ExecStart with nothing above it.

        The role installs node for the frontend build and the developer skill;
        a Baileys host with the web UI off is a third case that had no arm.
        """
        install = self._named("Install Node.js for developer skill")

        assert "istota_whatsapp_baileys_unit_wanted" in str(install["when"])

    def test_the_update_only_mode_restarts_it(self):
        """Handlers cannot reach it, so the explicit loop has to.

        The only task that notifies its restart is the dependency install,
        which is gated on the lockfile checksum — so a commit touching
        `index.js` alone reports no change anywhere and nothing fires. The
        unit would go on running the copy it loaded at start, under the mode
        whose whole job is "pull latest code, restart services".
        """
        task = self._named("Restart services (update-only mode)")
        names = [item["name"] for item in task["loop"]]

        assert "{{ istota_namespace }}-whatsapp-baileys" in names
        entry = next(i for i in task["loop"] if i["name"].endswith("whatsapp-baileys"))
        assert "istota_whatsapp_baileys_unit_wanted" in entry["enabled"]

    def test_the_auto_update_cron_reaches_it(self):
        """On the reference host the two-minute cron is the deploy path.

        It restarts the Python units because they hold their code in memory;
        the sidecar is a Node program run out of the same checkout and holds
        its own the same way. Its dependencies are the worse half — a
        cron-delivered lockfile bump leaves node_modules matching the old one,
        and the failure surfaces at the next unrelated restart, disconnected
        from the commit that caused it. This is ISSUE-428's class on a second
        non-Python artifact.
        """
        cron = (ANSIBLE / "templates" / "istota-update.sh.j2").read_text()

        assert 'systemctl restart "${NAMESPACE}-whatsapp-baileys"' in cron
        assert "docker/whatsapp-baileys/package-lock.json" in cron
        assert "npm ci --omit=dev" in cron
        # Stopped before the install for the reason the play stops it: `npm ci`
        # deletes node_modules first and the unit is Restart=always.
        assert 'systemctl stop "${NAMESPACE}-whatsapp-baileys"' in cron

    def test_the_cron_arms_are_gated_on_the_same_fact_as_the_unit(self):
        """Otherwise a Cloud host's cron restarts a unit that is not there."""
        cron = (ANSIBLE / "templates" / "istota-update.sh.j2").read_text()

        for line in cron.splitlines():
            if "whatsapp-baileys" in line and line.strip().startswith("{%"):
                assert "istota_whatsapp_baileys_unit_wanted" in line
        assert cron.count(
            "{% if istota_whatsapp_baileys_unit_wanted | default(false) %}"
        ) == 2

    def test_a_switch_away_tears_the_sidecar_down(self):
        """The half that is not bookkeeping.

        A host moving to `whatsapp_cloud`, or turning the surface off, would
        otherwise keep a sidecar holding the session directory — and the moment
        anyone pairs again that survivor is the second Baileys client the
        single-writer rule exists to prevent.
        """
        stop = self._named("Stop and disable WhatsApp Baileys sidecar when not wanted")
        remove = self._named(
            "Remove WhatsApp Baileys sidecar service file when not wanted"
        )

        assert stop["systemd"]["state"] == "stopped"
        assert stop["systemd"]["enabled"] is False
        assert "not (istota_whatsapp_baileys_unit_wanted | bool)" in str(stop["when"])
        assert remove["file"]["state"] == "absent"

    def test_the_role_refuses_two_sidecars(self):
        """The single-writer rule, in the one place that can see both halves.

        Neither process can detect the other — which is why `istota whatsapp
        pair` refuses a whole running daemon rather than trying to — so the
        pairing has to be refused before it is deployed.
        """
        defaults = yaml.safe_load(DEFAULTS_FILE.read_text())
        env = _ansible_ish_environment()
        clauses = _preflight_clauses(self._tasks())
        both = {
            "istota_whatsapp_enabled": True,
            "istota_whatsapp_provider": "baileys",
            "istota_whatsapp_baileys_sidecar_command": "/usr/bin/node /opt/s/index.js",
        }
        verdicts = [
            env.from_string("{{ " + clause + " }}").render({**defaults, **both}).strip()
            for clause in clauses
        ]
        assert "False" in verdicts, "nothing refuses a unit plus a spawned sidecar"

        # The control: with the unit turned off, naming a command is exactly
        # how an operator runs the sidecar somewhere else, and must pass.
        allowed = {**both, "istota_whatsapp_baileys_sidecar_unit": False}
        for clause in clauses:
            rendered = env.from_string("{{ " + clause + " }}").render(
                {**defaults, **allowed}
            ).strip()
            assert rendered == "True", f"{clause!r} -> {rendered!r}"


class TestTheMountGateOnALoadedConfig:
    """What the shipped generators produce, loaded, decides the webhook mount.

    This is the hazard the provider default carries and the one place it can be
    settled honestly. `whatsapp_webhooks_enabled` reads `provider`, both
    generators still render the pre-adapter flat block, and the default is now
    `baileys` — so if the flat block did not resolve back to `whatsapp_cloud`,
    every existing Cloud deployment would stop serving Meta's callback at the
    upgrade, with nothing in any log saying so.

    Hand-written TOML cannot answer it: the question is about what these two
    files emit, including the shape where the role deliberately omits the three
    credentials.

    Both generators now write the nested `[whatsapp.cloud]` shape and render a
    `provider` key only when the operator names one, so these still assert the
    compatibility path — it is just the nested half of it. That the *nested*
    read is what answers here rather than the flat one is the property
    `_WHATSAPP_CLOUD_SIGNAL_KEYS` records being deliberate: moving the keys is
    a spelling, and their being Meta identifiers is the fact.
    """

    def _ansible(self, tmp_path, name: str, **overrides):
        from istota.config import load_config

        path = tmp_path / name
        path.write_text(render_ansible_config(**overrides))
        return load_config(path)

    def test_an_ansible_cloud_render_still_serves_metas_callback(self, tmp_path):
        from istota.config import whatsapp_webhooks_enabled

        config = self._ansible(
            tmp_path, "cloud.toml",
            istota_whatsapp_enabled=True,
            istota_whatsapp_waba_id="100000000000001",
            istota_whatsapp_phone_number_id="100000000000002",
            istota_whatsapp_business_phone_number="+15551230000",
        )

        assert config.whatsapp.provider == "whatsapp_cloud"
        assert whatsapp_webhooks_enabled(config) is True

    def test_the_env_file_shape_resolves_on_the_ids_alone(self, tmp_path):
        """Under `istota_use_environment_file` the role renders no
        `access_token`, `app_secret` or `verify_token` line at all, so a
        migration keyed on the credentials would read the deployment this rule
        most has to protect as a non-Cloud one."""
        from istota.config import whatsapp_webhooks_enabled

        config = self._ansible(
            tmp_path, "envfile.toml",
            istota_use_environment_file=True,
            istota_whatsapp_enabled=True,
            istota_whatsapp_waba_id="100000000000001",
            istota_whatsapp_phone_number_id="100000000000002",
            istota_whatsapp_business_phone_number="+15551230000",
        )

        assert config.whatsapp.cloud.access_token == ""
        assert config.whatsapp.provider == "whatsapp_cloud"
        assert whatsapp_webhooks_enabled(config) is True

    def test_the_default_ansible_render_is_a_baileys_deployment(self, tmp_path):
        """The role renders the whole flat block whether or not WhatsApp is
        configured, so this is the render on every istota host there is. It
        must not read as a Cloud deployment."""
        from istota.config import whatsapp_webhooks_enabled

        config = self._ansible(tmp_path, "default.toml")

        assert config.whatsapp.enabled is False
        assert config.whatsapp.provider == "baileys"
        assert whatsapp_webhooks_enabled(config) is False

    def test_a_docker_cloud_render_still_serves_metas_callback(self, tmp_path):
        from istota.config import load_config, whatsapp_webhooks_enabled

        path = render_docker_config(tmp_path, **REQUIRED, **WHATSAPP_VALUES)
        config = load_config(path)

        assert config.whatsapp.provider == "whatsapp_cloud"
        assert config.whatsapp.cloud.waba_id == "100000000000001"
        assert whatsapp_webhooks_enabled(config) is True

    def test_the_default_docker_render_is_a_baileys_deployment(self, tmp_path):
        from istota.config import load_config, whatsapp_webhooks_enabled

        path = render_docker_config(tmp_path, **REQUIRED)
        config = load_config(path)

        assert config.whatsapp.enabled is False
        assert config.whatsapp.provider == "baileys"
        assert whatsapp_webhooks_enabled(config) is False

    def test_neither_generator_renders_a_provider_key_at_its_default(self, tmp_path):
        """What the four tests above rest on, stated where it can go red.

        Both generators can now render `provider`, and neither does unless the
        operator names one. If either starts writing a value by default, the
        Cloud assertions above stop being about the migration and — at the
        `baileys` default — an existing Cloud deployment loses Meta's callback
        at the upgrade with nothing in any log saying so.
        """
        ansible = render_ansible_config()
        docker = render_docker_config(tmp_path, **REQUIRED).read_text()

        assert "provider" not in _whatsapp_section(ansible)
        assert "provider" not in _whatsapp_section(docker)

    def test_a_named_provider_outranks_a_populated_cloud_block(self, tmp_path):
        """The escape hatch from the rule above, on both shapes.

        A deployment switching Cloud to Baileys keeps its Cloud block — the SMS
        switch rule, so late delivery callbacks still authenticate — and says
        so explicitly. An explicit value is never overridden by the signal.
        """
        from istota.config import load_config, whatsapp_webhooks_enabled

        ansible_path = tmp_path / "ansible.toml"
        ansible_path.write_text(render_ansible_config(
            istota_whatsapp_enabled=True,
            istota_whatsapp_provider="baileys",
            istota_whatsapp_waba_id="100000000000001",
            istota_whatsapp_phone_number_id="100000000000002",
        ))
        docker_path = render_docker_config(
            tmp_path / "docker", **REQUIRED, **WHATSAPP_VALUES,
            ISTOTA_WHATSAPP_PROVIDER="baileys",
        )

        for path in (ansible_path, docker_path):
            config = load_config(path)
            assert config.whatsapp.provider == "baileys", path
            assert whatsapp_webhooks_enabled(config) is False, path


#: The pre-flight assert's own name. The three walks below evaluate its clauses
#: against `defaults/main.yml` alone, which is what makes them meaningful — it
#: runs before anything is registered and reads nothing but inventory. There is
#: now a second WhatsApp assert, `Assert the WhatsApp sidecar lockfile is
#: present`, whose clause reads a `stat` result; rendering that against defaults
#: raises `UndefinedError`, and admitting it would make these tests fail for a
#: reason unrelated to what they check. Named rather than filtered, so a walk
#: cannot quietly stop covering the assert it exists for.
PREFLIGHT_ASSERT = "Assert WhatsApp settings the config loader accepts"


def _preflight_clauses(tasks: list) -> list[str]:
    task = next((t for t in tasks if t.get("name") == PREFLIGHT_ASSERT), None)
    assert task is not None, (
        f"the role has no {PREFLIGHT_ASSERT!r} task; an inventory value the "
        "config loader refuses would then be met by the first CLI task, with "
        "the running deployment's config already replaced"
    )
    return [str(clause) for clause in task["assert"]["that"]]


def _render_docker(directory: Path, provider: str):
    """`render-config.sh` run without asserting it succeeded.

    `tests.test_render_config.render` requires exit 0, which is the whole
    question for a refusal, so this is the same call with that assertion
    removed — and with the environment built from scratch for the reason that
    helper states: a developer host exports `ISTOTA_*` variables routinely.
    """
    import os
    import subprocess

    directory.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        ["bash", str(REPO / "docker" / "istota" / "render-config.sh")],
        env={
            "PATH": os.environ.get("PATH", ""),
            "CONFIG_FILE": str(directory / "config.toml"),
            **REQUIRED,
            "ISTOTA_WHATSAPP_PROVIDER": provider,
        },
        capture_output=True,
        text=True,
        timeout=120,
    )


def _whatsapp_section(rendered: str) -> str:
    """The `[whatsapp]` block's own lines, excluding its sub-tables."""
    lines = rendered.splitlines()
    start = lines.index("[whatsapp]")
    body: list[str] = []
    for line in lines[start + 1:]:
        if line.startswith("["):
            break
        body.append(line)
    return "\n".join(body)


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
