"""The config editor's web routes: preview, submit, status, history, cancel, restore.

Through the FastAPI app with a real database, a real `config.toml` on disk, the
requests directory in a temp dir, and the check subprocess replaced by a fake
(`tests/test_config_editor_schema.py` drives the real one).
"""

import hashlib
import json
import re
import stat
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

if _has_web_deps:
    from httpx import ASGITransport, AsyncClient

from istota import db, user_profiles
from istota.apply import CheckResult
from istota.config import Config, SiteConfig, UserConfig, WebConfig
from istota.config_editor import applier
from istota.config_editor import requests as config_requests
from istota.config_editor.secrets import SENTINEL
from istota.webui import auth as web_auth

pytestmark = pytest.mark.skipif(not _has_web_deps, reason="web dependencies not installed")

ORIGIN = {"Origin": "https://example.com"}
BASE = "/istota/api/admin/config"
FILE_SECRET = "fixture-file-credential-4c1e"
INLINE_SECRET = "fixture-inline-header-9b2d"
CURRENT = (
    '[email]\nimap_host = "old.example.org"\n\n'
    f'[brain.native.extra_headers]\nX-Harmless = "{INLINE_SECRET}"\n'
)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import istota.webui.app as mod
    from istota.config_editor import check

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.toml"
    config_file.write_text(CURRENT, encoding="utf-8")
    requests_dir = tmp_path / "requests"
    requests_dir.mkdir()
    monkeypatch.setenv(applier.APPLIER_ENV, "host")
    monkeypatch.setenv(applier.REQUESTS_DIR_ENV, str(requests_dir))
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)

    config = Config(
        db_path=tmp_path / "istota.db",
        workspace_path=tmp_path / "mount",
        site=SiteConfig(hostname="example.com"),
        users={"alice": UserConfig(display_name="Alice"), "bob": UserConfig(), "dave": UserConfig()},
        web=WebConfig(
            enabled=True, port=8766, oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web", oauth2_client_secret="test-secret",
            session_secret_key="test-session-key",
        ),
    )
    config.config_path = config_file
    config.admin_users = {"alice", "dave"}
    # The emailed-code step-up; `TestPasswordStepUp` turns mail off.
    config.email.enabled = True
    db.init_db(config.db_path)
    for user_id in ("alice", "bob", "carol"):
        user_profiles.ensure_profile(config.db_path, user_id, display_name=user_id.title())
        web_auth.upsert_identity(config.db_path, user_id, f"{user_id}@example.com")
    web_auth.set_disabled(config.db_path, "carol", True)

    checked: list[dict] = []

    async def fake_check(document, **kwargs):
        checked.append(document)
        return CheckResult()

    monkeypatch.setattr(check, "run_check", fake_check)
    check.forget_cached()
    mod._config = config
    mod.app.state.istota_config = config
    mod._oauth = MagicMock()
    sent: list[tuple] = []
    monkeypatch.setattr(mod.web_auth_mail, "send_auth_email", lambda *args: sent.append(args))
    return {"config": config, "file": config_file, "requests": requests_dir, "mail": sent, "checked": checked}


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


def _digest(setup) -> str:
    return hashlib.sha256(setup["file"].read_bytes()).hexdigest()


def _change(setup, **extra) -> dict:
    body = {
        "base_digest": _digest(setup),
        "ops": [{"op": "set", "path": ["email", "imap_host"], "value": "imap.example.org"}],
    }
    body.update(extra)
    return body


async def _step_up(client, setup) -> dict:
    response = await client.post(
        "/istota/api/settings/step-up", json={"action": "admin_config_change"}, headers=ORIGIN,
    )
    assert response.status_code == 200, response.text
    code = re.search(r"(?<![0-9])[0-9]{6}(?![0-9])", setup["mail"][-1][2])[0]
    return {"request_id": response.json()["request_id"], "code": code}


async def _submit(client, setup, **extra):
    body = _change(setup, **extra)
    if "step_up" not in body:
        # A new code retires the last one, so only ask when none was given.
        body["step_up"] = await _step_up(client, setup)
    return await client.post(f"{BASE}/requests", json=body, headers=ORIGIN)


class TestPasswordStepUp:
    """Without mail the admin confirms the change with their account password."""

    PASSWORD = "a long example passphrase"

    @pytest.fixture
    def no_mail(self, setup, monkeypatch):
        monkeypatch.setattr(web_auth, "_N", 1024)
        setup["config"].email.enabled = False
        with db.get_db(setup["config"].db_path) as conn:
            conn.execute("UPDATE web_auth_identities SET password_hash=? WHERE user_id='alice'",
                         (web_auth.hash_password(self.PASSWORD),))
        return setup

    async def _start(self, client):
        response = await client.post(
            "/istota/api/settings/step-up", json={"action": "admin_config_change"}, headers=ORIGIN,
        )
        assert response.status_code == 200, response.text
        assert response.json()["method"] == "password"
        return response.json()["request_id"]

    async def test_the_password_submits_the_change(self, alice, no_mail):
        missing = await alice.post(f"{BASE}/requests", json=_change(no_mail), headers=ORIGIN)
        assert missing.status_code == 403
        assert missing.json()["field"] == "password"
        request_id = await self._start(alice)
        wrong = await alice.post(f"{BASE}/requests", headers=ORIGIN, json=_change(
            no_mail, step_up={"request_id": request_id, "password": "not the password"}))
        assert wrong.status_code == 403
        assert wrong.json()["field"] == "password"
        assert wrong.json()["reason"] == "bad"
        with db.get_db(no_mail["config"].db_path) as conn:
            assert config_requests.list_recent(conn) == []
        response = await alice.post(f"{BASE}/requests", headers=ORIGIN, json=_change(
            no_mail, step_up={"request_id": request_id, "password": self.PASSWORD}))
        assert response.status_code == 202, response.text
        assert no_mail["mail"] == []
        assert self.PASSWORD not in json.dumps(response.json())


class TestTheGates:
    async def test_a_non_admin_is_refused_every_route(self, setup):
        client = await _client("bob")
        try:
            for method, path in (
                ("post", f"{BASE}/preview"), ("post", f"{BASE}/requests"),
                ("get", f"{BASE}/requests"), ("get", f"{BASE}/requests/1"),
                ("post", f"{BASE}/requests/1/cancel"), ("post", f"{BASE}/requests/1/restore"),
            ):
                kwargs = {"json": _change(setup), "headers": ORIGIN} if method == "post" else {}
                response = await getattr(client, method)(path, **kwargs)
                assert response.status_code == 403, path
            refused = await client.post(
                "/istota/api/settings/step-up", json={"action": "admin_config_change"}, headers=ORIGIN,
            )
            assert refused.status_code == 403
        finally:
            await client.aclose()

    async def test_a_post_without_an_origin_is_refused(self, alice, setup):
        assert (await alice.post(f"{BASE}/preview", json=_change(setup))).status_code == 403
        assert (await alice.post(f"{BASE}/requests", json=_change(setup))).status_code == 403

    async def test_an_admin_with_no_identity_is_refused_at_preview_and_submit(self, setup):
        client = await _client("dave")
        try:
            for path in (f"{BASE}/preview", f"{BASE}/requests"):
                response = await client.post(path, json=_change(setup), headers=ORIGIN)
                assert response.status_code == 403
                assert response.json()["error"] == "admin_identity_required"
        finally:
            await client.aclose()

    async def test_every_response_is_no_store(self, alice, setup):
        submitted = await _submit(alice, setup)
        request_id = submitted.json()["id"]
        responses = [
            await alice.get(BASE),
            await alice.post(f"{BASE}/preview", json=_change(setup), headers=ORIGIN),
            submitted,
            await alice.get(f"{BASE}/requests"),
            await alice.get(f"{BASE}/requests/{request_id}"),
            await alice.post(f"{BASE}/requests/{request_id}/restore", headers=ORIGIN),
            await alice.post(f"{BASE}/requests/{request_id}/cancel", headers=ORIGIN),
            await alice.get(f"{BASE}/requests/999"),
        ]
        for response in responses:
            assert response.headers.get("cache-control") == "no-store", response.request.url


class TestTheView:
    async def test_the_view_is_editable_with_the_applier(self, alice, setup):
        import istota.webui.app as mod

        mod._started_at = "2026-10-12T08:00:00Z"
        body = (await alice.get(BASE)).json()
        assert body["editable"] is True
        assert body["applier"] == {"kind": "host", "available": True, "reason": ""}
        assert body["started_at"] == "2026-10-12T08:00:00Z"
        assert body["document_digest"] == _digest(setup)

    async def test_without_the_variable_the_view_is_read_only(self, alice, setup, monkeypatch):
        monkeypatch.delenv(applier.APPLIER_ENV)
        body = (await alice.get(BASE)).json()
        assert body["editable"] is False
        assert body["applier"]["kind"] == "none"
        assert "istota-stack apply" in body["applier"]["reason"]


class TestDetect:
    def test_none_without_the_variable(self, tmp_path):
        found = applier.detect(environ={applier.REQUESTS_DIR_ENV: str(tmp_path)})
        assert found == {"kind": "none", "available": False, "reason": applier.NO_APPLIER_REASON}

    def test_none_without_the_directory(self, tmp_path):
        found = applier.detect(environ={
            applier.APPLIER_ENV: "host", applier.REQUESTS_DIR_ENV: str(tmp_path / "missing"),
        })
        assert found["kind"] == "none"
        assert found["reason"] == applier.NOT_RESTARTED_REASON

    def test_host_with_both(self, tmp_path):
        found = applier.detect(environ={applier.APPLIER_ENV: "host", applier.REQUESTS_DIR_ENV: str(tmp_path)})
        assert found == {"kind": "host", "available": True, "reason": ""}

    def test_read_managed_fails_closed(self, tmp_path):
        (tmp_path / "managed.json").write_text("{damaged", encoding="utf-8")
        assert applier.read_managed(tmp_path)["config"] == {"managed": True, "source": "unreadable"}


class TestPreview:
    async def test_a_preview_shows_the_change_and_stores_nothing(self, alice, setup):
        response = await alice.post(f"{BASE}/preview", json=_change(setup), headers=ORIGIN)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["changed"] is True
        assert body["changed_sections"] == ["email"]
        assert body["base_digest"] == _digest(setup)
        assert any(line.startswith('+imap_host = "imap.example.org"') for line in body["diff"])
        with db.get_db(setup["config"].db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM config_change_requests").fetchone()[0] == 0

    async def test_a_date_is_refused_with_its_label(self, alice, setup):
        response = await alice.post(f"{BASE}/preview", json={
            "base_digest": _digest(setup), "raw": "when = 1979-05-27\n" + CURRENT.replace(INLINE_SECRET, SENTINEL),
        }, headers=ORIGIN)
        assert response.status_code == 200
        assert any(error.startswith("when holds a date or time value") for error in response.json()["errors"])

    async def test_an_unreadable_change_is_400_without_the_input(self, alice, setup):
        body = _change(setup, ops=[{"op": "set", "path": "email.imap_host", "value": "marker-xyz"}])
        response = await alice.post(f"{BASE}/preview", json=body, headers=ORIGIN)
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_change"
        assert "marker-xyz" not in response.text

    async def test_a_body_that_is_not_json_or_too_large(self, alice, setup):
        response = await alice.post(f"{BASE}/preview", content=b"{not json marker-abc", headers=ORIGIN)
        assert response.status_code == 400
        assert "marker-abc" not in response.text
        big = json.dumps({"raw": "x" * (256 * 1024)})
        response = await alice.post(f"{BASE}/preview", content=big, headers=ORIGIN)
        assert response.status_code == 413
        assert response.json()["error"] == "too_large"

    async def test_an_unknown_field_is_refused(self, alice, setup):
        response = await alice.post(f"{BASE}/preview", json=_change(setup, extra="marker"), headers=ORIGIN)
        assert response.status_code == 400
        assert "marker" not in response.text


class TestSubmit:
    async def test_a_submit_stores_the_request_and_writes_only_its_id(self, alice, setup):
        response = await _submit(alice, setup)
        assert response.status_code == 202, response.text
        request_id = response.json()["id"]
        trigger = setup["requests"] / applier.TRIGGER_NAME
        assert trigger.read_text() == f"{request_id}\n"
        assert stat.S_IMODE(trigger.stat().st_mode) == 0o600
        status = (await alice.get(f"{BASE}/requests/{request_id}")).json()
        assert status["status"] == "pending"
        assert status["admin_id"] == "alice"

    async def test_step_up_is_required_on_every_submit(self, alice, setup):
        response = await alice.post(f"{BASE}/requests", json=_change(setup), headers=ORIGIN)
        assert response.status_code == 403
        assert response.json()["field"] == "code"
        proof = await _step_up(alice, setup)
        wrong = {**proof, "code": "000000" if proof["code"] != "000000" else "111111"}
        response = await alice.post(f"{BASE}/requests", json=_change(setup, step_up=wrong), headers=ORIGIN)
        assert response.status_code == 403
        assert (await _submit(alice, setup)).status_code == 202

    async def test_no_applier(self, alice, setup, monkeypatch):
        monkeypatch.delenv(applier.APPLIER_ENV)
        response = await _submit(alice, setup, step_up={})
        assert response.status_code == 409
        assert response.json()["error"] == "no_applier"

    async def test_config_changed(self, alice, setup):
        response = await _submit(alice, setup, base_digest="0" * 64, step_up={})
        assert response.status_code == 409
        assert response.json() | {"detail": None} == {
            "error": "config_changed", "detail": None, "digest": _digest(setup),
        }

    async def test_a_request_already_open(self, alice, setup):
        first = await _submit(alice, setup)
        response = await alice.post(f"{BASE}/requests", json=_change(setup), headers=ORIGIN)
        assert response.status_code == 409
        assert response.json()["error"] == "request_open"
        assert response.json()["id"] == first.json()["id"]

    async def test_a_managed_resource(self, alice, setup):
        (setup["file"].parent / "managed.json").write_text(
            json.dumps({"config": {"managed": True, "source": "ansible"}}), encoding="utf-8",
        )
        response = await _submit(alice, setup, step_up={})
        assert response.status_code == 409
        assert response.json()["error"] == "managed"
        assert response.json()["resources"] == ["config"]
        secret_only = _change(setup, ops=[], secrets={"istota_email_imap_password": FILE_SECRET})
        response = await alice.post(f"{BASE}/preview", json=secret_only, headers=ORIGIN)
        assert response.status_code == 200

    async def test_a_risky_key_needs_its_confirmation(self, alice, setup):
        ops = [{"op": "set", "path": ["security", "sandbox_enabled"], "value": False}]
        response = await _submit(alice, setup, ops=ops, step_up={})
        assert response.status_code == 409
        assert response.json()["error"] == "confirm_required"
        assert response.json()["keys"] == ["security.sandbox_enabled"]
        response = await _submit(alice, setup, ops=ops, confirm=["security.sandbox_enabled"])
        assert response.status_code == 202, response.text

    async def test_sign_in_without_email_is_refused(self, alice, setup):
        ops = [{"op": "set", "path": ["web", "auth"], "value": ["nextcloud"]}]
        response = await _submit(alice, setup, ops=ops, confirm=["web.auth"], step_up={})
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_change"
        assert any("must keep email" in error for error in response.json()["errors"])

    @pytest.mark.parametrize("admins", [["alice", "bob-has-no-identity"], ["alice", "carol"], []])
    async def test_an_admin_list_with_no_live_identity_or_empty_is_refused(self, alice, setup, admins):
        response = await _submit(alice, setup, ops=[], admins=admins, confirm=["admins"], step_up={})
        assert response.status_code == 400
        assert response.json()["errors"]

    async def test_an_admin_list_change_is_accepted(self, alice, setup):
        response = await _submit(alice, setup, ops=[], admins=["alice", "bob"], confirm=["admins"])
        assert response.status_code == 202, response.text

    async def test_nothing_to_change(self, alice, setup):
        response = await _submit(alice, setup, ops=[], step_up={})
        assert response.status_code == 400
        assert response.json()["errors"] == ["the change set changes nothing"]

    async def test_without_the_master_key_nothing_is_stored(self, alice, setup, monkeypatch):
        proof = await _step_up(alice, setup)
        monkeypatch.delenv("ISTOTA_SECRET_KEY")
        response = await _submit(
            alice, setup, secrets={"istota_email_imap_password": FILE_SECRET}, step_up=proof,
        )
        assert response.status_code == 503, response.text
        assert response.json()["error"] == "secret_key_unavailable"
        with db.get_db(setup["config"].db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM config_change_requests").fetchone()[0] == 0


class TestTheRequestAfterwards:
    async def test_list_status_and_cancel(self, alice, setup):
        request_id = (await _submit(alice, setup)).json()["id"]
        listed = (await alice.get(f"{BASE}/requests")).json()["requests"]
        assert [row["id"] for row in listed] == [request_id]
        assert listed[0]["changed_sections"] == ["email"]
        cancelled = await alice.post(f"{BASE}/requests/{request_id}/cancel", headers=ORIGIN)
        assert cancelled.status_code == 200
        again = await alice.post(f"{BASE}/requests/{request_id}/cancel", headers=ORIGIN)
        assert again.status_code == 409 and again.json()["error"] == "not_pending"
        assert (await alice.get(f"{BASE}/requests/{request_id}")).json()["status"] == "cancelled"
        assert (await alice.get(f"{BASE}/requests/not-a-number")).status_code == 404

    async def test_restore_drops_a_credential_cleared_since_and_submits(self, alice, setup):
        request_id = (await _submit(alice, setup)).json()["id"]
        with db.get_db(setup["config"].db_path) as conn:
            config_requests.cancel(conn, request_id)
            conn.execute(
                "UPDATE config_change_requests SET document_toml = ? WHERE id = ?",
                (
                    '[email]\nimap_host = "restored.example.org"\n\n'
                    f'[brain.native.extra_headers]\nX-Harmless = "{SENTINEL}"\nX-Gone = "{SENTINEL}"\n',
                    request_id,
                ),
            )
        response = await alice.post(f"{BASE}/requests/{request_id}/restore", headers=ORIGIN)
        assert response.status_code == 200, response.text
        body = response.json()
        warnings = body["preview"]["problems"]["warnings"]
        assert any("X-Gone was cleared after this change" in problem["message"] for problem in warnings)
        assert "X-Gone" not in body["change_set"]["raw"]
        assert "credentials and the admin list stay as they are now" in body["note"]
        submitted = await alice.post(f"{BASE}/requests", json={
            **body["change_set"], "step_up": await _step_up(alice, setup),
        }, headers=ORIGIN)
        assert submitted.status_code == 202, submitted.text
        assert setup["checked"][-1]["brain"]["native"]["extra_headers"] == {"X-Harmless": INLINE_SECRET}


async def test_no_credential_value_in_any_response(alice, setup):
    """Sentinel-by-value across preview, submit, status, history and restore."""
    secrets = {"istota_email_imap_password": FILE_SECRET, "brain.native.extra_headers.X-New": "fixture-new-header-7e"}
    seen = [await alice.post(f"{BASE}/preview", json=_change(setup, secrets=secrets), headers=ORIGIN)]
    submitted = await _submit(alice, setup, secrets=secrets)
    assert submitted.status_code == 202, submitted.text
    request_id = submitted.json()["id"]
    seen += [
        submitted,
        await alice.get(f"{BASE}/requests/{request_id}"),
        await alice.get(f"{BASE}/requests"),
        await alice.post(f"{BASE}/requests/{request_id}/restore", headers=ORIGIN),
        await alice.get(BASE),
    ]
    for response in seen:
        for value in (FILE_SECRET, INLINE_SECRET, "fixture-new-header-7e"):
            assert value not in response.text, response.request.url
    trigger = (setup["requests"] / applier.TRIGGER_NAME).read_text()
    assert trigger == f"{request_id}\n"
