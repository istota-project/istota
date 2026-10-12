"""Pins through the authenticated router and the real user-scoped store."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from istota import db
from istota.config import Config
from istota.webui.workspace_routes import router
from istota.webui.router_stubs import require_auth, verify_origin


@pytest.fixture
def config(tmp_path):
    config = Config(db_path=tmp_path / "istota.db", workspace_path=tmp_path / "workspace")
    db.init_db(config.db_path)
    folder = config.workspace_path / "Users/alice/notes"
    folder.mkdir(parents=True)
    (folder / "plan.md").write_text("plan")
    return config


@pytest.fixture
def app(config):
    app = FastAPI()
    app.state.istota_config = config
    app.include_router(router, prefix="/workspace")
    app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
    return app


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://example.com") as client:
        yield client


async def test_pin_upsert_and_availability(client, config):
    body = {"provider": "workspace", "ref": "/Users/alice/notes/plan.md", "name": "Plan"}
    first = await client.put("/workspace/pins", json=body)
    assert first.status_code == 200
    pin = first.json()["pin"]
    assert pin["available"] and not pin["is_dir"]
    body["name"] = "Updated plan"
    again = await client.put("/workspace/pins", json=body)
    assert again.json()["pin"]["id"] == pin["id"]
    (config.workspace_path / "Users/alice/notes/plan.md").unlink()
    pins = (await client.get("/workspace/pins")).json()["pins"]
    assert len(pins) == 1 and pins[0]["name"] == "Updated plan"
    assert not pins[0]["available"]


async def test_directory_and_live_room_roots(client, config):
    with db.get_db(config.db_path) as conn:
        room = db.create_web_chat_room(conn, "bob", "Shared")
        db.add_room_member(conn, room.token, "alice")
    folder = config.workspace_path / "Channels" / room.token
    folder.mkdir(parents=True)
    (folder / "plan.md").write_text("plan")
    for ref in ("/Users/alice/notes", f"/Channels/{room.token}/plan.md"):
        response = await client.put("/workspace/pins", json={"provider": "workspace", "ref": ref, "name": "Plan"})
        assert response.status_code == 200
        assert response.json()["pin"]["is_dir"] == ref.endswith("notes")
    with db.get_db(config.db_path) as conn:
        db.remove_room_member(conn, room.token, "alice")
    pins = (await client.get("/workspace/pins")).json()["pins"]
    assert [p["available"] for p in pins] == [True, False]


@pytest.mark.parametrize("ref", ["/Users/bob/secret", "/Talk/file", "/Users/alicex/file", "/Users/alice/missing", "/Users/alice/outside", "/Users/alice/linkdir"])
async def test_refused_workspace_pin(client, config, tmp_path, ref):
    (tmp_path / "secret").write_text("private")
    (config.workspace_path / "Users/alice/outside").symlink_to(tmp_path / "secret")
    (config.workspace_path / "Users/alice/linkdir").symlink_to(tmp_path, target_is_directory=True)
    response = await client.put("/workspace/pins", json={"provider": "workspace", "ref": ref, "name": "No"})
    assert response.status_code == 404


async def test_drive_validation_and_user_scoped_delete(client, app):
    body = {"provider": "drive", "ref": "file_123456", "name": "x" * 220, "mime_type": "m" * 110, "web_url": "https://docs.google.com.evil.example/doc"}
    response = await client.put("/workspace/pins", json=body)
    assert response.status_code == 200
    pin = response.json()["pin"]
    assert len(pin["name"]) == 200 and len(pin["mime_type"]) == 100
    assert pin["web_url"] is None
    app.dependency_overrides[require_auth] = lambda: {"username": "bob"}
    assert (await client.get("/workspace/pins")).json() == {"pins": []}
    assert (await client.delete(f"/workspace/pins/{pin['id']}")).status_code == 404
    app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
    assert (await client.delete(f"/workspace/pins/{pin['id']}")).status_code == 200
    assert (await client.put("/workspace/pins", json={**body, "ref": "bad'id"})).status_code == 400


@pytest.mark.parametrize("body", [None, [], {}, {"provider": "other", "ref": "x", "name": "x"}, {"provider": "drive", "ref": 1, "name": "x"}, {"provider": "drive", "ref": "file_123456", "name": "x", "web_url": []}])
async def test_malformed_pin_does_not_echo_payload(client, body):
    response = await client.put("/workspace/pins", json=body)
    assert response.status_code == 422
    assert response.json() == {"error": "Invalid pin."}


async def test_auth_and_csrf(client, app):
    def refuse():
        raise HTTPException(403)
    app.dependency_overrides[verify_origin] = refuse
    assert (await client.put("/workspace/pins", json={})).status_code == 403
    assert (await client.delete("/workspace/pins/1")).status_code == 403
    app.dependency_overrides.clear()
    assert (await client.get("/workspace/pins")).status_code == 401
    assert (await client.put("/workspace/pins", json={})).status_code == 401
    assert (await client.delete("/workspace/pins/1")).status_code == 401


async def test_limit_allows_updates(client):
    for n in range(50):
        response = await client.put("/workspace/pins", json={"provider": "drive", "ref": f"file_{n:010}", "name": "Plan"})
        assert response.status_code == 200
    body = {"provider": "drive", "ref": "file_0000000000", "name": "Updated"}
    assert (await client.put("/workspace/pins", json=body)).status_code == 200
    response = await client.put("/workspace/pins", json={**body, "ref": "file_extra_1"})
    assert response.status_code == 409 and response.json() == {"error": "pin_limit"}


def test_simultaneous_last_slot_is_atomic(config):
    with db.get_db(config.db_path) as conn:
        for n in range(49):
            db.add_pin(conn, "alice", "drive", f"file_{n:010}", "Plan")
    barrier = Barrier(2)
    def add(n):
        with db.get_db(config.db_path) as conn:
            barrier.wait()
            return db.add_pin(conn, "alice", "drive", f"last_slot_{n}", "Last")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(add, range(2)))
    assert sum(result is not None for result in results) == 1
    with db.get_db(config.db_path) as conn:
        assert len(db.list_pins(conn, "alice")) == 50
