"""Browser handoff across real middleware, signed cookies and SQLite."""

import base64
import hashlib
import json
import re
import secrets
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from istota import db, user_profiles
from istota.config import Config, SiteConfig, UserConfig, WebConfig
from istota.webui import auth

ROOT = "/istota/auth/mobile"
PASSWORD = "a long example passphrase"


@pytest.fixture
def configured(db_path, monkeypatch):
    from istota.webui import app as web

    config = Config(db_path=db_path, site=SiteConfig(hostname="example.com"),
                    web=WebConfig(auth=["email", "nextcloud"]),
                    users={"alice": UserConfig(display_name="Alice")})
    monkeypatch.setattr(web, "_config", config)
    monkeypatch.setattr(web.app.state, "istota_config", config, raising=False)
    monkeypatch.setattr(auth, "_N", 1024)
    user_profiles.ensure_profile(db_path, "alice", display_name="Alice")
    auth.upsert_identity(db_path, "alice", "alice@example.com")
    auth.set_password(db_path, "alice", PASSWORD)
    oauth = MagicMock()
    oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": "alice"})
    monkeypatch.setattr(web, "_oauth", oauth)
    return web


@pytest.fixture
async def client(configured):
    async with AsyncClient(transport=ASGITransport(app=configured.app),
                           base_url="https://example.com") as client:
        yield client


def csrf(response):
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text)[1]


def session(client):
    return json.loads(base64.b64decode(client.cookies.get("istota_session").split(".")[0]))


async def create(client):
    verifier, state = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    response = await client.post(ROOT + "/requests", json={"challenge": challenge, "state": state})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload == {"request_id": payload["request_id"],
                       "authorize_path": ROOT + "/authorize/" + payload["request_id"], "expires_in": 600}
    return payload, verifier, state


async def sign_in(client):
    page = await client.get("/istota/login")
    return await client.post("/istota/login/email", data={
        "email": "alice@example.com", "password": PASSWORD, "csrf_token": csrf(page),
    })


async def approve(client, attempt):
    page = await client.get(attempt["authorize_path"])
    assert page.status_code == 200
    assert "alice" in page.text and "https://example.com/istota/" in page.text
    response = await client.post(attempt["authorize_path"], data={"csrf_token": csrf(page)},
                                 headers={"Origin": "https://example.com"})
    assert response.status_code == 303, response.text
    callback = urlsplit(response.headers["location"])
    assert (callback.scheme, callback.netloc, callback.path) == ("com.cynium.istota.auth", "complete", "")
    fields = parse_qs(callback.query)
    assert set(fields) == {"request_id", "code", "state"}
    return {name: values[0] for name, values in fields.items()}


async def test_email_handoff_continues_login_and_creates_fresh_session(client, configured):
    attempt, verifier, state = await create(client)
    assert not client.cookies
    response = await client.get(attempt["authorize_path"])
    assert response.status_code == 302 and response.headers["location"] == "/istota/login"
    login = await sign_in(client)
    assert login.headers["location"] == attempt["authorize_path"]
    browser_session = session(client)
    callback = await approve(client, attempt)
    assert callback["state"] == state and callback["request_id"] == attempt["request_id"]
    async with AsyncClient(transport=ASGITransport(app=configured.app), base_url="https://example.com") as native:
        assert (await native.get("/istota/api/mobile/session")).status_code == 401
        response = await native.post(ROOT + "/exchange", data={
            "request_id": callback["request_id"], "code": callback["code"], "verifier": verifier,
        })
        assert response.status_code == 303, response.text
        assert response.headers["location"] == ROOT + "/complete"
        cookie = response.headers["set-cookie"].lower()
        assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie
        fresh = session(native)
        assert set(fresh) == {"user", "auth", "session_id", "mobile_handoff_id"}
        assert fresh["session_id"] != browser_session["session_id"]
        assert fresh["auth"] == browser_session["auth"]
        receipt = await native.get("/istota/api/mobile/session")
        assert receipt.status_code == 200
        assert receipt.json() == {"request_id": attempt["request_id"], "user_id": "alice"}
        assert receipt.headers["cache-control"] == "no-store"
        assert (await native.get(response.headers["location"])).status_code == 200
        replay = await native.post(ROOT + "/exchange", data={
            "request_id": callback["request_id"], "code": callback["code"], "verifier": verifier,
        })
        assert replay.status_code == 409
        auth.bump_epoch(configured._config.db_path, "alice")
        assert (await native.get("/istota/api/mobile/session")).status_code == 401


def in_app_sheet(page) -> bool:
    return '<body class="in-app-sheet">' in page.text


async def test_sign_in_pages_inside_the_app_sheet_drop_the_page_chrome(client, configured, monkeypatch):
    monkeypatch.setattr(configured, "_LOGIN_FAILURE_SECONDS", 0)
    plain = await client.get("/istota/login")
    assert not in_app_sheet(plain) and "<footer>" in plain.text
    attempt, _, _ = await create(client)
    await client.get(attempt["authorize_path"])
    login = await client.get("/istota/login")
    assert in_app_sheet(login) and "<footer>" not in login.text
    refused = await client.post("/istota/login/email", data={
        "email": "alice@example.com", "password": "not the password", "csrf_token": csrf(login),
    })
    assert refused.status_code == 400 and in_app_sheet(refused)
    await sign_in(client)
    # The fresh session no longer holds the request; the authorize path still says where we are.
    assert in_app_sheet(await client.get(attempt["authorize_path"]))


async def test_sign_in_pages_inside_the_app_sheet_do_not_zoom(client, configured):
    """ISSUE-821: the sheet's UA cannot say, so the server locks the viewport."""
    locked = f'<meta name="viewport" content="{configured._VIEWPORT_LOCKED}">'
    assert locked not in (await client.get("/istota/login")).text
    attempt, _, _ = await create(client)
    await client.get(attempt["authorize_path"])
    assert locked in (await client.get("/istota/login")).text


async def test_a_browser_sign_in_keeps_the_full_page(client, configured, monkeypatch):
    monkeypatch.setattr(configured, "_LOGIN_FAILURE_SECONDS", 0)
    refused = await client.post("/istota/login/email", data={
        "email": "alice@example.com", "password": "not the password",
        "csrf_token": csrf(await client.get("/istota/login")),
    })
    assert refused.status_code == 400 and not in_app_sheet(refused) and "<footer>" in refused.text


@pytest.mark.parametrize("endpoint,content_type", [
    ("requests", "application/json"), ("exchange", "application/x-www-form-urlencoded"),
])
@pytest.mark.parametrize("case,status", [("type", 415), ("origin", 403), ("port", 403),
                                          ("scheme", 403), ("null", 403), ("zero-port", 403), ("length", 413)])
async def test_rejected_headers_never_consume_body(client, endpoint, content_type, case, status):
    async def unread():
        pytest.fail("rejected handoff consumed a body")
        yield b"x"

    headers = {"Content-Type": content_type}
    if case == "type":
        headers["Content-Type"] = "text/plain"
    elif case == "length":
        headers["Content-Length"] = "4097"
    else:
        headers["Origin"] = {"origin": "https://other.example.com", "port": "https://example.com:444",
                             "scheme": "http://example.com", "null": "null",
                             "zero-port": "https://example.com:0"}[case]
    response = await client.post(ROOT + "/" + endpoint, content=unread(), headers=headers)
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("declared", [None, "1"])
@pytest.mark.parametrize("endpoint,content_type", [
    ("requests", "application/json"), ("exchange", "application/x-www-form-urlencoded"),
])
async def test_stream_limit_stops_before_the_rest(client, declared, endpoint, content_type):
    taken = 0

    async def chunks():
        nonlocal taken
        for _ in range(10):
            taken += 1024
            yield b"x" * 1024

    headers = {"Content-Type": content_type}
    if declared:
        headers["Content-Length"] = declared
    response = await client.post(ROOT + "/" + endpoint, content=chunks(), headers=headers)
    assert response.status_code == 413
    assert taken == 5120


@pytest.mark.parametrize("endpoint,body", [
    ("requests", b'{"challenge":"x","challenge":"y","state":"z"}'),
    ("requests", b'{"challenge":"x","state":"z","callback":"https://other.example.com"}'),
    ("requests", b'{"challenge":1,"state":null}'),
    ("requests", b'{"challenge":"x","state":"\xff"}'),
    ("requests", b'[' * 2000 + b']' * 2000),
    ("exchange", b'request_id=x&code=y&verifier=z&verifier=a'),
    ("exchange", b'request_id=x&code=y&verifier=%ff'),
    ("exchange", b'request_id=x&code=y&verifier=%xx'),
    ("exchange", b'request_id=x&code=y&verifier=z&next=https://other.example.com'),
])
async def test_strict_fields_fail_closed(client, endpoint, body):
    content_type = "application/json" if endpoint == "requests" else "application/x-www-form-urlencoded"
    response = await client.post(ROOT + "/" + endpoint, content=body, headers={"Content-Type": content_type})
    assert response.status_code == 400
    assert response.json() == {"error": "mobile_handoff_failed"}


async def test_chunked_native_requests_and_ordinary_api_body_guard(client, configured):
    verifier, state = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

    async def chunked(raw):
        yield raw[:10]
        yield raw[10:]

    response = await client.post(ROOT + "/requests", content=chunked(json.dumps({
        "challenge": challenge, "state": state,
    }).encode()), headers={"Content-Type": "application/json", "Origin": "https://example.com:443"})
    assert response.status_code == 200
    attempt = response.json()
    await sign_in(client)
    callback = await approve(client, attempt)
    client.cookies.clear()
    response = await client.post(ROOT + "/exchange", content=chunked(
        f'request_id={attempt["request_id"]}&code={callback["code"]}&verifier={verifier}'.encode()),
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code == 303
    client.cookies.clear()

    async def unread():
        pytest.fail("ordinary anonymous API body was read")
        yield b"{}"

    for path in ("/istota/api/mobile/config", "/istota/api/push/devices", "/istota/api/mobile/session"):
        assert (await client.post(path, content=unread())).status_code == 401


async def test_approval_needs_live_session_csrf_and_explicit_action(client, configured):
    attempt, verifier, _ = await create(client)
    assert (await client.post(attempt["authorize_path"], data={"csrf_token": "x"})).status_code == 401
    await sign_in(client)
    page = await client.get(attempt["authorize_path"])
    with db.get_db(configured._config.db_path) as conn:
        row = conn.execute("SELECT code_digest FROM web_mobile_auth_requests").fetchone()
    assert row[0] is None
    bad = await client.post(attempt["authorize_path"], data={"csrf_token": "x"})
    assert bad.status_code == 403
    auth.bump_epoch(configured._config.db_path, "alice")
    stale = await client.post(attempt["authorize_path"], data={"csrf_token": csrf(page)})
    assert stale.status_code == 401


@pytest.mark.parametrize("mutation", ["disabled", "epoch", "method", "expired", "verifier", "code"])
async def test_exchange_rechecks_authority_and_proof(client, configured, mutation):
    attempt, verifier, _ = await create(client)
    await sign_in(client)
    callback = await approve(client, attempt)
    if mutation == "disabled":
        auth.set_disabled(configured._config.db_path, "alice", True)
    elif mutation == "epoch":
        auth.bump_epoch(configured._config.db_path, "alice")
    elif mutation == "method":
        configured._config.web.auth = ["nextcloud"]
    elif mutation == "expired":
        with db.get_db(configured._config.db_path) as conn:
            conn.execute("UPDATE web_mobile_auth_requests SET code_expires_at=datetime('now','-1 second')")
    elif mutation == "verifier":
        verifier = secrets.token_urlsafe(32)
    else:
        callback["code"] = secrets.token_urlsafe(32)
    client.cookies.clear()
    response = await client.post(ROOT + "/exchange", data={
        "request_id": attempt["request_id"], "code": callback["code"], "verifier": verifier,
    })
    assert response.status_code == (410 if mutation == "expired" else 400)
    assert not client.cookies
    assert (await client.get("/istota/api/mobile/session")).status_code == 401


async def test_creation_is_throttled_through_http(client):
    for _ in range(5):
        await create(client)
    response = await client.post(ROOT + "/requests", json={
        "challenge": secrets.token_urlsafe(32), "state": secrets.token_urlsafe(32),
    })
    assert response.status_code == 429


async def test_email_code_resumes_mobile_approval(client, configured):
    attempt, _, _ = await create(client)
    await client.get(attempt["authorize_path"])
    page = await client.get("/istota/login")
    code_token = re.findall(r'name="csrf_token" value="([^"]+)"', page.text)[1]
    response = await client.post("/istota/auth/sign-in-code/request", data={
        "email": "alice@example.com", "csrf_token": code_token,
    })
    code = auth.mint_sign_in_code(configured._config.db_path, "alice").code
    signed_in = await client.post("/istota/auth/sign-in-code", data={"code": code, "csrf_token": csrf(response)})
    assert signed_in.status_code == 302
    assert signed_in.headers["location"] == attempt["authorize_path"]
    assert "mobile_login_request" not in session(client)
    await approve(client, attempt)


async def test_nextcloud_oauth_state_and_identity_without_email(client, configured, monkeypatch):
    from authlib.integrations.starlette_client import OAuth
    from httpx import MockTransport, Response

    auth.delete_identity(configured._config.db_path, "alice")
    requests = []

    async def provider(request):
        requests.append(request)
        return Response(200, json={"access_token": "example-provider-token", "token_type": "Bearer",
                                   "user_id": "alice"})

    oauth = OAuth()
    oauth.register("nextcloud", client_id="example-client", client_secret="example-secret",
                   authorize_url="https://identity.example.com/authorize",
                   access_token_url="https://identity.example.com/token",
                   client_kwargs={"transport": MockTransport(provider)})
    monkeypatch.setattr(configured, "_oauth", oauth)
    attempt, verifier, state = await create(client)
    await client.get(attempt["authorize_path"])
    response = await client.get("/istota/login?go=1")
    provider_state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    assert provider_state != state
    assert any(name.startswith("_state_nextcloud_") for name in session(client))
    wrong = await client.get("/istota/callback", params={"code": "example-code", "state": state})
    assert wrong.status_code == 400 and not requests
    response = await client.get("/istota/callback", params={"code": "example-code", "state": provider_state})
    assert response.status_code == 302
    assert response.headers["location"] == attempt["authorize_path"]
    assert len(requests) == 1
    callback = await approve(client, attempt)
    client.cookies.clear()
    response = await client.post(ROOT + "/exchange", data={
        "request_id": attempt["request_id"], "code": callback["code"], "verifier": verifier,
    })
    assert response.status_code == 303
    assert session(client)["auth"]["method"] == "nextcloud"
    assert auth.get_identity(configured._config.db_path, "alice") is None
    assert (await client.get("/istota/api/mobile/session")).json()["user_id"] == "alice"


async def test_continuation_expiry_and_arbitrary_next_are_not_redirects(client, configured):
    attempt, _, _ = await create(client)
    await client.get(attempt["authorize_path"])
    with db.get_db(configured._config.db_path) as conn:
        conn.execute("UPDATE web_mobile_auth_requests SET expires_at=datetime('now','-1 second')")
    page = await client.get("/istota/login?next=https://other.example.com/")
    response = await client.post("/istota/login/email", data={
        "email": "alice@example.com", "password": PASSWORD, "csrf_token": csrf(page),
        "next": "https://other.example.com/",
    })
    assert response.headers["location"] == "/istota/"
    assert "mobile_login_request" not in session(client)
    assert (await client.get(attempt["authorize_path"])).status_code == 410
    assert (await client.get("/istota/api/mobile/session")).status_code == 401


async def test_ambient_session_is_replaced_and_lost_response_does_not_allow_replay(client, configured):
    attempt, verifier, _ = await create(client)
    await sign_in(client)
    callback = await approve(client, attempt)
    old_session = session(client)
    old_cookie = client.cookies.get("istota_session")
    fields = {"request_id": attempt["request_id"], "code": callback["code"], "verifier": verifier}
    response = await client.post(ROOT + "/exchange", data=fields)
    assert response.status_code == 303
    assert session(client)["session_id"] != old_session["session_id"]
    assert not any(name.startswith("csrf.") for name in session(client))
    # A lost navigation completion with the cookie installed is recoverable.
    assert (await client.get("/istota/api/mobile/session")).json()["request_id"] == attempt["request_id"]
    # A lost cookie as well cannot be mistaken for that completed handoff.
    client.cookies.clear()
    client.cookies.set("istota_session", old_cookie, domain="example.com", path="/istota/")
    assert (await client.get("/istota/api/mobile/session")).status_code == 401
    assert (await client.post(ROOT + "/exchange", data=fields)).status_code == 409


async def test_no_auth_requires_configured_user_approval_and_receipt_cookie(client, configured):
    configured._config.web.auth = ["none"]
    configured._config.site.hostname = "localhost:8766"
    client.base_url = "https://localhost:8766"
    attempt, verifier, _ = await create(client)
    assert (await client.get("/istota/api/mobile/session")).status_code == 401
    response = await client.post(ROOT + "/exchange", data={
        "request_id": attempt["request_id"], "code": secrets.token_urlsafe(32), "verifier": verifier,
    })
    assert response.status_code == 400
    page = await client.get(attempt["authorize_path"])
    assert page.status_code == 200 and "alice" in page.text
    response = await client.post(attempt["authorize_path"], data={"csrf_token": csrf(page)},
                                 headers={"Origin": "http://localhost:8766"})
    assert response.status_code == 303
    code = parse_qs(urlsplit(response.headers["location"]).query)["code"][0]
    response = await client.post(ROOT + "/exchange", data={
        "request_id": attempt["request_id"], "code": code, "verifier": verifier,
    })
    assert response.status_code == 303
    assert (await client.get("/istota/api/mobile/session")).json() == {
        "request_id": attempt["request_id"], "user_id": "alice",
    }
    configured._config.users = {"bob": UserConfig()}
    assert (await client.get("/istota/api/mobile/session")).status_code == 401


async def test_concurrent_exchange_has_one_winner(client, configured):
    import asyncio

    attempt, verifier, _ = await create(client)
    await sign_in(client)
    callback = await approve(client, attempt)
    fields = {"request_id": attempt["request_id"], "code": callback["code"], "verifier": verifier}

    async def redeem():
        async with AsyncClient(transport=ASGITransport(app=configured.app), base_url="https://example.com") as native:
            response = await native.post(ROOT + "/exchange", data=fields)
            receipt = await native.get("/istota/api/mobile/session")
            return response.status_code, receipt.status_code

    assert sorted(await asyncio.gather(redeem(), redeem())) == [(303, 200), (409, 401)]


async def test_exchange_failure_after_consumption_stays_consumed(client, configured, monkeypatch):
    import sqlite3

    attempt, verifier, _ = await create(client)
    await sign_in(client)
    callback = await approve(client, attempt)
    fields = {"request_id": attempt["request_id"], "code": callback["code"], "verifier": verifier}
    client.cookies.clear()
    original = user_profiles.get_profile

    def unavailable(*args):
        raise sqlite3.OperationalError("unavailable")

    monkeypatch.setattr(user_profiles, "get_profile", unavailable)
    response = await client.post(ROOT + "/exchange", data=fields)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert not client.cookies
    monkeypatch.setattr(user_profiles, "get_profile", original)
    assert (await client.post(ROOT + "/exchange", data=fields)).status_code == 409
    assert (await client.get("/istota/api/mobile/session")).status_code == 401


async def test_local_code_continuation_keeps_host_guard(client, configured):
    configured._config.web.auth = ["local_code"]
    configured._config.site.hostname = "localhost:8766"
    client.base_url = "https://localhost:8766"
    attempt, _, _ = await create(client)
    await client.get(attempt["authorize_path"])
    page = await client.get("/istota/auth/local")
    code = auth.issue_token(configured._config.db_path, "alice", "local_login", 60)
    response = await client.post("/istota/auth/local", data={"code": code, "csrf_token": csrf(page)})
    assert response.status_code == 302 and response.headers["location"] == attempt["authorize_path"]
    assert session(client)["auth"]["method"] == "local_code"
    assert (await client.get(attempt["authorize_path"])).status_code == 200
    response = await client.get(attempt["authorize_path"], headers={"Host": "other.example.com"})
    assert response.status_code == 403


async def test_approval_escapes_identity_and_uses_configured_installation(client, configured):
    attempt, _, _ = await create(client)
    user_profiles.update_profile(configured._config.db_path, "alice", display_name='<img src=x onerror="bad()">')
    await sign_in(client)
    page = await client.get(attempt["authorize_path"], headers={"Host": "other.example.com",
                                                             "X-Forwarded-Host": "forwarded.example.com"})
    assert page.status_code == 200
    assert "&lt;img" in page.text and "<img src=x" not in page.text
    assert "https://example.com/istota/" in page.text
    assert "other.example.com" not in page.text and "forwarded.example.com" not in page.text


async def test_invalid_proofs_and_unavailable_database_fail_closed(client, configured):
    for challenge, state in [("x", secrets.token_urlsafe(32)), (secrets.token_urlsafe(32), "x")]:
        response = await client.post(ROOT + "/requests", json={"challenge": challenge, "state": state})
        assert response.status_code == 400 and not client.cookies
    with db.get_db(configured._config.db_path) as conn:
        conn.execute("DROP TABLE web_mobile_auth_requests")
    response = await client.post(ROOT + "/requests", json={
        "challenge": secrets.token_urlsafe(32), "state": secrets.token_urlsafe(32),
    })
    assert response.status_code == 503 and not client.cookies


async def test_no_auth_receipt_rejects_disabled_account(client, configured):
    configured._config.web.auth = ["none"]
    configured._config.site.hostname = "localhost:8766"
    client.base_url = "https://localhost:8766"
    attempt, verifier, _ = await create(client)
    page = await client.get(attempt["authorize_path"])
    approved = await client.post(attempt["authorize_path"], data={"csrf_token": csrf(page)})
    code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
    exchanged = await client.post(ROOT + "/exchange", data={
        "request_id": attempt["request_id"], "code": code, "verifier": verifier,
    })
    assert exchanged.status_code == 303
    auth.set_disabled(configured._config.db_path, "alice", True)
    assert (await client.get("/istota/api/mobile/session")).status_code == 401
