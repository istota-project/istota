"""The web config editor's host applier, driven through the web API on the VM.

The default suite holds each half alone: the routes against a fake applier
(`tests/test_web_admin_config_edit.py`) and `istota-stack config-apply` against
a stand-in `docker` (`tests/test_istota_stack.py`). Only a real VM has the path
unit, the timer, the stack lock and a restart between them. Here an admin signs
in through nginx with a native email identity, previews and submits a change to
an inert key (`[scheduler] email_max_body_chars`, read only by the email
poller, which the local mode does not run), and the case reads the result off
the web API as the page does:

- the host takes the request within two minutes, the page's "not picked up"
  threshold;
- the request ends `applied`, the web process restarts after the row's
  `finished_at`, and `GET /admin/config` then reports the new value;
- two requests in quick succession are both applied, with the timer stopped
  so only the path unit and the run's own loop can take them. The second is
  submitted the moment the first is `applied`, while that run is restarting the
  stack; the case reads which run took each from the journal. Whether
  `PathModified` fired into a run still active is what the spec left
  unverified; when the second trigger arrived after the first run ended, the
  case skips and says so rather than passing as a confirmation.

Sign-in: `istota auth add` and `set-password --password-stdin` give the
setup's admin an email identity, and the password form signs in. Step-up: the
code is mailed, and the local mode has no mail, so the case reads the row's
`code_hash` (sha256 of `request_id:code`, `webui.auth._code_digest`) and finds
the six digits offline, the reversal that function's comment names. Each
step-up spends one of the address's three mails an hour, so the budget's
attempts are cleared first. The session cookie is `Secure`, which httpx keeps
off plain http, so it is carried by hand.

The key is put back with `istota-stack apply` at the end, if it moved.

Control `config-apply-not-triggered` (`scripts/test-vm-negative-control.sh`):
`istota-config-apply.path` and `istota-config-apply.timer` disabled before the
submission, so nothing starts the applier and the request is still `pending`
at the threshold. The request is cancelled and both units re-enabled after.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shlex
import time
from dataclasses import dataclass
from html import unescape

import httpx
import pytest

from . import lima

pytestmark = pytest.mark.vm

CONTROL = "config-apply-not-triggered"

#: `lima.local_mode` publishes nginx on the VM's 127.0.0.1:8080 with the site
#: hostname `localhost:8080`; `host/lima/istota.yaml` forwards it here.
BASE_URL = "http://127.0.0.1:8080"
ORIGIN = "http://localhost:8080"
API = "/istota/api"
CONFIG = f"{API}/admin/config"
EMAIL = f"{lima.USER}@example.test"
SECTION, KEY = "scheduler", "email_max_body_chars"

#: The page's threshold for "the host has not picked this up".
PICKUP_SECONDS = 120
APPLY_SECONDS = 600
#: The page retries for three minutes; a restart on the VM's six cores is well inside.
RESTART_SECONDS = 300
TERMINAL = ("applied", "unchanged", "failed", "refused", "cancelled")
UNITS = "istota-config-apply.path istota-config-apply.timer"
DB_READ = (
    "import sqlite3,sys;c=sqlite3.connect('file:/data/db/istota.db?mode=ro',uri=True);"
    "r=c.execute(sys.argv[1],sys.argv[2:]).fetchone();print('' if r is None else r[0])"
)
DB_WRITE = (
    "import sqlite3,sys;c=sqlite3.connect('/data/db/istota.db',timeout=30);"
    "c.execute(sys.argv[1],sys.argv[2:]);c.commit()"
)


class WebSession:
    """An httpx client that carries `istota_session` itself, over plain http."""

    def __init__(self) -> None:
        self.client = httpx.Client(base_url=BASE_URL, timeout=60, trust_env=False)
        self.cookie = ""

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = dict(kwargs.pop("headers", None) or {})
        if self.cookie:
            headers["Cookie"] = f"istota_session={self.cookie}"
        if method != "GET":
            headers.setdefault("Origin", ORIGIN)
        response = self.client.request(method, path, headers=headers, **kwargs)
        self.client.cookies.clear()
        for header in response.headers.get_list("set-cookie"):
            name, _, rest = header.partition("=")
            if name.strip() == "istota_session":
                value = rest.split(";", 1)[0].strip()
                self.cookie = "" if value in ("", "null") else value
        return response

    def get(self, path: str, **kwargs) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    def answer(self, path: str, *, timeout: float) -> dict:
        """GET `path`'s JSON, waiting out a web process that is restarting."""
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            try:
                response = self.get(path)
            except httpx.TransportError as exc:
                last = type(exc).__name__
            else:
                if response.status_code == 200:
                    return response.json()
                if response.status_code not in (502, 503, 504):
                    raise AssertionError(f"GET {path}: {response.status_code} {response.text[:2000]}")
                last = str(response.status_code)
            time.sleep(1)
        raise AssertionError(f"GET {path} did not answer within {timeout:.0f}s (last: {last})")

    def close(self) -> None:
        self.client.close()


@dataclass
class Editor:
    vm: lima.Vm
    web: WebSession
    original: int


@dataclass
class Change:
    id: int
    value: int


def _form_fields(page: str, action: str) -> dict[str, str]:
    form = re.search(r'<form\b[^>]*action="' + re.escape(action) + r'"[^>]*>(.*?)</form>', page, re.S)
    assert form is not None, f"no form posting to {action}"
    return {
        name: unescape(value)
        for name, value in re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form[1])
    }


def _db_read(vm: lima.Vm, sql: str, *args: str) -> str:
    argv = " ".join(shlex.quote(a) for a in (sql, *args))
    return lima.stack_exec(vm, f"python3 -c {shlex.quote(DB_READ)} {argv}").stdout.strip()


def _db_write(vm: lima.Vm, sql: str, *args: str) -> None:
    argv = " ".join(shlex.quote(a) for a in (sql, *args))
    lima.stack_exec(vm, f"python3 -c {shlex.quote(DB_WRITE)} {argv}")


def _sign_in(vm: lima.Vm, web: WebSession) -> None:
    vm.run(f"istota auth add {lima.USER} --email {EMAIL}")
    password = secrets.token_urlsafe(24)
    path = f"{lima.SCRATCH}/config-editor-password"
    vm.write(path, password + "\n", mode="0600")
    try:
        lima.stack_exec(vm, f"istota auth set-password {lima.USER} --password-stdin", stdin_file=path)
    finally:
        vm.run(f"rm -f {path}")
    page = web.get("/istota/login")
    assert page.status_code == 200, page.text[:2000]
    fields = _form_fields(page.text, "/istota/login/email")
    signed_in = web.post("/istota/login/email", data={**fields, "email": EMAIL, "password": password})
    assert signed_in.status_code == 302, signed_in.text[:2000]
    me = web.get(f"{API}/me")
    assert me.status_code == 200 and me.json()["username"] == lima.USER, me.text[:2000]


def _step_up(vm: lima.Vm, web: WebSession) -> dict:
    """A redeemable step-up proof, its code read back from the row's digest."""
    _db_write(vm, "DELETE FROM web_auth_attempts WHERE kind = 'mail_link' AND key = ?", EMAIL)
    response = web.post(f"{API}/settings/step-up", json={"action": "admin_config_change"})
    assert response.status_code == 200, response.text[:2000]
    request_id = response.json()["request_id"]
    digest = ""
    deadline = time.monotonic() + 60
    while not digest and time.monotonic() < deadline:
        digest = _db_read(vm, "SELECT code_hash FROM web_auth_step_ups WHERE request_id = ?", request_id)
        if not digest:
            time.sleep(1)
    assert digest, "the step-up row was never written"
    for number in range(1_000_000):
        code = f"{number:06d}"
        if hashlib.sha256(f"{request_id}:{code}".encode()).hexdigest() == digest:
            return {"request_id": request_id, "code": code}
    raise AssertionError("no six-digit code matches the step-up row")


def _field(view: dict) -> dict:
    for section in view["sections"]:
        if section["key"] == SECTION:
            for field in section["fields"]:
                if field["name"] == KEY:
                    return field
    raise AssertionError(f"{SECTION}.{KEY} is not in the view")


def _fresh_value(*avoid: int) -> int:
    while True:
        value = 30_000 + secrets.randbelow(1_000)
        if value not in avoid:
            return value


def _change_body(digest: str, value: int) -> dict:
    return {"base_digest": digest, "ops": [{"op": "set", "path": [SECTION, KEY], "value": value}]}


def _submit(editor: Editor, value: int) -> int:
    """Preview, then submit, a change of KEY to `value`; the request id."""
    web = editor.web
    view = web.answer(CONFIG, timeout=RESTART_SECONDS)
    body = _change_body(view["document_digest"], value)
    preview = web.post(f"{CONFIG}/preview", json=body)
    assert preview.status_code == 200, preview.text[:3000]
    shown = preview.json()
    assert shown["changed"] and not shown["errors"] and not shown["problems"]["blocking"], shown
    assert shown["changed_sections"] == [SECTION] and not shown["risky_keys"], shown
    body["step_up"] = _step_up(editor.vm, web)
    response = web.post(f"{CONFIG}/requests", json=body)
    assert response.status_code == 202, response.text[:3000]
    return response.json()["id"]


def _request(web: WebSession, request_id: int, *, timeout: float = RESTART_SECONDS) -> dict:
    return web.answer(f"{CONFIG}/requests/{request_id}", timeout=timeout)


def _wait_status(web: WebSession, request_id: int, done, *, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    row = _request(web, request_id)
    while not done(row["status"]) and time.monotonic() < deadline:
        time.sleep(1)
        row = _request(web, request_id)
    return row


def _wait_restarted(web: WebSession, finished_at: str) -> dict:
    """The view once the web process started after `finished_at`, as the page waits."""
    deadline = time.monotonic() + RESTART_SECONDS
    view = web.answer(CONFIG, timeout=RESTART_SECONDS)
    while not (view.get("started_at") or "") > finished_at and time.monotonic() < deadline:
        time.sleep(2)
        view = web.answer(CONFIG, timeout=RESTART_SECONDS)
    assert (view.get("started_at") or "") > finished_at, \
        f"the web process did not restart after {finished_at} (started {view.get('started_at')})"
    return view


def _settle_open_requests(web: WebSession) -> None:
    """Cancel a request an earlier session left pending; wait out one the host holds."""
    rows = web.answer(f"{CONFIG}/requests", timeout=RESTART_SECONDS)["requests"]
    for row in rows:
        if row["status"] == "pending":
            web.post(f"{CONFIG}/requests/{row['id']}/cancel")
        elif row["status"] in ("taken", "building"):
            _wait_status(web, row["id"], lambda s: s in TERMINAL, timeout=APPLY_SECONDS)


def _wait_applier_idle(vm: lima.Vm) -> None:
    lima.wait_for(
        lambda: vm.out("systemctl show -p ActiveState --value istota-config-apply.service")
        in ("inactive", "failed"),
        timeout=APPLY_SECONDS, what="istota-config-apply.service to finish its run")


def _unit_state(vm: lima.Vm) -> dict[str, str]:
    shown = vm.out("systemctl show -p ActiveState -p InvocationID istota-config-apply.service")
    return dict(line.split("=", 1) for line in shown.splitlines() if "=" in line)


def _taken_by(vm: lima.Vm, since: str, request_id: int) -> str:
    """The invocation of istota-config-apply.service that took the request, or ''."""
    lines = vm.out(
        f"journalctl -u istota-config-apply.service --since @{since} -o json "
        f"--output-fields=MESSAGE,_SYSTEMD_INVOCATION_ID | grep -a 'config-apply: request' || true")
    wanted = f"config-apply: request {request_id}: taken"
    for line in lines.splitlines():
        entry = json.loads(line)
        message = entry.get("MESSAGE")
        if isinstance(message, str) and message.endswith(wanted):
            return entry.get("_SYSTEMD_INVOCATION_ID", "")
    return ""


@pytest.fixture(scope="module")
def editor(local_stack):
    vm = local_stack
    web = WebSession()
    try:
        _sign_in(vm, web)
        view = web.answer(CONFIG, timeout=RESTART_SECONDS)
        assert view["applier"]["available"], view["applier"]
        field = _field(view)
        assert field["editor"] != "readonly", field
        _settle_open_requests(web)
    except BaseException:
        web.close()
        raise
    original = int(field["value"])
    try:
        yield Editor(vm, web, original)
    finally:
        try:
            current = int(_field(web.answer(CONFIG, timeout=RESTART_SECONDS))["value"])
            if current != original:
                lima.apply_config(vm, {SECTION: {KEY: original}})
                lima.wait_healthy(vm)
        finally:
            web.close()


@pytest.fixture(scope="module")
def first_change(editor):
    vm = editor.vm
    disabled = lima.control() == CONTROL
    if disabled:
        _wait_applier_idle(vm)
        vm.run(f"systemctl disable --now {UNITS}")
    request_id = None
    try:
        value = _fresh_value(editor.original)
        request_id = _submit(editor, value)
        yield Change(request_id, value)
    finally:
        if disabled:
            if request_id is not None:
                editor.web.post(f"{CONFIG}/requests/{request_id}/cancel")
            vm.run(f"systemctl enable --now {UNITS}")


class TestAChangeFromTheWeb:
    def test_the_host_picks_the_request_up(self, editor, first_change):
        row = _wait_status(editor.web, first_change.id, lambda s: s != "pending", timeout=PICKUP_SECONDS)
        assert row["status"] != "pending", (
            f"request {first_change.id} is still pending after {PICKUP_SECONDS}s: "
            "the host has not picked this up")

    def test_the_change_is_applied_and_read_back_after_the_restart(self, editor, first_change):
        row = _wait_status(editor.web, first_change.id, lambda s: s in TERMINAL, timeout=APPLY_SECONDS)
        assert row["status"] == "applied", row
        view = _wait_restarted(editor.web, row["finished_at"])
        field = _field(view)
        assert field["value"] == first_change.value, field
        assert field["in_file"], field


class TestTwoRequestsInQuickSuccession:
    def test_both_are_applied_by_the_path_unit_and_the_loop(self, editor, first_change):
        vm, web = editor.vm, editor.web
        _wait_status(web, first_change.id, lambda s: s in TERMINAL, timeout=APPLY_SECONDS)
        _wait_applier_idle(vm)
        since = vm.out("date +%s")
        vm.run("systemctl stop istota-config-apply.timer")
        try:
            first_value = _fresh_value(editor.original, first_change.value)
            second_value = _fresh_value(editor.original, first_change.value, first_value)
            first = _submit(editor, first_value)
            before = web.answer(CONFIG, timeout=RESTART_SECONDS)["document_digest"]
            proof = _step_up(vm, web)
            row = _wait_status(web, first, lambda s: s in TERMINAL, timeout=APPLY_SECONDS)
            assert row["status"] == "applied", row

            # At once, while the first run restarts the stack: the 409 names the
            # digest the first request left, and a restarting web is retried.
            body = {**_change_body(before, second_value), "step_up": proof}
            second = None
            deadline = time.monotonic() + RESTART_SECONDS
            while second is None and time.monotonic() < deadline:
                try:
                    response = web.post(f"{CONFIG}/requests", json=body)
                except httpx.TransportError:
                    time.sleep(0.5)
                    continue
                if response.status_code == 409 and response.json().get("error") == "config_changed":
                    body["base_digest"] = response.json()["digest"]
                elif response.status_code == 202:
                    second = response.json()["id"]
                elif response.status_code in (502, 503, 504):
                    time.sleep(0.5)
                else:
                    raise AssertionError(f"second submit: {response.status_code} {response.text[:3000]}")
            assert second is not None, "the second request was never accepted"
            at_submit = _unit_state(vm)

            row = _wait_status(web, second, lambda s: s in TERMINAL, timeout=APPLY_SECONDS)
            assert row["status"] == "applied", (
                f"the second request ended {row['status']!r} with the timer stopped: neither the path "
                f"unit nor the first run's loop took it\n{row}")
            field = _field(_wait_restarted(web, row["finished_at"]))
            assert field["value"] == second_value, field
        finally:
            vm.run("systemctl start istota-config-apply.timer")

        first_run = _taken_by(vm, since, first)
        second_run = _taken_by(vm, since, second)
        assert first_run and second_run, (first_run, second_run)
        during = second_run == first_run or (
            at_submit.get("ActiveState") in ("activating", "active")
            and at_submit.get("InvocationID") == first_run)
        if not during:
            pytest.skip(
                f"both applied, in {len({first_run, second_run})} runs, but the second trigger "
                "arrived after the first run ended: PathModified into an active run was not "
                f"exercised (unit at submit: {at_submit})")
