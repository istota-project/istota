"""Answering a room relay question: web and Talk replies, and `!relay reply` in the room."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from istota import db
from istota.relay import relays
from istota.config import TalkConfig, WebConfig
from . import test_relay_room_delivery
from .test_relay_room_delivery import bind_talk, question_rows, relay, release, rows

setup = test_relay_room_delivery.setup
room = test_relay_room_delivery.room

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

needs_web = pytest.mark.skipif(not _has_web_deps, reason="web dependencies not installed")


def _web_config(config):
    config.web = WebConfig(
        enabled=True, port=8766, oauth2_provider='https://cloud.example.com',
        oauth2_client_id='istota-web', oauth2_client_secret='s', session_secret_key='test-session-key',
    )
    config.site.hostname = 'example.com'


def send_web(config, room_token, body, username='bob'):
    """One POST to the chat send endpoint, logged in as `username`."""
    from httpx import ASGITransport, AsyncClient
    from .test_web_chat_reply import ORIGIN, _login, _patch_app

    _web_config(config)
    app = _patch_app(config)
    with db.get_db(config.db_path) as conn:
        room_id = db.get_web_chat_room_by_token(conn, room_token).id

    async def run():
        async with AsyncClient(transport=ASGITransport(app=app), base_url='https://example.com') as client:
            cookies = await _login(client, username)
            return await client.post(f'/istota/api/chat/rooms/{room_id}/messages',
                                     json=body, cookies=cookies, headers=ORIGIN)
    return asyncio.run(run())


def send_talk(config, text, *, parent_id, message_id=900, token='bob-talk'):
    """One Talk message from bob through the poller's own filter chain."""
    from istota.transport.talk.inbound import _process_poll_results

    config.talk = TalkConfig(enabled=True, bot_username='bot')
    msg = {'id': message_id, 'actorId': 'bob', 'actorType': 'users', 'message': text,
           'messageType': 'comment', 'messageParameters': {}, 'timestamp': 1700000000,
           'parent': {'id': parent_id, 'message': 'the question'}}
    client = AsyncMock()
    return asyncio.run(_process_poll_results(config, client, [(token, [msg])], {token: 1}, {token: 'assistant'}))


def park_confirmation(config, conversation_token):
    with db.get_db(config.db_path) as conn:
        task_id = db.create_task(conn, prompt='delete the files', user_id='bob', source_type='web',
                                 conversation_token=conversation_token)
        db.set_task_confirmation(conn, task_id, 'Delete the files?')
    return task_id


def task(config, task_id):
    with db.get_db(config.db_path) as conn:
        return db.get_task(conn, task_id)


def notice_state(config, relay_id):
    (row,) = rows(config, "SELECT state FROM notifications WHERE source='relay_question' AND object_id=?", (relay_id,))
    return row['state']


@needs_web
class TestWebReplies:
    def test_the_exact_text_answers_and_creates_one_web_task(self, setup, room):
        config = room['config']
        held = release(setup)
        (question,) = question_rows(config, held['relay_id'])
        response = send_web(config, room['token'], {'text': '  Yes,  after 6\n', 'reply_to_msg_id': question['id']})
        assert response.status_code == 200
        task_id = response.json()['task_id']
        state = relay(config, held['relay_id'])
        assert state['state'] == 'answered' and state['answer_text'] == '  Yes,  after 6\n'
        assert state['recipient_task_id'] == task_id
        (user_row,) = rows(config, "SELECT id,reply_to_message_id FROM messages WHERE task_id=? AND role='user'", (task_id,))
        assert state['inbound_answer_id'] == f"web:{user_row['id']}"
        assert user_row['reply_to_message_id'] == question['id']
        recipient = task(config, task_id)
        assert recipient.source_type == 'web' and recipient.user_id == 'bob'
        assert recipient.reply_to_content is None
        assert len(rows(config, "SELECT 1 FROM tasks WHERE user_id='bob'")) == 1
        from istota.executor import build_prompt
        with db.get_db(config.db_path) as conn:
            composed = build_prompt(db.get_task(conn, task_id), [], config, conn=conn)
        assert '[END UNTRUSTED RELAY CONTEXT]' in composed.user and 'What time?' in composed.user
        assert notice_state(config, held['relay_id']) != 'open'

    def test_a_relay_reply_beats_a_parked_confirmation(self, setup, room):
        config = room['config']
        held = release(setup)
        (question,) = question_rows(config, held['relay_id'])
        parked = park_confirmation(config, room['token'])
        response = send_web(config, room['token'], {'text': 'yes', 'reply_to_msg_id': question['id']})
        assert response.json()['task_id'] is not None
        assert relay(config, held['relay_id'])['answer_text'] == 'yes'
        assert task(config, parked).confirmed_at is None
        assert task(config, parked).status != 'pending'

    def test_a_second_answer_is_refused_and_stays_an_ordinary_task(self, setup, room):
        config = room['config']
        held = release(setup)
        (question,) = question_rows(config, held['relay_id'])
        send_web(config, room['token'], {'text': 'First', 'reply_to_msg_id': question['id']})
        second = send_web(config, room['token'], {'text': 'Second', 'reply_to_msg_id': question['id']})
        assert relay(config, held['relay_id'])['answer_text'] == 'First'
        rejected = task(config, second.json()['task_id'])
        assert relays._REPLY_NOTICES['answered'] in rejected.reply_to_content
        assert rejected.prompt == 'Second'

    def test_a_reply_to_a_closed_question_carries_the_notice(self, setup, room):
        config = room['config']
        held = release(setup)
        (question,) = question_rows(config, held['relay_id'])
        with db.get_db(config.db_path) as conn:
            relays.cancel_relay(conn, actor_user_id='alice', relay_id=held['relay_id'])
        response = send_web(config, room['token'], {'text': 'yes', 'reply_to_msg_id': question['id']})
        rejected = task(config, response.json()['task_id'])
        assert relays._REPLY_NOTICES['closed'] in rejected.reply_to_content
        assert relay(config, held['relay_id'])['answer_text'] is None

    def test_a_reply_to_an_ordinary_row_is_an_ordinary_message(self, setup, room):
        config = room['config']
        held = release(setup)
        with db.get_db(config.db_path) as conn:
            other = db.add_message(conn, room['token'], role='assistant', body='Anything else?', origin_surface='web')
        response = send_web(config, room['token'], {'text': 'Yes, after 6', 'reply_to_msg_id': other})
        assert relay(config, held['relay_id'])['state'] == 'waiting'
        assert task(config, response.json()['task_id']).reply_to_content == 'Anything else?'


class TestTalkReplies:
    def test_a_reply_by_parent_id_answers_and_creates_one_talk_task(self, setup, room):
        config = room['config']
        bind_talk(room)
        held = release(setup)
        talk_id = relay(config, held['relay_id'])['question_talk_id']
        assert talk_id is not None
        (task_id,) = send_talk(config, 'Yes,  after 6', parent_id=talk_id)
        state = relay(config, held['relay_id'])
        assert state['state'] == 'answered' and state['answer_text'] == 'Yes,  after 6'
        assert state['inbound_answer_id'] == 'talk:900' and state['recipient_task_id'] == task_id
        recipient = task(config, task_id)
        assert recipient.source_type == 'talk' and recipient.reply_to_content is None
        assert notice_state(config, held['relay_id']) != 'open'

    def test_a_relay_reply_beats_a_parked_confirmation(self, setup, room):
        config = room['config']
        bind_talk(room)
        held = release(setup)
        talk_id = relay(config, held['relay_id'])['question_talk_id']
        parked = park_confirmation(config, room['token'])
        created = send_talk(config, 'yes', parent_id=talk_id)
        assert len(created) == 1
        assert relay(config, held['relay_id'])['answer_text'] == 'yes'
        assert task(config, parked).confirmed_at is None

    def test_a_talk_reply_after_a_web_answer_gets_answered(self, setup, room):
        config = room['config']
        bind_talk(room)
        held = release(setup)
        state = relay(config, held['relay_id'])
        with db.get_db(config.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            relays.accept_room_reply(conn, config, actor_user_id='bob', relay_id=held['relay_id'], surface='web',
                                     inbound_id=None, text='From web', channel=room['token'],
                                     reply_to_id=state['question_message_id'])
        (task_id,) = send_talk(config, 'From Talk', parent_id=state['question_talk_id'])
        assert relay(config, held['relay_id'])['answer_text'] == 'From web'
        assert relays._REPLY_NOTICES['answered'] in task(config, task_id).reply_to_content

    def test_a_reply_to_another_message_is_ordinary(self, setup, room):
        config = room['config']
        bind_talk(room)
        held = release(setup)
        (task_id,) = send_talk(config, 'Yes', parent_id=12345)
        assert relay(config, held['relay_id'])['state'] == 'waiting'
        assert task(config, task_id).reply_to_content == 'the question'


class TestTheReplyCommand:
    def dispatch(self, config, token, text):
        from istota.commands import dispatch
        return asyncio.run(dispatch(config, 'bob', token, text, surface='web')).text

    def test_it_answers_a_room_relay_from_its_room(self, setup, room):
        config = room['config']
        held = release(setup)
        text = self.dispatch(config, room['token'], f"!relay reply {held['relay_id']}  two  spaces")
        assert text == relays._REPLY_NOTICES['accepted']
        state = relay(config, held['relay_id'])
        assert state['answer_text'] == ' two  spaces' and state['inbound_answer_id'].startswith('web:command:')

    def test_it_is_refused_from_another_room(self, setup, room):
        config = room['config']
        held = release(setup)
        with db.get_db(config.db_path) as conn:
            other = db.create_web_chat_room(conn, 'bob', 'elsewhere').token
        text = self.dispatch(config, other, f"!relay reply {held['relay_id']} Yes")
        assert 'in the room its question reached you' in text
        assert relay(config, held['relay_id'])['answer_text'] is None

    def test_an_unknown_id_is_unavailable(self, setup, room):
        config = room['config']
        release(setup)
        text = self.dispatch(config, room['token'], '!relay reply nope Yes')
        assert text == relays._REPLY_NOTICES['unavailable']


def test_a_room_relay_is_not_answerable_over_whatsapp(setup, room):
    config = room['config']
    held = release(setup)
    with db.get_db(config.db_path) as conn:
        outcome = relays.accept_reply(conn, config, actor_user_id='bob', relay_id=held['relay_id'],
                                      surface='whatsapp', inbound_id='wa-1', text='Yes')
    assert outcome == 'unavailable'
    assert json.loads(relay(config, held['relay_id'])['destination'])['kind'] == 'room'
