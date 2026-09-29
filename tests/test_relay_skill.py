"""The `relay` skill through a real SkillProxy: identity comes from the proxy only."""
import json
import os
from pathlib import Path
import tempfile

from istota import db
from istota.config import Config
from istota.skill_proxy import SkillProxy
from istota.skills._loader import capability_disabled_skills, load_skill_index
from . import test_relay_questions
from .test_whatsapp_skill import call

setup = test_relay_questions.setup


def _config_file(config, tmp_path):
    cloud = config.whatsapp.cloud
    path = tmp_path / 'config.toml'
    path.write_text(
        f'db_path = {json.dumps(str(config.db_path))}\n'
        f'[whatsapp]\nenabled = true\nprovider = {json.dumps(config.whatsapp.provider)}\n'
        f'business_phone_number = {json.dumps(config.whatsapp.business_phone_number)}\n'
        '[whatsapp.cloud]\n'
        + ''.join(f'{name} = {json.dumps(getattr(cloud, name))}\n' for name in (
            'waba_id', 'phone_number_id', 'access_token', 'app_secret', 'verify_token'))
        + '[users.alice]\ndisplay_name = "Alice"\n[users.bob]\ndisplay_name = "Bob"\n'
    )
    return path


def test_ask_holds_as_the_proxys_actor_and_refuses_forgery(setup, tmp_path):
    config, ident, _, sent = setup
    with db.get_db(config.db_path) as conn:
        foreign = db.create_task(conn, user_id='bob', source_type='web', prompt='x')
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (foreign,))
    env = dict(os.environ, ISTOTA_USER_ID='alice', ISTOTA_TASK_ID=str(ident),
               ISTOTA_DB_PATH=str(config.db_path), ISTOTA_CONFIG_PATH=str(_config_file(config, tmp_path)))
    with tempfile.TemporaryDirectory(prefix='relay_skill_', dir='/tmp') as directory:
        sock = Path(directory) / 's'
        with SkillProxy(sock, {}, env, allowed_skills=frozenset({'relay'})):
            ask = ['ask', 'bob', '--request-key', 'k', '--via', 'whatsapp', 'What time?']
            forged = call(sock, ask, skill='relay', user_id='bob', task_id=foreign,
                          env={'ISTOTA_USER_ID': 'bob', 'ISTOTA_TASK_ID': str(foreign)})
            assert forged['returncode'] == 0, forged
            held = json.loads(forged['stdout'])
            assert held['status'] == 'held'
            # --via is a closed set; talk and web are not destinations.
            assert call(sock, ['ask', 'bob', '--request-key', 'k2', '--via', 'talk', 'x'], skill='relay')['returncode'] != 0
            # Without --via the default room is resolved, and bob has none.
            plain = call(sock, ['ask', 'bob', '--request-key', 'k3', 'x'], skill='relay')
            assert plain['returncode'] == 1
            assert json.loads(plain['stdout'])['error'] == 'recipient_has_no_private_room'
            assert call(sock, ['ask', 'bob', '--request-key', 'k', '--task-id', str(foreign), 'x'], skill='relay')['returncode'] != 0
    with db.get_db(config.db_path) as conn:
        rows = conn.execute('SELECT requester_user_id,origin_task_id,kind FROM whatsapp_skill_requests').fetchall()
    assert [tuple(r) for r in rows] == [('alice', ident, 'relay_question')]
    assert not sent


def test_status_is_for_relay_questions_and_whatsapp_status_is_for_self_sends(setup, monkeypatch):
    from argparse import Namespace
    from istota.skills.relay import _dispatch as relay_dispatch
    from istota.skills.whatsapp import _dispatch as whatsapp_dispatch
    from istota.whatsapp_requests import RequestError
    import pytest
    config, ident, _, _ = setup
    held = test_relay_questions.hold(setup)
    monkeypatch.setattr('istota.config.load_config', lambda: config)
    monkeypatch.setenv('ISTOTA_DB_PATH', str(config.db_path))
    monkeypatch.setenv('ISTOTA_USER_ID', 'alice')
    monkeypatch.setenv('ISTOTA_TASK_ID', str(ident))
    status = relay_dispatch(Namespace(command='status', request_id=held['request_id']))
    assert status['request']['relay']['id'] == held['relay_id']
    assert relay_dispatch(Namespace(command='list'))['relays'][0]['id'] == held['relay_id']
    with pytest.raises(RequestError, match='request_unavailable'):
        whatsapp_dispatch(Namespace(command='status', request_id=held['request_id']))


def test_whatsapp_no_longer_offers_ask_or_relays():
    from istota.skills.whatsapp import build_parser
    import pytest
    parser = build_parser()
    for argv in (['ask', 'bob', '--request-key', 'k', 'x'], ['relays']):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_the_skill_needs_no_optional_surface():
    index = load_skill_index(Path(__file__).parents[1] / 'src/istota/skills')
    meta = index['relay']
    assert meta.cli and not meta.admin_only and not meta.requires_capability
    assert set(meta.companion_skills) == {'sensitive_actions', 'untrusted_input'}
    assert 'relay' not in capability_disabled_skills(index, Config().available_capabilities())
