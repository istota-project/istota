"""One complete local relay through each provider boundary, with isolated prompts."""
import asyncio
import json
import os
from pathlib import Path
import tempfile

from istota import db
from istota.relay import relays
from istota.relay import requests
from istota.executor import build_prompt
from istota.sandbox.skill_proxy import SkillProxy
from . import test_relay_questions
from .test_relay_questions import park, approve
from .test_relay_answers import event, receive
from .test_whatsapp_skill import call

setup = test_relay_questions.setup


def test_skill_approval_question_reply_return_keeps_each_users_context(setup, tmp_path):
    config, ident, token, sent = setup
    question = 'Can you meet at seven?'
    answer = '  **Yes**, at seven.\n`exact text`  \n'
    private_asker = 'Asker-only planning notes'
    private_recipient = 'Recipient-only ordinary conversation'
    with db.get_db(config.db_path) as conn:
        conn.execute('UPDATE tasks SET prompt=? WHERE id=?', (private_asker, ident))
    # A real host-side skill subprocess receives its identity only from the proxy.
    config_path = tmp_path / 'config.toml'
    cloud = config.whatsapp.cloud
    config_path.write_text(
        f'db_path = {json.dumps(str(config.db_path))}\n'
        f'[whatsapp]\nenabled = true\nprovider = {json.dumps(config.whatsapp.provider)}\n'
        + f'business_phone_number = {json.dumps(config.whatsapp.business_phone_number)}\n'
        + '[whatsapp.cloud]\n'
        + ''.join(f'{name} = {json.dumps(getattr(cloud, name))}\n' for name in (
            'waba_id', 'phone_number_id', 'access_token', 'app_secret', 'verify_token'))
        + '[users.alice]\ndisplay_name = "Alice Example"\n[users.bob]\ndisplay_name = "Bob"\n'
    )
    env = dict(os.environ, ISTOTA_USER_ID='alice', ISTOTA_TASK_ID=str(ident),
               ISTOTA_DB_PATH=str(config.db_path), ISTOTA_CONFIG_PATH=str(config_path))
    with tempfile.TemporaryDirectory(prefix='relay_flow_', dir='/tmp') as directory:
        sock = Path(directory) / 's'
        with SkillProxy(sock, {}, env, allowed_skills=frozenset({'relay'})):
            args = ['ask', 'bob', '--request-key', 'meeting', '--via', 'whatsapp', question]
            response = call(sock, args, skill='relay')
            assert response['returncode'] == 0, response
            held = json.loads(response['stdout'])
            assert held['status'] == 'held' and held['needs_confirmation']
            assert json.loads(call(sock, args, skill='relay')['stdout']) == held
    asyncio.run(requests.drain_requests(config))
    assert sent == []
    assert park(setup)['preview'] == held['preview']
    with db.get_db(config.db_path) as conn:
        task = db.get_task(conn, ident)
        assert task.whatsapp_confirmation_request_id == held['request_id']
        assert task.confirmation_prompt == held['preview']
        body = conn.execute('SELECT service_body FROM whatsapp_skill_requests WHERE id=?',
                            (held['request_id'],)).fetchone()[0]
    approve(setup)
    asyncio.run(requests.drain_requests(config))
    asyncio.run(requests.drain_requests(config))
    assert len(sent) == 1 and sent[0].text == body
    assert question in body and 'Alice Example (alice)' in body
    assert f'!relay reply {held["relay_id"]}' in body
    ordinary = receive(config, event(config, private_recipient, quote=None, ident='ordinary-message'))
    with db.get_db(config.db_path) as conn:
        assert relays.get_relay(conn, actor_user_id='alice', relay_id=held['relay_id'])['answer_text'] is None
        assert db.get_task(conn, ordinary.task_id).user_id == 'bob'
    inbound = event(config, answer, ident='explicit-answer')
    result = receive(config, inbound)
    assert result.disposition == 'relay_answer'
    assert receive(config, inbound).disposition == 'duplicate'
    asyncio.run(requests.drain_requests(config))
    asyncio.run(requests.drain_requests(config))
    with db.get_db(config.db_path) as conn:
        relay = relays.get_relay(conn, actor_user_id='alice', relay_id=held['relay_id'])
        assert relay['answer_text'] == answer and relay['return_state'] == 'delivered'
        messages = conn.execute('SELECT * FROM messages WHERE delivery_reference=?',
                                ('relay-return:' + held['relay_id'],)).fetchall()
        assert len(messages) == 1 and messages[0]['room_token'] == token
        assert messages[0]['body'] == 'Answer from bob:\n\n' + answer
        assert private_recipient not in messages[0]['body']
        assert not conn.execute("SELECT 1 FROM messages WHERE room_token='shared'").fetchone()
        assert conn.execute('SELECT count(*) FROM sent_whatsapp WHERE logical_key=?',
                            ('relay-question:' + held['relay_id'],)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM tasks WHERE user_id='alice'").fetchone()[0] == 1
        task = db.get_task(conn, result.task_id)
        assert task.user_id == 'bob' and task.prompt == answer
        composed = build_prompt(task, [], config, conn=conn)
        assert question in composed.user and answer in composed.user
        assert private_asker not in composed.user + composed.system
        assert question not in composed.system and answer not in composed.system
        own_prompt = build_prompt(db.get_task(conn, ident), [], config, conn=conn)
        assert private_recipient not in own_prompt.user + own_prompt.system
