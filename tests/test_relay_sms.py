"""A relay question delivered by SMS, and its `!relay reply` answer over SMS."""
import asyncio

import pytest

from istota import db
from istota.relay import relays
from istota.relay import requests
from istota.transport.sms import sms_conversation_token
from istota.transport.sms.providers._types import SmsDeliveryEvent, SmsSendFailure, SmsSendResult
from istota.transport.sms.webhook import handle_provider_event
from . import test_relay_questions
from .test_relay_questions import approve, hold, park
from .test_sms_core import _adapter, _inbound, _providers

setup = test_relay_questions.setup
BOB = '+15557654321'
SERVICE = '+15551230000'


@pytest.fixture
def sms(setup, monkeypatch):
    """SMS on, bob bound to a number, and a recorder standing in for the provider."""
    config = setup[0]
    config.sms.enabled = True
    config.sms.provider = 'twilio'
    config.sms.service_numbers = [SERVICE]
    config.sms.default_sender_number = SERVICE
    config.sms.max_segments = 4
    config.users['bob'].sms_phone_number = BOB
    sent = []
    outcome = {'value': SmsSendResult(provider_message_id='sms-question', status='accepted', reported_segments=2)}

    def send(message):
        sent.append(message)
        return outcome['value']
    monkeypatch.setattr('istota.transport.sms.providers.registry.make_provider_registry',
                        lambda config: _providers(_adapter(send)))
    return dict(config=config, sent=sent, outcome=outcome)


def rows(config, sql, params=()):
    with db.get_db(config.db_path) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def relay(config, relay_id):
    return rows(config, 'SELECT * FROM message_relays WHERE id=?', (relay_id,))[0]


def request(config, relay_id):
    return rows(config, 'SELECT * FROM whatsapp_skill_requests WHERE relay_id=?', (relay_id,))[0]


def release(setup, *, before_drain=None):
    held = hold(setup, via='sms')
    assert held['status'] == 'held'
    park(setup)
    approve(setup)
    if before_drain is not None:
        before_drain()
    asyncio.run(requests.drain_requests(setup[0]))
    return held


def inbound(config, text, message_id='bob-1'):
    event = _inbound(from_number=BOB, to_number=SERVICE, text=text, provider_message_id=message_id,
                     provider_event_id='event-' + message_id)
    with db.get_db(config.db_path) as conn:
        return handle_provider_event(conn, config, event)


class TestTheQuestionSend:
    def test_one_exact_send_through_the_ledger_however_often_the_drain_runs(self, setup, sms):
        config = sms['config']
        held = release(setup)
        asyncio.run(requests.drain_requests(config))
        assert len(sms['sent']) == 1
        (message,) = sms['sent']
        assert message.to_number == BOB
        assert message.text == request(config, held['relay_id'])['service_body']
        assert 'What time?' in message.text
        assert f"send !relay reply {held['relay_id']} <answer>." in message.text
        (ledger,) = rows(config, 'SELECT * FROM sent_sms')
        assert ledger['logical_key'] == 'relay-question:' + held['relay_id']
        assert (ledger['user_id'], ledger['task_id'], ledger['status']) == ('bob', None, 'accepted')
        assert relay(config, held['relay_id'])['state'] == 'waiting'
        assert request(config, held['relay_id'])['state'] == 'sent'

    def test_the_recipient_notice_is_written_and_not_pushed(self, setup, sms):
        config = sms['config']
        held = release(setup)
        (notice,) = rows(config, "SELECT * FROM notifications WHERE source='relay_question'")
        assert notice['user_id'] == 'bob' and notice['object_id'] == held['relay_id']
        assert notice['last_delivered_at'] is None
        assert 'What time?' not in notice['body']

    def test_the_preview_names_the_destination_and_never_the_number(self, setup, sms):
        held = hold(setup, via='sms')
        assert "through Bob's SMS?" in held['preview']
        assert BOB not in held['preview']

    @pytest.mark.parametrize('change', ['number', 'removed', 'disabled'])
    def test_a_number_changed_after_approval_is_refused(self, setup, sms, change):
        config = sms['config']

        def alter():
            if change == 'number':
                config.users['bob'].sms_phone_number = '+15550000000'
            elif change == 'removed':
                config.users['bob'].sms_phone_number = ''
            else:
                config.sms.enabled = False
        held = release(setup, before_drain=alter)
        assert not sms['sent']
        assert relay(config, held['relay_id'])['state'] == 'failed'
        assert request(config, held['relay_id'])['error_code'] == 'binding_changed'

    def test_a_definite_failure_closes_the_relay(self, setup, sms):
        config = sms['config']
        sms['outcome']['value'] = SmsSendFailure(True, 'invalid_number', False, 'rejected')
        held = release(setup)
        assert len(sms['sent']) == 1
        assert relay(config, held['relay_id'])['state'] == 'failed'

    def test_an_unknown_outcome_is_uncertain_and_never_resent(self, setup, sms):
        config = sms['config']
        sms['outcome']['value'] = SmsSendFailure(False, None, False, 'timeout')
        held = release(setup)
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE whatsapp_skill_requests SET updated_at=datetime('now','-1 hour')")
        asyncio.run(requests.drain_requests(config))
        assert len(sms['sent']) == 1
        assert relay(config, held['relay_id'])['state'] == 'uncertain'
        assert request(config, held['relay_id'])['state'] == 'uncertain'

    def test_an_opted_out_number_is_not_sent_and_closes_the_relay(self, setup, sms):
        config = sms['config']
        with db.get_db(config.db_path) as conn:
            conn.execute("INSERT INTO sms_opt_outs (phone_number,opted_out_at,updated_at) "
                         "VALUES (?,datetime('now'),datetime('now'))", (BOB,))
        held = release(setup)
        assert not sms['sent']
        assert relay(config, held['relay_id'])['state'] == 'failed'

    def test_a_failed_receipt_closes_a_waiting_relay(self, setup, sms):
        config = sms['config']
        held = release(setup)
        with db.get_db(config.db_path) as conn:
            handle_provider_event(conn, config, SmsDeliveryEvent(
                provider='twilio', provider_event_id='status-1', provider_message_id='sms-question',
                status='failed', error_code='carrier_rejected', reported_segments=None))
        assert relay(config, held['relay_id'])['state'] == 'failed'

    def test_an_sms_question_is_not_admitted_by_a_whatsapp_send(self, setup, sms):
        config = sms['config']
        held = hold(setup, via='sms')
        park(setup)
        approve(setup)
        with db.get_db(config.db_path) as conn:
            with pytest.raises(requests.RequestError, match='request_unavailable'):
                requests.admit_request(conn, config, request_id=held['request_id'], user_id='bob',
                                       logical_key='relay-question:' + held['relay_id'],
                                       send_kind='service', status='pending')


class TestTheSmsReply:
    def test_the_exact_text_answers_and_creates_one_sms_task(self, setup, sms):
        config = sms['config']
        held = release(setup)
        result = inbound(config, f"!relay reply {held['relay_id']}   Yes,  after 6 ")
        assert result.disposition == 'relay_answer'
        assert result.response_text == relays._REPLY_NOTICES['accepted']
        state = relay(config, held['relay_id'])
        assert state['state'] == 'answered' and state['answer_text'] == '  Yes,  after 6 '
        assert state['inbound_answer_id'] == 'sms:twilio:bob-1'
        assert state['recipient_task_id'] == result.task_id
        (task,) = rows(config, "SELECT * FROM tasks WHERE user_id='bob'")
        assert task['source_type'] == 'sms'
        with db.get_db(config.db_path) as conn:
            token = db.resolve_room_token(conn, 'sms', sms_conversation_token('bob'))
            assert task['conversation_token'] == token
            assert db.get_room(conn, token).name == 'SMS'
            assert db._canonical_room_token(conn, sms_conversation_token('bob'), cross_surface=False) == token
            assert conn.execute('SELECT count(*) FROM messages WHERE room_token=?', (token,)).fetchone()[0] == 1
        from istota.executor import build_prompt
        with db.get_db(config.db_path) as conn:
            composed = build_prompt(db.get_task(conn, result.task_id), [], config, conn=conn)
        assert '[END UNTRUSTED RELAY CONTEXT]' in composed.user and 'What time?' in composed.user
        (notice,) = rows(config, "SELECT state FROM notifications WHERE source='relay_question'")
        assert notice['state'] != 'open'

    def test_a_relay_reply_beats_a_parked_sms_confirmation(self, setup, sms):
        config = sms['config']
        held = release(setup)
        with db.get_db(config.db_path) as conn:
            parked = db.create_task(conn, prompt='delete the files', user_id='bob', source_type='sms',
                                    conversation_token=sms_conversation_token('bob'), output_target='sms')
            db.set_task_confirmation(conn, parked, 'Delete the files?')
        result = inbound(config, f"!relay reply {held['relay_id']} yes")
        assert result.disposition == 'relay_answer'
        assert relay(config, held['relay_id'])['answer_text'] == 'yes'
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, parked)
        assert task.status == 'pending_confirmation' and task.confirmed_at is None

    @pytest.mark.parametrize('relay_id', ['no-such-relay', 'foreign'])
    def test_an_unknown_or_foreign_id_is_unavailable(self, setup, sms, relay_id):
        config = sms['config']
        held = release(setup)
        if relay_id == 'foreign':
            config.users['bob'].sms_phone_number = ''
            config.users['alice'].sms_phone_number = BOB
            relay_id = held['relay_id']
        result = inbound(config, f'!relay reply {relay_id} Yes')
        assert result.disposition == 'relay_rejected'
        assert result.response_text == relays._REPLY_NOTICES['unavailable']
        assert result.task_id is not None
        assert relay(config, held['relay_id'])['state'] == 'waiting'

    def test_a_second_answer_is_refused(self, setup, sms):
        config = sms['config']
        held = release(setup)
        inbound(config, f"!relay reply {held['relay_id']} Yes", message_id='bob-1')
        result = inbound(config, f"!relay reply {held['relay_id']} No", message_id='bob-2')
        assert result.response_text == relays._REPLY_NOTICES['answered']
        assert relay(config, held['relay_id'])['answer_text'] == 'Yes'

    def test_a_room_relay_is_not_answerable_by_sms(self, setup, sms):
        config = sms['config']
        with db.get_db(config.db_path) as conn:
            db.create_web_chat_room(conn, 'bob', 'assistant')
        held = hold(setup, via='room')
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE message_relays SET state='waiting',expires_at=datetime('now','+1 hour') WHERE id=?",
                         (held['relay_id'],))
        result = inbound(config, f"!relay reply {held['relay_id']} Yes")
        assert result.response_text == relays._REPLY_NOTICES['unavailable']
        assert relay(config, held['relay_id'])['state'] == 'waiting'

    def test_a_redelivered_reply_is_a_duplicate(self, setup, sms):
        config = sms['config']
        held = release(setup)
        inbound(config, f"!relay reply {held['relay_id']} Yes")
        again = inbound(config, f"!relay reply {held['relay_id']} Yes")
        assert again.disposition == 'duplicate'
        assert len(rows(config, "SELECT 1 FROM tasks WHERE user_id='bob'")) == 1

    def test_ordinary_text_is_not_a_relay_reply(self, setup, sms):
        config = sms['config']
        release(setup)
        result = inbound(config, 'what is on my calendar')
        assert result.disposition == 'task'
