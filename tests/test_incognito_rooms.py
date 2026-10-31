"""Incognito rooms: no ambient memory in, nothing into long-term memory out.

An ordinary private room is the control throughout: each test that shows an
incognito turn left out also shows the same turn kept in a room without the
flag, so an assertion cannot pass because the writer simply saw nothing.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from istota import commands, db, executor
from istota.assignments import caller
from istota.config import (
    MemorySearchConfig,
    UserConfig,
    WebConfig,
)
from istota.lib.text_match import parse_query
from istota.memory.knowledge_graph import ensure_table, get_current_facts
from istota.memory.search import reindex_all
from istota.memory.sleep_cycle import gather_channel_data, gather_day_data
from istota.rooms import policy, scopes
from istota.scheduler import process_one_task
from istota.scheduler_deferred import _process_deferred_kg_ops, _process_deferred_kv_ops
from istota.search.core import SearchContext
from istota.skills.kv import main as kv_main
from istota.skills.memory import main as memory_main
from istota.skills.memory_search import main as memory_search_main
from istota.skills.relay import main as relay_main
from istota.skills.room import main as room_main
from istota.skills.tasks import main as tasks_main
from tests import test_group_room_prompt as prompts
from tests import test_shared_room_restriction as harness

config = harness.config
_bwrap_flag_cache = harness._bwrap_flag_cache
world = prompts.world

CANARY = "incognitocanaryquokka"
CHANNEL_CANARY = "channelcanarynarwhal"


def _rooms(config) -> tuple[str, str]:
    with db.get_db(config.db_path) as conn:
        secret = db.create_web_chat_room(conn, "alice", "Secret", incognito=True).token
        plain = db.create_web_chat_room(conn, "alice", "Plain").token
    return secret, plain


def _done(conn, token, *, prompt=CANARY, **kwargs) -> int:
    task_id = db.create_task(
        conn, prompt=prompt, user_id="alice", source_type="web",
        conversation_token=token, **kwargs,
    )
    db.update_task_status(conn, task_id, "completed", result=f"{prompt} answered")
    return task_id


class TestTheFlagIsFrozenOntoEachTask:
    def test_a_room_is_incognito_only_when_created_so(self, config):
        secret, plain = _rooms(config)
        with db.get_db(config.db_path) as conn:
            assert db.get_room(conn, secret).incognito is True
            assert db.get_room(conn, plain).incognito is False

    @pytest.mark.parametrize("source_type", ["web", "subtask", "scheduled", "cli"])
    def test_every_creation_path_reads_the_room(self, config, source_type):
        secret, plain = _rooms(config)
        with db.get_db(config.db_path) as conn:
            inside = db.create_task(conn, prompt="p", user_id="alice",
                                    source_type=source_type, conversation_token=secret)
            outside = db.create_task(conn, prompt="p", user_id="alice",
                                     source_type=source_type, conversation_token=plain)
            assert db.get_task(conn, inside).incognito is True
            assert db.get_task(conn, outside).incognito is False

    def test_a_child_inherits_even_with_no_room_of_its_own(self, config):
        secret, _ = _rooms(config)
        with db.get_db(config.db_path) as conn:
            parent = db.create_task(conn, prompt="p", user_id="alice",
                                    source_type="web", conversation_token=secret)
            child = db.create_task(conn, prompt="c", user_id="alice",
                                   source_type="subtask", parent_task_id=parent)
            assert db.get_task(conn, child).incognito is True

    def test_it_survives_the_rooms_row(self, config):
        secret, _ = _rooms(config)
        with db.get_db(config.db_path) as conn:
            task_id = _done(conn, secret)
            conn.execute("DELETE FROM rooms WHERE token = ?", (secret,))
            assert db.get_task(conn, task_id).incognito is True


class TestNothingIn:
    def test_a_turn_loads_only_the_room(self, world):
        secret, plain = _rooms(world)
        channel = world.workspace_path / "Channels" / secret
        channel.mkdir(parents=True)
        (channel / "CHANNEL.md").write_text(f"- {CHANNEL_CANARY}\n")

        seen = prompts._turn(world, secret, "alice", "what do you know?")

        assert seen["task"].incognito is True
        assert seen["ambient_off"] is True
        assert CHANNEL_CANARY in seen["prompt"]
        for canary in (*prompts.PERSONAL, prompts.FAM_NOTEBOOK, prompts.CLUB_NOTEBOOK):
            assert canary not in seen["prompt"], canary
        assert "memory_search" not in seen["allowed_skills"]
        assert "kv" not in seen["allowed_skills"]
        assert "memory" in seen["allowed_skills"]

        control = prompts._turn(world, plain, "alice", "what do you know?")
        assert prompts.USER_MD in control["prompt"]
        assert prompts.KG in control["prompt"]
        assert "memory_search" in control["allowed_skills"]

    def test_the_system_half_says_what_the_room_is(self, config):
        secret, plain = _rooms(config)
        with db.get_db(config.db_path) as conn:
            inside = db.get_task(conn, _done(conn, secret))
            outside = db.get_task(conn, _done(conn, plain))
            card = executor.room_card(config, inside, conn, withheld_scopes=frozenset(),
                                      room_cli_available=True)
            control = executor.room_card(config, outside, conn, withheld_scopes=frozenset(),
                                         room_cli_available=True)
        assert executor.INCOGNITO_ROOM_LINE in card
        assert control == ""

    def test_no_group_material_is_resolved(self, world):
        secret, plain = _rooms(world)
        with db.get_db(world.db_path) as conn:
            inside = db.get_task(conn, _done(conn, secret))
            outside = db.get_task(conn, _done(conn, plain))
            assert scopes.task_group_ids(conn, inside) == []
            assert scopes.task_group_ids(conn, outside) == ["club", "fam"]


class TestNothingOut:
    def test_neither_sleep_cycle_reads_the_room(self, config):
        secret, plain = _rooms(config)
        with db.get_db(config.db_path) as conn:
            _done(conn, secret)
            _done(conn, plain, prompt="ordinarywombat")
            day = gather_day_data(config, conn, "alice", 24, None)
            assert "ordinarywombat" in day
            assert CANARY not in day
            assert gather_channel_data(config, conn, secret, 24, None) == ""
            assert "ordinarywombat" in gather_channel_data(config, conn, plain, 24, None)
            active = db.get_active_channel_tokens(conn, "2000-01-01")
            assert plain in active
            assert secret not in active

    def _namespaces(self, config, token) -> set[str]:
        config.memory_search = MemorySearchConfig(enabled=True, auto_index_conversations=True)
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, prompt=CANARY, user_id="alice",
                                     source_type="web", conversation_token=token)
        with patch("istota.scheduler.execute_task", return_value=(True, "The answer.", None, None)), \
                patch("istota.scheduler.run_coro", return_value=True), \
                patch("istota.memory.search.ensure_vec_table", return_value=False), \
                patch("istota.memory.search.enable_vec_extension", return_value=False):
            process_one_task(config)
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_id).status == "completed"
            rows = conn.execute("SELECT DISTINCT user_id FROM memory_chunks").fetchall()
        return {row[0] for row in rows}

    def test_the_auto_index_skips_a_turn(self, config):
        secret, _ = _rooms(config)
        assert self._namespaces(config, secret) == set()

    def test_control_the_auto_index_takes_an_ordinary_turn(self, config):
        _, plain = _rooms(config)
        assert self._namespaces(config, plain) == {"alice", f"channel:{plain}"}

    def test_a_reindex_does_not_pull_it_back_in(self, config):
        secret, plain = _rooms(config)
        for token, text in ((secret, CHANNEL_CANARY), (plain, "ordinarypangolin")):
            channel = config.workspace_path / "Channels" / token
            channel.mkdir(parents=True)
            (channel / "CHANNEL.md").write_text(f"- {text}\n")
        with db.get_db(config.db_path) as conn, \
                patch("istota.memory.search.ensure_vec_table", return_value=False), \
                patch("istota.memory.search.enable_vec_extension", return_value=False):
            _done(conn, secret)
            _done(conn, plain, prompt="ordinarywombat")
            reindex_all(conn, config, "alice", lookback_days=1)
            content = "\n".join(
                row[0] for row in conn.execute("SELECT content FROM memory_chunks")
            )
        assert "ordinarywombat" in content
        assert "ordinarypangolin" in content
        assert CANARY not in content
        assert CHANNEL_CANARY not in content

    def _deferred(self, config, conn, token, suffix, ops):
        task = db.get_task(conn, _done(conn, token))
        directory = config.temp_dir / "alice"
        path = directory / f"task_{task.id}_{suffix}.json"
        path.write_text(json.dumps(ops))
        return task, directory, path

    def test_a_model_written_fact_is_dropped(self, config):
        secret, plain = _rooms(config)
        op = [{"op": "add_fact", "subject": "alice", "predicate": "likes", "object": CANARY}]
        with db.get_db(config.db_path) as conn:
            ensure_table(conn)
            task, directory, path = self._deferred(config, conn, secret, "kg_ops", op)
        assert _process_deferred_kg_ops(config, task, directory) == 0
        assert not path.exists()
        with db.get_db(config.db_path) as conn:
            assert get_current_facts(conn, "alice") == []
            task, directory, _ = self._deferred(config, conn, plain, "kg_ops", op)
        assert _process_deferred_kg_ops(config, task, directory) == 1

    def test_a_model_written_kv_op_is_dropped(self, config):
        secret, plain = _rooms(config)
        op = [{"op": "set", "namespace": "n", "key": "k", "value": json.dumps(CANARY)}]
        with db.get_db(config.db_path) as conn:
            task, directory, path = self._deferred(config, conn, secret, "kv_ops", op)
        assert _process_deferred_kv_ops(config, task, directory) == 0
        assert not path.exists()
        with db.get_db(config.db_path) as conn:
            task, directory, _ = self._deferred(config, conn, plain, "kv_ops", op)
        assert _process_deferred_kv_ops(config, task, directory) == 1


@pytest.fixture
def cli(config, monkeypatch):
    """The skill CLIs' environment, in a task in the incognito room."""
    secret, plain = _rooms(config)
    (config.workspace_path / "Users" / "alice" / "istota" / "config").mkdir(parents=True)
    user_md = config.workspace_path / "Users" / "alice" / "istota" / "config" / "USER.md"
    user_md.write_text("## Notes\n\n- mine\n")
    channel_md = config.workspace_path / "Channels" / secret / "CHANNEL.md"
    channel_md.parent.mkdir(parents=True)
    channel_md.write_text("## Notes\n\n- ours\n")
    with db.get_db(config.db_path) as conn:
        inside = db.create_task(conn, prompt="p", user_id="alice", source_type="web",
                                conversation_token=secret)
        db.update_task_status(conn, inside, "running")
        outside = db.create_task(conn, prompt="p", user_id="alice", source_type="web",
                                 conversation_token=plain)
        db.update_task_status(conn, outside, "running")
    monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(config.workspace_path))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.setenv("ISTOTA_BOT_DIR_NAME", "istota")
    monkeypatch.setenv("ISTOTA_DB_PATH", str(config.db_path))
    monkeypatch.setenv("ISTOTA_TASK_ID", str(inside))
    monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", secret)
    return {"secret": secret, "plain": plain, "inside": inside, "outside": outside,
            "user_md": user_md, "channel_md": channel_md}


def _out(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


class TestTheMemoryCli:
    @pytest.mark.parametrize("argv", [
        ["append", "--heading", "Notes", "--line", "- leaked"],
        ["remove", "--heading", "Notes", "--match", "mine"],
        ["show"],
        ["headings"],
    ])
    def test_the_users_memory_is_refused_both_ways(self, cli, argv, capsys):
        with pytest.raises(SystemExit):
            memory_main(argv)
        assert _out(capsys).get("error") == "incognito_room"
        assert cli["user_md"].read_text() == "## Notes\n\n- mine\n"

    def test_the_rooms_own_notes_stay_writable(self, cli, capsys):
        memory_main(["append", "--channel", cli["secret"], "--heading", "Notes",
                     "--line", f"- {CHANNEL_CANARY}"])
        capsys.readouterr()
        assert CHANNEL_CANARY in cli["channel_md"].read_text()

    def test_a_group_is_refused(self, cli, capsys):
        with pytest.raises(SystemExit):
            memory_main(["show", "--group", "fam"])
        assert _out(capsys).get("error") == "incognito_room"

    def test_control_an_ordinary_room_reads_user_md(self, cli, monkeypatch, capsys):
        monkeypatch.setenv("ISTOTA_TASK_ID", str(cli["outside"]))
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", cli["plain"])
        memory_main(["show"])
        assert "mine" in capsys.readouterr().out


class TestTheOtherMemoryClis:
    @pytest.mark.parametrize("argv", [
        ["search", "anything"],
        ["facts"],
        ["add-fact", "alice", "likes", CANARY],
    ])
    def test_memory_search_refuses_every_verb(self, cli, argv, capsys):
        with pytest.raises(SystemExit):
            memory_search_main(argv)
        assert _out(capsys).get("reason") == "incognito_room"

    def test_kv_is_refused(self, cli, capsys):
        with pytest.raises(SystemExit):
            kv_main(["set", "n", "k", '"v"'])
        assert _out(capsys).get("reason") == "incognito_room"

    def test_an_ordinary_room_cannot_index_an_incognito_turn(self, cli, config, monkeypatch, capsys):
        with db.get_db(config.db_path) as conn:
            secret_task = _done(conn, cli["secret"])
        monkeypatch.setenv("ISTOTA_TASK_ID", str(cli["outside"]))
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", cli["plain"])
        with pytest.raises(SystemExit):
            memory_search_main(["index", "conversation", str(secret_task)])
        assert "incognito_room" in capsys.readouterr().out

    def test_relay_is_refused(self, cli, capsys):
        with pytest.raises(SystemExit):
            relay_main(["ask", "bob", "--request-key", "k", CANARY])
        assert "incognito_room" in capsys.readouterr().out

    def test_room_post_is_refused(self, cli, capsys):
        with pytest.raises(SystemExit):
            room_main(["post", "--request-key", "k", "--room", "Plain", CANARY])
        assert "incognito_room" in capsys.readouterr().out

    def test_no_assignment_starts_here(self, cli, config):
        with db.get_db(config.db_path) as conn:
            assert caller.user_turn(conn, task_id=cli["inside"], user_id="alice") is False
            assert caller.user_turn(conn, task_id=cli["outside"], user_id="alice") is True


class TestTaskReadsAcrossTheBoundary:
    def _complete(self, config, token, text):
        with db.get_db(config.db_path) as conn:
            return _done(conn, token, prompt=text)

    def test_status_from_another_room_is_not_found(self, cli, config, monkeypatch, capsys):
        secret_task = self._complete(config, cli["secret"], CANARY)
        monkeypatch.setenv("ISTOTA_TASK_ID", str(cli["outside"]))
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", cli["plain"])
        with pytest.raises(SystemExit):
            tasks_main(["status", str(secret_task)])
        out = capsys.readouterr().out
        assert '"not_found"' in out
        assert CANARY not in out

    def test_recent_from_another_room_leaves_it_out(self, cli, config, monkeypatch, capsys):
        self._complete(config, cli["secret"], CANARY)
        self._complete(config, cli["plain"], "ordinarywombat")
        monkeypatch.setenv("ISTOTA_TASK_ID", str(cli["outside"]))
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", cli["plain"])
        tasks_main(["recent"])
        out = capsys.readouterr().out
        assert "ordinarywombat" in out
        assert CANARY not in out

    def test_recent_inside_sees_only_this_room(self, cli, config, capsys):
        self._complete(config, cli["secret"], CANARY)
        self._complete(config, cli["plain"], "ordinarywombat")
        tasks_main(["recent"])
        out = capsys.readouterr().out
        assert CANARY in out
        assert "ordinarywombat" not in out

    def test_status_inside_cannot_read_another_room(self, cli, config, capsys):
        other = self._complete(config, cli["plain"], "ordinarywombat")
        with pytest.raises(SystemExit):
            tasks_main(["status", str(other)])
        assert '"not_found"' in capsys.readouterr().out

    def test_a_transcript_from_another_room_is_unavailable(self, cli, config, monkeypatch, capsys):
        secret_task = self._complete(config, cli["secret"], CANARY)
        monkeypatch.setenv("ISTOTA_TASK_ID", str(cli["outside"]))
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", cli["plain"])
        monkeypatch.setenv("ISTOTA_SESSION_LOG_DIR", str(config.db_path.parent / "logs"))
        try:
            tasks_main(["transcript", str(secret_task)])
        except SystemExit:
            pass
        assert '"available": false' in capsys.readouterr().out


class TestCommands:
    def _ctx(self, config, conn, token, args=""):
        return commands.CommandContext(
            config=config, conn=conn, user_id="alice", conversation_token=token,
            args=args, surface="web",
        )

    @pytest.mark.parametrize("handler,args", [
        (commands.cmd_memory, "user"),
        (commands.cmd_memory, "facts"),
        (commands.cmd_search, "anything"),
        (commands.cmd_status, ""),
    ])
    async def test_personal_replies_are_refused_inside(self, config, handler, args):
        secret, plain = _rooms(config)
        with db.get_db(config.db_path) as conn:
            answer = await handler(self._ctx(config, conn, secret, args))
            assert "private chat" in answer

    async def test_status_elsewhere_leaves_out_a_running_turn(self, config):
        secret, plain = _rooms(config)
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, prompt=CANARY, user_id="alice",
                                     source_type="web", conversation_token=secret)
            db.update_task_status(conn, task_id, "running")
            db.create_task(conn, prompt="ordinarywombat", user_id="alice",
                           source_type="web", conversation_token=plain)
            answer = await commands.cmd_status(self._ctx(config, conn, plain))
        assert CANARY not in answer

    async def test_more_from_another_room_finds_nothing(self, config):
        secret, plain = _rooms(config)
        with db.get_db(config.db_path) as conn:
            task_id = _done(conn, secret)
            conn.execute("UPDATE tasks SET execution_trace = '[]' WHERE id = ?", (task_id,))
            answer = await commands.cmd_more(self._ctx(config, conn, plain, str(task_id)))
            assert answer == f"Task #{task_id} not found."
            inside = await commands.cmd_more(self._ctx(config, conn, secret, str(task_id)))
            assert inside != f"Task #{task_id} not found."


class TestTheGroupLink:
    def test_it_is_refused(self, config):
        secret, plain = _rooms(config)
        with db.get_db(config.db_path) as conn:
            db.create_group(conn, "fam", kind="family", display_name="fam", created_by="op")
            db.add_group_member(conn, "fam", "alice", added_by="op")
            assert "incognito" in policy.group_link_refusal(conn, secret, "alice", "fam").lower()
            assert policy.group_link_refusal(conn, plain, "alice", "fam") is None


class TestWebSearch:
    def _ctx(self, config):
        return SearchContext(config, "alice", time.monotonic() + 10)

    def test_no_provider_finds_it(self, config):
        from istota.search.framework import chats, memory, rooms

        secret, plain = _rooms(config)
        with db.get_db(config.db_path) as conn:
            db.add_message(conn, secret, role="user", body="falcon secret", origin_surface="web")
            db.add_message(conn, plain, role="user", body="falcon plain", origin_surface="web")
            for token in (secret, plain):
                conn.execute(
                    "INSERT INTO memory_chunks (user_id, source_type, source_id, chunk_index, "
                    "content, content_hash) VALUES (?, 'channel_memory_durable', ?, 0, ?, ?)",
                    (f"channel:{token}", token, f"falcon {token}", token),
                )
        ctx = self._ctx(config)
        query = parse_query("falcon")
        found = chats(ctx, query, "strict", 20, 0)
        assert [hit.snippet for hit in found.hits] == [found.hits[0].snippet]
        assert "plain" in found.hits[0].snippet
        names = rooms(ctx, parse_query("secret"), "strict", 20, 0)
        assert names.hits == []
        assert rooms(ctx, parse_query("plain"), "strict", 20, 0).hits
        with patch("istota.memory.search.embed_text", side_effect=AssertionError):
            chunks = memory(ctx, query, "strict", 20, 0)
        assert [hit.snippet for hit in chunks.hits] == [f"falcon {plain}"]


ORIGIN = {"origin": "https://example.com"}
API = "/istota/api"


@pytest.fixture
async def client(config):
    import istota.webui.app as mod
    from httpx import ASGITransport, AsyncClient

    config.users = {"alice": UserConfig(display_name="Alice"), "bob": UserConfig(display_name="Bob")}
    config.web = WebConfig(
        enabled=True, port=8766, oauth2_provider="https://cloud.example.com",
        oauth2_client_id="istota-web", oauth2_client_secret="s",
        session_secret_key="test-session-key",
    )
    config.site.hostname = "example.com"
    mod._config = config
    mod.app.state.istota_config = config
    mod._oauth = MagicMock()
    mod._oauth.nextcloud = MagicMock()
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": "alice"})
    async with AsyncClient(transport=ASGITransport(app=mod.app),
                           base_url="https://example.com") as c:
        resp = await c.get("/istota/callback", follow_redirects=False)
        c.cookies.update(resp.cookies)
        yield c


async def _create(client, **body) -> dict:
    resp = await client.post(f"{API}/chat/rooms", headers=ORIGIN, json={"name": "Secret", **body})
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestTheRoutes:
    async def test_create_and_list_say_so(self, client):
        secret = await _create(client, incognito=True)
        plain = await _create(client, name="Plain")
        assert secret["incognito"] is True
        assert plain["incognito"] is False
        listed = (await client.get(f"{API}/chat/rooms")).json()["rooms"]
        assert {room["id"]: room["incognito"] for room in listed}[secret["id"]] is True

    async def test_a_truthy_value_is_not_a_yes(self, client):
        resp = await client.post(f"{API}/chat/rooms", headers=ORIGIN,
                                 json={"name": "Secret", "incognito": "yes"})
        assert resp.status_code == 400

    async def test_the_flag_cannot_be_changed(self, client, config):
        secret = await _create(client, incognito=True)
        resp = await client.patch(f"{API}/chat/rooms/{secret['id']}", headers=ORIGIN,
                                  json={"incognito": False})
        assert resp.status_code == 400
        with db.get_db(config.db_path) as conn:
            assert db.get_room(conn, secret["token"]).incognito is True

    async def test_no_second_member(self, client, config):
        secret = await _create(client, incognito=True)
        resp = await client.post(f"{API}/chat/rooms/{secret['id']}/members", headers=ORIGIN,
                                 json={"user_id": "bob", "acknowledge_history": True})
        assert resp.status_code == 409
        with db.get_db(config.db_path) as conn:
            assert db.list_room_members(conn, secret["token"]) == ["alice"]

    async def test_no_talk(self, client):
        secret = await _create(client, incognito=True)
        resp = await client.post(f"{API}/chat/rooms/{secret['id']}/promote", headers=ORIGIN)
        assert resp.status_code == 409
        assert "incognito" in resp.json()["error"].lower()

    async def test_delete_wipes_what_it_left_behind(self, client, config):
        from istota.session.session_log import resolve_session_log_dir

        secret = await _create(client, incognito=True)
        plain = await _create(client, name="Plain")
        token = secret["token"]
        log_root = resolve_session_log_dir(config.db_path)
        (log_root / "alice").mkdir(parents=True)
        inbox = config.workspace_path / "Users" / "alice" / "inbox" / "web-chat" / "2026-10-30"
        inbox.mkdir(parents=True)
        upload = inbox / "photo.png"
        upload.write_bytes(b"x")
        keep = inbox / "keep.png"
        keep.write_bytes(b"x")
        channel = config.workspace_path / "Channels" / token
        channel.mkdir(parents=True)
        (channel / "CHANNEL.md").write_text("- notes\n")
        with db.get_db(config.db_path) as conn:
            task_id = _done(conn, token, attachments=[str(upload)])
            kept_task = _done(conn, plain["token"], prompt="ordinarywombat", attachments=[str(keep)])
            db.log_task(conn, task_id, "info", CANARY)
            db.add_message(conn, token, role="user", body=CANARY, origin_surface="web", task_id=task_id)
            conn.execute(
                "INSERT INTO memory_chunks (user_id, source_type, source_id, chunk_index, "
                "content, content_hash) VALUES (?, 'channel_memory_durable', 'x', 0, 'x', 'x')",
                (f"channel:{token}",),
            )
        log = log_root / "alice" / f"2026-10-30T10-00-00-000Z_task-{task_id}-1.jsonl"
        log.write_text("{}\n")
        kept_log = log_root / "alice" / f"2026-10-30T10-00-00-000Z_task-{kept_task}-1.jsonl"
        kept_log.write_text("{}\n")

        resp = await client.delete(f"{API}/chat/rooms/{secret['id']}", headers=ORIGIN)
        assert resp.status_code == 200

        assert not channel.exists()
        assert not upload.exists()
        assert not log.exists()
        assert keep.exists()
        assert kept_log.exists()
        with db.get_db(config.db_path) as conn:
            for sql in ("SELECT 1 FROM tasks WHERE id = ?", "SELECT 1 FROM task_logs WHERE task_id = ?",
                        "SELECT 1 FROM messages WHERE task_id = ?"):
                assert conn.execute(sql, (task_id,)).fetchone() is None, sql
            assert conn.execute("SELECT 1 FROM memory_chunks WHERE user_id = ?",
                                (f"channel:{token}",)).fetchone() is None


class TestNothingLandsThereOrLeavesThrough:
    def test_the_log_route_is_not_asked(self, config):
        secret, plain = _rooms(config)
        for token, expected in ((secret, False), (plain, True)):
            with db.get_db(config.db_path) as conn:
                db.create_task(conn, prompt=CANARY, user_id="alice",
                               source_type="web", conversation_token=token)
            with patch("istota.scheduler.execute_task", return_value=(True, "ok", None, None)), \
                    patch("istota.scheduler.run_coro", return_value=True), \
                    patch("istota.scheduler.effective_log_destinations", return_value=[]) as dests:
                process_one_task(config)
            assert dests.called is expected, token

    def test_it_is_never_the_default_room_or_a_private_room(self, config):
        with db.get_db(config.db_path) as conn:
            secret = db.create_web_chat_room(conn, "alice", "Secret", incognito=True).token
            assert db.is_private_room_of(conn, secret, "alice") is False
            default = db.default_web_room(conn, "alice")
            assert default is None or default.token != secret
            assert db.ensure_default_web_chat_room(conn, "alice").token != secret
            plain = db.create_web_chat_room(conn, "alice", "Plain").token
            assert db.is_private_room_of(conn, plain, "alice") is True

    def test_it_cannot_be_pinned_or_routed_to(self, config):
        from istota.transport.route_checks import refuse_shared_destination

        secret, plain = _rooms(config)
        with db.get_db(config.db_path) as conn:
            with pytest.raises(ValueError, match="incognito"):
                refuse_shared_destination(conn, secret, "web room")
            refuse_shared_destination(conn, plain, "web room")
            conn.execute("INSERT OR IGNORE INTO user_profiles (user_id) VALUES ('alice')")
            for token, answer in ((secret, None), (plain, plain)):
                conn.execute("UPDATE user_profiles SET default_room = ? WHERE user_id = 'alice'",
                             (token,))
                assert db.configured_default_room(conn, "alice") == answer

    def test_a_model_written_alert_is_dropped(self, config):
        from istota.scheduler_deferred import _process_deferred_user_alerts

        secret, plain = _rooms(config)
        for token, reaches_the_sender in ((secret, False), (plain, True)):
            with db.get_db(config.db_path) as conn:
                task = db.get_task(conn, _done(conn, token))
            directory = config.temp_dir / "alice"
            path = directory / f"task_{task.id}_user_alerts.json"
            path.write_text(json.dumps([{"message": CANARY, "title": CANARY}]))
            with patch("istota.notifications.resolvers.task_alert.flatten",
                       return_value="") as read:
                _process_deferred_user_alerts(config, task, directory)
            assert not path.exists()
            assert read.called is reaches_the_sender, token

    def test_a_name_lookup_does_not_find_it(self, config):
        from istota.rooms.lookup import room_candidates

        secret, plain = _rooms(config)
        with db.get_db(config.db_path) as conn:
            tokens = {c.token for c in room_candidates(conn, "alice", shared_only=False)}
        assert plain in tokens
        assert secret not in tokens


class TestHeldQuestionsAndDrafts:
    def _park(self, conn, token, text):
        task_id = db.create_task(conn, prompt="p", user_id="alice", source_type="web",
                                 conversation_token=token)
        conn.execute(
            "UPDATE tasks SET status = 'pending_confirmation', confirmation_prompt = ? WHERE id = ?",
            (text, task_id),
        )
        return task_id

    def test_a_bare_yes_elsewhere_does_not_reach_its_question(self, config):
        from istota import confirmations

        secret, plain = _rooms(config)
        with db.get_db(config.db_path) as conn:
            parked = self._park(conn, secret, CANARY)
            assert confirmations.pending_for_user(conn, "alice") == []
            assert confirmations.resolve(conn, "alice", conversation_token=plain).task is None
            assert confirmations.resolve(conn, "alice", conversation_token=secret).task.id == parked

    def test_a_bare_yes_inside_does_not_reach_another_rooms_question(self, config):
        from istota import confirmations

        secret, plain = _rooms(config)
        with db.get_db(config.db_path) as conn:
            parked = self._park(conn, plain, "ordinary question")
            assert confirmations.resolve(conn, "alice", conversation_token=secret).task is None
            assert confirmations.resolve(conn, "alice", conversation_token=plain).task.id == parked

    def _hold(self, conn, token):
        from istota.mail import drafts

        return drafts.hold(
            conn, user_id="alice", task_id=None, room_token=token, to_addrs=["a@example.com"],
            cc_addrs=[], bcc_addrs=[], subject=CANARY, body=CANARY, html=False, in_reply_to=None,
            references=None, attachments=[], origin_target=None, hold_reason="test",
        )

    def test_its_drafts_are_not_listed_elsewhere(self, config):
        from istota.mail import drafts

        secret, plain = _rooms(config)
        with db.get_db(config.db_path) as conn:
            self._hold(conn, secret)
            assert drafts.pending_for_user(conn, "alice") == []
            self._hold(conn, plain)
            assert len(drafts.pending_for_user(conn, "alice")) == 1


class TestTheRefusedSkills:
    def test_assignment_is_refused(self, cli, capsys):
        from istota.skills.assignment import main as assignment_main

        with pytest.raises(SystemExit):
            assignment_main(["list"])
        assert _out(capsys).get("reason") == "incognito_room"

    def test_rooms_list_is_refused_inside(self, cli, capsys):
        from istota.skills.rooms import main as rooms_main

        with pytest.raises(SystemExit):
            rooms_main(["list"])
        assert _out(capsys).get("reason") == "incognito_room"

    def test_rooms_list_elsewhere_leaves_it_out(self, cli, monkeypatch, capsys):
        from istota.skills.rooms import main as rooms_main

        monkeypatch.setenv("ISTOTA_TASK_ID", str(cli["outside"]))
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", cli["plain"])
        rooms_main(["list"])
        out = capsys.readouterr().out
        assert cli["plain"] in out
        assert cli["secret"] not in out


class TestDeleteTakesEveryRow:
    async def test_drafts_notifications_and_sent_mail(self, client, config):
        secret = await _create(client, incognito=True)
        token = secret["token"]
        with db.get_db(config.db_path) as conn:
            TestHeldQuestionsAndDrafts()._hold(conn, token)
            conn.execute(
                "INSERT INTO notifications (user_id, source, dedup_key, title, room_token) "
                "VALUES ('alice', 'test', 'k', ?, ?)", (CANARY, token),
            )
            conn.execute(
                "INSERT INTO sent_emails (user_id, message_id, to_addr, conversation_token) "
                "VALUES ('alice', 'm', 'a@example.com', ?)", (token,),
            )
        resp = await client.delete(f"{API}/chat/rooms/{secret['id']}", headers=ORIGIN)
        assert resp.status_code == 200
        with db.get_db(config.db_path) as conn:
            for table, column in (("outbound_drafts", "room_token"), ("notifications", "room_token"),
                                  ("sent_emails", "conversation_token")):
                assert conn.execute(f"SELECT 1 FROM {table} WHERE {column} = ?",
                                    (token,)).fetchone() is None, table
