"""The admin user settings editor's API: GET, PATCH, the WhatsApp reset and the
login-email PUT, through the authenticated HTTP seam."""

import base64
import json
import logging

from httpx import ASGITransport, AsyncClient
from itsdangerous import TimestampSigner
import pytest

from istota import db, user_profiles
from istota.config import Config, SiteConfig, WebConfig
from istota.webui import auth as web_auth

pytest.importorskip("authlib")
pytest.importorskip("fastapi")

URL = "/istota/api/admin/users"


@pytest.fixture
def configured(db_path, monkeypatch):
    from istota.webui import app as mod

    config = Config(db_path=db_path, site=SiteConfig(hostname="example.com"),
                    web=WebConfig(auth=["email", "nextcloud"]), admin_users={"alice"})
    config.sms.enabled = True
    config.whatsapp.enabled = True
    monkeypatch.setattr(mod, "_config", config)
    monkeypatch.setattr(mod.app.state, "istota_config", config, raising=False)
    for name in ("alice", "bob", "carol"):
        user_profiles.ensure_profile(db_path, name, display_name=name.title())
    web_auth.upsert_identity(db_path, "alice", "alice@example.com")
    web_auth.upsert_identity(db_path, "bob", "bob@example.com")
    monkeypatch.setattr(mod.web_auth_mail, "send_auth_email", lambda *args: True)
    return mod


@pytest.fixture
def delivered(monkeypatch):
    from istota.notifications import store

    calls = []
    monkeypatch.setattr(store, "deliver_pending", lambda config, results: calls.append(list(results)))
    return calls


def session(client, mod, username):
    identity = web_auth.get_identity(mod._config.db_path, username)
    data = {"user": {"username": username, "display_name": username.title()},
            "auth": {"method": "nextcloud", "epoch": identity.credential_epoch if identity else 0}}
    secret = next(m.kwargs["secret_key"] for m in mod.app.user_middleware if m.cls.__name__ == "SessionMiddleware")
    value = TimestampSigner(str(secret)).sign(base64.b64encode(json.dumps(data).encode())).decode()
    client.cookies.set("istota_session", value, domain="example.com", path="/istota/")


@pytest.fixture
async def client(configured):
    async with AsyncClient(transport=ASGITransport(app=configured.app), base_url="https://example.com",
                           headers={"origin": "https://example.com"}) as client:
        session(client, configured, "alice")
        yield client


def _profile(configured, user_id):
    return user_profiles.get_profile(configured._config.db_path, user_id)


def _binding(configured, user_id):
    with db.get_db(configured._config.db_path) as conn:
        return db.get_whatsapp_binding(conn, user_id)


def _notifications(configured, user_id=None):
    with db.get_db(configured._config.db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM notifications WHERE source = 'admin_profile_change'"
            + (" AND user_id = ?" if user_id else ""),
            (user_id,) if user_id else (),
        ).fetchall()
    return [dict(row) for row in rows]


def _manage(configured, user_id, *fields):
    with db.get_db(configured._config.db_path) as conn:
        user_profiles.set_managed_fields(conn, user_id, fields)


WRITES = [("PATCH", "/bob"), ("POST", "/bob/whatsapp/reset"), ("PUT", "/bob/identity")]


@pytest.mark.parametrize("method,path", [("GET", "/bob"), *WRITES])
async def test_non_admin_refused(client, configured, method, path):
    session(client, configured, "bob")
    response = await client.request(method, URL + path, json={})
    assert response.status_code == 403


@pytest.mark.parametrize("method,path", WRITES)
async def test_writes_require_origin(client, method, path):
    response = await client.request(method, URL + path, json={}, headers={"origin": ""})
    assert response.status_code == 403


class TestGet:
    async def test_shape(self, client, configured):
        _manage(configured, "bob", "email_addresses")
        user_profiles.update_profile(configured._config.db_path, "bob",
                                     sms_phone_number="+15550100001", log_channel="tok1")
        data = (await client.get(URL + "/bob")).json()
        assert data["user_id"] == "bob"
        assert data["is_admin"] is False
        assert data["identity"]["email"] == "bob@example.com"
        assert data["identity"]["state"] == "passwordless"
        assert set(data["profile"]) == set(user_profiles.ADMIN_EDITABLE_FIELDS)
        assert data["profile"]["sms_phone_number"] == "+15550100001"
        assert data["channels"] == {"log_channel": "tok1", "alerts_channel": ""}
        assert data["managed"] == ["email_addresses"]
        options = data["options"]
        assert options["outbound_approval"] == ["", "off", "untrusted", "all"]
        assert options["outbound_approval_floor"] == "untrusted"
        assert options["sms_enabled"] is True and options["whatsapp_enabled"] is True
        assert options["skills"] and options["modules"]
        assert "password_hash" not in json.dumps(data)

    async def test_no_identity_and_admin_badge(self, client):
        alice = (await client.get(URL + "/alice")).json()
        carol = (await client.get(URL + "/carol")).json()
        assert alice["is_admin"] is True
        assert carol["identity"] is None

    async def test_missing_profile_is_404(self, client):
        assert (await client.get(URL + "/nobody")).status_code == 404

    async def test_whatsapp_status_unbound(self, client):
        data = (await client.get(URL + "/bob")).json()
        assert data["whatsapp"] == {"number": "", "status": "unbound", "identity": None,
                                    "provider": None, "last_seen_at": None}

    async def test_whatsapp_status_awaiting(self, client, configured):
        with db.get_db(configured._config.db_path) as conn:
            db.set_whatsapp_binding(conn, "bob", bootstrap_phone_number="+15550100002")
        data = (await client.get(URL + "/bob")).json()
        assert data["whatsapp"]["number"] == "+15550100002"
        assert data["whatsapp"]["status"] == "awaiting_first_message"
        assert data["whatsapp"]["identity"] is None

    async def test_whatsapp_status_enrolled_masks_identity(self, client, configured):
        jid = "15550100002@s.whatsapp.net"
        with db.get_db(configured._config.db_path) as conn:
            db.set_whatsapp_binding(conn, "bob", bootstrap_phone_number="+15550100002")
            conn.execute("UPDATE whatsapp_user_bindings SET jid = ?, provider = 'baileys' WHERE user_id = 'bob'", (jid,))
        data = (await client.get(URL + "/bob")).json()
        assert data["whatsapp"]["status"] == "enrolled"
        assert data["whatsapp"]["provider"] == "baileys"
        assert data["whatsapp"]["identity"] == user_profiles.mask_whatsapp_identifier(jid)
        assert jid not in json.dumps(data)

    async def test_whatsapp_status_opted_out_wins(self, client, configured):
        with db.get_db(configured._config.db_path) as conn:
            db.set_whatsapp_binding(conn, "bob", bootstrap_phone_number="+15550100002", bsuid="US.1")
            conn.execute("UPDATE whatsapp_user_bindings SET opted_out_at = '2026-01-01' WHERE user_id = 'bob'")
        data = (await client.get(URL + "/bob")).json()
        assert data["whatsapp"]["status"] == "opted_out"


class TestPatch:
    async def test_partial_write_touches_only_given_keys(self, client, configured):
        user_profiles.update_profile(configured._config.db_path, "bob", timezone="Europe/Warsaw",
                                     trusted_email_senders=["*@x.org"])
        response = await client.patch(URL + "/bob", json={"display_name": "Robert", "max_foreground_workers": 3})
        assert response.status_code == 200, response.text
        profile = _profile(configured, "bob")
        assert profile.display_name == "Robert"
        assert profile.max_foreground_workers == 3
        assert profile.timezone == "Europe/Warsaw"
        assert profile.trusted_email_senders == ["*@x.org"]
        assert response.json()["profile"]["display_name"] == "Robert"

    @pytest.mark.parametrize("body", [
        {"log_channel": "x"},
        {"routing": {}},
        {"nonsense": 1},
    ])
    async def test_unknown_or_self_only_key_is_400(self, client, body):
        response = await client.patch(URL + "/bob", json=body)
        assert response.status_code == 400
        assert "unknown field" in response.json()["detail"]

    @pytest.mark.parametrize("field,value", [
        ("sms_phone_number", "555-0100"),
        ("whatsapp_number", "0048123"),
        ("email_addresses", ["not-an-address"]),
        ("outbound_approval", "sometimes"),
        ("max_background_workers", -1),
    ])
    async def test_invalid_value_is_400_naming_the_field(self, client, field, value):
        response = await client.patch(URL + "/bob", json={field: value})
        assert response.status_code == 400
        assert response.json()["fields"] == [field]
        assert isinstance(response.json()["detail"], str)

    async def test_outbound_approval_is_floor_bounded(self, client, configured):
        response = await client.patch(URL + "/bob", json={"outbound_approval": "off"})
        assert response.status_code == 400
        assert "floor" in response.json()["detail"]
        assert (await client.patch(URL + "/bob", json={"outbound_approval": "all"})).status_code == 200
        assert (await client.patch(URL + "/bob", json={"outbound_approval": ""})).status_code == 200

    async def test_a_stored_value_below_the_floor_resubmitted_passes(self, client, configured):
        user_profiles.update_profile(configured._config.db_path, "bob", outbound_approval="off")
        response = await client.patch(URL + "/bob", json={"outbound_approval": "off", "display_name": "B"})
        assert response.status_code == 200

    async def test_managed_change_is_409_and_unchanged_resubmit_passes(self, client, configured):
        user_profiles.update_profile(configured._config.db_path, "bob", email_addresses=["bob@example.com"])
        _manage(configured, "bob", "email_addresses", "whatsapp_number")
        response = await client.patch(URL + "/bob", json={"email_addresses": ["other@example.com"]})
        assert response.status_code == 409
        assert response.json()["fields"] == ["email_addresses"]
        assert response.json()["error"] == "managed_by_provisioning"
        assert (await client.patch(URL + "/bob", json={"whatsapp_number": "+15550100009"})).status_code == 409
        response = await client.patch(URL + "/bob", json={"email_addresses": ["bob@example.com"], "display_name": "B"})
        assert response.status_code == 200
        assert _profile(configured, "bob").display_name == "B"

    async def test_email_held_in_another_users_addresses_is_409_naming_them(self, client, configured):
        user_profiles.update_profile(configured._config.db_path, "carol", email_addresses=["c@example.com"])
        response = await client.patch(URL + "/bob", json={"email_addresses": ["C@example.com"]})
        assert response.status_code == 409
        assert response.json()["fields"] == ["email_addresses"]
        assert "carol" in response.json()["detail"]

    async def test_email_held_as_another_users_login_is_409(self, client):
        response = await client.patch(URL + "/carol", json={"email_addresses": ["bob@example.com"]})
        assert response.status_code == 409
        assert "bob" in response.json()["detail"]

    async def test_duplicate_sms_is_409_naming_the_holder(self, client, configured):
        user_profiles.update_profile(configured._config.db_path, "carol", sms_phone_number="+15550100003")
        response = await client.patch(URL + "/bob", json={"sms_phone_number": "+15550100003"})
        assert response.status_code == 409
        assert response.json()["fields"] == ["sms_phone_number"]
        assert "carol" in response.json()["detail"]

    async def test_duplicate_whatsapp_is_409_naming_the_holder(self, client, configured):
        with db.get_db(configured._config.db_path) as conn:
            db.set_whatsapp_binding(conn, "carol", bootstrap_phone_number="+15550100004")
        response = await client.patch(URL + "/bob", json={"whatsapp_number": "+15550100004"})
        assert response.status_code == 409
        assert response.json()["fields"] == ["whatsapp_number"]
        assert "carol" in response.json()["detail"]

    async def test_same_number_for_sms_and_whatsapp_of_one_user_passes(self, client, configured):
        response = await client.patch(URL + "/bob", json={"sms_phone_number": "+15550100005",
                                                          "whatsapp_number": "+15550100005"})
        assert response.status_code == 200, response.text
        assert _binding(configured, "bob").bootstrap_phone_number == "+15550100005"

    async def test_one_bad_field_writes_nothing(self, client, configured):
        user_profiles.update_profile(configured._config.db_path, "carol", sms_phone_number="+15550100003")
        response = await client.patch(URL + "/bob", json={
            "display_name": "Changed", "whatsapp_number": "+15550100006",
            "sms_phone_number": "+15550100003",
        })
        assert response.status_code == 409
        assert _profile(configured, "bob").display_name == "Bob"
        assert _binding(configured, "bob") is None

    async def test_unchanged_whatsapp_resubmit_keeps_the_enrollment(self, client, configured):
        with db.get_db(configured._config.db_path) as conn:
            db.set_whatsapp_binding(conn, "bob", bootstrap_phone_number="+15550100007")
            conn.execute("UPDATE whatsapp_user_bindings SET jid = 'j@s.whatsapp.net', provider = 'baileys', "
                         "last_user_message_at = '2026-10-01 10:00:00' WHERE user_id = 'bob'")
        response = await client.patch(URL + "/bob", json={"whatsapp_number": "+15550100007"})
        assert response.status_code == 200
        binding = _binding(configured, "bob")
        assert binding.jid == "j@s.whatsapp.net"
        assert binding.last_user_message_at == "2026-10-01 10:00:00"

    async def test_changed_whatsapp_number_discards_the_enrollment(self, client, configured):
        with db.get_db(configured._config.db_path) as conn:
            db.set_whatsapp_binding(conn, "bob", bootstrap_phone_number="+15550100007")
            conn.execute("UPDATE whatsapp_user_bindings SET jid = 'j@s.whatsapp.net', "
                         "last_user_message_at = '2026-10-01 10:00:00' WHERE user_id = 'bob'")
        response = await client.patch(URL + "/bob", json={"whatsapp_number": "+15550100008"})
        assert response.status_code == 200
        binding = _binding(configured, "bob")
        assert binding.bootstrap_phone_number == "+15550100008"
        assert binding.jid == ""
        assert binding.last_user_message_at is None
        assert response.json()["whatsapp"]["status"] == "awaiting_first_message"

    async def test_empty_whatsapp_clears_the_binding(self, client, configured):
        with db.get_db(configured._config.db_path) as conn:
            db.set_whatsapp_binding(conn, "bob", bootstrap_phone_number="+15550100007")
        response = await client.patch(URL + "/bob", json={"whatsapp_number": ""})
        assert response.status_code == 200
        assert _binding(configured, "bob") is None
        assert response.json()["whatsapp"]["status"] == "unbound"

    async def test_missing_profile_is_404(self, client):
        assert (await client.patch(URL + "/nobody", json={"display_name": "x"})).status_code == 404

    async def test_log_line_names_fields_not_values(self, client, caplog):
        caplog.set_level(logging.INFO, logger="istota.webui.app")
        await client.patch(URL + "/bob", json={"sms_phone_number": "+15550100011"})
        lines = [r.getMessage() for r in caplog.records if "admin_user_update" in r.getMessage()]
        assert lines == ["admin_user_update admin=alice user=bob fields=['sms_phone_number'] managed_refused=[]"]
        assert "+15550100011" not in caplog.text

    async def test_conflict_log_names_kind_not_holder(self, client, configured, caplog):
        user_profiles.update_profile(configured._config.db_path, "carol", sms_phone_number="+15550100003")
        caplog.set_level(logging.INFO, logger="istota.webui.app")
        await client.patch(URL + "/bob", json={"sms_phone_number": "+15550100003"})
        lines = [r.getMessage() for r in caplog.records if "admin_user_conflict" in r.getMessage()]
        assert lines == ["admin_user_conflict admin=alice user=bob kind=sms"]


class TestNotifications:
    async def test_contact_change_of_another_user_notifies_them(self, client, configured, delivered):
        response = await client.patch(URL + "/bob", json={"sms_phone_number": "+15550100012",
                                                          "whatsapp_number": "+15550100013"})
        assert response.status_code == 200
        rows = _notifications(configured, "bob")
        assert len(rows) == 1
        assert rows[0]["title"] == "An administrator changed your contact details"
        assert "SMS number" in rows[0]["body"] and "WhatsApp number" in rows[0]["body"]
        assert "Alice" in rows[0]["body"]
        assert "+1555" not in rows[0]["body"]
        assert len(delivered) == 1

    async def test_admin_editing_their_own_row_is_not_notified(self, client, configured, delivered):
        response = await client.patch(URL + "/alice", json={"sms_phone_number": "+15550100014"})
        assert response.status_code == 200
        assert _notifications(configured) == []
        assert delivered == []

    async def test_non_contact_fields_do_not_notify(self, client, configured, delivered):
        response = await client.patch(URL + "/bob", json={"display_name": "R", "max_background_workers": 2,
                                                          "trusted_email_senders": ["*@y.org"]})
        assert response.status_code == 200
        assert _notifications(configured) == []

    async def test_unchanged_contact_resubmit_does_not_notify(self, client, configured, delivered):
        user_profiles.update_profile(configured._config.db_path, "bob", sms_phone_number="+15550100015")
        await client.patch(URL + "/bob", json={"sms_phone_number": "+15550100015"})
        assert _notifications(configured) == []

    async def test_resolver_renders_stored_text_and_never_none(self, client, configured, delivered):
        from istota.notifications import sources

        await client.patch(URL + "/bob", json={"email_addresses": ["robert@example.com"]})
        resolver = sources.get_resolver("admin_profile_change")
        assert resolver is not None and resolver.auto_resolve_on_seen is True
        row = _notifications(configured, "bob")[0]
        with db.get_db(configured._config.db_path) as conn:
            from istota.notifications.store import _row_to_notification

            stored = conn.execute("SELECT * FROM notifications WHERE id = ?", (row["id"],)).fetchone()
            view = resolver.resolve(configured._config, conn, _row_to_notification(stored))
        assert view is not None
        assert "email addresses" in view.body
        assert view.actions == () and view.link is None

    def test_dedup_key_is_bounded(self):
        from istota.notifications.resolvers import admin_profile_change as source

        assert source.dedup_key(["sms_phone_number", "../x", "email_addresses"]) == \
            "fields:email_addresses+sms_phone_number"


class TestWhatsAppReset:
    async def test_keeps_the_number_and_clears_the_identity(self, client, configured):
        with db.get_db(configured._config.db_path) as conn:
            db.set_whatsapp_binding(conn, "bob", bootstrap_phone_number="+15550100016", bsuid="US.2")
            conn.execute("UPDATE whatsapp_user_bindings SET opted_out_at = '2026-01-01' WHERE user_id = 'bob'")
        response = await client.post(URL + "/bob/whatsapp/reset")
        assert response.status_code == 200
        binding = _binding(configured, "bob")
        assert binding.bootstrap_phone_number == "+15550100016"
        assert binding.bsuid == "" and binding.opted_out_at is None
        assert response.json()["whatsapp"]["status"] == "awaiting_first_message"

    async def test_no_binding_is_400(self, client):
        assert (await client.post(URL + "/bob/whatsapp/reset")).status_code == 400


class TestIdentityPut:
    async def test_attach_with_address_append(self, client, configured, delivered):
        response = await client.put(URL + "/carol/identity",
                                    json={"email": " Carol@Example.com ", "add_to_addresses": True})
        assert response.status_code == 200, response.text
        assert web_auth.get_identity(configured._config.db_path, "carol").email == "carol@example.com"
        assert _profile(configured, "carol").email_addresses == ["carol@example.com"]
        assert response.json()["identity"]["email"] == "carol@example.com"
        body = _notifications(configured, "carol")[0]["body"]
        assert "login email" in body and "email addresses" in body

    async def test_change_bumps_epoch_and_voids_tokens(self, client, configured):
        path = configured._config.db_path
        before = web_auth.get_identity(path, "bob")
        token = web_auth.issue_token(path, "bob", "enrol", 3600)
        response = await client.put(URL + "/bob/identity", json={"email": "robert@example.com"})
        assert response.status_code == 200
        after = web_auth.get_identity(path, "bob")
        assert after.email == "robert@example.com"
        assert after.credential_epoch > before.credential_epoch
        assert web_auth.peek_token(path, token, "enrol") is None

    async def test_append_skipped_when_managed(self, client, configured):
        _manage(configured, "carol", "email_addresses")
        response = await client.put(URL + "/carol/identity",
                                    json={"email": "carol@example.com", "add_to_addresses": True})
        assert response.status_code == 200
        assert response.json()["addresses_skipped"] == "managed"
        assert _profile(configured, "carol").email_addresses == []

    async def test_login_held_by_another_user_is_400(self, client, configured):
        response = await client.put(URL + "/carol/identity", json={"email": "bob@example.com"})
        assert response.status_code == 400
        assert "bob" in response.json()["detail"]
        assert web_auth.get_identity(configured._config.db_path, "carol") is None

    async def test_address_held_in_another_users_addresses_is_400(self, client, configured):
        user_profiles.update_profile(configured._config.db_path, "bob", email_addresses=["shared@example.com"])
        response = await client.put(URL + "/carol/identity", json={"email": "shared@example.com"})
        assert response.status_code == 400
        assert "bob" in response.json()["detail"]
        assert web_auth.get_identity(configured._config.db_path, "carol") is None

    async def test_invite_failure_is_502_with_the_identity_kept(self, client, configured, monkeypatch):
        monkeypatch.setattr(configured.web_auth_mail, "send_auth_email", lambda *args: False)
        response = await client.put(URL + "/carol/identity", json={"email": "carol@example.com", "invite": True})
        assert response.status_code == 502
        assert "identity is saved" in response.json()["detail"]
        assert web_auth.get_identity(configured._config.db_path, "carol").email == "carol@example.com"

    async def test_invite_sends(self, client, configured, monkeypatch):
        mail = []
        monkeypatch.setattr(configured.web_auth_mail, "send_auth_email", lambda *args: mail.append(args) or True)
        response = await client.put(URL + "/carol/identity", json={"email": "carol@example.com", "invite": True})
        assert response.status_code == 200
        assert mail[0][1] == "carol@example.com"

    async def test_requires_email_sign_in(self, client, configured, monkeypatch):
        monkeypatch.setattr(configured._config, "web", WebConfig(auth=["nextcloud"]))
        response = await client.put(URL + "/carol/identity", json={"email": "carol@example.com"})
        assert response.status_code == 400

    async def test_missing_profile_is_404(self, client):
        response = await client.put(URL + "/nobody/identity", json={"email": "n@example.com"})
        assert response.status_code == 404


async def test_create_refuses_an_address_another_user_routes(client, configured):
    user_profiles.update_profile(configured._config.db_path, "bob", email_addresses=["shared@example.com"])
    response = await client.post(URL, json={"user_id": "dave", "email": "shared@example.com"})
    assert response.status_code == 400
    assert user_profiles.get_profile(configured._config.db_path, "dave") is None
