"""Email sign-in through nginx, delivered SMTP mail, and the shipped CLI."""

from __future__ import annotations

from html import unescape
import re
import secrets
import subprocess
import time

import httpx
import pytest

from testbed.services import mail
from testbed.stack import CONTAINER_CONFIG

pytestmark = pytest.mark.full


def _form_fields(page: str, action: str) -> dict[str, str]:
    form = re.search(r'<form\b[^>]*action="' + re.escape(action) + r'"[^>]*>(.*?)</form>', page, re.S)
    assert form is not None, f"Missing form: {action}"
    return {
        name: unescape(value)
        for name, value in re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form[1])
    }


def _delivered_code(service, since: int, recipient: str) -> str:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        with service.session(mail.EXTERNAL_ADDRESS) as inbox:
            for message in inbox.fetch_new_since(since):
                if recipient not in message.recipients or not message.subject.endswith(" sign-in code"):
                    continue
                # A multipart sign-in mail's text part arrives with CRLF line ends.
                codes = re.findall(r"^(\d{6})\r?$", message.body_text, re.M)
                assert len(codes) == 1, "Delivered sign-in mail must carry one code on its own line"
                assert "http" not in message.body_text, "A sign-in mail must not carry a link (ISSUE-574)"
                assert message.subject.startswith(codes[0])
                return codes[0]
        time.sleep(0.5)
    pytest.fail("No sign-in email delivered to the recipient over Maddy IMAP")


@pytest.mark.profile("full")
def test_delivered_sign_in_code_and_password_login(stack):
    # Distinct recipient plus an IMAP watermark exclude background mail and
    # messages left by another scenario on the session-scoped full stack.
    user_id = "email-login-" + secrets.token_hex(4)
    address = f"{user_id}@ext.test"
    command = ["uv", "run", "istota", "-c", CONTAINER_CONFIG, "auth"]
    seeded = stack.exec(command + ["add", user_id, "--email", address, "--create-user"])
    assert seeded.returncode == 0, seeded.stderr

    port = stack.published_port("nginx", 80)
    origin = f"http://127.0.0.1:{port}"
    service = stack.service("mail")
    with service.session(mail.EXTERNAL_ADDRESS) as inbox:
        since = max(inbox.uids(), default=0)

    try:
        with httpx.Client(base_url=origin, timeout=30, trust_env=False) as browser:
            page = browser.get("/istota/login")
            assert page.status_code == 200
            assert "Log in with Nextcloud" in page.text
            password_form = _form_fields(page.text, "/istota/login/email")
            assert "csrf_token" in password_form
            assert 'type="password"' in page.text
            request_form = _form_fields(page.text, "/istota/auth/sign-in-code/request")
            requested = browser.post("/istota/auth/sign-in-code/request", data={**request_form, "email": address})
            assert requested.status_code == 200
            assert 'autocomplete="one-time-code"' in requested.text
            assert browser.get("/istota/api/me").status_code == 401
            code = _delivered_code(service, since, address)

            # The code is bound to the browser that asked: another client that
            # started its own request cannot spend it.
            with httpx.Client(base_url=origin, timeout=30, trust_env=False) as other:
                other_page = other.get("/istota/login")
                other_form = _form_fields(other_page.text, "/istota/auth/sign-in-code/request")
                other_request = other.post("/istota/auth/sign-in-code/request",
                                           data={**other_form, "email": "someone-else@ext.test"})
                stolen = other.post("/istota/auth/sign-in-code", data={
                    **_form_fields(other_request.text, "/istota/auth/sign-in-code"), "code": code,
                })
                assert stolen.status_code == 400
                assert other.get("/istota/api/me").status_code == 401

            fields = _form_fields(requested.text, "/istota/auth/sign-in-code")
            signed_in = browser.post("/istota/auth/sign-in-code", data={**fields, "code": code})
            assert signed_in.status_code == 302
            me = browser.get("/istota/api/me")
            assert me.status_code == 200
            assert me.json()["username"] == user_id
            assert me.json()["auth"] == {"method": "email", "email": address, "can_change_password": False}
            replayed = browser.post("/istota/auth/sign-in-code", data={**fields, "code": code})
            assert replayed.status_code in (400, 403)

        # Operator recovery: the CLI prints a code for the user's own pending
        # request, which still works only in the browser that made it.
        with httpx.Client(base_url=origin, timeout=30, trust_env=False) as browser:
            page = browser.get("/istota/login")
            requested = browser.post("/istota/auth/sign-in-code/request", data={
                **_form_fields(page.text, "/istota/auth/sign-in-code/request"), "email": address,
            })
            assert requested.status_code == 200
            printed = stack.exec(command + ["sign-in-code", user_id])
            assert printed.returncode == 0, printed.stderr
            code = re.search(r"code=(\d{6})", printed.stdout)[1]
            signed_in = browser.post("/istota/auth/sign-in-code", data={
                **_form_fields(requested.text, "/istota/auth/sign-in-code"), "code": code,
            })
            assert signed_in.status_code == 302
            assert browser.get("/istota/api/me").status_code == 200

            password = secrets.token_urlsafe(24)
            changed = subprocess.run(
                stack.args + ["exec", "-T", "istota", *command, "set-password", user_id, "--password-stdin"],
                input=password + "\n", text=True, capture_output=True, timeout=60,
            )
            assert changed.returncode == 0, changed.stderr
            assert browser.get("/istota/api/me").status_code == 401

        with httpx.Client(base_url=origin, timeout=30, trust_env=False) as browser:
            page = browser.get("/istota/login")
            fields = _form_fields(page.text, "/istota/login/email")
            signed_in = browser.post("/istota/login/email", data={**fields, "email": address, "password": password})
            assert signed_in.status_code == 302
            me = browser.get("/istota/api/me")
            assert me.status_code == 200
            assert me.json()["username"] == user_id
            assert me.json()["auth"] == {"method": "email", "email": address, "can_change_password": True}
    finally:
        removed = stack.exec(command + ["remove", user_id])
        assert removed.returncode == 0, removed.stderr
