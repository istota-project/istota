"""Where a relay question goes: resolution order, room qualification, refusals."""
import json

import pytest

from istota import db, message_relays as relays, relay_destinations as dest, user_profiles
from istota import whatsapp_requests as requests
from istota.config import UserConfig
from . import test_relay_questions

setup = test_relay_questions.setup
hold = test_relay_questions.hold


def _bob_room(config, name='assistant'):
    with db.get_db(config.db_path) as conn:
        handle = db.create_web_chat_room(conn, 'bob', name)
    return handle.token


def _prefer(config, value):
    user_profiles.ensure_profile(config.db_path, 'bob', display_name='Bob')
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE user_profiles SET relay_delivery=? WHERE user_id='bob'", (value,))


def _sms(config, number='+15557654321'):
    config.sms.enabled = True
    config.users['bob'].sms_phone_number = number


def resolve(config, requested=None):
    with db.get_db(config.db_path) as conn:
        return dest.resolve_destination(conn, config, recipient_user_id='bob', requested=requested)


class TestTheResolutionOrder:
    def test_no_preference_and_no_via_is_the_default_room(self, setup):
        config = setup[0]
        token = _bob_room(config)
        found = resolve(config)
        assert found['kind'] == 'room' and found['room_token'] == token
        assert found['label'] == "Bob's room #assistant"

    def test_via_is_honoured_when_the_recipient_has_no_preference(self, setup):
        config = setup[0]
        _bob_room(config)
        _sms(config)
        assert resolve(config, 'whatsapp')['kind'] == 'whatsapp'
        assert resolve(config, 'sms')['kind'] == 'sms'

    def test_the_preference_beats_the_askers_via(self, setup):
        config = setup[0]
        _bob_room(config)
        _sms(config)
        _prefer(config, 'whatsapp')
        assert resolve(config, 'sms')['kind'] == 'whatsapp'
        assert resolve(config, 'room')['kind'] == 'whatsapp'

    def test_an_unusable_preference_falls_to_the_room_never_to_via(self, setup):
        config = setup[0]
        token = _bob_room(config)
        _sms(config)
        _prefer(config, 'sms')
        config.users['bob'].sms_phone_number = ''
        # WhatsApp is usable and named by the asker; the recipient chose SMS,
        # which fails, so the answer is their room and not the asker's choice.
        found = resolve(config, 'whatsapp')
        assert found['kind'] == 'room' and found['room_token'] == token

    def test_an_unusable_preference_with_no_room_is_the_room_refusal(self, setup):
        config = setup[0]
        _prefer(config, 'sms')
        with pytest.raises(requests.RequestError, match='recipient_has_no_private_room'):
            resolve(config, 'whatsapp')

    def test_an_unknown_stored_preference_is_no_preference(self, setup):
        config = setup[0]
        _bob_room(config)
        _prefer(config, 'carrier-pigeon')
        assert resolve(config, 'whatsapp')['kind'] == 'whatsapp'


class TestTheRefusals:
    def test_each_named_transport_says_what_failed(self, setup):
        config = setup[0]
        config.users['carol'] = UserConfig(display_name='Carol')
        with db.get_db(config.db_path) as conn:
            with pytest.raises(requests.RequestError, match='recipient_not_on_whatsapp'):
                dest.resolve_destination(conn, config, recipient_user_id='carol', requested='whatsapp')
            with pytest.raises(requests.RequestError, match='sms_unavailable'):
                dest.resolve_destination(conn, config, recipient_user_id='carol', requested='sms')
            config.sms.enabled = True
            with pytest.raises(requests.RequestError, match='recipient_not_on_sms'):
                dest.resolve_destination(conn, config, recipient_user_id='carol', requested='sms')
            config.whatsapp.enabled = False
            with pytest.raises(requests.RequestError, match='whatsapp_unavailable'):
                dest.resolve_destination(conn, config, recipient_user_id='bob', requested='whatsapp')
            with pytest.raises(requests.RequestError, match='recipient_has_no_private_room'):
                dest.resolve_destination(conn, config, recipient_user_id='carol', requested=None)
            with pytest.raises(requests.RequestError, match='invalid_via'):
                dest.resolve_destination(conn, config, recipient_user_id='bob', requested='talk')


class TestRoomQualification:
    def test_a_pinned_default_room_somebody_else_is_in_does_not_qualify(self, setup):
        config = setup[0]
        token = _bob_room(config)
        user_profiles.ensure_profile(config.db_path, 'bob', display_name='Bob')
        user_profiles.update_profile(config.db_path, 'bob', default_room=token)
        with db.get_db(config.db_path) as conn:
            db.add_room_member(conn, token, 'alice')
            assert db.default_web_room(conn, 'bob').token == token
        with pytest.raises(requests.RequestError, match='recipient_has_no_private_room'):
            resolve(config, 'room')

    def test_an_archived_room_does_not_qualify_and_none_is_created(self, setup):
        config = setup[0]
        token = _bob_room(config)
        with db.get_db(config.db_path) as conn:
            db.set_room_archived(conn, token, True)
            conn.execute('UPDATE web_chat_rooms SET archived=1 WHERE token=?', (token,))
        with pytest.raises(requests.RequestError, match='recipient_has_no_private_room'):
            resolve(config)
        with db.get_db(config.db_path) as conn:
            assert [r.token for r in db.list_rooms(conn, 'bob', include_archived=True)] == [token]

    def test_a_talk_binding_is_captured_and_moves_the_fingerprint(self, setup):
        config = setup[0]
        token = _bob_room(config)
        plain = resolve(config)
        assert plain['talk_ref'] is None
        assert resolve(config)['fingerprint'] == plain['fingerprint']
        with db.get_db(config.db_path) as conn:
            db.add_room_binding(conn, token, 'talk', 'bob-talk')
        bound = resolve(config)
        assert bound['talk_ref'] == 'bob-talk'
        assert bound['fingerprint'] != plain['fingerprint']
        assert bound['fingerprint'] == dest.destination_fingerprint(bound)


class TestWhatIsStoredAndRendered:
    def test_the_stored_destination_carries_no_number_or_binding(self, setup):
        config = setup[0]
        _bob_room(config)
        _sms(config)
        for requested in ('room', 'whatsapp', 'sms'):
            stored = dest.stored_destination(resolve(config, requested))
            assert 'fingerprint' not in stored and '+1555' not in json.dumps(stored)
        assert resolve(config, 'sms')['fingerprint'] == requests.text_hash('+15557654321')

    @pytest.mark.parametrize('kind,instruction', [
        ('room', 'To answer Alice Example, reply to this message.'),
        ('whatsapp', 'To send your answer to Alice Example, reply to this message or send !relay reply R <answer>.'),
        ('sms', 'To answer Alice Example, send !relay reply R <answer>.'),
    ])
    def test_only_the_reply_instruction_varies(self, setup, kind, instruction):
        config = setup[0]
        wording = dest.render_question(config, asker='alice', text='What time?',
                                       destination={'kind': kind}, relay_id='R')
        assert wording.endswith(instruction + ' Only that answer will be shared.')
        assert 'Alice Example (alice):\n\nWhat time?\n\n' in wording

    def test_nothing_is_shortened_to_fit(self, setup):
        config = setup[0]
        config.sms.max_segments = 1
        wording = 'x' * 200
        with pytest.raises(requests.RequestError, match='invalid_rendering'):
            dest.fit_question(config, {'kind': 'sms'}, wording)
        assert dest.fit_question(config, {'kind': 'sms'}, 'short')[0] == 'short'
        long = 'y' * 30001
        with pytest.raises(requests.RequestError, match='invalid_rendering'):
            dest.fit_question(config, {'kind': 'room', 'talk_ref': 'talk'}, long)
        assert dest.fit_question(config, {'kind': 'room', 'talk_ref': None}, long) == (long, None)


class TestTheHoldGate:
    """Until SMS can be delivered, holding one is refused."""

    @pytest.mark.parametrize('via', [None, 'room'])
    def test_a_room_question_is_held_and_names_its_room(self, setup, via):
        config = setup[0]
        token = _bob_room(config)
        held = hold(setup, via=via)
        assert held['status'] == 'held'
        assert "through Bob's room #assistant?" in held['preview']
        with db.get_db(config.db_path) as conn:
            row = conn.execute('SELECT surface,destination,provider,binding_fingerprint FROM message_relays WHERE id=?',
                               (held['relay_id'],)).fetchone()
        assert row['surface'] == 'room' and row['provider'] == 'room'
        assert json.loads(row['destination'])['room_token'] == token
        assert row['binding_fingerprint'] == dest.destination_fingerprint(
            {'kind': 'room', 'room_token': token, 'talk_ref': None})

    @pytest.mark.parametrize('via', ['sms'])
    def test_an_undeliverable_kind_is_refused_and_nothing_is_held(self, setup, via):
        config = setup[0]
        _bob_room(config)
        _sms(config)
        with pytest.raises(requests.RequestError, match='destination_unavailable'):
            hold(setup, via=via)
        with db.get_db(config.db_path) as conn:
            assert not conn.execute('SELECT 1 FROM whatsapp_skill_requests').fetchone()
            assert not conn.execute('SELECT 1 FROM message_relays').fetchone()

    def test_the_preference_can_route_a_plain_ask_to_whatsapp(self, setup):
        config = setup[0]
        _bob_room(config)
        _prefer(config, 'whatsapp')
        held = hold(setup, via=None)
        assert held['status'] == 'held'

    def test_the_whatsapp_hold_records_its_destination_and_names_it(self, setup):
        config = setup[0]
        held = hold(setup)
        assert "through Bob's WhatsApp?" in held['preview']
        assert '+1555' not in held['preview']
        with db.get_db(config.db_path) as conn:
            row = conn.execute('SELECT surface,destination,provider FROM message_relays WHERE id=?',
                               (held['relay_id'],)).fetchone()
        assert row['surface'] == 'whatsapp' and row['provider'] == config.whatsapp.provider
        assert json.loads(row['destination']) == {'kind': 'whatsapp', 'label': "Bob's WhatsApp"}

    def test_an_unknown_via_is_refused(self, setup):
        with pytest.raises(requests.RequestError, match='invalid_via'):
            hold(setup, via='talk')

    def test_a_block_outranks_every_destination_code(self, setup):
        config = setup[0]
        with db.get_db(config.db_path) as conn:
            relays.block(conn, actor_user_id='bob', asker_user_id='alice')
        for via in (None, 'room', 'sms', 'whatsapp'):
            with pytest.raises(requests.RequestError, match='recipient_unavailable'):
                hold(setup, via=via)
