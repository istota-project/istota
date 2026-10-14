"""Notebook API against signed sessions, SQLite and real files."""
# ruff: noqa: F811 -- imported shared pytest fixtures
import os
from contextlib import contextmanager

import pytest

from istota import db, storage
from istota.workspace import text_files
from tests.test_group_routes import BASE, configured, client, session  # noqa: F401

URL = BASE + "/groups/family/memory"


@pytest.fixture
def notebook(configured, tmp_path):
    config = configured._config
    config.workspace_path = tmp_path / "workspace"
    config.workspace_path.mkdir()
    storage.ensure_group_directories(config, "family")
    path = config.workspace_path / "Groups/family/GROUP.md"
    path.write_text("## Notes\n\n- original\n")
    return path


async def test_member_save_conflict_and_audit(client, configured, notebook):
    session(client, configured, "bob")
    loaded = (await client.get(URL)).json()
    assert loaded["content"] == notebook.read_text()
    result = await client.put(URL, json={"content": "## Notes\n\n- edited\n", "revision": loaded["revision"]})
    assert result.status_code == 200
    assert result.json()["audit_recorded"] is True
    assert result.json()["status"] == "ok"
    latest = (await client.get(URL)).json()["last_change"]
    assert latest["user_id"] == "bob" and latest["after_revision"] == result.json()["revision"]
    assert result.json()["revision"] == text_files.revision(notebook.read_text())
    conflict = await client.put(URL, json={"content": "stale", "revision": loaded["revision"]})
    assert conflict.status_code == 409
    activity = (await client.get(URL + "/activity")).json()
    [entry] = activity["entries"]
    assert entry["source"] == "web" and entry["user_id"] == "bob"
    assert entry["before_revision"] == loaded["revision"]
    assert entry["after_revision"] == result.json()["revision"]
    assert entry["ts"] and activity["next_cursor"] is None


@pytest.mark.parametrize("kind,status", [("large", 413), ("encoding", 422), ("symlink", 422), ("fifo", 422)])
async def test_refused_get_and_put_preserve_the_file(client, notebook, monkeypatch, kind, status):
    if kind == "large":
        monkeypatch.setattr(storage, "USER_CONFIG_READ_CAP_BYTES", 64)
        notebook.write_bytes(b"x" * 65)
    elif kind == "encoding":
        notebook.write_bytes(b"\xff")
    else:
        notebook.unlink()
        if kind == "symlink":
            target = notebook.with_name("other.md")
            target.write_text("private")
            notebook.symlink_to(target)
        else:
            os.mkfifo(notebook)
    before = notebook.lstat()
    def never_write(*args):
        pytest.fail("a refused notebook must never invoke the writer")
    monkeypatch.setattr(storage, "write_group_memory", never_write)
    for response in [await client.get(URL), await client.put(URL, json={"content": "replace", "revision": text_files.revision("")})]:
        assert response.status_code == status
        assert response.json()["error"] == "memory_unreadable"
        assert "content" not in response.json() and "revision" not in response.json()
    after = notebook.lstat()
    assert (after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns) == (
        before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns)
    if kind == "symlink":
        assert notebook.is_symlink() and target.read_text() == "private"
    elif kind != "fifo":
        assert notebook.read_bytes() == (b"\xff" if kind == "encoding" else b"x" * 65)


async def test_missing_leaf_is_creatable_but_missing_root_is_not(client, notebook):
    notebook.unlink()
    loaded = await client.get(URL)
    assert loaded.status_code == 200 and loaded.json()["status"] == "missing"
    response = await client.put(URL, json={"content": "created", "revision": loaded.json()["revision"]})
    assert response.status_code == 200 and notebook.read_text() == "created"
    notebook.unlink()
    notebook.parent.rename(notebook.parent.with_name("unavailable"))
    assert (await client.get(URL)).status_code == 503
    response = await client.put(URL, json={"content": "created", "revision": text_files.revision("")})
    assert response.status_code == 503 and not notebook.exists()


async def test_locked_reread_refuses_a_changed_file(client, notebook, monkeypatch):
    loaded = (await client.get(URL)).json()
    original = text_files.file_lock
    @contextmanager
    def swapped(*args, **kwargs):
        with original(*args, **kwargs):
            notebook.write_bytes(b"\xff")
            yield
    monkeypatch.setattr(text_files, "file_lock", swapped)
    response = await client.put(URL, json={"content": "replacement", "revision": loaded["revision"]})
    assert response.status_code == 422 and notebook.read_bytes() == b"\xff"


async def test_membership_rechecked_after_lock(client, configured, notebook, monkeypatch):
    loaded = (await client.get(URL)).json()
    original = text_files.file_lock
    @contextmanager
    def removed(*args, **kwargs):
        with original(*args, **kwargs):
            with db.get_db(configured._config.db_path) as conn:
                db.end_group_membership(conn, "family", "alice", ended_by="admin")
            yield
    monkeypatch.setattr(text_files, "file_lock", removed)
    response = await client.put(URL, json={"content": "replacement", "revision": loaded["revision"]})
    assert response.status_code == 404
    assert notebook.read_text() == loaded["content"]


async def test_failed_audit_still_reports_success(client, notebook, monkeypatch):
    from istota.memory.curation import audit
    loaded = (await client.get(URL)).json()
    def fail(*args, **kwargs):
        raise OSError("unavailable")
    monkeypatch.setattr(audit.db, "group_kv_set", fail)
    response = await client.put(URL, json={"content": "written", "revision": loaded["revision"]})
    assert response.status_code == 200 and response.json()["audit_recorded"] is False
    assert notebook.read_text() == "written"


@pytest.mark.parametrize("suffix", ["", "/activity"])
async def test_member_content_is_hidden_from_admin_nonmember(client, configured, notebook, suffix):
    session(client, configured, "admin")
    responses = [await client.get(BASE + f"/groups/{gid}/memory" + suffix) for gid in ("family", "missing")]
    assert responses[0].status_code == responses[1].status_code == 404
    assert responses[0].json() == responses[1].json()
    assert (await client.put(URL, json={"content": "x", "revision": "x"})).status_code == 404


async def test_storage_retry_is_admin_only_and_idempotent(client, configured, notebook):
    url = BASE + "/admin/groups/family/storage"
    assert (await client.post(url, json={})).status_code == 403
    session(client, configured, "admin")
    before = notebook.read_bytes()
    assert (await client.post(url, json={})).json()["storage_available"] is True
    assert notebook.read_bytes() == before


async def test_notebook_routes_require_session_and_origin(client, configured, notebook):
    assert (await client.put(URL, json={}, headers={"origin": "https://other.example.com"})).status_code == 403
    session(client, configured, "admin")
    assert (await client.post(BASE + "/admin/groups/family/storage", json={}, headers={"origin": ""})).status_code == 403
    client.cookies.clear()
    for method, url in [("GET", URL), ("PUT", URL), ("GET", URL + "/activity"),
                        ("POST", BASE + "/admin/groups/family/storage")]:
        assert (await client.request(method, url, json={})).status_code == 401


@pytest.mark.parametrize("payload", [{}, {"content": 1, "revision": "x"},
    {"content": "x", "revision": "x", "other": True}, {"content": "\ud800", "revision": "x"}])
async def test_invalid_save_payload_is_rejected(client, notebook, payload):
    import json
    before = notebook.read_bytes()
    response = await client.put(URL, content=json.dumps(payload).encode())
    assert response.status_code == 422 and notebook.read_bytes() == before


async def test_activity_pages_are_bounded_newest_first(client, configured, notebook):
    from istota.memory.curation import audit
    for number in range(3):
        assert audit.write_group_audit_log(configured._config, "family", "alice",
            applied=[{"op": {"op": "append", "line": str(number)}, "outcome": "applied"}], rejected=[])
    first = (await client.get(URL + "/activity?limit=2")).json()
    assert [e["applied"][0]["op"]["line"] for e in first["entries"]] == ["2", "1"]
    second = (await client.get(URL + "/activity", params={"cursor": first["next_cursor"], "limit": 2})).json()
    assert [e["applied"][0]["op"]["line"] for e in second["entries"]] == ["0"]
    assert second["next_cursor"] is None
    assert (await client.get(URL + "/activity?limit=101")).status_code == 422


async def test_lock_timeout_and_storage_io_are_reported(client, notebook, monkeypatch):
    loaded = (await client.get(URL)).json()
    @contextmanager
    def locked(*args, **kwargs):
        raise text_files.MemoryMdLocked()
        yield
    monkeypatch.setattr(text_files, "file_lock", locked)
    response = await client.put(URL, json={"content": "replacement", "revision": loaded["revision"]})
    assert response.status_code == 423 and notebook.read_text() == loaded["content"]


async def test_read_io_failure_is_not_an_empty_notebook(client, notebook, monkeypatch):
    from istota.skills import _loader
    def unreadable(*args, **kwargs):
        return None, _loader.OVERLAY_UNREADABLE, None
    monkeypatch.setattr(_loader, "read_overlay_bytes", unreadable)
    for response in [await client.get(URL), await client.put(URL, json={"content": "x", "revision": text_files.revision("")})]:
        assert response.status_code == 503
        assert "content" not in response.json() and "revision" not in response.json()
    assert "original" in notebook.read_text()


async def test_directory_io_failure_is_storage_unavailable(client, notebook, monkeypatch):
    import errno
    from istota.skills import _loader
    def unavailable(*args, **kwargs):
        raise OSError(errno.EIO, "unavailable")
    monkeypatch.setattr(_loader, "open_overlay_dir", unavailable)
    response = await client.get(URL)
    assert response.status_code == 503
    assert response.json()["reason"] == "storage_unavailable"


async def test_symlinked_group_directory_is_refused(client, notebook):
    original = notebook.parent.with_name("original")
    notebook.parent.rename(original)
    notebook.parent.symlink_to(original, target_is_directory=True)
    for response in [await client.get(URL), await client.put(URL, json={"content": "x", "revision": text_files.revision("")})]:
        assert response.status_code == 422 and "content" not in response.json()
    assert "original" in (original / "GROUP.md").read_text()
