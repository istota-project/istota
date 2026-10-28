"""A group-owned room leaves personal memory out at every headcount.

Driven through real web-turn ingestion (`transport.ingest.record_inbound`) and
a real `execute_task`, with the brain replaced by one that answers with the
whole prompt it was given, so the stored answer is the worst case of a model
repeating everything it saw. Distinct canaries sit in `USER.md`, personal
recall, the knowledge graph, playbooks, the linked group's notebook and a
second group's notebook. A one-member group room and a room that shrinks to
one member must carry the linked group's canary and none of the others; an
ordinary private room is the control that still loads personal memory.

Recall and playbooks are replaced at their executor seams, which is where
`skip_memory` reaches them: both are off by default and need an index. The
knowledge graph is the real table.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from istota import db, executor, storage
from istota.brain import BrainResult
from tests import test_shared_room_restriction as harness
from tests.test_shared_room_restriction import _binds

config = harness.config
_bwrap_flag_cache = harness._bwrap_flag_cache

USER_MD = "usermd-canary-heron"
RECALL = "recall-canary-otter"
KG = "kgcanaryfalcon"
PLAYBOOK = "playbook-canary-badger"
FAM_NOTEBOOK = "fam-notebook-canary-lynx"
CLUB_NOTEBOOK = "club-notebook-canary-wren"
PERSONAL = (USER_MD, RECALL, KG, PLAYBOOK)


def _echo_brain(captured: list):
    from istota.brain import make_brain

    def _make(brain_config):
        brain = make_brain(brain_config)

        def _execute(req):
            captured.append(req)
            return BrainResult(success=True, result_text=req.prompt)

        brain.execute = _execute
        return brain

    return _make


def _fake_recall(*_args, skip_memory=False, **_kwargs):
    return None if skip_memory else f"## Recalled memories\n\n- {RECALL}\n"


def _fake_playbooks(*_args, skip_memory=False, **_kwargs):
    return None if skip_memory else f"## Learned Playbooks\n\n- {PLAYBOOK}\n"


def _write_user_md(config, user):
    path = (config.workspace_path / "Users" / user / config.bot_dir_name
            / "config" / "USER.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# Memory\n\n- {USER_MD}-{user}\n", encoding="utf-8")


def _notebook(config, group_id, canary):
    storage.ensure_group_directories(config, group_id, display_name=group_id)
    path = config.workspace_path / "Groups" / group_id / "GROUP.md"
    path.write_text(path.read_text() + f"\n- {canary}\n")


@pytest.fixture
def world(config):
    (config.temp_dir / "bob").mkdir(parents=True, exist_ok=True)
    with db.get_db(config.db_path) as conn:
        db.create_group(conn, "fam", kind="family", display_name="fam",
                        created_by="operator")
        db.add_group_member(conn, "fam", "alice", role="owner", added_by="operator")
        db.create_group(conn, "club", kind="team", display_name="club",
                        created_by="operator")
        db.add_group_member(conn, "club", "alice", added_by="operator")
        from istota.memory.knowledge_graph import add_fact, ensure_table
        ensure_table(conn)
        add_fact(conn, "alice", "alice", "nickname", KG)
    _notebook(config, "fam", FAM_NOTEBOOK)
    _notebook(config, "club", CLUB_NOTEBOOK)
    _write_user_md(config, "alice")
    _write_user_md(config, "bob")
    return config


def _room(config, *, managed: bool) -> str:
    with db.get_db(config.db_path) as conn:
        token = db.create_web_chat_room(conn, "alice", "Planning").token
        db.set_room_group(conn, token, "fam")
        if managed:
            db.set_room_membership_source(conn, token, "group")
    return token


def _join(config, token, user):
    with db.get_db(config.db_path) as conn:
        db.add_group_member(conn, "fam", user, added_by="alice")
        db.add_web_room_member(conn, token, user)


def _leave(config, token, user):
    with db.get_db(config.db_path) as conn:
        db.end_group_membership(conn, "fam", user, ended_by="alice")
        db.drop_web_room_member(conn, token, user)


def _turn(config, token, user, text) -> dict:
    """One web turn, ingested and executed; the answer is stored as the
    scheduler stores it, so a later reader sees it in the transcript."""
    from istota.transport.ingest import record_inbound

    captured: list = []
    with patch("istota.executor.make_brain", _echo_brain(captured)), \
            patch("istota.executor._bwrap_available", return_value=True), \
            patch("istota.sandbox.skill_proxy.SkillProxy") as proxy, \
            patch.object(executor, "_recall_memories", _fake_recall), \
            patch.object(executor, "_recall_playbooks", _fake_playbooks):
        proxy.return_value.__enter__ = lambda s: s
        proxy.return_value.__exit__ = lambda s, *a: False
        with db.get_db(config.db_path) as conn:
            result = record_inbound(
                conn, config, surface="web", surface_ref=token, user_id=user,
                text=text, source_type="web", output_target="room",
                addressed_to_bot=True,
            )
            assert result.task_id, result
            task = db.get_task(conn, result.task_id)
            ambient_off = executor._ambient_memory_off(config, task, conn)
            success, answer, *_ = executor.execute_task(task, config, [], conn=conn)
            assert success, answer
            db.update_task_status(conn, task.id, "completed", result=answer)
            db.add_message(conn, token, role="assistant", body=answer,
                           origin_surface="web", task_id=task.id)
        assert captured, "the brain was never called"
        req = captured[0]
        argv = req.tool_server_wrap(["claude", "-p"])
    return {
        "task": task, "ambient_off": ambient_off, "prompt": req.prompt,
        "argv": argv, "allowed_skills": set(proxy.call_args.kwargs["allowed_skills"]),
    }


def _transcript(config, token) -> str:
    with db.get_db(config.db_path) as conn:
        rows = conn.execute(
            "SELECT body FROM messages WHERE room_token = ? ORDER BY id", (token,),
        ).fetchall()
    return "\n".join(r["body"] for r in rows)


def _assert_group_only(seen):
    assert seen["ambient_off"] is True
    assert FAM_NOTEBOOK in seen["prompt"]
    assert CLUB_NOTEBOOK not in seen["prompt"]
    for canary in PERSONAL:
        assert canary not in seen["prompt"], canary


class TestAOneOwnerGroupRoom:
    def test_only_the_linked_notebook_reaches_the_prompt(self, world):
        token = _room(world, managed=True)
        seen = _turn(world, token, "alice", "what should we plan for the weekend?")
        assert seen["task"].is_group_chat is False
        _assert_group_only(seen)

    def test_the_owner_keeps_their_deliberate_reach(self, world):
        token = _room(world, managed=True)
        seen = _turn(world, token, "alice", "what's on my calendar tomorrow?")
        user_dir = str((world.workspace_path / "Users" / "alice").resolve())
        assert user_dir in _binds(seen["argv"])
        assert "calendar" in seen["allowed_skills"]
        assert "memory" in seen["allowed_skills"]

    def test_a_joiner_reads_history_with_nothing_personal_in_it(self, world):
        token = _room(world, managed=True)
        _turn(world, token, "alice", "what should we plan for the weekend?")
        _join(world, token, "bob")
        history = _transcript(world, token)
        assert "plan for the weekend" in history
        for canary in PERSONAL:
            assert canary not in history, canary
        seen = _turn(world, token, "bob", "what did we say about the weekend?")
        assert "plan for the weekend" in seen["prompt"]
        assert f"{USER_MD}-bob" not in seen["prompt"]
        for canary in PERSONAL:
            assert canary not in seen["prompt"], canary


class TestARoomThatShrinksToOne:
    def test_it_stays_group_only_while_one_and_after_growing(self, world):
        token = _room(world, managed=True)
        _join(world, token, "bob")
        _assert_group_only(_turn(world, token, "alice", "two of us"))
        _leave(world, token, "bob")
        with db.get_db(world.db_path) as conn:
            assert not db.room_is_shared(conn, token)
        _assert_group_only(_turn(world, token, "alice", "just me now"))
        _join(world, token, "bob")
        _assert_group_only(_turn(world, token, "alice", "bob is back"))
        history = _transcript(world, token)
        for canary in PERSONAL:
            assert canary not in history, canary


class TestControlAnOrdinaryPrivateRoom:
    def test_personal_memory_still_loads(self, world):
        token = _room(world, managed=False)
        seen = _turn(world, token, "alice", "what should we plan for the weekend?")
        assert seen["ambient_off"] is False
        for canary in PERSONAL:
            assert canary in seen["prompt"], canary
        assert FAM_NOTEBOOK in seen["prompt"]
        assert CLUB_NOTEBOOK not in seen["prompt"]
