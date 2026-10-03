"""My notes: a member's private notes about a shared room (ISSUE-608).

A file in the member's own workspace, `{bot_dir}/config/rooms/<token>.md`,
read into their own turns in that room and never by the room. These pin the
storage helpers (alias fallback, symlink refusal, the cap), the room name
resolver every caller shares, `memory --room`, `!room notes`, the web pane's
endpoints, the room listing's two flags, and the cleanup when a room is deleted.
"""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from istota import commands, db, storage
from istota.config import Config, SiteConfig, UserConfig, WebConfig
from istota.rooms.lookup import Ambiguous, Found, NotFound, resolve_room
from istota.skills.memory import main as memory_main

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

ORIGIN = {"origin": "https://example.com"}


def _config(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    return Config(
        db_path=db_path,
        workspace_path=tmp_path / "mount",
        temp_dir=tmp_path / "temp",
        site=SiteConfig(hostname="example.com"),
        users={"alice": UserConfig(display_name="Alice"),
               "bob": UserConfig(display_name="Bob")},
        web=WebConfig(
            enabled=True, port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web", oauth2_client_secret="s",
            session_secret_key="test-session-key",
        ),
        bot_name="Istota",
    )


@pytest.fixture
def config(tmp_path):
    return _config(tmp_path)


def _config_dir(config, user):
    path = config.workspace_path / "Users" / user / "istota" / "config"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _notes_file(config, user, token):
    return _config_dir(config, user) / "rooms" / f"{token}.md"


def _shared_room(conn, name="Family", creator="alice", other="bob"):
    room = db.create_web_chat_room(conn, creator, name)
    db.add_web_room_member(conn, room.token, other)
    return room.token


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class TestStorage:
    def test_write_then_read(self, config):
        _config_dir(config, "alice")
        assert storage.write_room_notes(config, "alice", "rm_fam", "Skip the house sale.\n")
        assert _notes_file(config, "alice", "rm_fam").read_text() == "Skip the house sale.\n"
        assert storage.read_room_notes(config, "alice", "rm_fam") == "Skip the house sale.\n"
        # Another user has none.
        assert storage.read_room_notes(config, "bob", "rm_fam") is None

    def test_blank_reads_as_none(self, config):
        _config_dir(config, "alice")
        storage.write_room_notes(config, "alice", "rm_fam", "  \n")
        assert storage.read_room_notes(config, "alice", "rm_fam") is None

    def test_an_alias_is_read_while_the_canonical_file_is_absent(self, config):
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "rm_new", "alice", origin="web")
            conn.execute("INSERT INTO room_token_migration VALUES (?, ?, ?)",
                         ("old_tok", "rm_new", "2026-01-01T00:00:00Z"))
        path = _notes_file(config, "alice", "old_tok")
        path.parent.mkdir(parents=True)
        path.write_text("from before the rename")
        assert storage.read_room_notes(config, "alice", "rm_new") == "from before the rename"
        # A canonical file wins, even an empty one.
        _notes_file(config, "alice", "rm_new").write_text("")
        assert storage.read_room_notes(config, "alice", "rm_new") is None

    def test_a_symlinked_rooms_dir_leading_outside_is_refused(self, config, tmp_path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "rm_fam.md").write_text("not yours")
        os.symlink(outside, _config_dir(config, "alice") / "rooms")
        assert storage.room_notes_path(config, "alice", "rm_fam") is None
        assert storage.read_room_notes(config, "alice", "rm_fam") is None
        assert not storage.write_room_notes(config, "alice", "rm_fam", "x")
        assert (outside / "rm_fam.md").read_text() == "not yours"

    def test_a_symlinked_leaf_is_refused(self, config, tmp_path):
        target = tmp_path / "secret.txt"
        target.write_text("daemon secret")
        path = _notes_file(config, "alice", "rm_fam")
        path.parent.mkdir(parents=True)
        os.symlink(target, path)
        assert storage.read_room_notes(config, "alice", "rm_fam") is None
        assert not storage.write_room_notes(config, "alice", "rm_fam", "x")
        assert target.read_text() == "daemon secret"

    def test_a_file_past_the_cap_is_not_read(self, config):
        path = _notes_file(config, "alice", "rm_fam")
        path.parent.mkdir(parents=True)
        path.write_text("x" * (storage.NOTES_MAX_BYTES + 1))
        assert storage.read_room_notes(config, "alice", "rm_fam") is None

    def test_an_unsafe_token_is_refused(self, config):
        assert storage.room_notes_path(config, "alice", "../USER") is None

    def test_listing_names_non_empty_regular_files(self, config):
        _config_dir(config, "alice")
        storage.write_room_notes(config, "alice", "rm_a", "a")
        storage.write_room_notes(config, "alice", "rm_b", "")
        (_config_dir(config, "alice") / "rooms" / "notes.txt").write_text("x")
        assert storage.room_notes_tokens(config, "alice") == {"rm_a"}

    def test_delete_removes_each_members_notes_and_nothing_else(self, config):
        for user in ("alice", "bob"):
            _config_dir(config, user)
            storage.write_room_notes(config, user, "rm_gone", "about it")
            storage.write_room_notes(config, user, "rm_kept", "another room")
        storage.delete_room_notes_for(config, ["alice", "bob", "carol"], "rm_gone")
        for user in ("alice", "bob"):
            assert not _notes_file(config, user, "rm_gone").exists()
            assert _notes_file(config, user, "rm_kept").exists()

    def test_delete_does_not_follow_a_symlinked_rooms_dir(self, config, tmp_path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "rm_gone.md").write_text("keep me")
        os.symlink(outside, _config_dir(config, "alice") / "rooms")
        storage.delete_room_notes_for(config, ["alice"], "rm_gone")
        assert (outside / "rm_gone.md").read_text() == "keep me"

    def test_the_cli_cap_matches_the_daemons(self):
        from istota.skills import memory

        assert memory._MAX_ROOM_NOTES_BYTES == storage.NOTES_MAX_BYTES


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


class TestResolveRoom:
    def test_token_name_prefix_and_number(self, config):
        with db.get_db(config.db_path) as conn:
            fam = _shared_room(conn, "Family")
            work = _shared_room(conn, "Work crew")
            assert resolve_room(conn, "alice", fam).room.token == fam
            assert resolve_room(conn, "alice", "  family ").room.token == fam
            assert resolve_room(conn, "alice", "work").room.token == work
            # Numbered from 1 by (created_at, token); both rooms share a second
            # here, so the token decides.
            first, second = sorted([fam, work])
            assert resolve_room(conn, "alice", "1").room.token == first
            assert resolve_room(conn, "alice", "2").room.token == second
            assert isinstance(resolve_room(conn, "alice", "3"), NotFound)

    def test_ambiguous_and_unknown_return_the_numbered_list(self, config):
        with db.get_db(config.db_path) as conn:
            _shared_room(conn, "Family")
            _shared_room(conn, "Family games")
            match = resolve_room(conn, "alice", "fam")
            assert isinstance(match, Ambiguous)
            assert [c.number for c in match.candidates] == [1, 2]
            missing = resolve_room(conn, "alice", "zzz")
            assert isinstance(missing, NotFound) and len(missing.candidates) == 2

    def test_shared_only_leaves_out_private_rooms_and_rooms_left(self, config):
        with db.get_db(config.db_path) as conn:
            mine = db.create_web_chat_room(conn, "alice", "Mine").token
            fam = _shared_room(conn, "Family")
            assert isinstance(resolve_room(conn, "alice", mine), NotFound)
            assert isinstance(resolve_room(conn, "alice", "mine", shared_only=False), Found)
            # A Talk departure keeps the member row; current membership is asked.
            db.upsert_room_participant(conn, room_token=fam, surface="talk",
                                       surface_ref="alice", kind="principal",
                                       user_id="alice")
            conn.execute("UPDATE room_participants SET left_at = datetime('now') "
                         "WHERE room_token = ? AND user_id = 'alice'", (fam,))
            assert isinstance(resolve_room(conn, "alice", "Family"), NotFound)
            # Unless the user has notes about it.
            found = resolve_room(conn, "alice", "Family", include_left_with_notes={fam})
            assert isinstance(found, Found) and found.candidate.has_notes

    def test_names_only_matches_the_exact_name_alone(self, config):
        with db.get_db(config.db_path) as conn:
            fam = _shared_room(conn, "Family")
            assert isinstance(resolve_room(conn, "alice", "fam", names_only=True), NotFound)
            assert isinstance(resolve_room(conn, "alice", "1", names_only=True), NotFound)
            assert isinstance(resolve_room(conn, "alice", fam, names_only=True), NotFound)
            assert isinstance(resolve_room(conn, "alice", "FAMILY", names_only=True), Found)


class TestTheNextcloudGuard:
    """`nextcloud talk create`'s duplicate guard, moved onto the resolver."""

    def test_an_exact_name_still_blocks_and_a_prefix_does_not(self, config, monkeypatch):
        from istota.skills.nextcloud import _registry_room_named

        with db.get_db(config.db_path) as conn:
            token = db.create_web_chat_room(conn, "alice", "Weekly").token
            db.dismiss_room(conn, token, "alice")
        monkeypatch.setenv("ISTOTA_DB_PATH", str(config.db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        # A hidden private room still counts, as before.
        assert _registry_room_named(" weekly ")["token"] == token
        assert _registry_room_named("week") is None
        assert _registry_room_named("1") is None


# ---------------------------------------------------------------------------
# memory --room
# ---------------------------------------------------------------------------


@pytest.fixture
def cli(config, monkeypatch):
    _config_dir(config, "alice")
    (_config_dir(config, "alice") / "USER.md").write_text("## Notes\n\n- mine\n")
    with db.get_db(config.db_path) as conn:
        private = db.create_web_chat_room(conn, "alice", "Mine").token
        fam = _shared_room(conn, "Family")
        private_task = db.create_task(conn, prompt="hi", user_id="alice",
                                      source_type="web", conversation_token=private)
        shared_task = db.create_task(conn, prompt="hi", user_id="alice",
                                     source_type="web", conversation_token=fam)
    monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(config.workspace_path))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.setenv("ISTOTA_BOT_DIR_NAME", "istota")
    monkeypatch.setenv("ISTOTA_DB_PATH", str(config.db_path))
    monkeypatch.setenv("ISTOTA_TASK_ID", str(private_task))
    monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
    monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)

    class Env:
        pass

    e = Env()
    e.fam, e.private_task, e.shared_task = fam, private_task, shared_task
    return e


def _refused(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        memory_main(argv)
    assert exc.value.code == 1
    return json.loads(capsys.readouterr().out)


class TestMemoryRoomFlag:
    def test_append_by_name_writes_the_notes_file_and_not_user_md(self, config, cli, capsys):
        memory_main(["add-heading", "--room", "family", "--heading", "Avoid",
                     "--line", "The house sale"])
        assert json.loads(capsys.readouterr().out)["status"] == "ok"
        text = _notes_file(config, "alice", cli.fam).read_text()
        assert "- The house sale" in text
        assert "house sale" not in (_config_dir(config, "alice") / "USER.md").read_text()
        assert storage.read_room_notes(config, "alice", cli.fam) == text
        memory_main(["show", "--room", cli.fam])
        assert "The house sale" in capsys.readouterr().out

    def test_refused_from_a_shared_room(self, config, cli, monkeypatch, capsys):
        monkeypatch.setenv("ISTOTA_TASK_ID", str(cli.shared_task))
        out = _refused(["show", "--room", "family"], capsys)
        assert out["error"] == "room_notes_from_shared_room"
        out = _refused(["append", "--room", "family", "--heading", "Avoid",
                        "--line", "x"], capsys)
        assert out["error"] == "room_notes_from_shared_room"
        assert not _notes_file(config, "alice", cli.fam).exists()

    def test_an_unknown_or_private_room_is_unavailable(self, config, cli, capsys):
        assert _refused(["show", "--room", "nowhere"], capsys)["error"] == "room_unavailable"
        assert _refused(["show", "--room", "mine"], capsys)["error"] == "room_unavailable"

    def test_room_is_exclusive_with_channel_and_group(self, cli, capsys):
        out = _refused(["show", "--room", "family", "--group", "fam"], capsys)
        assert "--room" in out["error"]


# ---------------------------------------------------------------------------
# !room notes
# ---------------------------------------------------------------------------


def _say(config, conn, user, token, text):
    return asyncio.run(commands.dispatch(
        config, user, token, text, surface="web", conn=conn)).text


class TestRoomNotesCommand:
    def test_refused_in_a_shared_room_with_nothing_shown(self, config):
        _config_dir(config, "alice")
        with db.get_db(config.db_path) as conn:
            fam = _shared_room(conn, "Family")
            storage.write_room_notes(config, "alice", fam, "SECRET-NOTE")
            for text in ("!room notes", "!room notes family", f"!room notes {fam}"):
                reply = _say(config, conn, "alice", fam, text)
                assert reply == commands.ROOM_NOTES_REFUSAL
                assert reply == "Use your private chat with me for your notes."

    def test_list_show_and_numbers_in_a_private_room(self, config):
        _config_dir(config, "alice")
        with db.get_db(config.db_path) as conn:
            mine = db.create_web_chat_room(conn, "alice", "Mine").token
            fam = _shared_room(conn, "Family")
            games = _shared_room(conn, "Family games")
            # Numbers follow creation; pin it, since both rooms share a second.
            for token, stamp in ((fam, "2026-01-01 00:00:00"), (games, "2026-01-02 00:00:00")):
                conn.execute("UPDATE rooms SET created_at = ? WHERE token = ?", (stamp, token))
            storage.write_room_notes(config, "alice", fam, "Skip the house sale.")
            listing = _say(config, conn, "alice", mine, "!room notes")
            assert "1. Family (notes)" in listing
            assert "2. Family games" in listing and "games (notes)" not in listing
            assert "Skip the house sale." in _say(config, conn, "alice", mine, "!room notes 1")
            assert "no notes" in _say(config, conn, "alice", mine, "!room notes 2")
            ambiguous = _say(config, conn, "alice", mine, "!room notes fam")
            assert "More than one" in ambiguous and "1. Family" in ambiguous
            unknown = _say(config, conn, "alice", mine, "!room notes zzz")
            assert "No room of yours" in unknown

    def test_a_room_left_with_notes_is_still_readable(self, config):
        _config_dir(config, "alice")
        with db.get_db(config.db_path) as conn:
            mine = db.create_web_chat_room(conn, "alice", "Mine").token
            fam = _shared_room(conn, "Family", creator="bob", other="alice")
            storage.write_room_notes(config, "alice", fam, "Kept after leaving.")
            db.drop_web_room_member(conn, fam, "alice")
            assert "Kept after leaving." in _say(
                config, conn, "alice", mine, "!room notes Family")


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------


class TestThePrompt:
    def test_the_speakers_own_notes_load_in_the_shared_room_only(self, config):
        from istota.executor import _my_notes_prompt

        _config_dir(config, "alice")
        with db.get_db(config.db_path) as conn:
            fam = _shared_room(conn, "Family")
            mine = db.create_web_chat_room(conn, "alice", "Mine").token
            storage.write_room_notes(config, "alice", fam, "Skip the house sale.")
            in_room = db.get_task(conn, db.create_task(
                conn, prompt="hi", user_id="alice", source_type="web",
                conversation_token=fam))
            bobs = db.get_task(conn, db.create_task(
                conn, prompt="hi", user_id="bob", source_type="web",
                conversation_token=fam))
            private = db.get_task(conn, db.create_task(
                conn, prompt="hi", user_id="alice", source_type="web",
                conversation_token=mine))
            block = _my_notes_prompt(config, in_room, conn)
            assert block.startswith("## My notes about this room (private)")
            assert "Skip the house sale." in block
            assert _my_notes_prompt(config, bobs, conn) == ""
            assert _my_notes_prompt(config, private, conn) == ""


# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------


def _patch_app(config):
    import istota.webui.app as mod
    mod._config = config
    mod.app.state.istota_config = config
    mod._oauth = MagicMock()
    mod._oauth.nextcloud = MagicMock()
    return mod.app


async def _login(client, username):
    import istota.webui.app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(
        return_value={"user_id": username},
    )
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


@pytest.fixture
async def web(config):
    if not _has_web_deps:
        pytest.skip("web dependencies not installed")
    from httpx import ASGITransport, AsyncClient

    app = _patch_app(config)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="https://example.com") as c:
        yield c


async def _rooms(client, cookies):
    return (await client.get("/istota/api/chat/rooms", cookies=cookies)).json()["rooms"]


async def _room_named(client, cookies, name):
    return next(r for r in await _rooms(client, cookies) if r["name"] == name)


class TestWeb:
    async def test_read_and_save_round_trip(self, config, web):
        with db.get_db(config.db_path) as conn:
            fam = _shared_room(conn, "Family")
        alice = await _login(web, "alice")
        room = await _room_named(web, alice, "Family")
        url = f"/istota/api/chat/rooms/{room['id']}/notes"
        body = (await web.get(url, cookies=alice)).json()
        assert body["exists"] is False and body["content"] == ""
        resp = await web.put(url, json={"content": "Skip it.", "revision": body["revision"]},
                             cookies=alice, headers=ORIGIN)
        assert resp.status_code == 200, resp.text
        assert storage.read_room_notes(config, "alice", fam) == "Skip it."
        again = (await web.get(url, cookies=alice)).json()
        assert again["content"] == "Skip it." and again["revision"] == resp.json()["revision"]
        # Bob's notes about the same room are his own.
        bob = await _login(web, "bob")
        bobs = await _room_named(web, bob, "Family")
        assert (await web.get(f"/istota/api/chat/rooms/{bobs['id']}/notes",
                              cookies=bob)).json()["content"] == ""

    async def test_another_users_handle_is_404(self, config, web):
        with db.get_db(config.db_path) as conn:
            _shared_room(conn, "Family")
        alice = await _login(web, "alice")
        room = await _room_named(web, alice, "Family")
        bob = await _login(web, "bob")
        resp = await web.get(f"/istota/api/chat/rooms/{room['id']}/notes", cookies=bob)
        assert resp.status_code == 404
        resp = await web.put(f"/istota/api/chat/rooms/{room['id']}/notes",
                             json={"content": "x", "revision": ""},
                             cookies=bob, headers=ORIGIN)
        assert resp.status_code == 404

    async def test_conflict_and_cap(self, config, web):
        with db.get_db(config.db_path) as conn:
            fam = _shared_room(conn, "Family")
        alice = await _login(web, "alice")
        room = await _room_named(web, alice, "Family")
        url = f"/istota/api/chat/rooms/{room['id']}/notes"
        loaded = (await web.get(url, cookies=alice)).json()
        _config_dir(config, "alice")
        storage.write_room_notes(config, "alice", fam, "written by a task meanwhile")
        resp = await web.put(url, json={"content": "mine", "revision": loaded["revision"]},
                             cookies=alice, headers=ORIGIN)
        assert resp.status_code == 409 and resp.json()["code"] == "conflict"
        assert "mine" not in resp.text
        big = "x" * (storage.NOTES_MAX_BYTES + 1)
        resp = await web.put(url, json={"content": big, "revision": loaded["revision"]},
                             cookies=alice, headers=ORIGIN)
        assert resp.status_code == 413 and resp.json()["code"] == "too_large"

    async def test_listing_flags_and_the_memory_panes_shared(self, config, web):
        with db.get_db(config.db_path) as conn:
            fam = _shared_room(conn, "Family")
            db.create_web_chat_room(conn, "alice", "Mine")
        alice = await _login(web, "alice")
        _config_dir(config, "alice")
        storage.write_room_notes(config, "alice", fam, "noted")
        rooms = {r["name"]: r for r in await _rooms(web, alice)}
        assert rooms["Family"]["shared"] is True and rooms["Family"]["has_my_notes"] is True
        assert rooms["Mine"]["shared"] is False and rooms["Mine"]["has_my_notes"] is False
        # A shared web room's CHANNEL.md is read by all its members.
        memory = (await web.get(f"/istota/api/chat/rooms/{rooms['Family']['id']}/memory",
                                cookies=alice)).json()
        assert memory["shared"] is True

    async def test_deleting_a_room_deletes_every_members_notes(self, config, web):
        with db.get_db(config.db_path) as conn:
            fam = _shared_room(conn, "Family")
        for user in ("alice", "bob"):
            _config_dir(config, user)
            storage.write_room_notes(config, user, fam, f"{user}'s notes")
        alice = await _login(web, "alice")
        room = await _room_named(web, alice, "Family")
        resp = await web.delete(f"/istota/api/chat/rooms/{room['id']}",
                                cookies=alice, headers=ORIGIN)
        assert resp.status_code == 200, resp.text
        assert not _notes_file(config, "alice", fam).exists()
        assert not _notes_file(config, "bob", fam).exists()

    async def test_leaving_a_room_keeps_the_notes(self, config, web):
        with db.get_db(config.db_path) as conn:
            fam = _shared_room(conn, "Family", creator="bob", other="alice")
        _config_dir(config, "alice")
        storage.write_room_notes(config, "alice", fam, "still mine")
        alice = await _login(web, "alice")
        room = await _room_named(web, alice, "Family")
        resp = await web.delete(f"/istota/api/chat/rooms/{room['id']}",
                                cookies=alice, headers=ORIGIN)
        assert resp.status_code == 200, resp.text
        assert storage.read_room_notes(config, "alice", fam) == "still mine"
