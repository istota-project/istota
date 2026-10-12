"""The lean stack, booted from what the first-run web setup wrote.

Every other scenario in this tier boots from a `config.toml` the testbed
assembles from layers (`testbed.stack.LEAN_BASE_CONFIG` and the rest). This
one boots from the web flow's own output instead: the setup server runs in a
one-shot `compose run` of the image, as `istota-stack setup --web --local`
runs it, a browser's requests unlock it and finish it, and the stack then
starts from the config directory, the secret files, the `host.env` and the
database that run left behind. Nothing the testbed would normally write is
written, so a stack that boots here booted from the web setup's files.

The native brain is pointed at the scripted endpoint through the AI service
screen's own field (`native_base_url`), the only departure from what a user
types, so a task can be answered without a network. The witness is three
things: the daemon reaches the scheduler, a task is answered by the endpoint
the setup named, and the first admin signs in through nginx and the web app
with the password set at Finish.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from html import unescape
from pathlib import Path

import httpx
import pytest

from testbed import profiles
from testbed import stack as stack_support
from testbed.services import build as build_service
from testbed.services.model_endpoint import ENDPOINT_CREDENTIAL, SCRIPTED_MODEL

from ..conftest import (
    COMPOSE_FILE,
    LEAN_READY_TIMEOUT,
    PROJECT_PREFIX,
    _require_no_xdist,
    lean_image_tag,
    require_docker,
)

pytestmark = pytest.mark.smoke

CODE = "7KQ2M-X9D4F-H3T8W-QP6ZA"
USER_ID = "testuser"
EMAIL = "testuser@example.test"
PASSWORD = "a long enough passphrase for the smoke tier"
SERVER_TIMEOUT = 180
BUILD_TIMEOUT = 3600

#: The setup run's overlay: the checkout's image, and the shipped `rslave`
#: workspace bind replaced as `testbed.yml` replaces it. Not `testbed.yml`
#: itself, whose config bind is read-only: setup is the one run that writes it.
SETUP_OVERLAY = (
    "services:\n"
    "  daemon:\n"
    "    build: !reset null\n"
    "    image: ${ISTOTA_TEST_IMAGE}\n"
    "    volumes:\n"
    "      - testbed_mount:/mnt/shared\n"
    "volumes:\n"
    "  testbed_mount:\n"
)


@dataclass
class WebBootedStack:
    stack: stack_support.Stack
    finish: httpx.Response
    setup_exit: int | None
    setup_log: str
    config_text: str


def _session_cookie(response: httpx.Response) -> str:
    for header in response.headers.get_list("set-cookie"):
        match = re.match(r"istota_setup=([^;]+)", header)
        if match:
            return match.group(1)
    return ""


def _drive_setup(port: int, process: subprocess.Popen, log: Path, draft: dict) -> httpx.Response:
    """Unlock and finish the setup server the way the page does."""
    origin = f"http://127.0.0.1:{port}"
    with httpx.Client(base_url=origin, timeout=30, trust_env=False) as client:
        deadline = time.monotonic() + SERVER_TIMEOUT
        while True:
            if process.poll() is not None:
                pytest.fail(f"the setup server exited {process.returncode}:\n{log.read_text()[-4000:]}",
                            pytrace=False)
            try:
                client.get("/istota/api/setup/state")
                break
            except httpx.HTTPError:
                if time.monotonic() > deadline:
                    pytest.fail(f"the setup server did not answer:\n{log.read_text()[-4000:]}", pytrace=False)
                time.sleep(1)
        unlocked = client.post("/istota/api/setup/unlock", json={"code": CODE}, headers={"origin": origin})
        assert unlocked.status_code == 200, unlocked.text
        headers = {"origin": origin, "cookie": f"istota_setup={_session_cookie(unlocked)}"}
        put = client.put("/istota/api/setup/draft", json=draft, headers=headers)
        assert put.status_code == 200, put.text
        review = client.post("/istota/api/setup/validate", json={"step": "review"}, headers=headers)
        assert review.json() == {"ok": True, "fields": []}, review.text
        return client.post("/istota/api/setup/finish", headers=headers)


@pytest.fixture(scope="module")
def web_booted(pytestconfig, tmp_path_factory):
    _require_no_xdist(pytestconfig)
    require_docker()
    scratch = tmp_path_factory.mktemp("websetup-boot")
    vm = scratch / "vm"
    config_dir = vm / "config"
    secrets_dir = vm / "secrets"
    config_dir.mkdir(parents=True)
    secrets_dir.mkdir()
    # Compose refuses a service whose secret file is missing; setup fills them.
    for name in stack_support.SECRET_NAMES:
        (secrets_dir / name).write_text("")
    (vm / "setup-code").write_text(CODE + "\n")
    (vm / "setup-code").chmod(0o444)

    model = build_service("model", scratch=scratch, host=stack_support.PUBLIC_BIND)
    environment = stack_support.lean_env(
        {}, image=lean_image_tag(), config_dir=config_dir, secrets_dir=secrets_dir,
    )
    # The shipped file's own config bind, for the setup run, and the host.env
    # that run writes, which the daemon and nginx read as the VM's.
    environment["ISTOTA_CONFIG_DIR"] = str(config_dir)
    environment["ISTOTA_HOST_ENV"] = str(vm / "host.env")
    env_file = stack_support.write_env_file(scratch / "compose.env", environment)
    project = f"{PROJECT_PREFIX}websetup-{uuid.uuid4().hex[:8]}"
    setup_overlay = scratch / "setup.yml"
    setup_overlay.write_text(SETUP_OVERLAY)
    setup_args = stack_support.compose_args(
        COMPOSE_FILE, project=project, env_file=env_file, overlays=[setup_overlay],
    )
    lean_args = stack_support.compose_args(
        COMPOSE_FILE, project=project, env_file=env_file, overlays=[stack_support.TESTBED_OVERLAY],
    )

    try:
        built = subprocess.run(
            [*lean_args, "build", "daemon"], capture_output=True, text=True, timeout=BUILD_TIMEOUT,
        )
        assert built.returncode == 0, (built.stdout + built.stderr)[-4000:]

        port = stack_support.reserve_port()
        log = scratch / "setup.log"
        with open(log, "w") as out:
            process = subprocess.Popen(
                [*setup_args, "run", "--rm", "--no-deps", "-T", "-p", f"127.0.0.1:{port}:8080",
                 "-v", f"{vm}:/vm", "--entrypoint", "istota-drop", "daemon",
                 "istota", "setup", "--web", "--local", "--vm-dir", "/vm"],
                stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env={**os.environ, "ISTOTA_CONFIG_READ_ONLY": "false"},
            )
        try:
            finish = _drive_setup(port, process, log, {
                "bot_name": "Istota", "display_name": "Test User", "user_id": USER_ID,
                "user_email": EMAIL, "timezone": "UTC",
                "brain_kind": "native", "native_base_url": model.container_url,
                "native_model": SCRIPTED_MODEL, "native_api_key": ENDPOINT_CREDENTIAL,
                "ingress": "local", "hostname": "localhost:8080", "profiles": [],
                "password": PASSWORD,
            })
            setup_exit = process.wait(timeout=60)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30)
        assert finish.status_code == 200, finish.text
        assert setup_exit == 0, log.read_text()[-4000:]

        try:
            stack_support.up(lean_args, build=False)
        except stack_support.ComposeError as exc:
            raise stack_support.ComposeError(
                f"{exc}\n--- daemon logs ---\n{stack_support.logs(lean_args, 'daemon', tail=60)}"
            ) from exc
        stack_support.wait_all_ready(lean_args, ("daemon",), timeout=LEAN_READY_TIMEOUT)
        stack = stack_support.Stack(
            profile=profiles.Profile(name="web-setup", web=True),
            args=lean_args, services={"model": model}, env=environment, config_dir=config_dir,
        )
        stack.wait_healthy(timeout=LEAN_READY_TIMEOUT)
        yield WebBootedStack(
            stack=stack, finish=finish, setup_exit=setup_exit, setup_log=log.read_text(),
            config_text=(config_dir / "config.toml").read_text(),
        )
    finally:
        stack_support.down(lean_args, volumes=True)
        model.close()


class TestTheStackBootsFromTheWebSetup:
    def test_the_daemon_reads_the_config_the_setup_wrote(self, web_booted):
        stack = web_booted.stack
        seen = stack.exec(["cat", stack_support.CONTAINER_CONFIG])
        assert seen.returncode == 0, seen.stderr
        assert seen.stdout == web_booted.config_text
        assert f'base_url = "{stack.endpoint.container_url}"' in seen.stdout

    def test_a_task_is_answered_by_the_model_the_setup_named(self, web_booted):
        stack = web_booted.stack
        stack.script([{"text": "answered after a web setup"}])
        task_id = stack.submit("a question after the web setup", user_id=USER_ID)
        task = stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=120)
        assert task["status"] == "completed", (
            f"task {task_id} ended {task['status']!r}: {task.get('error')!r}\n{stack.logs()}"
        )
        assert "answered after a web setup" in (task.get("result") or "")

    def test_the_first_admin_signs_in_with_the_setup_password(self, web_booted):
        port = web_booted.stack.published_port("nginx", 80)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30, trust_env=False) as browser:
            page = browser.get("/istota/login", params={"email": EMAIL})
            assert page.status_code == 200, page.text[-2000:]
            form = re.search(r'<form\b[^>]*action="/istota/login/email"[^>]*>(.*?)</form>', page.text, re.S)
            assert form is not None, "no password form on the login page"
            fields = {
                name: unescape(value)
                for name, value in re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form[1])
            }
            assert fields.get("email") == EMAIL, "the login page did not take ?email= from the setup"
            signed_in = browser.post(
                "/istota/login/email", data={**fields, "email": EMAIL, "password": PASSWORD},
            )
            assert signed_in.status_code == 302, signed_in.text[-2000:]
            me = browser.get("/istota/api/me")
            assert me.status_code == 200, me.text
            assert me.json()["username"] == USER_ID
