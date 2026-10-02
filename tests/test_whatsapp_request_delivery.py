"""Durable task sends through the daemon and the existing provider ledger."""

import asyncio
from datetime import timedelta

import pytest

from istota import db
from istota.relay import requests
from istota.transport.whatsapp import outbound
from istota.transport.whatsapp._types import WhatsAppSendResult
from .test_whatsapp_delivery import _config, _bind
from .test_whatsapp_requests import task


@pytest.fixture(params=['whatsapp_cloud', 'baileys'])
def delivery(tmp_path, request, monkeypatch):
    config = _config(tmp_path, provider=request.param)
    if request.param == 'baileys':
        with db.get_db(config.db_path) as conn:
            db.set_whatsapp_binding(conn, 'alice', bootstrap_phone_number='+15551234567')
            db.latch_whatsapp_jid(conn, 'alice', jid='15551234567@s.whatsapp.net')
    else:
        _bind(config)
    sent = []
    async def send(message):
        sent.append(message)
        return WhatsAppSendResult(message_id='message-' + str(len(sent)))
    if request.param == 'baileys':
        from types import SimpleNamespace
        monkeypatch.setattr('istota.transport.whatsapp.baileys_bridge.active_bridge', lambda: SimpleNamespace(send=send))
    else:
        monkeypatch.setattr('istota.transport.whatsapp.providers.whatsapp_cloud._send', lambda config, message: send(message))
    with db.get_db(config.db_path) as conn:
        ident = task(conn)
    return config, ident, sent


def enqueue(config, ident, text='hello', key='one'):
    with db.get_db(config.db_path) as conn:
        return requests.enqueue_self_send(conn, config, actor_user_id='alice', task_id=ident,
                                     request_key=key, text=text)


def state(config, ident):
    with db.get_db(config.db_path) as conn:
        return requests.get_request(conn, actor_user_id='alice', request_id=ident)


def test_mid_task_send_and_retry_leave_task_output_unchanged(delivery):
    config, ident, sent = delivery
    with db.get_db(config.db_path) as conn:
        before = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone())
    req = enqueue(config, ident)
    assert req['status'] == 'queued' and req['delivery_status'] == 'pending'
    assert not sent
    asyncio.run(requests.drain_requests(config))
    assert len(sent) == 1 and sent[0].text == 'hello'
    assert state(config, req['request_id'])['state'] == 'sent'
    assert enqueue(config, ident)['request_id'] == req['request_id']
    asyncio.run(requests.drain_requests(config))
    with db.get_db(config.db_path) as conn:
        assert dict(conn.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone()) == before
    assert len(sent) == 1


def test_later_task_failure_does_not_retract_send(delivery):
    config, ident, sent = delivery
    enqueue(config, ident)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET status='failed' WHERE id=?", (ident,))
    asyncio.run(requests.drain_requests(config))
    assert len(sent) == 1


@pytest.mark.parametrize('gate', ['opted_out', 'binding_changed', 'queue_expired'])
def test_live_admission_refuses_changed_policy(delivery, gate):
    config, ident, sent = delivery
    req = enqueue(config, ident)
    with db.get_db(config.db_path) as conn:
        if gate == 'opted_out':
            conn.execute("UPDATE whatsapp_user_bindings SET opted_out_at=datetime('now')")
        elif gate == 'binding_changed':
            conn.execute("UPDATE whatsapp_user_bindings SET bootstrap_phone_number='+15551239999'")
        else:
            conn.execute("UPDATE whatsapp_skill_requests SET queue_deadline=datetime('now','-1 second')")
    asyncio.run(requests.drain_requests(config))
    assert not sent
    assert state(config, req['request_id'])['error_code'] == gate


def test_binding_replaced_between_claim_and_destination_never_retargets(delivery, monkeypatch):
    config, ident, sent = delivery
    req = enqueue(config, ident)
    original = outbound._send_claimed
    async def replace_then_send(*args, **kwargs):
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE whatsapp_user_bindings SET send_id='replacement',jid='15551239999@s.whatsapp.net'")
        return await original(*args, **kwargs)
    monkeypatch.setattr(outbound, '_send_claimed', replace_then_send)
    asyncio.run(requests.drain_requests(config))
    assert not sent
    assert state(config, req['request_id'])['error_code'] == 'binding_changed'


def test_claim_crash_is_uncertain_and_never_retried(delivery, monkeypatch):
    config, ident, sent = delivery
    req = enqueue(config, ident)
    def crash(*args, **kwargs):
        raise SystemExit('simulated process death')
    monkeypatch.setattr(outbound, '_send_claimed', crash)
    with pytest.raises(SystemExit):
        asyncio.run(requests.drain_requests(config))
    asyncio.run(requests.drain_requests(config))
    assert not sent
    assert state(config, req['request_id'])['state'] == 'uncertain'


@pytest.mark.parametrize('gate', ['window_closed', 'budget_exhausted', 'billing_blocked'])
def test_cloud_existing_gates(tmp_path, gate):
    config = _config(tmp_path)
    _bind(config, window=timedelta(days=2) if gate == 'window_closed' else timedelta())
    with db.get_db(config.db_path) as conn:
        ident = task(conn)
        if gate == 'billing_blocked':
            db.block_whatsapp_billing(conn, 'old-message')
    if gate == 'budget_exhausted':
        config.whatsapp.cloud.monthly_service_attempt_limit = 1
        with db.get_db(config.db_path) as conn:
            conn.execute("INSERT INTO sent_whatsapp (logical_key,user_id,send_kind,status,body_chars,body_sha256,quota_month,claimed_at,created_at,updated_at) VALUES ('old','alice','service','unknown',1,'hash',?,datetime('now'),datetime('now'),datetime('now'))", (outbound.quota_month(config),))
    req = enqueue(config, ident)
    asyncio.run(requests.drain_requests(config))
    assert state(config, req['request_id'])['error_code'] == gate


def test_local_refusal_creates_body_free_notice(delivery):
    config, ident, sent = delivery
    req = enqueue(config, ident, text='private fabricated message')
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE whatsapp_skill_requests SET queue_deadline=datetime('now','-1 second')")
    asyncio.run(requests.drain_requests(config))
    with db.get_db(config.db_path) as conn:
        notices = [dict(row) for row in conn.execute("SELECT * FROM notifications WHERE user_id='alice'")]
    assert len(notices) == 1
    assert req['request_id'] in str(notices)
    assert 'private fabricated message' not in str(notices)


def test_template_variant_never_truncates(tmp_path, monkeypatch):
    from istota.config import WhatsAppTemplateConfig
    config = _config(tmp_path, billing_policy='allow_paid')
    config.whatsapp.cloud.proactive_template = WhatsAppTemplateConfig(enabled=True, name='notice', language='en')
    _bind(config, window=timedelta(days=2))
    with db.get_db(config.db_path) as conn:
        ident = task(conn)
    async def forbidden(*args):
        pytest.fail('incomplete template reached provider')
    monkeypatch.setattr('istota.transport.whatsapp.providers.whatsapp_cloud._send', forbidden)
    req = enqueue(config, ident, text='x' * 1000)
    asyncio.run(requests.drain_requests(config))
    assert state(config, req['request_id'])['error_code'] == 'template_unavailable'


def test_ledger_settled_before_request_write_recovers(delivery, monkeypatch):
    config, ident, sent = delivery
    req = enqueue(config, ident)
    original = requests._finish_request
    def crash(*args, **kwargs):
        raise RuntimeError('simulated crash after ledger settlement')
    monkeypatch.setattr(requests, '_finish_request', crash)
    with pytest.raises(RuntimeError):
        asyncio.run(requests.drain_requests(config))
    monkeypatch.setattr(requests, '_finish_request', original)
    asyncio.run(requests.drain_requests(config))
    assert len(sent) == 1
    assert state(config, req['request_id'])['state'] == 'sent'


def test_stale_claim_never_sends_and_notifies(delivery):
    config, ident, sent = delivery
    req = enqueue(config, ident)
    adapter = outbound.active_adapter(config)
    outbound._claim(config, logical_key='skill-whatsapp:' + req['request_id'], user_id='alice',
                    task_id=None, bodies={}, ignore_opt_out=False, caps=adapter.caps,
                    request_id=req['request_id'])
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE whatsapp_skill_requests SET updated_at=datetime('now','-3 minutes')")
    asyncio.run(requests.drain_requests(config))
    assert not sent
    assert state(config, req['request_id'])['state'] == 'uncertain'
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM notifications WHERE user_id='alice'").fetchone()[0] == 1


def test_daemon_gate_runs_drain_but_one_shot_does_not(delivery):
    from istota.scheduler import build_interval_gates
    config, ident, sent = delivery
    req = enqueue(config, ident)
    gate = next(g for g in build_interval_gates(config) if g.name == 'whatsapp-requests')
    assert gate.background and not gate.one_shot
    gate.run(0)
    assert len(sent) == 1
    assert state(config, req['request_id'])['state'] == 'sent'


def test_concurrent_pollers_admit_one_provider_call(delivery):
    config, ident, sent = delivery
    req = enqueue(config, ident)
    async def race():
        await asyncio.gather(requests.drain_requests(config), requests.drain_requests(config))
    asyncio.run(race())
    assert len(sent) == 1
    assert state(config, req['request_id'])['state'] == 'sent'
    with db.get_db(config.db_path) as conn:
        assert conn.execute('SELECT count(*) FROM sent_whatsapp').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM notifications').fetchone()[0] == 0


def test_service_and_template_use_frozen_body(tmp_path, monkeypatch):
    from istota.config import WhatsAppTemplateConfig
    config = _config(tmp_path, billing_policy='allow_paid')
    config.whatsapp.cloud.proactive_template = WhatsAppTemplateConfig(enabled=True, name='notice', language='en')
    _bind(config, window=timedelta(days=2))
    with db.get_db(config.db_path) as conn:
        ident = task(conn)
    req = enqueue(config, ident, text='hello\nworld')
    sent = []
    async def send(config, message):
        sent.append(message)
        return WhatsAppSendResult(message_id='template-message')
    monkeypatch.setattr('istota.transport.whatsapp.providers.whatsapp_cloud._send', send)
    record = asyncio.run(outbound.deliver_whatsapp(
        config, logical_key='skill-whatsapp:' + req['request_id'], user_id='alice',
        text='mutable caller text must be ignored', request_id=req['request_id'],
    ))
    assert record.status == 'accepted'
    assert sent[0].kind == 'template' and sent[0].text == 'hello world'


def test_task_retry_retains_namespace_even_if_binding_disappears(delivery):
    config, ident, sent = delivery
    req = enqueue(config, ident)
    asyncio.run(requests.drain_requests(config))
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET status='pending' WHERE id=?", (ident,))
        with pytest.raises(requests.RequestError, match='task_unavailable'):
            requests.enqueue_self_send(conn, config, actor_user_id='alice', task_id=ident,
                                       request_key='one', text='hello')
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
        conn.execute('DELETE FROM whatsapp_user_bindings')
    assert enqueue(config, ident)['request_id'] == req['request_id']
    assert len(sent) == 1


def test_provider_switch_refuses_request(delivery):
    config, ident, sent = delivery
    req = enqueue(config, ident)
    config.whatsapp.provider = 'baileys' if config.whatsapp.provider == 'whatsapp_cloud' else 'whatsapp_cloud'
    asyncio.run(requests.drain_requests(config))
    assert not sent
    assert state(config, req['request_id'])['error_code'] == 'binding_changed'


def test_poll_batch_is_bounded(delivery):
    config, ident, sent = delivery
    for i in range(3):
        enqueue(config, ident, key=str(i))
    assert asyncio.run(requests.drain_requests(config, limit=2)) == 2
    assert len(sent) == 2
    assert asyncio.run(requests.drain_requests(config, limit=2)) == 1
    assert len(sent) == 3
