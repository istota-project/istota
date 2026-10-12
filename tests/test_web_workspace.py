"""Workspace router over a real database and filesystem."""
import errno

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from istota import db
from istota.config import Config
from istota.webui import workspace_routes
from istota.webui.router_stubs import require_auth


@pytest.fixture
def config(tmp_path):
    config = Config(db_path=tmp_path / "istota.db", workspace_path=tmp_path / "workspace")
    db.init_db(config.db_path)
    (config.workspace_path / "Users/alice/notes").mkdir(parents=True)
    (config.workspace_path / "Users/alice/notes/plan.md").write_text("plan")
    return config


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


async def test_places_and_normalized_listing(client):
    response = await client.get("/workspace/places")
    assert response.status_code == 200
    data = response.json()
    home = next(p for p in data["places"] if p["id"] == "home")
    assert home["view_root"] == "/Users/alice"
    assert home["reach"]["private"] == "rw"
    assert "root" not in home
    assert "Files are stored in a local folder." in data["notes"]
    response = await client.get("/workspace/list", params={"place": "home", "path": "/notes//"})
    assert response.status_code == 200
    data = response.json()
    assert data["place"] == "home" and data["path"] == "notes"
    assert data["entries"][0]["name"] == "plan.md"
    assert data["entries"][0]["size"] == 4
    assert not data["truncated"] and data["skipped_names"] == 0


@pytest.mark.parametrize("place,path", [("unknown", ""), ("talk", ""), ("home", "../bob"), ("home", "notes/../"), ("home", "bad\0name")])
async def test_refused_list(client, place, path):
    response = await client.get("/workspace/list", params={"place": place, "path": path})
    assert response.status_code == 404
    assert "error" in response.json()


async def test_membership_is_live(client, config):
    with db.get_db(config.db_path) as conn:
        room = db.create_web_chat_room(conn, "bob", "Shared")
        db.add_room_member(conn, room.token, "alice")
    (config.workspace_path / "Channels" / room.token).mkdir(parents=True)
    params = {"place": f"room:{room.token}"}
    assert (await client.get("/workspace/list", params=params)).status_code == 200
    with db.get_db(config.db_path) as conn:
        conn.execute("DELETE FROM room_members WHERE room_token = ? AND user_id = ?", (room.token, "alice"))
    assert (await client.get("/workspace/list", params=params)).status_code == 404


@pytest.mark.parametrize("path", ["/workspace/places", "/workspace/list?place=home"])
async def test_auth_required(client, app, path):
    app.dependency_overrides.clear()
    assert (await client.get(path)).status_code == 401


@pytest.mark.parametrize("error,status", [(errno.ENOENT, 404), (errno.ENOTDIR, 404), (errno.ELOOP, 404), (errno.EEXIST, 409), (errno.ENOSPC, 507), (errno.EDQUOT, 507), (errno.EACCES, 500)])
async def test_listing_error_mapping_without_path_in_log(client, monkeypatch, caplog, error, status):
    def fail(*args, **kwargs):
        raise OSError(error, "sensitive-name")
    monkeypatch.setattr(workspace_routes, "list_dir", fail)
    response = await client.get("/workspace/list", params={"place": "home", "path": "sensitive-name"})
    assert response.status_code == status
    assert "error" in response.json()
    assert "home" in caplog.text and str(error) in caplog.text
    assert "sensitive-name" not in caplog.text and "sensitive-name" not in response.text


async def test_upload_and_folder_routes(client, config):
    response = await client.post("/workspace/folders", json={"place": "home", "path": "/notes/", "name": "Drafts"})
    assert response.status_code == 200
    assert response.json() == {"path": "notes/Drafts"}
    for name in ["draft.bin", "draft (2).bin"]:
        response = await client.post("/workspace/upload", data={"place": "home", "path": "notes/Drafts"}, files={"file": ("draft.bin", b"hello")})
        assert response.status_code == 200
        assert response.json() == {"name": name, "path": f"notes/Drafts/{name}", "size": 5}
        assert (config.workspace_path / "Users/alice/notes/Drafts" / name).read_bytes() == b"hello"


async def test_upload_stops_body_at_cap(client, config):
    config.web.workspace.max_upload_mb = 1
    async def body():
        yield b'--test\r\nContent-Disposition: form-data; name="place"\r\n\r\nhome\r\n--test\r\nContent-Disposition: form-data; name="file"; filename="big.bin"\r\nContent-Type: application/octet-stream\r\n\r\n'
        yield b"x" * (1024 * 1024)
        yield b"x" * 1024
        pytest.fail("route consumed the rest of an oversized body")
    response = await client.post("/workspace/upload", content=body(), headers={"content-type": "multipart/form-data; boundary=test"})
    assert response.status_code == 413
    assert response.json() == {"error": "file exceeds 1 MB"}
    assert not (config.workspace_path / "Users/alice/big.bin").exists()


@pytest.mark.parametrize("place", ["talk", "room:sample", "linked:1", "unknown"])
async def test_writes_only_accept_home(client, place):
    assert (await client.post("/workspace/folders", json={"place": place, "path": "", "name": "Drafts"})).status_code == 404
    assert (await client.post("/workspace/upload", data={"place": place, "path": ""}, files={"file": ("note.txt", b"x")})).status_code == 404


async def test_writes_require_origin(client, app):
    from fastapi import HTTPException
    from istota.webui.router_stubs import verify_origin
    def refuse():
        raise HTTPException(403, "cross-origin request")
    app.dependency_overrides[verify_origin] = refuse
    assert (await client.post("/workspace/folders", json={"place": "home", "path": "", "name": "Drafts"})).status_code == 403
    assert (await client.post("/workspace/upload", files={"file": ("note.txt", b"x")})).status_code == 403


@pytest.mark.parametrize("headers,body", [({}, b""), ({"content-type": "text/plain"}, b"no"), ({"content-type": "multipart/form-data; boundary=bad"}, b"malformed")])
async def test_invalid_upload_is_not_a_server_error(client, headers, body):
    response = await client.post("/workspace/upload", content=body, headers=headers)
    assert response.status_code == 422


async def test_workspace_write_auth_required(client, app):
    app.dependency_overrides.clear()
    assert (await client.post("/workspace/upload")).status_code == 401
    assert (await client.post("/workspace/folders", json={})).status_code == 401


def test_workspace_upload_config_and_nginx_cap(tmp_path):
    import re
    from pathlib import Path
    from istota.config import load_config
    path = tmp_path / "config.toml"
    path.write_text("[web.workspace]\nmax_upload_mb = 7\n")
    assert load_config(path).web.workspace.max_upload_mb == 7
    compose = (Path(__file__).resolve().parents[1] / "docker/docker-compose.yml").read_text()
    nginx_mb = int(re.search(r"NGINX_CLIENT_MAX_BODY_SIZE:-([0-9]+)M", compose).group(1))
    assert nginx_mb >= Config().web.workspace.max_upload_mb


@pytest.mark.parametrize("error", [errno.ENOTCONN, errno.EIO])
@pytest.mark.parametrize("operation", ["list", "upload", "folder"])
async def test_nextcloud_dropped_mount(client, config, monkeypatch, caplog, error, operation):
    import os
    config.nextcloud.url = "https://cloud.example.com"
    root = config.workspace_root("alice")
    real_open = os.open

    def disconnected(path, flags, *args, **kwargs):
        if path == root:
            raise OSError(error, "sensitive-name")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", disconnected)
    if operation == "list":
        response = await client.get("/workspace/list", params={"place": "home"})
    elif operation == "upload":
        response = await client.post("/workspace/upload", data={"place": "home"}, files={"file": ("note.txt", b"hello")})
    else:
        response = await client.post("/workspace/folders", json={"place": "home", "path": "", "name": "Drafts"})
    assert response.status_code == 503
    assert response.json() == {"error": "Your Nextcloud folder is not reachable right now."}
    assert "sensitive-name" not in caplog.text
    assert "home" in caplog.text and str(error) in caplog.text


async def test_nextcloud_share_markers_and_recipient_urls(client, config):
    from unittest.mock import patch
    from tests.test_nextcloud_http import _ocs_response

    config.nextcloud.url = "https://cloud.example.com/"
    config.nextcloud.username = "istota"
    config.nextcloud.app_password = "example-password"
    config.nextcloud.dav_prefix = "Shared Files"
    directory = config.workspace_root("alice") / "notes"
    for name in ("group.md", "public.md", "other.md", "private.md"):
        (directory / name).write_text("notes")
    rows = [
        {"path": "/Shared Files/Users/alice/notes/plan.md", "share_type": 0, "share_with": "alice", "file_target": "/Shared & notes/plan.md"},
        {"path": "/Shared Files/Users/alice/notes/plan.md", "share_type": 0, "share_with": "bob", "file_target": "/Bob/plan.md"},
        {"path": "/Shared Files/Users/alice/notes/group.md", "share_type": 1, "share_with": "alice", "file_target": "/Team/group.md"},
        {"path": "/Shared Files/Users/alice/notes/public.md", "share_type": 3, "file_target": "/Public/public.md"},
        {"path": "/Shared Files/Users/alice/notes/other.md", "share_type": 0, "share_with": "bob", "file_target": "/Bob/other.md"},
        {"path": "/Shared Files/Users/bob/notes/private.md", "share_type": 0, "share_with": "alice", "file_target": "/Wrong/private.md"},
    ]
    for row in rows:
        row["uid_owner"] = "istota"
    with patch("istota.nextcloud._http.httpx.get", return_value=_ocs_response(rows)) as get:
        response = await client.get("/workspace/list", params={"place": "home", "path": "notes"})
    assert response.status_code == 200
    entries = {entry["name"]: entry for entry in response.json()["entries"]}
    assert entries["plan.md"]["share"] == {"kind": "user", "count": 2}
    assert entries["plan.md"]["nextcloud_url"] == "https://cloud.example.com/apps/files/files?dir=%2FShared+%26+notes"
    for name, kind in (("group.md", "group"), ("public.md", "link"), ("other.md", "user")):
        assert entries[name]["share"] == {"kind": kind, "count": 1}
        assert entries[name]["nextcloud_url"] is None
    assert entries["private.md"]["share"] is None
    get.assert_called_once()
    assert get.call_args.kwargs["params"] == {"path": "/Shared Files/Users/alice/notes", "subfiles": "true"}
    assert get.call_args.kwargs["timeout"] == 5
    assert get.call_args.kwargs["auth"] == ("istota", "example-password")


async def test_nextcloud_share_failure_is_a_note(client, config, caplog):
    from unittest.mock import patch
    from httpx import ReadTimeout
    config.nextcloud.url = "https://cloud.example.com"
    config.nextcloud.username = "istota"
    config.nextcloud.app_password = "example-password"
    with patch("istota.nextcloud._http.httpx.get", side_effect=ReadTimeout("sensitive-name")) as get:
        response = await client.get("/workspace/list", params={"place": "home", "path": "notes"})
    assert response.status_code == 200
    assert response.json()["entries"][0]["name"] == "plan.md"
    assert response.json()["notes"] == ["Nextcloud share state is unavailable."]
    assert get.call_args.kwargs["timeout"] == 5
    assert "Nextcloud share state" in caplog.text
    assert "sensitive-name" not in caplog.text


async def test_local_listing_does_not_fetch_share_state(client):
    from unittest.mock import patch
    with patch("istota.nextcloud.shares.list_shares") as shares:
        response = await client.get("/workspace/list", params={"place": "home"})
    assert response.status_code == 200
    shares.assert_not_called()


async def test_drive_not_granted(client, config):
    config.google_workspace.enabled = True
    response = await client.get("/workspace/drive/list")
    assert response.status_code == 409
    assert response.json() == {"error": "drive_not_granted"}


@pytest.mark.parametrize("grant", ["off", "readonly", "full"])
async def test_drive_route_uses_stored_grant_and_user(client, config, monkeypatch, grant):
    import httpx
    from datetime import datetime, timedelta, timezone
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    config.google_workspace.enabled = True
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    scopes = "[]" if grant == "off" else '["https://www.googleapis.com/auth/drive' + ('.readonly' if grant == 'readonly' else '') + '"]'
    with db.get_db(config.db_path) as conn:
        db.upsert_google_token(conn, "alice", "alice-token", "refresh", future, scopes)
        db.upsert_google_token(conn, "bob", "bob-token", "refresh", future, '["https://www.googleapis.com/auth/drive"]')
    requests = []
    def handle(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer alice-token"
        return httpx.Response(200, json={"files": [{"id": "file_123456", "name": "Plan", "mimeType": "application/pdf"}], "nextPageToken": "next"})
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    response = await client.get("/workspace/drive/list", params={"folder": "shared"})
    if grant == "off":
        assert response.status_code == 409 and not requests
    else:
        assert response.status_code == 200 and len(requests) == 1
        assert response.json()["next_page_token"] == "next"
        assert response.json()["items"][0]["name"] == "Plan"
    requests.clear()
    assert (await client.get("/workspace/drive/list", params={"folder": "bad'id"})).status_code == 400
    assert not requests


async def test_drive_auth_required(client, app):
    app.dependency_overrides.clear()
    assert (await client.get("/workspace/drive/list")).status_code == 401


async def test_drive_failed_refresh_returns_reconnect(client, config, monkeypatch):
    import httpx
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    config.google_workspace.enabled = True
    with db.get_db(config.db_path) as conn:
        db.upsert_google_token(conn, "alice", "old-token", "refresh", "expired", '["https://www.googleapis.com/auth/drive.readonly"]')
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: httpx.Response(401, json={"error": "invalid_grant"}))
    response = await client.get("/workspace/drive/list")
    assert response.status_code == 502
    assert response.json() == {"error": "Google rejected the stored credential. Reconnect in Settings."}


async def test_linked_file_place_lists_only_its_authorized_file(client, config):
    folder = config.workspace_path / "linked"
    folder.mkdir()
    (folder / "report.txt").write_text("report")
    (folder / "private.txt").write_text("private")
    with db.get_db(config.db_path) as conn:
        db.add_user_resource(conn, "alice", "shared_file", "linked/report.txt", permissions="read")
    places = (await client.get("/workspace/places")).json()["places"]
    place = next(p for p in places if p["kind"] == "linked")
    response = await client.get("/workspace/list", params={"place": place["id"]})
    assert response.status_code == 200
    entries = response.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["name"] == "report.txt"
    assert entries[0]["size"] == 6
    assert entries[0]["view_path"] == "/linked/report.txt"
    for path in ("report.txt", "../private.txt", "child"):
        response = await client.get("/workspace/list", params={"place": place["id"], "path": path})
        assert response.status_code == 404


async def test_delete_through_workspace_route(client, config):
    response = await client.delete("/workspace/files", params={"place": "home", "path": "notes", "name": "plan.md"})
    assert response.status_code == 200
    assert not (config.workspace_path / "Users/alice/notes/plan.md").exists()
    listing = await client.get("/workspace/list", params={"place": "home", "path": "notes"})
    assert listing.json()["entries"] == []


@pytest.mark.parametrize("place,path,name,status", [
    ("room:sample", "notes", "plan.md", 404),
    ("home", "../alice/notes", "plan.md", 404),
    ("home", "notes", "../notes/plan.md", 422),
    ("home", "", "notes", 409),
])
async def test_delete_refusal_keeps_files(client, config, place, path, name, status):
    response = await client.delete("/workspace/files", params={"place": place, "path": path, "name": name})
    assert response.status_code == status
    assert (config.workspace_path / "Users/alice/notes/plan.md").read_text() == "plan"


async def test_delete_requires_auth_and_origin(client, app, config):
    from fastapi import HTTPException
    from istota.webui.router_stubs import verify_origin

    def refuse():
        raise HTTPException(403, "cross-origin request")

    app.dependency_overrides[verify_origin] = refuse
    params = {"place": "home", "path": "notes", "name": "plan.md"}
    assert (await client.delete("/workspace/files", params=params)).status_code == 403
    app.dependency_overrides.clear()
    assert (await client.delete("/workspace/files", params=params)).status_code == 401
    assert (config.workspace_path / "Users/alice/notes/plan.md").exists()
