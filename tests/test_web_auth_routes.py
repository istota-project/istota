"""Native login and live session revocation through the web routes."""

import base64
import json
import re
import sqlite3
from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
from itsdangerous import TimestampSigner
import pytest

pytest.importorskip("authlib")
pytest.importorskip("fastapi")
from starlette.requests import Request

from istota import db, user_profiles
from istota.webui import auth as web_auth
from istota.config import Config, SiteConfig, UserConfig, WebConfig

PASSWORD = "a long example passphrase"


@pytest.fixture(autouse=True)
def _cheap_password_hash(monkeypatch):
    # The shipped scrypt cost is ~0.1s a hash; nothing here is about the cost.
    monkeypatch.setattr(web_auth, "_N", 1024)
    monkeypatch.setattr(web_auth, "DUMMY_HASH", web_auth.hash_password("unused"))


@pytest.fixture
def configured(db_path, monkeypatch):
    from istota.webui import app as mod

    config = Config(db_path=db_path, site=SiteConfig(hostname="example.com"),
                    web=WebConfig(auth=["email", "nextcloud"]),
                    users={"alice": UserConfig(display_name="Alice")},
                    admin_users={"alice"})
    monkeypatch.setattr(mod, "_config", config)
    monkeypatch.setattr(mod.app.state, "istota_config", config, raising=False)
    oauth = MagicMock()
    oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": "alice"})
    monkeypatch.setattr(mod, "_oauth", oauth)
    user_profiles.ensure_profile(db_path, "alice", display_name="Alice")
    web_auth.upsert_identity(db_path, "alice", "alice@example.com")
    web_auth.set_password(db_path, "alice", PASSWORD)
    return mod


@pytest.fixture
async def client(configured):
    async with AsyncClient(transport=ASGITransport(app=configured.app),
                           base_url="https://example.com") as client:
        yield client


def csrf(page, action="/istota/login/email"):
    form = re.search(r'<form[^>]*action="' + re.escape(action) + r'"[^>]*>(.*?)</form>', page.text, re.S)
    assert form, page.text
    return re.search(r'name="csrf_token" value="([^"]+)"', form[1])[1]


async def sign_in(client, email="alice@example.com", password=PASSWORD):
    page = await client.get("/istota/login")
    return await client.post("/istota/login/email", data={
        "email": email, "password": password, "csrf_token": csrf(page),
    })


def set_session(client, mod, session):
    secret = next(m.kwargs["secret_key"] for m in mod.app.user_middleware
                  if m.cls.__name__ == "SessionMiddleware")
    value = TimestampSigner(str(secret)).sign(base64.b64encode(json.dumps(session).encode())).decode()
    client.cookies.set("istota_session", value, domain="example.com", path="/istota/")


def session_value(client):
    value = client.cookies.get("istota_session")
    return json.loads(base64.b64decode(value.split(".")[0]))


@pytest.mark.parametrize("methods", [["email"], ["nextcloud"], ["email", "nextcloud"]])
async def test_login_choices(client, configured, methods):
    configured._config.web.auth = methods
    if methods == ["email"]:
        configured._oauth = None
    page = await client.get("/istota/login")
    assert page.status_code == 200
    assert ('name="password"' in page.text) == ("email" in methods)
    assert ('action="/istota/auth/sign-in-code/request"' in page.text) == ("email" in methods)
    assert ('href="/istota/login?go=1"' in page.text) == ("nextcloud" in methods)
    assert ('class="divider"' in page.text) == (len(methods) == 2)
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"


async def test_email_login_groups_password_and_code_as_alternatives(client):
    page = await client.get("/istota/login")
    choices = re.search(r'<fieldset class="email-login">(.*?)</fieldset>', page.text, re.S)
    assert choices is not None
    assert '<legend class="visually-hidden">Sign in with email</legend>' in choices[1]
    radios = re.findall(r'<input[^>]*type="radio"[^>]*>', choices[1])
    assert len(radios) == 2
    assert all('name="email-method"' in radio for radio in radios)
    assert 'id="email-password" checked' in radios[0]
    assert 'id="email-code"' in radios[1] and "checked" not in radios[1]
    password = re.search(r'<div class="email-panel password-panel">(.*?)</form>', choices[1], re.S)
    code = re.search(r'<div class="email-panel code-panel">(.*?)</form>', choices[1], re.S)
    assert password and 'action="/istota/login/email"' in password[1]
    assert code and 'action="/istota/auth/sign-in-code/request"' in code[1]
    assert 'href="/istota/auth/reset"' in password[1]
    assert csrf(page) != csrf(page, "/istota/auth/sign-in-code/request")


async def test_email_login_uses_live_profile_and_rotates_session(client, configured):
    configured._config.users = {"bob": UserConfig()}
    set_session(client, configured, {"planted": "old"})
    response = await sign_in(client)
    assert response.status_code == 302
    assert "planted" not in session_value(client)
    me = await client.get("/istota/api/me")
    assert me.status_code == 200
    assert me.json()["username"] == "alice"
    assert me.json()["auth"] == {"method": "email", "email": "alice@example.com", "can_change_password": True}


@pytest.mark.parametrize("method,mutation", [
    (method, mutation) for method in ("email", "nextcloud")
    for mutation in ("epoch", "delete", "disable", "method", "orphan")
    if (method, mutation) != ("nextcloud", "orphan")
])
async def test_session_revocation(client, configured, mutation, method):
    response = await sign_in(client) if method == "email" else await client.get("/istota/callback")
    assert response.status_code == 302
    path = configured._config.db_path
    if mutation == "epoch":
        web_auth.bump_epoch(path, "alice")
    elif mutation == "delete":
        web_auth.delete_identity(path, "alice")
    elif mutation == "disable":
        web_auth.set_disabled(path, "alice", True)
    elif mutation == "method":
        configured._config.web.auth = ["nextcloud" if method == "email" else "email"]
    elif mutation == "orphan":
        with db.get_db(path) as conn:
            conn.execute("DELETE FROM user_profiles WHERE user_id = 'alice'")
    assert (await client.get("/istota/api/me")).status_code == 401


async def test_failures_have_identical_bodies_and_orphans_stay_orphaned(client, configured):
    wrong = await sign_in(client, password="wrong")
    unknown = await sign_in(client, email="unknown@example.com")
    web_auth.set_disabled(configured._config.db_path, "alice", True)
    disabled = await sign_in(client)
    assert wrong.status_code == unknown.status_code == disabled.status_code == 400
    assert wrong.content == unknown.content == disabled.content
    web_auth.set_disabled(configured._config.db_path, "alice", False)
    with db.get_db(configured._config.db_path) as conn:
        conn.execute("DELETE FROM user_profiles WHERE user_id = 'alice'")
    orphan = await sign_in(client)
    assert orphan.content == wrong.content
    assert user_profiles.get_profile(configured._config.db_path, "alice") is None


@pytest.mark.parametrize("submitted", [None, "wrong"])
async def test_csrf_failure_is_distinct(client, submitted):
    await client.get("/istota/login")
    data = {"email": "alice@example.com", "password": PASSWORD}
    if submitted:
        data["csrf_token"] = submitted
    response = await client.post("/istota/login/email", data=data)
    assert response.status_code == 403
    assert "form expired" in response.text
    assert "user" not in session_value(client)


@pytest.mark.parametrize("attached,enabled", [(False, True), (True, True), (False, False)])
async def test_legacy_cookie_rule(client, configured, attached, enabled):
    username = "alice" if attached else "bob"
    if not attached:
        user_profiles.ensure_profile(configured._config.db_path, username)
    if not enabled:
        configured._config.web.auth = ["email"]
    set_session(client, configured, {"user": {"username": username}})
    response = await client.get("/istota/api/me")
    assert response.status_code == (200 if not attached and enabled else 401)
    if response.status_code == 200:
        assert session_value(client)["auth"] == {"method": "nextcloud", "epoch": 0}
        web_auth.upsert_identity(configured._config.db_path, username, "bob@example.com")
        assert (await client.get("/istota/api/me")).status_code == 401


async def test_nextcloud_disabled_and_reconnect_revoked(client, configured):
    assert (await client.get("/istota/callback")).status_code == 302
    web_auth.set_disabled(configured._config.db_path, "alice", True)
    assert (await client.get("/istota/callback")).status_code == 403
    response = await client.get("/istota/reconnect")
    assert response.status_code in (302, 401)
    if response.status_code == 302:
        assert response.headers["location"] == "/istota/login"
    configured._oauth.nextcloud.authorize_redirect.assert_not_called()


async def test_session_database_failure_denies_access(client, configured, monkeypatch):
    assert (await sign_in(client)).status_code == 302
    def unavailable(*args):
        raise sqlite3.OperationalError("unavailable")
    monkeypatch.setattr(web_auth, "get_identity", unavailable)
    assert (await client.get("/istota/api/me")).status_code == 401


def test_csrf_purposes_are_independent(configured):
    request = Request({"type": "http", "session": {}})
    original = configured._csrf_token(request, "login")
    reset = configured._csrf_token(request, "reset")
    assert original != reset
    assert configured._check_csrf(request, "login", original)
    assert not configured._check_csrf(request, "reset", original)


@pytest.mark.parametrize("hops,forwarded,expected", [
    (0, "192.0.2.1", None), (0, "", None),
    (1, "192.0.2.1, 192.0.2.2", "192.0.2.2"),
    (2, "192.0.2.1, 192.0.2.2", "192.0.2.1"),
    (3, "192.0.2.1", None), (1, "invalid", None),
])
def test_client_ip_uses_only_configured_proxy_hop(configured, hops, forwarded, expected):
    configured._config.web.trusted_proxy_hops = hops
    request = Request({"type": "http", "headers": [(b"x-forwarded-for", forwarded.encode())],
                       "client": ("192.0.2.9", 1234)})
    assert configured._client_ip(request) == expected


async def test_email_route_disabled(client, configured):
    configured._config.web.auth = ["nextcloud"]
    assert (await client.post("/istota/login/email")).status_code == 404


@pytest.mark.parametrize("stream", ["task", "room", "logs", "pairing"])
@pytest.mark.parametrize("mutation", ["epoch", "disable", "admin"])
async def test_open_stream_revocation(client, configured, monkeypatch, stream, mutation):
    import asyncio

    if mutation == "admin" and stream in ("task", "room"):
        pytest.skip("User streams do not require admin")
    assert (await sign_in(client)).status_code == 302
    request = Request({"type": "http", "headers": [], "session": session_value(client)})
    request.is_disconnected = AsyncMock(return_value=False)
    user = configured._require_api_auth(request)
    monkeypatch.setattr(configured.web_shutdown, "sleep_unless_shutdown", AsyncMock(return_value=True))
    path = configured._config.db_path
    with db.get_db(path) as conn:
        room = db.create_web_chat_room(conn, "alice", "general")
        task = db.create_task(conn, "hello", "alice", source_type="web", conversation_token=room.token)
        db.add_message(conn, room.token, role="assistant", body="first", origin_surface="web")
        db.add_message(conn, room.token, role="assistant", body="second", origin_surface="web")
        conn.execute("INSERT INTO task_events(task_id, seq, kind, payload) VALUES (?, 1, 'text', '{}')", (task,))
        conn.execute("INSERT INTO task_events(task_id, seq, kind, payload) VALUES (?, 2, 'text', '{}')", (task,))
        db.log_task(conn, task, "info", "first")
    if stream == "task":
        response = await configured.chat_task_stream(task, request, user=user)
    elif stream == "room":
        response = await configured.chat_room_stream(request, user=user)
    elif stream == "logs":
        response = await configured.admin_log_stream("tasks", request, "0", logger_name=None, _=user)
    else:
        # The sidecar is the external boundary; the generator and its auth are real.
        monkeypatch.setattr(configured, "_require_whatsapp_pairing", lambda: None)
        monkeypatch.setattr(configured, "_pairing_state_payload", lambda: None)
        response = await configured.admin_whatsapp_pairing_stream(request, _=user)
    iterator = response.body_iterator
    first = await asyncio.wait_for(anext(iterator), 2)
    assert "data:" in first
    if mutation == "epoch":
        web_auth.bump_epoch(path, "alice")
    elif mutation == "disable":
        web_auth.set_disabled(path, "alice", True)
    else:
        configured._config.admin_users = set()
    # In task/room streams the second frame was already loaded in the batch.
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(iterator), 2)


async def test_password_gate_refuses_without_database_work_and_delays(client, configured, monkeypatch):
    import time

    configured._config.web.auth_throttle_max_email = 1
    first = await sign_in(client, password="wrong")
    with db.get_db(configured._config.db_path) as conn:
        before = conn.execute("SELECT count(*) FROM web_auth_attempts").fetchone()[0]
    verify = MagicMock(side_effect=AssertionError("throttle reached KDF"))
    monkeypatch.setattr(web_auth, "verify_password", verify)
    started = time.monotonic()
    refused = await sign_in(client, password="wrong")
    assert time.monotonic() - started >= configured._LOGIN_FAILURE_SECONDS
    assert refused.status_code == first.status_code
    assert refused.content == first.content
    verify.assert_not_called()
    with db.get_db(configured._config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM web_auth_attempts").fetchone()[0] == before


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("operation", ["login", "set-password"])
async def test_password_work_is_bounded_off_event_loop(client, configured, monkeypatch, cancel, operation):
    import asyncio
    import threading

    page = await client.get("/istota/login")
    if operation == "login":
        route = "/istota/login/email"
        data = {"email": "alice@example.com", "password": "wrong", "csrf_token": csrf(page)}
    else:
        route = "/istota/auth/set-password"
        token = web_auth.issue_token(configured._config.db_path, "alice", "enrol", 3600)
        page = await client.get(route, params={"token": token})
        data = {"token": token, "password": PASSWORD, "confirm_password": PASSWORD, "csrf_token": csrf(page, route)}
    loop_thread = threading.get_ident()
    active = 0
    peak = 0
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    monkeypatch.setattr(configured, "_password_slots", asyncio.Semaphore(4))

    def authenticate(*args, **kwargs):
        nonlocal active, peak
        assert threading.get_ident() != loop_thread
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 4:
                entered.set()
        assert release.wait(5)
        with lock:
            active -= 1
        return ("bad", None) if operation == "login" else None

    target = "authenticate" if operation == "login" else "consume_and_set_password"
    monkeypatch.setattr(web_auth, target, authenticate)
    requests = [asyncio.create_task(client.post(route, data=data)) for _ in range(8)]
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        if cancel:
            for request in requests[:4]:
                request.cancel()
        await asyncio.sleep(0.05)  # The loop stays responsive, even during cancellation.
        assert peak == 4
    finally:
        release.set()
        responses = await asyncio.gather(*requests, return_exceptions=True)
    assert all(isinstance(response, asyncio.CancelledError) or response.status_code == 400
               for response in responses)
    assert peak == 4


@pytest.mark.parametrize("method,password_set", [("email", True), ("email", False), ("nextcloud", True)])
async def test_me_reports_password_capability(client, configured, method, password_set):
    if not password_set:
        web_auth.clear_password(configured._config.db_path, "alice")
    identity = web_auth.get_identity(configured._config.db_path, "alice")
    set_session(client, configured, {"user": {"username": "alice"},
                                    "auth": {"method": method, "epoch": identity.credential_epoch}})
    response = await client.get("/istota/api/me")
    assert response.status_code == 200
    assert response.json()["auth"] == {"method": method, "email": "alice@example.com",
                                       "can_change_password": method == "email" and password_set}


async def test_callback_database_failure_does_not_mint_session(client, configured, monkeypatch):
    def unavailable(*args):
        raise sqlite3.OperationalError("unavailable")
    monkeypatch.setattr(web_auth, "get_identity", unavailable)
    response = await client.get("/istota/callback")
    assert response.status_code == 403
    assert (await client.get("/istota/api/me")).status_code == 401


@pytest.mark.parametrize("purpose", ["enrol", "reset"])
async def test_link_get_is_read_only_and_post_rotates_session(client, configured, purpose):
    path = configured._config.db_path
    before = web_auth.get_identity(path, "alice")
    token = web_auth.issue_token(path, "alice", purpose, 3600)
    route = "/istota/auth/set-password"
    set_session(client, configured, {"planted": "old"})
    page = await client.get(route, params={"token": token})
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert web_auth.peek_token(path, token) is not None
    assert (await client.get("/istota/api/me")).status_code == 401
    data = {"token": token, "csrf_token": csrf(page, route),
            "password": "a different example passphrase", "confirm_password": "a different example passphrase"}
    # The scanner's cookie is not needed: the browser obtains its own form.
    page = await client.get(route, params={"token": token})
    data["csrf_token"] = csrf(page, route)
    response = await client.post(route, data=data)
    assert response.status_code == 302
    assert response.headers["location"] == "/istota/"
    session = session_value(client)
    assert "planted" not in session
    assert session["user"] == {"username": "alice", "display_name": "Alice"}
    assert session["auth"] == {"method": "email", "epoch": before.credential_epoch + 1}
    assert (await client.get("/istota/api/me")).status_code == 200
    assert web_auth.peek_token(path, token) is None
    replay = await client.post(route, data=data)
    assert replay.status_code == 400 and "link is invalid" in replay.text


async def test_login_csrf_survives_reset_page(client):
    login = await client.get("/istota/login")
    assert (await client.get("/istota/auth/reset")).status_code == 200
    response = await client.post("/istota/login/email", data={
        "email": "alice@example.com", "password": PASSWORD, "csrf_token": csrf(login),
    })
    assert response.status_code == 302


@pytest.mark.parametrize("method,route", [
    ("GET", "/auth/set-password"), ("POST", "/auth/set-password"),
    ("GET", "/auth/reset"), ("POST", "/auth/reset"),
    ("POST", "/auth/sign-in-code/request"), ("GET", "/auth/sign-in-code"), ("POST", "/auth/sign-in-code"),
])
async def test_all_link_routes_disabled(client, configured, method, route):
    configured._config.web.auth = ["nextcloud"]
    assert (await client.request(method, "/istota" + route)).status_code == 404


@pytest.mark.parametrize("purpose", ["enrol", "reset"])
@pytest.mark.parametrize("mutation", ["expired", "disabled", "email", "readded", "orphan"])
async def test_invalid_links_have_one_card(client, configured, purpose, mutation):
    path = configured._config.db_path
    token = web_auth.issue_token(path, "alice", purpose, 3600)
    if mutation == "expired":
        with db.get_db(path) as conn:
            conn.execute("UPDATE web_auth_tokens SET expires_at=datetime('now', '-1 second')")
    elif mutation == "disabled":
        web_auth.set_disabled(path, "alice", True)
    elif mutation == "email":
        web_auth.upsert_identity(path, "alice", "other@example.com")
    elif mutation == "readded":
        web_auth.delete_identity(path, "alice")
        web_auth.upsert_identity(path, "alice", "alice@example.com")
    elif mutation == "orphan":
        user_profiles.delete_profile(path, "alice")
    route = "/istota/auth/set-password"
    invalid = await client.get(route, params={"token": "invalid"})
    response = await client.get(route, params={"token": token})
    post = await client.post(route, data={"token": token, "password": PASSWORD})
    assert response.status_code == post.status_code == invalid.status_code == 400
    assert response.content == post.content == invalid.content
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize("route,purpose", [("set-password", "enrol"), ("set-password", "reset")])
async def test_token_post_requires_csrf(client, configured, route, purpose):
    token = web_auth.issue_token(configured._config.db_path, "alice", purpose, 3600)
    response = await client.post("/istota/auth/" + route, data={"token": token, "password": PASSWORD})
    assert response.status_code == 403 and "form expired" in response.text
    assert web_auth.peek_token(configured._config.db_path, token) is not None


async def test_password_policy_and_confirmation_leave_token_usable(client, configured):
    token = web_auth.issue_token(configured._config.db_path, "alice", "enrol", 3600)
    route = "/istota/auth/set-password"
    page = await client.get(route, params={"token": token})
    for password, confirm, message in [("short", "short", "at least"), (PASSWORD, "different", "match")]:
        response = await client.post(route, data={"token": token, "csrf_token": csrf(page, route),
                                                 "password": password, "confirm_password": confirm})
        assert response.status_code == 400 and message in response.text
        assert web_auth.peek_token(configured._config.db_path, token) is not None
        assert PASSWORD not in response.text


async def mail_request(client, purpose, email="alice@example.com"):
    route = "/istota/auth/reset" if purpose == "reset" else "/istota/auth/sign-in-code/request"
    page = await client.get("/istota/auth/reset" if purpose == "reset" else "/istota/login")
    return await client.post(route, data={"email": email, "csrf_token": csrf(page, route)})


async def test_mail_requests_share_budget_and_delivered_codes_work(client, configured, monkeypatch):
    from istota.skills import email as mail

    configured._config.email.enabled = True
    sent = MagicMock()
    monkeypatch.setattr(mail, "send_email", sent)
    responses = [await mail_request(client, purpose) for purpose in ("reset", "reset", "login", "login")]
    assert sent.call_count == 3
    assert responses[2].content == responses[3].content
    assert all(response.status_code == 200 for response in responses)
    for response in responses:
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
    # The third mail is the code for the first code request. The fourth request
    # was over budget and sent nothing, and must not have retired that code.
    code = re.search(r"^(\d{6})$", sent.call_args.kwargs["body"], re.M)[1]
    response = await client.post("/istota/auth/sign-in-code", data={
        "code": code, "csrf_token": csrf(responses[3], "/istota/auth/sign-in-code"),
    })
    assert response.status_code == 302
    assert (await client.get("/istota/api/me")).status_code == 200


@pytest.mark.parametrize("purpose", ["reset", "login"])
async def test_mail_request_responses_do_not_disclose_identity(client, configured, monkeypatch, purpose):
    from istota.webui import auth_mail as web_auth_mail

    send = MagicMock(return_value=True)
    monkeypatch.setattr(web_auth_mail, "send_auth_email", send)
    known = await mail_request(client, purpose)
    unknown = await mail_request(client, purpose, "unknown@example.com")
    web_auth.set_disabled(configured._config.db_path, "alice", True)
    disabled = await mail_request(client, purpose)
    assert known.content == unknown.content == disabled.content
    assert known.status_code == unknown.status_code == disabled.status_code == 200
    assert send.call_count == 1


@pytest.mark.parametrize("purpose", ["reset", "login"])
async def test_mail_csrf_refusal_does_no_work(client, configured, monkeypatch, purpose):
    issue, start = MagicMock(), MagicMock()
    monkeypatch.setattr(web_auth, "issue_mail_link_if_allowed", issue)
    monkeypatch.setattr(web_auth, "start_sign_in", start)
    route = "/istota/auth/reset" if purpose == "reset" else "/istota/auth/sign-in-code/request"
    response = await client.post(route, data={"email": "alice@example.com"})
    assert response.status_code == 403 and "form expired" in response.text
    issue.assert_not_called()
    start.assert_not_called()


@pytest.mark.parametrize("state", ["known", "unknown", "disabled"])
async def test_mail_lookup_runs_after_response_and_pending_addresses_coalesce(client, configured, monkeypatch, state):
    import asyncio
    import threading
    from istota.webui import auth_mail as web_auth_mail

    if state == "unknown":
        web_auth.delete_identity(configured._config.db_path, "alice")
    elif state == "disabled":
        web_auth.set_disabled(configured._config.db_path, "alice", True)
    reset = await client.get("/istota/auth/reset")
    response_sent = asyncio.Event()
    entered = threading.Event()
    release = threading.Event()
    original = web_auth.issue_mail_link_if_allowed
    calls = []
    event_loop_thread = threading.get_ident()

    def issue(*args):
        assert threading.get_ident() != event_loop_thread
        assert response_sent.is_set(), "Identity lookup happened before the response body"
        calls.append(args[2])
        entered.set()
        assert release.wait(5)
        return original(*args)

    async def observed_app(scope, receive, send):
        async def observed_send(message):
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                response_sent.set()
        await configured.app(scope, receive, observed_send)

    send = MagicMock(return_value=True)
    monkeypatch.setattr(web_auth, "issue_mail_link_if_allowed", issue)
    monkeypatch.setattr(web_auth_mail, "send_auth_email", send)
    async with AsyncClient(transport=ASGITransport(app=observed_app), base_url="https://example.com",
                           cookies=client.cookies) as observer:
        first = asyncio.create_task(observer.post("/istota/auth/reset", data={
            "email": " ALICE@example.com ", "csrf_token": csrf(reset, "/istota/auth/reset"),
        }))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            second = await asyncio.wait_for(observer.post("/istota/auth/reset", data={
                "email": "alice@example.com", "csrf_token": csrf(reset, "/istota/auth/reset"),
            }), 2)
            assert second.status_code == 200
            assert calls == ["alice@example.com"]
        finally:
            release.set()
            first_response = await first
    assert first_response.content == second.content
    assert send.call_count == (state == "known")
    assert not configured._mail_link_pending


async def test_unknown_address_flood_does_not_spend_other_addresses_budget(client, configured, monkeypatch):
    from istota.webui import auth_mail as web_auth_mail

    send = MagicMock(return_value=True)
    monkeypatch.setattr(web_auth_mail, "send_auth_email", send)
    for index in range(12):
        assert (await mail_request(client, "login", f"unknown{index}@example.com")).status_code == 200
        assert (await mail_request(client, "reset", f"unknown{index}@example.com")).status_code == 200
    assert (await mail_request(client, "login")).status_code == 200
    assert send.call_count == 1
    with db.get_db(configured._config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM web_auth_attempts WHERE kind='mail_link'").fetchone()[0] == 1


async def test_background_failure_is_generic_and_releases_pending_address(client, configured, monkeypatch, caplog):
    original = web_auth.issue_mail_link_if_allowed
    issue = MagicMock(side_effect=RuntimeError("private-token alice@example.com"))
    monkeypatch.setattr(web_auth, "issue_mail_link_if_allowed", issue)
    failed = await mail_request(client, "reset")
    assert failed.status_code == 200
    assert not configured._mail_link_pending
    assert "private-token" not in caplog.text and "alice@example.com" not in caplog.text
    monkeypatch.setattr(web_auth, "issue_mail_link_if_allowed", original)
    configured._config.email.enabled = False
    disabled_mail = await mail_request(client, "reset")
    assert disabled_mail.content == failed.content
    assert not configured._mail_link_pending


async def test_token_password_write_failure_rolls_back_and_can_retry(client, configured):
    path = configured._config.db_path
    token = web_auth.issue_token(path, "alice", "reset", 3600)
    page = await client.get("/istota/auth/set-password", params={"token": token})
    data = {"token": token, "password": PASSWORD, "confirm_password": PASSWORD,
            "csrf_token": csrf(page, "/istota/auth/set-password")}
    with db.get_db(path) as conn:
        conn.execute("CREATE TRIGGER reject_password BEFORE UPDATE OF password_hash ON web_auth_identities BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    response = await client.post("/istota/auth/set-password", data=data)
    assert response.status_code == 400 and "link is invalid" in response.text
    assert web_auth.peek_token(path, token) is not None
    with db.get_db(path) as conn:
        conn.execute("DROP TRIGGER reject_password")
    assert (await client.post("/istota/auth/set-password", data=data)).status_code == 302


@pytest.mark.parametrize("purpose", ["enrol", "reset"])
async def test_token_database_failure_is_a_generic_card(client, configured, monkeypatch, purpose):
    token = web_auth.issue_token(configured._config.db_path, "alice", purpose, 3600)
    def unavailable(*args):
        raise sqlite3.OperationalError("private-token alice@example.com")
    monkeypatch.setattr(web_auth, "peek_token", unavailable)
    route = "/istota/auth/set-password"
    response = await client.get(route, params={"token": token})
    assert response.status_code == 400 and "link is invalid" in response.text
    assert "private-token" not in response.text


async def test_account_password_revokes_both_clients(client, configured):
    async with AsyncClient(transport=ASGITransport(app=configured.app),
                           base_url="https://example.com") as other:
        assert (await sign_in(client)).status_code == 302
        assert (await sign_in(other)).status_code == 302
        path = configured._config.db_path
        before = web_auth.get_identity(path, "alice")
        payload = {"current_password": "wrong", "new_password": PASSWORD + " changed"}
        response = await client.post("/istota/api/account/password", json=payload,
                                     headers={"Origin": "https://example.com"})
        assert response.status_code == 400
        assert web_auth.get_identity(path, "alice") == before
        payload["current_password"] = PASSWORD
        response = await client.post("/istota/api/account/password", json=payload,
                                     headers={"Origin": "https://example.com"})
        assert response.status_code == 200
        assert response.json() == {"signed_out": True}
        after = web_auth.get_identity(path, "alice")
        assert after.credential_epoch > before.credential_epoch
        assert web_auth.verify_password(payload["new_password"], after.password_hash)[0]
        assert (await client.get("/istota/api/me")).status_code == 401
        assert (await other.get("/istota/api/me")).status_code == 401


@pytest.mark.parametrize("case", ["anonymous", "nextcloud", "origin", "passwordless", "disabled-method", "policy", "malformed"])
async def test_account_password_guards(client, configured, case):
    if case == "passwordless":
        web_auth.clear_password(configured._config.db_path, "alice")
        identity = web_auth.get_identity(configured._config.db_path, "alice")
        set_session(client, configured, {"user": {"username": "alice"},
                    "auth": {"method": "email", "epoch": identity.credential_epoch}})
    elif case == "nextcloud":
        await client.get("/istota/callback")
    elif case != "anonymous":
        await sign_in(client)
    if case == "disabled-method":
        configured._config.web.auth = ["nextcloud"]
    before = web_auth.get_identity(configured._config.db_path, "alice")
    payload = {"current_password": PASSWORD, "new_password": "short" if case == "policy" else PASSWORD + " changed"}
    response = await client.post("/istota/api/account/password",
        json=[] if case == "malformed" else payload,
        headers={} if case == "origin" else {"Origin": "https://example.com"})
    expected = {"anonymous": 401, "nextcloud": 403, "origin": 403, "passwordless": 400,
                "disabled-method": 404, "policy": 400, "malformed": 400}
    assert response.status_code == expected[case]
    assert web_auth.get_identity(configured._config.db_path, "alice") == before


@pytest.mark.parametrize("ingress", [True, False])
async def test_account_password_throttle_skips_kdf(client, configured, monkeypatch, ingress):
    await sign_in(client)
    configured._config.web.auth_throttle_max_email = 1
    if not ingress:
        configured._login_ingress.clear()
    from unittest.mock import Mock
    verify = Mock(side_effect=AssertionError("Throttled request ran scrypt"))
    monkeypatch.setattr(web_auth, "verify_password", verify)
    response = await client.post("/istota/api/account/password",
        json={"current_password": PASSWORD, "new_password": PASSWORD + " changed"},
        headers={"Origin": "https://example.com"})
    assert response.status_code == 400
    verify.assert_not_called()


async def test_account_password_database_failure_is_closed(client, configured, monkeypatch):
    await sign_in(client)
    before = web_auth.get_identity(configured._config.db_path, "alice")

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("unavailable")

    monkeypatch.setattr(web_auth, "check_and_record", unavailable)
    response = await client.post("/istota/api/account/password",
        json={"current_password": PASSWORD, "new_password": PASSWORD + " changed"},
        headers={"Origin": "https://example.com"})
    assert response.status_code == 503
    assert web_auth.get_identity(configured._config.db_path, "alice") == before


@pytest.mark.parametrize("legacy", [False, True])
async def test_removed_identity_never_revives_older_nextcloud_cookie(client, configured, legacy):
    path = configured._config.db_path
    user_profiles.ensure_profile(path, "bob")
    configured._config.users["bob"] = UserConfig()
    original = {"user": {"username": "bob"}}
    if not legacy:
        original["auth"] = {"method": "nextcloud", "epoch": 0}
    set_session(client, configured, original)
    assert (await client.get("/istota/api/me")).status_code == 200
    web_auth.upsert_identity(path, "bob", "bob@example.com")
    for mutation in (lambda: None, lambda: web_auth.bump_epoch(path, "bob"),
                     lambda: web_auth.delete_identity(path, "bob")):
        mutation()
        set_session(client, configured, original)
        assert (await client.get("/istota/api/me")).status_code == 401
    db.init_db(path)
    set_session(client, configured, original)
    assert (await client.get("/istota/api/me")).status_code == 401
    configured._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": "bob"})
    assert (await client.get("/istota/callback")).status_code == 302
    assert (await client.get("/istota/api/me")).status_code == 200
    assert session_value(client)["auth"]["epoch"] != 0
    assert not web_auth.delete_identity(path, "bob")
    assert (await client.get("/istota/api/me")).status_code == 200
    web_auth.upsert_identity(path, "bob", "bob@example.com")
    web_auth.delete_identity(path, "bob")
    assert (await client.get("/istota/api/me")).status_code == 401


@pytest.mark.parametrize("route", ["connect", "callback"])
@pytest.mark.parametrize("revocation", ["disabled", "epoch", "method"])
async def test_google_routes_refuse_revoked_session(client, configured, monkeypatch, route, revocation):
    from starlette.responses import RedirectResponse
    from unittest.mock import Mock

    assert (await sign_in(client)).status_code == 302
    google = configured._oauth.google
    google.authorize_redirect = AsyncMock(return_value=RedirectResponse("https://accounts.example.com"))
    google.authorize_access_token = AsyncMock(return_value={"access_token": "test-access", "refresh_token": "test-refresh"})
    monkeypatch.setattr(configured, "_google_requested_scopes", lambda user: ["test-scope"])
    write = Mock()
    monkeypatch.setattr(db, "upsert_google_token", write)
    if revocation == "disabled":
        web_auth.set_disabled(configured._config.db_path, "alice", True)
    elif revocation == "epoch":
        web_auth.bump_epoch(configured._config.db_path, "alice")
    else:
        configured._config.web.auth = ["nextcloud"]
    response = await client.get("/istota/google/" + route)
    assert response.status_code == 302 and response.headers["location"] == "/istota/login"
    google.authorize_redirect.assert_not_awaited()
    google.authorize_access_token.assert_not_awaited()
    write.assert_not_called()


async def test_google_callback_rechecks_after_token_exchange(client, configured, monkeypatch):
    from unittest.mock import Mock

    assert (await sign_in(client)).status_code == 302
    async def exchange(request):
        web_auth.bump_epoch(configured._config.db_path, "alice")
        return {"access_token": "test-access", "refresh_token": "test-refresh"}
    configured._oauth.google.authorize_access_token = AsyncMock(side_effect=exchange)
    write = Mock()
    monkeypatch.setattr(db, "upsert_google_token", write)
    response = await client.get("/istota/google/callback")
    assert response.status_code == 302 and response.headers["location"] == "/istota/login"
    write.assert_not_called()


async def test_recovery_codes_need_the_password_again_on_an_email_session(client, configured, monkeypatch):
    """ISSUE-688: the one web path that returns a stored value asks for the password."""
    from istota.credentials import generated

    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    path = configured._config.db_path
    with db.get_db(path) as conn:
        generated.create(conn, "alice", name="generated_acme", username="alice@example.com",
                         password="fixture-password", url="https://acme.example", mirror=False)
        generated.set_recovery(conn, "alice", "generated_acme", "fixture-rc-1111-aaaa")
    assert (await sign_in(client)).status_code == 302
    url = "/istota/api/settings/credentials/generated_acme/recovery"
    origin = {"Origin": "https://example.com"}
    for body in ({"confirm": True}, {"confirm": True, "password": "wrong password"}):
        refused = await client.post(url, json=body, headers=origin)
        assert refused.status_code == 403
        assert refused.json()["field"] == "password"
        assert "fixture-rc" not in refused.text
    response = await client.post(url, json={"confirm": True, "password": PASSWORD}, headers=origin)
    assert response.status_code == 200, response.text
    assert response.json() == {"codes": "fixture-rc-1111-aaaa"}
