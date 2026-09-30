"""A guest in the room withholds every scope; the answer goes to the side room.

Multiplayer Stage 13 (D3 `mixed`, D4 item 1). Four things, each a boundary
rather than advice:

1. **Reach, not advice, under a `mixed` audience.** A member's grants are
   consent to disclose to the room's *members*. Once a guest is present they
   are ignored for front-stage output: the member's own turn runs at room-safe
   reach at every seam, whatever they granted. Control: the same grants with
   the guest gone reach again.
2. **The side-room answer.** A front-stage task that needs a withheld scope
   queues the principal's own question in their side room, where it runs as a
   private task. The public task never gains the scope.
3. **The restricted task's own directories.** A restricted task binds neither
   the per-user temp dir, which every task of that user shares, nor the flat
   Talk attachments dir, which holds every conversation's attachments. It gets
   a directory of its own and a copy of its own attachments.
4. **`files` and `memory` are independent.** Granting `files` and not `memory`
   masks the memory directories inside the workspace bind, and a skill CLI's
   host paths refuse them.

The default suite reads argv; the smoke witness for the changed mounts is owed
by Stage 28 (`tests/smoke/test_sandbox_shared_room.py`).
"""

from __future__ import annotations

import pytest

from istota import db, side_rooms
from istota.whatsapp_requests import RequestError

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

ALL_GRANTS = ("files", "memory", "calendar", "health")
SENTINEL = "alice-private-memory-sentinel"


def _guest(config, token: str) -> int:
    with db.get_db(config.db_path) as conn:
        return db.upsert_room_participant(
            conn, room_token=token, surface="talk", surface_ref="guests/max",
            kind="guest", display_name="Max",
        )


def _guest_leaves(config, token: str) -> None:
    with db.get_db(config.db_path) as conn:
        conn.execute(
            "UPDATE room_participants SET left_at = datetime('now') "
            "WHERE room_token = ? AND kind = 'guest'", (token,),
        )


def _user_md(config) -> None:
    path = (config.workspace_path / "Users" / "alice" / config.bot_dir_name
            / "config" / "USER.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# Memory\n\n{SENTINEL}\n", encoding="utf-8")


def _assert_no_private_reach(seen, config) -> None:
    assert "calendar" in seen["disabled"]
    assert "calendar" not in seen["allowed_skills"]
    assert "HEALTH_DB_PATH" not in seen["proxy_base_env"]
    assert "HEALTH_DB_PATH" not in seen["model_env"]
    assert _user_dir(config) not in _binds(seen["argv"])
    assert SENTINEL not in seen["prompt"]
    assert seen["vault"] == {}


class TestAGuestPresentWithholdsEveryGrant:
    """Reach, not advice: every seam, on the member's own turn."""

    def test_a_members_grants_do_not_reach_a_room_a_guest_reads(self, config):
        _user_md(config)
        token = _room(config, shared=True, grants=ALL_GRANTS)
        _guest(config, token)
        _assert_no_private_reach(_run(config, token), config)

    def test_a_guests_turn_reaches_nothing_the_host_granted(self, config):
        _user_md(config)
        token = _room(config, shared=True, grants=ALL_GRANTS)
        _guest(config, token)
        _assert_no_private_reach(_run(config, token, guest=True), config)

    def test_a_turn_stored_as_mixed_stays_withheld_after_the_guest_left(self, config):
        # Either signal withholds: the audience the turn was written for, or
        # the audience that reads the answer.
        token = _room(config, shared=True, grants=ALL_GRANTS)
        _guest(config, token)
        _guest_leaves(config, token)
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE tasks SET audience = 'mixed'")
        from istota import room_scopes

        with db.get_db(config.db_path) as conn:
            withheld = room_scopes.task_withheld_scopes(
                conn, policy="restrict", conversation_token=token, user_id="alice",
                skill_index={"calendar": object()}, assume_mixed=True,
            )
        assert {"calendar", "files", "memory"} <= withheld

    def test_control_the_grants_reach_again_once_the_guest_has_left(self, config):
        _user_md(config)
        token = _room(config, shared=True, grants=ALL_GRANTS)
        _guest(config, token)
        _guest_leaves(config, token)
        seen = _run(config, token)
        assert "calendar" in seen["allowed_skills"]
        assert seen["proxy_base_env"].get("HEALTH_DB_PATH") == HEALTH_DB
        assert _user_dir(config) in _binds(seen["argv"])
        assert SENTINEL in seen["prompt"]


class TestTheCardAndTheGrantCommandSaySo:
    def test_the_card_offers_no_grant_while_a_guest_reads_the_room(self, config):
        from istota.executor import room_card

        token = _room(config, shared=True, grants=("calendar",))
        _guest(config, token)
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, _running(conn, "alice", token))
        card = room_card(
            config, task, withheld_scopes=frozenset({"calendar", "files"}),
            room_cli_available=True,
        )
        assert "!room share" not in card
        assert "whatever 'alice' has granted" in card
        assert "istota-skill room answer-privately" in card
        # Without the room CLI the verb is not named.
        assert "answer-privately" not in room_card(
            config, task, withheld_scopes=frozenset({"calendar"}),
            room_cli_available=False,
        )

    def test_control_without_a_guest_the_grant_command_is_offered(self, config):
        from istota.executor import room_card

        token = _room(config, shared=True)
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, _running(conn, "alice", token))
        card = room_card(config, task, withheld_scopes=frozenset({"calendar"}),
                         room_cli_available=True)
        assert "`!room share <scope>`" in card
        assert "istota-skill room answer-privately" in card

    def test_room_share_says_grants_are_ignored_while_a_guest_is_present(self, config):
        token = _room(config, shared=True)
        _guest(config, token)
        import asyncio

        from istota import commands

        with db.get_db(config.db_path) as conn:
            reply = asyncio.run(commands.dispatch(
                config, "alice", token, "!room share calendar", surface="web", conn=conn,
            ))
            listing = asyncio.run(commands.dispatch(
                config, "alice", token, "!room share", surface="web", conn=conn,
            ))
        assert "guest" in reply.text and "ignored" in reply.text
        assert "guest" in listing.text and "ignored" in listing.text


# ---------------------------------------------------------------------------
# The side-room answer (D4 item 1)
# ---------------------------------------------------------------------------


def _running(conn, user, token, *, prompt="what's on my calendar tomorrow?", **kw):
    ident = db.create_task(conn, user_id=user, source_type=kw.pop("source_type", "talk"),
                           prompt=prompt, conversation_token=token, **kw)
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
    return ident


class TestTheSideRoomAnswer:
    def test_the_principals_own_question_runs_in_their_side_room(self, config):
        token = _room(config, shared=True)
        with db.get_db(config.db_path) as conn:
            origin = _running(conn, "alice", token)
            result = side_rooms.queue_side_answer(
                conn, config, actor_user_id="alice", task_id=origin)
            again = side_rooms.queue_side_answer(
                conn, config, actor_user_id="alice", task_id=origin)
            side = db.get_side_room(conn, token, "alice")
            task = db.get_task(conn, result["task_id"])
            rows = conn.execute(
                "SELECT role, body, author_user_id, task_id FROM messages "
                "WHERE room_token = ?", (side.token,)).fetchall()
        assert result["status"] == "queued" and again["task_id"] == result["task_id"]
        assert task.conversation_token == side.token
        assert task.user_id == "alice" and task.status == "pending"
        assert task.prompt == "what's on my calendar tomorrow?"
        assert task.parent_task_id is None and task.guest_participant_id is None
        assert [(r["role"], r["body"], r["author_user_id"], r["task_id"]) for r in rows] == [
            ("user", "what's on my calendar tomorrow?", "alice", task.id)]

    def test_the_side_room_task_runs_at_the_principals_full_reach(self, config):
        token = _room(config, shared=True)
        _guest(config, token)
        with db.get_db(config.db_path) as conn:
            origin = _running(conn, "alice", token)
            new = side_rooms.queue_side_answer(
                conn, config, actor_user_id="alice", task_id=origin)["task_id"]
            conn.execute("UPDATE tasks SET status='pending'")
        from istota.executor import _task_withheld_scopes

        with db.get_db(config.db_path) as conn:
            front = _task_withheld_scopes(
                config, conn, db.get_task(conn, origin), {"calendar": object()})
            back = _task_withheld_scopes(
                config, conn, db.get_task(conn, new), {"calendar": object()})
        assert "calendar" in front
        assert back == frozenset()

    def test_a_guests_turn_cannot_start_a_full_reach_task(self, config):
        token = _room(config, shared=True)
        pid = _guest(config, token)
        with db.get_db(config.db_path) as conn:
            origin = _running(conn, "alice", token, guest_participant_id=pid)
            with pytest.raises(RequestError, match="guest_turn"):
                side_rooms.queue_side_answer(
                    conn, config, actor_user_id="alice", task_id=origin)

    def test_refused_outside_a_shared_room_and_from_a_background_task(self, config):
        private = _room(config, shared=False)
        shared = _room(config, shared=True)
        with db.get_db(config.db_path) as conn:
            ident = _running(conn, "alice", private)
            with pytest.raises(RequestError, match="not_a_shared_room"):
                side_rooms.queue_side_answer(conn, config, actor_user_id="alice", task_id=ident)
            ident = _running(conn, "alice", shared, source_type="scheduled")
            with pytest.raises(RequestError, match="unsupported_origin"):
                side_rooms.queue_side_answer(conn, config, actor_user_id="alice", task_id=ident)
            ident = _running(conn, "bob", shared)
            with pytest.raises(RequestError, match="task_unavailable"):
                side_rooms.queue_side_answer(conn, config, actor_user_id="alice", task_id=ident)

    def test_its_answer_is_pinned_and_marked_for_the_talk_view(self, config):
        token = _room(config, shared=True)
        with db.get_db(config.db_path) as conn:
            origin = _running(conn, "alice", token)
            new = side_rooms.queue_side_answer(
                conn, config, actor_user_id="alice", task_id=origin)["task_id"]
            assert side_rooms.side_answer_parent(conn, db.get_task(conn, new)) == token
            assert side_rooms.side_answer_parent(conn, db.get_task(conn, origin)) is None


# ---------------------------------------------------------------------------
# The restricted task's own directories
# ---------------------------------------------------------------------------


def _rw(argv):
    return [argv[i + 1] for i, tok in enumerate(argv) if tok == "--bind"]


class TestARestrictedTasksOwnDirectories:
    def test_the_per_user_temp_dir_is_not_bound_only_its_own(self, config):
        seen = _run(config, _room(config, shared=True))
        user_dir = str((config.temp_dir / "alice").resolve())
        rw = _rw(seen["argv"])
        assert user_dir not in rw
        own = [p for p in rw if p.startswith(user_dir + "/room-task-")]
        assert len(own) == 1
        assert seen["model_env"]["ISTOTA_DEFERRED_DIR"] == own[0]

    def test_the_scheduler_reads_its_deferred_ops_from_there(self, config):
        from istota.executor import task_deferred_dir

        _run(config, _room(config, shared=True))
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, 1)
        assert task_deferred_dir(config, task).name == "room-task-1"

    def test_control_a_private_task_binds_the_per_user_dir(self, config):
        from istota.executor import task_deferred_dir

        seen = _run(config, _room(config, shared=False))
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
        seen = _run(config, token, attachments=[str(own)])
        binds = _binds(seen["argv"])
        assert str(talk.resolve()) not in binds
        assert str(own) not in seen["prompt"]
        copied = [p for p in _rw(seen["argv"]) if "/room-task-" in p]
        assert copied and (config.temp_dir / "alice" / "room-task-1" / "attachments"
                           / "ticket.pdf").read_bytes() == b"%PDF-1.4 ticket"

    def test_control_a_private_task_binds_the_talk_dir(self, config):
        (config.workspace_path / "Talk").mkdir()
        seen = _run(config, _room(config, shared=False))
        assert str((config.workspace_path / "Talk").resolve()) in _binds(seen["argv"])


# ---------------------------------------------------------------------------
# files and memory are independent
# ---------------------------------------------------------------------------


def _masks(argv):
    return [argv[i + 1] for i, tok in enumerate(argv) if tok == "--tmpfs"]


class TestFilesWithoutMemory:
    def _memory_dirs(self, config):
        base = (config.workspace_path / "Users" / "alice").resolve()
        dirs = [base / "memories", base / config.bot_dir_name / "config",
                base / config.bot_dir_name / "playbooks"]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
        return [str(d) for d in dirs]

    def test_files_granted_alone_masks_the_memory_directories(self, config):
        dirs = self._memory_dirs(config)
        seen = _run(config, _room(config, shared=True, grants=("files",)))
        assert _user_dir(config) in _binds(seen["argv"])
        masks = _masks(seen["argv"])
        for d in dirs:
            assert d in masks

    def test_control_both_granted_masks_nothing_of_the_workspace(self, config):
        dirs = self._memory_dirs(config)
        seen = _run(config, _room(config, shared=True, grants=("files", "memory")))
        masks = _masks(seen["argv"])
        assert not [d for d in dirs if d in masks]

    def test_a_skill_cli_host_path_refuses_memory_when_it_is_withheld(
        self, tmp_path, monkeypatch,
    ):
        from istota.skill_host_paths import WITHHELD_SCOPES_VAR, resolve_host_path

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
