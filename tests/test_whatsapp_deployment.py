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
DOCKER_NGINX = REPO / "docker" / "nginx" / "istota.conf.template"
ENV_EXAMPLE = REPO / "docker" / ".env.example"

class TestTheComposeStack:
    def test_the_receiver_is_served_by_the_web_process(self):
        compose = yaml.safe_load(COMPOSE.read_text())
        web = compose["services"]["web"]

        # `ports` here would publish the raw receiver beside nginx, which is
        # what the signed webhook contract is behind.
        assert "ports" not in web
        assert "istota.webui.stack_app:app" in web["entrypoint"][-1]

    def test_the_env_example_documents_both_profiles(self):
        text = ENV_EXAMPLE.read_text()

        assert re.search(r"^#\s+whatsapp\s", text, re.M), (
            "the optional-services list at the top of .env.example does not "
            "name the whatsapp profile"
        )
        assert re.search(r"^#\s+whatsapp-baileys\s", text, re.M), (
            "the optional-services list at the top of .env.example does not "
            "name the whatsapp-baileys profile"
        )


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
        web = yaml.safe_load(COMPOSE.read_text())["services"]["web"]

        assert service["profiles"] == ["whatsapp-baileys"]
        # The receiver is in the web process, which no profile gates; its
        # WhatsApp handlers 404 unless the config enables Meta's adapter.
        assert "profiles" not in web
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
        resolves itself when `session_dir` is empty, which is what `istota
        setup` leaves it as, and the media directory has no override in any shape — so
        all three paths are derived from `db_path`, and a compose literal that
        drifts from that derivation is a sidecar writing where the daemon never
        reads, with no error on either side.
        """
        from istota.transport.whatsapp.baileys_bridge import (
            default_session_dir, default_socket_path,
        )
        from istota.transport.whatsapp.media import default_media_dir

        config = _container_config(tmp_path)
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
        from istota.transport.whatsapp.media import default_media_dir

        config = _container_config(tmp_path)
        service = self._service()

        istota = yaml.safe_load(COMPOSE.read_text())["services"]["daemon"]

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
        command = compose.split("\n  web:\n", 1)[1]
        line = next(
            ln for ln in command.splitlines()
            if "uvicorn istota.webui.stack_app:app" in ln
        )

        assert "--no-access-log" in line

    def test_the_combined_process_already_disabled_it(self):
        """The control for the two above: if `serve` ever turns it back on,
        every shape leaks and these assertions would be the only ones left
        describing a property nothing holds."""
        from istota import serve

        source = (Path(serve.__file__)).read_text()

        assert "access_log=False" in source


class TestTheContainerSetupNamesNoProvider:
    def test_the_default_render_writes_no_whatsapp_table(self):
        """The container's `istota setup` writes `provider` only for the
        whatsapp-baileys profile. Were it to write a value by default, an
        existing Cloud deployment would lose Meta's callback at the upgrade to
        the `baileys` default with nothing in any log saying so."""
        from istota.setup_wizard import ContainerAnswers, render_container_config

        container = render_container_config(
            ContainerAnswers(user_id="alice", session_secret="s" * 64),
            inline_credentials=False,
        )

        assert "whatsapp" not in tomllib.loads(container)


class TestThePairingKeys:
    """The four `[whatsapp.baileys]` pairing keys, declared and documented.

    `tests/test_config_field_coverage.py` compares the dataclass against
    `config/config.example.toml`, so an undocumented key turns the default
    suite red; this names the four, so it says which key is missing.
    """

    PAIRING_KEYS = (
        "pairing_enabled",
        "pairing_window_seconds",
        "pairing_relay_path",
        "restart_interval_seconds",
    )

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

    def test_the_daemons_timeout_scales_with_the_declared_value(self):
        """What the number is actually for. It gates nothing — it supplies the
        scaling term of the wait for a sidecar to reappear — so the assertion
        is that the two are related, not that either is a threshold."""
        from istota.transport.whatsapp.baileys_bridge import sidecar_return_timeout

        assert sidecar_return_timeout(30) == sidecar_return_timeout(0) + 30
        assert sidecar_return_timeout(0) == sidecar_return_timeout(-1)


def _container_config(tmp_path: Path):
    """The config the container's `istota setup` writes for a Baileys install, loaded."""
    from istota.config import load_config
    from istota.setup_wizard import ContainerAnswers, render_container_config

    path = tmp_path / "config.toml"
    path.write_text(render_container_config(
        ContainerAnswers(user_id="alice", profiles=("whatsapp-baileys",), session_secret="s" * 64),
        inline_credentials=False,
    ))
    return load_config(path)


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
