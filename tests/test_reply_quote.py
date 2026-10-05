"""An answer quotes the message that asked it once the room has moved on
(ISSUE-641).

The rule, on every surface with quoted replies: when the first part of the
answer is sent, quote the triggering message if anything else was posted in the
room since it, and send unquoted while it is still the latest. This task's own
acknowledgement and progress posts do not count; another task's answer does.

Talk asks Talk, through the real `TalkTransport.deliver` over a mocked client.
Web is driven through `process_one_task` and read back through the history
endpoint's builder, which is what a reload renders. WhatsApp goes through
`WhatsAppTransport.send_record` to the adapter's request, and the bridge half
runs the sidecar's own `Session.send` under node.
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from istota import db
from istota.config import (
    Config,
    EmailConfig,
    NextcloudConfig,
    SchedulerConfig,
    TalkConfig,
    UserConfig,
)
from istota.transport import reply_quote
from istota.transport.talk import TalkTransport


# ---------------------------------------------------------------------------
# The rule over the canonical store
# ---------------------------------------------------------------------------


def _user_row(conn, token, task_id, body="question", **kw):
    return db.add_message(
        conn, token, role="user", body=body, origin_surface="web", task_id=task_id, **kw,
    )


@pytest.fixture
def conn(db_path):
    with db.get_db(db_path) as c:
        db.register_room(c, "room", "alice", origin="web")
        yield c


class TestTheRuleOverTheStore:
    def test_the_latest_message_is_not_quoted(self, conn):
        _user_row(conn, "room", 1)
        assert reply_quote.quoted_trigger(conn, 1, "room") is None

    def test_a_later_message_from_anyone_quotes(self, conn):
        trigger = _user_row(conn, "room", 1)
        db.add_message(conn, "room", role="user", body="meanwhile", origin_surface="web",
                       author_user_id="bob")
        quoted = reply_quote.quoted_trigger(conn, 1, "room")
        assert quoted is not None and quoted.message_id == trigger

    def test_another_tasks_answer_quotes(self, conn):
        trigger = _user_row(conn, "room", 1)
        db.add_message(conn, "room", role="assistant", body="other", origin_surface="web",
                       task_id=2)
        assert reply_quote.quoted_trigger(conn, 1, "room").message_id == trigger

    def test_the_askers_own_follow_up_quotes(self, conn):
        trigger = _user_row(conn, "room", 1)
        _user_row(conn, "room", 2, body="and also")
        assert reply_quote.quoted_trigger(conn, 1, "room").message_id == trigger

    def test_this_tasks_own_rows_and_notices_do_not(self, conn):
        _user_row(conn, "room", 1)
        db.add_message(conn, "room", role="assistant", body="mine", origin_surface="web",
                       task_id=1)
        db.add_message(conn, "room", role="system", body="a notice", origin_surface="web")
        assert reply_quote.quoted_trigger(conn, 1, "room") is None

    def test_an_answer_into_another_room_has_nothing_to_quote(self, conn):
        db.register_room(conn, "elsewhere", "alice", origin="web")
        _user_row(conn, "room", 1)
        _user_row(conn, "room", 2)
        assert reply_quote.quoted_trigger(conn, 1, "elsewhere") is None

    def test_a_task_with_no_user_row_quotes_nothing(self, conn):
        assert reply_quote.quoted_trigger(conn, 99, "room") is None


class TestTheRuleOverTalk:
    def _moved(self, messages):
        return reply_quote.talk_room_moved_on(
            messages, trigger_id=10, task_id=5, bot_actor_ids={"istota"},
        )

    def test_nothing_after_the_trigger(self):
        assert not self._moved([{"id": 10, "message": "q"}])

    def test_this_tasks_own_posts_and_system_messages_do_not_count(self):
        bot = {"actorType": "users", "actorId": "istota"}
        assert not self._moved([
            {"id": 11, "referenceId": "istota:task:5:ack", "message": "working", **bot},
            {"id": 12, "referenceId": "istota:task:5:text", "message": "so far", **bot},
            {"id": 13, "systemMessage": "user_added", "message": "x"},
        ])

    def test_a_participant_borrowing_the_tasks_reference_still_counts(self):
        # Any participant can set a `referenceId`; only the bot's own posts
        # are this task's.
        assert self._moved([{
            "id": 11, "referenceId": "istota:task:5:ack", "message": "hi",
            "actorType": "users", "actorId": "mallory",
        }])

    @pytest.mark.parametrize("message", [
        {"id": 11, "message": "meanwhile"},
        {"id": 11, "referenceId": "istota:task:6:result", "message": "other answer"},
        # Task 50's prefix shares task 5's digits; it is still another task.
        {"id": 11, "referenceId": "istota:task:50:result", "message": "other answer"},
    ])
    def test_anything_else_counts(self, message):
        assert self._moved([message])


# ---------------------------------------------------------------------------
# Talk
# ---------------------------------------------------------------------------


def _talk_config(**kw):
    return Config(nextcloud=NextcloudConfig(
        url="https://nc.example.com", username="istota", app_password="secret",
    ), **kw)


def _talk_task(**overrides):
    fields = dict(
        id=5, status="completed", source_type="talk", user_id="bob",
        prompt="hi", conversation_token="room123", is_group_chat=True,
        talk_message_id=42,
    )
    fields.update(overrides)
    return db.Task(**fields)


def _talk_deliver(task, later, *, parts=None, threaded=True, config=None, token="room123"):
    with patch("istota.transport.talk.get_talk_client") as client_factory:
        client = client_factory.return_value
        client.send_message = AsyncMock(return_value={"id": 100})
        if isinstance(later, Exception):
            client.fetch_messages_since = AsyncMock(side_effect=later)
        else:
            client.fetch_messages_since = AsyncMock(return_value=later)
        transport = TalkTransport(config or _talk_config())
        if parts is not None:
            with patch("istota.transport.talk.split_message", return_value=parts):
                asyncio.run(transport.deliver(
                    token, "answer", task=task, threaded=threaded))
        else:
            asyncio.run(transport.deliver(token, "answer", task=task, threaded=threaded))
    return client


class TestTalk:
    def test_an_immediate_answer_goes_out_clean_and_still_mentions(self):
        client = _talk_deliver(_talk_task(), [])
        # One short page, never the whole room.
        client.fetch_messages_since.assert_awaited_once()
        call = client.fetch_messages_since.await_args
        assert call.args == ("room123", 42)
        assert call.kwargs["max_pages"] == 1 and call.kwargs["timeout"] <= 5
        client.send_message.assert_awaited_once_with(
            "room123", "@bob answer", reply_to=None, reference_id=None,
        )

    def test_a_room_that_moved_on_is_quoted(self):
        client = _talk_deliver(_talk_task(), [{"id": 43, "message": "meanwhile"}])
        client.send_message.assert_awaited_once_with(
            "room123", "@bob answer", reply_to=42, reference_id=None,
        )

    def test_own_progress_does_not_quote(self):
        client = _talk_deliver(_talk_task(), [{
            "id": 43, "referenceId": "istota:task:5:ack", "message": "…",
            "actorType": "users", "actorId": "istota",
        }])
        assert client.send_message.await_args.kwargs["reply_to"] is None

    def test_a_follow_up_in_a_one_to_one_room_is_quoted_without_a_mention(self):
        client = _talk_deliver(
            _talk_task(is_group_chat=False), [{"id": 43, "message": "also this"}],
        )
        client.send_message.assert_awaited_once_with(
            "room123", "answer", reply_to=42, reference_id=None,
        )

    def test_only_the_first_part_quotes(self):
        client = _talk_deliver(
            _talk_task(), [{"id": 43, "message": "x"}], parts=["P1", "P2"],
        )
        calls = client.send_message.await_args_list
        assert calls[0].args == ("room123", "@bob P1")
        assert calls[0].kwargs["reply_to"] == 42
        assert calls[1].args == ("room123", "P2")
        assert calls[1].kwargs["reply_to"] is None

    def test_a_failed_check_sends_unquoted(self):
        client = _talk_deliver(_talk_task(), RuntimeError("nextcloud down"))
        assert client.send_message.await_args.kwargs["reply_to"] is None
        assert client.send_message.await_count == 1

    def test_an_answer_into_another_conversation_does_not_quote(self):
        client = _talk_deliver(_talk_task(), [{"id": 43}], token="elsewhere")
        client.fetch_messages_since.assert_not_awaited()
        assert client.send_message.await_args.kwargs["reply_to"] is None

    def test_a_web_turn_quotes_its_repost_in_the_bound_conversation(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "webroom", "bob", origin="web")
            db.add_room_binding(conn, "webroom", "talk", "room123")
            db.add_message(conn, "webroom", role="user", body="q", origin_surface="web",
                           task_id=5, external_ids={"talk": "77"})
        task = _talk_task(source_type="web", talk_message_id=None,
                          conversation_token="webroom")
        client = _talk_deliver(task, [{"id": 78, "message": "meanwhile"}],
                               config=_talk_config(db_path=db_path))
        assert client.fetch_messages_since.await_args.args == ("room123", 77)
        # Quoted, and no mention: the asker did not ask on Talk.
        client.send_message.assert_awaited_once_with(
            "room123", "answer", reply_to=77, reference_id=None,
        )

    def test_progress_never_reads_or_quotes(self):
        client = _talk_deliver(_talk_task(), [{"id": 43}], threaded=False)
        client.fetch_messages_since.assert_not_awaited()
        assert client.send_message.await_args.kwargs["reply_to"] is None


# ---------------------------------------------------------------------------
# Web, through the scheduler and back out of the history builder
# ---------------------------------------------------------------------------


def _scheduler_config(db_path, tmp_path):
    mount = tmp_path / "mount"
    mount.mkdir(exist_ok=True)
    return Config(
        db_path=db_path,
        nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota",
                                  app_password="s"),
        talk=TalkConfig(enabled=True, bot_username="istota"),
        email=EmailConfig(enabled=False),
        scheduler=SchedulerConfig(),
        workspace_path=mount,
        temp_dir=tmp_path / "temp",
        users={"alice": UserConfig(display_name="Alice"),
               "bob": UserConfig(display_name="Bob")},
    )


def _run_web_turn(db_path, tmp_path, *, moved_on):
    from istota.scheduler import process_one_task

    config = _scheduler_config(db_path, tmp_path)
    with db.get_db(db_path) as conn:
        db.register_room(conn, "webroom", "alice", origin="web")
        db.add_room_member(conn, "webroom", "bob")
        task_id = db.create_task(conn, prompt="what time is it", user_id="alice",
                                 source_type="web", conversation_token="webroom")
        trigger = db.add_message(conn, "webroom", role="user", body="what time is it",
                                 origin_surface="web", task_id=task_id,
                                 author_user_id="alice")
        if moved_on:
            db.add_message(conn, "webroom", role="user", body="lunch?",
                           origin_surface="web", author_user_id="bob")
    with patch("istota.scheduler.execute_task", return_value=(True, "noon", None, None)):
        result = process_one_task(config)
    assert result is not None and result[1] is True
    with db.get_db(db_path) as conn:
        row = conn.execute(
            "SELECT id, reply_to_message_id FROM messages "
            "WHERE task_id = ? AND role = 'assistant'", (task_id,),
        ).fetchone()
    return config, trigger, row


class TestWeb:
    def test_an_immediate_answer_cites_nothing(self, db_path, tmp_path):
        _config, _trigger, row = _run_web_turn(db_path, tmp_path, moved_on=False)
        assert row["reply_to_message_id"] is None

    def test_a_room_that_moved_on_cites_the_question_after_a_reload(
        self, db_path, tmp_path, monkeypatch,
    ):
        pytest.importorskip("fastapi")
        from istota.webui import app as web_app

        config, trigger, row = _run_web_turn(db_path, tmp_path, moved_on=True)
        assert row["reply_to_message_id"] == trigger

        monkeypatch.setattr(web_app, "_config", config)
        page = web_app._chat_room_messages("alice", "webroom", 50)
        (answer,) = [m for m in page["messages"] if m.get("msg_id") == row["id"]]
        assert answer["reply_to"]["msg_id"] == trigger
        assert answer["reply_to"]["role"] == "user"
        assert answer["reply_to"]["excerpt"] == "what time is it"


# ---------------------------------------------------------------------------
# WhatsApp: the daemon half
# ---------------------------------------------------------------------------


def _whatsapp_send(tmp_path, monkeypatch, *, moved_on, reference_id=None):
    from istota.transport.whatsapp import (
        WhatsAppTransport,
        outbound,
        whatsapp_conversation_token,
    )

    from .test_whatsapp_outbound_media import USER, _Adapter, _bind, _config, _with

    config = _config(tmp_path)
    _bind(config)
    adapter = _Adapter(config)
    monkeypatch.setattr(outbound, "active_adapter", lambda c: _with(c, adapter))
    with db.get_db(config.db_path) as conn:
        # The user's private chat, keyed by its stable token rather than a
        # group's: an answer into a group goes only to that group.
        db.register_room(conn, "waroom", USER, origin="whatsapp")
        task_id = db.create_task(conn, prompt="weather?", user_id=USER,
                                 source_type="whatsapp",
                                 conversation_token=whatsapp_conversation_token(USER))
        db.add_message(conn, "waroom", role="user", body="weather?",
                       origin_surface="whatsapp", task_id=task_id,
                       external_ids={"whatsapp": "WAMID.TRIGGER"})
        if moved_on:
            db.add_message(conn, "waroom", role="user", body="and tomorrow?",
                           origin_surface="whatsapp", task_id=task_id + 1)
        task = db.get_task(conn, task_id)
    asyncio.run(WhatsAppTransport(config).send_record(
        "", "Sunny.", task=task, reference_id=reference_id or f"task-result:{task.id}",
    ))
    return adapter


class TestWhatsAppDaemon:
    def test_an_immediate_answer_is_not_a_reply(self, tmp_path, monkeypatch):
        adapter = _whatsapp_send(tmp_path, monkeypatch, moved_on=False)
        assert adapter.requests[0].reply_to_message_id is None

    def test_a_chat_that_moved_on_replies_to_the_trigger(self, tmp_path, monkeypatch):
        adapter = _whatsapp_send(tmp_path, monkeypatch, moved_on=True)
        assert adapter.requests[0].reply_to_message_id == "WAMID.TRIGGER"

    def test_only_the_tasks_own_answer_quotes(self, tmp_path, monkeypatch):
        adapter = _whatsapp_send(
            tmp_path, monkeypatch, moved_on=True, reference_id="confirmation-task:1",
        )
        assert adapter.requests[0].reply_to_message_id is None


# ---------------------------------------------------------------------------
# WhatsApp: the bridge
# ---------------------------------------------------------------------------


class TestTheBridgeQuotes:
    """`Session.send` quotes only an original it still holds, and only into
    the chat it came from. A miss is decided before the send and answers ok."""

    @staticmethod
    def _send(tmp_path, payload, *, remember=None):
        from .test_whatsapp_sidecar_vendoring import PROGRAM, TestTheSidecarsInboundMedia

        remembered = ""
        if remember is not None:
            chat, message = remember
            remembered = f"m.rememberInbound({json.dumps(chat)}, {json.dumps(message)});"
        script = (
            f"const m = require({json.dumps(str(PROGRAM))});"
            "const answers = []; const sends = [];"
            "const link = {greeted: true, send: (t, f) => {"
            " answers.push(Object.assign({type: t}, f)); return true; }};"
            + remembered +
            "const s = new m.Session(link);"
            "s.sock = {sendMessage: async (to, c, o) => {"
            " sends.push({keys: Object.keys(c).sort(),"
            "  quoted: o && o.quoted ? o.quoted.key.id : null,"
            "  chat: o && o.quoted ? o.quoted.key.remoteJid : null});"
            " return {key: {id: 'id' + sends.length}, message: {}}; }};"
            f"s.send({json.dumps(payload)}).then(() =>"
            " process.stdout.write(JSON.stringify({answers, sends})));"
        )
        return TestTheSidecarsInboundMedia._run(script, media_dir=tmp_path)

    _ORIGINAL = {"key": {"id": "IN1", "remoteJid": "1@s.whatsapp.net"},
                 "message": {"conversation": "weather?"}}

    def _payload(self, **kw):
        payload = {"request_id": "r1", "to": "1@s.whatsapp.net", "text": "Sunny.",
                   "kind": "service", "reply_to_message_id": "IN1"}
        payload.update(kw)
        return payload

    def test_a_held_original_is_quoted(self, tmp_path):
        out = self._send(tmp_path, self._payload(),
                         remember=("1@s.whatsapp.net", self._ORIGINAL))
        assert out["sends"] == [{"keys": ["text"], "quoted": "IN1",
                                 "chat": "1@s.whatsapp.net"}]
        assert out["answers"][0]["ok"] is True

    def test_a_lid_addressed_original_is_quoted_into_the_send_chat(self, tmp_path):
        # The chat was recorded under its phone JID; the message keeps its
        # `@lid` key, which would read as a quote from another chat.
        original = {"key": {"id": "IN1", "remoteJid": "99@lid",
                            "remoteJidAlt": "1@s.whatsapp.net"},
                    "message": {"conversation": "weather?"}}
        out = self._send(tmp_path, self._payload(),
                         remember=("1@s.whatsapp.net", original))
        assert out["sends"] == [{"keys": ["text"], "quoted": "IN1",
                                 "chat": "1@s.whatsapp.net"}]

    def test_a_miss_sends_unquoted_and_answers_ok(self, tmp_path):
        out = self._send(tmp_path, self._payload())
        assert out["sends"] == [{"keys": ["text"], "quoted": None, "chat": None}]
        assert out["answers"] == [{"type": "send_result", "request_id": "r1",
                                   "ok": True, "message_id": "id1"}]

    def test_an_original_from_another_chat_is_not_quoted(self, tmp_path):
        out = self._send(tmp_path, self._payload(),
                         remember=("2@s.whatsapp.net", self._ORIGINAL))
        assert out["sends"] == [{"keys": ["text"], "quoted": None, "chat": None}]

    def test_only_the_first_message_of_a_split_send_quotes(self, tmp_path):
        (tmp_path / "out-a.png").write_bytes(b"PNG data")
        out = self._send(tmp_path, self._payload(
            text="x" * 1100,
            media={"name": "out-a.png", "mimetype": "image/png", "kind": "image",
                   "caption": "x" * 1100},
        ), remember=("1@s.whatsapp.net", self._ORIGINAL))
        assert [s["quoted"] for s in out["sends"]] == ["IN1", None]

    def test_the_cache_is_bounded(self, tmp_path):
        from .test_whatsapp_sidecar_vendoring import TestTheSidecarsInboundMedia

        evicted, kept = TestTheSidecarsInboundMedia._call(
            "(() => { for (let i = 0; i <= m.INBOUND_CACHE_LIMIT; i++)"
            "  m.rememberInbound('1@s.whatsapp.net', {key: {id: 'k' + i}});"
            " return [m.recallInbound('k0', '1@s.whatsapp.net') === undefined,"
            "  m.recallInbound('k' + m.INBOUND_CACHE_LIMIT, '1@s.whatsapp.net') !== undefined];"
            "})()"
        )
        assert evicted is True and kept is True

