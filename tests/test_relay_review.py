"""Regressions from the accumulated relay review."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from istota import db
from istota.relay import relays
from istota.relay import requests
from . import test_relay_questions, test_whatsapp_request_delivery
from .test_relay_questions import hold, approve
from .test_relay_returns import origin_setup

setup = test_relay_questions.setup
delivery = test_whatsapp_request_delivery.delivery


@pytest.mark.parametrize('surface', ['whatsapp', 'sms'])
def test_successive_phone_previews_use_the_request_identity(setup, monkeypatch, surface):
    config, ident, _, sent = setup
    origin_setup(setup, surface, monkeypatch)
    if surface == 'sms':
        from .test_sms_core import _adapter, _providers
        from istota.transport.sms.providers._types import SmsSendResult
        config.sms.max_segments = 10
        def send(message):
            sent.append(message)
            return SmsSendResult(provider_message_id='preview-' + str(len(sent)), status='accepted', reported_segments=1)
        monkeypatch.setattr('istota.transport.sms.providers.registry.make_provider_registry', lambda config: _providers(_adapter(send)))
    first = hold(setup, text='First question?')
    with db.get_db(config.db_path) as conn:
        task = db.get_task(conn, ident)
    assert asyncio.run(requests.present_question(config, task=task, success=True))
    approve(setup)
    with db.get_db(config.db_path) as conn:
        relays.cancel_relay(conn, actor_user_id='alice', relay_id=first['relay_id'])
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
    second = hold(setup, request_key='second', text='Second question?')
    assert asyncio.run(requests.present_question(config, task=task, success=True))
    assert len(sent) == 2
    assert 'First question?' in sent[0].text and 'Second question?' in sent[1].text
    with db.get_db(config.db_path) as conn:
        current = db.get_task(conn, ident)
        assert current.whatsapp_confirmation_request_id == second['request_id']
        assert current.confirmation_prompt == second['preview']


@pytest.mark.parametrize('invocation', ['istota-skill relay', 'python -m istota.skills.relay', "istota-skill rel''ay",
                                        'istota-skill whatsapp'])
@pytest.mark.parametrize('brain', ['claude', 'native'])
@pytest.mark.parametrize('description', ['', 'Ask Bob the Secret question?'])
def test_relay_tool_content_never_enters_progress_logs(setup, brain, description, invocation):
    from istota.agent.events import _describe_tool_use, _tool_invocation
    from istota.brain._events import parse_stream_line, ToolEndEvent, ToolProgressEvent, ToolUseEvent
    from istota.brain.native import _tool_use_event
    from istota.consumers.log_channel import LogChannelSubscriber
    from istota.events import EventWriter
    from istota.executor_stream import TaskStreamAdapter
    from istota.transport import Destination
    config, ident, _, _ = setup
    config.scheduler.progress_show_tool_use = True
    args = {'command': f"{invocation} ask bob --request-key q 'Secret question?'", 'description': description}
    with db.get_db(config.db_path) as conn:
        task = db.get_task(conn, ident)
    transport = SimpleNamespace(capabilities=SimpleNamespace(supports_edit=True), deliver=AsyncMock(return_value=1))
    subscriber = LogChannelSubscriber(config, task, [Destination('talk', 'shared-log')], 'Task',
                                      registry={'talk': transport})
    writer = EventWriter(ident, config.db_path)
    writer.subscribe(subscriber)
    adapter = TaskStreamAdapter(config, task, writer)
    if brain == 'claude':
        stream = parse_stream_line(json.dumps({'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 'tool-1', 'name': 'Bash', 'input': args}]}}))
    else:
        stream = _tool_use_event('Bash', _describe_tool_use('Bash', args), 'tool-1')
    adapter.on_event(stream)
    adapter.on_event(ToolProgressEvent(tool_name='Bash', tool_call_id='tool-1',
                                      text=json.dumps({'preview': 'Secret question?'})))
    adapter.on_event(ToolEndEvent(tool_name='Bash', tool_call_id='tool-1', success=True, duration_ms=1))
    assert transport.deliver.await_count == 1
    assert 'Secret question?' not in transport.deliver.call_args.args[1]
    assert 'Secret question?' not in (_tool_invocation('Bash', args) or '')
    with db.get_db(config.db_path) as conn:
        assert all('Secret question?' not in row[0] for row in conn.execute('SELECT payload FROM task_events'))
    adapter.on_event(ToolUseEvent(tool_name='Bash', tool_call_id='ordinary-tool', description='List files'))
    adapter.on_event(ToolProgressEvent(tool_name='Bash', tool_call_id='ordinary-tool', text='ordinary stdout'))
    with db.get_db(config.db_path) as conn:
        assert any('ordinary stdout' in row[0] for row in conn.execute('SELECT payload FROM task_events'))


@pytest.mark.parametrize('receipt,initial,expected', [('failed', 'sent', 'failed'), ('delivered', 'uncertain', 'sent')])
def test_self_send_late_receipt_updates_public_status_without_resend(delivery, receipt, initial, expected):
    from istota.transport.whatsapp._types import WhatsAppDeliveryEvent
    from istota.transport.whatsapp.outbound import apply_delivery_event
    from .test_whatsapp_request_delivery import enqueue, state
    config, ident, sent = delivery
    request = enqueue(config, ident)
    asyncio.run(requests.drain_requests(config))
    with db.get_db(config.db_path) as conn:
        conn.execute('UPDATE whatsapp_skill_requests SET state=? WHERE id=?', (initial, request['request_id']))
        apply_delivery_event(conn, config, WhatsAppDeliveryEvent(message_id='message-1', status=receipt,
            waba_id='', phone_number_id='', recipient_id='', occurred_at=None, error_code=None,
            billable=None, pricing_model=None, pricing_category=None, pricing_type=None))
    asyncio.run(requests.drain_requests(config))
    assert state(config, request['request_id'])['state'] == expected
    assert len(sent) == 1


def test_cancel_during_completion_finishes_task_and_events(setup):
    from istota.scheduler import process_one_task
    config, ident, _, _ = setup
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET status='pending' WHERE id=?", (ident,))
    def execute(*args, **kwargs):
        hold(setup)
        with db.get_db(config.db_path) as conn:
            conn.execute('UPDATE tasks SET cancel_requested=1 WHERE id=?', (ident,))
        return True, 'Done.', None, None
    with patch('istota.scheduler.execute_task', side_effect=execute):
        process_one_task(config)
    with db.get_db(config.db_path) as conn:
        assert db.get_task(conn, ident).status == 'cancelled'
        assert conn.execute('SELECT state FROM message_relays').fetchone()[0] == 'cancelled'
        kinds = [row[0] for row in conn.execute('SELECT kind FROM task_events WHERE task_id=?', (ident,))]
        assert 'cancelled' in kinds and 'done' in kinds


def test_talk_bot_actor_can_differ_from_login_without_allowing_a_third_user(setup, monkeypatch):
    from istota.relay.requests import RequestError
    config = setup[0]
    origin_setup(setup, 'talk', monkeypatch)
    config.nextcloud.username = 'bot@example.com'
    config.talk.bot_username = 'bot'
    origin = {'talk_ref': 'private-talk'}
    asyncio.run(relays.verify_private_audience(config, actor_user_id='alice', origin=origin))
    monkeypatch.setattr('istota.talk.TalkClient.get_participants', AsyncMock(return_value=[
        {'actorType': 'users', 'actorId': name} for name in ('alice', 'bot', 'carol')]))
    with pytest.raises(RequestError, match='unsupported_origin'):
        asyncio.run(relays.verify_private_audience(config, actor_user_id='alice', origin=origin))
