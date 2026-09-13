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

    def test_the_image_runs_the_program_as_the_daemons_user(self):
        """No USER directive, which is load-bearing here.

        `ensure_session_dir` refuses a session directory owned by another uid,
        so the sidecar and the daemon have to be the same user. The istota
        image declares no USER either, so both are root; a `USER node` here
        leaves this container unable to read the credential the daemon paired.
        """
        dockerfile = (REPO / "docker" / "whatsapp-baileys" / "Dockerfile").read_text()

        assert not re.search(r"^USER\s", dockerfile, re.M)
        assert re.search(r"^FROM node:", dockerfile, re.M)
        assert re.search(r"^RUN npm ci\b", dockerfile, re.M)

    def test_the_lockfile_is_committed_and_pins_the_library(self):
        """`npm ci` needs one, and the image's whole install step is `npm ci`.

        Without it the build fails outright — which is the loud direction —
        but the same lockfile is what makes the pinned version the version
        actually installed, in the image and in a checkout alike.
        """
        import json

        directory = REPO / "docker" / "whatsapp-baileys"
        lock = json.loads((directory / "package-lock.json").read_text())
        manifest = json.loads((directory / "package.json").read_text())
        pinned = manifest["dependencies"]["@whiskeysockets/baileys"]

        entry = lock["packages"]["node_modules/@whiskeysockets/baileys"]
        assert entry["version"] == pinned
        assert entry["integrity"]


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
        # A third value reaches the loader the same way: an unknown provider
        # is refused whether or not the block is enabled, so a typo in the
        # inventory is a config no istota process can read.
        assert "istota_whatsapp_provider" in conditions

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
