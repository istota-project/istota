"""Email sign-in through nginx, delivered SMTP mail, and the shipped CLI."""

from __future__ import annotations

from html import unescape
import re
import secrets
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit

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


def _delivered_link(service, since: int, recipient: str) -> str:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        with service.session(mail.EXTERNAL_ADDRESS) as inbox:
            for message in inbox.fetch_new_since(since):
                if recipient not in message.recipients or not message.subject.endswith(": Sign in"):
                    continue
                links = re.findall(r'https?://[^\s<>]+/istota/auth/login-link\?token=[^\s<>]+', message.body_text)
                assert len(links) == 1, "Delivered sign-in mail must contain one plain-text link"
                return links[0]
        time.sleep(0.5)
    pytest.fail("No sign-in email delivered to the recipient over Maddy IMAP")


@pytest.mark.profile("full")
def test_delivered_sign_in_link_and_password_login(stack):
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
            request_form = _form_fields(page.text, "/istota/auth/login-link/request")
            requested = browser.post("/istota/auth/login-link/request", data={**request_form, "email": address})
            assert requested.status_code == 200
            assert browser.get("/istota/api/me").status_code == 401

            delivered = urlsplit(_delivered_link(service, since, address))
            assert delivered.scheme == "http"
            assert delivered.hostname in {"localhost", "127.0.0.1"}
            assert delivered.port == port, "The emailed link must target the published nginx port"
            # Compose advertises localhost. Use published_port's IPv4 answer
            # without changing the delivered path or token.
            link = urlunsplit((delivered.scheme, f"127.0.0.1:{port}", delivered.path, delivered.query, ""))
            with httpx.Client(base_url=origin, timeout=30, trust_env=False) as scanner:
                scanned = scanner.get(link)
                assert scanned.status_code == 200
                _form_fields(scanned.text, "/istota/auth/login-link")
                assert scanner.get("/istota/api/me").status_code == 401

            confirmation = browser.get(link)
            assert confirmation.status_code == 200
            fields = _form_fields(confirmation.text, "/istota/auth/login-link")
            assert browser.get("/istota/api/me").status_code == 401
            signed_in = browser.post("/istota/auth/login-link", data=fields)
            assert signed_in.status_code == 302
            me = browser.get("/istota/api/me")
            assert me.status_code == 200
            assert me.json()["username"] == user_id
            assert me.json()["auth"] == {"method": "email", "email": address, "can_change_password": False}

            # Replay with the original CSRF-bearing scanner session, so a
            # rejection cannot be explained by a missing confirmation form.
            with httpx.Client(base_url=origin, timeout=30, trust_env=False) as replay:
                replay.cookies.update(scanner.cookies)
                rejected = replay.post("/istota/auth/login-link", data=_form_fields(scanned.text, "/istota/auth/login-link"))
                assert rejected.status_code == 400
                assert "This link is invalid" in rejected.text
                assert replay.get("/istota/api/me").status_code == 401

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
