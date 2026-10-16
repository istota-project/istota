"""Group management through the host app, signed sessions and real SQLite."""
import base64
import json

import pytest
from httpx import ASGITransport, AsyncClient
from itsdangerous import TimestampSigner

from istota import db, user_profiles
from istota.config import Config, SiteConfig, WebConfig
from istota.webui import auth as web_auth

BASE = "/istota/api"
WRITES = [
    ("PATCH", "/groups/family"), ("PUT", "/groups/family/members/carol"),
    ("DELETE", "/groups/family/members/bob"), ("POST", "/admin/groups"),
    ("PATCH", "/admin/groups/family"), ("POST", "/admin/groups/family/archive"),
]
READS = ["/groups", "/groups/family", "/groups/family/user-options",
         "/admin/groups", "/admin/groups/family", "/admin/groups/user-options"]


@pytest.fixture
def configured(db_path, monkeypatch):
    from istota.webui import app as mod
    config = Config(db_path=db_path, site=SiteConfig(hostname="example.com"),
                    web=WebConfig(auth=["nextcloud"]), admin_users={"admin"})
    monkeypatch.setattr(mod, "_config", config)
    monkeypatch.setattr(mod.app.state, "istota_config", config, raising=False)
    for name in ("admin", "alice", "bob", "carol", "disabled"):
        user_profiles.ensure_profile(db_path, name, display_name=name.title())
    web_auth.upsert_identity(db_path, "disabled", "disabled@example.com")
    web_auth.set_disabled(db_path, "disabled", True)
    with db.get_db(db_path) as conn:
        db.create_group(conn, "family", kind="family", display_name="Family", created_by="admin")
        db.add_group_member(conn, "family", "alice", role="owner", added_by="admin")
        db.add_group_member(conn, "family", "bob", added_by="alice")
        db.create_group(conn, "private", kind="team", display_name="Private", created_by="admin")
        db.add_group_member(conn, "private", "carol", role="owner", added_by="admin")
        visible = db.create_web_chat_room(conn, "alice", "Visible")
        hidden = db.create_web_chat_room(conn, "carol", "Hidden")
        db.set_room_group(conn, visible.token, "family")
        db.set_room_group(conn, hidden.token, "family")
    return mod


def session(client, mod, username):
    identity = web_auth.get_identity(mod._config.db_path, username)
    data = {"user": {"username": username}, "auth": {
        "method": "nextcloud", "epoch": identity.credential_epoch if identity else 0}}
    secret = next(m.kwargs["secret_key"] for m in mod.app.user_middleware if m.cls.__name__ == "SessionMiddleware")
    value = TimestampSigner(str(secret)).sign(base64.b64encode(json.dumps(data).encode())).decode()
    client.cookies.set("istota_session", value, domain="example.com", path="/istota/")


@pytest.fixture
async def client(configured):
    async with AsyncClient(transport=ASGITransport(app=configured.app), base_url="https://example.com",
                           headers={"origin": "https://example.com"}) as client:
        session(client, configured, "alice")
        yield client


async def test_member_listing_detail_and_room_visibility(client):
    response = await client.get(BASE + "/groups")
    assert response.status_code == 200
    groups = response.json()["groups"]
    assert [g["group_id"] for g in groups] == ["family"]
    assert set(groups[0]) == {"group_id", "display_name", "kind", "role", "revision", "capabilities", "system_key"}
    assert groups[0]["role"] == "owner" and groups[0]["revision"] == 0
    assert groups[0]["capabilities"]["edit_memory"]
    detail = (await client.get(BASE + "/groups/family")).json()
    assert [(m["user_id"], m["display_name"]) for m in detail["members"]] == [("alice", "Alice"), ("bob", "Bob")]
    assert [r["name"] for r in detail["rooms"]] == ["Visible"]
    assert "carol" not in json.dumps(detail) and "history" not in detail
    assert "@" not in json.dumps(detail)


@pytest.mark.parametrize("method,path", [("GET", p) for p in READS] + WRITES)
async def test_session_required(client, method, path):
    client.cookies.clear()
    assert (await client.request(method, BASE + path, json={})).status_code == 401


@pytest.mark.parametrize("method,path", WRITES)
@pytest.mark.parametrize("origin", ["", "https://other.example.com"])
async def test_writes_require_real_origin_guard(client, configured, method, path, origin):
    session(client, configured, "admin")
    assert (await client.request(method, BASE + path, json={}, headers={"origin": origin})).status_code == 403


@pytest.mark.parametrize("method,suffix,payload", [
    ("GET", "", None), ("PATCH", "", {"revision": 0, "display_name": "Changed"}),
    ("GET", "/user-options", None),
    ("PUT", "/members/carol", {"revision": 0, "role": "member", "acknowledge_history": True}),
    ("DELETE", "/members/bob", {"revision": 0}),
])
async def test_member_routes_hide_unknown_archived_and_nonmember_groups(client, configured, method, suffix, payload):
    with db.get_db(configured._config.db_path) as conn:
        db.create_group(conn, "archived", kind="group", display_name="Archived", created_by="admin")
        db.add_group_member(conn, "archived", "alice", added_by="admin")
        db.archive_group(conn, "archived")
    responses = [await client.request(method, BASE + f"/groups/{gid}" + suffix, json=payload)
                 for gid in ("missing", "private", "archived")]
    assert [r.status_code for r in responses] == [404] * 3
    assert responses[0].json() == responses[1].json() == responses[2].json()
    session(client, configured, "admin")
    assert (await client.request(method, BASE + "/groups/family" + suffix, json=payload)).status_code == 404


async def test_member_cannot_manage_or_enumerate_users_but_can_leave(client, configured):
    session(client, configured, "bob")
    assert (await client.get(BASE + "/groups/family/user-options")).status_code == 403
    assert (await client.patch(BASE + "/groups/family", json={"revision": 0, "display_name": "X"})).status_code == 403
    assert (await client.put(BASE + "/groups/family/members/carol", json={"revision": 0, "role": "owner"})).status_code == 403
    assert (await client.request("DELETE", BASE + "/groups/family/members/bob", json={"revision": 0})).status_code == 200
    assert (await client.get(BASE + "/groups/family")).status_code == 404
    assert (await client.get(BASE + "/groups")).json() == {"groups": []}


async def test_history_acknowledgement_revision_and_rejoin(client, configured):
    url = BASE + "/groups/family/members/carol"
    payload = {"revision": 0, "role": "member"}
    response = await client.put(url, json=payload)
    assert response.status_code == 409
    assert response.json()["disclosure"] == {
        "memory": "Groups/family/GROUP.md", "rooms": [],
        "briefings": ["All retained group briefing editions, including those published before joining"],
    }
    with db.get_db(configured._config.db_path) as conn:
        assert not db.is_group_member(conn, "family", "carol")
        assert conn.execute("SELECT COUNT(*) FROM group_events").fetchone()[0] == 0
    payload["acknowledge_history"] = True
    assert (await client.put(url, json=payload)).json()["revision"] == 1
    assert (await client.patch(BASE + "/groups/family", json={"revision": 0, "kind": "team"})).status_code == 409
    assert (await client.request("DELETE", url, json={"revision": 1})).status_code == 200
    payload["revision"] = 2
    assert (await client.put(url, json=payload)).status_code == 200
    session(client, configured, "admin")
    detail = (await client.get(BASE + "/admin/groups/family")).json()
    rows = [row for row in detail["history"] if row["user_id"] == "carol"]
    assert len(rows) == 2 and rows[0]["ended_at"] and rows[1]["ended_at"] is None
    assert [e["event_type"] for e in detail["events"]] == ["member_added", "member_removed", "member_added"]


async def test_owner_replacement_and_admin_recovery_archive(client, configured):
    url = BASE + "/groups/family/members/alice"
    assert (await client.request("DELETE", url, json={"revision": 0})).json()["error"] == "last_owner"
    assert (await client.request("DELETE", url, json={"revision": 0, "replacement_owner": "bob"})).status_code == 200
    with db.get_db(configured._config.db_path) as conn:
        db.create_group(conn, "legacy", kind="group", display_name="Legacy", created_by="alice")
        db.add_group_member(conn, "legacy", "alice", added_by="admin")
    session(client, configured, "admin")
    groups = (await client.get(BASE + "/admin/groups")).json()["groups"]
    assert next(g for g in groups if g["group_id"] == "legacy")["needs_owner"]
    response = await client.patch(BASE + "/admin/groups/legacy", json={"revision": 0, "user_id": "alice", "role": "owner"})
    assert response.status_code == 200 and response.json()["revision"] == 1
    assert (await client.post(BASE + "/admin/groups/legacy/archive", json={"revision": 1})).status_code == 409
    assert (await client.post(BASE + "/admin/groups/legacy/archive", json={"revision": 1, "confirm": True})).status_code == 200
    assert (await client.get(BASE + "/admin/groups/legacy")).json()["archived_at"]
    session(client, configured, "alice")
    assert (await client.get(BASE + "/groups/legacy")).status_code == 404


async def test_create_and_enabled_user_options(client, configured):
    options = (await client.get(BASE + "/groups/family/user-options")).json()["users"]
    assert {u["user_id"] for u in options} == {"admin", "alice", "bob", "carol"}
    assert all(set(u) == {"user_id", "display_name"} for u in options)
    session(client, configured, "admin")
    assert (await client.get(BASE + "/admin/groups/user-options")).json()["users"] == options
    payload = {"group_id": "new", "display_name": "New", "owners": ["alice"], "revision": 0}
    response = await client.post(BASE + "/admin/groups", json=payload)
    assert response.status_code == 200
    assert response.json()["storage_available"] is False
    assert (await client.get(BASE + "/groups")).json() == {"groups": []}
    for owners in (["disabled"], ["unknown"], []):
        payload.update(group_id="invalid", owners=owners)
        assert (await client.post(BASE + "/admin/groups", json=payload)).status_code == 422


@pytest.mark.parametrize("method,path", [("GET", p) for p in READS if p.startswith("/admin/")] + [w for w in WRITES if w[1].startswith("/admin/")])
async def test_admin_routes_fail_closed(client, configured, method, path):
    assert (await client.request(method, BASE + path, json={})).status_code == 403
    session(client, configured, "admin")
    configured._config.admin_users = set()
    assert (await client.request(method, BASE + path, json={})).status_code == 403


@pytest.mark.parametrize("payload", [[], {"revision": True}, {"revision": "0"}, {"revision": 0, "display_name": None},
                                     {"revision": 0, "is_admin": True}, {"revision": 0, "group_id": "other"}])
async def test_strict_payloads(client, payload):
    assert (await client.patch(BASE + "/groups/family", json=payload)).status_code == 422


async def test_bounded_stream_and_invalid_json(client):
    async def body():
        yield b"{" + b" " * (64 * 1024)
        pytest.fail("body consumed beyond cap")
    response = await client.patch(BASE + "/groups/family", content=body())
    assert response.status_code == 413
    assert (await client.patch(BASE + "/groups/family", content=b"{bad")).status_code == 422


async def test_disabled_session_and_live_navigation(client, configured):
    assert (await client.get(BASE + "/me")).json()["features"]["groups"] is True
    session(client, configured, "admin")
    assert (await client.get(BASE + "/me")).json()["features"]["groups"] is True
    session(client, configured, "bob")
    with db.get_db(configured._config.db_path) as conn:
        db.end_group_membership(conn, "family", "bob", ended_by="alice")
    assert (await client.get(BASE + "/me")).json()["features"]["groups"] is False
    session(client, configured, "disabled")
    assert (await client.get(BASE + "/groups")).status_code == 401


async def test_new_profile_and_disabled_membership_target_are_live(client, configured):
    user_profiles.ensure_profile(configured._config.db_path, "dana", display_name="Dana")
    options = (await client.get(BASE + "/groups/family/user-options")).json()["users"]
    assert {"user_id": "dana", "display_name": "Dana"} in options
    payload = {"revision": 0, "role": "member", "acknowledge_history": True}
    assert (await client.put(BASE + "/groups/family/members/dana", json=payload)).status_code == 200
    web_auth.upsert_identity(configured._config.db_path, "carol", "carol@example.com")
    web_auth.set_disabled(configured._config.db_path, "carol", True)
    payload["revision"] = 1
    assert (await client.put(BASE + "/groups/family/members/carol", json=payload)).status_code == 422


async def test_admin_patch_rejects_mixed_operations_without_partial_edit(client, configured):
    session(client, configured, "admin")
    response = await client.patch(BASE + "/admin/groups/family", json={
        "revision": 0, "display_name": "Changed", "user_id": "bob", "role": "owner"})
    assert response.status_code == 422
    with db.get_db(configured._config.db_path) as conn:
        assert db.get_group(conn, "family")["display_name"] == "Family"
        assert db.get_group(conn, "family")["revision"] == 0


@pytest.mark.parametrize("method,path", WRITES)
async def test_all_writes_reject_unknown_authority_fields(client, configured, method, path):
    session(client, configured, "admin")
    response = await client.request(method, BASE + path, json={"revision": 0, "is_operator": True})
    assert response.status_code == 422


async def test_system_group_capabilities_and_write_refusals(client, configured):
    from istota.groups.system import refresh_all_users
    refresh_all_users(configured._config, provision=True)
    with db.get_db(configured._config.db_path) as conn:
        gid = conn.execute("SELECT group_id FROM groups WHERE system_key = 'all_users'").fetchone()["group_id"]
    detail = (await client.get(BASE + f"/groups/{gid}")).json()
    assert detail["system_key"] == "all_users"
    assert not detail["capabilities"]["leave"] and not detail["capabilities"]["author_briefings"]
    assert "disabled" not in {m["user_id"] for m in detail["members"]}
    revision = detail["revision"]
    response = await client.request("DELETE", BASE + f"/groups/{gid}/members/alice", json={"revision": revision})
    assert response.status_code == 403 and response.json()["error"] == "automatic_membership"
    session(client, configured, "admin")
    detail = (await client.get(BASE + f"/admin/groups/{gid}")).json()
    assert not detail["needs_owner"] and detail["capabilities"]["author_briefings"]
    public_detail = (await client.get(BASE + f"/groups/{gid}")).json()
    assert public_detail["capabilities"]["author_briefings"]
    response = await client.patch(BASE + f"/admin/groups/{gid}", json={"revision": revision, "user_id": "alice", "role": "owner"})
    assert response.status_code == 403 and response.json()["error"] == "automatic_membership"
    response = await client.post(BASE + f"/admin/groups/{gid}/archive", json={"revision": revision, "confirm": True})
    assert response.status_code == 403 and response.json()["error"] == "system_group"
    assert (await client.patch(BASE + f"/admin/groups/{gid}", json={"revision": revision, "system_key": None})).status_code == 422
