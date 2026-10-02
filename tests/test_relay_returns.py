"""Durable return delivery and scoped recovery of exact relay answers."""
import asyncio
import json
from unittest.mock import patch

import pytest

from istota import db
from istota.relay import relays
from istota.relay import requests
from . import test_relay_questions
from .test_relay_answers import question, event, receive

setup = test_relay_questions.setup
ANSWER = "  **Seven**\n`exact`  \n"


def answered(setup):
    relay = question(setup)
    assert receive(setup[0], event(setup[0], ANSWER)).disposition == 'relay_answer'
    return relay


def test_atomic_web_return_is_exact_and_deduplicated(setup):
    config, _, token, _ = setup
    relay = answered(setup)
    asyncio.run(relays.deliver_returns(config))
    asyncio.run(relays.deliver_returns(config))
    with db.get_db(config.db_path) as conn:
        row = relays.get_relay(conn, actor_user_id='alice', relay_id=relay)
        assert row['return_state'] == 'delivered'
        messages = conn.execute("SELECT * FROM messages WHERE delivery_reference=?", ('relay-return:' + relay,)).fetchall()
        assert len(messages) == 1 and messages[0]['room_token'] == token
        assert messages[0]['body'] == 'Answer from bob:\n\n' + ANSWER
        assert not conn.execute("SELECT 1 FROM messages WHERE room_token='shared'").fetchone()


def test_web_message_rolls_back_with_failed_settlement(setup):
    import sqlite3
    config = setup[0]
    relay = answered(setup)
    with db.get_db(config.db_path) as conn:
        conn.execute("CREATE TRIGGER fail_return BEFORE UPDATE OF return_state ON message_relays "
                     "WHEN NEW.return_state='delivered' BEGIN SELECT RAISE(ABORT,'failed'); END")
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(relays.deliver_returns(config))
    with db.get_db(config.db_path) as conn:
        assert not conn.execute('SELECT 1 FROM messages WHERE delivery_reference IS NOT NULL').fetchone()
        assert relays.get_relay(conn, actor_user_id='alice', relay_id=relay)['return_state'] == 'pending'
        conn.execute('DROP TRIGGER fail_return')
    asyncio.run(relays.deliver_returns(config))


def test_shared_origin_retains_answer_and_body_free_notice(setup):
    config, _, token, _ = setup
    relay = answered(setup)
    with db.get_db(config.db_path) as conn:
        db.add_room_member(conn, token, 'bob')
    asyncio.run(relays.deliver_returns(config))
    asyncio.run(relays.deliver_returns(config))
    with db.get_db(config.db_path) as conn:
        row = relays.get_relay(conn, actor_user_id='alice', relay_id=relay)
        assert row['return_state'] == 'blocked' and row['answer_text'] == ANSWER
        assert not conn.execute('SELECT 1 FROM messages WHERE delivery_reference IS NOT NULL').fetchone()
        notices = conn.execute("SELECT * FROM notifications WHERE source='message_relay'").fetchall()
        assert len(notices) == 1 and notices[0]['user_id'] == 'alice'
        assert ANSWER not in str(dict(notices[0])) and 'What time?' not in str(dict(notices[0]))


def test_expiry_and_retention_run_without_send_queue(setup):
    config = setup[0]
    relay = question(setup)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE message_relays SET expires_at=datetime('now','-1 second')")
    asyncio.run(requests.drain_requests(config))
    with db.get_db(config.db_path) as conn:
        assert relays.get_relay(conn, actor_user_id='alice', relay_id=relay)['state'] == 'expired'
        assert conn.execute("SELECT count(*) FROM notifications WHERE source='message_relay'").fetchone()[0] == 1
        conn.execute("UPDATE message_relays SET content_expires_at=datetime('now','-1 second')")
    asyncio.run(requests.drain_requests(config))
    with db.get_db(config.db_path) as conn:
        row = relays.get_relay(conn, actor_user_id='alice', relay_id=relay)
        assert row['question'] is None and row['content_cleared_at']
        assert conn.execute('SELECT content_hash FROM whatsapp_skill_requests').fetchone()[0]


def test_late_failed_receipt_closes_only_unanswered_question(setup):
    from istota.transport.whatsapp.outbound import apply_delivery_event
    from istota.transport.whatsapp._types import WhatsAppDeliveryEvent
    config = setup[0]
    relay = question(setup)
    with db.get_db(config.db_path) as conn:
        apply_delivery_event(conn, config, WhatsAppDeliveryEvent(message_id='question-id', status='failed', waba_id='', phone_number_id='', recipient_id='', occurred_at=None, error_code=None, billable=None, pricing_model=None, pricing_category=None, pricing_type=None))
        assert relays.get_relay(conn, actor_user_id='alice', relay_id=relay)['state'] == 'failed'


def test_failed_receipt_cannot_regress_committed_answer(setup):
    from istota.transport.whatsapp.outbound import apply_delivery_event
    from istota.transport.whatsapp._types import WhatsAppDeliveryEvent
    config = setup[0]
    relay = answered(setup)
    with db.get_db(config.db_path) as conn:
        apply_delivery_event(conn, config, WhatsAppDeliveryEvent(message_id='question-id', status='failed', waba_id='', phone_number_id='', recipient_id='', occurred_at=None, error_code=None, billable=None, pricing_model=None, pricing_category=None, pricing_type=None))
        row = relays.get_relay(conn, actor_user_id='alice', relay_id=relay)
        assert row['state'] == 'answered' and row['answer_text'] == ANSWER
    asyncio.run(relays.deliver_returns(config))


def origin_setup(setup, surface, monkeypatch):
    from unittest.mock import AsyncMock
    from istota.transport.whatsapp import whatsapp_conversation_token
    from istota.transport.sms import sms_conversation_token
    from .test_whatsapp_delivery import _bind
    config, ident, token, _ = setup
    if surface == 'whatsapp':
        if config.whatsapp.provider == 'whatsapp_cloud':
            _bind(config, 'alice', bsuid='US.1111111111', bootstrap_phone_number='+15551230001')
        else:
            with db.get_db(config.db_path) as conn:
                db.set_whatsapp_binding(conn, 'alice', bootstrap_phone_number='+15551230001')
                db.latch_whatsapp_jid(conn, 'alice', jid='15551230001@s.whatsapp.net')
        token = whatsapp_conversation_token('alice')
    elif surface == 'sms':
        config.sms.enabled = True
        config.sms.service_numbers = ['+15551230000']
        config.sms.default_sender_number = '+15551230000'
        config.users['alice'].sms_phone_number = '+15551230001'
        token = sms_conversation_token('alice')
    elif surface == 'talk':
        config.nextcloud.url = 'https://cloud.example.com'
        config.nextcloud.username = 'bot'
        with db.get_db(config.db_path) as conn:
            db.add_room_binding(conn, token, 'talk', 'private-talk')
        monkeypatch.setattr('istota.talk.TalkClient.get_participants', AsyncMock(return_value=[
            {'actorType': 'users', 'actorId': 'alice'}, {'actorType': 'users', 'actorId': 'bot'}]))
    with db.get_db(config.db_path) as conn:
        conn.execute('UPDATE tasks SET source_type=?,conversation_token=? WHERE id=?', (surface, token, ident))


def test_whatsapp_return_uses_exact_body_and_own_ledger(setup, monkeypatch):
    from istota.transport.whatsapp._types import WhatsAppSendResult
    origin_setup(setup, 'whatsapp', monkeypatch)
    config = setup[0]
    relay = answered(setup)
    sent = []
    async def send(config, message):
        sent.append(message)
        return WhatsAppSendResult(message_id='return-id')
    monkeypatch.setattr('istota.transport.whatsapp.providers.whatsapp_cloud._send', send)
    from types import SimpleNamespace
    async def bridge_send(message):
        return await send(config, message)
    monkeypatch.setattr('istota.transport.whatsapp.baileys_bridge.active_bridge', lambda: SimpleNamespace(send=bridge_send))
    asyncio.run(relays.deliver_returns(config))
    asyncio.run(relays.deliver_returns(config))
    assert len(sent) == 1 and sent[0].text == 'Answer from bob:\n\n' + ANSWER
    with db.get_db(config.db_path) as conn:
        row = conn.execute('SELECT * FROM sent_whatsapp WHERE logical_key=?', ('relay-return:' + relay,)).fetchone()
        assert row['user_id'] == 'alice' and row['task_id'] is None
        assert relays.get_relay(conn, actor_user_id='alice', relay_id=relay)['return_state'] == 'delivered'


def test_sms_return_preserves_exact_text_and_ledger(setup, monkeypatch):
    from .test_sms_core import _adapter, _providers
    from istota.transport.sms.providers._types import SmsSendResult
    origin_setup(setup, 'sms', monkeypatch)
    config = setup[0]
    relay = answered(setup)
    sent = []
    def send(message):
        sent.append(message)
        return SmsSendResult(provider_message_id='return-id', status='accepted', reported_segments=1)
    monkeypatch.setattr('istota.transport.sms.providers.registry.make_provider_registry', lambda config: _providers(_adapter(send)))
    asyncio.run(relays.deliver_returns(config))
    asyncio.run(relays.deliver_returns(config))
    assert len(sent) == 1 and sent[0].text == 'Answer from bob:\n\n' + ANSWER
    with db.get_db(config.db_path) as conn:
        row = conn.execute('SELECT * FROM sent_sms WHERE logical_key=?', ('relay-return:' + relay,)).fetchone()
        assert row['user_id'] == 'alice' and row['task_id'] is None
        assert relays.get_relay(conn, actor_user_id='alice', relay_id=relay)['return_state'] == 'delivered'


@pytest.mark.parametrize('settled', [False, True])
def test_talk_restart_uses_readback_and_never_posts_again(setup, monkeypatch, settled):
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    origin_setup(setup, 'talk', monkeypatch)
    config = setup[0]
    relay = answered(setup)
    post = AsyncMock(return_value=42)
    history = [{'id': 42, 'actorType': 'users', 'actorId': 'bot', 'referenceId': 'relay-return:' + relay}] if settled else []
    client = SimpleNamespace(send_message=post, fetch_chat_history=AsyncMock(return_value=history))
    monkeypatch.setattr('istota.transport.talk.get_talk_client', lambda config: client)
    with patch('istota.relay.relays._record_return', side_effect=RuntimeError('crash after send')):
        with pytest.raises(RuntimeError):
            asyncio.run(relays.deliver_returns(config))
    assert post.await_count == 1
    assert post.call_args.args == ('private-talk', 'Answer from bob:\n\n' + ANSWER)
    assert post.call_args.kwargs['reference_id'] == 'relay-return:' + relay
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE message_relays SET return_claimed_at=datetime('now','-3 minutes')")
    asyncio.run(relays.deliver_returns(config))
    asyncio.run(relays.deliver_returns(config))
    assert post.await_count == 1
    with db.get_db(config.db_path) as conn:
        assert relays.get_relay(conn, actor_user_id='alice', relay_id=relay)['return_state'] == ('delivered' if settled else 'uncertain')


def test_external_origin_changed_during_talk_privacy_check(setup, monkeypatch):
    from unittest.mock import AsyncMock
    origin_setup(setup, 'talk', monkeypatch)
    config, _, token, _ = setup
    relay = answered(setup)
    async def participants(*args, **kwargs):
        with db.get_db(config.db_path) as conn:
            db.add_room_member(conn, token, 'bob')
        return [{'actorType': 'users', 'actorId': 'alice'}, {'actorType': 'users', 'actorId': 'bot'}]
    monkeypatch.setattr('istota.talk.TalkClient.get_participants', participants)
    delivery = AsyncMock()
    monkeypatch.setattr('istota.transport.talk.TalkTransport.deliver', delivery)
    asyncio.run(relays.deliver_returns(config))
    assert not delivery.called
    with db.get_db(config.db_path) as conn:
        assert relays.get_relay(conn, actor_user_id='alice', relay_id=relay)['return_state'] == 'blocked'


def test_late_failed_return_receipt_changes_outcome_not_answer(setup, monkeypatch):
    from istota.transport.whatsapp.outbound import apply_delivery_event
    from istota.transport.whatsapp._types import WhatsAppDeliveryEvent, WhatsAppSendResult
    origin_setup(setup, 'whatsapp', monkeypatch)
    config = setup[0]
    relay = answered(setup)
    from types import SimpleNamespace
    async def send(*args):
        return WhatsAppSendResult(message_id='return-id')
    monkeypatch.setattr('istota.transport.whatsapp.providers.whatsapp_cloud._send', send)
    monkeypatch.setattr('istota.transport.whatsapp.baileys_bridge.active_bridge', lambda: SimpleNamespace(send=send))
    asyncio.run(relays.deliver_returns(config))
    with db.get_db(config.db_path) as conn:
        apply_delivery_event(conn, config, WhatsAppDeliveryEvent(message_id='return-id', status='failed', waba_id='', phone_number_id='', recipient_id='', occurred_at=None, error_code=None, billable=None, pricing_model=None, pricing_category=None, pricing_type=None))
        row = relays.get_relay(conn, actor_user_id='alice', relay_id=relay)
        assert row['return_state'] == 'blocked' and row['state'] == 'answered' and row['answer_text'] == ANSWER


@pytest.mark.parametrize('reason', ['opt_out', 'binding', 'closed_window'])
def test_whatsapp_blocked_return_never_sends_or_changes_answer(setup, monkeypatch, reason):
    from istota.config import WhatsAppTemplateConfig
    origin_setup(setup, 'whatsapp', monkeypatch)
    config = setup[0]
    if reason == 'closed_window' and config.whatsapp.provider != 'whatsapp_cloud':
        pytest.skip('Cloud service window only')
    relay = answered(setup)
    before = len(setup[3])
    with db.get_db(config.db_path) as conn:
        if reason == 'opt_out':
            conn.execute("UPDATE whatsapp_user_bindings SET opted_out_at=datetime('now') WHERE user_id='alice'")
        elif reason == 'binding':
            conn.execute("UPDATE whatsapp_user_bindings SET send_id='replacement' WHERE user_id='alice'")
        else:
            config.whatsapp.cloud.billing_policy = 'allow_paid'
            config.whatsapp.cloud.fallback_template = WhatsAppTemplateConfig(name='notice', language='en_US')
            conn.execute("UPDATE whatsapp_user_bindings SET last_user_message_at=datetime('now','-2 days') WHERE user_id='alice'")
    asyncio.run(relays.deliver_returns(config))
    assert len(setup[3]) == before
    with db.get_db(config.db_path) as conn:
        row = relays.get_relay(conn, actor_user_id='alice', relay_id=relay)
        assert row['return_state'] == 'blocked' and row['answer_text'] == ANSWER


def test_binding_replaced_between_return_ledger_claim_and_destination(setup, monkeypatch):
    from istota.transport.whatsapp import outbound
    origin_setup(setup, 'whatsapp', monkeypatch)
    config = setup[0]
    relay = answered(setup)
    before = len(setup[3])
    original = outbound._claim
    def replace_after_claim(*args, **kwargs):
        result = original(*args, **kwargs)
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE whatsapp_user_bindings SET send_id='replacement',jid='15551239999@s.whatsapp.net' WHERE user_id='alice'")
        return result
    monkeypatch.setattr(outbound, '_claim', replace_after_claim)
    asyncio.run(relays.deliver_returns(config))
    assert len(setup[3]) == before
    with db.get_db(config.db_path) as conn:
        assert relays.get_relay(conn, actor_user_id='alice', relay_id=relay)['return_state'] == 'blocked'


def test_private_scoped_show_and_skill_status_include_retained_answer(setup, monkeypatch):
    from istota.commands import dispatch
    from istota.skills.relay import _dispatch
    from argparse import Namespace
    config, ident, token, _ = setup
    relay = answered(setup)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
        request_id = conn.execute('SELECT request_id FROM message_relays WHERE id=?', (relay,)).fetchone()[0]
    result = asyncio.run(dispatch(config, 'alice', token, '!relay show ' + relay, surface='web'))
    assert ANSWER in result.text and 'Content retained until' in result.text
    monkeypatch.setattr('istota.config.load_config', lambda: config)
    monkeypatch.setenv('ISTOTA_DB_PATH', str(config.db_path))
    monkeypatch.setenv('ISTOTA_USER_ID', 'alice')
    monkeypatch.setenv('ISTOTA_TASK_ID', str(ident))
    status = _dispatch(Namespace(command='status', request_id=request_id))
    assert status['request']['relay']['answer_text'] == ANSWER
    assert 'binding_fingerprint' not in json.dumps(status)
    assert _dispatch(Namespace(command='list'))['relays'][0]['id'] == relay
    with db.get_db(config.db_path) as conn:
        db.add_room_member(conn, token, 'bob')
    result = asyncio.run(dispatch(config, 'alice', token, '!relay show ' + relay, surface='web'))
    assert ANSWER not in result.text and 'private' in result.text
    with pytest.raises(requests.RequestError, match='unsupported_origin'):
        _dispatch(Namespace(command='status', request_id=request_id))
    with pytest.raises(requests.RequestError, match='unsupported_origin'):
        _dispatch(Namespace(command='list'))


def test_ask_cli_enqueues_held_question_and_waits_for_exact_approval(setup, monkeypatch, capsys):
    from istota.skills.relay import main
    config, ident, _, sent = setup
    monkeypatch.setattr('istota.config.load_config', lambda: config)
    monkeypatch.setenv('ISTOTA_DB_PATH', str(config.db_path))
    monkeypatch.setenv('ISTOTA_USER_ID', 'alice')
    monkeypatch.setenv('ISTOTA_TASK_ID', str(ident))
    main(['ask', 'bob', '--request-key', 'cli-question', '--via', 'whatsapp', 'What time?'])
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'held' and result['needs_confirmation']
    asyncio.run(requests.drain_requests(config))
    assert not sent


def test_uncertain_return_never_retries_and_notice_stays_seen(setup, monkeypatch):
    from istota.transport.whatsapp._types import WhatsAppSendFailure
    from types import SimpleNamespace
    origin_setup(setup, 'whatsapp', monkeypatch)
    config = setup[0]
    relay = answered(setup)
    sent = []
    async def fail(*args):
        sent.append(args)
        return WhatsAppSendFailure(definite=False, error_code=None, safe_reason='unknown')
    monkeypatch.setattr('istota.transport.whatsapp.providers.whatsapp_cloud._send', fail)
    monkeypatch.setattr('istota.transport.whatsapp.baileys_bridge.active_bridge', lambda: SimpleNamespace(send=fail))
    asyncio.run(relays.deliver_returns(config))
    with db.get_db(config.db_path) as conn:
        assert relays.get_relay(conn, actor_user_id='alice', relay_id=relay)['return_state'] == 'uncertain'
        conn.execute("UPDATE notifications SET state='resolved' WHERE source='message_relay'")
        conn.execute("UPDATE message_relays SET return_claimed_at=datetime('now','-3 minutes')")
    asyncio.run(relays.deliver_returns(config))
    assert len(sent) == 1
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT state FROM notifications WHERE source='message_relay'").fetchone()[0] == 'resolved'


def test_concurrent_pollers_write_one_web_return(setup):
    from concurrent.futures import ThreadPoolExecutor
    config = setup[0]
    relay = answered(setup)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: asyncio.run(relays.deliver_returns(config)), range(2)))
    with db.get_db(config.db_path) as conn:
        assert conn.execute('SELECT count(*) FROM messages WHERE delivery_reference=?', ('relay-return:' + relay,)).fetchone()[0] == 1


def test_revocation_after_answer_does_not_retract_its_return(setup):
    config = setup[0]
    relay = answered(setup)
    with db.get_db(config.db_path) as conn:
        relays.block(conn, actor_user_id='bob', asker_user_id='alice')
    asyncio.run(relays.deliver_returns(config))
    with db.get_db(config.db_path) as conn:
        assert relays.get_relay(conn, actor_user_id='alice', relay_id=relay)['return_state'] == 'delivered'


def test_resolver_never_uses_stored_private_notification_text(setup):
    from istota.notifications.resolvers.message_relay import RESOLVER, write
    from istota.notifications.store import _row_to_notification
    config = setup[0]
    relay = answered(setup)
    with db.get_db(config.db_path) as conn:
        row = conn.execute('SELECT * FROM message_relays WHERE id=?', (relay,)).fetchone()
        write(conn, row)
        conn.execute("UPDATE notifications SET body=?,title=? WHERE source='message_relay'", (ANSWER, 'What time?'))
        notice = conn.execute("SELECT * FROM notifications WHERE source='message_relay'").fetchone()
        view = RESOLVER.resolve(config, conn, _row_to_notification(notice))
        assert ANSWER not in str(view) and 'What time?' not in str(view)
        from dataclasses import replace
        assert RESOLVER.resolve(config, conn, replace(_row_to_notification(notice), user_id='bob')) is None


def test_content_deadline_expires_undelivered_answer_before_send(setup):
    config = setup[0]
    relay = answered(setup)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE message_relays SET content_expires_at=datetime('now','-1 second')")
    asyncio.run(requests.drain_requests(config))
    with db.get_db(config.db_path) as conn:
        row = relays.get_relay(conn, actor_user_id='alice', relay_id=relay)
        assert row['return_state'] == 'expired' and row['answer_text'] is None
        assert not conn.execute('SELECT 1 FROM messages WHERE delivery_reference IS NOT NULL').fetchone()


@pytest.mark.parametrize('surface', ['whatsapp', 'sms'])
def test_external_ledger_settlement_recovers_after_process_crash(setup, monkeypatch, surface):
    from istota.transport.whatsapp._types import WhatsAppSendResult
    from istota.transport.sms.providers._types import SmsSendResult
    from .test_sms_core import _adapter, _providers
    from types import SimpleNamespace
    origin_setup(setup, surface, monkeypatch)
    config = setup[0]
    relay = answered(setup)
    sent = []
    async def wa_send(*args):
        sent.append(args)
        return WhatsAppSendResult(message_id='return-id')
    def sms_send(message):
        sent.append(message)
        return SmsSendResult(provider_message_id='return-id', status='accepted', reported_segments=1)
    monkeypatch.setattr('istota.transport.whatsapp.providers.whatsapp_cloud._send', wa_send)
    monkeypatch.setattr('istota.transport.whatsapp.baileys_bridge.active_bridge', lambda: SimpleNamespace(send=wa_send))
    monkeypatch.setattr('istota.transport.sms.providers.registry.make_provider_registry', lambda config: _providers(_adapter(sms_send)))
    with patch('istota.relay.relays._record_return', side_effect=RuntimeError('crash after send')):
        with pytest.raises(RuntimeError):
            asyncio.run(relays.deliver_returns(config))
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE message_relays SET return_claimed_at=datetime('now','-3 minutes')")
    asyncio.run(relays.deliver_returns(config))
    assert len(sent) == 1
    with db.get_db(config.db_path) as conn:
        assert relays.get_relay(conn, actor_user_id='alice', relay_id=relay)['return_state'] == 'delivered'


def test_sms_final_destination_revalidates_frozen_origin(setup, monkeypatch):
    from .test_sms_core import _adapter, _providers
    from unittest.mock import Mock
    origin_setup(setup, 'sms', monkeypatch)
    config = setup[0]
    relay = answered(setup)
    send = Mock()
    providers = _providers(_adapter(send))
    original = providers.get
    def replace_binding(name):
        config.users['alice'].sms_phone_number = '+15551239999'
        return original(name)
    providers.get = replace_binding
    monkeypatch.setattr('istota.transport.sms.providers.registry.make_provider_registry', lambda config: providers)
    asyncio.run(relays.deliver_returns(config))
    assert not send.called
    with db.get_db(config.db_path) as conn:
        assert relays.get_relay(conn, actor_user_id='alice', relay_id=relay)['return_state'] == 'blocked'


def test_sms_late_failure_updates_return_and_keeps_answer(setup, monkeypatch):
    from .test_sms_core import _adapter, _providers
    from istota.transport.sms.providers._types import SmsSendResult, SmsDeliveryEvent
    from istota.transport.sms.outbound import apply_delivery_event
    origin_setup(setup, 'sms', monkeypatch)
    config = setup[0]
    relay = answered(setup)
    providers = _providers(_adapter(lambda message: SmsSendResult(provider_message_id='return-id', status='accepted', reported_segments=1)))
    monkeypatch.setattr('istota.transport.sms.providers.registry.make_provider_registry', lambda config: providers)
    asyncio.run(relays.deliver_returns(config))
    with db.get_db(config.db_path) as conn:
        apply_delivery_event(conn, SmsDeliveryEvent(provider='twilio', provider_message_id='return-id', provider_event_id='receipt', status='failed', error_code=None, reported_segments=1))
        row = relays.get_relay(conn, actor_user_id='alice', relay_id=relay)
        assert row['return_state'] == 'blocked' and row['answer_text'] == ANSWER


def test_external_return_concurrent_pollers_admit_one_provider_call(setup, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from istota.transport.whatsapp._types import WhatsAppSendResult
    from types import SimpleNamespace
    origin_setup(setup, 'whatsapp', monkeypatch)
    config = setup[0]
    relay = answered(setup)
    calls = []
    async def send(*args):
        calls.append(args)
        await asyncio.sleep(0.03)
        return WhatsAppSendResult(message_id='return-id')
    monkeypatch.setattr('istota.transport.whatsapp.providers.whatsapp_cloud._send', send)
    monkeypatch.setattr('istota.transport.whatsapp.baileys_bridge.active_bridge', lambda: SimpleNamespace(send=send))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: asyncio.run(relays.deliver_returns(config)), range(2)))
    assert len(calls) == 1
    with db.get_db(config.db_path) as conn:
        assert relays.get_relay(conn, actor_user_id='alice', relay_id=relay)['return_state'] == 'delivered'


def test_claim_timestamp_migrates_existing_relay_schema(setup):
    config = setup[0]
    relay = answered(setup)
    with db.get_db(config.db_path) as conn:
        conn.execute('ALTER TABLE message_relays DROP COLUMN return_claimed_at')
    db.init_db(config.db_path)
    db.init_db(config.db_path)
    with db.get_db(config.db_path) as conn:
        assert conn.execute('SELECT return_claimed_at FROM message_relays WHERE id=?', (relay,)).fetchone()[0] is None
        assert relays.get_relay(conn, actor_user_id='alice', relay_id=relay)['answer_text'] == ANSWER


def test_receipt_reconciles_uncertain_question_request_status(setup):
    from istota.transport.whatsapp.outbound import apply_delivery_event
    from istota.transport.whatsapp._types import WhatsAppDeliveryEvent
    config = setup[0]
    relay = question(setup)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE whatsapp_skill_requests SET state='uncertain'")
        conn.execute("UPDATE message_relays SET state='uncertain'")
        apply_delivery_event(conn, config, WhatsAppDeliveryEvent(message_id='question-id', status='delivered', waba_id='', phone_number_id='', recipient_id='', occurred_at=None, error_code=None, billable=None, pricing_model=None, pricing_category=None, pricing_type=None))
        assert conn.execute('SELECT state FROM whatsapp_skill_requests').fetchone()[0] == 'sent'
        assert relays.get_relay(conn, actor_user_id='alice', relay_id=relay)['state'] == 'waiting'


def test_explicit_answer_resolves_question_send_uncertainty(setup):
    config = setup[0]
    relay = question(setup)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE whatsapp_skill_requests SET state='uncertain',error_code='unknown'")
        conn.execute("UPDATE message_relays SET state='uncertain'")
    assert receive(config, event(config, f'!relay reply {relay} Yes', quote=None)).disposition == 'relay_answer'
    with db.get_db(config.db_path) as conn:
        assert conn.execute('SELECT state,error_code FROM whatsapp_skill_requests').fetchone()[:] == ('sent', None)


def test_bounded_expiry_and_cleanup_keep_request_tombstones(setup):
    from .test_relay_questions import hold, park, approve
    config = setup[0]
    first = question(setup)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE message_relays SET expires_at=datetime('now','-1 second')")
        assert relays.expire_relays(conn, limit=0) == 0
        assert relays.expire_relays(conn, limit=1) == 1
        conn.execute("UPDATE tasks SET status='running'")
    second = hold(setup, request_key='second')
    park(setup)
    approve(setup)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE message_relays SET expires_at=datetime('now','-1 second')")
        assert relays.expire_relays(conn, limit=1) == 1
        conn.execute("UPDATE message_relays SET content_expires_at=datetime('now','-1 second')")
        assert requests.cleanup_content(conn, limit=1) == 1
        assert conn.execute('SELECT count(*) FROM message_relays WHERE question IS NOT NULL').fetchone()[0] == 1
        assert requests.cleanup_content(conn, limit=1) == 1
        assert {row[0] for row in conn.execute('SELECT id FROM message_relays')} == {first, second['relay_id']}
        assert conn.execute('SELECT count(*) FROM whatsapp_skill_requests WHERE content_hash IS NOT NULL').fetchone()[0] == 2


def test_body_free_expiry_notice_targets_frozen_origin_then_deduplicates(setup, monkeypatch):
    from unittest.mock import Mock
    config, _, token, _ = setup
    question(setup)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE message_relays SET expires_at=datetime('now','-1 second')")
    send = Mock(return_value=True)
    monkeypatch.setattr('istota.notifications.delivery.send_notification', send)
    asyncio.run(requests.drain_requests(config))
    asyncio.run(requests.drain_requests(config))
    assert send.call_count == 1
    assert send.call_args.kwargs['surface'] == 'web:' + token
    assert 'What time?' not in str(send.call_args) and ANSWER not in str(send.call_args)
