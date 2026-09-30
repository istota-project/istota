"""`!room share` / `!room unshare` (speech gate draft B3, multiplayer Stage 12).

A grant is a member's own consent, for one room, that answers using a scope
may land in a transcript the other members read. The command writes it for the
caller and nobody else, takes no user id, and speaks the same scope vocabulary
the reach seams withhold (`room_scopes.scope_names`). The last class drives a
grant through the command and then through `_task_withheld_scopes`, the one
answer every reach seam reads, so the command is shown to open exactly what
it says it opens.
"""

import asyncio
from types import SimpleNamespace

import pytest

from istota import commands, db, room_scopes
from istota.config import Config, NextcloudConfig, UserConfig

INDEX = {
    "calendar": SimpleNamespace(shared_room="private"),
    "health": SimpleNamespace(shared_room="private"),
    "room": SimpleNamespace(shared_room="safe"),
}


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    db.init_db(path)
    monkeypatch.setattr(
        "istota.skills._loader.load_skill_index", lambda *a, **k: dict(INDEX),
    )
    return Config(
        db_path=path,
        temp_dir=tmp_path / "temp",
        nextcloud=NextcloudConfig(url="https://cloud.example.test"),
        users={"alice": UserConfig(), "bob": UserConfig(), "carol": UserConfig()},
    )


def _shared(conn, token="grp"):
    db.register_room(conn, token, "alice", origin="web", name="Family")
    db.add_web_room_member(conn, token, "alice")
    db.add_web_room_member(conn, token, "bob")


def _run(config, conn, user, text, token="grp"):
    return asyncio.run(commands.dispatch(
        config, user, token, text, surface="web", conn=conn,
    )).text


def _grants(conn, user, token="grp"):
    return room_scopes.granted_scopes(conn, token, user)


class TestGranting:
    def test_share_grants_one_scope_for_the_caller_only(self, config):
        with db.get_db(config.db_path) as conn:
            _shared(conn)
            reply = _run(config, conn, "bob", "!room share calendar")
            assert _grants(conn, "bob") == {"calendar"}
            assert _grants(conn, "alice") == frozenset()
        assert "calendar" in reply

    def test_unshare_revokes_it(self, config):
        with db.get_db(config.db_path) as conn:
            _shared(conn)
            _run(config, conn, "bob", "!room share calendar")
            _run(config, conn, "bob", "!room unshare calendar")
            assert _grants(conn, "bob") == frozenset()

    def test_all_and_none(self, config):
        with db.get_db(config.db_path) as conn:
            _shared(conn)
            _run(config, conn, "bob", "!room share all")
            assert _grants(conn, "bob") == {"calendar", "health", "files", "memory"}
            _run(config, conn, "bob", "!room share none")
            assert _grants(conn, "bob") == frozenset()

    def test_bare_share_lists_granted_and_withheld(self, config):
        with db.get_db(config.db_path) as conn:
            _shared(conn)
            _run(config, conn, "bob", "!room share health")
            reply = _run(config, conn, "bob", "!room share")
        granted, withheld = reply.split("Withheld", 1)
        assert "health" in granted
        assert "calendar" in withheld and "files" in withheld

    def test_a_safe_skill_or_an_unknown_name_is_not_a_scope(self, config):
        with db.get_db(config.db_path) as conn:
            _shared(conn)
            for name in ("room", "nonsense"):
                reply = _run(config, conn, "bob", f"!room share {name}")
                assert "not a scope" in reply
            assert _grants(conn, "bob") == frozenset()

    def test_a_non_member_cannot_grant(self, config):
        with db.get_db(config.db_path) as conn:
            _shared(conn)
            reply = _run(config, conn, "carol", "!room share calendar")
            assert _grants(conn, "carol") == frozenset()
        assert "member" in reply

    def test_a_side_room_has_nothing_to_share(self, config):
        with db.get_db(config.db_path) as conn:
            _shared(conn)
            side = db.ensure_side_room(conn, "grp", "bob")
            reply = _run(config, conn, "bob", "!room share calendar", token=side.token)
            assert _grants(conn, "bob", side.token) == frozenset()
        assert "side room" in reply

    def test_a_private_room_accepts_a_grant_and_says_when_it_applies(self, config):
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "solo", "alice", origin="web", name="Mine")
            reply = _run(config, conn, "alice", "!room share calendar", token="solo")
            assert _grants(conn, "alice", "solo") == {"calendar"}
        assert "private" in reply


class TestTheGrantReachesTheSeams:
    def test_a_grant_through_the_command_is_what_the_task_reaches(self, config):
        from istota.executor import _task_withheld_scopes

        with db.get_db(config.db_path) as conn:
            _shared(conn)
            task = db.Task(
                id=1, status="running", source_type="web", user_id="bob",
                prompt="x", conversation_token="grp", is_group_chat=True,
            )
            before = _task_withheld_scopes(config, conn, task, INDEX)
            _run(config, conn, "bob", "!room share calendar")
            after = _task_withheld_scopes(config, conn, task, INDEX)
        assert "calendar" in before and "calendar" not in after
        assert after == before - {"calendar"}
