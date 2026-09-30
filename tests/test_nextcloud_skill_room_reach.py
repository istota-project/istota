"""The nextcloud skill honours a shared room's reach (multiplayer Stage 16).

Two routes the executor's seams did not reach, because the skill acts with the
bot's own credentials over Nextcloud's APIs rather than through the sandbox:

- `talk send` (and every other write into a conversation) let a private,
  full-reach task post into a Talk room other people read, around the held
  `room post` path and the delivery gate.
- the WebDAV verbs read the caller's whole workspace, so a `nextcloud` grant
  reached the files and the memory the room withholds.
"""
import asyncio
import json
from unittest.mock import patch

import pytest

from istota import db
from istota.skills.nextcloud import main

BOT = "istota"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


@pytest.fixture(autouse=True)
def _nc_env(monkeypatch, db_path):
    monkeypatch.setenv("NC_URL", "https://cloud.example.com")
    monkeypatch.setenv("NC_USER", BOT)
    monkeypatch.setenv("NC_PASS", "secret")
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.setenv("ISTOTA_BOT_DIR_NAME", "istota")
    monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
    monkeypatch.setenv("ISTOTA_TASK_ID", "9")
    monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "dm-alice")
    monkeypatch.delenv("ISTOTA_WITHHELD_SCOPES", raising=False)


def _people(*actors):
    return [{"actorType": kind, "actorId": actor} for kind, actor in actors]


ROSTERS = {
    "grp": _people(("users", "alice"), ("users", "bob"), ("users", BOT)),
    "dm-alice": _people(("users", "alice"), ("users", BOT)),
    "dm-bob": _people(("users", "bob"), ("users", BOT)),
    "with-guest": _people(("users", "alice"), ("guests", "abc"), ("users", BOT)),
    "tk-promoted": _people(("users", "alice"), ("users", "bob"), ("users", BOT)),
}


class FakeTalk:
    def __init__(self, fail=False):
        self.fail = fail
        self.writes = []

    async def get_participants(self, token):
        if self.fail:
            raise OSError("unreachable")
        return ROSTERS[token]

    async def send_message(self, token, message, reply_to=None):
        self.writes.append(("send", token))
        return {"id": 5}

    async def rename_conversation(self, token, name):
        self.writes.append(("rename", token))


def _run(capsys, argv, talk=None):
    talk = talk or FakeTalk()
    code = 0
    with patch("istota.skills.nextcloud._talk_run",
               side_effect=lambda factory: asyncio.run(factory(talk))):
        try:
            main(argv)
        except SystemExit as e:
            code = e.code
    out = capsys.readouterr().out
    return (json.loads(out) if out.strip() else None), code, talk


class TestWritesIntoAConversation:
    def test_a_post_into_a_room_others_read_is_refused(self, capsys):
        out, _code, talk = _run(capsys, ["talk", "send", "grp", "the calendar says"])
        assert out["reason"] == "shared_room"
        assert "room post" in out["error"]
        assert talk.writes == []

    def test_a_post_into_another_users_one_to_one_is_refused(self, capsys):
        out, _code, talk = _run(capsys, ["talk", "send", "dm-bob", "hi"])
        assert out["reason"] == "shared_room" and talk.writes == []

    def test_a_guest_counts_as_someone_else(self, capsys):
        out, _code, talk = _run(capsys, ["talk", "send", "with-guest", "hi"])
        assert out["reason"] == "shared_room" and talk.writes == []

    def test_the_callers_own_conversation_is_open(self, capsys, monkeypatch):
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "elsewhere")
        out, _code, talk = _run(capsys, ["talk", "send", "dm-alice", "hi"])
        assert out["status"] == "ok" and talk.writes == [("send", "dm-alice")]

    def test_the_room_the_task_was_asked_in_is_open(self, capsys, monkeypatch):
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "grp")
        out, _code, talk = _run(capsys, ["talk", "send", "grp", "hi"])
        assert out["status"] == "ok" and talk.writes == [("send", "grp")]

    def test_including_by_another_surfaces_ref(self, capsys, monkeypatch, db_path):
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "plans")
            db.add_room_binding(conn, room.token, "talk", "tk-promoted")
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", room.token)
        out, _code, talk = _run(capsys, ["talk", "send", "tk-promoted", "hi"])
        assert out["status"] == "ok"

    def test_changing_a_shared_room_is_never_exempt(self, capsys, monkeypatch):
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "grp")
        out, _code, talk = _run(capsys, ["talk", "rename", "grp", "--name", "Mine now"])
        assert out["reason"] == "shared_room" and talk.writes == []

    def test_an_unreadable_roster_refuses(self, capsys):
        out, _code, talk = _run(capsys, ["talk", "send", "dm-bob", "hi"],
                                talk=FakeTalk(fail=True))
        assert out["reason"] == "audience_unavailable" and talk.writes == []

    def test_outside_a_task_nothing_is_checked(self, capsys, monkeypatch):
        monkeypatch.delenv("ISTOTA_TASK_ID")
        out, _code, talk = _run(capsys, ["talk", "send", "grp", "hi"])
        assert talk.writes == [("send", "grp")]


class TestTheWebdavVerbs:
    def test_memory_withheld_refuses_the_memory_directories(self, capsys, monkeypatch):
        monkeypatch.setenv("ISTOTA_WITHHELD_SCOPES", "memory")
        with patch("istota.skills.nextcloud.dav_mod.stat", return_value={"ok": 1}) as stat:
            for path in ("/Users/alice/istota/config/USER.md",
                         "/Users/alice/memories/2026-09-30.md",
                         "memories/../memories/x.md",
                         "/Users/alice/istota/playbooks"):
                out, code, _ = _run(capsys, ["files", "stat", path])
                assert code == 1 and "memory" in out["error"], path
            stat.assert_not_called()
            out, _code, _ = _run(capsys, ["files", "stat", "/Users/alice/notes.md"])
            assert out == {"ok": 1}

    def test_a_share_of_a_directory_holding_memory_is_refused(self, capsys, monkeypatch):
        monkeypatch.setenv("ISTOTA_WITHHELD_SCOPES", "memory")
        with patch("istota.skills.nextcloud.ocs_create_public_link") as link:
            out, code, _ = _run(capsys, ["share", "link", "/Users/alice"])
        assert code == 1 and "memory" in out["error"]
        link.assert_not_called()

    def test_files_withheld_closes_the_file_and_share_verbs(self, capsys, monkeypatch):
        monkeypatch.setenv("ISTOTA_WITHHELD_SCOPES", "files,memory")
        with patch("istota.skills.nextcloud.dav_mod") as dav:
            for argv in (["files", "stat", "/Users/alice/notes.md"],
                         ["files", "trash", "list"], ["share", "list"]):
                out, code, _ = _run(capsys, argv)
                assert code == 1 and out["reason"] == "files_withheld", argv
        assert dav.mock_calls == []

    def test_a_private_room_reaches_everything(self, capsys):
        with patch("istota.skills.nextcloud.dav_mod.stat", return_value={"ok": 1}):
            out, _code, _ = _run(capsys, ["files", "stat", "/Users/alice/istota/config/USER.md"])
        assert out == {"ok": 1}
