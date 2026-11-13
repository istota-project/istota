"""`POST /admin/config/backup`: the encrypted installation backup.

Through the FastAPI app with the config editor's fixture (a real database, a
real `config.toml`, the emailed-code step-up), a real secret directory and a
test-sized key derivation. Every refusal that does not need the work comes
before the step-up, so its code survives; nothing secret leaves the encrypted
bytes, and only a finished export is audited and announced.
"""

import base64
import io
import json
import logging
import re

import pytest

from istota import db
from istota.config_editor import backup
from istota.webui import auth as web_auth
from tests.test_kdbx_export import cheap, keyfile  # noqa: F401
from tests.test_web_admin_config_edit import (  # noqa: F401
    BASE, INLINE_SECRET, ORIGIN, _client, _submit, alice, setup,
)

# ruff: noqa: F811

pykeepass = pytest.importorskip("pykeepass")

BACKUP = f"{BASE}/backup"
FILE_VALUE = "fixture-backup-imap-password-71ae"
EXPORT_PASSWORD = "SENTINEL-config-backup-password-2b9e"


@pytest.fixture
def install(setup, tmp_path, monkeypatch, cheap):
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "istota_email_imap_password").write_text(FILE_VALUE + "\n", encoding="utf-8")
    monkeypatch.setenv("ISTOTA_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setattr(cheap, "generate_export_password", lambda: EXPORT_PASSWORD)
    return {**setup, "secrets": secrets_dir}


async def _proof(client, setup) -> dict:
    response = await client.post(
        "/istota/api/settings/step-up", json={"action": "admin_config_backup"}, headers=ORIGIN,
    )
    assert response.status_code == 200, response.text
    code = re.search(r"(?<![0-9])[0-9]{6}(?![0-9])", setup["mail"][-1][2])[0]
    return {"request_id": response.json()["request_id"], "code": code}


def _exports(setup) -> list:
    with db.get_db(setup["config"].db_path) as conn:
        return conn.execute(
            "SELECT user_id, detail_json FROM credential_audit WHERE action='export'"
        ).fetchall()


def _notices(setup) -> list:
    with db.get_db(setup["config"].db_path) as conn:
        return conn.execute(
            "SELECT user_id, title, body FROM notifications WHERE dedup_key LIKE 'config-backup-exported:%' "
            "ORDER BY user_id"
        ).fetchall()


def _open(body, key=None):
    from pykeepass import PyKeePass

    return PyKeePass(
        io.BytesIO(base64.b64decode(body["file"])), password=body["password"],
        keyfile=io.BytesIO(key) if key is not None else None,
    )


class TestTheExport:
    async def test_a_step_up_downloads_the_installation_and_announces_it(self, alice, install, caplog):
        caplog.set_level(logging.INFO)
        proof = await _proof(alice, install)
        response = await alice.post(BACKUP, json={"step_up": proof}, headers=ORIGIN)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert set(body) == {"filename", "file", "password", "summary"}
        assert re.fullmatch(r"istota-config-\d{8}T\d{6}Z\.kdbx", body["filename"])
        assert body["summary"]["file_credentials"] == 2

        kp = _open(body)
        values = [entry.password or "" for entry in kp.entries]
        assert FILE_VALUE in values
        # The inline header table goes to its secret file as JSON.
        assert any(INLINE_SECRET in value for value in values)

        # Nothing secret outside the encrypted bytes: not the response's other
        # fields, the logs, the database, or the mail.
        public = json.dumps({key: value for key, value in body.items() if key != "file"})
        for secret in (FILE_VALUE, INLINE_SECRET):
            assert secret not in public
            assert secret not in caplog.text
        assert EXPORT_PASSWORD not in caplog.text + str(install["mail"])
        with db.get_db(install["config"].db_path) as conn:
            dump = "\n".join(conn.iterdump())
        for secret in (FILE_VALUE, INLINE_SECRET, EXPORT_PASSWORD):
            assert secret not in dump

        [(user_id, detail)] = _exports(install)
        assert user_id == "alice"
        assert json.loads(detail)["scope"] == "installation_config"
        # The exporting admin and the other admin, nobody else.
        assert [row[0] for row in _notices(install)] == ["alice", "dave"]
        assert "result=ok" in caplog.text

        # The code was spent.
        again = await alice.post(BACKUP, json={"step_up": proof}, headers=ORIGIN)
        assert again.status_code == 403

    async def test_a_key_file_is_required_to_open_the_backup(self, alice, install):
        key = keyfile()
        response = await alice.post(BACKUP, headers=ORIGIN, json={
            "keyfile": base64.b64encode(key).decode(), "step_up": await _proof(alice, install),
        })
        assert response.status_code == 200, response.text
        body = response.json()
        assert FILE_VALUE in {entry.password for entry in _open(body, key).entries}
        with pytest.raises(Exception):
            _open(body)

    async def test_the_view_advertises_the_limit_and_preamble(self, alice, install):
        view = (await alice.get(BASE)).json()
        assert view["backup_limits"] == {
            "config_bytes": backup.CONFIG_MAX_BYTES, "config_preamble": backup.CONFIG_PREAMBLE,
        }
        assert view["backup_encrypted_available"] is True


class TestRefusalsBeforeTheStepUp:
    """Each of these leaves the code usable, then the same code succeeds."""

    async def _refused_then_used(self, alice, install, body, status, error):
        proof = await _proof(alice, install)
        response = await alice.post(BACKUP, json={**body, "step_up": proof}, headers=ORIGIN)
        assert response.status_code == status, response.text
        assert response.json()["error"] == error
        assert response.headers["cache-control"] == "no-store"
        assert "password" not in response.json() and "file" not in response.json()
        assert _exports(install) == []
        return proof

    async def test_a_key_file_that_is_not_strict_base64(self, alice, install):
        proof = await self._refused_then_used(alice, install, {"keyfile": "not base64!"}, 400, "keyfile_invalid")
        assert (await alice.post(BACKUP, json={"step_up": proof}, headers=ORIGIN)).status_code == 200

    async def test_base64_that_is_not_a_key_file(self, alice, install):
        await self._refused_then_used(
            alice, install, {"keyfile": base64.b64encode(b"<KeyFile/>").decode()}, 400, "keyfile_invalid",
        )

    async def test_an_unexpected_field(self, alice, install):
        await self._refused_then_used(alice, install, {"password": "chosen"}, 400, "invalid_body")

    async def test_the_shared_export_limit(self, alice, install):
        install["config"].security.credential_exports_per_day = 1
        with db.get_db(install["config"].db_path) as conn:
            # A vault export by the same admin counts against the same limit.
            conn.execute(
                "INSERT INTO credential_audit (user_id, action, actor) VALUES ('alice', 'export', 'web:alice')"
            )
        proof = await _proof(alice, install)
        response = await alice.post(BACKUP, json={"step_up": proof}, headers=ORIGIN)
        assert response.status_code == 429
        assert response.json()["error"] == "export_rate_limited"
        assert response.json()["next_allowed_at"]
        install["config"].security.credential_exports_per_day = 2
        assert (await alice.post(BACKUP, json={"step_up": proof}, headers=ORIGIN)).status_code == 200

    async def test_an_open_change_request(self, alice, install):
        assert (await _submit(alice, install)).status_code == 202
        await self._refused_then_used(alice, install, {}, 409, "request_open")

    async def test_no_vault_extra(self, alice, install, monkeypatch):
        monkeypatch.setattr(backup, "encrypted_backup_available", lambda: False)
        await self._refused_then_used(alice, install, {}, 503, "vault_unavailable")


class TestTheGates:
    async def test_a_non_admin_is_refused_the_route_and_the_step_up(self, install):
        client = await _client("bob")
        try:
            assert (await client.post(BACKUP, json={}, headers=ORIGIN)).status_code == 403
            refused = await client.post(
                "/istota/api/settings/step-up", json={"action": "admin_config_backup"}, headers=ORIGIN,
            )
            assert refused.status_code == 403
        finally:
            await client.aclose()

    async def test_no_origin_is_refused(self, alice, install):
        assert (await alice.post(BACKUP, json={})).status_code == 403

    async def test_an_admin_with_no_live_identity_is_refused(self, install):
        client = await _client("dave")
        try:
            response = await client.post(BACKUP, json={}, headers=ORIGIN)
            assert response.status_code == 403
            assert response.json()["error"] == "admin_identity_required"
            assert response.headers["cache-control"] == "no-store"
        finally:
            await client.aclose()

    async def test_no_step_up_is_refused_and_records_nothing(self, alice, install):
        response = await alice.post(BACKUP, json={}, headers=ORIGIN)
        assert response.status_code == 403
        assert response.json()["field"] == "code"
        assert _exports(install) == [] and _notices(install) == []

    async def test_the_password_confirms_it_where_mail_can_send(self, alice, install, monkeypatch):
        """Mail that is configured but not delivered must not leave an admin
        unable to make the recovery file: the password is offered first, as
        for the other admin actions, and the code stays the alternative."""
        password = "a long example passphrase"
        monkeypatch.setattr(web_auth, "_N", 1024)
        with db.get_db(install["config"].db_path) as conn:
            conn.execute("UPDATE web_auth_identities SET password_hash=? WHERE user_id='alice'",
                         (web_auth.hash_password(password),))
        mailed = len(install["mail"])
        started = await alice.post(
            "/istota/api/settings/step-up", json={"action": "admin_config_backup"}, headers=ORIGIN,
        )
        assert started.status_code == 200, started.text
        assert started.json()["method"] == "password"
        assert started.json()["methods"] == ["password", "email"]
        request_id = started.json()["request_id"]
        wrong = await alice.post(BACKUP, headers=ORIGIN, json={
            "step_up": {"request_id": request_id, "password": "not the password"}})
        assert wrong.status_code == 403
        assert wrong.json()["field"] == "password" and wrong.json()["reason"] == "bad"
        assert _exports(install) == []
        response = await alice.post(BACKUP, headers=ORIGIN, json={
            "step_up": {"request_id": request_id, "password": password}})
        assert response.status_code == 200, response.text
        assert FILE_VALUE in {entry.password for entry in _open(response.json()).entries}
        assert password not in response.text
        assert len(install["mail"]) == mailed
        assert len(_exports(install)) == 1


class TestRefusalsAfterTheStepUp:
    async def test_an_oversized_configuration_returns_no_file_and_no_record(self, alice, install):
        install["file"].write_text(
            f'[email]\nimap_host = "{"x" * (backup.CONFIG_MAX_BYTES + 1)}"\n', encoding="utf-8",
        )
        response = await alice.post(BACKUP, json={"step_up": await _proof(alice, install)}, headers=ORIGIN)
        assert response.status_code == 413
        assert response.json()["error"] == "too_large"
        assert str(backup.CONFIG_MAX_BYTES) in response.json()["detail"]
        assert "file" not in response.json() and "password" not in response.json()
        assert _exports(install) == [] and _notices(install) == []

    async def test_a_symlinked_secret_file_refuses_without_naming_its_value(self, alice, install, tmp_path):
        target = tmp_path / "elsewhere"
        target.write_text("fixture-symlink-target-value", encoding="utf-8")
        (install["secrets"] / "istota_email_smtp_password").symlink_to(target)
        response = await alice.post(BACKUP, json={"step_up": await _proof(alice, install)}, headers=ORIGIN)
        assert response.status_code == 409
        assert response.json()["error"] == "unreadable_secret"
        assert "fixture-symlink-target-value" not in response.text
        assert _exports(install) == []

    async def test_a_busy_export_slot(self, alice, install, cheap, monkeypatch):
        class Busy:
            def acquire(self, timeout):
                assert timeout == 30
                return False

        monkeypatch.setattr(cheap, "EXPORT_SLOT", Busy())
        response = await alice.post(BACKUP, json={"step_up": await _proof(alice, install)}, headers=ORIGIN)
        assert response.status_code == 503
        assert response.json()["error"] == "backup_busy"
        assert _exports(install) == []

    async def test_an_unexpected_failure_names_no_detail(self, alice, install, monkeypatch, caplog):
        def broken(*args, **kwargs):
            raise RuntimeError(f"failure mentioning {FILE_VALUE}")

        monkeypatch.setattr(backup, "build_installation_kdbx", broken)
        response = await alice.post(BACKUP, json={"step_up": await _proof(alice, install)}, headers=ORIGIN)
        assert response.status_code == 500
        assert FILE_VALUE not in response.text + caplog.text
        assert _exports(install) == [] and _notices(install) == []


def test_the_secret_directory_follows_the_launcher():
    assert str(backup.deployment_secrets_dir({})) == "/run/secrets"
    assert str(backup.deployment_secrets_dir({"ISTOTA_SECRETS_DIR": ""})) == "/run/secrets"
    assert str(backup.deployment_secrets_dir({"ISTOTA_SECRETS_DIR": "/srv/s"})) == "/srv/s"


def test_the_backup_action_is_an_admin_step_up():
    import istota.webui.app as mod

    assert "admin_config_backup" in web_auth.STEP_UP_ACTIONS
    assert "admin_config_backup" in mod._ADMIN_STEP_UP_ACTIONS
    assert "admin_config_backup" in mod._PASSWORD_STEP_UP_ACTIONS
