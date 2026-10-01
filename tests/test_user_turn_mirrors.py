"""User-turn mirror planning uses surface refs and static surface facts."""

from dataclasses import replace
import sqlite3

import pytest

from istota import db, surfaces
from istota.config import Config
from istota.transport import routing

from .support.rooms import promoted_room


def test_promoted_room_returns_talk_ref_even_when_talk_is_disabled(db_path):
    config = Config(db_path=db_path)
    config.talk.enabled = False
    with db.get_db(db_path) as conn:
        room = promoted_room(conn, "alice")
        assert routing.plan_user_turn_mirrors(conn, config, room.canonical, "web") == [
            routing.UserTurnMirror("talk", room.talk_ref, "as_user"),
        ]
        # Origin Talk is already visible there; canonical web needs no post.
        assert routing.plan_user_turn_mirrors(conn, config, room.canonical, "talk") == []


@pytest.mark.parametrize("view,mode", [
    ("canonical", "as_user"), (None, "as_user"), ("external", None),
    ("external", "attributed"),
])
def test_planner_reads_both_destination_facts(db_path, monkeypatch, view, mode):
    monkeypatch.setitem(surfaces.SURFACES, "talk", replace(
        surfaces.SURFACES["talk"], room_view=view, user_turn_mirror=mode,
    ))
    with db.get_db(db_path) as conn:
        room = promoted_room(conn, "alice")
        mirrors = routing.plan_user_turn_mirrors(conn, Config(), room.canonical, "email")
    expected = []
    if view == "external" and mode is not None:
        expected = [routing.UserTurnMirror("talk", room.talk_ref, mode)]
    assert mirrors == expected


@pytest.mark.parametrize("state", ["missing", "unbound", "archived"])
def test_no_mirrors_without_a_live_bound_room(db_path, state):
    with db.get_db(db_path) as conn:
        token = "web-empty"
        if state == "archived":
            token = promoted_room(conn, "alice").canonical
            conn.execute("UPDATE rooms SET archived = 1 WHERE token = ?", (token,))
        elif state == "unbound":
            db.register_room(conn, token, "alice", origin="web")
        assert routing.plan_user_turn_mirrors(conn, Config(), token, "web") == []


def test_database_failure_is_logged_and_returns_no_mirrors(caplog):
    with sqlite3.connect(":memory:") as conn:
        assert routing.plan_user_turn_mirrors(conn, Config(), "missing", "web") == []
    assert "mirror" in caplog.text.lower()


def test_container_bindings_are_not_external_views(db_path):
    with db.get_db(db_path) as conn:
        room = promoted_room(conn, "alice")
        db.add_room_binding(conn, room.canonical, "email", "email-thread")
        db.add_room_binding(conn, room.canonical, "whatsapp", "group@g.us")
        assert routing.plan_user_turn_mirrors(conn, Config(), room.canonical, "web") == [
            routing.UserTurnMirror("talk", room.talk_ref, "as_user"),
        ]


@pytest.mark.parametrize("surface", ["sms", "whatsapp"])
def test_phone_bindings_are_not_user_turn_mirror_targets(db_path, surface):
    with db.get_db(db_path) as conn:
        room = promoted_room(conn, "alice")
        db.add_room_binding(conn, room.canonical, surface, surface + "-alice-thread")
        assert routing.plan_user_turn_mirrors(conn, Config(), room.canonical, "web") == [
            routing.UserTurnMirror("talk", room.talk_ref, "as_user"),
        ]


@pytest.mark.parametrize("surface", ["sms", "whatsapp"])
def test_phone_view_alone_cannot_enable_user_turn_mirroring(db_path, monkeypatch, surface):
    monkeypatch.setitem(surfaces.SURFACES, surface, replace(
        surfaces.SURFACES[surface], room_view="external",
    ))
    with db.get_db(db_path) as conn:
        room = promoted_room(conn, "alice")
        db.add_room_binding(conn, room.canonical, surface, surface + "-alice-thread")
        assert routing.plan_user_turn_mirrors(conn, Config(), room.canonical, "web") == [
            routing.UserTurnMirror("talk", room.talk_ref, "as_user"),
        ]
