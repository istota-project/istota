"""`POST /istota/api/chat/voice`: a recording from the shell's quick action.

One request uploads the audio and sends it into the user's default room as an
attachment-only message, so the shell holds nothing between two calls. It is
the one chat route a device token reaches, with the `voice` scope. These tests
drive the real app: the body guard, the bearer dependency, the default-room
resolution, the replay check and the task creation are the production ones.
"""

import json
import re

import pytest

from tests.test_device_tokens import DEVICE, ORIGIN, _bearer, _login, _mint_direct
from tests.test_web_app import _make_config, _needs_web_deps, _patch_app

pytestmark = _needs_web_deps

try:
    from httpx import ASGITransport, AsyncClient
except ImportError:  # pragma: no cover - covered by _needs_web_deps
    pass

VOICE = "/istota/api/chat/voice"
KEY = "voice-take-0001"
AUDIO = b"\x00\x00\x00\x18ftypM4A \x00\x00\x02\x00" + b"\x00" * 64


@pytest.fixture
def config(tmp_path):
    from istota import user_profiles
    from istota.config import UserConfig

    cfg = _make_config(tmp_path, users={
        "alice": UserConfig(display_name="Alice"),
        "bob": UserConfig(display_name="Bob"),
    })
    for user_id in ("alice", "bob"):
        cfg.workspace_root(user_id).mkdir(parents=True)
        user_profiles.ensure_profile(cfg.db_path, user_id)
    return cfg


@pytest.fixture
def app(config):
    return _patch_app(config)


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://example.com") as c:
        yield c


def _voice_token(config, user_id="alice", scopes=("voice",)):
    return _mint_direct(config, user_id=user_id, device_id=DEVICE, scopes=scopes)[1]


async def _post(client, headers, *, key=KEY, filename="recording.m4a", data=AUDIO, cookies=None):
    fields = {} if key is None else {"client_msg_id": key}
    return await client.post(
        VOICE, headers=headers, cookies=cookies, data=fields,
        files={"file": (filename, data, "audio/mp4")},
    )


def _stored(config, user_id="alice"):
    return sorted((config.workspace_path / "Users" / user_id).rglob("voice-*"))


def _user_turns(config, user_id="alice"):
    from istota import db

    with db.get_db(config.db_path) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT id, room_token, body, attachments, client_msg_id, task_id FROM messages "
            "WHERE role = 'user' AND author_user_id = ? ORDER BY id", (user_id,),
        )]


def _tasks(config):
    from istota import db

    with db.get_db(config.db_path) as conn:
        return [dict(row) for row in conn.execute("SELECT id, conversation_token, attachments FROM tasks")]


def _web_room(config, name, user_id="alice"):
    from istota import db

    with db.get_db(config.db_path) as conn:
        return db.create_web_chat_room(conn, user_id, name)


# --- the bearer path -----------------------------------------------------------


async def test_a_voice_bearer_sends_into_the_default_room_with_no_cookie_and_no_origin(client, config):
    token = _voice_token(config)
    response = await _post(client, _bearer(token))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["replay"] is False
    assert body["room"]["name"] == "general"
    [turn] = _user_turns(config)
    assert turn["room_token"] == body["room"]["token"]
    assert turn["client_msg_id"] == KEY
    assert turn["id"] == body["message_id"]
    assert turn["body"] == "Voice message (see attached audio)."
    [stored] = _stored(config)
    assert stored.read_bytes() == AUDIO
    assert stored.name.startswith("voice-") and stored.suffix == ".m4a"
    assert re.fullmatch(r'\["voice-\d{8}-\d{6}\.m4a"\]', turn["attachments"])
    [task] = _tasks(config)
    assert task["id"] == body["task_id"] == turn["task_id"]
    assert task["conversation_token"] == body["room"]["token"]
    assert json.loads(task["attachments"]) == [str(stored)]


async def test_the_configured_default_room_is_honoured(client, config):
    from istota import user_profiles

    _web_room(config, "general")
    pinned = _web_room(config, "inbox")
    user_profiles.update_profile(config.db_path, "alice", default_room=pinned.token)
    response = await _post(client, _bearer(_voice_token(config)))
    assert response.status_code == 201, response.text
    assert response.json()["room"] == {"token": pinned.token, "name": "inbox"}


async def test_with_no_pin_the_heuristic_room_takes_it(client, config):
    first = _web_room(config, "errands")
    _web_room(config, "later")
    response = await _post(client, _bearer(_voice_token(config)))
    assert response.status_code == 201, response.text
    assert response.json()["room"] == {"token": first.token, "name": "errands"}


async def test_the_same_key_twice_is_one_message_one_task_one_file(client, config):
    token = _voice_token(config)
    first = await _post(client, _bearer(token))
    assert first.status_code == 201, first.text
    again = await _post(client, _bearer(token))
    assert again.status_code == 200, again.text
    assert again.json() == {**first.json(), "replay": True}
    assert len(_user_turns(config)) == 1
    assert len(_tasks(config)) == 1
    assert len(_stored(config)) == 1


async def test_a_retry_after_the_default_room_moved_replays_the_first_send(client, config):
    from istota import user_profiles

    token = _voice_token(config)
    first = await _post(client, _bearer(token))
    assert first.status_code == 201, first.text
    moved = _web_room(config, "inbox")
    user_profiles.update_profile(config.db_path, "alice", default_room=moved.token)
    again = await _post(client, _bearer(token))
    assert again.status_code == 200, again.text
    assert again.json() == {**first.json(), "replay": True}
    assert again.json()["room"]["token"] != moved.token
    assert len(_user_turns(config)) == 1
    assert len(_tasks(config)) == 1
    assert len(_stored(config)) == 1


async def test_another_users_key_in_another_room_is_not_a_replay(client, config):
    bob = await _post(client, _bearer(_voice_token(config, user_id="bob")))
    assert bob.status_code == 201, bob.text
    alice = await _post(client, _bearer(_voice_token(config)))
    assert alice.status_code == 201, alice.text
    assert alice.json()["room"]["token"] != bob.json()["room"]["token"]
    assert len(_user_turns(config)) == 1


async def test_the_room_name_is_the_registry_name_after_a_rename(client, config):
    from istota import db, user_profiles

    room = _web_room(config, "general")
    user_profiles.update_profile(config.db_path, "alice", default_room=room.token)
    with db.get_db(config.db_path) as conn:
        db.rename_room(conn, room.token, "Errands")
        expected = db.default_web_chat_room_name(conn, "alice")
    assert expected == "Errands"
    token = _voice_token(config)
    first = await _post(client, _bearer(token))
    assert first.status_code == 201, first.text
    assert first.json()["room"]["name"] == expected
    again = await _post(client, _bearer(token))
    assert again.json()["room"]["name"] == expected


async def test_a_rate_limit_met_inside_the_write_removes_the_stored_audio(client, config, monkeypatch):
    import istota.webui.app as mod

    monkeypatch.setattr(mod, "_voice_rate_limited", lambda username: False)
    monkeypatch.setattr(config.web.chat, "rate_limit_messages", 0)
    response = await _post(client, _bearer(_voice_token(config)))
    assert response.status_code == 429, response.text
    assert response.json()["code"] == "rate_limited"
    assert _stored(config) == []
    assert _user_turns(config) == []


async def test_a_room_switched_off_inside_the_write_removes_the_stored_audio(client, config, monkeypatch):
    import istota.webui.app as mod
    from istota import db, user_profiles

    async def no_refusal(token, **kwargs):
        return None

    monkeypatch.setattr(mod, "_room_write_refusal", no_refusal)
    room = _web_room(config, "general")
    user_profiles.update_profile(config.db_path, "alice", default_room=room.token)
    with db.get_db(config.db_path) as conn:
        conn.execute(
            "INSERT INTO room_policy (room_token, vetoed_at) VALUES (?, datetime('now'))", (room.token,),
        )
    response = await _post(client, _bearer(_voice_token(config)))
    assert response.status_code == 409, response.text
    assert _stored(config) == []


async def test_an_overlapping_same_key_send_replays_and_removes_its_copy(client, config, monkeypatch):
    import istota.webui.app as mod

    token = _voice_token(config)
    first = await _post(client, _bearer(token))
    assert first.status_code == 201, first.text
    real = mod._voice_own_send

    # The first request's turn lands after this one's replay check: the check
    # misses it, and only the write transaction sees the key.
    def unseen_before_the_write(username, client_msg_id, token=None):
        return None if token is None else real(username, client_msg_id, token)

    monkeypatch.setattr(mod, "_voice_own_send", unseen_before_the_write)
    again = await _post(client, _bearer(token))
    assert again.status_code == 200, again.text
    assert again.json() == {**first.json(), "replay": True}
    assert len(_user_turns(config)) == 1
    assert len(_tasks(config)) == 1
    assert len(_stored(config)) == 1


@pytest.mark.parametrize("key", [None, "", "short", "x" * 65, "has space-1234", "under_score-12"])
async def test_a_missing_or_malformed_key_is_a_400(client, config, key):
    response = await _post(client, _bearer(_voice_token(config)), key=key)
    assert response.status_code == 400
    assert _stored(config) == []
    assert _user_turns(config) == []


async def test_a_missing_file_is_a_400(client, config):
    response = await client.post(VOICE, headers=_bearer(_voice_token(config)), data={"client_msg_id": KEY})
    assert response.status_code == 400
    assert _user_turns(config) == []


async def test_a_notes_only_token_is_a_403(client, config):
    from istota import user_profiles

    user_profiles.update_profile(config.db_path, "alice", quick_notes_folder="Notes/Inbox")
    response = await _post(client, _bearer(_voice_token(config, scopes=("notes",))))
    assert response.status_code == 403
    assert _stored(config) == []
    assert _user_turns(config) == []


async def test_a_revoked_token_is_a_401(client, config):
    from istota import db

    token = _voice_token(config)
    with db.get_db(config.db_path) as conn:
        conn.execute("DELETE FROM device_tokens")
    response = await _post(client, _bearer(token))
    assert response.status_code == 401
    assert _stored(config) == []


async def test_a_bad_bearer_is_a_401_even_beside_a_valid_session(client, config):
    cookies = await _login(client)
    response = await _post(client, {**ORIGIN, **_bearer("not-a-token")}, cookies=cookies)
    assert response.status_code == 401
    assert _stored(config) == []
    assert _user_turns(config) == []


async def test_the_voice_scope_mints_from_the_devices_route(client, config):
    cookies = await _login(client)
    response = await client.post("/istota/api/devices", cookies=cookies, headers=ORIGIN, json={
        "device_id": DEVICE, "label": "iPhone", "scopes": ["voice"],
    })
    assert response.status_code == 201, response.text
    assert response.json()["scopes"] == ["voice"]
    sent = await _post(client, _bearer(response.json()["token"]))
    assert sent.status_code == 201, sent.text


# --- refusals ------------------------------------------------------------------


async def test_a_read_only_default_room_is_a_409_and_nothing_is_stored(client, config):
    from istota import db, user_profiles
    from istota.transport.ingest import record_phone_turn
    from istota.transport.sms import sms_conversation_token

    with db.get_db(config.db_path) as conn:
        sms = record_phone_turn(
            conn, config, surface="sms", surface_ref=sms_conversation_token("alice"),
            user_id="alice", text="first text", channel_name="SMS",
        ).room_token
    user_profiles.update_profile(config.db_path, "alice", default_room=sms)
    turns_before = len(_user_turns(config))
    tasks_before = len(_tasks(config))
    response = await _post(client, _bearer(_voice_token(config)))
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "room_read_only"
    assert "SMS" in body["error"]
    assert _stored(config) == []
    assert len(_user_turns(config)) == turns_before
    assert len(_tasks(config)) == tasks_before


async def test_a_switched_off_default_room_is_a_409(client, config):
    from istota import db, user_profiles

    room = _web_room(config, "general")
    user_profiles.update_profile(config.db_path, "alice", default_room=room.token)
    with db.get_db(config.db_path) as conn:
        conn.execute(
            "INSERT INTO room_policy (room_token, vetoed_at) VALUES (?, datetime('now'))", (room.token,),
        )
    response = await _post(client, _bearer(_voice_token(config)))
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "room_read_only"
    assert _stored(config) == []
    assert _user_turns(config) == []


@pytest.mark.parametrize("filename", ["recording.exe", "notes.txt", "recording"])
async def test_a_file_that_is_not_allowed_audio_is_a_400(client, config, filename):
    response = await _post(client, _bearer(_voice_token(config)), filename=filename)
    assert response.status_code == 400
    assert _stored(config) == []
    assert _user_turns(config) == []


async def test_an_audio_type_off_the_allowlist_is_a_400(client, config, monkeypatch):
    monkeypatch.setattr(config.web.chat, "attachment_extensions", ["webm"])
    response = await _post(client, _bearer(_voice_token(config)))
    assert response.status_code == 400
    assert _stored(config) == []


async def test_a_file_over_the_attachment_limit_is_a_413(client, config, monkeypatch):
    monkeypatch.setattr(config.web.chat, "max_attachment_mb", 0)
    response = await _post(client, _bearer(_voice_token(config)))
    assert response.status_code == 413
    assert _stored(config) == []
    assert _user_turns(config) == []


async def test_the_rate_limit_is_a_429_with_retry_after(client, config, monkeypatch):
    monkeypatch.setattr(config.web.chat, "rate_limit_messages", 0)
    response = await _post(client, _bearer(_voice_token(config)))
    assert response.status_code == 429
    assert response.headers["retry-after"] == str(config.web.chat.rate_limit_window_seconds)
    assert _stored(config) == []
    assert _user_turns(config) == []


# --- the session path ------------------------------------------------------------


async def test_the_session_path_works_with_a_cookie_and_an_origin(client, config):
    cookies = await _login(client)
    response = await _post(client, ORIGIN, cookies=cookies)
    assert response.status_code == 201, response.text
    assert len(_user_turns(config)) == 1


async def test_the_session_path_is_refused_without_an_origin(client, config):
    cookies = await _login(client)
    response = await _post(client, {}, cookies=cookies)
    assert response.status_code == 403
    assert _stored(config) == []
    assert _user_turns(config) == []


async def test_a_voice_token_reaches_no_other_chat_route(client, config):
    token = _voice_token(config)
    upload = await client.post(
        "/istota/api/chat/attachments", headers=_bearer(token),
        files={"file": ("recording.m4a", AUDIO, "audio/mp4")},
    )
    assert upload.status_code == 401
    assert (await client.get("/istota/api/chat/rooms", headers=_bearer(token))).status_code == 401
