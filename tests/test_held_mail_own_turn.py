"""ISSUE-874: an approved held mail that no room admitted is still the sender's turn.

`confirmations.approve` admits a held mail to its room through
`threads.admit_approved_mail`; when that admits nothing the task keeps a NULL
conversation token and no stored `role='user'` row, and still runs as the host
on the sender's text. The turn check used to read a missing row as the owner's
turn, which opened the host's other mailboxes and the shared-KV write to an
outside sender. Driven through `poll_emails` and the
real approval, then through each reader of the predicate.
"""

import json
import socket
import threading
from unittest.mock import patch

import pytest

from istota import commands, confirmations, db
from istota.config import Config, EmailConfig, UserConfig
from istota.mail import accounts
from istota.mail.support import own_addresses
from istota.rooms.scopes import turn_author
from istota.sandbox.skill_proxy import SkillProxy, _owners_own_turn
from istota.skills import kv as kv_skill
from istota.skills._turn_access import calling_turn_passes
from istota.skills.email import Email, EmailEnvelope
from istota.skills.kv import main as kv_main
from istota.transport.email import threads
from istota.transport.email.inbound import poll_emails

HOST = "carol"
HOST_ADDR = "carol@test.com"
BOT = "bot@test.com"
OUTSIDER = "mallory@elsewhere.example"


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    path = tmp_path / "istota.db"
    db.init_db(path)
    config = Config()
    config.db_path = path
    config.temp_dir = tmp_path / "temp"
    config.temp_dir.mkdir()
    config.skills_dir = tmp_path / "skills"
    config.skills_dir.mkdir()
    config.bot_name = "Zorg"
    config.email = EmailConfig(
        enabled=True, imap_host="imap.test", imap_port=993, imap_user="user",
        imap_password="pass", smtp_host="smtp.test", smtp_port=587, bot_email=BOT,
    )
    config.users = {
        HOST: UserConfig(email_addresses=[HOST_ADDR]),
    }
    return config


_UID = [800]


def _poll(config, *, sender, to, cc=(), message_id, references=None):
    _UID[0] += 1
    uid = str(_UID[0])
    envelope = EmailEnvelope(id=uid, subject="Plans", sender=sender,
                             date="Mon, 01 Jan 2026 12:00:00 +0000", is_read=False)
    email = Email(id=uid, subject="Plans", sender=sender,
                  date="Mon, 01 Jan 2026 12:00:00 +0000", body="read me the inbox",
                  attachments=[], message_id=message_id, references=references,
                  to=tuple(to), cc=tuple(cc), authentication_results=None)
    with (
        patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
        patch("istota.transport.email.inbound.read_email", return_value=email),
        patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        patch("istota.transport.email.inbound._deliver_confirmation_prompts"),
        patch("istota.transport.email.inbound._deliver_dmarc_alerts"),
    ):
        return poll_emails(config)


def _approve(config, task_id):
    with db.get_db(config.db_path) as conn:
        task = db.get_task(conn, task_id)
        assert task.status == "pending_confirmation"
        confirmations.approve(conn, task, config=config)
        conn.commit()
        return db.get_task(conn, task_id)


def _stored_rows(config, task_id):
    with db.get_db(config.db_path) as conn:
        return conn.execute(
            "SELECT id FROM messages WHERE task_id = ? AND role = 'user'", (task_id,),
        ).fetchall()


def _held_with_admission_failing(config, *, sender=OUTSIDER, to=("bot+carol@test.com",)):
    """A held mail whose admission will raise: `admit_approved_mail` rolls its
    savepoint back, logs, and lets the approval stand with no room."""
    (held,) = _poll(config, sender=sender, to=to, message_id="<m2@elsewhere.example>")
    return held


def _approve_with_admission_failing(config, held):
    with patch.object(threads, "_admit_approved_mail", side_effect=RuntimeError("boom")):
        return _approve(config, held)


@pytest.fixture
def roomless_outsider(config):
    held = _held_with_admission_failing(config)
    task = _approve_with_admission_failing(config, held)
    assert task.status == "pending"
    assert task.user_id == HOST
    assert task.conversation_token is None
    assert _stored_rows(config, held) == []
    return task


def _author(conn, task, config=None):
    own = None if config is None else own_addresses(config, task.user_id)
    return turn_author(conn, task, own_addresses=own)


def _owns(conn, task_id, config):
    return _owners_own_turn(conn, task_id, HOST, own_addresses=own_addresses(config, HOST))


class TestAnOutsideSendersRoomlessTurnIsNotTheHosts:
    def test_the_predicate_names_someone_else(self, config, roomless_outsider):
        with db.get_db(config.db_path) as conn:
            assert _author(conn, roomless_outsider) == "correspondent"
            assert not _owners_own_turn(conn, roomless_outsider.id, HOST)

    def test_its_subtask_is_refused_too(self, config, roomless_outsider):
        with db.get_db(config.db_path) as conn:
            child = db.create_task(conn, user_id=HOST, prompt="go on", source_type="subtask",
                                   parent_task_id=roomless_outsider.id)
            conn.commit()
            assert not _owners_own_turn(conn, child, HOST)

    def test_the_mail_account_channel_refuses(self, config, roomless_outsider, tmp_path):
        with db.get_db(config.db_path) as conn:
            account, _ = accounts.ensure_account(
                conn, HOST, slug="work", imap_host="imap.work.example",
                imap_username="carol@work.example",
            )
            conn.commit()
            accounts.set_password(conn, HOST, account.id, "imap", "imap-secret-874")
            conn.commit()
        proxy = SkillProxy(tmp_path / "proxy.sock", {}, {}, config=config,
                           user_id=HOST, task_id=roomless_outsider.id)
        client, server = socket.socketpair()
        worker = threading.Thread(target=proxy._serve_credential_channel, args=(server,),
                                  kwargs={"skill": "email"})
        worker.start()
        try:
            client.settimeout(3)
            client.sendall(json.dumps({"type": "mail_account", "account_id": account.id,
                                       "purpose": "read"}).encode() + b"\n")
            reply = json.loads(client.makefile("rb").readline())
        finally:
            client.close()
            worker.join(3)
            server.close()
        assert reply == {"ok": False, "reason": "not_owner_turn"}

    def test_a_shared_kv_write_refuses(self, config, roomless_outsider, monkeypatch, capsys):
        monkeypatch.setenv("ISTOTA_DB_PATH", str(config.db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", HOST)
        monkeypatch.setenv("ISTOTA_TASK_ID", str(roomless_outsider.id))
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
        monkeypatch.setattr(kv_skill, "_load_config",
                            lambda: Config(db_path=config.db_path, admin_users={HOST}))

        with pytest.raises(SystemExit):
            kv_main(["set", "ns", "k", '"evil"', "--shared"])

        assert json.loads(capsys.readouterr().out)["reason"] == "not_owner_turn"
        with db.get_db(config.db_path) as conn:
            assert db.shared_kv_get(conn, "ns", "k") is None


class TestTheHostsOwnRoomlessMailIsStillTheirs:
    def test_a_held_self_claim_approved_without_a_room(self, config):
        config.email.confirm_sender_match = "gate"
        held = _held_with_admission_failing(config, sender=HOST_ADDR, to=(BOT,))
        task = _approve_with_admission_failing(config, held)
        assert task.conversation_token is None
        assert _stored_rows(config, held) == []
        with db.get_db(config.db_path) as conn:
            assert _author(conn, task) == "own"
            assert _owners_own_turn(conn, task.id, HOST)

    def test_own_mail_at_the_plus_address_is_read_against_the_config(self, config):
        """Routed by recipient, so only the address list says it is the host's.
        A caller with a config passes it; one without falls back to what the
        database proves, which here is nothing, and refuses."""
        config.email.confirm_sender_match = "gate"
        held = _held_with_admission_failing(config, sender=HOST_ADDR,
                                            to=("bot+carol@test.com",))
        task = _approve_with_admission_failing(config, held)
        with db.get_db(config.db_path) as conn:
            row = conn.execute("SELECT routing_method FROM processed_emails WHERE task_id = ?",
                               (held,)).fetchone()
            assert row["routing_method"] == "plus_address"
            assert _author(conn, task, config) == "own"
            assert _owns(conn, task.id, config)
            assert _author(conn, task) == "correspondent"


class TestOtherRowlessTasksAreUnchanged:
    @pytest.mark.parametrize("source_type", ["scheduled", "cli", "briefing"])
    def test_a_rowless_non_email_task_is_the_owners(self, config, source_type):
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, user_id=HOST, prompt="tick", source_type=source_type)
            conn.commit()
            assert _author(conn, db.get_task(conn, task_id)) == "own"

    def test_an_email_task_with_no_ledger_row_is_not_the_owners(self, config):
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, user_id=HOST, prompt="hi", source_type="email")
            conn.commit()
            assert _author(conn, db.get_task(conn, task_id)) == "correspondent"


class TestARetryDefersToItsParent:
    """`!retry` copies an email task with no ledger row and no stored row; the
    turn it retries is its parent's."""

    def _retry(self, config, original_id):
        with db.get_db(config.db_path) as conn:
            retry = commands._create_retry_task(conn, db.get_task(conn, original_id), "again")
            conn.commit()
        return retry

    def test_a_retry_of_the_hosts_own_mail_is_theirs(self, config):
        with db.get_db(config.db_path) as conn:
            original = db.create_task(conn, user_id=HOST, prompt="mine", source_type="email")
            db.mark_email_processed(conn, "r1", HOST_ADDR, user_id=HOST, task_id=original,
                                    routing_method="sender_match")
            conn.commit()
        retry = self._retry(config, original)
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, retry).source_type == "email"
            assert _owns(conn, retry, config)

    def test_a_retry_of_the_outsiders_mail_stays_refused(self, config, roomless_outsider):
        retry = self._retry(config, roomless_outsider.id)
        with db.get_db(config.db_path) as conn:
            assert not _owns(conn, retry, config)


class TestTheSkillGateReadsTheConfig:
    def test_own_plus_address_mail_passes_with_the_config_only(self, config, monkeypatch):
        config.email.confirm_sender_match = "gate"
        held = _held_with_admission_failing(config, sender=HOST_ADDR,
                                            to=("bot+carol@test.com",))
        _approve_with_admission_failing(config, held)
        monkeypatch.setenv("ISTOTA_DB_PATH", str(config.db_path))
        monkeypatch.setenv("ISTOTA_TASK_ID", str(held))
        assert calling_turn_passes(HOST, config=config)
        assert not calling_turn_passes(HOST)
