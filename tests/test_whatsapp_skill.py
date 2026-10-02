"""The real skill subprocess can only enqueue as its proxy's trusted actor."""

import json
import os
from pathlib import Path
import socket
import tempfile

from istota import db
from istota.config import Config
from istota.sandbox.skill_proxy import SkillProxy
from istota.skills._loader import capability_disabled_skills, load_skill_index
from .test_whatsapp_requests import task


def call(sock_path, args, skill='whatsapp', **forged):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10)
        sock.connect(str(sock_path))
        sock.sendall((json.dumps(dict(skill=skill, args=args, **forged)) + '\n').encode())
        with sock.makefile('rb') as stream:
            return json.loads(stream.readline())


def test_real_proxy_enqueues_as_trusted_actor_and_replays_on_retry(tmp_path):
    path = tmp_path / 'state.db'
    db.init_db(path)
    with db.get_db(path) as conn:
        ident = task(conn)
        foreign = task(conn, 'bob')
        db.set_whatsapp_binding(conn, 'alice', bootstrap_phone_number='+15551234567')
        db.latch_whatsapp_jid(conn, 'alice', jid='15551234567@s.whatsapp.net')
    config_path = tmp_path / 'config.toml'
    config_path.write_text(f'db_path = "{path}"\n[whatsapp]\nenabled = true\nprovider = "baileys"\n[users.alice]\n')
    env = dict(os.environ, ISTOTA_USER_ID='alice', ISTOTA_TASK_ID=str(ident),
               ISTOTA_DB_PATH=str(path), ISTOTA_CONFIG_PATH=str(config_path))
    with tempfile.TemporaryDirectory(prefix='wa_proxy_', dir='/tmp') as directory:
        sock = Path(directory) / 's'
        with SkillProxy(sock, {}, env, allowed_skills=frozenset({'whatsapp'})):
            args = ['send', '--request-key', 'stable', 'fabricated notice']
            response = call(sock, args, user_id='bob', task_id=foreign,
                            env={'ISTOTA_USER_ID': 'bob', 'ISTOTA_TASK_ID': str(foreign)})
            assert response['returncode'] == 0, response
            first = json.loads(response['stdout'])
            assert first['status'] == 'queued'
            assert json.loads(call(sock, args)['stdout'])['request_id'] == first['request_id']
            assert call(sock, ['send', '--request-key', 'stable', 'changed'])['returncode'] == 1
            assert call(sock, ['send', '--request-key', 'new', '--task-id', str(foreign), 'x'])['returncode'] != 0
            assert call(sock, ['ask', 'bob', '--request-key', 'new', 'x'])['returncode'] != 0
            assert call(sock, ['status', 'unknown'])['returncode'] == 1
        with SkillProxy(sock, {}, dict(env, ISTOTA_TASK_ID=str(foreign)), allowed_skills=frozenset({'whatsapp'})):
            assert call(sock, ['status', first['request_id']])['returncode'] == 1
        with SkillProxy(sock, {}, dict(env, ISTOTA_USER_ID='bob'), allowed_skills=frozenset({'whatsapp'})):
            assert call(sock, ['status', first['request_id']])['returncode'] == 1
            assert call(sock, ['send', '--request-key', 'forged', 'x'])['returncode'] == 1
    with db.get_db(path) as conn:
        rows = conn.execute('SELECT * FROM whatsapp_skill_requests').fetchall()
        assert len(rows) == 1 and rows[0]['requester_user_id'] == 'alice'
        assert rows[0]['origin_task_id'] == ident
        assert conn.execute('SELECT count(*) FROM sent_whatsapp').fetchone()[0] == 0


def test_skill_capability_and_companions():
    config = Config()
    index = load_skill_index(Path(__file__).parents[1] / 'src/istota/skills')
    assert 'whatsapp' in capability_disabled_skills(index, config.available_capabilities())
    config.whatsapp.enabled = True
    assert 'whatsapp' not in capability_disabled_skills(index, config.available_capabilities())
    assert set(index['whatsapp'].companion_skills) == {'sensitive_actions', 'untrusted_input'}
    assert not index['whatsapp'].admin_only
