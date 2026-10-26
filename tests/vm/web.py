"""The web UI and the framework DB as the vm tier's cases reach them.

A signed-in session through nginx on the local mode, and one-row reads and
writes of `/data/db/istota.db` from inside the daemon container. Shared by
`test_config_editor.py` and `test_assignments.py`. Imports no pytest.
"""

from __future__ import annotations

import re
import secrets
import shlex
import time
from html import unescape

import httpx

from . import lima

#: `lima.local_mode` publishes nginx on the VM's 127.0.0.1:8080 with the site
#: hostname `localhost:8080`; `host/lima/istota.yaml` forwards it here.
BASE_URL = "http://127.0.0.1:8080"
ORIGIN = "http://localhost:8080"
API = "/istota/api"
EMAIL = f"{lima.USER}@example.test"

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
        self.password = ""

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


def form_fields(page: str, action: str) -> dict[str, str]:
    form = re.search(r'<form\b[^>]*action="' + re.escape(action) + r'"[^>]*>(.*?)</form>', page, re.S)
    assert form is not None, f"no form posting to {action}"
    return {
        name: unescape(value)
        for name, value in re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form[1])
    }


def db_read(vm: lima.Vm, sql: str, *args: str) -> str:
    """The first column of the first row, '' for none."""
    argv = " ".join(shlex.quote(str(a)) for a in (sql, *args))
    return lima.stack_exec(vm, f"python3 -c {shlex.quote(DB_READ)} {argv}").stdout.strip()


def db_write(vm: lima.Vm, sql: str, *args: str) -> None:
    argv = " ".join(shlex.quote(str(a)) for a in (sql, *args))
    lima.stack_exec(vm, f"python3 -c {shlex.quote(DB_WRITE)} {argv}")


def sign_in(vm: lima.Vm, web: WebSession) -> None:
    """Give the setup's admin an email identity and a fresh password, and sign in."""
    vm.run(f"istota auth add {lima.USER} --email {EMAIL}")
    password = secrets.token_urlsafe(24)
    path = f"{lima.SCRATCH}/web-sign-in-password"
    vm.write(path, password + "\n", mode="0600")
    try:
        lima.stack_exec(vm, f"istota auth set-password {lima.USER} --password-stdin", stdin_file=path)
    finally:
        vm.run(f"rm -f {path}")
    page = web.get("/istota/login")
    assert page.status_code == 200, page.text[:2000]
    fields = form_fields(page.text, "/istota/login/email")
    signed_in = web.post("/istota/login/email", data={**fields, "email": EMAIL, "password": password})
    assert signed_in.status_code == 302, signed_in.text[:2000]
    me = web.get(f"{API}/me")
    assert me.status_code == 200 and me.json()["username"] == lima.USER, me.text[:2000]
    web.password = password
