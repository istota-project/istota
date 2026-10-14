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

The second case is `istota-stack setup --web --force --local` over the stack
the first one set up, twice: once abandoned with `istota-stack setup-cancel`
after config.toml and secrets/ were changed under it (what a Finish that died
partway leaves), and once finished with one answer changed. Only here do the
real `compose stop`, the publish on the port nginx gave up, `docker rm -f` of
the named server, `istota setup --restore` and `cmd_up` run together.
"""

from __future__ import annotations

import re
import time
import tomllib
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


RERUN_UNIT = "istota-vmtier-websetup-rerun"
RERUN_BOT_NAME = "Istota Again"
WATCHDOG = "istota-browser-watchdog.timer"
SNAPSHOT = f"{lima.STACK}/config/.setup-rerun"
#: What a Finish that died partway could have left: a changed config and a
#: secret file the previous setup did not have.
HALF_WRITTEN_SECRET = f"{lima.STACK}/secrets/vmtier_half_written"


@dataclass
class RerunOnVm:
    config_before: str
    watchdog_before: str
    abandon_state: httpx.Response
    abandon_daemon_during: str
    abandon_watchdog_during: str
    abandon_unit: tuple[str, str]
    abandon_config_after: str
    abandon_half_written_left: bool
    abandon_snapshot_left: bool
    abandon_watchdog_after: str
    backups: list[str]
    seeded: httpx.Response
    finish: httpx.Response
    finish_unit: tuple[str, str]
    finish_config_after: str
    config_backups: list[str]
    finish_snapshot_left: bool
    finish_watchdog_after: str
    signed_in: httpx.Response
    me: httpx.Response


def _start_rerun(vm: lima.Vm) -> None:
    vm.run(f"""
systemctl stop {RERUN_UNIT} 2>/dev/null || true
systemctl reset-failed {RERUN_UNIT} 2>/dev/null || true
systemd-run --unit {RERUN_UNIT} --property=Type=exec --property=RemainAfterExit=yes \\
    {lima.STACK_COMMAND} setup --web --force --local
""", timeout=600)


def _rerun_code(vm: lima.Vm) -> str:
    """The re-run's code, which it prints only after it has stopped the stack."""
    lima.wait_for(
        lambda: "#code=" in vm.out(f"{lima.STACK_COMMAND} setup-code 2>/dev/null || true", check=False),
        timeout=SERVER_TIMEOUT, what="istota-stack setup-code to print the re-run's address")
    instructions = vm.out(f"{lima.STACK_COMMAND} setup-code")
    match = re.search(r"http://localhost:8080/istota/setup#code=([0-9A-Z-]+)", instructions)
    assert match, instructions
    return match.group(1)


def _unlock(client: httpx.Client, code: str) -> dict[str, str]:
    unlocked = client.post("/istota/api/setup/unlock", json={"code": code}, headers={"origin": ORIGIN})
    assert unlocked.status_code == 200, unlocked.text
    jar: dict[str, str] = {}
    _cookies(unlocked, jar)
    return {"origin": ORIGIN, **_cookie_header(jar)}


def _rerun_ended(vm: lima.Vm) -> tuple[str, str]:
    def sub_state() -> str:
        return vm.out(f"systemctl show -p SubState --value {RERUN_UNIT}", check=False)

    lima.wait_for(lambda: sub_state() in ("exited", "failed", "dead"), timeout=SETUP_TIMEOUT,
                  interval=10, what="istota-stack setup --web --force to end")
    return sub_state(), vm.out(f"systemctl show -p ExecMainStatus --value {RERUN_UNIT}", check=False)


def _watchdog(vm: lima.Vm) -> str:
    return vm.out(f"systemctl is-active {WATCHDOG} 2>/dev/null || true", check=False)


def _sign_in(vm: lima.Vm) -> tuple[httpx.Response, httpx.Response]:
    with httpx.Client(base_url=ORIGIN, timeout=30, trust_env=False) as browser:
        login_page = _wait(browser, f"/istota/login?email={EMAIL}", timeout=STACK_TIMEOUT,
                           what="the stack's login page after the re-run")
        jar: dict[str, str] = {}
        _cookies(login_page, jar)
        form = re.search(r'<form\b[^>]*action="/istota/login/email"[^>]*>(.*?)</form>', login_page.text, re.S)
        fields = dict(re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form[1])) if form else {}
        signed_in = browser.post(
            "/istota/login/email", data={**fields, "email": EMAIL, "password": PASSWORD},
            headers=_cookie_header(jar),
        )
        _cookies(signed_in, jar)
        return signed_in, browser.get("/istota/api/me", headers=_cookie_header(jar))


@pytest.fixture(scope="module")
def web_rerun(vm, web_setup) -> RerunOnVm:
    """Both re-runs, over the stack `web_setup` left running."""
    assert web_setup.unit_status == "0", "the first-run case did not set the stack up"
    lima.wait_healthy(vm, timeout=STACK_TIMEOUT)
    config_path = f"{lima.STACK}/config/config.toml"
    config_before = vm.read(config_path)
    watchdog_before = _watchdog(vm)
    try:
        # Abandoned: unlocked, a change drafted, files changed under it, then
        # cancelled from another shell.
        _start_rerun(vm)
        code = _rerun_code(vm)
        abandon_daemon_during = lima.container(vm, "daemon")
        abandon_watchdog_during = _watchdog(vm)
        with httpx.Client(base_url=ORIGIN, timeout=30, trust_env=False) as client:
            abandon_state = _wait(client, "/istota/api/setup/state", timeout=SERVER_TIMEOUT,
                                  what="the re-run's setup server")
            headers = _unlock(client, code)
            put = client.put("/istota/api/setup/draft", headers=headers, json={"bot_name": "Never Written"})
            assert put.status_code == 200, put.text
        vm.run(f"""
printf '\\n# half-written by a Finish that died\\n' >> {config_path}
install -m 0400 -o 10001 -g 10001 /dev/null {HALF_WRITTEN_SECRET}
""")
        vm.run(f"{lima.STACK_COMMAND} setup-cancel")
        abandon_unit = _rerun_ended(vm)
        lima.wait_healthy(vm, timeout=STACK_TIMEOUT)
        abandon_config_after = vm.read(config_path)
        abandon_half_written_left = vm.ok(f"test -e {HALF_WRITTEN_SECRET}")
        abandon_snapshot_left = vm.ok(f"test -e {SNAPSHOT}")
        abandon_watchdog_after = _watchdog(vm)

        # Finished: the seeded draft, one answer changed, Finish.
        _start_rerun(vm)
        code = _rerun_code(vm)
        with httpx.Client(base_url=ORIGIN, timeout=30, trust_env=False) as client:
            _wait(client, "/istota/api/setup/state", timeout=SERVER_TIMEOUT, what="the re-run's setup server")
            headers = _unlock(client, code)
            seeded = client.get("/istota/api/setup/draft", headers=headers)
            put = client.put("/istota/api/setup/draft", headers=headers, json={"bot_name": RERUN_BOT_NAME})
            assert put.status_code == 200, put.text
            finish = client.post("/istota/api/setup/finish", headers=headers)
        finish_unit = _rerun_ended(vm)
        if finish_unit[1] == "0":
            lima.wait_healthy(vm, timeout=STACK_TIMEOUT)
        finish_config_after = vm.read(config_path)
        config_backups = vm.out(f"ls {lima.STACK}/config | grep '^config.toml.bak' || true").split()
        finish_snapshot_left = vm.ok(f"test -e {SNAPSHOT}")
        finish_watchdog_after = _watchdog(vm)
        backups = vm.out(f"ls {lima.STACK}/backups 2>/dev/null | grep '^setup-' || true").split()
        signed_in, me = _sign_in(vm)

        return RerunOnVm(
            config_before=config_before, watchdog_before=watchdog_before,
            abandon_state=abandon_state, abandon_daemon_during=abandon_daemon_during,
            abandon_watchdog_during=abandon_watchdog_during, abandon_unit=abandon_unit,
            abandon_config_after=abandon_config_after,
            abandon_half_written_left=abandon_half_written_left,
            abandon_snapshot_left=abandon_snapshot_left, abandon_watchdog_after=abandon_watchdog_after,
            backups=backups, seeded=seeded, finish=finish, finish_unit=finish_unit,
            finish_config_after=finish_config_after, config_backups=config_backups,
            finish_snapshot_left=finish_snapshot_left, finish_watchdog_after=finish_watchdog_after,
            signed_in=signed_in, me=me,
        )
    finally:
        journal = vm.out(f"journalctl -u {RERUN_UNIT} --no-pager -n 120 2>/dev/null || true", check=False)
        vm.run(f"""
systemctl stop {RERUN_UNIT} 2>/dev/null || true
systemctl reset-failed {RERUN_UNIT} 2>/dev/null || true
rm -f {HALF_WRITTEN_SECRET}
""", check=False)
        if journal:
            print(f"--- {RERUN_UNIT} ---\n{journal}")


class TestSetupWebRerunAbandoned:
    def test_the_stack_is_stopped_before_the_page_answers(self, web_rerun):
        assert web_rerun.abandon_daemon_during == "", "the daemon ran while the re-run's page was up"
        assert web_rerun.abandon_watchdog_during != "active"

    def test_the_forward_reaches_the_rerun_server(self, web_rerun):
        state = web_rerun.abandon_state.json()
        assert (state["phase"], state["rerun"], state["local"]) == ("locked", True, True)
        assert "user_id" not in state

    def test_cancel_puts_the_previous_setup_back(self, web_rerun):
        assert web_rerun.abandon_unit[1] != "0", web_rerun.abandon_unit
        assert web_rerun.abandon_config_after == web_rerun.config_before
        assert not web_rerun.abandon_half_written_left, "a secret file the snapshot did not have was kept"
        assert not web_rerun.abandon_snapshot_left

    def test_the_watchdog_is_as_it_was(self, web_rerun):
        assert web_rerun.abandon_watchdog_after == web_rerun.watchdog_before

    def test_each_run_backed_up_the_stack_env_files(self, web_rerun):
        assert len(web_rerun.backups) >= 2, web_rerun.backups


class TestSetupWebRerunFinished:
    def test_the_draft_is_seeded_from_the_stack(self, web_rerun):
        assert web_rerun.seeded.status_code == 200, web_rerun.seeded.text
        draft = web_rerun.seeded.json()["draft"]
        assert (draft["user_id"], draft["user_email"]) == (lima.USER, EMAIL)
        assert draft["native_api_key"] == {"set": True}
        assert "password" not in draft

    def test_finish_answers_without_an_email(self, web_rerun):
        assert web_rerun.finish.status_code == 200, web_rerun.finish.text
        assert web_rerun.finish.json() == {"login_url": "http://localhost:8080/istota/login"}

    def test_the_change_is_written_and_the_stack_starts(self, web_rerun):
        assert web_rerun.finish_unit == ("exited", "0")
        before = tomllib.loads(web_rerun.config_before)
        after = tomllib.loads(web_rerun.finish_config_after)
        assert after["bot_name"] == RERUN_BOT_NAME
        assert {k: v for k, v in after.items() if k != "bot_name"} == \
            {k: v for k, v in before.items() if k != "bot_name"}
        assert web_rerun.config_backups, "no config.toml.bak.<stamp> after the re-run"
        assert not web_rerun.finish_snapshot_left
        assert web_rerun.finish_watchdog_after == web_rerun.watchdog_before

    def test_the_admin_keeps_their_password(self, web_rerun):
        assert web_rerun.signed_in.status_code == 302, web_rerun.signed_in.text[-2000:]
        assert web_rerun.me.status_code == 200, web_rerun.me.text
        assert web_rerun.me.json()["username"] == lima.USER
