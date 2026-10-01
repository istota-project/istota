"""ISSUE-581 — a bind that did not land has to be visible to the caller.

`add_room_binding` is `INSERT OR IGNORE`, and the UNIQUE index on
`room_bindings (surface, surface_ref)` means the ignore also swallows a bind of a
ref another room already holds. A caller that minted a room and bound it without
resolving first got a room with no binding and no error.
"""

import logging

import pytest

from istota import db


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    with db.get_db(path) as c:
        yield c


def _room(conn, user="alice"):
    return db.register_room(conn, None, user, origin="talk").token


def test_a_fresh_bind_reports_it_landed(conn):
    room = _room(conn)
    assert db.add_room_binding(conn, room, "talk", "conv-1") is True
    assert db.resolve_room_token(conn, "talk", "conv-1") == room


def test_repeating_the_rooms_own_binding_is_still_success(conn):
    room = _room(conn)
    db.add_room_binding(conn, room, "talk", "conv-1")
    assert db.add_room_binding(conn, room, "talk", "conv-1") is True


def test_binding_a_ref_another_room_holds_is_refused_visibly(conn, caplog):
    holder = _room(conn)
    db.add_room_binding(conn, holder, "talk", "conv-1")
    second = _room(conn, "bob")

    with caplog.at_level(logging.WARNING, logger="istota.db"):
        landed = db.add_room_binding(conn, second, "talk", "conv-1")

    assert landed is False
    assert db.resolve_room_token(conn, "talk", "conv-1") == holder
    assert db.get_room_binding(conn, second, "talk") is None
    assert any("held by another room" in r.getMessage() for r in caplog.records)
    # The ref itself is surface-native data (a group JID, a Message-ID) and stays
    # out of the log line.
    assert not any("conv-1" in r.getMessage() for r in caplog.records)


def test_a_room_already_bound_elsewhere_on_that_surface_is_refused_quietly(conn, caplog):
    """A migrated room keeps its legacy web ref and is self-bound on every room
    list and web send, so this case must not warn."""
    room = _room(conn)
    db.add_room_binding(conn, room, "talk", "conv-1")
    with caplog.at_level(logging.WARNING, logger="istota.db"):
        assert db.add_room_binding(conn, room, "talk", "conv-2") is False
    assert caplog.records == []
    assert db.resolve_room_token(conn, "talk", "conv-1") == room
    assert db.resolve_room_token(conn, "talk", "conv-2") is None


def test_the_same_ref_on_another_surface_does_not_conflict(conn):
    holder = _room(conn)
    db.add_room_binding(conn, holder, "talk", "shared-ref")
    other = _room(conn, "bob")
    assert db.add_room_binding(conn, other, "web", "shared-ref") is True


def test_register_bound_room_mints_and_binds(conn):
    room = db.register_bound_room(
        conn, "alice", origin="talk", name="weekly",
        surface="talk", surface_ref="conv-1",
    )
    assert room is not None
    assert db.resolve_room_token(conn, "talk", "conv-1") == room.token
    assert db.is_room_member(conn, room.token, "alice")


def test_register_bound_room_leaves_no_unbound_room_behind(conn):
    holder = _room(conn)
    db.add_room_binding(conn, holder, "talk", "conv-1")
    before = conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]

    room = db.register_bound_room(
        conn, "bob", origin="talk", name="dup",
        surface="talk", surface_ref="conv-1",
    )

    assert room is None
    assert conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == before
    assert conn.execute(
        "SELECT COUNT(*) FROM room_members WHERE user_id = 'bob'"
    ).fetchone()[0] == 0
    assert db.resolve_room_token(conn, "talk", "conv-1") == holder
