"""The admin browser console: the ticket store, the relay counter, the
ticket route and the websocket relay.

The route runs through the FastAPI app with a real database and a password
step-up. The relay runs through Starlette's TestClient against the real app,
with a threaded Unix-socket server standing in for x11vnc: the TestClient runs
the app's loop in its own thread, so a server on the test's loop would never
be serviced.
"""

import logging
import queue
import shutil
import socket
import socketserver
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("authlib")
pytest.importorskip("fastapi")

from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

from istota import user_profiles
from istota.lib.browser_console import console_socket_name
from istota.config import Config, SiteConfig, UserConfig, WebConfig
from istota.webui import admin_browser_console as console
from istota.webui import auth as web_auth
from tests.test_web_auth_routes import (  # noqa: F401 -- fixtures
    PASSWORD, _cheap_password_hash, session_value,
)

ORIGIN = {"Origin": "https://example.com"}
TICKET = "/istota/api/admin/browsers/console-ticket"
STEP_UP = "/istota/api/settings/step-up"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# ---- TicketStore ----


def test_a_ticket_redeems_once_for_its_target():
    store = console.TicketStore()
    ticket = store.mint("alice", "s1", "bob")
    assert store.redeem(ticket, "alice", "s1") == "bob"
    assert store.redeem(ticket, "alice", "s1") is None


def test_a_ticket_is_long_and_url_safe():
    ticket = console.TicketStore().mint("alice", "s1", "bob")
    assert len(ticket) >= 43
    assert set(ticket) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


@pytest.mark.parametrize("admin, session_id", [("dave", "s1"), ("alice", "s2")])
def test_a_ticket_from_another_admin_or_session_is_refused_and_deleted(admin, session_id):
    store = console.TicketStore()
    ticket = store.mint("alice", "s1", "bob")
    assert store.redeem(ticket, admin, session_id) is None
    assert store.redeem(ticket, "alice", "s1") is None


def test_an_expired_ticket_is_refused():
    clock = Clock()
    store = console.TicketStore(clock=clock)
    ticket = store.mint("alice", "s1", "bob")
    clock.now += console.TICKET_TTL_SECONDS - 0.5
    fresh = store.mint("alice", "s1", "carol")
    clock.now += 1
    assert store.redeem(ticket, "alice", "s1") is None
    assert store.redeem(fresh, "alice", "s1") == "carol"


def test_an_unknown_ticket_is_refused():
    assert console.TicketStore().redeem("nothing-minted", "alice", "s1") is None


def test_the_oldest_ticket_is_dropped_past_the_cap():
    store = console.TicketStore()
    tickets = [store.mint("alice", "s1", f"user{i}") for i in range(console.MAX_TICKETS + 1)]
    assert store.redeem(tickets[0], "alice", "s1") is None
    assert store.redeem(tickets[1], "alice", "s1") == "user1"
    assert store.redeem(tickets[-1], "alice", "s1") == f"user{console.MAX_TICKETS}"


# ---- RelayCounter ----


def test_the_relay_counter_holds_the_cap():
    relays = console.RelayCounter()
    assert console.MAX_RELAYS == 8
    for _ in range(console.MAX_RELAYS):
        assert not relays.full()
        assert relays.try_acquire()
    assert relays.full()
    assert not relays.try_acquire()
    relays.release()
    assert not relays.full()
    assert relays.try_acquire()


def test_a_release_never_takes_the_count_below_zero():
    relays = console.RelayCounter()
    relays.release()
    for _ in range(console.MAX_RELAYS):
        assert relays.try_acquire()
    assert not relays.try_acquire()


def test_the_close_codes_are_the_spec_table():
    assert {code for code in console.CLOSE_CODES} == {1000, 1011, 4400, 4401, 4403, 4404, 4408, 4429}
    assert [code for code, row in console.CLOSE_CODES.items() if not row.retry] == [4401, 4403, 4404]
    assert console.CLOSE_FORBIDDEN == 4403 and console.CLOSE_TOO_MANY == 4429


# ---- The ticket route ----


@pytest.fixture
def setup(db_path, monkeypatch):
    import istota.webui.app as mod

    config = Config(
        db_path=db_path,
        site=SiteConfig(hostname="example.com"),
        users={"alice": UserConfig(display_name="Alice"), "bob": UserConfig(), "dave": UserConfig()},
        web=WebConfig(auth=["email", "nextcloud"]),
    )
    config.admin_users = {"alice", "dave"}
    config.browser.enabled = True
    for user_id in ("alice", "bob"):
        user_profiles.ensure_profile(db_path, user_id, display_name=user_id.title())
        web_auth.upsert_identity(db_path, user_id, f"{user_id}@example.com")
    web_auth.set_password(db_path, "alice", PASSWORD)
    monkeypatch.setattr(mod, "_config", config)
    monkeypatch.setattr(mod.app.state, "istota_config", config, raising=False)
    monkeypatch.setattr(mod, "_oauth", MagicMock())
    monkeypatch.setattr(console, "TICKETS", console.TicketStore())
    monkeypatch.setattr(console, "RELAYS", console.RelayCounter())
    return config


async def _client(user_id):
    import istota.webui.app as mod

    client = AsyncClient(transport=ASGITransport(app=mod.app), base_url="https://example.com")
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": user_id})
    await client.get("/istota/callback", follow_redirects=False)
    return client


@pytest.fixture
async def alice(setup):
    client = await _client("alice")
    yield client
    await client.aclose()


async def _proof(client) -> dict:
    response = await client.post(STEP_UP, json={"action": "admin_browser_console"}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["method"] == "password"
    return {"request_id": body["request_id"], "password": PASSWORD}


async def _assert_proof_still_redeems(client, proof):
    response = await client.post(TICKET, json={"user": "bob", "step_up": proof}, headers=ORIGIN)
    assert response.status_code == 200, response.text


async def test_a_valid_request_returns_a_ticket_for_this_session(alice, caplog):
    proof = await _proof(alice)
    with caplog.at_level(logging.INFO, logger="istota.webui.admin_browser_console"):
        response = await alice.post(TICKET, json={"user": "bob", "step_up": proof}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["expires_in"] == 60
    assert set(body) == {"ticket", "expires_in"}
    session_id = session_value(alice)["session_id"]
    assert console.TICKETS.redeem(body["ticket"], "alice", session_id) == "bob"
    assert "browser_console_ticket admin='alice' user='bob'" in caplog.text
    assert body["ticket"] not in caplog.text


async def test_a_proof_is_spent_by_a_valid_request(alice):
    proof = await _proof(alice)
    await _assert_proof_still_redeems(alice, proof)
    again = await alice.post(TICKET, json={"user": "bob", "step_up": proof}, headers=ORIGIN)
    assert again.status_code == 403
    assert "ticket" not in again.json()


async def test_a_non_admin_is_refused(setup):
    client = await _client("bob")
    try:
        response = await client.post(TICKET, json={"user": "alice"}, headers=ORIGIN)
        assert response.status_code == 403
        assert "ticket" not in response.text
    finally:
        await client.aclose()


async def test_no_session_is_refused(setup):
    import istota.webui.app as mod

    async with AsyncClient(transport=ASGITransport(app=mod.app), base_url="https://example.com") as client:
        response = await client.post(TICKET, json={"user": "bob"}, headers=ORIGIN)
    assert response.status_code in (401, 403)


@pytest.mark.parametrize("headers", [{}, {"Origin": "https://evil.example"}])
async def test_a_bad_origin_is_refused_and_spends_no_proof(alice, headers):
    proof = await _proof(alice)
    response = await alice.post(TICKET, json={"user": "bob", "step_up": proof}, headers=headers)
    assert response.status_code == 403
    assert "ticket" not in response.text
    await _assert_proof_still_redeems(alice, proof)


@pytest.mark.parametrize("user", ["", None, 7, ["bob"], "x" * 257, "bob\nadmin", "bob\x00", "bob\x7f"])
async def test_a_malformed_user_is_400_and_spends_no_proof(alice, user):
    proof = await _proof(alice)
    response = await alice.post(TICKET, json={"user": user, "step_up": proof}, headers=ORIGIN)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_user"
    if isinstance(user, str) and user:
        assert user not in response.text
    await _assert_proof_still_redeems(alice, proof)


async def test_a_long_user_id_at_the_limit_and_a_space_are_accepted(alice):
    for user in ("x" * 256, "a user"):
        proof = await _proof(alice)
        response = await alice.post(TICKET, json={"user": user, "step_up": proof}, headers=ORIGIN)
        assert response.status_code == 200, response.text


@pytest.mark.parametrize("content", [b"not json", b"[1, 2]", b"\xff\xfe"])
async def test_a_body_that_is_not_a_json_object_is_400(alice, content):
    response = await alice.post(TICKET, content=content,
                                headers={**ORIGIN, "Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_body"


async def test_an_oversized_body_is_413_and_spends_no_proof(alice):
    proof = await _proof(alice)
    response = await alice.post(TICKET, json={"user": "bob", "step_up": proof, "pad": "x" * 5000},
                                headers=ORIGIN)
    assert response.status_code == 413
    assert response.json()["error"] == "too_large"
    await _assert_proof_still_redeems(alice, proof)


async def test_a_disabled_browser_is_404_and_spends_no_proof(alice, setup):
    proof = await _proof(alice)
    setup.browser.enabled = False
    response = await alice.post(TICKET, json={"user": "bob", "step_up": proof}, headers=ORIGIN)
    assert response.status_code == 404
    assert response.json()["error"] == "browser_disabled"
    setup.browser.enabled = True
    await _assert_proof_still_redeems(alice, proof)


async def test_an_admin_without_a_native_identity_is_refused(setup):
    client = await _client("dave")
    try:
        response = await client.post(TICKET, json={"user": "bob"}, headers=ORIGIN)
        assert response.status_code == 403
        assert response.json() == {
            "error": "admin_identity_required",
            "detail": "Opening a browser console needs an admin with a live email sign-in identity.",
        }
    finally:
        await client.aclose()


async def test_a_disabled_identity_is_refused(alice, setup):
    web_auth.set_disabled(setup.db_path, "alice", True)
    response = await alice.post(TICKET, json={"user": "bob"}, headers=ORIGIN)
    assert response.status_code in (401, 403)
    assert "ticket" not in response.text


async def test_a_full_relay_counter_is_429_and_spends_no_proof(alice):
    proof = await _proof(alice)
    for _ in range(console.MAX_RELAYS):
        assert console.RELAYS.try_acquire()
    response = await alice.post(TICKET, json={"user": "bob", "step_up": proof}, headers=ORIGIN)
    assert response.status_code == 429
    assert response.json()["error"] == "console_limit"
    assert len(console.TICKETS._tickets) == 0
    console.RELAYS.release()
    await _assert_proof_still_redeems(alice, proof)


async def test_a_missing_step_up_is_the_step_up_refusal(alice, setup):
    await _proof(alice)
    response = await alice.post(TICKET, json={"user": "bob"}, headers=ORIGIN)
    assert response.status_code == 403
    assert response.json()["field"] == "password"
    assert "reason" in response.json()
    assert len(console.TICKETS._tickets) == 0


async def test_a_wrong_password_is_the_step_up_refusal(alice):
    proof = await _proof(alice)
    response = await alice.post(TICKET, json={"user": "bob", "step_up": {**proof, "password": "not it"}},
                                headers=ORIGIN)
    assert response.status_code == 403
    assert response.json()["reason"] == "bad"
    assert len(console.TICKETS._tickets) == 0


# ---- The websocket relay ----

# Absolute: TestClient joins a relative websocket path onto ws://testserver,
# which the Secure session cookie for example.com does not reach.
CONSOLE = "wss://example.com/istota/api/admin/browsers/console"
BANNER = b"RFB 003.008\n"
LOGGER = "istota.webui.admin_browser_console"


class _FakeVnc(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path):
        self.received = queue.Queue()
        self.peers = queue.Queue()
        super().__init__(str(path), _FakeVncHandler)


class _FakeVncHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.peers.put(self.request)
        self.request.sendall(BANNER)
        while True:
            try:
                data = self.request.recv(65536)
            except OSError:
                return
            if not data:
                return
            self.server.received.put(data)


@pytest.fixture
def console_dir(monkeypatch):
    # tmp_path on macOS is past AF_UNIX's 104-byte limit.
    path = Path(tempfile.mkdtemp(prefix="bcon_", dir="/tmp"))
    monkeypatch.setattr(console, "CONSOLE_DIR", path)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def vnc(console_dir):
    server = _FakeVnc(console_dir / console_socket_name("bob"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _ws_client(user_id=None):
    import istota.webui.app as mod

    client = TestClient(mod.app, base_url="https://example.com")
    if user_id is not None:
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": user_id})
        client.get("/istota/callback", follow_redirects=False)
    return client


@pytest.fixture
def admin_ws(setup, console_dir):
    return _ws_client("alice")


def _ticket(client, admin="alice", target="bob"):
    return console.TICKETS.mint(admin, session_value(client)["session_id"], target)


def _closed_with(ws) -> int:
    """The close code, after any relayed binary frames; never a text frame."""
    while True:
        message = ws.receive()
        if message["type"] == "websocket.close":
            return message.get("code", 1000)
        assert message["type"] == "websocket.send", message
        assert message.get("text") is None, message


def _open(ws, client):
    ws.send_text(_ticket(client))
    assert ws.receive_text() == "ok"


def _wait_released():
    # The handler gives its slot back after the close frame has gone out.
    deadline = time.monotonic() + 5
    while console.RELAYS._open and time.monotonic() < deadline:
        time.sleep(0.02)
    return console.RELAYS._open == 0


def test_no_session_closes_4403_after_accept(setup, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        with _ws_client().websocket_connect(CONSOLE, headers=ORIGIN) as ws:
            assert _closed_with(ws) == 4403
    assert "browser_console_refused admin=- user=- code=4403" in caplog.text


def test_a_non_admin_closes_4403(setup, caplog):
    client = _ws_client("bob")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        with client.websocket_connect(CONSOLE, headers=ORIGIN) as ws:
            assert _closed_with(ws) == 4403
    assert "browser_console_refused admin='bob' user=- code=4403" in caplog.text


@pytest.mark.parametrize("headers", [{}, {"Origin": "https://evil.example"}])
def test_a_bad_origin_closes_4403(admin_ws, headers):
    with admin_ws.websocket_connect(CONSOLE, headers=headers) as ws:
        assert _closed_with(ws) == 4403


def test_no_ticket_frame_in_time_closes_4400(admin_ws, monkeypatch):
    monkeypatch.setattr(console, "TICKET_WAIT_SECONDS", 0.1)
    with admin_ws.websocket_connect(CONSOLE, headers=ORIGIN) as ws:
        assert _closed_with(ws) == 4400
    assert _wait_released()


@pytest.mark.parametrize("frame", [b"binary-ticket", "", "x" * 200])
def test_a_malformed_ticket_frame_closes_4400(admin_ws, frame):
    with admin_ws.websocket_connect(CONSOLE, headers=ORIGIN) as ws:
        if isinstance(frame, bytes):
            ws.send_bytes(frame)
        else:
            ws.send_text(frame)
        assert _closed_with(ws) == 4400


def test_an_unknown_ticket_closes_4401(admin_ws, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        with admin_ws.websocket_connect(CONSOLE, headers=ORIGIN) as ws:
            ws.send_text("not-a-ticket-anyone-minted")
            assert _closed_with(ws) == 4401
    assert "browser_console_refused admin='alice' user=- code=4401" in caplog.text


def test_a_ticket_for_another_session_closes_4401_and_is_spent(admin_ws):
    ticket = console.TICKETS.mint("alice", "another-session", "bob")
    with admin_ws.websocket_connect(CONSOLE, headers=ORIGIN) as ws:
        ws.send_text(ticket)
        assert _closed_with(ws) == 4401
    assert console.TICKETS.redeem(ticket, "alice", "another-session") is None


def test_a_used_ticket_closes_4401(admin_ws):
    ticket = _ticket(admin_ws)
    with admin_ws.websocket_connect(CONSOLE, headers=ORIGIN) as ws:
        ws.send_text(ticket)
        assert _closed_with(ws) == 4404
    with admin_ws.websocket_connect(CONSOLE, headers=ORIGIN) as ws:
        ws.send_text(ticket)
        assert _closed_with(ws) == 4401


def test_a_user_with_no_socket_closes_4404(admin_ws, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        with admin_ws.websocket_connect(CONSOLE, headers=ORIGIN) as ws:
            ws.send_text(_ticket(admin_ws))
            assert _closed_with(ws) == 4404
    assert "browser_console_refused admin='alice' user='bob' code=4404" in caplog.text
    assert _wait_released()


def test_a_symlink_at_the_socket_path_closes_4404(admin_ws, console_dir, vnc):
    # A live socket under another name; bob's name is a symlink to it.
    real = console_dir / "real.sock"
    (console_dir / console_socket_name("bob")).rename(real)
    (console_dir / console_socket_name("bob")).symlink_to(real)
    with admin_ws.websocket_connect(CONSOLE, headers=ORIGIN) as ws:
        ws.send_text(_ticket(admin_ws))
        assert _closed_with(ws) == 4404
    assert vnc.peers.empty()


def test_a_regular_file_at_the_socket_path_closes_4404(admin_ws, console_dir):
    (console_dir / console_socket_name("bob")).write_text("not a socket")
    with admin_ws.websocket_connect(CONSOLE, headers=ORIGIN) as ws:
        ws.send_text(_ticket(admin_ws))
        assert _closed_with(ws) == 4404


def test_a_valid_ticket_relays_bytes_both_ways(admin_ws, vnc, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        with admin_ws.websocket_connect(CONSOLE, headers=ORIGIN) as ws:
            _open(ws, admin_ws)
            assert ws.receive_bytes() == BANNER
            ws.send_bytes(b"RFB 003.008\n")
            assert vnc.received.get(timeout=5) == b"RFB 003.008\n"
            ws.send_bytes(b"\x01\x02")
            assert vnc.received.get(timeout=5) == b"\x01\x02"
            ws.close(code=1005)
        assert _wait_released()
    assert "browser_console_opened admin='alice' user='bob'" in caplog.text
    closed = [r.getMessage() for r in caplog.records if "browser_console_closed" in r.getMessage()]
    assert len(closed) == 1
    assert closed[0].startswith("browser_console_closed admin='alice' user='bob' seconds=")
    assert closed[0].endswith(" code=1005")
    assert _ticket(admin_ws) not in caplog.text


def test_an_open_relay_holds_a_slot(admin_ws, vnc, monkeypatch):
    monkeypatch.setattr(console, "MAX_RELAYS", 1)
    with admin_ws.websocket_connect(CONSOLE, headers=ORIGIN) as ws:
        _open(ws, admin_ws)
        assert console.RELAYS.full()
        ws.close(code=1005)
    assert _wait_released()


def test_the_vnc_server_ending_the_stream_closes_1000(admin_ws, vnc, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        with admin_ws.websocket_connect(CONSOLE, headers=ORIGIN) as ws:
            _open(ws, admin_ws)
            assert ws.receive_bytes() == BANNER
            vnc.peers.get(timeout=5).shutdown(socket.SHUT_RDWR)
            assert _closed_with(ws) == 1000
        assert _wait_released()
    assert "code=1000" in caplog.text


def test_a_full_counter_closes_4429_and_leaves_the_ticket(admin_ws, vnc, caplog):
    for _ in range(console.MAX_RELAYS):
        assert console.RELAYS.try_acquire()
    ticket = _ticket(admin_ws)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        with admin_ws.websocket_connect(CONSOLE, headers=ORIGIN) as ws:
            assert _closed_with(ws) == 4429
    assert "browser_console_refused admin='alice' user=- code=4429" in caplog.text
    console.RELAYS.release()
    with admin_ws.websocket_connect(CONSOLE, headers=ORIGIN) as ws:
        ws.send_text(ticket)
        assert ws.receive_text() == "ok"
        assert ws.receive_bytes() == BANNER
        ws.close(code=1005)


def test_a_failing_re_check_closes_4401(admin_ws, vnc, monkeypatch, caplog):
    import istota.webui.app as mod

    monkeypatch.setattr(console, "RECHECK_SECONDS", 0.1)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        with admin_ws.websocket_connect(CONSOLE, headers=ORIGIN) as ws:
            _open(ws, admin_ws)
            assert ws.receive_bytes() == BANNER
            monkeypatch.setattr(mod, "_stream_authorized", AsyncMock(return_value=False))
            assert _closed_with(ws) == 4401
        assert _wait_released()
    assert "code=4401" in caplog.text


def test_a_passing_re_check_keeps_the_relay_open(admin_ws, vnc, monkeypatch):
    import istota.webui.app as mod

    monkeypatch.setattr(console, "RECHECK_SECONDS", 0.05)
    checks = AsyncMock(return_value=True)
    monkeypatch.setattr(mod, "_stream_authorized", checks)
    with admin_ws.websocket_connect(CONSOLE, headers=ORIGIN) as ws:
        _open(ws, admin_ws)
        assert ws.receive_bytes() == BANNER
        time.sleep(0.3)
        ws.send_bytes(b"still here")
        assert vnc.received.get(timeout=5) == b"still here"
        ws.close(code=1005)
    assert checks.await_count >= 2
    _, kwargs = checks.await_args
    assert kwargs == {"admin": True}


def test_the_lifetime_limit_closes_4408(admin_ws, vnc, monkeypatch, caplog):
    monkeypatch.setattr(console, "LIFETIME_SECONDS", 0.2)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        with admin_ws.websocket_connect(CONSOLE, headers=ORIGIN) as ws:
            _open(ws, admin_ws)
            assert ws.receive_bytes() == BANNER
            assert _closed_with(ws) == 4408
        assert _wait_released()
    assert "code=4408" in caplog.text


def test_an_unexpected_relay_error_closes_1011(admin_ws, vnc, monkeypatch, caplog):
    async def broken(*args, **kwargs):
        raise ValueError("secret detail")

    monkeypatch.setattr(console, "_copy_to_socket", broken)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        with admin_ws.websocket_connect(CONSOLE, headers=ORIGIN) as ws:
            _open(ws, admin_ws)
            assert _closed_with(ws) == 1011
        assert _wait_released()
    assert "browser_console_error admin='alice' user='bob' err=ValueError" in caplog.text
    assert "secret detail" not in caplog.text
