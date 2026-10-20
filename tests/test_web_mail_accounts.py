"""Settings > Connections mail account routes, over the real store.

The properties that matter: an account id from a URL reaches only the signed-in
user's row (another user's id is a 404 that changes nothing), no response on
any path carries a password, a refused body echoes nothing it was sent, and the
connection test runs in the daemon, never in this process.
"""

import json
import logging
from unittest.mock import AsyncMock

import pytest

from istota import db
from istota.mail import account_tests, accounts, drafts
from tests.test_web_app import app, client, config  # noqa: F401 -- shared fixtures

BASE = "/istota/api/settings/mail-accounts"
ORIGIN = {"Origin": "https://example.com"}
MARKER = "synthetic-mail-password-9f3e"
SMTP_MARKER = "synthetic-smtp-password-41aa"


async def login(http, user):
    import istota.webui.app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": user})
    response = await http.get("/istota/callback", follow_redirects=False)
    assert response.status_code in (302, 303, 307), response.text


@pytest.fixture
async def signed_client(client, config, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    config.security.allow_unsandboxed_multi_user_vaults = True
    await login(client, "alice")
    return client


def account_body(**changes):
    body = {
        "slug": "work", "label": "Work", "imap_host": "imap.example.com",
        "imap_port": 993, "imap_security": "tls", "imap_username": "alice@example.com",
        "imap_password": MARKER,
    }
    body.update(changes)
    return body


def sender_body(**changes):
    return account_body(
        permission="read_send", from_address="alice@example.com",
        smtp={"host": "smtp.example.com", "port": 587, "security": "starttls",
              "same_login": False, "username": "alice-smtp"},
        smtp_password=SMTP_MARKER, **changes,
    )


async def create(http, **changes):
    response = await http.post(BASE, json=account_body(**changes), headers=ORIGIN)
    assert response.status_code == 200, response.text
    return response.json()["account"]


def _row(config, user, account_id):  # noqa: F811
    with db.get_db(config.db_path) as conn:
        return accounts.get_account_by_id(conn, user, account_id)


def _secret_count(config, user="alice"):  # noqa: F811
    with db.get_db(config.db_path) as conn:
        return conn.execute(
            "SELECT count(*) FROM secrets WHERE user_id = ? AND service = 'mail_account'", (user,),
        ).fetchone()[0]


# ---------------------------------------------------------------------------
# Isolation: another user's id reaches nothing
# ---------------------------------------------------------------------------


async def test_another_user_cannot_patch_delete_or_test_an_account(signed_client, config):  # noqa: F811
    alices = await create(signed_client)
    await login(signed_client, "bob")
    bobs = await create(signed_client, imap_username="bob@example.com")

    for method, path, body in (
        ("patch", f"/{alices['id']}", {"label": "Mine now"}),
        ("delete", f"/{alices['id']}", None),
        ("post", f"/{alices['id']}/test", None),
    ):
        response = await signed_client.request(method, BASE + path, json=body, headers=ORIGIN)
        assert response.status_code == 404, (method, response.text)

    row = _row(config, "alice", alices["id"])
    assert row is not None and row.label == "Work" and row.has_imap_password
    assert account_tests.requests_pending(config) is False
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM mail_accounts").fetchone()[0] == 2

    # The same three verbs on bob's own account are not refused.
    assert (await signed_client.patch(BASE + f"/{bobs['id']}", json={"label": "B"},
                                      headers=ORIGIN)).status_code == 200
    assert (await signed_client.post(BASE + f"/{bobs['id']}/test", headers=ORIGIN)).status_code == 202
    assert (await signed_client.delete(BASE + f"/{bobs['id']}", headers=ORIGIN)).status_code == 200
    listing = (await signed_client.get(BASE)).json()
    assert listing["accounts"] == []


async def test_another_users_test_request_reads_unknown(signed_client, config):  # noqa: F811
    alices = await create(signed_client)
    response = await signed_client.post(BASE + f"/{alices['id']}/test", headers=ORIGIN)
    assert response.status_code == 202
    request_id = response.json()["request_id"]

    await login(signed_client, "bob")
    seen = await signed_client.get(BASE + f"/test-requests/{request_id}")
    assert seen.status_code == 200
    assert seen.json() == {"status": "unknown"}

    await login(signed_client, "alice")
    assert (await signed_client.get(BASE + f"/test-requests/{request_id}")).json() == {"status": "pending"}


async def test_listing_shows_only_the_signed_in_users_accounts(signed_client, config):  # noqa: F811
    await create(signed_client)
    await login(signed_client, "bob")
    listing = (await signed_client.get(BASE)).json()
    assert listing["accounts"] == []


# ---------------------------------------------------------------------------
# No password on any response path
# ---------------------------------------------------------------------------


async def test_no_password_on_any_response(signed_client, config, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    texts = []
    created = await signed_client.post(BASE, json=sender_body(), headers=ORIGIN)
    assert created.status_code == 200, created.text
    texts.append(created.text)
    account = created.json()["account"]
    assert account["imap_password_set"] is True and account["smtp_password_set"] is True
    assert account["smtp"] == {"host": "smtp.example.com", "port": 587, "security": "starttls",
                               "same_login": False, "username": "alice-smtp"}

    listing = await signed_client.get(BASE)
    texts.append(listing.text)
    assert listing.headers["cache-control"] == "no-store"
    data = listing.json()
    assert data["enabled"] is True and data["allow_send"] is True and data["max_per_user"] == 10
    [row] = data["accounts"]
    assert set(row) >= {
        "id", "slug", "label", "permission", "imap_host", "imap_port", "imap_security",
        "imap_username", "default_folder", "smtp", "from_address", "from_name", "sent_folder",
        "enabled", "imap_password_set", "smtp_password_set", "pending_drafts", "updated_at",
    }
    assert not any("password" in key and not key.endswith("_set") for key in row)

    patched = await signed_client.patch(
        BASE + f"/{account['id']}", json={"imap_password": MARKER + "-2", "label": "W"},
        headers=ORIGIN,
    )
    assert patched.status_code == 200
    texts.append(patched.text)
    response = await signed_client.post(BASE + f"/{account['id']}/test", headers=ORIGIN)
    texts.append(response.text)
    texts.append((await signed_client.get(
        BASE + f"/test-requests/{response.json()['request_id']}")).text)
    texts.append((await signed_client.delete(BASE + f"/{account['id']}", headers=ORIGIN)).text)

    for text in texts:
        assert MARKER not in text and SMTP_MARKER not in text
    assert MARKER not in caplog.text and SMTP_MARKER not in caplog.text


@pytest.mark.parametrize("body", [
    [MARKER],
    {MARKER: MARKER},
    account_body(imap_password=12345, label=MARKER),
    account_body(imap_port=MARKER),
    account_body(smtp=MARKER),
    account_body(smtp={"host": "smtp.example.com", MARKER: MARKER}),
    account_body(enabled=MARKER),
    account_body(imap_host=f"imap://{MARKER}"),
    account_body(slug=MARKER.upper()),
])
async def test_bad_body_never_echoes_input(signed_client, caplog, body):
    caplog.set_level(logging.DEBUG)
    response = await signed_client.post(BASE, json=body, headers=ORIGIN)
    assert response.status_code == 400, response.text
    assert set(response.json()) >= {"detail", "field"}
    assert MARKER not in response.text and MARKER not in caplog.text


async def test_unparseable_and_oversized_bodies(signed_client, config):  # noqa: F811
    for raw in (MARKER + "{", "[" * 10000):
        response = await signed_client.post(BASE, content=raw, headers=ORIGIN)
        assert response.status_code == 400
        assert MARKER not in response.text
    response = await signed_client.post(BASE, content="x" * 20000, headers=ORIGIN)
    assert response.status_code == 413
    assert _secret_count(config) == 0


async def test_unexpected_store_error_logs_the_type_only(signed_client, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    def broken(*args, **kwargs):
        raise RuntimeError(MARKER)

    monkeypatch.setattr(accounts, "ensure_account", broken)
    response = await signed_client.post(BASE, json=account_body(), headers=ORIGIN)
    assert response.status_code == 500
    assert MARKER not in response.text and MARKER not in caplog.text
    assert "RuntimeError" in caplog.text


# ---------------------------------------------------------------------------
# Gates: origin, isolation, the feature switch, the master key
# ---------------------------------------------------------------------------


async def test_writes_need_origin_and_the_isolation_gate(signed_client, config, monkeypatch):  # noqa: F811
    from istota.credentials import names as vault

    account = await create(signed_client)
    writes = [("post", "", account_body(slug="other")), ("patch", f"/{account['id']}", {"label": "x"}),
              ("delete", f"/{account['id']}", None), ("post", f"/{account['id']}/test", None)]
    for method, path, body in writes:
        response = await signed_client.request(method, BASE + path, json=body)
        assert response.status_code == 403, (method, path)

    monkeypatch.setattr(vault, "vault_isolation_refusal", lambda *a, **k: "isolation refused")
    for method, path, body in writes:
        response = await signed_client.request(method, BASE + path, json=body, headers=ORIGIN)
        assert response.status_code == 403, (method, path)
    listing = await signed_client.get(BASE)
    assert listing.status_code == 200
    assert listing.json()["refusal"] == "isolation refused"
    assert _row(config, "alice", account["id"]).label == "Work"
    assert account_tests.requests_pending(config) is False

    signed_client.cookies.clear()
    assert (await signed_client.get(BASE)).status_code == 401


async def test_every_route_404s_when_mail_accounts_are_off(signed_client, config):  # noqa: F811
    account = await create(signed_client)
    config.mail_accounts.enabled = False
    for method, path in (("get", ""), ("post", ""), ("patch", f"/{account['id']}"),
                         ("delete", f"/{account['id']}"), ("post", f"/{account['id']}/test"),
                         ("get", "/test-requests/0123456789abcdef")):
        response = await signed_client.request(method, BASE + path, json={}, headers=ORIGIN)
        assert response.status_code == 404, (method, path)
    assert _row(config, "alice", account["id"]) is not None


async def test_a_password_write_without_the_master_key_is_503_and_writes_nothing(
    signed_client, config, monkeypatch,  # noqa: F811
):
    monkeypatch.delenv("ISTOTA_SECRET_KEY")
    response = await signed_client.post(BASE, json=account_body(), headers=ORIGIN)
    assert response.status_code == 503
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM mail_accounts").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Create, update, delete
# ---------------------------------------------------------------------------


async def test_slug_taken_and_too_many_accounts(signed_client, config):  # noqa: F811
    await create(signed_client)
    response = await signed_client.post(BASE, json=account_body(label="Again"), headers=ORIGIN)
    assert response.status_code == 409
    assert response.json()["field"] == "slug"
    assert response.json()["reason"] == "slug_taken"

    config.mail_accounts.max_per_user = 1
    response = await signed_client.post(BASE, json=account_body(slug="second"), headers=ORIGIN)
    assert response.status_code == 400
    assert response.json()["reason"] == "too_many_accounts"


async def test_create_needs_an_imap_password(signed_client, config):  # noqa: F811
    body = account_body()
    del body["imap_password"]
    response = await signed_client.post(BASE, json=body, headers=ORIGIN)
    assert response.status_code == 400
    assert response.json()["field"] == "imap_password"
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM mail_accounts").fetchone()[0] == 0


async def test_patch_renames_by_id_and_keeps_the_passwords(signed_client, config):  # noqa: F811
    account = await create(signed_client)
    response = await signed_client.patch(BASE + f"/{account['id']}", json={"slug": "office"},
                                         headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert response.json()["account"]["slug"] == "office"
    row = _row(config, "alice", account["id"])
    assert row.slug == "office" and row.has_imap_password


async def test_patch_null_password_keeps_it_and_a_string_replaces_it(signed_client, config):  # noqa: F811
    from istota.credentials import store

    account = await create(signed_client)
    key = accounts.secret_key(account["id"], "imap")
    await signed_client.patch(BASE + f"/{account['id']}", json={"imap_password": None},
                              headers=ORIGIN)
    assert store.get_secret(config.db_path, "alice", "mail_account", key) == MARKER
    await signed_client.patch(BASE + f"/{account['id']}", json={"imap_password": "new-one"},
                              headers=ORIGIN)
    assert store.get_secret(config.db_path, "alice", "mail_account", key) == "new-one"


async def test_patch_smtp_null_drops_sending_and_its_secret(signed_client, config):  # noqa: F811
    created = await signed_client.post(BASE, json=sender_body(), headers=ORIGIN)
    account = created.json()["account"]
    assert account["permission"] == "read_send"
    response = await signed_client.patch(BASE + f"/{account['id']}", json={"smtp": None},
                                         headers=ORIGIN)
    assert response.status_code == 200, response.text
    changed = response.json()["account"]
    assert changed["smtp"] is None and changed["permission"] == "read"
    assert changed["smtp_password_set"] is False
    assert _secret_count(config) == 1


async def test_patch_partial_smtp_changes_only_what_it_names(signed_client, config):  # noqa: F811
    created = await signed_client.post(BASE, json=sender_body(), headers=ORIGIN)
    account = created.json()["account"]
    response = await signed_client.patch(BASE + f"/{account['id']}", json={"smtp": {"port": 465,
                                         "security": "tls"}}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert response.json()["account"]["smtp"] == {
        "host": "smtp.example.com", "port": 465, "security": "tls",
        "same_login": False, "username": "alice-smtp",
    }


async def test_patch_refuses_send_without_smtp(signed_client, config):  # noqa: F811
    account = await create(signed_client)
    response = await signed_client.patch(BASE + f"/{account['id']}",
                                         json={"permission": "read_send"}, headers=ORIGIN)
    assert response.status_code == 400
    assert response.json()["field"] == "smtp.host"
    assert _row(config, "alice", account["id"]).permission == "read"


async def test_delete_discards_pending_drafts_and_says_how_many(signed_client, config):  # noqa: F811
    created = await signed_client.post(BASE, json=sender_body(), headers=ORIGIN)
    account = created.json()["account"]
    with db.get_db(config.db_path) as conn:
        for _ in range(2):
            draft_id = drafts.hold(
                conn, user_id="alice", task_id=None, room_token=None,
                to_addrs=["dana@example.org"], cc_addrs=[], bcc_addrs=[],
                subject="Re: lunch", body="Yes.", html=False, in_reply_to=None,
                references=None, attachments=[], origin_target=None,
                hold_reason="untrusted_recipient",
            )
            conn.execute("UPDATE outbound_drafts SET account_id = ? WHERE id = ?",
                         (account["id"], draft_id))
    listing = (await signed_client.get(BASE)).json()
    assert listing["accounts"][0]["pending_drafts"] == 2

    response = await signed_client.delete(BASE + f"/{account['id']}", headers=ORIGIN)
    assert response.status_code == 200
    assert response.json() == {"deleted": True, "drafts_discarded": 2}
    assert _row(config, "alice", account["id"]) is None
    assert _secret_count(config) == 0
    with db.get_db(config.db_path) as conn:
        statuses = {r[0] for r in conn.execute("SELECT status FROM outbound_drafts")}
    assert statuses == {drafts.STATUS_DISCARDED}


@pytest.mark.parametrize("ident", ["abc", "-1", "1" * 30, "١"])
async def test_a_malformed_id_is_404(signed_client, ident):
    response = await signed_client.patch(BASE + f"/{ident}", json={}, headers=ORIGIN)
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# The connection test, through the daemon
# ---------------------------------------------------------------------------


class FakeMailbox:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.calls.append(("login", user, password))


async def test_the_test_runs_in_the_daemon_and_the_page_reads_the_answer(
    signed_client, config, monkeypatch,  # noqa: F811
):
    from istota.mail import connect

    calls = []

    def fake_open_imap(email, *, check_public=False, private_hosts=()):
        calls.append(("open", email.imap_host, check_public, tuple(private_hosts)))
        return FakeMailbox(calls)

    def no_smtp(*a, **k):
        raise AssertionError("a read account's test signs in to no SMTP server")

    monkeypatch.setattr(connect, "open_imap", fake_open_imap)
    monkeypatch.setattr(connect, "open_smtp", no_smtp)
    config.mail_accounts.private_hosts = ["imap.example.com"]

    account = await create(signed_client)
    response = await signed_client.post(BASE + f"/{account['id']}/test", headers=ORIGIN)
    assert response.status_code == 202
    assert response.headers["cache-control"] == "no-store"
    request_id = response.json()["request_id"]
    assert calls == []  # nothing ran in the web process

    account_tests.answer_pending(config)
    assert calls == [("open", "imap.example.com", True, ("imap.example.com",)),
                     ("login", "alice@example.com", MARKER)]

    answered = await signed_client.get(BASE + f"/test-requests/{request_id}")
    body = answered.json()
    assert body["status"] == "answered"
    assert body["result"]["ok"] is True
    assert body["result"]["lines"] == [
        {"step": "imap", "ok": True, "message": "Signed in to imap.example.com."},
    ]
    assert MARKER not in answered.text


async def test_one_pending_test_per_user_then_429(signed_client, config):  # noqa: F811
    account = await create(signed_client)
    first = await signed_client.post(BASE + f"/{account['id']}/test", headers=ORIGIN)
    assert first.status_code == 202
    second = await signed_client.post(BASE + f"/{account['id']}/test", headers=ORIGIN)
    assert second.status_code == 429
    assert "retry_after" not in second.json()


async def test_the_hourly_cap_is_429_with_retry_after(signed_client, config, monkeypatch):  # noqa: F811
    account = await create(signed_client)

    def over_cap(*a, **k):
        raise account_tests.RateLimited(1234)

    monkeypatch.setattr(account_tests, "request_test", over_cap)
    response = await signed_client.post(BASE + f"/{account['id']}/test", headers=ORIGIN)
    assert response.status_code == 429
    assert response.json()["retry_after"] == 1234
    assert response.headers.get("retry-after") == "1234"


async def test_a_malformed_request_id_reads_unknown(signed_client):
    response = await signed_client.get(BASE + "/test-requests/" + "x" * 40)
    assert response.status_code == 200
    assert response.json() == {"status": "unknown"}
    assert json.dumps(response.json()).count("x") == 0
