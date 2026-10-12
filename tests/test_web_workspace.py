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
