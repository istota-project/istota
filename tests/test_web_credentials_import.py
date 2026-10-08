import json

import pytest

from istota import db
from istota.credentials import store, vault
from tests.test_kdbx_import import PASSPHRASE, kdbx
from tests.test_web_credentials_local import app, client, config, signed_client  # noqa: F401

BASE = "/istota/api/settings/credentials/import"
ORIGIN = {"Origin": "https://example.com"}


def upload(data, keyfile=None):
    parts = [("file", ("fixture.kdbx", data))]
    if keyfile is not None:
        parts.append(("keyfile", ("fixture.key", keyfile)))
    return parts


async def test_preview_apply_with_keyfile(signed_client, config, caplog):  # noqa: F811
    key = b"k" * 32
    data = kdbx([{"title": "portal"}], keyfile=key)
    fields = {"passphrase": PASSPHRASE}
    response = await signed_client.post(BASE + "/preview", files=upload(data, key), data=fields, headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    preview = response.json()
    assert preview["items"][0]["name"] == "portal"
    response = await signed_client.post(BASE, files=upload(data, key), data={
        **fields, "selected": json.dumps(["portal"]), "digest": preview["digest"]}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert response.json()["imported"] == ["portal"]
    assert store.get_secret(config.db_path, "alice", "vault_entries", "portal") == "fixture-value"
    with db.get_db(config.db_path) as conn:
        assert PASSPHRASE not in "\n".join(conn.iterdump())
    assert PASSPHRASE not in response.text + caplog.text


@pytest.mark.parametrize("suffix", ["", "/preview"])
async def test_auth_origin_isolation_and_bounds(signed_client, config, monkeypatch, suffix):  # noqa: F811
    data = kdbx([{"title": "portal"}])
    fields = {"passphrase": PASSPHRASE, "selected": '["portal"]', "digest": "0" * 64}
    if suffix:
        fields = {"passphrase": PASSPHRASE}
    assert (await signed_client.post(BASE + suffix, files=upload(data), data=fields)).status_code == 403
    monkeypatch.setattr(vault, "vault_isolation_refusal", lambda *a: "isolation-required")
    r = await signed_client.post(BASE + suffix, files=upload(data), data=fields, headers=ORIGIN)
    assert r.status_code == 403
    monkeypatch.setattr(vault, "vault_isolation_refusal", lambda *a: None)
    r = await signed_client.post(BASE + suffix, files=upload(b"x" * (vault.VAULT_READ_CAP_BYTES + 1)),
                                 data=fields, headers=ORIGIN)
    assert r.status_code == 413
    signed_client.cookies.clear()
    assert (await signed_client.post(BASE + suffix, files=upload(data), data=fields, headers=ORIGIN)).status_code == 401


@pytest.mark.parametrize("keyfile", [None, b"x" * 32])
async def test_wrong_or_missing_keyfile_is_safe(signed_client, keyfile, caplog):  # noqa: F811 -- shared fixture
    data = kdbx([{"title": "portal"}], keyfile=b"k" * 32)
    r = await signed_client.post(BASE + "/preview", files=upload(data, keyfile),
                                 data={"passphrase": PASSPHRASE}, headers=ORIGIN)
    assert r.status_code == 400
    assert r.json()["field"] == "passphrase"
    assert PASSPHRASE not in r.text + caplog.text


async def test_multipart_rejects_duplicates_unknown_fields_and_extra_files(signed_client):  # noqa: F811 -- shared fixture
    data = kdbx([{"title": "portal"}])
    for parts in [upload(data) * 2, upload(data) + [("other", ("x", b"x"))],
                  upload(data) + [("keyfile", ("k", b"x"))] * 2]:
        r = await signed_client.post(BASE + "/preview", files=parts,
                                     data={"passphrase": PASSPHRASE}, headers=ORIGIN)
        assert r.status_code == 400
    r = await signed_client.post(BASE + "/preview", files=upload(data),
                                 data={"passphrase": PASSPHRASE, "unknown": PASSPHRASE}, headers=ORIGIN)
    assert r.status_code == 400
    assert PASSPHRASE not in r.text


async def test_upload_never_rolls_to_disk(signed_client, monkeypatch):  # noqa: F811 -- shared fixture
    from tempfile import SpooledTemporaryFile
    def refuse_rollover(self):
        pytest.fail("credential upload spilled to disk")
    monkeypatch.setattr(SpooledTemporaryFile, "rollover", refuse_rollover)
    r = await signed_client.post(BASE + "/preview", files=upload(b"x" * (2 * 1024 * 1024)),
                                 data={"passphrase": PASSPHRASE}, headers=ORIGIN)
    assert r.status_code == 400


async def test_missing_store_key_is_unavailable(signed_client, monkeypatch):  # noqa: F811 -- shared fixture
    monkeypatch.delenv("ISTOTA_SECRET_KEY")
    data = kdbx([{"title": "portal"}])
    r = await signed_client.post(BASE + "/preview", files=upload(data),
                                 data={"passphrase": PASSPHRASE}, headers=ORIGIN)
    assert r.status_code == 503


async def test_wrong_passphrase_is_safe(signed_client, caplog):  # noqa: F811 -- shared fixture
    r = await signed_client.post(BASE + "/preview", files=upload(kdbx([{"title": "portal"}])),
                                 data={"passphrase": "SENTINEL-wrong-passphrase"}, headers=ORIGIN)
    assert r.status_code == 400
    assert r.json()["field"] == "passphrase"
    assert "SENTINEL" not in r.text + caplog.text
