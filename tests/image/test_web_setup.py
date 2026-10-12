"""The first-run setup server in the built image, driven over HTTPS.

`istota setup --web --vm-dir /vm` runs as `istota-stack setup --web` runs it:
a one-shot `compose run` of the shipped `daemon` service, through
`istota-drop`, with the config directory opened and a staging directory at
`/vm` holding the setup code, a certificate and its key. The setup code and
the certificate are minted here rather than by `istota-stack` (that half is
`tests/test_istota_stack.py`'s and the vm tier's). What this witnesses is the
image's half: that the server starts with no config, serves the page from the
bundle the image built, refuses a wrong code, hands one session to the right
one, writes everything at Finish and exits 0, and that the first admin's
password then signs in against the database it wrote.

The negative control is `scripts/test-image-negative-control.sh web-setup`:
an image whose setup server takes any code (`docker/test/Dockerfile.setup-
any-code`). The wrong-code witness must go red there, and the Finish witness
must stay green, which is what says the red came from the code check rather
than from a server that never came up.
"""

from __future__ import annotations

import os
import re
import secrets
import ssl
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from testbed.certs import generate_self_signed
from testbed.stack import reserve_port

from .shipped_stack import shipped_compose

pytestmark = pytest.mark.image

CODE = "7KQ2M-X9D4F-H3T8W-QP6ZA"
WRONG_CODE = "00000-00000-00000-00000"
PASSWORD = "a long enough passphrase for the image tier"
NATIVE_KEY = f"image-tier-native-{secrets.token_hex(6)}"
USER_ID = "ada"
EMAIL = "ada@example.test"

#: A draft that finishes: local storage, the native brain, no connections and
#: no browser profile, so the written `.env` names nothing to build.
DRAFT = {
    "bot_name": "Istota", "display_name": "Ada", "user_id": USER_ID,
    "user_email": EMAIL, "timezone": "UTC",
    "brain_kind": "native", "native_model": "scripted-test-model", "native_api_key": NATIVE_KEY,
    "ingress": "local", "hostname": "localhost:8080", "profiles": [],
    "password": PASSWORD,
}

SERVER_TIMEOUT = 180
COOKIE = "istota_setup"


@dataclass
class WebSetupRun:
    port: int
    state_before: httpx.Response
    page: httpx.Response
    root: httpx.Response
    draft_without_session: httpx.Response
    wrong_code: httpx.Response
    right_code: httpx.Response
    second_unlock: httpx.Response
    finish: httpx.Response | None
    exit_code: int | None
    server_log: str
    vm: Path
    config_dir: Path
    secrets_dir: Path
    sign_in: subprocess.CompletedProcess | None = None
    second_run: subprocess.CompletedProcess | None = None
    files: dict[str, str] = field(default_factory=dict)


def _session_cookie(response: httpx.Response) -> str:
    """The session cookie's value from a `Set-Cookie`, or ''.

    Read by hand rather than through httpx's jar, so the attributes the server
    set are what the assertions see.
    """
    for header in response.headers.get_list("set-cookie"):
        match = re.match(rf"{COOKIE}=([^;]+)", header)
        if match:
            return match.group(1)
    return ""


def _wait_for_server(client: httpx.Client, process: subprocess.Popen, log: Path) -> httpx.Response:
    deadline = time.monotonic() + SERVER_TIMEOUT
    last = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(
                f"the setup server exited {process.returncode} before it answered:\n"
                f"{log.read_text()[-4000:]}",
                pytrace=False,
            )
        try:
            return client.get("/istota/api/setup/state")
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        time.sleep(1)
    pytest.fail(f"the setup server did not answer within {SERVER_TIMEOUT}s ({last}):\n"
                f"{log.read_text()[-4000:]}", pytrace=False)


@pytest.fixture(scope="module")
def web_setup(istota_image, tmp_path_factory) -> WebSetupRun:
    work = tmp_path_factory.mktemp("websetup")
    with shipped_compose(istota_image.tag, work, project_prefix="istota-websetup") as shipped:
        vm = shipped.vm
        (vm / "setup-code").write_text(CODE + "\n")
        cert, key = generate_self_signed(work / "tls", sans=("localhost", "127.0.0.1"))
        # uid 10001 reads these through the bind; the key is this run's own.
        (vm / "cert.pem").write_bytes(cert.read_bytes())
        (vm / "key.pem").write_bytes(key.read_bytes())
        for name in ("setup-code", "cert.pem", "key.pem"):
            (vm / name).chmod(0o444)

        port = reserve_port()
        log = work / "server.log"
        argv = shipped.compose_argv([
            "run", "--rm", "--no-deps", "-T", "-p", f"127.0.0.1:{port}:8443",
            "-v", f"{vm}:/vm", "--entrypoint", "istota-drop", "daemon",
            "istota", "setup", "--web", "--vm-dir", "/vm",
        ])
        with open(log, "w") as out:
            process = subprocess.Popen(
                argv, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env={**os.environ, "ISTOTA_CONFIG_READ_ONLY": "false"},
            )
        try:
            context = ssl.create_default_context(cafile=str(cert))
            origin = f"https://127.0.0.1:{port}"
            headers = {"origin": origin}
            with httpx.Client(base_url=origin, verify=context, timeout=30, trust_env=False) as client:
                state_before = _wait_for_server(client, process, log)
                # The bundle sets `trailingSlash = 'always'`, so the printed
                # address redirects once; a browser keeps the fragment across it.
                page = client.get("/istota/setup", follow_redirects=True)
                root = client.get("/", follow_redirects=False)
                draft_without_session = client.get("/istota/api/setup/draft")

                session = ""
                wrong = client.post("/istota/api/setup/unlock", json={"code": WRONG_CODE}, headers=headers)
                session = _session_cookie(wrong) or session
                right = client.post("/istota/api/setup/unlock", json={"code": CODE}, headers=headers)
                session = _session_cookie(right) or session
                again = client.post("/istota/api/setup/unlock", json={"code": CODE}, headers=headers)

                finish = None
                if session:
                    with_session = {**headers, "cookie": f"{COOKIE}={session}"}
                    put = client.put("/istota/api/setup/draft", json=DRAFT, headers=with_session)
                    assert put.status_code == 200, put.text
                    finish = client.post("/istota/api/setup/finish", headers=with_session)
            try:
                exit_code = process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                exit_code = None
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30)

        run = WebSetupRun(
            port=port, state_before=state_before, page=page, root=root,
            draft_without_session=draft_without_session, wrong_code=wrong, right_code=right,
            second_unlock=again, finish=finish, exit_code=exit_code,
            server_log=log.read_text(), vm=vm, config_dir=shipped.config_dir,
            secrets_dir=shipped.secrets_dir,
        )
        for name in ("config/config.toml", "config/admins", ".env", "host.env",
                     "secrets/istota_brain_native_api_key", "setup-done"):
            path = vm / name
            run.files[name] = path.read_text() if path.is_file() else "<missing>"

        if exit_code == 0:
            # The identity row lives on the stack's state volume, which only a
            # container of the same project can read.
            run.sign_in = shipped.compose([
                "run", "--rm", "--no-deps", "-T", "--entrypoint", "istota-drop", "daemon",
                "python3", "-c",
                "import sys\n"
                "from istota.config import Config\n"
                "from istota.webui import auth\n"
                "status, identity = auth.authenticate('/data/db/istota.db', "
                "auth.policy_from_config(Config()), sys.argv[1], sys.argv[2], ip='127.0.0.1')\n"
                "print(status, identity.user_id if identity else '')\n",
                EMAIL, PASSWORD,
            ], timeout=120)
            run.second_run = shipped.compose([
                "run", "--rm", "--no-deps", "-T", "-v", f"{vm}:/vm",
                "--entrypoint", "istota-drop", "daemon",
                "istota", "setup", "--web", "--vm-dir", "/vm",
            ], timeout=120, extra_env={"ISTOTA_CONFIG_READ_ONLY": "false"})
        return run


class TestTheServerStartsWithNoConfig:
    def test_it_answers_locked(self, web_setup):
        assert web_setup.state_before.status_code == 200, web_setup.server_log[-2000:]
        assert web_setup.state_before.json()["phase"] == "locked"

    def test_it_serves_the_setup_page_from_the_images_bundle(self, web_setup):
        assert web_setup.page.status_code == 200
        assert web_setup.page.url.path == "/istota/setup/"
        assert "text/html" in web_setup.page.headers.get("content-type", "")
        assert "kit.start(" in web_setup.page.text

    def test_the_root_goes_to_the_setup_page(self, web_setup):
        assert web_setup.root.status_code == 302
        assert web_setup.root.headers["location"] == "/istota/setup"

    def test_the_draft_needs_a_session(self, web_setup):
        assert web_setup.draft_without_session.status_code == 401


class TestTheSetupCode:
    def test_a_wrong_code_is_refused(self, web_setup):
        assert web_setup.wrong_code.status_code == 403, web_setup.wrong_code.text
        assert _session_cookie(web_setup.wrong_code) == ""

    def test_the_right_code_unlocks_with_a_strict_secure_cookie(self, web_setup):
        assert web_setup.right_code.status_code == 200, web_setup.right_code.text
        header = next(
            h for h in web_setup.right_code.headers.get_list("set-cookie") if h.startswith(f"{COOKIE}=")
        ).lower()
        for attribute in ("httponly", "samesite=strict", "secure", "path=/istota"):
            assert attribute in header, header

    def test_the_code_is_spent_once_used(self, web_setup):
        assert web_setup.second_unlock.status_code == 403

    def test_the_code_reaches_no_log_line(self, web_setup):
        compact = CODE.replace("-", "")
        assert CODE not in web_setup.server_log
        assert compact not in web_setup.server_log


class TestFinish:
    def test_finish_answers_and_the_server_exits_0(self, web_setup):
        assert web_setup.finish is not None, "no session, so the flow never reached Finish"
        assert web_setup.finish.status_code == 200, web_setup.finish.text
        assert web_setup.finish.json() == {
            "login_url": "http://localhost:8080/istota/login", "email": EMAIL,
        }
        assert web_setup.exit_code == 0, web_setup.server_log[-4000:]

    def test_it_wrote_the_config_admins_and_marker(self, web_setup):
        config = web_setup.files["config/config.toml"]
        assert 'kind = "native"' in config, config
        assert 'model = "scripted-test-model"' in config, config
        assert NATIVE_KEY not in config
        admins = [
            line.strip() for line in web_setup.files["config/admins"].splitlines()
            if line.strip() and not line.startswith("#")
        ]
        assert admins == [USER_ID]
        assert web_setup.files["setup-done"] == ""

    def test_the_credentials_went_to_secret_files(self, web_setup):
        assert web_setup.files["secrets/istota_brain_native_api_key"] == NATIVE_KEY

    def test_it_staged_the_env_files(self, web_setup):
        assert "INGRESS=local" in web_setup.files["host.env"].splitlines(), web_setup.files["host.env"]
        assert "COMPOSE_PROFILES=" in web_setup.files[".env"], web_setup.files[".env"]

    def test_no_credential_reached_the_log(self, web_setup):
        assert NATIVE_KEY not in web_setup.server_log
        assert PASSWORD not in web_setup.server_log

    def test_the_first_admin_signs_in_with_the_password(self, web_setup):
        assert web_setup.sign_in is not None, "the server did not finish"
        assert web_setup.sign_in.returncode == 0, web_setup.sign_in.stderr[-2000:]
        assert web_setup.sign_in.stdout.split()[-2:] == ["ok", USER_ID], web_setup.sign_in.stdout

    def test_a_second_run_refuses_before_binding(self, web_setup):
        assert web_setup.second_run is not None, "the server did not finish"
        assert web_setup.second_run.returncode == 1, web_setup.second_run.stdout
        output = web_setup.second_run.stdout + web_setup.second_run.stderr
        assert "already set up" in output, output[-2000:]
