"""`POST /admin/config/restore/preview` and `/restore/requests`: restoring an
uploaded installation backup through the config editor's request path.

Through the FastAPI app with the config editor's fixture (a real database, a
real `config.toml`, the emailed-code step-up), a real secret directory, a
test-sized key derivation and the real decrypt worker. The round trip ends in
`requests.take` and `handoff.build`, the host's half; restarting is the VM
tier's. Every refusal before the step-up leaves its code usable, and no
credential value or restore password is in a response, a log or a public
request column.
"""

import asyncio
import base64
import json
import logging
import subprocess
import sys
import tomllib

import pytest

from istota import db
from istota.config_editor import applier, backup, handoff
from istota.config_editor import check as config_check
from istota.config_editor import requests as config_requests
from istota.credentials import store
from tests.test_kdbx_export import cheap, keyfile  # noqa: F401
from tests.test_web_admin_config_edit import (  # noqa: F401
    _REAL_RUN_CHECK, BASE, CURRENT, INLINE_SECRET, ORIGIN, _client, _digest, _step_up, _submit,
    alice, setup,
)
from tests.test_web_config_backup import FILE_VALUE, install  # noqa: F401

# ruff: noqa: F811

pytest.importorskip("pykeepass")

PREVIEW = f"{BASE}/restore/preview"
SUBMIT = f"{BASE}/restore/requests"
RESTORE_PASSWORD = "SENTINEL-restore-password-6d0a"
MAP_KEY = "fixture-restore-map-key-8c3f"
ROTATED = "fixture-rotated-imap-password-1b7e"
NC_PASSWORD = "fixture-nc-app-password-5a2c"
IMAP = "file:istota_email_imap_password"
HEADERS = "file:istota_brain_native_extra_headers"
MAP = "inline:web.map.api_key"
NEXTCLOUD = "file:istota_nextcloud_app_password"
VALUES = (FILE_VALUE, INLINE_SECRET, MAP_KEY, ROTATED, NC_PASSWORD, RESTORE_PASSWORD)


@pytest.fixture(autouse=True)
def restore_limit(monkeypatch):
    """A fresh per-admin restore budget for each test, which the module-level
    window would otherwise carry from one test to the next."""
    import istota.webui.app as mod
    from istota.lib.rate_limit import SlidingWindow

    window = SlidingWindow(mod._CONFIG_RESTORE_WINDOW_SECONDS, mod._CONFIG_RESTORE_MAX_PER_ADMIN)
    monkeypatch.setattr(mod, "_config_restore_window", window)
    return window


@pytest.fixture
def archive(install):
    """A backup of the installation as it was, then the installation moved on:
    a setting changed, the map key removed and the IMAP password rotated."""
    install["file"].write_text(CURRENT + f'\n[web.map]\napi_key = "{MAP_KEY}"\n', encoding="utf-8")
    (install["secrets"] / "istota_nextcloud_app_password").write_text(NC_PASSWORD, encoding="utf-8")
    bundle = backup.gather_backup(install["file"], install["secrets"], environ={})
    data = backup.build_installation_kdbx(bundle, password=RESTORE_PASSWORD, keyfile=None)
    install["file"].write_text(CURRENT.replace("old.example.org", "new.example.org"), encoding="utf-8")
    (install["secrets"] / "istota_email_imap_password").write_text(ROTATED, encoding="utf-8")
    return {**install, "kdbx": data, "toml": bundle.config_toml}


def _body(setup, data, *, fmt="kdbx", **extra) -> dict:
    body = {
        "format": fmt, "file": base64.b64encode(data).decode("ascii"),
        "base_digest": _digest(setup), "restore_config": True, "selected": [],
    }
    if fmt == "kdbx":
        body["password"] = RESTORE_PASSWORD
    body.update(extra)
    return body


async def _preview(client, setup, **extra):
    data = extra.pop("data", setup["kdbx"])
    return await client.post(PREVIEW, json=_body(setup, data, **extra), headers=ORIGIN)


async def _reviewed(client, setup, **extra) -> dict:
    """A preview, then the body its submit sends."""
    response = await _preview(client, setup, **extra)
    assert response.status_code == 200, response.text
    reviewed = response.json()
    data = extra.pop("data", setup["kdbx"])
    return {
        **_body(setup, data, **extra),
        "input_digest": reviewed["input_digest"], "preview_digest": reviewed["preview_digest"],
    }


def _no_value_in(text: str) -> None:
    for value in VALUES:
        assert value not in text


def _rows(setup) -> list[dict]:
    with db.get_db(setup["config"].db_path) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM config_change_requests").fetchall()]


class TestTheRoundTrip:
    async def test_selected_credentials_reach_sealed_storage_and_the_host_handoff(
        self, alice, archive, monkeypatch, caplog,
    ):
        caplog.set_level(logging.INFO)
        # The real check child, once, on a restored candidate.
        monkeypatch.setattr(config_check, "run_check", _REAL_RUN_CHECK)
        seen = []
        response = await _preview(alice, archive, selected=[IMAP, MAP])
        seen.append(response)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert set(body) == {"preview", "input_digest", "preview_digest", "credentials", "note"}
        statuses = {row["id"]: row["status"] for row in body["credentials"]}
        assert statuses == {IMAP: "changed", HEADERS: "unchanged", MAP: "new", NEXTCLOUD: "host_only"}
        preview = body["preview"]
        assert preview["errors"] == [] and preview["problems"]["blocking"] == []
        assert preview["changed"] is True
        assert set(preview["secret_names"]) == {"istota_email_imap_password", "web.map.api_key"}
        assert any("imap_host" in line for line in preview["diff"])

        submit = {
            **_body(archive, archive["kdbx"], selected=[IMAP, MAP]),
            "input_digest": body["input_digest"], "preview_digest": body["preview_digest"],
            "step_up": await _step_up(alice, archive),
        }
        response = await alice.post(SUBMIT, json=submit, headers=ORIGIN)
        seen.append(response)
        assert response.status_code == 202, response.text
        request_id = response.json()["id"]
        assert (archive["requests"] / applier.TRIGGER_NAME).read_text() == f"{request_id}\n"
        seen += [await alice.get(f"{BASE}/requests/{request_id}"), await alice.get(f"{BASE}/requests")]

        # Sealed until the host takes it; no public column holds a value.
        [row] = _rows(archive)
        for column, value in row.items():
            if column != "secrets_sealed":
                _no_value_in(str(value))
        assert json.loads(store.unseal(row["secrets_sealed"])) == {
            "istota_email_imap_password": FILE_VALUE, "web.map.api_key": MAP_KEY,
        }
        live = archive["file"].read_bytes()
        with db.get_db(archive["config"].db_path) as conn:
            taken = config_requests.take(conn, _digest(archive))
            assert conn.execute(
                "SELECT secrets_sealed FROM config_change_requests WHERE id = ?", (request_id,),
            ).fetchone()[0] is None
        plan, files, _ = handoff.build(taken, tomllib.loads(live.decode()), environ={})
        assert plan["config"]["email"]["imap_host"] == "old.example.org"
        # The missing inline credential was put in before its sentinel was resolved.
        assert plan["config"]["web"]["map"]["api_key"] == MAP_KEY
        # Exactly the selected file credential; the unchanged header file and
        # the host-only Nextcloud password are not written.
        assert files["secrets"] == {"istota_email_imap_password": FILE_VALUE}

        for response in seen:
            _no_value_in(response.text)
        _no_value_in(caplog.text)
        with db.get_db(archive["config"].db_path) as conn:
            dump = "\n".join(conn.iterdump())
        assert RESTORE_PASSWORD not in dump and ROTATED not in dump

    async def test_unselected_credentials_stay_and_a_missing_one_is_named(self, alice, archive):
        response = await _preview(alice, archive)
        assert response.status_code == 200, response.text
        preview = response.json()["preview"]
        assert preview["secret_names"] == []
        assert any("web.map.api_key" in item["message"] for item in preview["problems"]["warnings"])

    async def test_a_credentials_only_restore_leaves_the_document(self, alice, archive):
        response = await _preview(alice, archive, restore_config=False, selected=[IMAP])
        preview = response.json()["preview"]
        assert preview["changed_keys"] == 0
        assert preview["secret_names"] == ["istota_email_imap_password"]
        submit = {**await _reviewed(alice, archive, restore_config=False, selected=[IMAP]),
                  "step_up": await _step_up(alice, archive)}
        assert (await alice.post(SUBMIT, json=submit, headers=ORIGIN)).status_code == 202

    async def test_a_plain_backup_restores_the_configuration(self, alice, archive):
        data = archive["toml"].encode()
        submit = {**await _reviewed(alice, archive, fmt="toml", data=data),
                  "step_up": await _step_up(alice, archive)}
        response = await alice.post(SUBMIT, json=submit, headers=ORIGIN)
        assert response.status_code == 202, response.text
        [row] = _rows(archive)
        assert json.loads(row["secret_names_json"]) == []


SESSION_KEY = "fixture-session-signing-key-3f6d"
TOKEN_KEY = "fixture-web-token-key-c81e"


def _archive_text(kp) -> str:
    """Every field, property and attachment of an opened archive, as one text."""
    parts = []
    for entry in kp.entries:
        parts += [entry.title or "", entry.username or "", entry.password or "", entry.notes or ""]
        parts += [f"{key}={value}" for key, value in entry.custom_properties.items()]
        parts += [attachment.data.decode("utf-8", "replace") for attachment in entry.attachments]
    return "\n".join(parts)


class TestTheExportRoundTrip:
    """The backup the export route hands out is the one the restore routes take:
    `POST /backup`, the installation moves on, then preview, submit,
    `requests.take` and `handoff.build` with the downloaded file and the
    password the export generated."""

    async def test_an_exported_backup_reaches_the_host_handoff(self, alice, install, monkeypatch, caplog):
        from tests.test_web_config_backup import BACKUP, EXPORT_PASSWORD, _open, _proof

        caplog.set_level(logging.INFO)
        monkeypatch.setattr(config_check, "run_check", _REAL_RUN_CHECK)
        install["file"].write_text(CURRENT + f'\n[web.map]\napi_key = "{MAP_KEY}"\n', encoding="utf-8")
        secrets_dir = install["secrets"]
        (secrets_dir / "istota_nextcloud_app_password").write_text(NC_PASSWORD, encoding="utf-8")
        (secrets_dir / "istota_web_session_secret_key").write_text(SESSION_KEY, encoding="utf-8")
        monkeypatch.setenv("ISTOTA_WEB_TOKEN_KEY", TOKEN_KEY)
        master_key = "a" * 64  # the fixture's ISTOTA_SECRET_KEY

        response = await alice.post(BACKUP, json={"step_up": await _proof(alice, install)}, headers=ORIGIN)
        assert response.status_code == 200, response.text
        exported = response.json()
        assert exported["password"] == EXPORT_PASSWORD

        # The manifest names what a rebuild needs beyond this file, and no
        # runtime key is anywhere in it.
        kp = _open(exported)
        [root] = kp.root_group.subgroups
        manifest = next(entry for entry in root.entries if entry.title == "Backup manifest")
        assert manifest.custom_properties["istota.recovery_scope"] == backup.RECOVERY_SCOPE
        requires = json.loads(manifest.custom_properties["istota.recovery_requires"])
        assert requires == list(backup.RECOVERY_REQUIRES)
        assert "the original credential-store master key" in requires
        text = _archive_text(kp)
        for value in (FILE_VALUE, MAP_KEY, NC_PASSWORD):
            assert value in text
        for runtime_key in (SESSION_KEY, TOKEN_KEY, master_key):
            assert runtime_key not in text

        # The installation moves on after the backup.
        install["file"].write_text(CURRENT.replace("old.example.org", "new.example.org"), encoding="utf-8")
        (secrets_dir / "istota_email_imap_password").write_text(ROTATED, encoding="utf-8")

        upload = {
            "format": "kdbx", "file": exported["file"], "password": exported["password"],
            "base_digest": _digest(install), "restore_config": True, "selected": [IMAP, MAP],
        }
        seen = [response]
        response = await alice.post(PREVIEW, json=upload, headers=ORIGIN)
        seen.append(response)
        assert response.status_code == 200, response.text
        reviewed = response.json()
        statuses = {row["id"]: row["status"] for row in reviewed["credentials"]}
        assert statuses == {IMAP: "changed", HEADERS: "unchanged", MAP: "new", NEXTCLOUD: "host_only"}
        assert reviewed["preview"]["errors"] == [] and reviewed["preview"]["problems"]["blocking"] == []

        response = await alice.post(SUBMIT, headers=ORIGIN, json={
            **upload, "input_digest": reviewed["input_digest"], "preview_digest": reviewed["preview_digest"],
            "step_up": await _step_up(alice, install),
        })
        seen.append(response)
        assert response.status_code == 202, response.text

        live = install["file"].read_bytes()
        with db.get_db(install["config"].db_path) as conn:
            taken = config_requests.take(conn, _digest(install))
        plan, files, _ = handoff.build(taken, tomllib.loads(live.decode()), environ={})
        assert plan["config"]["email"]["imap_host"] == "old.example.org"
        assert plan["config"]["web"]["map"]["api_key"] == MAP_KEY
        assert files["secrets"] == {"istota_email_imap_password": FILE_VALUE}

        # Values only inside the encrypted file and the sealed column; the
        # export password nowhere the server keeps.
        public = [json.dumps({k: v for k, v in seen[0].json().items() if k not in ("file", "password")})]
        public += [r.text for r in seen[1:]]
        for text in public + [caplog.text]:
            for value in (FILE_VALUE, MAP_KEY, NC_PASSWORD, SESSION_KEY, TOKEN_KEY, EXPORT_PASSWORD):
                assert value not in text
        with db.get_db(install["config"].db_path) as conn:
            dump = "\n".join(conn.iterdump())
        for value in (EXPORT_PASSWORD, SESSION_KEY, TOKEN_KEY, FILE_VALUE, MAP_KEY, NC_PASSWORD):
            assert value not in dump


class TestSelection:
    async def test_host_only_unchanged_and_unknown_selections_are_refused(self, alice, archive):
        for selected in ([NEXTCLOUD], [HEADERS], ["file:../../etc/passwd"]):
            response = await _preview(alice, archive, selected=selected)
            assert response.status_code == 400, selected
            assert response.json()["error"] == "invalid_selection"
        rows = {row["id"]: row for row in (await _preview(alice, archive)).json()["credentials"]}
        assert rows[NEXTCLOUD]["reason"] and not rows[NEXTCLOUD]["selectable"]

    async def test_no_changes_spends_no_code(self, alice, archive):
        archive["file"].write_text(CURRENT + f'\n[web.map]\napi_key = "{MAP_KEY}"\n', encoding="utf-8")
        response = await _preview(alice, archive)
        assert response.json()["preview"]["changed"] is False
        proof = await _step_up(alice, archive)
        submit = {**await _reviewed(alice, archive), "step_up": proof}
        response = await alice.post(SUBMIT, json=submit, headers=ORIGIN)
        assert response.status_code == 400
        assert response.json()["errors"] == ["the change set changes nothing"]
        assert (await _submit(alice, archive, step_up=proof)).status_code == 202


class TestRefusedUploads:
    async def _refused(self, alice, setup, status, error, **extra):
        response = await _preview(alice, setup, **extra)
        assert response.status_code == status, response.text
        assert response.json()["error"] == error
        assert response.headers["cache-control"] == "no-store"
        _no_value_in(response.text)
        return response

    async def test_the_wrong_password(self, alice, archive):
        await self._refused(alice, archive, 400, "backup_locked", password="not the password")

    async def test_no_password_or_key_file(self, alice, archive):
        await self._refused(alice, archive, 400, "backup_locked", password=None)

    async def test_a_key_file_is_needed_where_the_backup_has_one(self, alice, archive):
        bundle = backup.gather_backup(archive["file"], archive["secrets"], environ={})
        key = keyfile()
        data = backup.build_installation_kdbx(bundle, password=RESTORE_PASSWORD, keyfile=key)
        await self._refused(alice, archive, 400, "backup_locked", data=data)
        response = await _preview(alice, archive, data=data, keyfile=base64.b64encode(key).decode())
        assert response.status_code == 200, response.text

    async def test_a_declared_format_the_bytes_do_not_match(self, alice, archive):
        await self._refused(alice, archive, 400, "invalid_backup", data=archive["toml"].encode())
        await self._refused(alice, archive, 400, "invalid_backup", fmt="toml")

    @pytest.mark.parametrize("data", [b"", b" \n", b"\xff\xfe", b"[email\n"])
    async def test_an_empty_or_malformed_plain_file(self, alice, archive, data):
        if not data:
            await self._refused(alice, archive, 400, "invalid_body", fmt="toml", data=data)
        else:
            await self._refused(alice, archive, 400, "invalid_backup", fmt="toml", data=data)

    async def test_a_plain_file_holding_a_credential_value(self, alice, archive):
        data = CURRENT.encode()
        response = await self._refused(alice, archive, 400, "plaintext_secrets", fmt="toml", data=data)
        assert "change_set" not in response.json() and "preview" not in response.json()

    async def test_a_plain_file_over_the_configuration_limit(self, alice, archive):
        await self._refused(
            alice, archive, 413, "too_large", fmt="toml", data=b"#" * (backup.CONFIG_MAX_BYTES + 1),
        )

    async def test_base64_that_is_not_strict(self, alice, archive):
        await self._refused(alice, archive, 400, "invalid_body", file="not base64!")

    async def test_a_plain_file_with_unlock_material_or_a_selection(self, alice, archive):
        data = archive["toml"].encode()
        await self._refused(alice, archive, 400, "invalid_body", fmt="toml", data=data, password="x")
        await self._refused(alice, archive, 400, "invalid_body", fmt="toml", data=data, selected=[IMAP])

    async def test_an_unknown_field(self, alice, archive):
        await self._refused(alice, archive, 400, "invalid_body", ops=[])

    async def test_the_ordinary_routes_take_no_file(self, alice, archive):
        body = {"base_digest": _digest(archive), "ops": [], "file": "AAAA"}
        for path in (f"{BASE}/preview", f"{BASE}/requests"):
            response = await alice.post(path, json=body, headers=ORIGIN)
            assert response.status_code == 400
            assert response.json()["error"] == "invalid_change"

    async def test_the_vault_extra_missing(self, alice, archive, monkeypatch):
        monkeypatch.setattr(backup, "encrypted_backup_available", lambda: False)
        await self._refused(alice, archive, 503, "vault_unavailable")


class TestTheGates:
    async def test_a_non_admin_is_refused_both_routes(self, archive):
        client = await _client("bob")
        try:
            for path in (PREVIEW, SUBMIT):
                assert (await client.post(path, json=_body(archive, archive["kdbx"]), headers=ORIGIN)).status_code == 403
        finally:
            await client.aclose()

    async def test_no_origin_is_refused(self, alice, archive):
        for path in (PREVIEW, SUBMIT):
            assert (await alice.post(path, json=_body(archive, archive["kdbx"]))).status_code == 403

    async def test_an_admin_with_no_live_identity_is_refused(self, archive):
        client = await _client("dave")
        try:
            for path in (PREVIEW, SUBMIT):
                response = await client.post(path, json=_body(archive, archive["kdbx"]), headers=ORIGIN)
                assert response.status_code == 403
                assert response.json()["error"] == "admin_identity_required"
                assert response.headers["cache-control"] == "no-store"
        finally:
            await client.aclose()

    async def test_no_step_up_stores_nothing(self, alice, archive):
        response = await alice.post(SUBMIT, json=await _reviewed(alice, archive, selected=[IMAP]), headers=ORIGIN)
        assert response.status_code == 403
        assert response.json()["field"] == "code"
        assert _rows(archive) == []

    async def test_no_applier_still_previews(self, alice, archive, monkeypatch):
        submit = await _reviewed(alice, archive, selected=[IMAP])
        monkeypatch.delenv(applier.APPLIER_ENV)
        assert (await _preview(alice, archive, selected=[IMAP])).status_code == 200
        response = await alice.post(SUBMIT, json={**submit, "step_up": {}}, headers=ORIGIN)
        assert response.status_code == 409
        assert response.json()["error"] == "no_applier"

    async def test_a_managed_configuration_allows_a_credentials_only_restore(self, alice, archive):
        (archive["file"].parent / "managed.json").write_text(
            json.dumps({"config": {"managed": True, "source": "ansible"}}), encoding="utf-8",
        )
        response = await _preview(alice, archive, selected=[IMAP])
        assert response.status_code == 409
        assert response.json()["error"] == "managed"
        assert (await _preview(alice, archive, restore_config=False, selected=[IMAP])).status_code == 200


class TestTheRestoreLimit:
    """A restore preview reports whether an uploaded value equals the current
    one, so a stolen admin session could test guesses with it. Every restore
    request counts, refused or not, previews and submits alike."""

    async def test_the_budget_is_per_admin_and_counts_refusals(self, alice, archive, monkeypatch):
        import istota.webui.app as mod

        for _ in range(mod._CONFIG_RESTORE_MAX_PER_ADMIN):
            response = await _preview(alice, archive, password="not the password")
            assert response.status_code == 400 and response.json()["error"] == "backup_locked"
        for path in (PREVIEW, SUBMIT):
            response = await alice.post(path, json=_body(archive, archive["kdbx"]), headers=ORIGIN)
            assert response.status_code == 429, response.text
            assert response.json()["error"] == "restore_rate_limited"
            assert response.headers["cache-control"] == "no-store"
            _no_value_in(response.text)

        monkeypatch.setattr(archive["config"], "admin_users", {"alice", "bob", "dave"})
        bob = await _client("bob")
        try:
            assert (await _preview(bob, archive)).status_code == 200
        finally:
            await bob.aclose()


class TestStaleReviews:
    """Each refuses before the step-up, and the same code then succeeds."""

    async def test_every_stale_submit_keeps_the_code(self, alice, archive):
        proof = await _step_up(alice, archive)
        reviewed = await _reviewed(alice, archive, selected=[IMAP])

        stale_selection = {**reviewed, "selected": [IMAP, MAP], "step_up": proof}
        response = await alice.post(SUBMIT, json=stale_selection, headers=ORIGIN)
        assert response.status_code == 409 and response.json()["error"] == "preview_changed"

        stale_base = {**reviewed, "base_digest": "0" * 64, "step_up": proof}
        response = await alice.post(SUBMIT, json=stale_base, headers=ORIGIN)
        assert response.status_code == 409 and response.json()["error"] == "config_changed"

        # The IMAP file now holds the backup's value: the reviewed preview
        # said "changed", the rebuilt one would not.
        current = (archive["secrets"] / "istota_email_imap_password").read_text()
        (archive["secrets"] / "istota_email_imap_password").write_text(FILE_VALUE, encoding="utf-8")
        response = await alice.post(SUBMIT, json={**reviewed, "step_up": proof}, headers=ORIGIN)
        assert response.status_code == 400 and response.json()["error"] == "invalid_selection"
        (archive["secrets"] / "istota_email_imap_password").write_text(current, encoding="utf-8")

        assert (await _submit(alice, archive, step_up=proof)).status_code == 202
        response = await alice.post(SUBMIT, json={**reviewed, "step_up": proof}, headers=ORIGIN)
        assert response.status_code == 409 and response.json()["error"] == "request_open"

    async def test_a_changed_preview_is_refused(self, alice, archive):
        proof = await _step_up(alice, archive)
        reviewed = await _reviewed(alice, archive, selected=[IMAP])
        response = await alice.post(SUBMIT, json={**reviewed, "preview_digest": "0" * 64, "step_up": proof},
                                    headers=ORIGIN)
        assert response.status_code == 409 and response.json()["error"] == "preview_changed"
        assert _rows(archive) == []
        response = await alice.post(SUBMIT, json={**reviewed, "step_up": proof}, headers=ORIGIN)
        assert response.status_code == 202, response.text


_HOLDER = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print("held", flush=True)
sys.stdin.read()
"""


class TestDecryptAdmission:
    @pytest.fixture
    def held(self, archive):
        """Another process, as another web worker would, holding the decrypt slot."""
        holder = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(backup.decrypt_lock_path(archive["config"].db_path))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        assert holder.stdout.readline().strip() == "held"
        yield holder
        if holder.poll() is None:
            holder.stdin.close()
            holder.wait(timeout=10)

    async def test_a_busy_slot_refuses_at_once_and_keeps_the_code(self, alice, archive, held):
        response = await _preview(alice, archive, selected=[IMAP])
        assert response.status_code == 503
        assert response.json()["error"] == "backup_busy"
        assert response.headers["cache-control"] == "no-store"

        held.stdin.close()
        held.wait(timeout=10)
        reviewed = await _reviewed(alice, archive, selected=[IMAP])
        proof = await _step_up(alice, archive)

        again = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(backup.decrypt_lock_path(archive["config"].db_path))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        try:
            assert again.stdout.readline().strip() == "held"
            response = await alice.post(SUBMIT, json={**reviewed, "step_up": proof}, headers=ORIGIN)
            assert response.status_code == 503
            assert response.json()["error"] == "backup_busy"
            assert _rows(archive) == []
        finally:
            again.stdin.close()
            again.wait(timeout=10)
        response = await alice.post(SUBMIT, json={**reviewed, "step_up": proof}, headers=ORIGIN)
        assert response.status_code == 202, response.text

    async def test_concurrent_previews_are_admitted_or_refused_busy(self, alice, archive):
        responses = await asyncio.gather(*(_preview(alice, archive, selected=[IMAP]) for _ in range(4)))
        outcomes = [(response.status_code, response.json().get("error")) for response in responses]
        assert all(outcome in {(200, None), (503, "backup_busy")} for outcome in outcomes), outcomes
        assert (200, None) in outcomes
        # Nothing is left holding the slot.
        assert (await _preview(alice, archive, selected=[IMAP])).status_code == 200


PUSH = backup.PUSH_KEY_ID


class TestThePushSigningKey:
    """#947, through the routes: a backup made with the key at the default
    path, the key lost, then a restore that selects it. It travels sealed and
    reaches the host's handoff; the live key decides new or changed."""

    @pytest.fixture
    def pushed(self, install):
        from istota.push import envelope

        key = install["file"].parent / "push" / "signing.pem"
        text = CURRENT + f'\n[push]\ncontent = "private"\nsigning_key_path = "{key}"\n'
        install["file"].write_text(text, encoding="utf-8")
        envelope.write_signing_key(key)
        pem = key.read_text(encoding="ascii")
        bundle = backup.gather_backup(install["file"], install["secrets"], environ={})
        data = backup.build_installation_kdbx(bundle, password=RESTORE_PASSWORD, keyfile=None)
        key.unlink()
        envelope.public_key_path(key).unlink()
        return {**install, "kdbx": data, "key": key, "pem": pem}

    async def test_a_lost_key_is_restored_through_the_sealed_request(self, alice, pushed, caplog):
        caplog.set_level(logging.INFO)
        response = await _preview(alice, pushed, restore_config=False, selected=[PUSH])
        assert response.status_code == 200, response.text
        body = response.json()
        row = next(row for row in body["credentials"] if row["id"] == PUSH)
        assert (row["status"], row["kind"], row["selectable"]) == ("new", "push_key", True)
        assert body["preview"]["changed"] is True
        body_line = pushed["pem"].splitlines()[1]
        assert body_line not in response.text

        submit = {**await _reviewed(alice, pushed, restore_config=False, selected=[PUSH]),
                  "step_up": await _step_up(alice, pushed)}
        response = await alice.post(SUBMIT, json=submit, headers=ORIGIN)
        assert response.status_code == 202, response.text
        [row] = _rows(pushed)
        for column, value in row.items():
            if column != "secrets_sealed":
                assert body_line not in str(value)
        assert body_line not in caplog.text

        live = pushed["file"].read_bytes()
        with db.get_db(pushed["config"].db_path) as conn:
            taken = config_requests.take(conn, _digest(pushed))
        plan, _, _ = handoff.build(taken, tomllib.loads(live.decode()), environ={})
        handed = handoff.push_key(taken, plan["config"], config_dir=pushed["file"].parent, environ={})
        assert handed == {"pem": pushed["pem"], "force": False}

    async def test_a_different_live_key_is_changed_and_the_preview_says_phones_enroll_again(
        self, alice, pushed,
    ):
        from istota.push import envelope

        envelope.write_signing_key(pushed["key"])
        response = await _preview(alice, pushed, restore_config=False, selected=[PUSH])
        assert response.status_code == 200, response.text
        body = response.json()
        row = next(row for row in body["credentials"] if row["id"] == PUSH)
        assert row["status"] == "changed" and "enroll again" in row["reason"]
        assert any("enroll again" in item["message"] for item in body["preview"]["problems"]["warnings"])

    async def test_a_live_key_that_cannot_be_read_holds_back_only_its_own_row(self, alice, pushed, tmp_path):
        pushed["key"].parent.mkdir(exist_ok=True)
        pushed["key"].symlink_to(tmp_path / "nowhere.pem")
        response = await _preview(alice, pushed)
        assert response.status_code == 200, response.text
        row = next(row for row in response.json()["credentials"] if row["id"] == PUSH)
        assert row["status"] == "host_only" and "push-key import --force" in row["reason"]
        response = await _preview(alice, pushed, selected=[PUSH])
        assert response.status_code == 400 and response.json()["error"] == "invalid_selection"

    async def test_a_key_kept_elsewhere_is_host_only(self, alice, pushed, tmp_path):
        text = pushed["file"].read_text().replace(str(pushed["key"]), str(tmp_path / "elsewhere.pem"))
        pushed["file"].write_text(text, encoding="utf-8")
        response = await _preview(alice, pushed, restore_config=False)
        row = next(row for row in response.json()["credentials"] if row["id"] == PUSH)
        assert row["status"] == "host_only" and "istota-stack push-key import" in row["reason"]
        response = await _preview(alice, pushed, restore_config=False, selected=[PUSH])
        assert response.status_code == 400 and response.json()["error"] == "invalid_selection"
