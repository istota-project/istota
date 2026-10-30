"""A correspondent's turn runs as the host and withholds all but the allowlist,
with the host's USER.md and overlays loaded and the rest of their memory not.

Driven through `execute_task`, the seams' one caller, with the harness
`test_shared_room_restriction.py` uses, so every seam is read off what the
brain and the skill proxy were handed. A Talk guest's turn in the same
harness is the control: it still withholds everything.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from istota import db
from istota.config import UserConfig
from istota.memory.knowledge_graph import add_fact, ensure_table

from .test_shared_room_restriction import HEALTH_DB, _binds, _capturing_brain, _user_dir
from . import test_shared_room_restriction as _base

config = _base.config
_bwrap_flag_cache = _base._bwrap_flag_cache

SENTINEL = "alice-private-memory-sentinel"
OVERLAY_SENTINEL = "alice-overlay-sentinel"
DATED_SENTINEL = "alice-dated-memory-sentinel"
FACT_SENTINEL = "alice-knowledge-fact-sentinel"
PIN_SENTINEL = "alice-pinned-file-sentinel"
CORRESPONDENT = "carol@example.com"


def _user_md(config) -> None:
    path = (config.workspace_path / "Users" / "alice" / config.bot_dir_name
            / "config" / "USER.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# Memory\n\n{SENTINEL}\n", encoding="utf-8")


def _thread_turn(conn, attachments=None) -> int:
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
        attachments=attachments,
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
        argv = req.tool_server_wrap(["claude", "-p"])
    args, kwargs = mock_proxy.call_args
    return {
        "disabled": disabled_seen[0],
        "allowed_skills": set(kwargs["allowed_skills"]),
        "proxy_base_env": args[2],
        "model_env": req.env,
        "argv": argv,
        "system": open(req.composed_system_prompt_path).read(),
        "prompt": req.prompt,
        "vault": kwargs["vault_credentials"],
        "allowed_tools": req.allowed_tools,
    }


def _roomless_turn(conn) -> int:
    """An approved held mail no room admitted: no token and no stored row."""
    task_id = db.create_task(conn, prompt="<email_content>\nhello\n</email_content>",
                             user_id="alice", source_type="email")
    db.mark_email_processed(conn, f"uid-{task_id}", CORRESPONDENT, user_id="alice",
                            task_id=task_id, routing_method="plus_address")
    return task_id


def _own_thread_turn(conn) -> int:
    room = db.register_bound_room(conn, "alice", origin="email", name="Dinner",
                                  surface="email", surface_ref="<own@example.com>")
    task_id = db.create_task(conn, prompt="what is on Thursday?", user_id="alice",
                             source_type="email", conversation_token=room.token,
                             is_group_chat=True)
    db.add_message(conn, room.token, role="user", body="b", origin_surface="email",
                   task_id=task_id, author_user_id="alice")
    return task_id


def _overlay(config) -> None:
    path = (config.workspace_path / "Users" / "alice" / config.bot_dir_name
            / "config" / "skills" / "email.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"- {OVERLAY_SENTINEL}\n", encoding="utf-8")


def _ambient(config) -> None:
    """Everything a correspondent's turn must not load, each its own sentinel."""
    config.sleep_cycle.enabled = True
    config.sleep_cycle.auto_load_dated_days = 3
    config.memory_search.enabled = True
    config.memory_search.auto_recall = True
    config.playbooks.enabled = True
    memories = config.workspace_path / "Users" / "alice" / "memories"
    memories.mkdir(parents=True, exist_ok=True)
    today = datetime.now(UTC).date().isoformat()
    (memories / f"{today}.md").write_text(f"{DATED_SENTINEL}\n", encoding="utf-8")
    with db.get_db(config.db_path) as conn:
        ensure_table(conn)
        add_fact(conn, "alice", "alice", "knows", FACT_SENTINEL)
        db.add_pin(conn, "alice", "workspace", f"/Users/alice/{PIN_SENTINEL}.md", "Plan")
        conn.commit()


class TestACorrespondentsTurn:
    def test_it_withholds_all_but_the_allowlist(self, config):
        seen = _run(config, _thread_turn)
        assert {"calendar", "health", "nextcloud", "memory", "kv"} <= seen["disabled"]
        assert not {"calendar", "kv", "nextcloud", "memory", "health"} & seen["allowed_skills"]
        assert {"email", "skills"} <= seen["allowed_skills"]
        assert "HEALTH_DB_PATH" not in seen["proxy_base_env"]
        assert seen["vault"] == {}

    def test_the_mount_plan_has_no_users_or_group_or_talk_bind(self, config):
        with db.get_db(config.db_path) as conn:
            db.create_group(conn, "fam", kind="family", display_name="Fam", created_by="op")
            db.add_group_member(conn, "fam", "alice", added_by="op")
            conn.commit()
        for make_turn in (_thread_turn, _roomless_turn):
            binds = _binds(_run(config, make_turn)["argv"])
            assert _user_dir(config) not in binds
            assert not [b for b in binds if "/Groups/" in b or b.endswith("/Talk")]

    def test_the_temp_dir_is_its_own(self, config):
        argv = _run(config, _thread_turn)["argv"]
        host_dir = str((config.temp_dir / "alice").resolve())
        rw = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--bind"]
        assert host_dir not in rw
        assert len([p for p in rw if p.startswith(host_dir + "/room-task-")]) == 1

    def test_user_md_and_an_overlay_load(self, config):
        _user_md(config)
        _overlay(config)
        seen = _run(config, _thread_turn)
        assert SENTINEL in seen["prompt"] + seen["system"]
        assert OVERLAY_SENTINEL in seen["prompt"] + seen["system"]

    def test_the_reply_and_untrusted_input_bodies_load(self, config):
        # The bundled catalogue, where `untrusted_input` is `shared_room: safe`.
        system = _run(config, _thread_turn)["system"]
        assert "### Email" in system
        assert "### Untrusted" in system

    def test_the_rest_of_the_memory_does_not(self, config):
        _user_md(config)
        _ambient(config)
        with patch("istota.memory.search.search") as search:
            for make_turn in (_thread_turn, _roomless_turn):
                seen = _run(config, make_turn)
                text = seen["prompt"] + seen["system"]
                for sentinel in (DATED_SENTINEL, FACT_SENTINEL, PIN_SENTINEL):
                    assert sentinel not in text
                assert SENTINEL in text
        search.assert_not_called()

    def test_control_the_hosts_own_turn_loads_all_of_it(self, config):
        _user_md(config)
        _ambient(config)
        seen = _run(config, _own_thread_turn)
        text = seen["prompt"] + seen["system"]
        for sentinel in (SENTINEL, DATED_SENTINEL, FACT_SENTINEL, PIN_SENTINEL):
            assert sentinel in text
        assert _user_dir(config) in _binds(seen["argv"])
        assert "calendar" not in seen["disabled"]

    def test_the_card_says_what_it_withholds(self, config):
        seen = _run(config, _thread_turn)
        assert "on their correspondence" in seen["system"]
        assert "answer `NO_ACTION:`" in seen["system"]
        assert "you can answer the mail and nothing else of 'alice'" in seen["system"]
        assert "did not write this message" in seen["system"]
        assert "their yes would not make it available" in seen["system"]
        assert "You can also use" not in seen["system"]
        assert "You are responding to a request from user" not in seen["system"]

    def test_an_unattributed_row_is_not_called_the_hosts(self, config):
        def _turn(conn):
            room = db.register_bound_room(conn, "alice", origin="email", name="Dinner",
                                          surface="email", surface_ref="<na@example.com>")
            task_id = db.create_task(conn, prompt="p", user_id="alice", source_type="email",
                                     conversation_token=room.token, is_group_chat=True)
            db.add_message(conn, room.token, role="user", body="p", origin_surface="email",
                           task_id=task_id)
            db.mark_email_processed(conn, f"uid-{task_id}", CORRESPONDENT, user_id="alice",
                                    task_id=task_id, routing_method="plus_address")
            return task_id

        system = _run(config, _turn)["system"]
        assert "This message is from 'alice'" not in system
        assert "did not write this message" in system

    def test_a_widened_set_is_named(self, config, monkeypatch):
        from istota.rooms import scopes

        monkeypatch.setattr(
            scopes, "correspondent_scopes",
            lambda *a, **k: scopes.CorrespondentAllowance(
                scopes=frozenset({"email", "skills", "calendar"}), web=True,
            ),
        )
        seen = _run(config, _thread_turn)
        assert "You can also use: calendar, the web." in seen["system"]
        assert "calendar" not in seen["disabled"]

    def test_the_line_is_in_the_system_half_on_a_roomless_turn(self, config):
        seen = _run(config, _roomless_turn)
        assert "did not write this message" in seen["system"]
        assert "You are responding to a request from user" not in seen["system"]

    def test_control_the_hosts_own_turn_has_no_line(self, config):
        system = _run(config, _own_thread_turn)["system"]
        assert "did not write this message" not in system
        assert "This message is from 'alice'" in system

    def test_a_staged_attachment_is_named_and_the_inbox_path_is_not(self, config):
        inbox = config.workspace_path / "Users" / "alice" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        source = inbox / "note.pdf"
        source.write_bytes(b"%PDF-1.4")

        seen = _run(config, lambda conn: _thread_turn(conn, attachments=[str(source)]))
        assert "room-attachments/note.pdf" in seen["prompt"]
        assert str(source) not in seen["prompt"]


NO_WEB_LINE = "you have no search or fetch tool on this turn"


class TestTheWebTools:
    @staticmethod
    def _allow_web(monkeypatch):
        from istota.rooms import scopes

        monkeypatch.setattr(
            scopes, "correspondent_scopes",
            lambda *a, **k: scopes.CorrespondentAllowance(
                scopes=frozenset(scopes.CORRESPONDENT_DEFAULT_SCOPES), web=True,
            ),
        )

    def test_a_correspondents_turn_has_none(self, config):
        from istota.executor import harness_web_fetch_policy

        for make_turn in (_thread_turn, _roomless_turn):
            seen = _run(config, make_turn)
            assert not {"WebSearch", "WebFetch"} & set(seen["allowed_tools"])
            assert harness_web_fetch_policy(config, seen["allowed_tools"]) is None
            assert NO_WEB_LINE in seen["system"]
            assert "Web search: WebSearch" not in seen["system"]

    def test_they_come_back_when_web_is_allowed(self, config, monkeypatch):
        self._allow_web(monkeypatch)
        seen = _run(config, _thread_turn)
        assert {"WebSearch", "WebFetch"} <= set(seen["allowed_tools"])
        assert "Web search: WebSearch" in seen["system"]
        assert NO_WEB_LINE not in seen["system"]

    def test_control_the_hosts_own_turn_has_them(self, config):
        seen = _run(config, _own_thread_turn)
        assert {"WebSearch", "WebFetch"} <= set(seen["allowed_tools"])
        assert NO_WEB_LINE not in seen["system"]

    def test_control_a_guests_line_is_its_own(self, config):
        seen = _run(config, _talk_guest_turn)
        assert not {"WebSearch", "WebFetch"} & set(seen["allowed_tools"])
        assert "You are answering a guest" in seen["system"]
        assert NO_WEB_LINE not in seen["system"]

    def test_the_browser_is_not_named_where_browse_is_withheld(self, config, monkeypatch):
        """Allowing `web` does not allow `browse`, so the read line points at
        WebFetch rather than a skill the proxy would refuse."""
        config.browser.enabled = True
        self._allow_web(monkeypatch)
        system = _run(config, _thread_turn)["system"]
        assert "prefer the browse skill" not in system
        assert "Web browser for JS-rendered pages" not in system
        assert "WebFetch fetches a URL" in system

    def test_control_the_hosts_own_turn_is_pointed_at_the_browser(self, config):
        config.browser.enabled = True
        system = _run(config, _own_thread_turn)["system"]
        assert "prefer the browse skill" in system
        assert "Web browser for JS-rendered pages" in system


CHANNEL_SENTINEL = "alice-thread-channel-sentinel"


def _with_channel_chunk(make_turn):
    """A turn whose room already has this exchange in its namespace, under the
    words the turn's prompt will search for."""
    from istota.memory.search import index_conversation

    def _turn(conn):
        task_id = make_turn(conn)
        token = db.get_task(conn, task_id).conversation_token
        with patch("istota.memory.search.ensure_vec_table", return_value=False), \
                patch("istota.memory.search.enable_vec_extension", return_value=False):
            assert index_conversation(
                conn, f"channel:{token}", task_id + 1000,
                "what is on Thursday?", CHANNEL_SENTINEL,
            ) > 0
        return task_id
    return _turn


class TestNoRecallAtAll:
    """Their text stays in the thread's namespace for the host, and a
    correspondent's own later turns do not read it back: the mail would be the
    query and the reply a way out."""

    @pytest.fixture(autouse=True)
    def _recall_on(self, config):
        config.memory_search.enabled = True
        config.memory_search.auto_recall = True

    def test_a_correspondents_turn_recalls_nothing_from_the_room(self, config):
        seen = _run(config, _with_channel_chunk(_thread_turn))
        assert CHANNEL_SENTINEL not in seen["prompt"] + seen["system"]

    def test_control_the_hosts_own_turn_there_recalls_it(self, config):
        seen = _run(config, _with_channel_chunk(_own_thread_turn))
        assert CHANNEL_SENTINEL in seen["prompt"] + seen["system"]


class TestTheAutoIndex:
    """Through `process_one_task` and the real index."""

    @staticmethod
    def _namespaces(config, make_turn) -> tuple[str | None, set[str]]:
        from istota.config import MemorySearchConfig
        from istota.scheduler import process_one_task

        config.memory_search = MemorySearchConfig(enabled=True, auto_index_conversations=True)
        with db.get_db(config.db_path) as conn:
            task_id = make_turn(conn)
            token = db.get_task(conn, task_id).conversation_token
        with patch("istota.scheduler.execute_task", return_value=(True, "The answer.", None, None)), \
                patch("istota.scheduler.post_result_to_email", return_value=True), \
                patch("istota.scheduler.run_coro", return_value=True), \
                patch("istota.memory.search.ensure_vec_table", return_value=False), \
                patch("istota.memory.search.enable_vec_extension", return_value=False):
            process_one_task(config)
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_id).status == "completed"
            rows = conn.execute("SELECT DISTINCT user_id FROM memory_chunks").fetchall()
        return token, {row[0] for row in rows}

    def test_a_correspondents_turn_is_in_the_channel_namespace_only(self, config):
        token, namespaces = self._namespaces(config, _thread_turn)
        assert namespaces == {f"channel:{token}"}

    def test_a_roomless_correspondents_turn_is_nowhere(self, config):
        _, namespaces = self._namespaces(config, _roomless_turn)
        assert namespaces == set()

    def test_control_the_hosts_own_turn_is_in_both(self, config):
        token, namespaces = self._namespaces(config, _own_thread_turn)
        assert namespaces == {"alice", f"channel:{token}"}


class TestTheTurnAuthorReachesTheCli:
    """The posture the email CLI refuses on, from the daemon's own derivation.
    It goes to the proxy alone: the model's env is the model's to change."""

    @pytest.mark.parametrize("make_turn,author", [
        (_thread_turn, "correspondent"),
        (_roomless_turn, "correspondent"),
        (_own_thread_turn, "own"),
        (_talk_guest_turn, "guest"),
    ], ids=["thread", "roomless", "own", "guest"])
    def test_the_proxy_has_it_and_the_model_does_not(self, config, make_turn, author):
        from istota.sandbox.host_paths import TURN_AUTHOR_VAR

        seen = _run(config, make_turn)
        assert seen["proxy_base_env"][TURN_AUTHOR_VAR] == author
        assert TURN_AUTHOR_VAR not in seen["model_env"]

    def test_the_cli_refuses_on_what_the_runtime_handed_it(
        self, config, monkeypatch, capsys,
    ):
        from istota.sandbox.host_paths import TURN_AUTHOR_VAR
        from istota.skills import email as email_skill

        seen = _run(config, _thread_turn)
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv(TURN_AUTHOR_VAR, seen["proxy_base_env"][TURN_AUTHOR_VAR])
        with pytest.raises(SystemExit):
            email_skill.main(["list"])
        assert '"reason": "not_owner_turn"' in capsys.readouterr().out


class TestTheDefaultsWorkWithFilesWithheld:
    """`transcribe` and `whisper` are in the default because they read the
    mail's own staged attachments: checked through each CLI's real parser."""

    @pytest.fixture
    def roots(self, tmp_path, monkeypatch):
        attachments = tmp_path / "control" / "room-attachments"
        attachments.mkdir(parents=True)
        inbox = tmp_path / "mount" / "Users" / "alice" / "inbox"
        inbox.mkdir(parents=True)
        monkeypatch.setenv("ISTOTA_ATTACHMENTS_DIR", str(attachments))
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(tmp_path / "mount"))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(tmp_path / "deferred"))
        monkeypatch.setenv("ISTOTA_WITHHELD_SCOPES", "files,memory,calendar")
        (attachments / "a.bin").write_bytes(b"x")
        (inbox / "a.bin").write_bytes(b"x")
        return attachments, inbox

    @pytest.mark.parametrize("skill,verb", [("transcribe", "ocr"), ("whisper", "transcribe")])
    def test_the_staged_copy_is_accepted_and_the_workspace_refused(
        self, roots, skill, verb, capsys,
    ):
        from istota.skills import _cli
        from istota.skills import transcribe as transcribe_skill
        from istota.skills.whisper import cli as whisper_cli

        attachments, inbox = roots
        parser = (transcribe_skill if skill == "transcribe" else whisper_cli).build_parser()
        staged = attachments / "a.bin"
        args = _cli.parse_and_resolve(parser, [verb, str(staged)])
        assert str(staged.resolve()) in [str(v) for v in vars(args).values()]
        with pytest.raises(SystemExit):
            _cli.parse_and_resolve(parser, [verb, str(inbox / "a.bin")])
        assert "host_path_refused" in capsys.readouterr().out


class TestControlATalkGuestsTurn:
    def test_it_still_withholds_everything_and_loads_no_memory(self, config):
        _user_md(config)
        seen = _run(config, _talk_guest_turn)
        assert "calendar" in seen["disabled"]
        assert "HEALTH_DB_PATH" not in seen["proxy_base_env"]
        assert _user_dir(config) not in _binds(seen["argv"])
        assert SENTINEL not in seen["prompt"] + seen["system"]
        assert seen["vault"] == {}


class TestTheOfficeTurn:
    """Email on rooms stage 3, the `office` regression, narrowed: a stranger's
    mail at the plus-address of a user who trusts every sender is a thread room
    turn with that user's USER.md loaded and every other scope withheld. Driven
    from the poller rather than a hand-built row."""

    def test_a_strangers_first_mail_loads_user_md_and_withholds_the_rest(self, config):
        from istota.config import EmailConfig

        from .test_email_thread_rooms import _poll

        config.email = EmailConfig(
            enabled=True, imap_host="imap.test", imap_port=993, imap_user="user",
            imap_password="pass", smtp_host="smtp.test", smtp_port=587,
            bot_email="bot@test.com",
        )
        config.bot_name = "Zorg"
        config.users["alice"] = UserConfig(email_addresses=["alice@test.com"],
                                           trusted_email_senders=["*"])
        _user_md(config)

        def _turn(conn):
            (task_id,) = _poll(config, sender=CORRESPONDENT, to=("bot+alice@test.com",),
                               message_id="<s1@example.com>", body="Are you open Sunday?")
            return task_id

        seen = _run(config, _turn)
        assert "calendar" in seen["disabled"]
        assert "email" not in seen["disabled"]
        assert _user_dir(config) not in _binds(seen["argv"])
        assert SENTINEL in seen["prompt"] + seen["system"]
        assert "on their correspondence" in seen["system"]
        assert "did not write this message" in seen["system"]

    @pytest.mark.asyncio
    async def test_the_reply_is_sent_with_no_draft(self, config):
        from istota.config import EmailConfig
        from istota.executor import task_deferred_dir
        from istota.scheduler import _drain_deferred_ops, post_result_to_email

        from .test_email_thread_rooms import _poll

        config.email = EmailConfig(
            enabled=True, imap_host="imap.test", imap_port=993, imap_user="user",
            imap_password="pass", smtp_host="smtp.test", smtp_port=587,
            bot_email="bot@test.com",
        )
        config.bot_name = "Zorg"
        config.users["alice"] = UserConfig(email_addresses=["alice@test.com"],
                                           trusted_email_senders=["*"])
        _user_md(config)
        made: list[int] = []

        def _turn(conn):
            (task_id,) = _poll(config, sender=CORRESPONDENT, to=("bot+alice@test.com",),
                               message_id="<s2@example.com>", body="Are you open Sunday?")
            made.append(task_id)
            return task_id

        seen = _run(config, _turn)
        assert "did not write this message" in seen["system"]
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, made[0])
        directory = task_deferred_dir(config, task)
        assert directory.name.startswith("room-task-")
        (directory / f"task_{task.id}_email_output.json").write_text(json.dumps(
            {"subject": "", "body": "We open at ten.", "format": "plain"}))

        _drain_deferred_ops(config, task, "done")
        with patch("istota.transport.email.outbound.reply_to_email",
                   return_value="<out@test.com>") as reply:
            assert await post_result_to_email(config, task, "done") is True

        assert reply.call_args.kwargs["to_addr"] == CORRESPONDENT
        assert reply.call_args.kwargs["body"] == "We open at ten."
        with db.get_db(config.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM outbound_drafts").fetchone()[0] == 0
