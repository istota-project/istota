"""A relay question delivered into the recipient's room, and its Talk mirror."""
import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from istota import db, message_relays as relays, whatsapp_requests as requests
from istota.notification_resolvers import relay_question
from . import test_relay_questions
from .support.talk_double import FakeTalkClient, talk_bot_client
from .test_relay_questions import hold, park, approve

setup = test_relay_questions.setup
BOT = [{'actorType': 'users', 'actorId': 'bob'}, {'actorType': 'users', 'actorId': 'bot'}]


@pytest.fixture
def room(setup, monkeypatch):
    """Bob's private room, a Talk double, and a recorder for notification pushes."""
    config = setup[0]
    config.nextcloud.url = 'https://cloud.example.com'
    config.nextcloud.username = 'bot'
    with db.get_db(config.db_path) as conn:
        token = db.create_web_chat_room(conn, 'bob', 'assistant').token
    talk = FakeTalkClient(config.db_path)
    participants = AsyncMock(return_value=BOT)
    monkeypatch.setattr('istota.talk.TalkClient.get_participants', participants)
    pushes = []

    def send_notification(config, user_id, message, **kwargs):
        pushes.append((user_id, message, kwargs))
        return True
    monkeypatch.setattr('istota.notifications.send_notification', send_notification)
    with patch('istota.transport.talk.get_talk_client', talk_bot_client(talk)):
        yield dict(config=config, token=token, talk=talk, pushes=pushes, participants=participants)


def bind_talk(room, ref='bob-talk'):
    with db.get_db(room['config'].db_path) as conn:
        db.add_room_binding(conn, room['token'], 'talk', ref)


def release(setup):
    held = hold(setup, via=None)
    assert held['status'] == 'held'
    park(setup)
    approve(setup)
    asyncio.run(requests.drain_requests(setup[0]))
    return held


def rows(config, sql, params=()):
    with db.get_db(config.db_path) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def question_rows(config, relay_id):
    return rows(config, 'SELECT * FROM messages WHERE delivery_reference=?', ('relay-question:' + relay_id,))


def relay(config, relay_id):
    return rows(config, 'SELECT * FROM message_relays WHERE id=?', (relay_id,))[0]


def request(config, relay_id):
    return rows(config, 'SELECT * FROM whatsapp_skill_requests WHERE relay_id=?', (relay_id,))[0]


class TestTheWebRow:
    def test_one_row_in_the_recipients_room_however_often_the_drain_runs(self, setup, room):
        config, sent = room['config'], setup[3]
        held = release(setup)
        asyncio.run(requests.drain_requests(config))
        asyncio.run(requests.drain_requests(config))
        (row,) = question_rows(config, held['relay_id'])
        assert row['room_token'] == room['token'] and row['role'] == 'system'
        assert 'Alice Example (alice):\n\nWhat time?\n\n' in row['body']
        assert row['body'].endswith('To answer Alice Example, reply to this message. Only that answer will be shared.')
        state = relay(config, held['relay_id'])
        assert state['state'] == 'waiting' and state['question_message_id'] == row['id']
        assert state['question_talk_id'] is None and state['surface'] == 'room'
        assert request(config, held['relay_id'])['state'] == 'sent'
        assert not sent and room['talk'].calls == []

    def test_the_recipient_is_notified_once_and_pushed_without_the_question(self, setup, room):
        config = room['config']
        held = release(setup)
        asyncio.run(requests.drain_requests(config))
        (notice,) = rows(config, "SELECT * FROM notifications WHERE source='relay_question'")
        assert notice['user_id'] == 'bob' and notice['object_id'] == held['relay_id']
        assert notice['room_token'] == room['token'] and notice['last_delivered_at']
        assert notice['title'] == 'Alice Example asked you a question'
        assert notice['body'] == "Reply to the message in Bob's room #assistant."
        pushed = [p for p in room['pushes'] if p[0] == 'bob']
        assert len(pushed) == 1 and 'What time?' not in json.dumps(pushed)


class TestTheTalkPost:
    def test_the_post_carries_the_reference_and_links_both_halves(self, setup, room):
        config = room['config']
        bind_talk(room)
        held = release(setup)
        asyncio.run(requests.drain_requests(config))
        (call,) = [c for c in room['talk'].calls if c.method == 'send_message']
        assert call.token == 'bob-talk' and not call.refused
        assert call.args['reference_id'] == 'relay-question:' + held['relay_id']
        (row,) = question_rows(config, held['relay_id'])
        assert call.args['message'] == row['body']
        state = relay(config, held['relay_id'])
        assert state['question_talk_id'] == call.sent_id
        assert json.loads(row['external_ids']) == {'talk': str(call.sent_id)}
        assert request(config, held['relay_id'])['state'] == 'sent'
        room['participants'].assert_any_await('bob-talk')

    def test_a_talk_failure_leaves_the_relay_answerable_on_web(self, setup, room, caplog):
        config = room['config']
        bind_talk(room)
        room['talk'].send_failures['bob-talk'] = ValueError('refused')
        held = release(setup)
        state = relay(config, held['relay_id'])
        assert state['state'] == 'waiting' and state['question_talk_id'] is None
        assert len(question_rows(config, held['relay_id'])) == 1
        assert request(config, held['relay_id'])['state'] == 'sent'
        assert 'not posted to Talk' in caplog.text and 'What time?' not in caplog.text

    @pytest.mark.parametrize('landed', [True, False])
    def test_recovery_after_a_crash_reads_back_and_never_posts_again(self, setup, room, monkeypatch, landed):
        config, talk = room['config'], room['talk']
        bind_talk(room)
        original = relays._post_room_question

        async def crash(*args, **kwargs):
            raise RuntimeError('daemon died between the row and the post')
        monkeypatch.setattr(relays, '_post_room_question', crash)
        with pytest.raises(RuntimeError):
            release(setup)
        (held,) = rows(config, 'SELECT relay_id FROM whatsapp_skill_requests')
        relay_id = held['relay_id']
        assert request(config, relay_id)['state'] == 'sending'
        assert relay(config, relay_id)['state'] == 'waiting'
        monkeypatch.setattr(relays, '_post_room_question', original)
        # A live claim is left alone; only a stale one is recovered.
        asyncio.run(requests.drain_requests(config))
        assert request(config, relay_id)['state'] == 'sending'
        if landed:
            talk.messages['bob-talk'] = [{'id': 4242, 'actorType': 'users', 'actorId': 'bot',
                                          'referenceId': 'relay-question:' + relay_id}]
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE whatsapp_skill_requests SET updated_at=datetime('now','-5 minutes')")
        asyncio.run(requests.drain_requests(config))
        assert not [c for c in talk.calls if c.method == 'send_message']
        assert request(config, relay_id)['state'] == 'sent'
        assert relay(config, relay_id)['question_talk_id'] == (4242 if landed else None)
        assert len(question_rows(config, relay_id)) == 1


class TestRefusalsAtDelivery:
    def test_a_shared_talk_room_closes_the_relay_as_not_private(self, setup, room):
        config = room['config']
        bind_talk(room)
        room['participants'].return_value = BOT + [{'actorType': 'users', 'actorId': 'carol'}]
        held = release(setup)
        assert relay(config, held['relay_id'])['state'] == 'failed'
        assert request(config, held['relay_id'])['error_code'] == 'destination_not_private'
        assert not question_rows(config, held['relay_id'])
        assert not [c for c in room['talk'].calls if c.method == 'send_message']
        assert not rows(config, "SELECT 1 FROM notifications WHERE source='relay_question'")
        assert rows(config, "SELECT 1 FROM notifications WHERE source='message_relay' AND user_id='alice'")

    def test_a_participant_fetch_failure_waits_for_the_next_tick(self, setup, room):
        # A Nextcloud 5xx or timeout says nothing about who is in the room, so
        # it must not close the question as not private; the drain retries it.
        config = room['config']
        bind_talk(room)
        room['participants'].side_effect = RuntimeError('503 Service Unavailable')
        held = release(setup)
        assert request(config, held['relay_id'])['state'] == 'queued'
        assert request(config, held['relay_id'])['error_code'] is None
        assert relay(config, held['relay_id'])['state'] == 'queued'
        assert not question_rows(config, held['relay_id'])
        assert not rows(config, "SELECT 1 FROM notifications WHERE source='message_relay'")
        room['participants'].side_effect = None
        asyncio.run(requests.drain_requests(config))
        assert relay(config, held['relay_id'])['state'] == 'waiting'
        assert len(question_rows(config, held['relay_id'])) == 1

    def test_a_fetch_failure_past_the_queue_deadline_expires(self, setup, room):
        config = room['config']
        bind_talk(room)
        room['participants'].side_effect = RuntimeError('timed out')
        held = release(setup)
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE whatsapp_skill_requests SET queue_deadline=datetime('now','-1 minute') "
                         "WHERE relay_id=?", (held['relay_id'],))
        asyncio.run(requests.drain_requests(config))
        assert request(config, held['relay_id'])['state'] == 'expired'
        assert request(config, held['relay_id'])['error_code'] == 'queue_expired'
        assert relay(config, held['relay_id'])['state'] == 'expired'
        assert not question_rows(config, held['relay_id'])

    def test_the_askers_origin_check_waits_too(self, setup, room):
        config = room['config']
        with db.get_db(config.db_path) as conn:
            db.add_room_binding(conn, setup[2], 'talk', 'alice-talk')
        room['participants'].return_value = [{'actorType': 'users', 'actorId': 'alice'},
                                             {'actorType': 'users', 'actorId': 'bot'}]
        held = hold(setup)  # WhatsApp, so only the asker-side check reaches Talk
        assert json.loads(relay(config, held['relay_id'])['origin'])['talk_ref'] == 'alice-talk'
        park(setup)
        approve(setup)
        room['participants'].side_effect = RuntimeError('502 Bad Gateway')
        asyncio.run(requests.drain_requests(config))
        assert request(config, held['relay_id'])['state'] == 'queued'
        assert not setup[3]
        room['participants'].side_effect = None
        asyncio.run(requests.drain_requests(config))
        assert len(setup[3]) == 1
        assert relay(config, held['relay_id'])['state'] == 'waiting'

    @pytest.mark.parametrize('change', ['talk_binding', 'shared', 'preference'])
    def test_a_destination_changed_after_approval_is_refused(self, setup, room, change):
        config = room['config']
        held = hold(setup, via=None)
        park(setup)
        approve(setup)
        with db.get_db(config.db_path) as conn:
            if change == 'talk_binding':
                db.add_room_binding(conn, room['token'], 'talk', 'bob-talk')
            elif change == 'shared':
                db.add_room_member(conn, room['token'], 'alice')
            else:
                conn.execute("INSERT OR IGNORE INTO user_profiles (user_id) VALUES ('bob')")
                conn.execute("UPDATE user_profiles SET relay_delivery='whatsapp' WHERE user_id='bob'")
        asyncio.run(requests.drain_requests(config))
        assert relay(config, held['relay_id'])['state'] == 'failed'
        assert request(config, held['relay_id'])['error_code'] == 'destination_changed'
        assert not question_rows(config, held['relay_id'])
        assert not setup[3]


class TestThePhoneDestinationNotice:
    def test_a_whatsapp_question_writes_the_notice_without_pushing_it(self, setup, room):
        config = room['config']
        held = hold(setup, via='whatsapp')
        park(setup)
        approve(setup)
        asyncio.run(requests.drain_requests(config))
        assert len(setup[3]) == 1
        (notice,) = rows(config, "SELECT * FROM notifications WHERE source='relay_question'")
        assert notice['user_id'] == 'bob' and notice['object_id'] == held['relay_id']
        assert notice['last_delivered_at'] is None and notice['room_token'] is None
        assert notice['body'] == 'Quote it on WhatsApp.'
        assert not [p for p in room['pushes'] if p[0] == 'bob']


def test_the_resolver_is_registered(setup):
    from istota import notification_sources
    assert notification_sources.get_resolver('relay_question') is relay_question.RESOLVER
