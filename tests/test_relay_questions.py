"""Question authorization through real task confirmations and the send ledger."""
import asyncio
from unittest.mock import patch

import pytest

from istota import db, confirmations
from istota.relay import relays
from istota.relay import requests
from istota.config import UserConfig
from istota.transport.whatsapp._types import WhatsAppSendResult
from .test_whatsapp_delivery import _config, _bind


@pytest.fixture(params=["whatsapp_cloud", "baileys"])
def setup(tmp_path, monkeypatch, request):
    config = _config(tmp_path, provider=request.param)
    config.users['bob'] = UserConfig(display_name='Bob')
    config.users['alice'].display_name = 'Alice\nExample'
    if request.param == 'whatsapp_cloud':
        _bind(config, 'bob')
    else:
        with db.get_db(config.db_path) as conn:
            db.set_whatsapp_binding(conn, 'bob', bootstrap_phone_number='+15551234567')
            db.latch_whatsapp_jid(conn, 'bob', jid='15551234567@s.whatsapp.net')
    with db.get_db(config.db_path) as conn:
        room = db.create_web_chat_room(conn, 'alice', 'Private')
        token = room.token
        ident = db.create_task(conn, user_id='alice', source_type='web', prompt='Ask a question', conversation_token=token, output_target='talk:shared')
        conn.execute("UPDATE tasks SET status='running',confirmed_at=datetime('now') WHERE id=?", (ident,))
    sent = []
    async def send(config, message):
        sent.append(message)
        return WhatsAppSendResult(message_id='question-id')
    monkeypatch.setattr('istota.transport.whatsapp.providers.whatsapp_cloud._send', send)
    if request.param == 'baileys':
        from types import SimpleNamespace
        async def bridge_send(message):
            return await send(config, message)
        monkeypatch.setattr('istota.transport.whatsapp.baileys_bridge.active_bridge', lambda: SimpleNamespace(send=bridge_send))
    return config, ident, token, sent


def hold(setup, **kw):
    config, ident, _, _ = setup
    args = dict(actor_user_id='alice', task_id=ident, recipient_user_id='bob', request_key='question', text='What time?', via='whatsapp')
    args.update(kw)
    with db.get_db(config.db_path) as conn:
        return requests.hold_question(conn, config, **args)


def park(setup):
    config, ident, _, _ = setup
    with db.get_db(config.db_path) as conn:
        return requests.park_question(conn, config, task=db.get_task(conn, ident))


def approve(setup):
    config, ident, _, _ = setup
    with db.get_db(config.db_path) as conn:
        confirmations.approve(conn, db.get_task(conn, ident), config=config, by='web')


def test_exact_preview_and_approval_release_without_another_skill_call(setup):
    config, ident, token, sent = setup
    held = hold(setup)
    assert held['status'] == 'held' and held['needs_confirmation']
    assert 'What time?' in held['preview'] and '24 hours' in held['preview']
    assert token in held['preview']
    asyncio.run(requests.drain_requests(config))
    assert not sent
    parked = park(setup)
    assert parked['preview'] == held['preview']
    approve(setup)
    asyncio.run(requests.drain_requests(config))
    asyncio.run(requests.drain_requests(config))
    assert len(sent) == 1
    assert 'Alice Example (alice)' in sent[0].text
    assert 'Only that answer will be shared.' in sent[0].text
    with db.get_db(config.db_path) as conn:
        row = relays.get_relay(conn, actor_user_id='alice', relay_id=held['relay_id'])
        assert row['state'] == 'waiting'
        assert db.get_task(conn, ident).status == 'pending'
        ledger = conn.execute('SELECT user_id,task_id FROM sent_whatsapp').fetchone()
        assert tuple(ledger) == ('bob', None)


@pytest.mark.parametrize('change', ['consent', 'binding', 'origin', 'digest'])
def test_changes_after_approval_cannot_send(setup, change):
    config, _, token, sent = setup
    hold(setup)
    park(setup)
    approve(setup)
    with db.get_db(config.db_path) as conn:
        if change == 'consent':
            relays.block(conn, actor_user_id='bob', asker_user_id='alice')
        elif change == 'binding':
            conn.execute("UPDATE whatsapp_user_bindings SET send_id='different'")
        elif change == 'origin':
            db.add_room_member(conn, token, 'bob')
        else:
            conn.execute("UPDATE whatsapp_skill_requests SET service_body='tampered'")
    asyncio.run(requests.drain_requests(config))
    assert not sent


@pytest.mark.parametrize('close', ['decline', 'cancel', 'expire', 'supersede'])
def test_all_confirmation_close_paths_release_reservation(setup, close):
    config, ident, token, sent = setup
    held = hold(setup)
    park(setup)
    with db.get_db(config.db_path) as conn:
        task = db.get_task(conn, ident)
        if close == 'decline':
            confirmations.decline(conn, task)
        elif close == 'cancel':
            db.cancel_task(conn, ident)
        elif close == 'expire':
            conn.execute("UPDATE tasks SET updated_at=datetime('now','-3 hours')")
            db.expire_stale_confirmations(conn, 120)
        else:
            confirmations.cancel_for_conversation(conn, token, 'alice')
        assert relays.get_relay(conn, actor_user_id='alice', relay_id=held['relay_id'])['state'] in ('cancelled', 'expired')
        assert db.get_task(conn, ident).whatsapp_confirmation_request_id is None
    asyncio.run(requests.drain_requests(config))
    assert not sent


def test_old_confirmed_at_and_wrong_digest_are_not_authority(setup):
    config, ident, _, sent = setup
    held = hold(setup)
    with db.get_db(config.db_path) as conn:
        with pytest.raises(requests.RequestError):
            requests.approve_request(conn, task=db.get_task(conn, ident), request_id=held['request_id'], preview_digest='wrong')
    asyncio.run(requests.drain_requests(config))
    assert not sent


def test_shared_origin_rejected_before_reservation(setup):
    config, _, token, _ = setup
    with db.get_db(config.db_path) as conn:
        db.add_room_member(conn, token, 'bob')
    with pytest.raises(requests.RequestError, match='unsupported_origin'):
        hold(setup)
    with db.get_db(config.db_path) as conn:
        assert not conn.execute('SELECT 1 FROM message_relays').fetchone()


def test_direct_block_command_is_private_and_directional(setup):
    from istota.commands import CommandContext, cmd_relay
    config, _, token, _ = setup
    with db.get_db(config.db_path) as conn:
        ctx = CommandContext(config, conn, 'alice', token, 'block bob', surface='web')
        assert 'blocked' in asyncio.run(cmd_relay(ctx)).lower()
        assert relays.is_blocked(conn, actor_user_id='alice', asker_user_id='bob')
        assert not relays.is_blocked(conn, actor_user_id='bob', asker_user_id='alice')
        ctx.args = 'blocked'
        assert 'bob' in asyncio.run(cmd_relay(ctx))
        ctx.args = 'unblock bob'
        assert 'unblocked' in asyncio.run(cmd_relay(ctx)).lower()
        assert not relays.is_blocked(conn, actor_user_id='alice', asker_user_id='bob')
        ctx.args = 'allow bob'
        assert asyncio.run(cmd_relay(ctx)).startswith('Use !relay block')
        db.add_room_member(conn, token, 'bob')
        ctx.args = 'blocked'
        assert asyncio.run(cmd_relay(ctx)) == 'Relay commands require a verified private conversation.'


def test_ask_distinguishes_why_a_recipient_cannot_be_asked(setup):
    config, _, _, _ = setup
    with pytest.raises(requests.RequestError, match='unknown_user'):
        hold(setup, recipient_user_id='nobody')
    config.users['carol'] = UserConfig(display_name='Carol')
    with pytest.raises(requests.RequestError, match='recipient_not_on_whatsapp'):
        hold(setup, recipient_user_id='carol')
    config.whatsapp.enabled = False
    with pytest.raises(requests.RequestError, match='whatsapp_unavailable'):
        hold(setup)
    config.whatsapp.enabled = True
    with pytest.raises(requests.RequestError, match='sms_unavailable'):
        hold(setup, via='sms')
    # A block is checked ahead of the destination, so a blocked asker gets the
    # same answer whatever the recipient's bindings are.
    with db.get_db(config.db_path) as conn:
        relays.block(conn, actor_user_id='bob', asker_user_id='alice')
    for via in ('whatsapp', 'sms', None):
        with pytest.raises(requests.RequestError, match='recipient_unavailable'):
            hold(setup, via=via)


def test_scheduler_parks_even_without_model_phrase_and_never_fans_out(setup):
    from istota.scheduler import process_one_task
    config, ident, _, _ = setup
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET status='pending' WHERE id=?", (ident,))
    def execute(*args, **kwargs):
        hold(setup)
        return True, 'Done.', None, None
    with patch('istota.scheduler.execute_task', side_effect=execute), patch('istota.scheduler.post_result_to_talk') as talk, patch('istota.scheduler.deliver_pending') as notify:
        process_one_task(config)
    talk.assert_not_called()
    with db.get_db(config.db_path) as conn:
        task = db.get_task(conn, ident)
        assert task.status == 'pending_confirmation'
        assert 'What time?' in task.confirmation_prompt
        notices = conn.execute('SELECT title,body FROM notifications').fetchall()
        assert notices and all('What time?' not in str(tuple(row)) for row in notices)
    assert all('What time?' not in str(call) for call in notify.call_args_list)


@pytest.mark.parametrize('surface', ['talk', 'whatsapp'])
def test_preview_delivered_only_to_its_private_push_origin(setup, surface, monkeypatch):
    from unittest.mock import AsyncMock
    config, ident, token, _ = setup
    config.nextcloud.username = 'bot'
    config.nextcloud.url = 'https://cloud.example.com'
    config.talk.enabled = True
    if surface == 'whatsapp':
        from istota.transport.whatsapp import whatsapp_conversation_token
        if config.whatsapp.provider == 'whatsapp_cloud':
            _bind(config, 'alice', bsuid='US.1111111111', bootstrap_phone_number='+15551230001')
        else:
            with db.get_db(config.db_path) as conn:
                db.set_whatsapp_binding(conn, 'alice', bootstrap_phone_number='+15551230001')
                db.latch_whatsapp_jid(conn, 'alice', jid='15551230001@s.whatsapp.net')
        token = whatsapp_conversation_token('alice')
    else:
        with db.get_db(config.db_path) as conn:
            db.add_room_binding(conn, token, 'talk', 'private-talk')
        monkeypatch.setattr('istota.nextcloud.talk.TalkClient.get_participants', AsyncMock(return_value=[
            {'actorType': 'users', 'actorId': 'alice'}, {'actorType': 'users', 'actorId': 'bot'}]))
    with db.get_db(config.db_path) as conn:
        conn.execute('UPDATE tasks SET source_type=?,conversation_token=? WHERE id=?', (surface, token, ident))
    held = hold(setup)
    seam = 'istota.transport.talk.TalkTransport.deliver' if surface == 'talk' else 'istota.transport.whatsapp.WhatsAppTransport.deliver'
    with patch(seam, autospec=True) as delivery:
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, ident)
        assert asyncio.run(requests.present_question(config, task=task, success=True))
    assert delivery.call_args.args[1] == ('private-talk' if surface == 'talk' else token)
    assert delivery.call_args.args[2] == held['preview']


def test_scheduler_failed_attempt_closes_unsurfaced_question(setup):
    config, ident, _, sent = setup
    held = hold(setup)
    with db.get_db(config.db_path) as conn:
        task = db.get_task(conn, ident)
    assert not asyncio.run(requests.present_question(config, task=task, success=False))
    with db.get_db(config.db_path) as conn:
        assert relays.get_relay(conn, actor_user_id='alice', relay_id=held['relay_id'])['state'] == 'cancelled'
        assert db.get_task(conn, ident).confirmation_prompt is None
    assert not sent


def test_origin_becomes_shared_before_parking_body_never_published(setup):
    config, ident, token, _ = setup
    hold(setup)
    with db.get_db(config.db_path) as conn:
        db.add_room_member(conn, token, 'bob')
        task = db.get_task(conn, ident)
    assert asyncio.run(requests.present_question(config, task=task, success=True))
    with db.get_db(config.db_path) as conn:
        assert db.get_task(conn, ident).status == 'cancelled'
        assert db.get_task(conn, ident).confirmation_prompt is None
        assert all('What time?' not in str(tuple(row)) for row in conn.execute('SELECT title,body FROM notifications'))


def test_binding_replacement_after_claim_cannot_retarget_question(setup, monkeypatch):
    from istota.transport.whatsapp import outbound
    config, _, _, sent = setup
    hold(setup)
    park(setup)
    approve(setup)
    original = outbound._send_claimed
    async def replaced(*args, **kwargs):
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE whatsapp_user_bindings SET send_id='replacement'")
        return await original(*args, **kwargs)
    monkeypatch.setattr(outbound, '_send_claimed', replaced)
    asyncio.run(requests.drain_requests(config))
    assert not sent


@pytest.mark.asyncio
async def test_web_admin_cannot_approve_another_users_relay(setup, monkeypatch):
    from fastapi import HTTPException
    from istota.webui import app as web_app
    config, ident, _, sent = setup
    hold(setup)
    park(setup)
    monkeypatch.setattr(web_app, '_config', config)
    monkeypatch.setattr(web_app, '_user_is_web_admin', lambda user: True)
    with pytest.raises(HTTPException) as error:
        await web_app.chat_confirm_task(ident, user={'username': 'bob'}, _csrf=None)
    assert error.value.status_code == 403
    await requests.drain_requests(config)
    assert not sent
    await web_app.chat_confirm_task(ident, user={'username': 'alice'}, _csrf=None)
    await requests.drain_requests(config)
    assert len(sent) == 1


def test_public_status_hides_recipient_opt_out(setup):
    config, _, _, _ = setup
    held = hold(setup)
    park(setup)
    approve(setup)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE whatsapp_user_bindings SET opted_out_at=datetime('now')")
    asyncio.run(requests.drain_requests(config))
    with db.get_db(config.db_path) as conn:
        status = requests.get_request(conn, actor_user_id='alice', request_id=held['request_id'])
        assert status['error_code'] == 'could_not_deliver'


@pytest.mark.parametrize('change', ['revoked_after_claim', 'crash_after_claim'])
def test_claimed_question_is_never_retried_or_reopened(setup, monkeypatch, change):
    from istota.transport.whatsapp import outbound
    config, _, _, sent = setup
    held = hold(setup)
    park(setup)
    approve(setup)
    original = outbound._send_claimed
    async def change_after_claim(*args, **kwargs):
        if change == 'crash_after_claim':
            raise SystemExit('simulated process death')
        with db.get_db(config.db_path) as conn:
            relays.block(conn, actor_user_id='bob', asker_user_id='alice')
        return await original(*args, **kwargs)
    monkeypatch.setattr(outbound, '_send_claimed', change_after_claim)
    if change == 'crash_after_claim':
        with pytest.raises(SystemExit):
            asyncio.run(requests.drain_requests(config))
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE whatsapp_skill_requests SET updated_at=datetime('now','-5 minutes')")
    else:
        asyncio.run(requests.drain_requests(config))
    monkeypatch.setattr(outbound, '_send_claimed', original)
    asyncio.run(requests.drain_requests(config))
    with db.get_db(config.db_path) as conn:
        row = relays.get_relay(conn, actor_user_id='alice', relay_id=held['relay_id'])
        assert row['state'] == ('cancelled' if change == 'revoked_after_claim' else 'uncertain')
    assert len(sent) == (1 if change == 'revoked_after_claim' else 0)


def test_changed_preview_cannot_authorize_a_different_request(setup):
    config, ident, _, sent = setup
    hold(setup)
    park(setup)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET confirmation_prompt='different action' WHERE id=?", (ident,))
        with pytest.raises(requests.RequestError, match='confirmation_unavailable'):
            confirmations.approve(conn, db.get_task(conn, ident), config=config)
    asyncio.run(requests.drain_requests(config))
    assert not sent


def test_template_variant_is_complete_or_unavailable(setup):
    config, _, _, _ = setup
    config.whatsapp.cloud.proactive_template.enabled = True
    config.whatsapp.cloud.proactive_template.name = 'relay_question'
    config.whatsapp.cloud.proactive_template.language = 'en_US'
    held = hold(setup, text='Word ' * 350)
    assert '(unavailable)' in held['preview']
    assert 'Word ' * 350 in held['preview']


def test_private_command_rechecks_audience_after_remote_check(setup, monkeypatch):
    from istota.commands import CommandContext, cmd_relay
    config, _, token, _ = setup
    async def change_audience(*args, **kwargs):
        with db.get_db(config.db_path) as other:
            db.add_room_member(other, token, 'bob')
    monkeypatch.setattr(relays, 'verify_private_audience', change_audience)
    with db.get_db(config.db_path) as conn:
        ctx = CommandContext(config, conn, 'alice', token, 'block bob', surface='web')
        assert asyncio.run(cmd_relay(ctx)) == 'Relay commands require a verified private conversation.'
        assert not relays.is_blocked(conn, actor_user_id='alice', asker_user_id='bob')



def test_whatsapp_preview_never_falls_back_to_a_shortened_template(setup):
    from istota.transport.whatsapp import whatsapp_conversation_token
    config, ident, _, sent = setup
    if config.whatsapp.provider != 'whatsapp_cloud':
        pytest.skip('Cloud service window only')
    config.whatsapp.cloud.billing_policy = 'allow_paid'
    config.whatsapp.cloud.proactive_template.enabled = True
    config.whatsapp.cloud.proactive_template.name = 'relay_question'
    config.whatsapp.cloud.proactive_template.language = 'en_US'
    _bind(config, 'alice', bsuid='US.1111111111', bootstrap_phone_number='+15551230001', window=None)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET source_type='whatsapp',conversation_token=? WHERE id=?",
                     (whatsapp_conversation_token('alice'), ident))
    hold(setup, text='Question ' * 150)
    with db.get_db(config.db_path) as conn:
        task = db.get_task(conn, ident)
    asyncio.run(requests.present_question(config, task=task, success=True))
    assert not sent



def test_cancellation_requested_before_parking_closes_unsurfaced_draft(setup):
    config, ident, _, _ = setup
    held = hold(setup)
    with db.get_db(config.db_path) as conn:
        conn.execute('UPDATE tasks SET cancel_requested=1 WHERE id=?', (ident,))
    assert park(setup) is None
    with db.get_db(config.db_path) as conn:
        assert relays.get_relay(conn, actor_user_id='alice', relay_id=held['relay_id'])['state'] == 'cancelled'
        assert db.get_task(conn, ident).confirmation_prompt is None


def test_retry_cannot_publish_previous_attempts_unsurfaced_draft(setup):
    from istota.scheduler import process_one_task
    config, ident, _, _ = setup
    held = hold(setup)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET status='pending',attempt_count=1,output_target='web' WHERE id=?", (ident,))
    with patch('istota.scheduler.execute_task', return_value=(True, 'Done.', None, None)):
        process_one_task(config)
    with db.get_db(config.db_path) as conn:
        assert relays.get_relay(conn, actor_user_id='alice', relay_id=held['relay_id'])['state'] == 'cancelled'
        assert db.get_task(conn, ident).status == 'completed'



def test_shared_command_cannot_approve_private_relay(setup):
    from istota.commands import CommandContext, cmd_confirm
    config, ident, _, sent = setup
    hold(setup)
    park(setup)
    with db.get_db(config.db_path) as conn:
        shared = db.create_web_chat_room(conn, 'alice', 'Shared')
        db.add_room_member(conn, shared.token, 'bob')
        ctx = CommandContext(config, conn, 'alice', shared.token, str(ident), surface='web')
        assert 'private conversation' in asyncio.run(cmd_confirm(ctx))
    asyncio.run(requests.drain_requests(config))
    assert not sent


def test_ask_leaves_the_talk_audience_check_to_the_daemon(setup, monkeypatch):
    # ISSUE-567: the skill subprocess carries no Nextcloud credential, so a
    # Talk call from hold_question refused every Talk-bound room. The daemon
    # checks the audience before it shows the preview, so a failure there
    # still cancels the question without publishing it.
    config, ident, token, _ = setup
    with db.get_db(config.db_path) as conn:
        db.add_room_binding(conn, token, 'talk', 'talk-ref')
    calls = []
    async def unreachable(*args, **kwargs):
        calls.append(kwargs['origin']['talk_ref'])
        raise requests.RequestError('unsupported_origin')
    monkeypatch.setattr(relays, 'verify_private_audience', unreachable)
    held = hold(setup)
    assert held['status'] == 'held'
    assert calls == []
    with db.get_db(config.db_path) as conn:
        task = db.get_task(conn, ident)
    assert asyncio.run(requests.present_question(config, task=task, success=True))
    assert calls == ['talk-ref']
    with db.get_db(config.db_path) as conn:
        assert db.get_task(conn, ident).status == 'cancelled'
        assert db.get_task(conn, ident).confirmation_prompt is None
