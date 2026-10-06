"""#675: in a friendly room the gate filters and the agent decides.

A turn the classifier lets through in a `friendly` room runs as a declinable
task. The agent may answer `NO_ACTION:`, and then nothing is stored, posted,
streamed or pushed: the room is left as if the bot never looked, and the
decline is one more row in `speech_gate_decisions`. #656's probable-miss row
is here too, since it is written against the same turns.
"""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from istota import db
from istota.config import Config, NextcloudConfig, TalkConfig, UserConfig
from istota.rooms import speech_gate
from istota.rooms.speech_gate import (
    FRIENDLY,
    RESERVED,
    GateDecision,
    WindowTurn,
    build_window,
    decline_reason,
    ends_with_name,
    is_declinable,
    is_nudge,
    record_decision,
    unopened_file_label,
    unopened_file_line,
)
from istota.transport import IncomingMessage, classify_ahead, ingest_message


def _human(author, text):
    return WindowTurn(author=author, text=text, is_bot=False)


def _bot(text):
    return WindowTurn(author="Istota (assistant)", text=text, is_bot=True)


# --- the rules, without a database ----------------------------------------


class TestTheTwoNewFacts:
    @pytest.mark.parametrize("text", [
        "seems relevant Istota", "seems relevant istota!", "seems relevant @Istota.",
    ])
    def test_a_trailing_name_is_seen(self, text):
        assert ends_with_name(text, "Istota")

    @pytest.mark.parametrize("text", ["Istota, look", "", "Istotas", "not istota-ish"])
    def test_anything_else_is_not(self, text):
        assert not ends_with_name(text, "Istota")

    def test_a_two_word_name(self):
        assert ends_with_name("ask Old Zorg", "Old Zorg")

    def test_the_friendly_prompt_states_both(self):
        turns = [
            _human("alice", unopened_file_line("a GIF")),
            _human("alice", "seems relevant Istota"),
        ]
        prompt = build_window(turns, bot_name="Istota", disposition=FRIENDLY)
        assert "The newest message ends with Istota's name: yes." in prompt
        assert "has not opened: yes (a GIF)." in prompt

    def test_a_file_the_bot_spoke_after_is_not_unopened_news(self):
        turns = [
            _human("alice", unopened_file_line("a GIF")),
            _bot("Nice."),
            _human("bob", "ha"),
        ]
        prompt = build_window(turns, bot_name="Istota", disposition=FRIENDLY)
        assert "has not opened: no." in prompt
        assert "ends with Istota's name: no." in prompt

    def test_the_stand_in_reads_back(self):
        assert unopened_file_label("caption\n" + unopened_file_line("an image")) == (
            "an image"
        )
        assert unopened_file_label("I sent a file") is None
        assert unopened_file_label(None) is None


class TestWhatIsDeclinable:
    @pytest.mark.parametrize("rung", ["classifier", "follow_up"])
    def test_a_classifier_verdict_in_a_friendly_room(self, rung):
        assert is_declinable(GateDecision(True, rung), FRIENDLY)

    @pytest.mark.parametrize("rung", [
        "addressed", "not_multi_human", "mode_off", "classifier",
    ])
    def test_nothing_in_a_reserved_room(self, rung):
        assert not is_declinable(GateDecision(True, rung), RESERVED)

    @pytest.mark.parametrize("rung", ["addressed", "not_multi_human", "mode_off"])
    def test_a_certain_rule_is_never_declinable(self, rung):
        assert not is_declinable(GateDecision(True, rung), FRIENDLY)

    def test_a_silent_decision_is_not(self):
        assert not is_declinable(GateDecision(False, "classifier"), FRIENDLY)


class TestTheDeclineMarker:
    def test_a_leading_marker_declines_with_its_reason(self):
        assert decline_reason("  NO_ACTION: they are talking to each other\nmore") == (
            "they are talking to each other"
        )

    def test_a_bare_marker_declines_with_no_reason(self):
        assert decline_reason("NO_ACTION:") == ""

    @pytest.mark.parametrize("result", [
        "Sure. NO_ACTION: is what a cron job answers", "", None, 3,
    ])
    def test_anything_else_is_an_answer(self, result):
        assert decline_reason(result) is None


class TestANudge:
    @pytest.mark.parametrize("text", ["Istota", "@istota?", "istota!", "?", "hello?"])
    def test_a_call_back(self, text):
        assert is_nudge(text, "Istota")

    @pytest.mark.parametrize("text", ["Istota, what time is it?", "hello there", ""])
    def test_a_real_turn(self, text):
        assert not is_nudge(text, "Istota")


# --- through ingest --------------------------------------------------------


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "istota.db"
    cfg.temp_dir = tmp_path / "temp"
    cfg.temp_dir.mkdir()
    db.init_db(cfg.db_path)
    cfg.bot_name = "Istota"
    cfg.speech_gate.mode = "classifier"
    return cfg


def _seed(config, token="grp"):
    with db.get_db(config.db_path) as conn:
        db.register_room(conn, token, "alice", origin="talk", name="Family")
        db.add_message(conn, token, role="user", body="dinner at seven?",
                       origin_surface="talk", author_user_id="bob")


_PLATFORM_ID = [700]


def _ingest(config, text, answer=None, *, addressed=False, user="alice", token="grp"):
    classified = None
    if answer is not None:
        with patch("istota.executor.build_speech_gate_completer",
                   return_value=lambda _p: answer):
            classified = classify_ahead(
                config, surface="talk", surface_ref=token, user_id=user,
                text=text, is_group_chat=True, addressed_to_bot=addressed,
            )
    _PLATFORM_ID[0] += 1
    with db.get_db(config.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        task_id = ingest_message(conn, config, IncomingMessage(
            user_id=user, text=text, source_type="talk", surface="talk",
            channel_token=token, is_group_chat=True, addressed_to_bot=addressed,
            platform_message_id=_PLATFORM_ID[0], classified=classified,
        ))
        task = db.get_task(conn, task_id) if task_id is not None else None
    return task


class TestTheTaskIsMarked:
    def test_a_friendly_classifier_yes_is_declinable(self, config):
        config.speech_gate.disposition = FRIENDLY
        _seed(config)
        task = _ingest(config, "anyone know a plumber?", '{"speak": true}')
        assert task is not None and task.declinable

    def test_a_reserved_classifier_yes_is_not(self, config):
        config.speech_gate.disposition = RESERVED
        _seed(config)
        task = _ingest(config, "anyone know a plumber?", '{"speak": true}')
        assert task is not None and not task.declinable

    def test_an_addressed_turn_is_not(self, config):
        config.speech_gate.disposition = FRIENDLY
        _seed(config)
        task = _ingest(config, "Istota, a plumber?", addressed=True)
        assert task is not None and not task.declinable


class TestAProbableMiss:
    """#656: a member's nudge marks the turn the bot passed over."""

    def _misses(self, config):
        with db.get_db(config.db_path) as conn:
            return [tuple(r) for r in conn.execute(
                "SELECT message_id, reason FROM speech_gate_decisions "
                "WHERE rung = 'probable_miss'"
            )]

    def _turn_id(self, config, text):
        with db.get_db(config.db_path) as conn:
            return conn.execute(
                "SELECT id FROM messages WHERE body = ?", (text,),
            ).fetchone()[0]

    def test_a_nudge_after_a_skipped_turn(self, config):
        config.speech_gate.disposition = FRIENDLY
        _seed(config)
        assert _ingest(config, "seems relevant Istota", '{"speak": false}') is None
        _ingest(config, "Istota?", addressed=True)
        assert self._misses(config) == [
            (self._turn_id(config, "seems relevant Istota"), "nudged after classifier"),
        ]

    def test_a_nudge_after_an_answer_marks_nothing(self, config):
        config.speech_gate.disposition = FRIENDLY
        _seed(config)
        _ingest(config, "anyone know a plumber?", '{"speak": false}')
        with db.get_db(config.db_path) as conn:
            db.add_message(conn, "grp", role="assistant", body="Try Joe's.",
                           origin_surface="talk")
        _ingest(config, "Istota?", addressed=True)
        assert self._misses(config) == []

    def test_an_ordinary_turn_marks_nothing(self, config):
        config.speech_gate.disposition = FRIENDLY
        _seed(config)
        _ingest(config, "anyone know a plumber?", '{"speak": false}')
        _ingest(config, "Istota, a plumber please", addressed=True)
        assert self._misses(config) == []

    def test_a_second_nudge_marks_the_turn_once(self, config):
        config.speech_gate.disposition = FRIENDLY
        _seed(config)
        _ingest(config, "anyone know a plumber?", '{"speak": false}')
        _ingest(config, "Istota?", addressed=True)
        _ingest(config, "hello?", addressed=True)
        assert len(self._misses(config)) == 1


# --- through the scheduler -------------------------------------------------


@pytest.fixture
def sched_config(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    temp = tmp_path / "temp"
    temp.mkdir()
    mount = tmp_path / "mount"
    mount.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    cfg = Config(
        db_path=path,
        nextcloud=NextcloudConfig(
            url="https://nc.example.com", username="istota", app_password="s",
        ),
        talk=TalkConfig(enabled=True, bot_username="istota"),
        workspace_path=mount,
        temp_dir=temp,
        users={"alice": UserConfig(display_name="Alice")},
    )
    cfg.skills_dir = skills
    cfg.bot_name = "Istota"
    cfg.scheduler.push_notification_sources = ["talk"]
    return cfg


def _talk_turn(config, *, declinable, token="talkroom"):
    with db.get_db(config.db_path) as conn:
        db.register_room(conn, token, "alice", origin="talk")
        db.add_room_binding(conn, token, "talk", token)
        task_id = db.create_task(
            conn, prompt="anyone know a plumber?", user_id="alice",
            source_type="talk", conversation_token=token, is_group_chat=True,
            declinable=declinable,
        )
        message_id = db.add_message(
            conn, token, role="user", body="anyone know a plumber?",
            origin_surface="talk", author_user_id="alice", task_id=task_id,
        )
        record_decision(
            conn, room_token=token, surface="talk", user_id="alice",
            message_id=message_id,
            decision=GateDecision(True, "classifier" if declinable else "addressed"),
            disposition=FRIENDLY,
        )
    return task_id, message_id


def _close_and_answer(coro, **_kw):
    close = getattr(coro, "close", None)
    if close is not None:
        close()
    return 123


def _run(config, result):
    posted = MagicMock(return_value=None)
    pushes = []

    def execute(task, config, user_resources, dry_run=False, event_writer=None, **_):
        return True, result, None, None

    with ExitStack() as stack:
        stack.enter_context(patch("istota.scheduler.execute_task", side_effect=execute))
        stack.enter_context(patch("istota.scheduler.post_result_to_talk", new=posted))
        stack.enter_context(patch("istota.scheduler.run_coro",
                                  side_effect=_close_and_answer))
        stack.enter_context(patch(
            "istota.scheduler.PushNotificationSubscriber",
            side_effect=lambda *a, **k: pushes.append(a) or MagicMock(),
        ))
        outcome = process_one_task(config)
    return outcome, posted, pushes


from istota.scheduler import process_one_task  # noqa: E402


class TestADecline:
    def test_nothing_is_stored_posted_or_pushed(self, sched_config):
        task_id, message_id = _talk_turn(sched_config, declinable=True)
        outcome, posted, pushes = _run(
            sched_config, "NO_ACTION: they are talking to each other",
        )

        assert outcome == (task_id, True)
        assert posted.call_count == 0  # neither the ack nor an answer
        assert pushes == []
        with db.get_db(sched_config.db_path) as conn:
            task = db.get_task(conn, task_id)
            rows = conn.execute(
                "SELECT role FROM messages WHERE task_id = ?", (task_id,),
            ).fetchall()
            audit = conn.execute(
                "SELECT spoke, rung, reason, message_id FROM speech_gate_decisions "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            events = db.get_task_events(conn, task_id, 0)
        assert (task.status, task.result) == ("completed", None)
        assert [r["role"] for r in rows] == ["user"]
        assert tuple(audit) == (
            0, "agent_declined", "they are talking to each other", message_id,
        )
        kinds = [e["kind"] for e in events]
        assert "text_delta" not in kinds
        (result,) = [e for e in events if e["kind"] == "result"]
        assert "text" not in result["payload"]
        assert "talking to each other" not in str([e["payload"] for e in events])

    def test_a_declinable_turn_that_answers_posts_as_today(self, sched_config):
        task_id, _ = _talk_turn(sched_config, declinable=True)
        _outcome, posted, _ = _run(sched_config, "Try Joe's, they're quick.")

        assert posted.call_count == 1  # the answer, and no ack before it
        with db.get_db(sched_config.db_path) as conn:
            body = conn.execute(
                "SELECT body FROM messages WHERE task_id = ? AND role = 'assistant'",
                (task_id,),
            ).fetchone()
        assert body["body"] == "Try Joe's, they're quick."

    def test_an_addressed_turn_never_declines(self, sched_config):
        task_id, _ = _talk_turn(sched_config, declinable=False)
        _outcome, posted, pushes = _run(sched_config, "NO_ACTION: not for me")

        assert posted.call_count == 2  # the ack, then the answer
        assert pushes != []
        with db.get_db(sched_config.db_path) as conn:
            task = db.get_task(conn, task_id)
            rungs = [r[0] for r in conn.execute(
                "SELECT rung FROM speech_gate_decisions")]
        assert task.result == "NO_ACTION: not for me"
        assert "agent_declined" not in rungs


class TestTheWebView:
    """A declinable turn shows no slot while it runs, and none after a decline."""

    def _page(self, monkeypatch, config, token):
        from istota.webui import app as web_app

        monkeypatch.setattr(web_app, "_config", config)
        return web_app._chat_room_messages("alice", token, 50)

    def _web_turn(self, config, *, status, result=None, token="webroom"):
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, token, "alice", origin="web")
            db.add_web_room_member(conn, token, "alice")
            task_id = db.create_task(
                conn, prompt="anyone?", user_id="alice", source_type="web",
                conversation_token=token, declinable=True,
            )
            db.add_message(conn, token, role="user", body="anyone?",
                           origin_surface="web", author_user_id="alice",
                           task_id=task_id)
            db.update_task_status(conn, task_id, status, result=result)
        return task_id

    def test_a_running_turn_has_no_slot(self, monkeypatch, sched_config):
        task_id = self._web_turn(sched_config, status="running")
        page = self._page(monkeypatch, sched_config, "webroom")
        assert page["active_tasks"] == []
        assert not [m for m in page["messages"]
                    if m["role"] == "assistant" and m.get("task_id") == task_id]

    def test_a_declined_turn_has_no_bubble(self, monkeypatch, sched_config):
        task_id = self._web_turn(sched_config, status="completed")
        page = self._page(monkeypatch, sched_config, "webroom")
        assert not [m for m in page["messages"]
                    if m["role"] == "assistant" and m.get("task_id") == task_id]

    def test_the_room_stream_marks_the_turn(self, sched_config):
        from istota.webui import app as web_app

        self._web_turn(sched_config, status="running")
        with db.get_db(sched_config.db_path) as conn:
            rows = db.list_room_events_since(conn, "alice", since_id=0)
        (user_row,) = [r for r in rows if r["role"] == "user"]
        assert web_app._cross_room_message_dict(user_row, "alice")["declinable"] is True


def test_the_marker_line_matches_the_whatsapp_stand_in():
    from istota.transport.whatsapp.groups import _unopened_stand_in

    assert _unopened_stand_in("gif") == unopened_file_line("a GIF")
    assert unopened_file_label(_unopened_stand_in("image")) == "an image"


def test_the_decline_line_names_the_marker():
    assert speech_gate.DECLINE_MARKER in speech_gate.DECLINE_TASK_LINE
