"""A guest in the room: who reaches what, and the private answer.

Multiplayer Stage 13 (D3 `mixed`, D4 item 1), as ISSUE-576 left it:

1. **A member's turn runs at full reach with a guest present.** The boundary
   is the member's own choices: having the guest there, and asking in front of
   them. What it loses is ambient memory, which no shared room loads.
2. **A guest's turn reaches nothing of the host's**, at every seam.
3. **The private answer.** A member's question can be asked again in their
   own private room (ISSUE-608), where it runs as a private task with their
   memory loaded, linked to the shared room.
4. **A guest's turn gets its own directories.** It binds neither the per-user
   temp dir, which every task of the host shares, nor the flat Talk
   attachments dir, which holds every conversation's attachments.

The default suite reads argv; the smoke witness for the mounts is
`tests/smoke/test_sandbox_shared_room.py`.
"""

from __future__ import annotations

import pytest

from istota import db
from istota.rooms import private_replies
from istota.relay.requests import RequestError

from .test_shared_room_restriction import (
    HEALTH_DB,
    _binds,
    _room,
    _run,
    _user_dir,
)
from . import test_shared_room_restriction as _base

# The same two fixtures, bound here so pytest finds them in this module.
config = _base.config
_bwrap_flag_cache = _base._bwrap_flag_cache

SENTINEL = "alice-private-memory-sentinel"


def _guest(config, token: str) -> int:
    with db.get_db(config.db_path) as conn:
        return db.upsert_room_participant(
            conn, room_token=token, surface="talk", surface_ref="guests/max",
            kind="guest", display_name="Max",
        )


def _user_md(config) -> None:
    path = (config.workspace_path / "Users" / "alice" / config.bot_dir_name
            / "config" / "USER.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# Memory\n\n{SENTINEL}\n", encoding="utf-8")


class TestAGuestInTheRoom:
    def test_a_members_turn_still_reaches_everything_but_ambient_memory(self, config):
        _user_md(config)
        token = _room(config, shared=True)
        _guest(config, token)
        seen = _run(config, token)
        assert "calendar" in seen["allowed_skills"]
        assert seen["proxy_base_env"].get("HEALTH_DB_PATH") == HEALTH_DB
        assert _user_dir(config) in _binds(seen["argv"])
        assert seen["vault"] == {"bank": "s3cret"}
        assert SENTINEL not in seen["prompt"]

    def test_a_guests_turn_reaches_nothing_of_the_hosts(self, config):
        _user_md(config)
        token = _room(config, shared=True)
        _guest(config, token)
        seen = _run(config, token, guest=True)
        assert "calendar" in seen["disabled"]
        assert "calendar" not in seen["allowed_skills"]
        assert "HEALTH_DB_PATH" not in seen["proxy_base_env"]
        assert "HEALTH_DB_PATH" not in seen["model_env"]
        assert _user_dir(config) not in _binds(seen["argv"])
        assert SENTINEL not in seen["prompt"]
        assert seen["vault"] == {}


class TestTheCard:
    def test_the_card_says_a_guest_reads_the_room(self, config):
        from istota.executor import room_card

        token = _room(config, shared=True)
        _guest(config, token)
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, _running(conn, "alice", token))
        card = room_card(
            config, task, withheld_scopes=frozenset(), room_cli_available=True,
        )
        assert "A guest reads this room." in card
        assert "everything 'alice' can reach" in card
        assert "!room share" not in card
        assert "istota-skill room answer-privately" in card
        # Without the room CLI the verb is not named.
        assert "answer-privately" not in room_card(
            config, task, withheld_scopes=frozenset(), room_cli_available=False,
        )

    def test_control_without_a_guest_it_says_nothing_about_one(self, config):
        from istota.executor import room_card

        token = _room(config, shared=True)
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, _running(conn, "alice", token))
        card = room_card(config, task, withheld_scopes=frozenset(),
                         room_cli_available=True)
        # The room's standing rule describes guests on every card (ISSUE-602);
        # what a guest-free room must not carry is the claim that one reads it.
        assert "A guest reads this room." not in card
        assert "A guest's turn" in card
        assert "personal memory is not loaded" in card


# ---------------------------------------------------------------------------
# The private answer (D4 item 1)
# ---------------------------------------------------------------------------


def _running(conn, user, token, *, prompt="what's on my calendar tomorrow?", **kw):
    ident = db.create_task(conn, user_id=user, source_type=kw.pop("source_type", "talk"),
                           prompt=prompt, conversation_token=token, **kw)
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
    return ident


class TestThePrivateAnswer:
    def test_the_principals_own_question_runs_in_their_private_room(self, config):
        token = _room(config, shared=True)
        private = _room(config, shared=False)
        with db.get_db(config.db_path) as conn:
            origin = _running(conn, "alice", token)
            rooms_before = conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
            result = private_replies.queue_private_answer(
                conn, config, actor_user_id="alice", task_id=origin)
            again = private_replies.queue_private_answer(
                conn, config, actor_user_id="alice", task_id=origin)
            task = db.get_task(conn, result["task_id"])
            rows = conn.execute(
                "SELECT role, body, author_user_id, task_id, delivery_reference FROM messages "
                "WHERE room_token = ?", (private,)).fetchall()
            rooms_after = conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
        assert result["status"] == "queued" and again["task_id"] == result["task_id"]
        assert rooms_after == rooms_before
        assert task.conversation_token == private
        assert task.about_room_token == token
        assert task.source_type == "web"
        assert task.user_id == "alice" and task.status == "pending"
        assert task.prompt == "what's on my calendar tomorrow?"
        assert task.parent_task_id is None and task.guest_participant_id is None
        assert [(r["role"], r["body"], r["author_user_id"], r["task_id"],
                 r["delivery_reference"]) for r in rows] == [
            ("user", "what's on my calendar tomorrow?", "alice", task.id,
             f"private-answer:{origin}")]

    def test_refused_when_the_principal_has_no_private_room(self, config):
        token = _room(config, shared=True)
        with db.get_db(config.db_path) as conn:
            origin = _running(conn, "alice", token)
            tasks_before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            with pytest.raises(RequestError, match="no_private_room"):
                private_replies.queue_private_answer(
                    conn, config, actor_user_id="alice", task_id=origin)
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == tasks_before

    def test_the_private_task_loads_the_principals_memory(self, config):
        token = _room(config, shared=True)
        _room(config, shared=False)
        _guest(config, token)
        with db.get_db(config.db_path) as conn:
            origin = _running(conn, "alice", token)
            new = private_replies.queue_private_answer(
                conn, config, actor_user_id="alice", task_id=origin)["task_id"]
            conn.execute("UPDATE tasks SET status='pending'")
        from istota.executor import _ambient_memory_off

        with db.get_db(config.db_path) as conn:
            front = _ambient_memory_off(config, db.get_task(conn, origin), conn)
            back = _ambient_memory_off(config, db.get_task(conn, new), conn)
        assert front is True
        assert back is False

    def test_a_guests_turn_cannot_start_a_full_reach_task(self, config):
        token = _room(config, shared=True)
        pid = _guest(config, token)
        with db.get_db(config.db_path) as conn:
            origin = _running(conn, "alice", token, guest_participant_id=pid)
            with pytest.raises(RequestError, match="guest_turn"):
                private_replies.queue_private_answer(
                    conn, config, actor_user_id="alice", task_id=origin)

    def test_refused_outside_a_shared_room_and_from_a_background_task(self, config):
        private = _room(config, shared=False)
        shared = _room(config, shared=True)
        with db.get_db(config.db_path) as conn:
            ident = _running(conn, "alice", private)
            with pytest.raises(RequestError, match="not_a_shared_room"):
                private_replies.queue_private_answer(conn, config, actor_user_id="alice", task_id=ident)
            ident = _running(conn, "alice", shared, source_type="scheduled")
            with pytest.raises(RequestError, match="unsupported_origin"):
                private_replies.queue_private_answer(conn, config, actor_user_id="alice", task_id=ident)
            ident = _running(conn, "bob", shared)
            with pytest.raises(RequestError, match="task_unavailable"):
                private_replies.queue_private_answer(conn, config, actor_user_id="alice", task_id=ident)

    def test_a_talk_departure_refuses_though_the_member_row_stays(self, config):
        token = _room(config, shared=True)
        _room(config, shared=False)
        with db.get_db(config.db_path) as conn:
            db.upsert_room_participant(conn, room_token=token, surface="talk",
                                       surface_ref="alice", kind="principal", user_id="alice")
            conn.execute("UPDATE room_participants SET left_at = datetime('now') "
                         "WHERE room_token = ? AND user_id = 'alice'", (token,))
            origin = _running(conn, "alice", token)
            assert db.is_room_member(conn, token, "alice")
            with pytest.raises(RequestError, match="not_a_shared_room"):
                private_replies.queue_private_answer(
                    conn, config, actor_user_id="alice", task_id=origin)

    def test_its_answer_never_reaches_the_shared_room(self, config):
        from istota.transport.registry import make_registry
        from istota.transport.routing import resolve_delivery_plan

        token = _room(config, shared=True)
        private = _room(config, shared=False)
        with db.get_db(config.db_path) as conn:
            origin = _running(conn, "alice", token)
            new = private_replies.queue_private_answer(
                conn, config, actor_user_id="alice", task_id=origin)["task_id"]
            task = db.get_task(conn, new)
        plan = resolve_delivery_plan(config, task, make_registry(config))
        assert plan
        assert all(d.channel != token for d in plan)
        assert task.conversation_token == private


# ---------------------------------------------------------------------------
# The restricted task's own directories
# ---------------------------------------------------------------------------


def _rw(argv):
    return [argv[i + 1] for i, tok in enumerate(argv) if tok == "--bind"]


class TestAGuestsTurnHasItsOwnDirectories:
    def test_the_per_user_temp_dir_is_not_bound_only_its_own(self, config):
        token = _room(config, shared=True)
        _guest(config, token)
        seen = _run(config, token, guest=True)
        user_dir = str((config.temp_dir / "alice").resolve())
        rw = _rw(seen["argv"])
        assert user_dir not in rw
        own = [p for p in rw if p.startswith(user_dir + "/emissary-task-")]
        assert len(own) == 1
        assert seen["model_env"]["ISTOTA_DEFERRED_DIR"] == own[0]

    def test_control_a_members_turn_binds_the_per_user_dir(self, config):
        from istota.executor import task_deferred_dir

        seen = _run(config, _room(config, shared=True))
        user_dir = str((config.temp_dir / "alice").resolve())
        assert user_dir in _rw(seen["argv"])
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, 1)
        assert task_deferred_dir(config, task) == config.temp_dir / "alice"

    def test_the_talk_dir_is_not_bound_and_its_own_attachment_is_copied_in(self, config):
        talk = config.workspace_path / "Talk"
        talk.mkdir()
        (talk / "other-room.pdf").write_bytes(b"not yours")
        own = talk / "ticket.pdf"
        own.write_bytes(b"%PDF-1.4 ticket")
        token = _room(config, shared=True)
        _guest(config, token)
        seen = _run(config, token, guest=True, attachments=[str(own)])
        binds = _binds(seen["argv"])
        assert str(talk.resolve()) not in binds
        assert str(own) not in seen["prompt"]
        staged = (config.temp_dir / ".control" / "alice" / "task_1"
                  / "room-attachments" / "ticket.pdf")
        assert staged.read_bytes() == b"%PDF-1.4 ticket"
        assert str(staged.resolve()) in seen["prompt"]

    def test_a_retry_of_a_task_an_earlier_release_restricted_writes_where_it_did(
        self, config,
    ):
        # Before ISSUE-576 a member's shared-room turn ran restricted in
        # `room-task-<id>`. A retry after the upgrade is unrestricted and must
        # still write where `task_deferred_dir` reads.
        from istota.executor import task_deferred_dir, task_temp_dir

        token = _room(config, shared=True)
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, _running(conn, "alice", token))
        legacy = config.temp_dir / "alice" / f"room-task-{task.id}"
        legacy.mkdir(parents=True)
        assert task_temp_dir(config, task, restricted=False) == legacy
        assert task_deferred_dir(config, task) == legacy

    def test_a_skill_cli_host_path_refuses_the_talk_dir_when_restricted(
        self, tmp_path, monkeypatch,
    ):
        from istota.sandbox.host_paths import WITHHELD_SCOPES_VAR, resolve_host_path

        mount = tmp_path / "mount"
        (mount / "Users" / "alice").mkdir(parents=True)
        (mount / "Talk").mkdir()
        other = mount / "Talk" / "other-room.ogg"
        other.write_bytes(b"not yours")
        monkeypatch.setenv("ISTOTA_WORKSPACE_PATH", str(mount))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv(WITHHELD_SCOPES_VAR, "calendar")
        refused, _ = resolve_host_path(other, writable=False, operation="read")
        assert refused is None
        monkeypatch.delenv(WITHHELD_SCOPES_VAR)
        ok, err = resolve_host_path(other, writable=False, operation="read")
        assert ok is not None, err

    def test_control_a_private_task_binds_the_talk_dir(self, config):
        (config.workspace_path / "Talk").mkdir()
        seen = _run(config, _room(config, shared=False))
        assert str((config.workspace_path / "Talk").resolve()) in _binds(seen["argv"])


# ---------------------------------------------------------------------------
# The memory directories under a withheld scope
# ---------------------------------------------------------------------------


class TestTheMemoryHostPathRefusal:
    def test_a_skill_cli_host_path_refuses_memory_when_it_is_withheld(
        self, tmp_path, monkeypatch,
    ):
        from istota.sandbox.host_paths import WITHHELD_SCOPES_VAR, resolve_host_path

        mount = tmp_path / "mount"
        memories = mount / "Users" / "alice" / "memories"
        memories.mkdir(parents=True)
        (memories / "2026-09-30.md").write_text("private")
        (mount / "Users" / "alice" / "notes.txt").write_text("fine")
        monkeypatch.setenv("ISTOTA_WORKSPACE_PATH", str(mount))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv(WITHHELD_SCOPES_VAR, "calendar,memory")
        refused, err = resolve_host_path(
            memories / "2026-09-30.md", writable=False, operation="read")
        assert refused is None and "memory" in err
        ok, err = resolve_host_path(
            mount / "Users" / "alice" / "notes.txt", writable=False, operation="read")
        assert ok is not None, err
        monkeypatch.setenv(WITHHELD_SCOPES_VAR, "calendar")
        ok, err = resolve_host_path(
            memories / "2026-09-30.md", writable=False, operation="read")
        assert ok is not None, err
