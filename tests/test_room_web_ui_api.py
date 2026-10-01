"""The web UI's half of multiplayer (Stage 17, speech-gate draft SG 12).

What the room settings modal, the members and grants panes and the transcript
need from the server, and nothing the client could work out for itself:

- the room listing says whether a room is shared, who hosts it, and why this
  caller may not change its settings, so the modal can show them read-only
  and the chat page can say when a room has lost its host;
- a member claims a hostless room, and the host sets `guest_reply`, over the
  same rules `!room host` and `!room guests` apply;
- the grants pane reads and writes the caller's own grants, and nobody else's;
- a transcript row says when the viewer may not delete it, from the same
  owner rule the delete endpoint enforces;
- the settings pages refuse a shared room as a delivery destination at save,
  and mark it in the list, instead of accepting a pin delivery then drops.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from istota import db, room_policy
from istota.config import Config, SiteConfig, UserConfig, WebConfig

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

if _has_web_deps:
    from httpx import ASGITransport, AsyncClient

ORIGIN = {"origin": "https://example.com"}
pytestmark = pytest.mark.skipif(not _has_web_deps, reason="web dependencies not installed")


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


def _config(db_path, tmp_path):
    return Config(
        db_path=db_path,
        workspace_path=tmp_path / "mount",
        site=SiteConfig(hostname="example.com"),
        users={
            "alice": UserConfig(display_name="Alice"),
            "bob": UserConfig(display_name="Bob"),
            "carol": UserConfig(),
        },
        web=WebConfig(
            enabled=True, port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web", oauth2_client_secret="s",
            session_secret_key="test-session-key",
        ),
        bot_name="Istota",
    )


@pytest.fixture
async def client(db_path, tmp_path):
    import istota.web_app as mod
    config = _config(db_path, tmp_path)
    mod._config = config
    mod.app.state.istota_config = config
    mod._oauth = MagicMock()
    mod._oauth.nextcloud = MagicMock()
    transport = ASGITransport(app=mod.app)
    async with AsyncClient(transport=transport, base_url="https://example.com") as c:
        yield c


async def _login(client, username):
    import istota.web_app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(
        return_value={"user_id": username},
    )
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


def _shared_room(db_path, name="plans"):
    """A web room alice created and bob was added to."""
    with db.get_db(db_path) as conn:
        room = db.create_web_chat_room(conn, "alice", name)
        db.add_web_room_member(conn, room.token, "bob", display_name="Bob")
    return room


def _private_room(db_path, owner="alice", name="mine"):
    with db.get_db(db_path) as conn:
        return db.create_web_chat_room(conn, owner, name)


async def _listed(client, cookies, token):
    resp = await client.get("/istota/api/chat/rooms", cookies=cookies)
    assert resp.status_code == 200
    return next(r for r in resp.json()["rooms"] if r["token"] == token)


# ---------------------------------------------------------------------------
# The listing: shared, host, and who may change the settings
# ---------------------------------------------------------------------------


class TestTheListingCarriesThePolicy:
    async def test_a_private_room_has_no_policy(self, client, db_path):
        room = _private_room(db_path)
        cookies = await _login(client, "alice")
        listed = await _listed(client, cookies, room.token)
        assert listed["shared"] is False
        assert listed["policy"] is None

    async def test_the_host_may_change_a_shared_rooms_settings(self, client, db_path):
        room = _shared_room(db_path)
        cookies = await _login(client, "alice")
        listed = await _listed(client, cookies, room.token)
        assert listed["shared"] is True
        assert listed["policy"]["host"] == "alice"
        assert listed["policy"]["is_host"] is True
        assert listed["policy"]["settings_refusal"] is None
        assert listed["policy"]["guest_reply"] == "direct"

    async def test_another_member_is_told_whose_settings_they_are(self, client, db_path):
        room = _shared_room(db_path)
        cookies = await _login(client, "bob")
        listed = await _listed(client, cookies, room.token)
        assert listed["policy"]["is_host"] is False
        assert "alice" in listed["policy"]["settings_refusal"]

    async def test_a_hostless_room_says_so(self, client, db_path):
        room = _shared_room(db_path)
        with db.get_db(db_path) as conn:
            room_policy.ensure_policy(conn, room.token)
            conn.execute("UPDATE room_policy SET host_user_id = NULL WHERE room_token = ?",
                         (room.token,))
        cookies = await _login(client, "bob")
        listed = await _listed(client, cookies, room.token)
        assert listed["policy"]["host"] is None
        assert "!room host" in listed["policy"]["settings_refusal"]


class TestTheListingSaysTheRoomIsOff:
    """Stage 20 left a switched-off room visible only as a 409 on send."""

    def _switch_off(self, db_path, token, person, *, participant=None, agreed=False):
        with db.get_db(db_path) as conn:
            room_policy.ensure_policy(conn, token)
            pid = None
            if participant is not None:
                pid = db.upsert_room_participant(
                    conn, room_token=token, surface="talk", surface_ref=participant,
                    kind="guest", display_name="Max Guest",
                )
            conn.execute(
                "INSERT INTO room_vetoes (room_token, person, participant_id, agreed_at) "
                "VALUES (?, ?, ?, CASE WHEN ? THEN datetime('now') END)",
                (token, person, pid, agreed),
            )
            conn.execute("UPDATE room_policy SET vetoed_at = datetime('now') "
                         "WHERE room_token = ?", (token,))

    async def test_a_room_that_is_on_says_nothing(self, client, db_path):
        room = _shared_room(db_path)
        cookies = await _login(client, "alice")
        assert (await _listed(client, cookies, room.token))["off"] is None

    async def test_an_off_room_names_who_switched_it_off(self, client, db_path):
        room = _shared_room(db_path)
        self._switch_off(db_path, room.token, "u:bob")
        self._switch_off(db_path, room.token, "talk:guests/max",
                         participant="guests/max", agreed=True)
        cookies = await _login(client, "alice")
        off = (await _listed(client, cookies, room.token))["off"]
        assert off["at"]
        assert off["by"] == [
            {"name": "Bob", "guest": False, "agreed": False},
            {"name": "Max Guest", "guest": True, "agreed": True},
        ]
        assert "!istota on" in off["way_back"]

    async def test_a_bot_removed_from_its_group_has_nobody_to_name(self, client, db_path):
        room = _shared_room(db_path)
        with db.get_db(db_path) as conn:
            room_policy.ensure_policy(conn, room.token)
            conn.execute("UPDATE room_policy SET vetoed_at = datetime('now') "
                         "WHERE room_token = ?", (room.token,))
        cookies = await _login(client, "bob")
        assert (await _listed(client, cookies, room.token))["off"]["by"] == []


class TestClaimingTheHost:
    async def test_a_member_claims_a_hostless_room(self, client, db_path):
        room = _shared_room(db_path)
        with db.get_db(db_path) as conn:
            room_policy.ensure_policy(conn, room.token)
            conn.execute("UPDATE room_policy SET host_user_id = NULL WHERE room_token = ?",
                         (room.token,))
        cookies = await _login(client, "bob")
        room_id = (await _listed(client, cookies, room.token))["id"]
        resp = await client.post(f"/istota/api/chat/rooms/{room_id}/host",
                                 cookies=cookies, headers=ORIGIN)
        assert resp.status_code == 200
        assert resp.json()["outcome"] == "claimed"
        assert resp.json()["policy"]["host"] == "bob"
        with db.get_db(db_path) as conn:
            assert room_policy.get_policy(conn, room.token).host_user_id == "bob"

    async def test_a_present_host_is_never_displaced(self, client, db_path):
        room = _shared_room(db_path)
        cookies = await _login(client, "bob")
        room_id = (await _listed(client, cookies, room.token))["id"]
        resp = await client.post(f"/istota/api/chat/rooms/{room_id}/host",
                                 cookies=cookies, headers=ORIGIN)
        assert resp.status_code == 409
        with db.get_db(db_path) as conn:
            assert room_policy.get_policy(conn, room.token).host_user_id == "alice"

    async def test_someone_elses_room_is_not_found(self, client, db_path):
        room = _shared_room(db_path)
        alice = await _login(client, "alice")
        room_id = (await _listed(client, alice, room.token))["id"]
        carol = await _login(client, "carol")
        resp = await client.post(f"/istota/api/chat/rooms/{room_id}/host",
                                 cookies=carol, headers=ORIGIN)
        assert resp.status_code == 404


class TestGuestReplyThroughThePatch:
    async def _patch(self, client, cookies, room_id, body):
        return await client.patch(f"/istota/api/chat/rooms/{room_id}", json=body,
                                  cookies=cookies, headers=ORIGIN)

    async def test_the_host_sets_it(self, client, db_path):
        room = _shared_room(db_path)
        cookies = await _login(client, "alice")
        room_id = (await _listed(client, cookies, room.token))["id"]
        resp = await self._patch(client, cookies, room_id, {"guest_reply": "held"})
        assert resp.status_code == 200
        assert resp.json()["policy"]["guest_reply"] == "held"
        with db.get_db(db_path) as conn:
            assert room_policy.get_policy(conn, room.token).guest_reply == "held"

    async def test_another_member_is_refused_and_nothing_changes(self, client, db_path):
        room = _shared_room(db_path)
        cookies = await _login(client, "bob")
        room_id = (await _listed(client, cookies, room.token))["id"]
        resp = await self._patch(client, cookies, room_id,
                                 {"guest_reply": "off", "color": "rose"})
        assert resp.status_code == 403
        with db.get_db(db_path) as conn:
            assert room_policy.get_policy(conn, room.token).guest_reply == "direct"
            handle = db.get_web_chat_room(conn, room_id)
            assert not handle.color

    async def test_an_unknown_value_is_refused(self, client, db_path):
        room = _shared_room(db_path)
        cookies = await _login(client, "alice")
        room_id = (await _listed(client, cookies, room.token))["id"]
        resp = await self._patch(client, cookies, room_id, {"guest_reply": "sometimes"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Grants: the caller's own, read and replaced
# ---------------------------------------------------------------------------


class TestTheGrantsEndpoints:
    async def test_nothing_is_granted_until_the_member_grants_it(self, client, db_path):
        room = _shared_room(db_path)
        cookies = await _login(client, "alice")
        room_id = (await _listed(client, cookies, room.token))["id"]
        resp = await client.get(f"/istota/api/chat/rooms/{room_id}/grants", cookies=cookies)
        assert resp.status_code == 200
        body = resp.json()
        names = [s["name"] for s in body["scopes"]]
        assert "files" in names and "memory" in names
        assert not any(s["granted"] for s in body["scopes"])
        assert body["state"] == "active"

    async def test_a_put_replaces_the_callers_grants_and_nobody_elses(self, client, db_path):
        room = _shared_room(db_path)
        with db.get_db(db_path) as conn:
            conn.execute(
                "INSERT INTO room_data_grants (room_token, user_id, scope) "
                "VALUES (?, 'bob', 'memory')", (room.token,),
            )
        cookies = await _login(client, "alice")
        room_id = (await _listed(client, cookies, room.token))["id"]
        url = f"/istota/api/chat/rooms/{room_id}/grants"
        first = await client.put(url, json={"scopes": ["files", "memory"]},
                                 cookies=cookies, headers=ORIGIN)
        assert first.status_code == 200
        second = await client.put(url, json={"scopes": ["files"]},
                                  cookies=cookies, headers=ORIGIN)
        assert second.status_code == 200
        granted = {s["name"] for s in second.json()["scopes"] if s["granted"]}
        assert granted == {"files"}
        with db.get_db(db_path) as conn:
            rows = conn.execute(
                "SELECT user_id, scope FROM room_data_grants WHERE room_token = ? "
                "ORDER BY user_id, scope", (room.token,),
            ).fetchall()
        assert [tuple(r) for r in rows] == [("alice", "files"), ("bob", "memory")]

    async def test_an_unknown_scope_changes_nothing(self, client, db_path):
        room = _shared_room(db_path)
        cookies = await _login(client, "alice")
        room_id = (await _listed(client, cookies, room.token))["id"]
        resp = await client.put(f"/istota/api/chat/rooms/{room_id}/grants",
                                json={"scopes": ["files", "everything"]},
                                cookies=cookies, headers=ORIGIN)
        assert resp.status_code == 400
        with db.get_db(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM room_data_grants").fetchone()[0] == 0

    async def test_the_body_takes_no_user(self, client, db_path):
        room = _shared_room(db_path)
        cookies = await _login(client, "alice")
        room_id = (await _listed(client, cookies, room.token))["id"]
        resp = await client.put(f"/istota/api/chat/rooms/{room_id}/grants",
                                json={"scopes": ["files"], "user_id": "bob"},
                                cookies=cookies, headers=ORIGIN)
        assert resp.status_code == 200
        with db.get_db(db_path) as conn:
            assert conn.execute(
                "SELECT user_id FROM room_data_grants"
            ).fetchall()[0][0] == "alice"

    async def test_a_private_room_says_the_grant_waits_for_someone_to_join(
        self, client, db_path,
    ):
        room = _private_room(db_path)
        cookies = await _login(client, "alice")
        room_id = (await _listed(client, cookies, room.token))["id"]
        resp = await client.get(f"/istota/api/chat/rooms/{room_id}/grants", cookies=cookies)
        assert resp.json()["state"] == "private"

    async def test_a_guest_makes_every_grant_inert(self, client, db_path):
        room = _shared_room(db_path)
        with db.get_db(db_path) as conn:
            db.upsert_room_participant(conn, room_token=room.token, surface="talk",
                                       surface_ref="guest/abc", kind="guest")
        cookies = await _login(client, "alice")
        room_id = (await _listed(client, cookies, room.token))["id"]
        resp = await client.get(f"/istota/api/chat/rooms/{room_id}/grants", cookies=cookies)
        assert resp.json()["state"] == "guests_present"

    async def test_a_side_room_has_nothing_to_share(self, client, db_path):
        room = _shared_room(db_path)
        with db.get_db(db_path) as conn:
            side = db.ensure_side_room(conn, room.token, "alice")
        cookies = await _login(client, "alice")
        room_id = (await _listed(client, cookies, side.token))["id"]
        get = await client.get(f"/istota/api/chat/rooms/{room_id}/grants", cookies=cookies)
        assert get.status_code == 409
        put = await client.put(f"/istota/api/chat/rooms/{room_id}/grants",
                               json={"scopes": ["files"]}, cookies=cookies, headers=ORIGIN)
        assert put.status_code == 409

    async def test_a_non_member_is_not_found(self, client, db_path):
        room = _shared_room(db_path)
        alice = await _login(client, "alice")
        room_id = (await _listed(client, alice, room.token))["id"]
        carol = await _login(client, "carol")
        resp = await client.get(f"/istota/api/chat/rooms/{room_id}/grants", cookies=carol)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# The members listing counts what an add discloses
# ---------------------------------------------------------------------------


class TestTheMembersListingCountsTheHistory:
    async def test_the_count_is_the_rooms_transcript(self, client, db_path):
        room = _private_room(db_path)
        with db.get_db(db_path) as conn:
            for i in range(3):
                db.add_message(conn, room.token, role="user", body=f"m{i}",
                               origin_surface="web", author_user_id="alice")
        cookies = await _login(client, "alice")
        room_id = (await _listed(client, cookies, room.token))["id"]
        resp = await client.get(f"/istota/api/chat/rooms/{room_id}/members", cookies=cookies)
        assert resp.json()["message_count"] == 3


# ---------------------------------------------------------------------------
# Delete affordance: the owner rule, on the wire
# ---------------------------------------------------------------------------


def _row(conn, token, author, body):
    return db.add_message(conn, token, role="user", body=body,
                          origin_surface="web", author_user_id=author)


class TestRowsSayWhoMayDeleteThem:
    async def test_another_members_row_in_a_shared_room_is_not_deletable(
        self, client, db_path,
    ):
        room = _shared_room(db_path)
        with db.get_db(db_path) as conn:
            _row(conn, room.token, "alice", "from alice")
            _row(conn, room.token, "bob", "from bob")
        cookies = await _login(client, "bob")
        room_id = (await _listed(client, cookies, room.token))["id"]
        resp = await client.get(f"/istota/api/chat/rooms/{room_id}/messages", cookies=cookies)
        by_text = {m["text"]: m for m in resp.json()["messages"]}
        assert by_text["from alice"]["deletable"] is False
        assert by_text["from bob"].get("deletable", True) is True

    async def test_a_private_room_marks_nothing(self, client, db_path):
        room = _private_room(db_path)
        with db.get_db(db_path) as conn:
            _row(conn, room.token, "alice", "mine")
        cookies = await _login(client, "alice")
        room_id = (await _listed(client, cookies, room.token))["id"]
        resp = await client.get(f"/istota/api/chat/rooms/{room_id}/messages", cookies=cookies)
        assert all("deletable" not in m for m in resp.json()["messages"])

    async def test_the_live_stream_carries_the_same_mark(self, client, db_path):
        import istota.web_app as mod
        room = _shared_room(db_path)
        with db.get_db(db_path) as conn:
            _row(conn, room.token, "alice", "streamed from alice")
        batch = mod._room_events_batch("bob", 0)
        row = next(e for e in batch["events"] if e["text"] == "streamed from alice")
        assert row["deletable"] is False

    async def test_the_mark_agrees_with_the_delete_endpoint(self, client, db_path):
        """The mark is the endpoint's own rule: a row marked undeletable is one
        the endpoint refuses, and an unmarked one it accepts."""
        room = _shared_room(db_path)
        with db.get_db(db_path) as conn:
            theirs = _row(conn, room.token, "alice", "theirs")
            mine = _row(conn, room.token, "bob", "mine")
        cookies = await _login(client, "bob")
        refused = await client.delete(f"/istota/api/chat/messages/{theirs}",
                                      cookies=cookies, headers=ORIGIN)
        accepted = await client.delete(f"/istota/api/chat/messages/{mine}",
                                       cookies=cookies, headers=ORIGIN)
        assert refused.status_code == 403
        assert accepted.status_code == 200


# ---------------------------------------------------------------------------
# Settings: a shared room is refused as a destination, and marked
# ---------------------------------------------------------------------------


def _talk_room_with_guest(db_path, ref="talkref1"):
    """A Talk room with one istota member and one Talk guest: shared, though
    `room_members` alone counts one."""
    with db.get_db(db_path) as conn:
        db.register_room(conn, ref, "alice", origin="talk", name="family")
        db.add_room_binding(conn, ref, "talk", ref)
        db.upsert_room_participant(conn, room_token=ref, surface="talk",
                                   surface_ref="alice", kind="principal", user_id="alice")
        db.upsert_room_participant(conn, room_token=ref, surface="talk",
                                   surface_ref="guest/max", kind="guest")
        assert db.list_room_members(conn, ref) == ["alice"]
    return ref


class TestSettingsRefuseASharedRoom:
    @pytest.fixture(autouse=True)
    def _config(self, db_path, tmp_path):
        import istota.web_app as mod
        mod._config = _config(db_path, tmp_path)

    def test_a_web_route_to_a_shared_room_is_refused(self, db_path):
        import istota.web_app as mod
        room = _shared_room(db_path)
        with pytest.raises(ValueError, match="shared"):
            mod._validate_descriptor_rooms(f"web:{room.token}", "alice")

    def test_a_web_route_to_a_private_room_is_accepted(self, db_path):
        import istota.web_app as mod
        room = _private_room(db_path)
        mod._validate_descriptor_rooms(f"web:{room.token}", "alice")

    def test_a_talk_route_to_a_room_with_a_guest_is_refused(self, db_path):
        import istota.web_app as mod
        ref = _talk_room_with_guest(db_path)
        with pytest.raises(ValueError, match="shared"):
            mod._validate_descriptor_rooms(f"talk:{ref}", "alice")

    def test_a_briefing_token_into_a_shared_room_is_refused(self, db_path):
        import istota.web_app as mod
        ref = _talk_room_with_guest(db_path)
        with pytest.raises(ValueError, match="shared"):
            mod._validate_talk_route_token(ref, "alice")

    def test_an_alerts_channel_into_a_shared_room_is_refused(self, db_path):
        import istota.web_app as mod
        ref = _talk_room_with_guest(db_path)
        with pytest.raises(ValueError, match="shared"):
            mod._validate_talk_channel(ref, "alice")

    def test_a_default_room_pin_on_a_shared_room_is_refused(self, db_path):
        import istota.web_app as mod
        room = _shared_room(db_path)
        with pytest.raises(ValueError, match="shared"):
            mod._validate_default_room(room.token, "alice")

    async def test_a_briefing_on_a_room_that_became_shared_can_still_be_edited(
        self, client, db_path,
    ):
        """The pin was legitimate when saved; the room gaining a guest must
        not stop the user disabling the briefing it sits on."""
        from istota import user_briefings
        ref = "talkref2"
        with db.get_db(db_path) as conn:
            db.register_room(conn, ref, "alice", origin="talk", name="family")
            db.add_room_binding(conn, ref, "talk", ref)
        user_briefings.ensure_briefing(
            db_path, user_id="alice", name="morning", cron="0 7 * * *", title="",
            conversation_token=ref, output="talk", enabled=True,
        )
        with db.get_db(db_path) as conn:
            db.upsert_room_participant(conn, room_token=ref, surface="talk",
                                       surface_ref="alice", kind="principal", user_id="alice")
            db.upsert_room_participant(conn, room_token=ref, surface="talk",
                                       surface_ref="guest/max", kind="guest")
        cookies = await _login(client, "alice")
        body = {"name": "morning", "cron": "0 7 * * *", "output": "talk",
                "conversation_token": ref}
        kept = await client.post("/istota/api/settings/briefings",
                                 json={**body, "enabled": False},
                                 cookies=cookies, headers=ORIGIN)
        assert kept.status_code == 200
        other = "talkref3"
        with db.get_db(db_path) as conn:
            db.register_room(conn, other, "alice", origin="talk", name="team")
            db.add_room_binding(conn, other, "talk", other)
            db.upsert_room_participant(conn, room_token=other, surface="talk",
                                       surface_ref="alice", kind="principal", user_id="alice")
            db.upsert_room_participant(conn, room_token=other, surface="talk",
                                       surface_ref="guest/max", kind="guest")
        moved = await client.post("/istota/api/settings/briefings",
                                  json={**body, "conversation_token": other},
                                  cookies=cookies, headers=ORIGIN)
        assert moved.status_code == 400

    def test_the_web_picker_counts_a_talk_guest_as_sharing(self, db_path):
        import istota.web_app as mod
        ref = _talk_room_with_guest(db_path)
        with db.get_db(db_path) as conn:
            db.ensure_web_chat_handle(conn, "alice", ref, "family")
        listed = {r["token"]: r for r in mod._user_web_rooms("alice")}
        assert listed[ref]["shared"] is True

    def test_the_talk_picker_marks_a_shared_conversation(self, db_path):
        import istota.web_app as mod
        ref = _talk_room_with_guest(db_path)
        with db.get_db(db_path) as conn:
            db.add_room_member(conn, ref, "alice")
        listed = {r["token"]: r for r in mod._user_talk_rooms("alice")}
        assert listed[ref]["shared"] is True


# ---------------------------------------------------------------------------
# The room's group link (multiplayer Stage 27)
# ---------------------------------------------------------------------------


def _groups(db_path):
    with db.get_db(db_path) as conn:
        db.create_group(conn, "fam", kind="family", display_name="Fam",
                        created_by="operator")
        db.add_group_member(conn, "fam", "alice", added_by="operator")
        db.add_group_member(conn, "fam", "bob", added_by="operator")
        db.create_group(conn, "bobs", kind="team", display_name="Bobs",
                        created_by="operator")
        db.add_group_member(conn, "bobs", "bob", added_by="operator")


class TestTheGroupLinkEndpoints:
    async def _url(self, client, cookies, room):
        room_id = (await _listed(client, cookies, room.token))["id"]
        return f"/istota/api/chat/rooms/{room_id}/group"

    async def test_the_host_reads_their_own_groups_and_links_one(self, client, db_path):
        _groups(db_path)
        room = _shared_room(db_path)
        cookies = await _login(client, "alice")
        url = await self._url(client, cookies, room)
        body = (await client.get(url, cookies=cookies)).json()
        assert body["group_id"] is None and body["can_set"] is True
        assert [g["group_id"] for g in body["choices"]] == ["fam"]
        resp = await client.put(url, json={"group_id": "fam"},
                                cookies=cookies, headers=ORIGIN)
        assert resp.status_code == 200
        assert resp.json()["group_id"] == "fam"
        assert resp.json()["group_name"] == "Fam"
        with db.get_db(db_path) as conn:
            assert db.get_room(conn, room.token).group_id == "fam"
        resp = await client.put(url, json={"group_id": None},
                                cookies=cookies, headers=ORIGIN)
        assert resp.status_code == 200 and resp.json()["group_id"] is None

    async def test_another_member_reads_it_and_is_refused(self, client, db_path):
        _groups(db_path)
        room = _shared_room(db_path)
        with db.get_db(db_path) as conn:
            db.set_room_group(conn, room.token, "fam")
        cookies = await _login(client, "bob")
        url = await self._url(client, cookies, room)
        body = (await client.get(url, cookies=cookies)).json()
        assert body["group_id"] == "fam" and body["can_set"] is False
        assert body["choices"] == []
        resp = await client.put(url, json={"group_id": "bobs"},
                                cookies=cookies, headers=ORIGIN)
        assert resp.status_code == 403
        with db.get_db(db_path) as conn:
            assert db.get_room(conn, room.token).group_id == "fam"

    async def test_a_group_the_host_is_not_in_is_refused(self, client, db_path):
        _groups(db_path)
        room = _shared_room(db_path)
        cookies = await _login(client, "alice")
        url = await self._url(client, cookies, room)
        resp = await client.put(url, json={"group_id": "bobs"},
                                cookies=cookies, headers=ORIGIN)
        assert resp.status_code == 403
        assert resp.json()["error"] == "You are not a member of group 'bobs'."
        with db.get_db(db_path) as conn:
            assert db.get_room(conn, room.token).group_id is None

    async def test_a_malformed_body_is_refused(self, client, db_path):
        room = _private_room(db_path)
        cookies = await _login(client, "alice")
        url = await self._url(client, cookies, room)
        resp = await client.put(url, json={"group_id": 3},
                                cookies=cookies, headers=ORIGIN)
        assert resp.status_code == 400

    async def test_someone_elses_room_is_not_found(self, client, db_path):
        room = _private_room(db_path)
        cookies = await _login(client, "alice")
        url = await self._url(client, cookies, room)
        other = await _login(client, "carol")
        assert (await client.get(url, cookies=other)).status_code == 404
