"""`!room share` / `!room unshare` are retired (ISSUE-576).

A member's turn in a shared room runs at their full reach, so there is no grant
to make. The commands still answer, with what replaced them, rather than falling
through to a usage line a member would read as a typo. The table they wrote
to is dropped by the migration (`tests/test_room_members_api.py`).
"""

import asyncio

import pytest

from istota import commands, db
from istota.config import Config, NextcloudConfig, UserConfig


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "state.db"
    db.init_db(path)
    return Config(
        db_path=path,
        temp_dir=tmp_path / "temp",
        nextcloud=NextcloudConfig(url="https://cloud.example.test"),
        users={"alice": UserConfig(), "bob": UserConfig()},
    )


def _shared(conn, token="grp"):
    db.register_room(conn, token, "alice", origin="web", name="Family")
    db.add_web_room_member(conn, token, "alice")
    db.add_web_room_member(conn, token, "bob")


def _run(config, conn, user, text, token="grp"):
    return asyncio.run(commands.dispatch(
        config, user, token, text, surface="web", conn=conn,
    )).text


@pytest.mark.parametrize("text", ["!room share calendar", "!room unshare all", "!room share"])
def test_the_retired_commands_say_what_replaced_them(config, text):
    with db.get_db(config.db_path) as conn:
        _shared(conn)
        reply = _run(config, conn, "bob", text)
    assert "Room grants are gone" in reply
    assert "personal memory is not loaded" in reply


def test_the_usage_line_no_longer_offers_them(config):
    with db.get_db(config.db_path) as conn:
        _shared(conn)
        reply = _run(config, conn, "bob", "!room nonsense")
    assert "share" not in reply

