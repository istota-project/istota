"""The recipient's inbox row for a relay question: what it shows and what closes it."""
import pytest

from istota import db
from istota.relay import relays
from istota.notification_resolvers import relay_question
from istota.notification_sources import NotificationRow, invalid_paths
from . import test_relay_questions
from .test_relay_answers import question, event, receive

setup = test_relay_questions.setup


def notice(config):
    with db.get_db(config.db_path) as conn:
        return dict(conn.execute("SELECT * FROM notifications WHERE source='relay_question'").fetchone())


def as_row(stored, **overrides):
    values = dict(id=stored['id'], user_id=stored['user_id'], source=stored['source'],
                  dedup_key=stored['dedup_key'], object_type=stored['object_type'],
                  object_id=stored['object_id'], severity=stored['severity'],
                  actionable=bool(stored['actionable']), title=stored['title'], body=stored['body'],
                  room_token=stored['room_token'])
    values.update(overrides)
    return NotificationRow(**values)


def view(config, row):
    with db.get_db(config.db_path) as conn:
        return relay_question.RESOLVER.resolve(config, conn, row)


def test_the_view_frames_the_question_and_the_stored_row_never_holds_it(setup):
    config = setup[0]
    relay_id = question(setup)
    stored = notice(config)
    assert stored['user_id'] == 'bob' and stored['object_id'] == relay_id and stored['state'] == 'open'
    assert 'What time?' not in stored['title'] + stored['body']
    shown = view(config, as_row(stored))
    assert 'What time?' in shown.body and 'UNTRUSTED RELAY QUESTION' in shown.body
    assert shown.body.startswith('Quote it on WhatsApp.')
    assert shown.actions == () and invalid_paths(shown) == []


def test_a_room_question_links_to_the_chat_within_the_url_allowlist(setup):
    config = setup[0]
    relay_id = question(setup)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE message_relays SET surface='room',destination=? WHERE id=?",
                     ('{"kind":"room","label":"Bob\'s room #assistant","room_token":"r","talk_ref":null}', relay_id))
    shown = view(config, as_row(notice(config)))
    (action,) = shown.actions
    assert action.method == 'LINK' and action.href == relay_question.RELAY_QUESTION_HREF
    assert invalid_paths(shown) == []
    assert shown.body.startswith("Reply to the message in Bob's room #assistant.")


def test_another_users_row_naming_the_relay_resolves_to_nothing(setup):
    config = setup[0]
    question(setup)
    assert view(config, as_row(notice(config), user_id='alice')) is None


@pytest.mark.parametrize('close', ['answered', 'cancelled', 'expired', 'failed'])
def test_every_way_out_of_waiting_closes_the_row_and_the_resolver_agrees(setup, close):
    config = setup[0]
    relay_id = question(setup)
    stored = notice(config)
    if close == 'answered':
        assert receive(config, event(config, 'Seven')).disposition == 'relay_answer'
    else:
        with db.get_db(config.db_path) as conn:
            if close == 'cancelled':
                relays.cancel_relay(conn, actor_user_id='alice', relay_id=relay_id)
            elif close == 'expired':
                conn.execute("UPDATE message_relays SET expires_at=datetime('now','-1 second')")
                relays.expire_relays(conn)
            else:
                relays.reconcile_question_delivery(conn, logical_key='relay-question:' + relay_id, status='failed')
    after = notice(config)
    assert after['state'] == 'resolved' and after['resolved_by'] == 'system'
    assert view(config, as_row(stored)) is None


def test_an_uncertain_question_is_still_open_for_its_recipient(setup):
    config = setup[0]
    relay_id = question(setup)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE message_relays SET state='uncertain' WHERE id=?", (relay_id,))
    assert view(config, as_row(notice(config))) is not None
