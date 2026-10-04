"""Email on rooms, section 2a: a correspondent's turn on an email thread room
runs as the host at full reach, with the host's ambient memory loaded.

Driven through `execute_task`, the seams' one caller, with the harness
`test_shared_room_restriction.py` uses, so every seam is read off what the
brain and the skill proxy were handed. A Talk guest's turn in the same
harness is the control: it still withholds everything.
"""

from __future__ import annotations

from unittest.mock import patch

from istota import db

from .test_shared_room_restriction import HEALTH_DB, _binds, _capturing_brain, _user_dir
from . import test_shared_room_restriction as _base

config = _base.config
_bwrap_flag_cache = _base._bwrap_flag_cache

SENTINEL = "alice-private-memory-sentinel"
CORRESPONDENT = "carol@example.com"


def _user_md(config) -> None:
    path = (config.workspace_path / "Users" / "alice" / config.bot_dir_name
            / "config" / "USER.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# Memory\n\n{SENTINEL}\n", encoding="utf-8")


def _thread_turn(conn) -> int:
    """A thread room as the poller leaves it, and a correspondent's turn on it:
    stored under their label, run as the host, marked a group chat."""
    room = db.register_bound_room(conn, "alice", origin="email", name="Dinner",
                                  surface="email", surface_ref="<root@example.com>")
    participant = db.upsert_room_participant(
        conn, room_token=room.token, surface="email", surface_ref=CORRESPONDENT,
        kind="guest",
    )
    task_id = db.create_task(
        conn, prompt="<email_content>\nwhat is on Thursday?\n</email_content>",
        user_id="alice", source_type="email", conversation_token=room.token,
        is_group_chat=True, audience="mixed", host_absent=True,
    )
    db.add_message(conn, room.token, role="user", body="what is on Thursday?",
                   origin_surface="email", task_id=task_id, author_label=CORRESPONDENT,
                   author_participant_id=participant)
    return task_id


def _talk_guest_turn(conn) -> int:
    room = db.create_web_chat_room(conn, "alice", "Family")
    db.add_room_member(conn, room.token, "bob")
    task_id = db.create_task(conn, prompt="what is on Thursday?", user_id="alice",
                             source_type="web", conversation_token=room.token)
    conn.execute("UPDATE tasks SET guest_participant_id = 1 WHERE id = ?", (task_id,))
    return task_id


def _run(config, make_turn) -> dict:
    from istota.skills import _loader

    captured: list = []
    disabled_seen: list[set[str]] = []
    real_disabled = _loader.effective_disabled_skills

    def _spy_disabled(*args, **kwargs):
        result = real_disabled(*args, **kwargs)
        disabled_seen.append(set(result))
        return result

    with patch("istota.executor.make_brain", _capturing_brain(captured)), \
            patch("istota.executor._bwrap_available", return_value=True), \
            patch("istota.sandbox.skill_proxy.SkillProxy") as mock_proxy, \
            patch.object(_loader, "effective_disabled_skills", _spy_disabled), \
            patch("istota.skills.health.setup_env", lambda ctx: {"HEALTH_DB_PATH": HEALTH_DB}), \
            patch("istota.sandbox.task_env._vault_credentials", lambda *a: {"bank": "s3cret"}):
        mock_proxy.return_value.__enter__ = lambda s: s
        mock_proxy.return_value.__exit__ = lambda s, *a: False
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, make_turn(conn))
            from istota.executor import execute_task
            outcome = execute_task(task, config, [], conn=conn)
        assert captured, f"the brain was never called: {outcome!r}"
        req = captured[0]
        argv = req.sandbox_wrap(["claude", "-p"])
    args, kwargs = mock_proxy.call_args
    return {
        "disabled": disabled_seen[0],
        "allowed_skills": set(kwargs["allowed_skills"]),
        "proxy_base_env": args[2],
        "argv": argv,
        "system": open(req.composed_system_prompt_path).read(),
        "prompt": req.prompt,
        "vault": kwargs["vault_credentials"],
    }


class TestACorrespondentsTurn:
    def test_it_reaches_everything_the_host_can(self, config):
        seen = _run(config, _thread_turn)
        assert "calendar" not in seen["disabled"]
        assert "calendar" in seen["allowed_skills"]
        assert seen["proxy_base_env"].get("HEALTH_DB_PATH") == HEALTH_DB
        assert _user_dir(config) in _binds(seen["argv"])
        assert seen["vault"] == {"bank": "s3cret"}

    def test_the_hosts_user_md_is_in_the_prompt(self, config):
        _user_md(config)
        seen = _run(config, _thread_turn)
        assert SENTINEL in seen["prompt"] + seen["system"]

    def test_the_card_is_the_email_one(self, config):
        seen = _run(config, _thread_turn)
        assert "on their correspondence" in seen["system"]
        assert "answer `NO_ACTION:`" in seen["system"]
        assert "Withheld from this turn" not in seen["system"]


class TestControlATalkGuestsTurn:
    def test_it_still_withholds_everything_and_loads_no_memory(self, config):
        _user_md(config)
        seen = _run(config, _talk_guest_turn)
        assert "calendar" in seen["disabled"]
        assert "HEALTH_DB_PATH" not in seen["proxy_base_env"]
        assert _user_dir(config) not in _binds(seen["argv"])
        assert SENTINEL not in seen["prompt"] + seen["system"]
        assert seen["vault"] == {}
