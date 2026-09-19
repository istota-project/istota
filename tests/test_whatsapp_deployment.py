"""Deployment wiring for the WhatsApp Cloud API webhook surface.

Written against `tests/test_sms_deployment.py`, which did this job one spec
earlier. The two surfaces share one webhook process, one nginx prefix and one
`secrets.env`, so the assertions that matter here are the ones about them
*sharing* rather than each having its own: a deployment with location, SMS and
WhatsApp all on runs one receiver, and every gate that decides whether it runs
has to name all three.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import subprocess
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
SIDECAR = REPO / "docker" / "whatsapp-baileys" / "index.js"


def derive_media_dir(session_dir: str) -> str:
    """What the sidecar itself would stage into, given only a session path.

    ISSUE-508. The two deployment literals used to be load-bearing — four
    sites had to agree or the surface was down — and are now the thing that
    pins this derivation against `media.default_media_dir`. Asking the
    program rather than restating its rule is the whole point: a restated
    `dirname` here would agree with a sidecar that had stopped deriving.

    Driven through `node` with no `node_modules`, which the program supports
    because `loadBaileys` is a lazy dynamic import and the file is CommonJS.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    result = subprocess.run(
        [
            node, "-e",
            f"const m = require({json.dumps(str(SIDECAR))});"
            f"process.stdout.write(m.deriveMediaDir({json.dumps(session_dir)}));",
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout
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

    def test_it_is_given_every_variable_the_program_requires(self):
        """With any one missing the sidecar exits 2 and logs nowhere.

        Its only log destination is a file inside the session directory, which
        is one of the three values — so a service that withholds them produces
        a container that restarts for ever with an empty `docker logs`.

        The media directory joined the pair once the sidecar started staging
        inbound photos, and it is the one that could be forgotten quietly: a
        deployment that upgrades to this image without the variable loses the
        whole WhatsApp surface, text included, rather than losing images.
        """
        environment = self._service()["environment"]

        assert environment["ISTOTA_BAILEYS_SOCKET"]
        assert environment["ISTOTA_BAILEYS_SESSION_DIR"]
        assert environment["ISTOTA_BAILEYS_MEDIA_DIR"]

    def test_the_paths_it_is_given_are_where_the_daemon_puts_them(self, tmp_path):
        """Asked of the product rather than restated.

        The socket has no config override at all, the session directory
        resolves itself when `session_dir` is empty, which is what the render
        leaves it as, and the media directory has no override in any shape — so
        all three paths are derived from `db_path`, and a compose literal that
        drifts from that derivation is a sidecar writing where the daemon never
        reads, with no error on either side.
        """
        from istota.config import load_config
        from istota.transport.whatsapp.baileys_bridge import (
            default_session_dir, default_socket_path,
        )
        from istota.transport.whatsapp.media import default_media_dir

        config = load_config(render_docker_config(tmp_path, **REQUIRED))
        environment = self._service()["environment"]

        assert environment["ISTOTA_BAILEYS_SOCKET"] == str(default_socket_path(config))
        assert environment["ISTOTA_BAILEYS_SESSION_DIR"] == str(
            default_session_dir(config)
        )
        assert environment["ISTOTA_BAILEYS_MEDIA_DIR"] == str(
            default_media_dir(config)
        )

    def test_the_literal_and_the_sidecars_own_derivation_agree(self):
        """ISSUE-508. The variable is an override now, so the two answers
        have to be the same one — otherwise a container started from a
        compose file that predates it stages somewhere the daemon never
        reads, which is a quieter version of the outage this replaced.

        This is what the literal is *for* now. It used to be load-bearing
        (absent, the sidecar exited 2 and the surface went down); it is now
        the fixed point that pins the derivation.
        """
        environment = self._service()["environment"]

        assert derive_media_dir(environment["ISTOTA_BAILEYS_SESSION_DIR"]) == (
            environment["ISTOTA_BAILEYS_MEDIA_DIR"]
        )

    def test_the_media_directory_is_inside_the_volume_both_containers_share(
        self, tmp_path,
    ):
        """The staging directory's second reason, asserted rather than assumed.

        A photo is written by this container and read by the daemon in the
        other one, so the two must see the same inodes. `istota_data:/data` is
        already mounted for the socket and the session directory; this asserts
        that the media directory falls inside it rather than needing a volume
        of its own, which is the property that let the directory be added with
        no compose change beyond one line.
        """
        from istota.config import load_config
        from istota.transport.whatsapp.media import default_media_dir

        config = load_config(render_docker_config(tmp_path, **REQUIRED))
        service = self._service()

        istota = yaml.safe_load(COMPOSE.read_text())["services"]["istota"]

        assert "istota_data:/data" in service["volumes"]
        # **Both sides, because the property is that they see one inode set.**
        # This container writes the file and the daemon in the istota
        # container reads it back and copies it out, so asserting the
        # sidecar's mount alone names a two-sided property and checks one.
        assert "istota_data:/data" in istota["volumes"]
        assert str(default_media_dir(config)).startswith("/data/")

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

    def test_the_unit_is_given_every_variable_the_program_requires(self):
        rendered = self._unit()

        assert (
            "Environment=ISTOTA_BAILEYS_SOCKET="
            "/srv/app/istota/data/whatsapp-baileys.sock" in rendered
        )
        assert (
            "Environment=ISTOTA_BAILEYS_SESSION_DIR="
            "/srv/app/istota/data/whatsapp-baileys-session" in rendered
        )
        # The third, and the one whose absence is loudest out of proportion to
        # what it is for: the sidecar exits 2 without it, `Restart=always`
        # brings it straight back, and a deployment that stages no photos
        # loses text messages too.
        assert (
            "Environment=ISTOTA_BAILEYS_MEDIA_DIR="
            "/srv/app/istota/data/whatsapp-media" in rendered
        )

    def test_the_paths_it_is_given_are_where_the_daemon_puts_them(self, tmp_path):
        """Asked of the product, against this role's own rendered config.

        The socket name has no config override, `session_dir` is left unset in
        the render, and the media directory has no override in any shape, so
        all three resolve from db_path. A unit that drifts from that derivation
        is a sidecar writing where the daemon never reads, with no error on
        either side.
        """
        from istota.config import load_config
        from istota.transport.whatsapp.baileys_bridge import (
            default_session_dir, default_socket_path,
        )
        from istota.transport.whatsapp.media import default_media_dir

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
        assert (
            f"Environment=ISTOTA_BAILEYS_MEDIA_DIR={default_media_dir(config)}"
            in rendered
        )

    def test_the_literal_and_the_sidecars_own_derivation_agree(self, tmp_path):
        """ISSUE-508, the Ansible half of the same fixed point.

        The rendered unit is the one an upgrade leaves stale — the two-minute
        update cron ships the program and cannot re-render this file — so the
        property that matters is that a sidecar given only the session
        literal lands on the media literal anyway.
        """
        home = "/srv/app/istota"
        rendered = self._unit(istota_home=home)
        values = {}
        for line in rendered.splitlines():
            if line.startswith("Environment=ISTOTA_BAILEYS_"):
                name, _, value = line[len("Environment="):].partition("=")
                values[name] = value

        # `partition` would hand back an empty value for a quoted
        # `Environment="NAME=value"` form, and the comparison below would then
        # be between two empty strings and pass. Both have to be real paths
        # before the agreement means anything.
        session = values["ISTOTA_BAILEYS_SESSION_DIR"]
        media = values["ISTOTA_BAILEYS_MEDIA_DIR"]
        assert session.startswith("/") and media.startswith("/")

        assert derive_media_dir(values["ISTOTA_BAILEYS_SESSION_DIR"]) == (
            values["ISTOTA_BAILEYS_MEDIA_DIR"]
        )

    def test_the_media_directory_is_inside_the_write_path_this_unit_grants(
        self, tmp_path,
    ):
        """The staging directory's first reason, asserted against the unit.

        `ProtectSystem=strict` makes the whole filesystem read-only bar what
        `ReadWritePaths=` names, and this unit's write path is deliberately
        narrower than its siblings' because it runs a third-party dependency
        tree while holding a full WhatsApp account. So the staging directory
        had to land inside that path or the sidecar could not write a photo —
        and the alternative location, a sibling of the task control directory
        under `temp_dir`, is refused by this directive alone: widening it to
        reach `temp_dir` would hand the sidecar every user's task temp
        directory. **Not by `PrivateTmp=true`**, which is also on the unit and
        is easy to credit here by mistake: the role renders `temp_dir` as
        `{istota_home}/tmp`, and a private `/tmp` does not reach it.

        Read as a **verification rather than an assumption**: the unit is
        parsed for its own directive and the directory is asked of the product,
        so a later narrowing of either fails here rather than in production.
        """
        from istota.config import load_config
        from istota.transport.whatsapp.media import default_media_dir

        home = "/srv/app/istota"
        path = tmp_path / "config.toml"
        path.write_text(render_ansible_config(istota_home=home))
        config = load_config(path)
        rendered = self._unit(istota_home=home)

        granted = [
            line.split("=", 1)[1].strip()
            for line in rendered.splitlines()
            if line.startswith("ReadWritePaths=")
        ]
        assert granted, "the unit grants no write path at all"

        media_dir = default_media_dir(config)
        assert any(
            media_dir.is_relative_to(Path(root)) for root in granted
        ), f"{media_dir} is outside {granted}; the sidecar cannot stage a photo"
        # The control against a repair that widens the unit instead of keeping
        # the directory where it belongs: the write path must still not reach
        # the deployment root or the temp directory.
        assert granted == [f"{home}/data"]

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
        """Wants, and none of the three directives that propagate one.

        `Requires=` propagates a stop, so a scheduler restart would drop the
        WhatsApp link on every deploy. The sidecar retries a missing socket
        every two seconds, so the ordering is a courtesy and the dependency is
        not one.

        **`PartOf=` and `BindsTo=` are the two spellings this used to miss, and
        since ISSUE-504 they matter more than `Requires=` does.** `PartOf=`
        propagates a stop *and a restart*, which is precisely what the
        two-minute update cron does to the scheduler on any commit — so a later
        `PartOf=istota-scheduler.service` would take the sidecar down on every
        commit while leaving the old two assertions green. What these two lines
        mean is no longer only "the paired session survives a scheduler
        restart": it is that plus "the next scheduler re-adopts the window this
        sidecar is still offering codes into", a stronger claim resting on the
        same two lines.
        """
        rendered = self._unit()
        # Directives, not mentions: a `#` comment in the template names the two
        # spellings in order to say why they must not be there, and a substring
        # test cannot tell that apart from the directive itself.
        directives = [
            line.strip() for line in rendered.splitlines()
            if not line.lstrip().startswith("#")
        ]

        assert "Wants=istota-scheduler.service" in directives
        for propagates_a_stop in ("Requires=", "PartOf=", "BindsTo="):
            assert not [
                line for line in directives
                if line.startswith(propagates_a_stop)
            ]

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
        install = self._named(
            "Install Node.js for the web build, developer skill and WhatsApp sidecar"
        )

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

    def test_the_checkout_task_restarts_it_on_a_source_sync(self):
        """ISSUE-497's other half: a full play left it on stale code.

        The sidecar runs `index.js` out of the checkout, so it holds its own
        copy exactly as the Python services hold theirs — and its handler is
        notified only by the dependency install, gated on the lockfile
        checksum, and by the unit template. Both are deployment changes rather
        than source ones, so a commit touching `index.js` alone fired neither
        and the unit went on running what it loaded at start.

        `Restart services (update-only mode)` covers that one mode with an
        explicit loop, and a full play had nothing. Nor could the cron repair
        it: the play writes the deployed marker at the new tip, so every later
        tick diffs from a point already past the change and sees nothing. The
        unconditional restart this issue removed was what healed it by
        accident, which is why the two halves had to be fixed together.

        The rule is the checkout task's own, written for ISSUE-294: every
        long-running service importing from the checkout is listed there.
        """
        task = self._named("Clone or update istota repository (branch checkout)")

        assert "restart istota-whatsapp-baileys" in task["notify"]

    def test_the_baileys_handler_carries_its_own_guard(self):
        """Which is why the notify site above needs no condition on it.

        A Cloud-adapter or WhatsApp-off host runs no such unit, and the
        checkout task fires on every source sync.
        """
        handlers = yaml.safe_load(
            (ANSIBLE / "handlers" / "main.yml").read_text()
        )
        handler = next(
            h for h in handlers if h.get("name") == "restart istota-whatsapp-baileys"
        )

        assert "istota_whatsapp_baileys_unit_wanted" in str(handler.get("when"))

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


class TestThePairingKeysReachBothGenerators:
    """The four `[whatsapp.baileys]` pairing keys, through every place a
    `[whatsapp.baileys]` key has to land.

    Seven places, not two, and two of them fail in ways unrelated to this
    surface: `config.toml.j2` references the Ansible variable unguarded, so a
    missing `defaults/main.yml` entry fails the play at the template task
    having already replaced the running deployment's config, and
    `tests/test_config_field_coverage.py` compares the dataclass against
    `config/config.example.toml`, so an undocumented key turns the **default
    suite** red. Both are asserted here rather than left to be discovered.
    """

    PAIRING_KEYS = (
        "pairing_enabled",
        "pairing_window_seconds",
        "pairing_relay_path",
        "restart_interval_seconds",
    )

    PAIRING_ENV = {
        "ISTOTA_WHATSAPP_BAILEYS_PAIRING_ENABLED",
        "ISTOTA_WHATSAPP_BAILEYS_PAIRING_WINDOW_SECONDS",
        "ISTOTA_WHATSAPP_BAILEYS_PAIRING_RELAY_PATH",
        "ISTOTA_WHATSAPP_BAILEYS_RESTART_INTERVAL_SECONDS",
    }

    def test_the_dataclass_declares_all_four(self):
        from istota.config import WhatsAppBaileysConfig

        declared = {f.name for f in dataclasses.fields(WhatsAppBaileysConfig)}
        for name in self.PAIRING_KEYS:
            assert name in declared, name

    def test_the_example_config_documents_all_four(self):
        """`tests/test_config_field_coverage.py` is the general guard; this is
        the one that names the four and so says which key is missing."""
        text = (REPO / "config" / "config.example.toml").read_text()
        start = text.index("[whatsapp.baileys]")
        block = text[start:]
        end = block.find("\n[", 1)
        block = block if end < 0 else block[:end]
        for name in self.PAIRING_KEYS:
            assert re.search(rf"^{name}\s*=", block, re.M), name

    def test_the_docker_render_writes_all_four(self, tmp_path):
        path = render_docker_config(
            tmp_path, **REQUIRED, **WHATSAPP_VALUES, **WHATSAPP_ADAPTER_VALUES,
        )
        baileys = tomllib.loads(path.read_text())["whatsapp"]["baileys"]
        for name in self.PAIRING_KEYS:
            assert name in baileys, name
        # The shape's own defaults: pairing on, the shipped window, the relay
        # resolved rather than pointed somewhere, and no restart interval
        # declared because Docker's backoff from 100ms is not one.
        assert baileys["pairing_enabled"] is True
        assert baileys["pairing_window_seconds"] == 300
        assert baileys["pairing_relay_path"] == ""
        assert baileys["restart_interval_seconds"] == 0

    def test_the_docker_render_honours_each_value(self, tmp_path):
        path = render_docker_config(
            tmp_path,
            **REQUIRED,
            **WHATSAPP_VALUES,
            **WHATSAPP_ADAPTER_VALUES,
            ISTOTA_WHATSAPP_BAILEYS_PAIRING_ENABLED="false",
            ISTOTA_WHATSAPP_BAILEYS_PAIRING_WINDOW_SECONDS="90",
            ISTOTA_WHATSAPP_BAILEYS_PAIRING_RELAY_PATH="/srv/relay/qr.json",
            ISTOTA_WHATSAPP_BAILEYS_RESTART_INTERVAL_SECONDS="7",
        )
        baileys = _loaded(path).whatsapp.baileys

        assert baileys.pairing_enabled is False
        assert baileys.pairing_window_seconds == 90
        assert baileys.pairing_relay_path == "/srv/relay/qr.json"
        assert baileys.restart_interval_seconds == 7

    def test_a_quote_in_the_relay_path_still_renders_loadable_toml(self, tmp_path):
        """The heredoc runs under `set -u` and interpolates, so the path is
        escaped where its neighbour is — an unescaped `"` leaves a config.toml
        that does not parse, which on this shape is a container that will not
        boot."""
        path = render_docker_config(
            tmp_path,
            **REQUIRED,
            **WHATSAPP_VALUES,
            **WHATSAPP_ADAPTER_VALUES,
            ISTOTA_WHATSAPP_BAILEYS_PAIRING_RELAY_PATH='/srv/a"b/qr.json',
        )
        loaded = _loaded(path)

        assert loaded.whatsapp.baileys.pairing_relay_path == '/srv/a"b/qr.json'

    def test_compose_passes_every_pairing_variable_the_render_reads(self):
        """The testbed rules' two-file rule: a variable the generator reads
        must also be passed through, and the two files are not automatically
        in sync."""
        compose = yaml.safe_load(COMPOSE.read_text())
        environment = compose["services"]["istota"]["environment"]

        for name in self.PAIRING_ENV:
            assert name in environment, f"compose withholds {name}"

    def test_the_env_example_documents_every_pairing_variable(self):
        text = ENV_EXAMPLE.read_text()

        for name in self.PAIRING_ENV:
            assert re.search(rf"^{name}=", text, re.M), (
                f".env.example does not document {name}"
            )

    def test_the_ansible_template_renders_all_four(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(render_ansible_config(
            istota_whatsapp_enabled=True,
            istota_whatsapp_provider="baileys",
        ))
        baileys = _loaded(path).whatsapp.baileys

        assert baileys.pairing_enabled is True
        assert baileys.pairing_window_seconds == 300
        assert baileys.pairing_relay_path == ""
        # The role declares an interval because systemd genuinely has one.
        assert baileys.restart_interval_seconds == 30

    def test_the_ansible_template_honours_each_value(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(render_ansible_config(
            istota_whatsapp_enabled=True,
            istota_whatsapp_provider="baileys",
            istota_whatsapp_baileys_pairing_enabled=False,
            istota_whatsapp_baileys_pairing_window_seconds=120,
            istota_whatsapp_baileys_pairing_relay_path="/var/lib/istota/qr.json",
            istota_whatsapp_baileys_restart_sec=45,
        ))
        baileys = _loaded(path).whatsapp.baileys

        assert baileys.pairing_enabled is False
        assert baileys.pairing_window_seconds == 120
        assert baileys.pairing_relay_path == "/var/lib/istota/qr.json"
        assert baileys.restart_interval_seconds == 45

    def test_every_variable_the_template_names_has_a_default(self):
        """`config.toml.j2` references these unguarded, and the play renders
        under `StrictUndefined`, so a missing default is a failed converge
        rather than a missing key — and it fails at the template task, having
        already replaced the running deployment's config."""
        defaults = yaml.safe_load(DEFAULTS_FILE.read_text())

        for name in (
            "istota_whatsapp_baileys_pairing_enabled",
            "istota_whatsapp_baileys_pairing_window_seconds",
            "istota_whatsapp_baileys_pairing_relay_path",
            "istota_whatsapp_baileys_restart_sec",
        ):
            assert name in defaults, f"defaults/main.yml has no {name}"


class TestTheRestartIntervalCannotDrift:
    """`RestartSec=` and `restart_interval_seconds` come from one variable.

    That is the whole reason the config key is trustworthy: the daemon sizes
    how long a re-pair waits for the sidecar to come back from a number it
    cannot observe, and the only way the two cannot disagree is that one
    Ansible variable renders both files.

    **Asserted over a non-default value**, because a test that checks only the
    default passes while the two are independent literals — which is exactly
    the state this stage found them in.
    """

    UNIT = ANSIBLE / "templates" / "istota-whatsapp-baileys.service.j2"

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

    @pytest.mark.parametrize("seconds", [30, 45, 5])
    def test_both_artifacts_agree_for_any_value(self, tmp_path, seconds):
        unit = self._unit(istota_whatsapp_baileys_restart_sec=seconds)
        config_path = tmp_path / f"config-{seconds}.toml"
        config_path.write_text(render_ansible_config(
            istota_whatsapp_enabled=True,
            istota_whatsapp_provider="baileys",
            istota_whatsapp_baileys_restart_sec=seconds,
        ))

        match = re.search(r"^RestartSec=(\d+)$", unit, re.M)
        assert match, "the unit renders no RestartSec"
        declared = _loaded(config_path).whatsapp.baileys.restart_interval_seconds

        assert int(match.group(1)) == declared == seconds

    @pytest.mark.parametrize("value", ["30s", "1min", "100ms"])
    def test_a_systemd_time_span_does_not_render_unloadable_toml(
        self, tmp_path, value
    ):
        """The cost of the two consumers sharing one variable.

        `RestartSec=` accepts a systemd time span and `defaults/main.yml`
        invites "set it to the unit's own RestartSec" — and the same value
        written bare into `restart_interval_seconds` is TOML no istota process
        can parse, so `load_config` would fail in the scheduler, the web app,
        the webhook receiver and every host-side skill CLI spawn alike. The
        template's `| int` renders 0 there, which is the "undeclared" value
        that gates nothing; the role's own assert is what makes such an
        inventory fail the converge rather than the daemon.

        Negative control: dropping `| int` from the template turns this red on
        `tomllib` refusing the render.
        """
        path = tmp_path / f"config-{value}.toml"
        path.write_text(render_ansible_config(
            istota_whatsapp_enabled=True,
            istota_whatsapp_provider="baileys",
            istota_whatsapp_baileys_restart_sec=value,
        ))

        baileys = _loaded(path).whatsapp.baileys

        assert baileys.restart_interval_seconds == 0

    def test_the_role_refuses_a_non_integer_interval(self):
        """The half the filter cannot do: with `| int` the config still loads,
        so the two artifacts silently disagree — the unit waits 1min and the
        daemon believes it waits none. The assert is what makes that loud."""
        tasks = yaml.safe_load(TASKS_FILE.read_text())
        task = next(
            t for t in tasks
            if t.get("name") == "Assert WhatsApp settings the config loader accepts"
        )
        clauses = " ".join(str(c) for c in task["assert"]["that"])

        assert "istota_whatsapp_baileys_restart_sec" in clauses
        assert "istota_whatsapp_baileys_pairing_window_seconds" in clauses

        env = _ansible_ish_environment()
        for value, ok in (("30", True), ("30s", False), ("1min", False)):
            rendered = env.from_string(
                "{{ (v == (v | int)) | lower }}"
            ).render(v=int(value) if value.isdigit() else value)
            assert (rendered == "true") is ok, (value, rendered)

    def test_the_unit_still_carries_the_reason_for_thirty(self):
        """The comment explaining why thirty rather than five stays put as the
        reason for the default, which is what a templated literal loses if
        somebody moves the number without it."""
        source = self.UNIT.read_text()

        assert "{{ istota_whatsapp_baileys_restart_sec }}" in source
        assert "RestartSec=30" not in source
        assert "twelve entries a minute" in source

    def test_the_daemons_timeout_scales_with_the_declared_value(self):
        """What the number is actually for. It gates nothing — it supplies the
        scaling term of the wait for a sidecar to reappear — so the assertion
        is that the two are related, not that either is a threshold."""
        from istota.transport.whatsapp.baileys_bridge import sidecar_return_timeout

        assert sidecar_return_timeout(30) == sidecar_return_timeout(0) + 30
        assert sidecar_return_timeout(0) == sidecar_return_timeout(-1)


def _loaded(path: Path):
    """`load_config` on a rendered file, with the import at function scope.

    Every other caller in this file imports it inside its own test; one helper
    says the same thing once.
    """
    from istota.config import load_config

    return load_config(path)


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


class TestTheCronDoesNotBounceALiveSession:
    """ISSUE-497: an unrelated deploy must not reconnect a paired session.

    The sidecar holds a WhatsApp Web session with a third party, which is a
    kind of state none of the other units restarted here have: the Python
    services hold nothing but their own code, so bouncing one costs a few
    seconds of local downtime, while bouncing this one drops and re-establishes
    a link WhatsApp is watching for exactly that pattern. The credential
    survives — it is on disk, not in the process — so what a restart costs is
    invisible from this side, which is why the original was written
    unconditional.

    These execute the rendered script rather than scanning it, on the seam
    `test_ansible_web_build` established, and for the reason that file's own
    docstring gives: every version of this template contains the string
    `systemctl restart "${NAMESPACE}-whatsapp-baileys"` — including the one
    that runs it on every tick. Only a run can tell the two apart.
    """

    @staticmethod
    def _rig(tmp_path: Path, **kwargs):
        from tests.test_ansible_web_build import Rig

        rig = Rig(tmp_path, baileys=True, **kwargs)
        # The first run only seeds the deployed marker and exits at the
        # up-to-date check, so it restarts nothing. Clearing after it is what
        # makes the assertions below about the second run alone.
        rig.run()
        rig.clear_calls()
        return rig

    def test_it_refuses_to_start_the_unit_without_an_interpreter(self, tmp_path):
        """ISSUE-494, the half the play cannot reach.

        The play asserts on the interpreter before it deploys the unit, but
        nothing in the play runs this script — so a host left with the unit
        enabled and no node (a pre-fix update-only converge, or node removed
        out of band) had this arm start an interpreter-less unit every two
        minutes indefinitely, each start logged as success.

        Driven rather than scanned, for this class's own reason: the template
        contains the start line in every version of itself, including the one
        that runs it unconditionally.
        """
        rig = self._rig(tmp_path, node_present=False)
        rig.commit({"docker/whatsapp-baileys/index.js": "// changed\n"})
        result = rig.run()

        assert result.returncode == 0, result.stderr
        calls = rig.calls()
        assert not any("whatsapp-baileys" in c and "start" in c for c in calls), (
            f"started the sidecar with no interpreter: {calls}"
        )
        assert not any("whatsapp-baileys" in c and "restart" in c for c in calls)
        # The rest of the run has to carry on: this is one unit, and holding
        # the migrations and every other restart back over it would turn a
        # missing sidecar dependency into a stalled deployment.
        assert any("systemctl restart istota-scheduler" == c for c in calls)
        assert "ERROR" in rig.log_text() and "run a full play" in rig.log_text()

    def test_a_deploy_that_leaves_the_sidecar_alone_does_not_restart_it(self, tmp_path):
        """The reported defect: a docs commit reconnected a live session."""
        rig = self._rig(tmp_path)
        rig.commit({"docs/whatever.md": "prose\n"})

        result = rig.run()

        assert result.returncode == 0, result.stderr
        assert "Update complete" in rig.log_text()
        assert "systemctl restart istota-whatsapp-baileys" not in rig.calls()

    def test_it_is_still_started_so_a_stopped_sidecar_comes_back(self, tmp_path):
        """The property the unconditional restart was defending.

        The install arm above stops the unit before `npm ci`, and an operator
        or a crash can leave it down too. `start` is a no-op on a running unit
        and brings back a stopped one, which is what lets the restart be
        dropped without stranding anything.
        """
        rig = self._rig(tmp_path)
        rig.commit({"docs/whatever.md": "prose\n"})

        result = rig.run()

        assert result.returncode == 0, result.stderr
        assert "systemctl start istota-whatsapp-baileys" in rig.calls()

    def test_a_commit_under_the_sidecar_restarts_it(self, tmp_path):
        """The control. Without this the test above passes on a script that
        never restarts the sidecar at all, which is the opposite defect."""
        rig = self._rig(tmp_path)
        rig.commit({"docker/whatsapp-baileys/index.js": "// changed\n"})

        result = rig.run()

        assert result.returncode == 0, result.stderr
        assert "systemctl restart istota-whatsapp-baileys" in rig.calls()

    def test_a_lockfile_bump_still_stops_installs_and_restarts(self, tmp_path):
        """The install arm is unchanged, and the restart is what revives it."""
        rig = self._rig(tmp_path)
        rig.commit({"docker/whatsapp-baileys/package-lock.json": '{"v": 2}\n'})

        result = rig.run()

        assert result.returncode == 0, result.stderr
        calls = rig.calls()
        assert "systemctl stop istota-whatsapp-baileys" in calls
        assert any(call.startswith("npm ci --omit=dev") for call in calls)
        assert "systemctl restart istota-whatsapp-baileys" in calls
        # Ordering matters, and the claim is about the *install* rather than
        # the stop: a restart landing before `npm ci` re-execs against the tree
        # that command is about to delete. Comparing against the stop instead
        # asserts almost nothing, it being a precondition of the install and 60
        # lines above the restart in a script with no branch between them.
        install = next(
            i for i, call in enumerate(calls) if call.startswith("npm ci --omit=dev")
        )
        assert install < calls.index("systemctl restart istota-whatsapp-baileys")

    def test_an_unreadable_deployed_sha_restarts_rather_than_assuming(self, tmp_path):
        """`BAILEYS_CHANGED` is the literal `unknown-revision` when the marker
        names a commit this checkout does not have, and the gate has to read
        that as "something moved". Assuming the other way would skip the
        restart on exactly the runs that know least."""
        from tests.test_ansible_web_build import Rig

        rig = Rig(tmp_path, baileys=True)
        rig.run()
        (rig.state / "last-deployed-sha").write_text("0" * 40 + "\n")
        rig.clear_calls()
        rig.commit({"docs/whatever.md": "prose\n"})

        rig.run()

        assert "systemctl restart istota-whatsapp-baileys" in rig.calls()


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
