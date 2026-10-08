"""Framework providers use live membership and the existing memory index."""

import time
from unittest.mock import patch

import pytest

from istota import db
from istota.lib.text_match import parse_query
from istota.search.core import SearchContext
from tests.test_web_app import _make_config


def chunk(conn, user, kind, content, source="note.md"):
    return conn.execute(
        "INSERT INTO memory_chunks (user_id, source_type, source_id, chunk_index, content, content_hash) VALUES (?, ?, ?, 0, ?, ?)",
        (user, kind, source, content, content),
    ).lastrowid


def room(conn, token, user, name):
    conn.execute("INSERT INTO rooms (token, user_id, name, origin) VALUES (?, ?, ?, 'web')", (token, user, name))
    conn.execute("INSERT INTO room_members (room_token, user_id) VALUES (?, ?)", (token, user))


@pytest.fixture
def ctx(tmp_path):
    return SearchContext(_make_config(tmp_path), "alice", time.monotonic() + 10)


def test_memory_alias_scope_and_vector_disabled(ctx):
    from istota.search.framework import memory
    with db.get_db(ctx.config.db_path) as conn:
        room(conn, "current", "alice", "Falcon room")
        conn.execute("INSERT INTO room_token_migration VALUES ('legacy', 'current', '2026-01-01')")
        alias = chunk(conn, "channel:legacy", "channel_memory_durable", "falcon alias")
        own = chunk(conn, "alice", "memory_file", "falcon own")
        chunk(conn, "alice", "conversation", "falcon transcript")
        chunk(conn, "alice", "skill_overlay", "falcon instruction")
        chunk(conn, "bob", "memory_file", "falcon secret")
    with patch("istota.memory.search.embed_text", side_effect=AssertionError("embedding loaded")):
        result = memory(ctx, parse_query("falcon"), "strict", 20, 0)
    assert {h.id for h in result.hits} == {f"memory:{alias}", f"memory:{own}"}
    alias_hit = next(h for h in result.hits if h.id == f"memory:{alias}")
    assert alias_hit.link == {"type": "route", "path": "/chat/", "params": {"room": "current"}}
    assert alias_hit.title == "CHANNEL.md · Falcon room"
    assert next(h for h in result.hits if h.id == f"memory:{own}").link is None
    with db.get_db(ctx.config.db_path) as conn:
        conn.execute("DELETE FROM room_members WHERE room_token='current'")
    assert [h.id for h in memory(ctx, parse_query("falcon"), "strict", 20, 0).hits] == [f"memory:{own}"]


def test_rooms_display_name_and_facts_current_only(ctx):
    from istota.search.framework import facts, rooms
    with db.get_db(ctx.config.db_path) as conn:
        room(conn, "current", "alice", "Falcon room")
        room(conn, "private", "bob", "Falcon private")
        conn.execute("INSERT INTO web_chat_rooms (token,user_id,name) VALUES ('current','alice','Old handle')")
        conn.execute("INSERT INTO knowledge_facts (user_id,subject,predicate,object,valid_until) VALUES ('alice','falcon','is','current',NULL), ('alice','falcon','was','expired','2000-01-01'), ('bob','falcon','is','secret',NULL)")
    result = rooms(ctx, parse_query("falcon"), "strict", 5, 0)
    assert [h.title for h in result.hits] == ["Falcon room"]
    assert rooms(ctx, parse_query("Old handle"), "strict", 5, 0).hits == []
    result = facts(ctx, parse_query("falcon"), "strict", 5, 0)
    assert [h.title for h in result.hits] == ["falcon is current"]
    assert result.hits[0].link is None


def test_memory_file_links_are_scoped(ctx):
    from istota.search.framework import memory
    root = ctx.config.workspace_root("alice")
    root.mkdir(parents=True)
    path = root / "note.md"
    path.write_text("falcon")
    with db.get_db(ctx.config.db_path) as conn:
        chunk(conn, "alice", "memory_file", "falcon file", str(path))
    result = memory(ctx, parse_query("falcon"), "strict", 5, 0)
    assert result.hits[0].link == {"type": "file", "path": "/Users/alice/note.md"}



def test_chats_badges_author_label_and_paging(ctx):
    from istota.search.framework import chats
    with db.get_db(ctx.config.db_path) as conn:
        room(conn, "shared", "alice", "Falcon room")
        conn.execute("INSERT INTO room_members (room_token,user_id) VALUES ('shared','bob')")
        first = db.add_message(conn, "shared", role="user", body="falcon invoice", origin_surface="web", author_user_id="bob", author_label="Guest")
        db.add_message(conn, "shared", role="assistant", body="falcon invoice", origin_surface="web")
        conn.execute("INSERT INTO message_stars (message_id,user_id) VALUES (?, 'alice')", (first,))
    result = chats(ctx, parse_query("falcon"), "strict", 1, 0)
    assert result.has_more is True
    assert result.hits[0].subtitle == ctx.config.bot_name
    result = chats(ctx, parse_query("falcon"), "strict", 1, 1)
    assert result.has_more is False
    assert result.hits[0].subtitle == "Guest"
    assert result.hits[0].badges == ["shared", "starred"]
    assert result.hits[0].link["params"]["msg"] == str(first)
    with db.get_db(ctx.config.db_path) as conn:
        stamp = conn.execute("SELECT created_at FROM messages WHERE id=?", (first,)).fetchone()[0]
    assert result.hits[0].cursor == {"ts": stamp, "id": first}
    assert result.hits[0].link["params"]["ts"] == stamp


def test_hidden_archived_dismissed_rooms_exclude_channel_memory(ctx):
    from istota.search.framework import memory, rooms
    with db.get_db(ctx.config.db_path) as conn:
        for token in ("archived", "dismissed", "hidden"):
            room(conn, token, "alice", "Falcon " + token)
            chunk(conn, "channel:" + token, "channel_memory", "falcon " + token)
        conn.execute("INSERT INTO room_bindings (room_token,surface,surface_ref) VALUES ('hidden','email','thread@example.com')")
        conn.execute("UPDATE rooms SET archived=1 WHERE token='archived'")
        conn.execute("INSERT INTO room_dismissals (room_token,user_id) VALUES ('dismissed','alice')")
    assert memory(ctx, parse_query("falcon"), "strict", 5, 0).hits == []
    assert rooms(ctx, parse_query("falcon"), "strict", 5, 0).hits == []


def test_fact_calendar_dates_stay_dates(ctx):
    from istota.search.framework import facts
    with db.get_db(ctx.config.db_path) as conn:
        conn.execute("INSERT INTO knowledge_facts (user_id,subject,predicate,object,valid_from) VALUES ('alice','falcon','is','current','2026-01-04')")
    assert facts(ctx, parse_query("falcon"), "strict", 5, 0).hits[0].date == "2026-01-04"
