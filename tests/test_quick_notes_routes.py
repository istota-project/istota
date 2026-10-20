"""POST and PUT /notes over a real database and filesystem."""

import errno
import os

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from istota import db, user_profiles
from istota.config import Config
from istota.webui import workspace_routes
from istota.webui.router_stubs import require_auth
from istota.workspace import quick_notes
from istota.workspace.text_files import revision

FOLDER = "Notes/Inbox"


@pytest.fixture
def config(tmp_path):
    config = Config(db_path=tmp_path / "istota.db", workspace_path=tmp_path / "workspace")
    db.init_db(config.db_path)
    (config.workspace_path / "Users/alice").mkdir(parents=True)
    user_profiles.ensure_profile(config.db_path, "alice")
    user_profiles.update_profile(config.db_path, "alice", quick_notes_folder=FOLDER)
    return config


@pytest.fixture
def home(config):
    return config.workspace_path / "Users/alice"


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


def _keys(config):
    with db.get_db(config.db_path) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT client_note_id, path FROM quick_note_keys WHERE user_id = 'alice'"
        )]


async def test_create_writes_exactly_the_content_into_a_new_folder(client, home):
    content = "Buy milk\n- eggs\n- bread\n"
    assert not (home / "Notes").exists()
    response = await client.post("/workspace/notes", json={"content": content})
    assert response.status_code == 201
    assert response.json() == {
        "path": "Notes/Inbox/Buy milk.md", "name": "Buy milk.md",
        "revision": revision(content), "created": True,
    }
    assert (home / "Notes/Inbox/Buy milk.md").read_bytes() == content.encode()


async def test_a_name_collision_is_numbered(client, home):
    first = await client.post("/workspace/notes", json={"content": "Buy milk"})
    second = await client.post("/workspace/notes", json={"content": "Buy milk\nagain"})
    assert first.json()["name"] == "Buy milk.md"
    assert second.status_code == 201
    assert second.json()["path"] == "Notes/Inbox/Buy milk (2).md"
    assert (home / "Notes/Inbox/Buy milk (2).md").read_text() == "Buy milk\nagain"


async def test_an_unset_folder_is_a_409(client, config, home):
    user_profiles.update_profile(config.db_path, "alice", quick_notes_folder=None)
    response = await client.post("/workspace/notes", json={"content": "Buy milk"})
    assert response.status_code == 409
    assert response.json()["code"] == "notes_folder_unset"
    assert not (home / "Notes").exists()


@pytest.mark.parametrize("content", ["", "   \n\t\n"])
async def test_empty_content_is_a_422(client, home, content):
    response = await client.post("/workspace/notes", json={"content": content})
    assert response.status_code == 422
    assert response.json()["code"] == "empty"
    assert not (home / "Notes").exists()


async def test_oversized_content_is_a_413(client, home):
    content = "x" * (quick_notes.QUICK_NOTE_MAX_BYTES + 1)
    response = await client.post("/workspace/notes", json={"content": content})
    assert response.status_code == 413
    assert response.json()["code"] == "too_large"
    assert not (home / "Notes").exists()


@pytest.mark.parametrize("body", [{}, {"content": 5}, {"content": "x", "client_note_id": 7}, ["x"]])
async def test_a_malformed_body_is_a_400(client, body):
    response = await client.post("/workspace/notes", json=body)
    assert response.status_code == 400


@pytest.mark.parametrize("key", ["short", "has space-in-it", "x" * 65, "abc/defghi"])
async def test_a_malformed_client_note_id_is_a_400(client, config, key):
    response = await client.post("/workspace/notes", json={"content": "Buy milk", "client_note_id": key})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_client_note_id"
    assert _keys(config) == []


async def test_the_same_client_note_id_replays_to_one_file(client, config, home):
    body = {"content": "Buy milk", "client_note_id": "0F8FAD5B-D9CB-469F-A165-70867728950E"}
    first = await client.post("/workspace/notes", json=body)
    second = await client.post("/workspace/notes", json=body)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == {**first.json(), "created": False}
    assert sorted(p.name for p in (home / "Notes/Inbox").iterdir()) == ["Buy milk.md"]
    assert _keys(config) == [{"client_note_id": body["client_note_id"], "path": "Notes/Inbox/Buy milk.md"}]


async def test_a_replay_reports_the_files_current_revision(client, home):
    body = {"content": "Buy milk", "client_note_id": "note-key-0001"}
    await client.post("/workspace/notes", json=body)
    (home / "Notes/Inbox/Buy milk.md").write_text("Buy milk\nedited in Files")
    response = await client.post("/workspace/notes", json=body)
    assert response.json()["revision"] == revision("Buy milk\nedited in Files")


async def test_a_replay_after_the_file_was_deleted_does_not_recreate_it(client, home):
    body = {"content": "Buy milk", "client_note_id": "note-key-0002"}
    await client.post("/workspace/notes", json=body)
    (home / "Notes/Inbox/Buy milk.md").unlink()
    response = await client.post("/workspace/notes", json=body)
    assert response.status_code == 200
    assert response.json() == {
        "path": "Notes/Inbox/Buy milk.md", "name": "Buy milk.md", "revision": None, "created": False,
    }
    assert not (home / "Notes/Inbox/Buy milk.md").exists()


async def test_a_claimed_key_with_no_path_is_in_progress(client, config, home):
    with db.get_db(config.db_path) as conn:
        conn.execute(
            "INSERT INTO quick_note_keys (user_id, client_note_id) VALUES ('alice', 'note-key-0003')"
        )
    response = await client.post("/workspace/notes", json={"content": "Buy milk", "client_note_id": "note-key-0003"})
    assert response.status_code == 409
    assert response.json()["code"] == "in_progress"
    assert not (home / "Notes").exists()


async def test_a_failed_write_frees_the_key(client, config, home, monkeypatch):
    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(quick_notes, "write_new_file", full)
    body = {"content": "Buy milk", "client_note_id": "note-key-0004"}
    response = await client.post("/workspace/notes", json=body)
    assert response.status_code == 507
    assert _keys(config) == []
    monkeypatch.undo()
    response = await client.post("/workspace/notes", json=body)
    assert response.status_code == 201
    assert (home / "Notes/Inbox/Buy milk.md").read_text() == "Buy milk"


async def test_keys_older_than_thirty_days_are_pruned(client, config):
    with db.get_db(config.db_path) as conn:
        conn.execute(
            "INSERT INTO quick_note_keys (user_id, client_note_id, path, created_at)"
            " VALUES ('alice', 'note-key-old1', 'Notes/Inbox/old.md', datetime('now', '-31 days'))"
        )
        conn.execute(
            "INSERT INTO quick_note_keys (user_id, client_note_id, path, created_at)"
            " VALUES ('alice', 'note-key-new1', 'Notes/Inbox/new.md', datetime('now', '-29 days'))"
        )
    await client.post("/workspace/notes", json={"content": "Buy milk"})
    assert sorted(row["client_note_id"] for row in _keys(config)) == ["note-key-new1"]


async def test_a_folder_reached_through_a_symlink_is_refused(client, home, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (home / "Notes").mkdir()
    os.symlink(elsewhere, home / "Notes/Inbox")
    response = await client.post("/workspace/notes", json={"content": "Buy milk"})
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_folder"
    assert list(elsewhere.iterdir()) == []


async def _created(client, content="Buy milk"):
    response = await client.post("/workspace/notes", json={"content": content})
    assert response.status_code == 201
    return response.json()


async def test_put_with_the_current_revision_saves(client, home):
    note = await _created(client)
    response = await client.put("/workspace/notes", json={
        "path": note["path"], "content": "Buy milk\nand eggs", "revision": note["revision"],
    })
    assert response.status_code == 200
    assert response.json() == {"revision": revision("Buy milk\nand eggs")}
    assert (home / "Notes/Inbox/Buy milk.md").read_text() == "Buy milk\nand eggs"


async def test_put_with_a_stale_revision_is_a_conflict(client, home):
    note = await _created(client)
    (home / "Notes/Inbox/Buy milk.md").write_text("changed elsewhere")
    response = await client.put("/workspace/notes", json={
        "path": note["path"], "content": "mine", "revision": note["revision"],
    })
    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert (home / "Notes/Inbox/Buy milk.md").read_text() == "changed elsewhere"


@pytest.mark.parametrize("path", [
    "Notes/Inbox/../Inbox/Buy milk.md",
    "../alice/Notes/Inbox/Buy milk.md",
    "/Notes/Inbox/Buy milk.md",
    "Notes/Inbox/Buy milk.txt",
    "Notes/Inbox/Buy milk",
    "Notes/Other/Buy milk.md",
    "Notes/Inbox/sub/Buy milk.md",
    "Notes/Buy milk.md",
    "Notes//Inbox/Buy milk.md",
    "Notes/Inbox/",
    "Notes/Inbox/bad\0name.md",
])
async def test_put_refuses_a_path_outside_the_folder(client, home, path):
    note = await _created(client)
    (home / "Notes/Other").mkdir()
    (home / "Notes/Other/Buy milk.md").write_text("Buy milk")
    response = await client.put("/workspace/notes", json={
        "path": path, "content": "overwritten", "revision": note["revision"],
    })
    assert response.status_code == 403
    assert response.json()["code"] == "outside_folder"
    assert (home / "Notes/Inbox/Buy milk.md").read_text() == "Buy milk"
    assert (home / "Notes/Other/Buy milk.md").read_text() == "Buy milk"


async def test_put_follows_the_current_folder(client, config, home):
    note = await _created(client)
    user_profiles.update_profile(config.db_path, "alice", quick_notes_folder="Notes/Later")
    response = await client.put("/workspace/notes", json={
        "path": note["path"], "content": "overwritten", "revision": note["revision"],
    })
    assert response.status_code == 403
    assert response.json()["code"] == "outside_folder"


async def test_put_never_creates_a_missing_file(client, home):
    await _created(client)
    response = await client.put("/workspace/notes", json={
        "path": "Notes/Inbox/Gone.md", "content": "resurrected", "revision": revision(""),
    })
    assert response.status_code == 404
    assert response.json()["code"] == "missing"
    assert not (home / "Notes/Inbox/Gone.md").exists()


async def test_put_never_creates_a_missing_folder(client, config, home):
    user_profiles.update_profile(config.db_path, "alice", quick_notes_folder="Notes/Never")
    response = await client.put("/workspace/notes", json={
        "path": "Notes/Never/Buy milk.md", "content": "x", "revision": revision(""),
    })
    assert response.status_code == 422
    assert not (home / "Notes/Never").exists()


async def test_put_refuses_a_symlinked_folder(client, home, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "Buy milk.md").write_text("Buy milk")
    (home / "Notes").mkdir()
    os.symlink(elsewhere, home / "Notes/Inbox")
    response = await client.put("/workspace/notes", json={
        "path": "Notes/Inbox/Buy milk.md", "content": "overwritten", "revision": revision("Buy milk"),
    })
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_folder"
    assert (elsewhere / "Buy milk.md").read_text() == "Buy milk"


async def test_put_refuses_a_symlinked_file(client, home, tmp_path):
    await _created(client, "Other note")
    target = tmp_path / "target.md"
    target.write_text("Buy milk")
    os.symlink(target, home / "Notes/Inbox/Buy milk.md")
    response = await client.put("/workspace/notes", json={
        "path": "Notes/Inbox/Buy milk.md", "content": "overwritten", "revision": revision("Buy milk"),
    })
    assert response.status_code == 422
    assert target.read_text() == "Buy milk"


async def test_put_requires_path_content_and_revision(client):
    note = await _created(client)
    for missing in ("path", "content", "revision"):
        body = {"path": note["path"], "content": "x", "revision": note["revision"]}
        del body[missing]
        assert (await client.put("/workspace/notes", json=body)).status_code == 400


@pytest.mark.parametrize("method", ["post", "put"])
async def test_notes_routes_require_a_session(client, app, method):
    app.dependency_overrides.clear()
    body = {"content": "x", "path": "Notes/Inbox/x.md", "revision": revision("")}
    response = await getattr(client, method)("/workspace/notes", json=body)
    assert response.status_code == 401
