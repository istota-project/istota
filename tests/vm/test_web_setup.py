"""The first-run web setup on the VM: `istota-stack setup --web --local`.

What `host/lima/istota.yaml`'s comments tell a person to run, driven the way
they would drive it: `istota-stack setup --web --local` as root in the VM (in a
transient unit here, since it holds the terminal until the page finishes),
`istota-stack setup-code` for the printed address and code, the page through
Lima's 127.0.0.1:8080 forward from the Mac, then the same forward again once
`istota-stack` has installed the staged files, provisioned and started the
stack. The witness ends where a person's first run ends: signed in with the
password chosen at Finish.

The image and smoke tiers hold the setup server and the files it writes;
`tests/test_istota_stack.py` holds the host script against stubs. This is the
only place the two run together as root on a provisioned machine, with a real
publish, a real `install_staged`, `provision_outdated` and
`systemctl enable --now istota-stack`.

The case removes `config/config.toml`, since setup refuses a configured stack,
and the mode record, so the next module's mode is applied again with
`istota-stack setup --force`. Port 8080 on the Mac must be free for the
forward.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

import httpx
import pytest

from . import lima

pytestmark = pytest.mark.vm

UNIT = "istota-vmtier-websetup"
ORIGIN = "http://127.0.0.1:8080"
EMAIL = f"{lima.USER}@example.test"
PASSWORD = "a long enough passphrase for the vm tier"
SERVER_TIMEOUT = 300
SETUP_TIMEOUT = lima.BUILD_TIMEOUT
STACK_TIMEOUT = 900


@dataclass
class WebSetupOnVm:
    instructions: str
    code: str
    state: httpx.Response
    finish: httpx.Response
    unit_state: str
    unit_status: str
    setup_dir_left: bool
    code_after: str
    login_page: httpx.Response
    signed_in: httpx.Response
    me: httpx.Response


def _cookies(response: httpx.Response, jar: dict[str, str]) -> None:
    """Keep each `Set-Cookie` by hand: the web app's cookies are `Secure`
    (the wizard writes insecure cookies off for every ingress), and httpx sends
    a `Secure` cookie over https only, where a browser treats
    http://localhost as secure."""
    for header in response.headers.get_list("set-cookie"):
        name, _, rest = header.partition("=")
        jar[name.strip()] = rest.split(";", 1)[0]


def _cookie_header(jar: dict[str, str]) -> dict[str, str]:
    return {"cookie": "; ".join(f"{k}={v}" for k, v in jar.items())} if jar else {}


def _wait(client: httpx.Client, path: str, *, timeout: float, what: str) -> httpx.Response:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            response = client.get(path)
            if response.status_code == 200:
                return response
            last = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        time.sleep(3)
    raise lima.VmError(f"{what} did not answer at {ORIGIN}{path} within {timeout:.0f}s ({last})")


def _unit_finished(vm: lima.Vm) -> bool:
    return vm.out(f"systemctl show -p SubState --value {UNIT}", check=False) in ("exited", "failed", "dead")


@pytest.fixture(scope="module")
def web_setup(vm, release, model) -> WebSetupOnVm:
    vm.run(f"""
rm -f {lima.MODE_FILE}
systemctl stop {UNIT} 2>/dev/null || true
systemctl reset-failed {UNIT} 2>/dev/null || true
systemctl stop istota-stack.service || true
rm -f {lima.STACK}/config/config.toml
systemd-run --unit {UNIT} --property=Type=exec --property=RemainAfterExit=yes \\
    {lima.STACK_COMMAND} setup --web --local
""", timeout=600)
    try:
        instructions = ""
        lima.wait_for(
            lambda: "#code=" in vm.out(f"{lima.STACK_COMMAND} setup-code 2>/dev/null || true", check=False),
            timeout=SERVER_TIMEOUT, what="istota-stack setup-code to print the address")
        instructions = vm.out(f"{lima.STACK_COMMAND} setup-code")
        match = re.search(r"http://localhost:8080/istota/setup#code=([0-9A-Z-]+)", instructions)
        assert match, instructions
        code = match.group(1)

        with httpx.Client(base_url=ORIGIN, timeout=30, trust_env=False) as client:
            state = _wait(client, "/istota/api/setup/state", timeout=SERVER_TIMEOUT, what="the setup server")
            unlocked = client.post("/istota/api/setup/unlock", json={"code": code}, headers={"origin": ORIGIN})
            assert unlocked.status_code == 200, unlocked.text
            jar: dict[str, str] = {}
            _cookies(unlocked, jar)
            headers = {"origin": ORIGIN, **_cookie_header(jar)}
            put = client.put("/istota/api/setup/draft", headers=headers, json={
                "bot_name": "Istota", "display_name": "Admin", "user_id": lima.USER,
                "user_email": EMAIL, "timezone": "UTC",
                "brain_kind": "native", "native_base_url": model.vm_url,
                "native_model": "scripted-test-model", "native_api_key": lima.NATIVE_KEY,
                "ingress": "local", "hostname": "localhost:8080", "profiles": [],
                "password": PASSWORD,
            })
            assert put.status_code == 200, put.text
            finish = client.post("/istota/api/setup/finish", headers=headers)

        lima.wait_for(lambda: _unit_finished(vm), timeout=SETUP_TIMEOUT, interval=10,
                      what="istota-stack setup --web to install, provision and start")
        unit_state = vm.out(f"systemctl show -p SubState --value {UNIT}", check=False)
        unit_status = vm.out(f"systemctl show -p ExecMainStatus --value {UNIT}", check=False)
        if unit_status == "0":
            lima.wait_healthy(vm, timeout=STACK_TIMEOUT)
        setup_dir_left = vm.ok(f"test -e {lima.STACK}/.setup")
        code_after = vm.run(f"{lima.STACK_COMMAND} setup-code", check=False).stderr

        with httpx.Client(base_url=ORIGIN, timeout=30, trust_env=False) as browser:
            login_page = _wait(browser, f"/istota/login?email={EMAIL}", timeout=STACK_TIMEOUT,
                               what="the stack's login page")
            jar = {}
            _cookies(login_page, jar)
            form = re.search(r'<form\b[^>]*action="/istota/login/email"[^>]*>(.*?)</form>', login_page.text, re.S)
            fields = dict(re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form[1])) if form else {}
            signed_in = browser.post(
                "/istota/login/email", data={**fields, "email": EMAIL, "password": PASSWORD},
                headers=_cookie_header(jar),
            )
            _cookies(signed_in, jar)
            me = browser.get("/istota/api/me", headers=_cookie_header(jar))

        return WebSetupOnVm(
            instructions=instructions, code=code, state=state, finish=finish,
            unit_state=unit_state, unit_status=unit_status, setup_dir_left=setup_dir_left,
            code_after=code_after, login_page=login_page, signed_in=signed_in, me=me,
        )
    finally:
        journal = vm.out(f"journalctl -u {UNIT} --no-pager -n 80 2>/dev/null || true", check=False)
        vm.run(f"systemctl stop {UNIT} 2>/dev/null || true; systemctl reset-failed {UNIT} 2>/dev/null || true",
               check=False)
        if journal:
            print(f"--- {UNIT} ---\n{journal}")


class TestSetupWebLocal:
    def test_setup_code_prints_the_local_address_and_no_fingerprint(self, web_setup):
        assert "fingerprint" not in web_setup.instructions
        assert f"Setup code (if you open the page some other way): {web_setup.code}" in web_setup.instructions

    def test_the_page_answers_through_the_forward(self, web_setup):
        assert web_setup.state.json()["phase"] == "locked"
        assert web_setup.state.json()["local"] is True

    def test_finish_names_the_local_login_address(self, web_setup):
        assert web_setup.finish.status_code == 200, web_setup.finish.text
        assert web_setup.finish.json() == {"login_url": "http://localhost:8080/istota/login", "email": EMAIL}

    def test_istota_stack_installs_and_starts_the_stack(self, web_setup):
        assert (web_setup.unit_state, web_setup.unit_status) == ("exited", "0")
        assert not web_setup.setup_dir_left, ".setup/ is still there after a finished setup"
        assert "no setup in progress" in web_setup.code_after

    def test_the_admin_signs_in_on_the_same_port(self, web_setup):
        assert f'value="{EMAIL}"' in web_setup.login_page.text
        assert web_setup.signed_in.status_code == 302, web_setup.signed_in.text[-2000:]
        assert web_setup.me.status_code == 200, web_setup.me.text
        assert web_setup.me.json()["username"] == lima.USER
