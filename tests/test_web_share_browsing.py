"""Browsing an accepted Open Cloud Mesh share on the Files page (#849).

The workspace router over a real database and the WebDAV double in
`tests/support/ocm_dav.py`, the same server the `shares` skill's tests drive.
The share secret is the stored local credential `ocm_share_<id>`; nothing
here stands in for the guarded client, the credential store or the row.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from istota import db
from istota.config import Config, SharingConfig
from istota.sharing import sources
from istota.webui import workspace_routes
from istota.webui.router_stubs import require_auth
from tests.support.ocm_dav import SECRET, Dav, seed_share

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture(autouse=True)
def secret_key_env():
    with mock.patch.dict(os.environ, {"ISTOTA_SECRET_KEY": "deadbeef" * 8}):
        yield


@pytest.fixture
def dav():
    server = Dav()
    server.dirs |= {"receipts"}
    server.files.update({"notes.txt": b"hello from carol\n", "receipts/march.pdf": b"%PDF-1.4 x"})
    yield server
    server.close()


@pytest.fixture
def config(tmp_path):
    config = Config(db_path=tmp_path / "istota.db", workspace_path=tmp_path / "workspace",
                    sharing=SharingConfig(private_hosts=["127.0.0.1"]))
    db.init_db(config.db_path)
    (config.workspace_path / "Users/alice").mkdir(parents=True)
    return config


@pytest.fixture
def share_id(config, dav):
    with db.get_db(config.db_path) as conn:
        source_id = seed_share(conn, dav, access="read")
        conn.commit()
    return source_id


@pytest.fixture
def app(config):
    app = FastAPI()
    app.state.istota_config = config
    app.include_router(workspace_routes.router, prefix="/workspace")
    app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
    return app


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://example.com") as client:
        yield client


def _row(config, source_id, user="alice"):
    with db.get_db(config.db_path) as conn:
        return sources.get_source(conn, source_id, user)


async def _list(client, source_id, path=""):
    return await client.get("/workspace/list", params={"place": f"share:{source_id}", "path": path})


class TestPlaces:
    async def test_a_share_is_a_browsable_place_under_one_index(self, client, share_id):
        response = await client.get("/workspace/places")
        data = response.json()
        share = next(p for p in data["places"] if p["id"] == f"share:{share_id}")
        assert share["browsable"] is True and share["view_root"] is None
        assert share["remote"]["host"].startswith("127.0.0.1:")
        assert data["share_index"] is True
        assert "webdav" not in response.text and SECRET not in response.text

    async def test_no_index_without_shares_or_receiving(self, client):
        data = (await client.get("/workspace/places")).json()
        assert data["share_index"] is False

    async def test_receiving_shows_the_index_before_any_share(self, client, config):
        config.sharing.ocm.enabled = True
        config.site.hostname = "istota.example.org"
        data = (await client.get("/workspace/places")).json()
        assert data["share_index"] is True


class TestListing:
    async def test_the_root_lists_its_children_with_the_share_secret(self, client, share_id, dav):
        response = await _list(client, share_id)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["place"] == f"share:{share_id}" and data["path"] == ""
        assert [(e["name"], e["kind"]) for e in data["entries"]] == [
            ("receipts", "dir"), ("notes.txt", "file")]
        notes = data["entries"][1]
        assert notes["size"] == len(dav.files["notes.txt"]) and notes["modified"] > 0
        assert notes["share_path"] == "notes.txt"
        assert data["access"] == "write" and data["root_kind"] == "folder"
        assert {r["user"] for r in dav.received} == {SECRET}
        assert SECRET not in response.text

    async def test_a_subfolder(self, client, share_id):
        data = (await _list(client, share_id, "receipts/")).json()
        assert data["path"] == "receipts"
        assert [e["share_path"] for e in data["entries"]] == ["receipts/march.pdf"]

    async def test_every_listing_reads_the_live_permissions(self, client, config, share_id, dav):
        assert (await _list(client, share_id)).json()["access"] == "write"
        assert _row(config, share_id).access == "write"
        dav.permissions = "RG"
        assert (await _list(client, share_id, "receipts")).json()["access"] == "read"
        assert _row(config, share_id).access == "read"
        propfinds = [r for r in dav.received if r["method"] == "PROPFIND"]
        assert [r["headers"]["depth"] for r in propfinds] == ["0", "1", "0", "1"]

    async def test_a_share_of_one_file_lists_that_file_under_the_share_name(
            self, client, config, share_id, dav):
        dav.files = {"": b"one file"}
        dav.dirs = set()
        data = (await _list(client, share_id)).json()
        assert data["root_kind"] == "file"
        assert data["entries"] == [{
            "name": "Taxes 2026", "kind": "file", "size": 8, "modified": data["entries"][0]["modified"],
            "hidden": False, "share_path": "",
        }]
        assert _row(config, share_id).extras["root_kind"] == "file"
        assert (await _list(client, share_id, "inner")).status_code == 404

    async def test_hrefs_outside_the_root_are_left_out_and_counted(self, client, share_id, dav):
        dav.extra_hrefs = ["/elsewhere/secret.txt", "/public.php/webdav/a%2Fb", "http://evil.test/public.php/webdav/x"]
        data = (await _list(client, share_id)).json()
        assert data["skipped_names"] == 3
        assert {e["name"] for e in data["entries"]} == {"receipts", "notes.txt"}

    async def test_a_name_from_the_sharer_is_data(self, client, share_id, dav):
        dav.files["<img src=x onerror=alert(1)>.txt"] = b"x"
        data = (await _list(client, share_id)).json()
        assert "<img src=x onerror=alert(1)>.txt" in [e["name"] for e in data["entries"]]

    async def test_a_missing_folder(self, client, share_id):
        response = await _list(client, share_id, "nope")
        assert response.status_code == 404


class TestRefusals:
    @pytest.mark.parametrize("path", ["../x", "a/../b", "a\\b", "bad\0", "./a"])
    async def test_a_path_that_leaves_the_share_sends_nothing(self, client, share_id, dav, path):
        response = await _list(client, share_id, path)
        assert response.status_code == 404
        assert dav.received == []

    async def test_another_users_share_is_not_found(self, client, config, dav):
        with db.get_db(config.db_path) as conn:
            bobs = seed_share(conn, dav, user="bob", key="9")
            conn.commit()
        assert (await _list(client, bobs)).status_code == 404
        assert (await client.get("/workspace/share/file", params={"place": f"share:{bobs}", "path": "notes.txt"})).status_code == 404
        assert dav.received == []

    async def test_a_pending_offer_is_not_browsed(self, client, config, dav):
        with db.get_db(config.db_path) as conn:
            pending = sources.create_source(
                conn, user_id="alice", kind="ocm_share", state="pending", remote_host=dav.host,
                remote_key=f"{dav.host}|5", access="read",
                extras={"name": "Offer", "webdav": f"http://{dav.host}/public.php/webdav/"},
            )
            conn.commit()
        assert (await _list(client, pending)).status_code == 404
        assert dav.received == []

    @pytest.mark.parametrize("place", ["share:", "share:x", "share:-1", "share:1:a", "share:99999"])
    async def test_a_malformed_or_unknown_place(self, client, share_id, dav, place):
        response = await client.get("/workspace/list", params={"place": place})
        assert response.status_code == 404
        assert dav.received == []

    @pytest.mark.parametrize("status", [401, 404])
    async def test_a_root_the_sharer_no_longer_serves_revokes_the_share(
            self, client, config, share_id, dav, status):
        if status == 401:
            dav.revoked = True
        else:
            dav.root_status = 404
        response = await _list(client, share_id)
        assert response.status_code == 410
        assert response.json()["code"] == "share_revoked"
        row = _row(config, share_id)
        assert row.state == "revoked"
        with db.get_db(config.db_path) as conn:
            from istota.credentials import local
            assert not local.is_local(conn, "alice", sources.credential_name(share_id))
        places = (await client.get("/workspace/places")).json()["places"]
        assert f"share:{share_id}" not in [p["id"] for p in places]

    async def test_a_host_outside_private_hosts_is_refused_and_recorded(
            self, client, config, share_id, dav):
        config.sharing.private_hosts = []
        response = await _list(client, share_id)
        assert response.status_code == 502
        assert response.json()["code"] == "host_refused"
        assert dav.received == []
        row = _row(config, share_id)
        assert row.state == "error" and row.last_reason == "host_refused"

    async def test_a_server_error_below_the_root_is_recorded(self, client, config, share_id, dav):
        dav.statuses["receipts"] = 503
        response = await _list(client, share_id, "receipts")
        assert (response.status_code, response.json()["code"]) == (502, "http_503")
        row = _row(config, share_id)
        assert (row.state, row.last_reason) == ("error", "http_503")

    async def test_a_forbidden_folder_is_said_so_and_left_alone(self, client, config, share_id, dav):
        dav.statuses["receipts"] = 403
        response = await _list(client, share_id, "receipts")
        assert response.status_code == 403
        assert "does not let you read" in response.json()["error"]
        assert _row(config, share_id).state == "active"

    async def test_a_recovered_share_returns_to_active(self, client, config, share_id, dav):
        config.sharing.private_hosts = []
        await _list(client, share_id)
        config.sharing.private_hosts = ["127.0.0.1"]
        assert (await _list(client, share_id)).status_code == 200
        assert _row(config, share_id).state == "active"

    async def test_a_deleted_credential_removes_the_share(self, client, config, share_id, dav):
        from istota.credentials import local
        with db.get_db(config.db_path) as conn:
            local.delete_in(conn, "alice", sources.credential_name(share_id))
            conn.commit()
        response = await _list(client, share_id)
        assert response.status_code == 410
        assert response.json()["code"] == "secret_missing"
        assert _row(config, share_id).state == "revoked"
        assert dav.received == []


class TestDownload:
    async def _get(self, client, source_id, path, **headers):
        return await client.get("/workspace/share/file", headers=headers,
                                params={"place": f"share:{source_id}", "path": path})

    async def test_a_file_streams_as_an_attachment_with_our_headers(self, client, share_id, dav):
        dav.content_type = "text/html"
        dav.files["page.html"] = b"<script>alert(1)</script>"
        response = await self._get(client, share_id, "page.html")
        assert response.status_code == 200
        assert response.content == b"<script>alert(1)</script>"
        assert response.headers["content-disposition"] == "attachment; filename*=utf-8''page.html"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
        assert response.headers["cache-control"] == "private, no-store"
        # The sharer's server never chooses the type: it said text/html here.
        notes = await self._get(client, share_id, "notes.txt")
        assert notes.headers["content-type"].startswith("text/plain")

    async def test_a_raster_is_served_inline_under_its_sniffed_type(self, client, share_id, dav):
        dav.files["photo.bin"] = PNG
        response = await self._get(client, share_id, "photo.bin")
        assert response.headers["content-type"] == "image/png"
        assert response.headers["content-disposition"].startswith("inline;")

    async def test_one_range_is_forwarded(self, client, share_id, dav):
        dav.files["clip.mp4"] = bytes(range(100))
        response = await self._get(client, share_id, "clip.mp4", range="bytes=10-19")
        assert response.status_code == 206
        assert response.content == bytes(range(10, 20))
        assert response.headers["content-range"] == "bytes 10-19/100"
        gets = [r for r in dav.received if r["method"] == "GET"]
        assert gets[-1]["headers"]["range"] == "bytes=10-19"

    async def test_a_file_share_downloads_from_its_root(self, client, share_id, dav):
        dav.files = {"": b"one file"}
        dav.dirs = set()
        response = await self._get(client, share_id, "")
        assert response.status_code == 200 and response.content == b"one file"
        assert "Taxes%202026" in response.headers["content-disposition"]

    async def test_a_folder_root_is_not_a_file(self, client, share_id, dav):
        response = await self._get(client, share_id, "")
        assert response.status_code == 404

    async def test_a_file_over_the_transfer_limit_is_refused_before_a_byte(
            self, client, share_id, dav, monkeypatch):
        from istota.sharing import ocm_files
        monkeypatch.setattr(ocm_files, "MAX_TRANSFER_BYTES", 8)
        dav.files["big.bin"] = b"x" * 64
        response = await self._get(client, share_id, "big.bin")
        assert response.status_code == 413
        assert response.json()["error"] == "This file is larger than the transfer limit."
        assert "GET" not in dav.methods()

    async def test_the_download_rereads_access_first(self, client, config, share_id, dav):
        dav.permissions = "RG"
        await self._get(client, share_id, "notes.txt")
        assert _row(config, share_id).access == "read"
        assert [r["method"] for r in dav.received] == ["PROPFIND", "PROPFIND", "GET"]

    async def test_a_preview_of_a_text_file(self, client, share_id):
        response = await client.get("/workspace/share/preview",
                                    params={"place": f"share:{share_id}", "path": "notes.txt"})
        assert response.status_code == 200
        data = response.json()
        assert data["kind"] == "text" and data["text"] == "hello from carol\n"
        assert data["name"] == "notes.txt"
        assert response.headers["cache-control"] == "private, no-store"

    async def test_a_preview_of_a_folder_is_refused(self, client, share_id):
        response = await client.get("/workspace/share/preview",
                                    params={"place": f"share:{share_id}", "path": "receipts"})
        assert response.status_code == 400
