"""The secret link form: `GET /istota/secret?token=` and `POST /istota/secret`.

GET writes nothing, so a link preview cannot spend a link. POST stores the
credential, grants it to the asking conversation and spends the link in one
transaction, then leaves a notice for the daemon to deliver. The value is
never echoed back, never logged and never put in an exception.
"""
import asyncio
import json
import logging
import re
from collections import OrderedDict
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

pytest.importorskip("authlib")
pytest.importorskip("fastapi")

from istota import db, user_profiles
from istota.config import Config, SiteConfig, UserConfig, WebConfig
from istota.credentials import local
from istota.credentials import names as secrets_vault
from istota.credentials import store as secrets_store
from istota.credentials.broker import bindings, grants
from istota.notifications import store as notification_store
from istota.webui import auth as web_auth
from istota.webui import secret_links

PASSWORD = "a long example passphrase"
VALUE = "correct-horse-battery-staple-1729"
USERNAME = "alice-at-acme"


@pytest.fixture(autouse=True)
def _cheap_password_hash(monkeypatch):
    monkeypatch.setattr(web_auth, "_N", 1024)
    monkeypatch.setattr(web_auth, "DUMMY_HASH", web_auth.hash_password("unused"))


@pytest.fixture
def configured(db_path, monkeypatch):
    from istota.webui import app as mod

    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    config = Config(db_path=db_path, bot_name="Ada", site=SiteConfig(hostname="example.com"),
                    web=WebConfig(auth=["email"]),
                    users={"alice": UserConfig(display_name="Alice"), "bob": UserConfig(display_name="Bob")})
    config.security.allow_unsandboxed_multi_user_vaults = True
    monkeypatch.setattr(mod, "_config", config)
    monkeypatch.setattr(mod.app.state, "istota_config", config, raising=False)
    monkeypatch.setattr(mod, "_login_ingress", OrderedDict())
    monkeypatch.setattr(mod, "_oauth", MagicMock())
    for user_id, name in (("alice", "Alice"), ("bob", "Bob")):
        user_profiles.ensure_profile(db_path, user_id, display_name=name)
        web_auth.upsert_identity(db_path, user_id, f"{user_id}@example.com")
        web_auth.set_password(db_path, user_id, PASSWORD)
    return mod


@pytest.fixture
async def client(configured):
    async with AsyncClient(transport=ASGITransport(app=configured.app),
                           base_url="https://example.com") as client:
        yield client


def _task(config, user_id="alice"):
    with db.get_db(config.db_path) as conn:
        room = db.create_web_chat_room(conn, user_id, "general").token
        task_id = db.create_task(conn, user_id=user_id, source_type="web",
                                 prompt="here is my password", conversation_token=room)
    return task_id, room


def _issue(config, *, user_id="alice", name="acme", url="https://acme.example", username=True,
           intent="credential", clean=None):
    task_id, room = _task(config, user_id)
    params = {"name": name, "url": url, "username": username} if intent == "credential" else {}
    if clean is not None:
        params["clean"] = clean
    with db.get_db(config.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        token, link = secret_links.issue(conn, user_id=user_id, intent=intent, params=params,
                                         task_id=task_id, room_token=room, ttl_seconds=900)
    return token, link


def _link_row(config, link_id):
    with db.get_db(config.db_path) as conn:
        return dict(conn.execute("SELECT * FROM secret_links WHERE id=?", (link_id,)).fetchone())


def _notices(config, user_id="alice"):
    with db.get_db(config.db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM notifications WHERE user_id=? ORDER BY id", (user_id,))]


def _handoffs(config):
    with db.get_db(config.db_path) as conn:
        return conn.execute("SELECT count(*) FROM shared_kv WHERE namespace=?",
                            (notification_store.HANDOFF_NAMESPACE,)).fetchone()[0]


def _stored(config, user_id, key):
    return secrets_store.get_secret(config.db_path, user_id, secrets_vault.VAULT_ENTRY_SERVICE, key)


def _field(page, name):
    match = re.search(r'<form[^>]*action="/istota/secret"[^>]*>(?:(?!</form>).)*?name="action" value="save"'
                      r'(?:(?!</form>).)*</form>', page.text, re.S)
    assert match, page.text
    return re.search(r'name="' + name + r'" value="([^"]*)"', match[0])[1]


async def _post(client, token, *, action="save", value=VALUE, username=USERNAME, csrf=None):
    if csrf is None:
        csrf = _field(await client.get("/istota/secret", params={"token": token}), "csrf_token")
    data = {"token": token, "csrf_token": csrf, "action": action}
    if action == "save":
        data["value"] = value
        if username is not None:
            data["username"] = username
    return await client.post("/istota/secret", data=data)


async def _sign_in(client, email):
    page = await client.get("/istota/login")
    form = re.search(r'<form[^>]*action="/istota/login/email"[^>]*>(.*?)</form>', page.text, re.S)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', form[1])[1]
    answer = await client.post("/istota/login/email",
                               data={"email": email, "password": PASSWORD, "csrf_token": csrf})
    assert answer.status_code == 302


class TestTheForm:
    async def test_it_names_what_is_stored_and_writes_nothing(self, client, configured):
        token, link = _issue(configured._config)
        before = _link_row(configured._config, link.id)
        for _ in range(3):
            page = await client.get("/istota/secret", params={"token": token})
            assert page.status_code == 200
        assert _link_row(configured._config, link.id) == before
        assert _notices(configured._config) == []
        text = page.text
        assert "acme" in text and "acme.example" in text and "Alice" in text
        assert 'autocomplete="current-password"' in text
        assert 'autocomplete="username"' in text
        assert 'name="action" value="cancel"' in text
        assert _field(page, "token") == token
        assert "UTC" in text

    async def test_no_username_field_unless_asked(self, client, configured):
        token, _ = _issue(configured._config, username=False)
        page = await client.get("/istota/secret", params={"token": token})
        assert 'name="username"' not in page.text
        assert 'autocomplete="current-password"' in page.text

    async def test_the_expiry_is_in_the_users_zone(self, client, configured):
        user_profiles.update_profile(configured._config.db_path, "alice", timezone="Pacific/Kiritimati")
        token, link = _issue(configured._config)
        page = await client.get("/istota/secret", params={"token": token})
        when = secret_links.local_clock(configured._config, "alice", secret_links.expiry(link.expires_at))
        assert "UTC" not in when and when in page.text

    async def test_the_headers_keep_the_page_out_of_frames_caches_and_indexes(self, client, configured):
        token, _ = _issue(configured._config)
        for answer in (await client.get("/istota/secret", params={"token": token}),
                       await client.get("/istota/secret", params={"token": "nope"})):
            assert answer.headers["cache-control"] == "no-store"
            assert answer.headers["referrer-policy"] == "no-referrer"
            assert answer.headers["content-security-policy"] == "frame-ancestors 'none'"
            assert answer.headers["x-robots-tag"] == "noindex"

    async def test_an_existing_name_shows_the_host_it_is_bound_to(self, client, configured):
        config = configured._config
        with db.get_db(config.db_path) as conn:
            local.create(conn, "alice", local.LocalCredential(
                name="acme", value="old", url="https://stored.example"))
        token, _ = _issue(config, url="https://requested.example")
        page = await client.get("/istota/secret", params={"token": token})
        assert "stored.example" in page.text
        assert "requested.example" not in page.text


    async def test_a_link_asked_for_cleanly_shows_no_caution(self, client, configured):
        token, _ = _issue(configured._config, clean=True)
        page = await client.get("/istota/secret", params={"token": token})
        assert page.status_code == 200
        assert "partway through other work" not in page.text
        assert 'class="form-alert"' not in page.text

    @pytest.mark.parametrize("clean", [False, None], ids=["not_clean", "unrecorded"])
    async def test_a_link_not_asked_for_cleanly_says_so_above_the_fields(self, client, configured, clean):
        token, _ = _issue(configured._config, clean=clean)
        page = await client.get("/istota/secret", params={"token": token})
        assert page.status_code == 200
        caution = re.search(r'<p class="form-alert"[^>]*>.*?</p>', page.text, re.S)
        assert caution and "partway through other work" in caution[0]
        assert "Only continue if you asked to give me a password" in caution[0]
        assert page.text.index("partway through other work") < page.text.index('name="value"')

class TestDeadLinks:
    async def _generic(self, client, token):
        page = await client.get("/istota/secret", params={"token": token})
        return page.status_code, page.text

    async def test_unknown_expired_used_and_cancelled_look_the_same(self, client, configured):
        config = configured._config
        expired, expired_link = _issue(config, name="one")
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE secret_links SET expires_at='2000-01-01 00:00:00.000000' WHERE id=?",
                         (expired_link.id,))
        used, used_link = _issue(config, name="two")
        cancelled, cancelled_link = _issue(config, name="three")
        with db.get_db(config.db_path) as conn:
            secret_links.spend(conn, used_link.id, "stored")
            secret_links.spend(conn, cancelled_link.id, "cancelled")
        answers = {await self._generic(client, token)
                   for token in ("unknown-token", expired, used, cancelled)}
        assert len(answers) == 1
        ((status, text),) = answers
        assert status == 400
        assert "expired or was already used" in text
        assert "Ask Ada for a new one" in text
        assert (await client.get("/istota/secret")).status_code == 400

    async def test_a_google_link_gets_its_own_page_not_the_credential_form(self, client, configured):
        # The Google intent's own cases are in tests/test_admin_google_connection.py.
        token, _ = _issue(configured._config, intent="google_connect")
        status, text = await self._generic(client, token)
        assert status == 200 and "Connect Google" in text
        assert 'name="value"' not in text and 'value="save"' not in text


class TestAnotherAccount:
    async def test_a_session_for_someone_else_is_refused_and_the_link_kept(self, client, configured):
        token, link = _issue(configured._config)
        await _sign_in(client, "bob@example.com")
        page = await client.get("/istota/secret", params={"token": token})
        assert page.status_code == 403
        assert "belongs to another account" in page.text
        answer = await client.post("/istota/secret", data={
            "token": token, "csrf_token": "x", "action": "save", "value": VALUE})
        assert answer.status_code == 403
        assert _link_row(configured._config, link.id)["used_at"] is None
        assert not _is_local(configured._config)

    async def test_a_session_for_the_same_user_is_fine(self, client, configured):
        token, _ = _issue(configured._config)
        await _sign_in(client, "alice@example.com")
        assert (await client.get("/istota/secret", params={"token": token})).status_code == 200
        assert "Saved" in (await _post(client, token)).text


def _is_local(config, name="acme"):
    with db.get_db(config.db_path) as conn:
        return local.is_local(conn, "alice", name)


class TestSaving:
    async def test_without_csrf_nothing_happens(self, client, configured):
        token, link = _issue(configured._config)
        await client.get("/istota/secret", params={"token": token})
        answer = await client.post("/istota/secret", data={
            "token": token, "csrf_token": "forged", "action": "save", "value": VALUE})
        assert answer.status_code == 403
        assert _link_row(configured._config, link.id)["used_at"] is None
        assert _link_row(configured._config, link.id)["failures"] == 0
        assert not _is_local(configured._config)

    async def test_a_new_name_is_created_granted_spent_and_noticed(self, client, configured):
        config = configured._config
        token, link = _issue(config)
        answer = await _post(client, token)
        assert answer.status_code == 200
        assert "Saved" in answer.text and "go back to the chat" in answer.text
        assert VALUE not in answer.text and USERNAME not in answer.text
        assert _is_local(config)
        assert _stored(config, "alice", "acme") == VALUE
        username_name, url_name, _ = local.derived_names("acme")
        assert _stored(config, "alice", username_name) == USERNAME
        assert _stored(config, "alice", url_name) == "https://acme.example"
        with db.get_db(config.db_path) as conn:
            grant = grants.get_grant(conn, "alice", "acme")
        assert grant is not None and grant["rooms"] == [link.room_token]
        row = _link_row(config, link.id)
        assert row["used_at"] is not None and row["outcome"] == "stored"
        (notice,) = _notices(config)
        assert notice["title"] == "Credential acme saved"
        assert "If that wasn't you" in notice["body"]
        assert VALUE not in notice["body"]
        assert _handoffs(config) == 1

    async def test_the_asking_task_is_not_granted_the_new_credential(self, client, configured):
        config = configured._config
        token, link = _issue(config)
        with db.get_db(config.db_path) as conn:
            # The asking task froze its snapshot when it started.
            grants.ensure_credential_grants(conn, link.task_id, "alice")
            conn.commit()
        assert "Saved" in (await _post(client, token)).text
        with db.get_db(config.db_path) as conn:
            assert grants.get_grant(conn, "alice", "acme")["rooms"] == [link.room_token]
            assert conn.execute("SELECT count(*) FROM credential_task_grants WHERE task_id=?",
                                (link.task_id,)).fetchone()[0] == 0
            assert grants.check_credential_use(conn, link.task_id, "alice", "acme") == "credential_not_granted"
            conn.rollback()

    async def test_a_subtask_of_the_asking_task_is_not_granted_it(self, client, configured):
        # A subtask the asking task deferred can start after the save; it is
        # still the asking turn's work, not the user's next message.
        config = configured._config
        token, link = _issue(config)
        assert "Saved" in (await _post(client, token)).text
        with db.get_db(config.db_path) as conn:
            child = db.create_task(conn, user_id="alice", source_type="subtask", prompt="fill it",
                                   conversation_token=link.room_token, parent_task_id=link.task_id)
            grandchild = db.create_task(conn, user_id="alice", source_type="subtask", prompt="now",
                                        conversation_token=link.room_token, parent_task_id=child)
            for task_id in (child, grandchild):
                assert "acme" not in grants.ensure_credential_grants(conn, task_id, "alice")
                assert grants.check_credential_use(conn, task_id, "alice", "acme") == "credential_not_granted"
            conn.rollback()

    async def test_the_next_task_in_the_room_is_granted_it(self, client, configured):
        config = configured._config
        token, link = _issue(config)
        assert "Saved" in (await _post(client, token)).text
        with db.get_db(config.db_path) as conn:
            later = db.create_task(conn, user_id="alice", source_type="web", prompt="sign in now",
                                   conversation_token=link.room_token)
            assert "acme" in grants.ensure_credential_grants(conn, later, "alice")
            assert grants.check_credential_use(conn, later, "alice", "acme") is None
            conn.rollback()

    async def test_the_link_works_once(self, client, configured):
        token, _ = _issue(configured._config)
        csrf = _field(await client.get("/istota/secret", params={"token": token}), "csrf_token")
        assert "Saved" in (await _post(client, token, csrf=csrf)).text
        again = await _post(client, token, value="another-value-entirely", csrf=csrf)
        assert again.status_code == 400 and "expired or was already used" in again.text
        assert _stored(configured._config, "alice", "acme") == VALUE
        assert len(_notices(configured._config)) == 1

    async def test_an_existing_local_name_gets_a_new_value_and_keeps_the_rest(self, client, configured):
        config = configured._config
        with db.get_db(config.db_path) as conn:
            local.create(conn, "alice", local.LocalCredential(
                name="acme", value="old", username="kept-user", url="https://stored.example",
                extra_hosts="api.stored.example", revealable=True),
                access={"scope_mode": "all", "allow_scheduled": True})
            before_binding = bindings.get_entry_binding(conn, "alice", "acme")
            before_grant = grants.get_grant(conn, "alice", "acme")
        token, _ = _issue(config, url="https://requested.example", username=True)
        answer = await _post(client, token, username="")
        assert "Saved" in answer.text
        assert _stored(config, "alice", "acme") == VALUE
        username_name, url_name, _ = local.derived_names("acme")
        assert _stored(config, "alice", username_name) == "kept-user"
        assert _stored(config, "alice", url_name) == "https://stored.example"
        with db.get_db(config.db_path) as conn:
            assert bindings.get_entry_binding(conn, "alice", "acme") == before_binding
            assert grants.get_grant(conn, "alice", "acme") == before_grant

    async def test_concurrent_saves_store_once(self, client, configured):
        config = configured._config
        token, link = _issue(config)
        csrf = _field(await client.get("/istota/secret", params={"token": token}), "csrf_token")
        answers = await asyncio.gather(*(
            _post(client, token, value=f"{VALUE}-{n}", csrf=csrf) for n in range(4)))
        assert sorted(a.status_code for a in answers) == [200, 400, 400, 400]
        winner = next(a for a in answers if a.status_code == 200)
        assert "Saved" in winner.text
        assert _stored(config, "alice", "acme").startswith(VALUE)
        assert len(_notices(config)) == 1
        assert _link_row(config, link.id)["outcome"] == "stored"


class TestFailures:
    async def test_a_refused_value_re_renders_counts_and_keeps_the_link(self, client, configured):
        config = configured._config
        token, link = _issue(config)
        answer = await _post(client, token, value=" padded ")
        assert answer.status_code == 400
        assert "whitespace" in answer.text
        assert " padded " not in answer.text
        assert 'name="action" value="save"' in answer.text
        # Beside the password field, which is marked, with the username kept.
        assert answer.text.index('id="secret-value"') < answer.text.index("whitespace")
        assert 'aria-invalid="true" aria-describedby="secret-value-error secret-value-help"' in answer.text
        assert f'value="{USERNAME}"' in answer.text
        row = _link_row(config, link.id)
        assert row["failures"] == 1 and row["used_at"] is None
        assert not _is_local(config)
        assert "Saved" in (await _post(client, token)).text

    async def test_the_fifth_failure_closes_the_link_and_says_so(self, client, configured):
        config = configured._config
        token, link = _issue(config)
        csrf = _field(await client.get("/istota/secret", params={"token": token}), "csrf_token")
        for _ in range(secret_links.MAX_FAILURES - 1):
            assert (await _post(client, token, value="", csrf=csrf)).status_code == 400
        last = await _post(client, token, value="", csrf=csrf)
        assert last.status_code == 400 and "closed" in last.text
        row = _link_row(config, link.id)
        assert row["outcome"] == "failed" and row["failures"] == secret_links.MAX_FAILURES
        (notice,) = _notices(config)
        assert "acme" in notice["title"]
        after = await _post(client, token, csrf=csrf)
        assert "expired or was already used" in after.text
        assert not _is_local(config)

    async def test_cancel_spends_the_link_and_stores_nothing(self, client, configured):
        config = configured._config
        token, link = _issue(config)
        answer = await _post(client, token, action="cancel")
        assert answer.status_code == 200 and "cancelled" in answer.text
        assert _link_row(config, link.id)["outcome"] == "cancelled"
        assert not _is_local(config)
        (notice,) = _notices(config)
        assert "cancelled" in notice["title"].lower()

    @pytest.mark.parametrize("change", ["removed", "isolation", "no_profile", "disabled"])
    async def test_an_account_that_changed_since_issue_spends_the_link(self, client, configured,
                                                                      monkeypatch, change):
        config = configured._config
        token, link = _issue(config)
        csrf = _field(await client.get("/istota/secret", params={"token": token}), "csrf_token")
        if change == "removed":
            del config.users["alice"]
        elif change == "isolation":
            monkeypatch.setattr(configured, "_web_vault_refusal", lambda user_id: "not isolated")
        elif change == "no_profile":
            # Not `delete_profile`, which takes the link with it: a configured
            # user with no profile row reaches the form's own refusal.
            with db.get_db(config.db_path) as conn:
                conn.execute("DELETE FROM user_profiles WHERE user_id='alice'")
        else:
            web_auth.set_disabled(config.db_path, "alice", True)
        answer = await _post(client, token, csrf=csrf)
        assert answer.status_code == 400 and "expired or was already used" in answer.text
        assert _link_row(config, link.id)["outcome"] == "failed"
        assert not _is_local(config)

    async def test_a_user_re_created_under_the_same_id_cannot_use_the_old_link(self, client, configured):
        config = configured._config
        token, _ = _issue(config)
        bobs_token, bobs_link = _issue(config, user_id="bob")
        user_profiles.delete_profile(config.db_path, "alice")
        user_profiles.ensure_profile(config.db_path, "alice", display_name="Alice")
        page = await client.get("/istota/secret", params={"token": token})
        assert page.status_code == 400 and "expired or was already used" in page.text
        answer = await client.post("/istota/secret", data={
            "token": token, "csrf_token": "x", "action": "save", "value": VALUE})
        assert answer.status_code == 400
        assert not _is_local(config)
        with db.get_db(config.db_path) as conn:
            assert conn.execute("SELECT count(*) FROM secret_links WHERE user_id='alice'").fetchone()[0] == 0
        assert _link_row(config, bobs_link.id)["used_at"] is None
        assert (await client.get("/istota/secret", params={"token": bobs_token})).status_code == 200

    async def test_a_name_taken_elsewhere_since_issue_spends_the_link(self, client, configured):
        config = configured._config
        token, link = _issue(config)
        csrf = _field(await client.get("/istota/secret", params={"token": token}), "csrf_token")
        secrets_store.set_secret(
            config.db_path, "alice", secrets_vault.VAULT_ENTRY_SERVICE, "acme", "x",
            binding=bindings.parse_binding("acme.example", {}, [], source="generated"),
        )
        answer = await _post(client, token, csrf=csrf)
        assert answer.status_code == 400
        assert _link_row(config, link.id)["outcome"] == "failed"
        assert _stored(config, "alice", "acme") == "x"

    async def test_a_body_past_the_limit_is_refused_before_parsing(self, client, configured):
        token, link = _issue(configured._config)
        answer = await client.post("/istota/secret", data={
            "token": token, "action": "save", "value": "x" * (200 * 1024)})
        assert answer.status_code == 413
        assert _link_row(configured._config, link.id)["failures"] == 0

    async def test_the_largest_storable_value_fits_the_form(self, client, configured):
        token, _ = _issue(configured._config, username=False)
        value = "%" * secrets_vault.VAULT_MAX_VALUE_BYTES
        answer = await _post(client, token, value=value, username=None)
        assert "Saved" in answer.text
        assert _stored(configured._config, "alice", "acme") == value


class TestNothingLeaks:
    async def test_the_value_and_username_reach_no_log_line(self, client, configured, caplog):
        caplog.set_level(logging.DEBUG)
        token, _ = _issue(configured._config)
        await _post(client, token, value=" bad-value-in-logs ")
        await _post(client, token)
        # The test's own client logs each request URL; the app's lines are what count.
        logged = "\n".join(record.getMessage() for record in caplog.records
                           if not record.name.startswith(("httpx", "httpcore")))
        assert "secret_link" in logged
        for leaked in (VALUE, USERNAME, "bad-value-in-logs", token):
            assert leaked not in logged
        assert json.dumps(_notices(configured._config)).find(VALUE) == -1
