"""The connected mail account registry: `istota.mail.accounts`.

The properties that matter are ownership (every by-id path is scoped on the
user, so one user's id reaches nothing of another's), the permission rule
(`read_send` needs SMTP and a From address, held by the store and by the
table's own CHECK), and that a password never leaves the store except through
`resolve_for_daemon`, and only for the purpose that needs it.
"""

from __future__ import annotations

import dataclasses
import sqlite3

import pytest

from istota import db
from istota.config import MailAccountsConfig
from istota.credentials import store
from istota.mail import accounts, drafts
from istota.mail.accounts import MailAccountError, ResolvedAccount, Unresolved


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


@pytest.fixture
def conn(db_path):
    with db.get_db(db_path) as c:
        yield c


def _ensure(conn, user="alice", slug="work", **fields):
    base = {"imap_host": "imap.example.com", "imap_username": "alice@example.com"}
    base.update(fields)
    account, state = accounts.ensure_account(conn, user, slug=slug, **base)
    conn.commit()
    return account, state


def _sender(conn, user="alice", slug="work", **fields):
    values = {
        "permission": "read_send", "smtp_host": "smtp.example.com",
        "from_address": "Alice <Alice@Example.com>",
    }
    values.update(fields)
    return _ensure(conn, user, slug, **values)


def _password(conn, account, which="imap", value="imap-secret", user="alice"):
    state = accounts.set_password(conn, user, account.id, which, value)
    conn.commit()
    return state


def _secret_keys(conn, user="alice"):
    rows = conn.execute(
        "SELECT key FROM secrets WHERE user_id = ? AND service = 'mail_account' ORDER BY key",
        (user,),
    ).fetchall()
    return [r["key"] for r in rows]


def _hold_draft(conn, account_id, user="alice"):
    draft_id = drafts.hold(
        conn, user_id=user, task_id=None, room_token=None,
        to_addrs=["dana@example.org"], cc_addrs=[], bcc_addrs=[],
        subject="Re: lunch", body="Yes.", html=False, in_reply_to=None,
        references=None, attachments=[], origin_target=None,
        hold_reason="untrusted_recipient",
    )
    conn.execute("UPDATE outbound_drafts SET account_id = ? WHERE id = ?", (account_id, draft_id))
    conn.commit()
    return draft_id


def _resolve(db_path, account, *, purpose="read", user="alice", **settings):
    return accounts.resolve_for_daemon(
        db_path, user, account.id, purpose=purpose,
        settings=MailAccountsConfig(**settings),
    )


class TestEnsure:
    def test_created_then_noop_then_updated(self, conn):
        account, state = _ensure(conn)
        assert state == "created"
        assert account.permission == "read"
        assert account.imap_port == 993 and account.imap_security == "tls"
        assert account.default_folder == "INBOX"
        assert account.smtp_host is None

        again, state = _ensure(conn)
        assert state == "noop"
        assert again.id == account.id

        changed, state = _ensure(conn, label="Work")
        assert state == "updated"
        assert changed.label == "Work"
        assert changed.id == account.id

    def test_a_field_left_off_keeps_its_value(self, conn):
        account, _ = _ensure(conn, label="Work", default_folder="Archive")
        updated, state = accounts.ensure_account(conn, "alice", slug="work", imap_port=1993)
        assert state == "updated"
        assert (updated.label, updated.default_folder, updated.imap_port) == ("Work", "Archive", 1993)

    def test_rename_keeps_the_id_and_the_passwords(self, conn):
        account, _ = _ensure(conn)
        _password(conn, account)
        renamed, state = accounts.ensure_account(conn, "alice", slug="work", rename_to="office")
        conn.commit()
        assert state == "updated"
        assert renamed.id == account.id and renamed.slug == "office"
        assert accounts.get_account(conn, "alice", "work") is None
        assert renamed.has_imap_password

    def test_the_same_slug_for_two_users_is_two_accounts(self, conn):
        alice, _ = _ensure(conn, user="alice")
        bob, state = _ensure(conn, user="bob")
        assert state == "created"
        assert alice.id != bob.id

    def test_max_per_user(self, conn):
        accounts.ensure_account(conn, "alice", slug="one", imap_host="h.example.com",
                                imap_username="u", max_accounts=2)
        accounts.ensure_account(conn, "alice", slug="two", imap_host="h.example.com",
                                imap_username="u", max_accounts=2)
        with pytest.raises(MailAccountError) as e:
            accounts.ensure_account(conn, "alice", slug="three", imap_host="h.example.com",
                                    imap_username="u", max_accounts=2)
        assert e.value.reason == "too_many_accounts"
        # An existing slug is an update, not a new row, so it is still allowed.
        _, state = accounts.ensure_account(conn, "alice", slug="two", label="Two", max_accounts=2)
        assert state == "updated"
        # And the cap is per user.
        _, state = accounts.ensure_account(conn, "bob", slug="one", imap_host="h.example.com",
                                           imap_username="u", max_accounts=2)
        assert state == "created"

    def test_unknown_field_is_a_type_error(self, conn):
        with pytest.raises(TypeError):
            _ensure(conn, imap_password="nope")


class TestUpdateById:
    def test_slug_change_keeps_the_id_and_secrets(self, conn):
        account, _ = _sender(conn)
        _password(conn, account, "imap", "one")
        accounts.update_account(conn, "alice", account.id, smtp_same_login=False,
                                smtp_username="alice-smtp")
        _password(conn, account, "smtp", "two")
        renamed = accounts.update_account(conn, "alice", account.id, slug="office")
        conn.commit()
        assert renamed.id == account.id and renamed.slug == "office"
        assert _secret_keys(conn) == [f"{account.id}.imap_password", f"{account.id}.smtp_password"]

    def test_slug_taken(self, conn):
        _ensure(conn, slug="work")
        other, _ = _ensure(conn, slug="home")
        with pytest.raises(MailAccountError) as e:
            accounts.update_account(conn, "alice", other.id, slug="work")
        assert e.value.reason == "slug_taken"

    def test_removing_smtp_drops_sending_and_its_password(self, conn):
        account, _ = _sender(conn, smtp_same_login=False, smtp_username="alice-smtp")
        _password(conn, account, "smtp", "smtp-secret")
        updated = accounts.update_account(conn, "alice", account.id, smtp_host=None)
        conn.commit()
        assert updated.permission == "read"
        assert updated.smtp_host is None
        assert not updated.has_smtp_password
        assert _secret_keys(conn) == []


class TestOwnership:
    """An id belonging to another user reaches nothing, on every by-id path."""

    def test_by_id_reads_and_writes_refuse_another_users_id(self, conn, db_path):
        account, _ = _sender(conn, user="alice")
        _password(conn, account, user="alice")

        assert accounts.get_account_by_id(conn, "bob", account.id) is None
        assert accounts.get_account(conn, "bob", "work") is None
        assert accounts.list_accounts(conn, "bob") == []
        with pytest.raises(MailAccountError) as e:
            accounts.update_account(conn, "bob", account.id, label="mine now")
        assert e.value.reason == "not_found"
        with pytest.raises(MailAccountError):
            accounts.set_password(conn, "bob", account.id, "imap", "planted")
        with pytest.raises(MailAccountError):
            accounts.clear_password(conn, "bob", account.id, "imap")
        assert accounts.delete_account(conn, "bob", account.id) is False
        conn.commit()

        mine = accounts.get_account_by_id(conn, "alice", account.id)
        assert mine is not None and mine.label == ""
        assert store.get_secret(db_path, "alice", "mail_account", f"{account.id}.imap_password") == "imap-secret"
        assert store.get_secret(db_path, "bob", "mail_account", f"{account.id}.imap_password") is None
        assert _resolve(db_path, account, user="bob") == Unresolved("account_not_found")


class TestPermissionRule:
    def test_read_send_needs_smtp(self, conn):
        with pytest.raises(MailAccountError) as e:
            _ensure(conn, permission="read_send", from_address="a@example.com")
        assert e.value.field == "smtp_host"

    def test_read_send_needs_a_from_address(self, conn):
        with pytest.raises(MailAccountError) as e:
            _ensure(conn, permission="read_send", smtp_host="smtp.example.com")
        assert e.value.field == "from_address"

    def test_the_table_refuses_it_too(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO mail_accounts (user_id, slug, permission, imap_host, imap_username) "
                "VALUES ('alice', 'raw', 'read_send', 'h', 'u')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO mail_accounts (user_id, slug, permission, imap_host, imap_username, smtp_host) "
                "VALUES ('alice', 'raw', 'read_send', 'h', 'u', 's')"
            )

    def test_from_address_is_one_address(self, conn):
        account, _ = _sender(conn)
        assert account.from_address == "alice@example.com"
        with pytest.raises(MailAccountError) as e:
            _sender(conn, slug="two", from_address="a@example.com, b@example.com")
        assert e.value.field == "from_address"


class TestValidation:
    @pytest.mark.parametrize("slug", ["bot", "Work", "", "a" * 33, "a/b", "-work", None])
    def test_validate_slug_rejects(self, slug):
        with pytest.raises(MailAccountError):
            accounts.validate_slug(slug)

    @pytest.mark.parametrize("slug", ["work", "a", "a" * 32, "w0rk_2-x"])
    def test_validate_slug_accepts(self, slug):
        assert accounts.validate_slug(slug) == slug

    @pytest.mark.parametrize("host", [
        "imaps://imap.example.com", "imap.example.com:993", "imap.example.com/x", "", "two words",
    ])
    def test_host_shape(self, conn, host):
        with pytest.raises(MailAccountError) as e:
            _ensure(conn, imap_host=host)
        assert e.value.field == "imap_host"

    @pytest.mark.parametrize("field,value", [
        ("imap_port", 0), ("imap_port", 65536), ("imap_port", "993"), ("smtp_port", True),
        ("imap_security", "ssl"), ("permission", "write"),
        ("default_folder", "x" * 255), ("default_folder", "In\nbox"), ("default_folder", ""),
        ("sent_folder", "Sent\x00"),
    ])
    def test_field_refusals_name_the_field(self, conn, field, value):
        with pytest.raises(MailAccountError) as e:
            _ensure(conn, **{field: value})
        assert e.value.field == field

    def test_a_refusal_does_not_echo_the_value(self, conn):
        with pytest.raises(MailAccountError) as e:
            _ensure(conn, imap_host="https://secret-host.example.com")
        assert "secret-host" not in str(e.value)

    def test_separate_smtp_login_needs_a_username(self, conn):
        with pytest.raises(MailAccountError) as e:
            _sender(conn, smtp_same_login=False)
        assert e.value.field == "smtp_username"


class TestNoPasswordInTheListing:
    def test_list_accounts_carries_presence_not_values(self, conn):
        account, _ = _ensure(conn)
        _password(conn, account, value="hunter2-imap")
        [listed] = accounts.list_accounts(conn, "alice")
        assert listed.has_imap_password and not listed.has_smtp_password
        names = {f.name for f in dataclasses.fields(listed)}
        assert not {n for n in names if "password" in n} - {"has_imap_password", "has_smtp_password"}
        assert "hunter2-imap" not in repr(listed)

    def test_a_resolved_account_keeps_passwords_out_of_its_repr(self, conn, db_path):
        account, _ = _sender(conn)
        _password(conn, account, value="hunter2-imap")
        resolved = _resolve(db_path, account, purpose="send")
        assert isinstance(resolved, ResolvedAccount)
        assert "hunter2" not in repr(resolved)

    def test_set_password_states(self, conn):
        account, _ = _ensure(conn)
        assert _password(conn, account, value="a") == "created"
        assert _password(conn, account, value="a") == "noop"
        assert _password(conn, account, value="b") == "updated"

    def test_smtp_password_needs_an_smtp_server(self, conn):
        account, _ = _ensure(conn)
        with pytest.raises(MailAccountError):
            accounts.set_password(conn, "alice", account.id, "smtp", "x")


class TestDelete:
    def test_removes_row_secrets_and_discards_pending_drafts(self, conn):
        account, _ = _sender(conn, smtp_same_login=False, smtp_username="s")
        _password(conn, account, "imap", "i")
        _password(conn, account, "smtp", "s")
        pending = _hold_draft(conn, account.id)
        bot_box_draft = _hold_draft(conn, None)
        assert accounts.get_account_by_id(conn, "alice", account.id).pending_drafts == 1

        assert accounts.delete_account(conn, "alice", account.id, by="web") is True
        conn.commit()

        assert accounts.get_account_by_id(conn, "alice", account.id) is None
        assert _secret_keys(conn) == []
        assert drafts.get(conn, pending).status == drafts.STATUS_DISCARDED
        assert drafts.get(conn, bot_box_draft).status == drafts.STATUS_PENDING

    def test_a_sending_draft_is_left_alone(self, conn):
        account, _ = _sender(conn)
        draft_id = _hold_draft(conn, account.id)
        conn.execute("UPDATE outbound_drafts SET status = 'sending' WHERE id = ?", (draft_id,))
        accounts.delete_account(conn, "alice", account.id)
        conn.commit()
        assert drafts.get(conn, draft_id).status == drafts.STATUS_SENDING

    def test_rollback_leaves_all_three(self, conn):
        account, _ = _sender(conn, smtp_same_login=False, smtp_username="s")
        _password(conn, account, "imap", "i")
        _password(conn, account, "smtp", "s")
        draft_id = _hold_draft(conn, account.id)

        assert accounts.delete_account(conn, "alice", account.id) is True
        conn.rollback()

        assert accounts.get_account_by_id(conn, "alice", account.id) is not None
        assert len(_secret_keys(conn)) == 2
        assert drafts.get(conn, draft_id).status == drafts.STATUS_PENDING


class TestResolveForDaemon:
    def test_read_returns_imap_and_no_smtp(self, conn, db_path):
        account, _ = _sender(conn)
        _password(conn, account, value="imap-secret")
        resolved = _resolve(db_path, account, purpose="read")
        assert isinstance(resolved, ResolvedAccount)
        assert resolved.imap_password == "imap-secret"
        assert resolved.smtp_password is None
        assert resolved.account.slug == "work"

    def test_send_with_the_same_login_uses_the_imap_password(self, conn, db_path):
        account, _ = _sender(conn)
        _password(conn, account, value="imap-secret")
        resolved = _resolve(db_path, account, purpose="send")
        assert resolved.smtp_password == "imap-secret"

    def test_send_with_a_separate_login_needs_its_own_password(self, conn, db_path):
        account, _ = _sender(conn, smtp_same_login=False, smtp_username="s")
        _password(conn, account, value="imap-secret")
        assert _resolve(db_path, account, purpose="send") == Unresolved("password_missing")
        assert isinstance(_resolve(db_path, account, purpose="read"), ResolvedAccount)
        _password(conn, account, "smtp", "smtp-secret")
        assert _resolve(db_path, account, purpose="send").smtp_password == "smtp-secret"

    def test_send_on_a_read_account_is_refused(self, conn, db_path):
        account, _ = _ensure(conn)
        _password(conn, account)
        assert _resolve(db_path, account, purpose="send") == Unresolved("permission_denied")

    def test_allow_send_off_refuses_every_send(self, conn, db_path):
        account, _ = _sender(conn)
        _password(conn, account)
        assert _resolve(db_path, account, purpose="send", allow_send=False) == Unresolved("permission_denied")
        assert isinstance(_resolve(db_path, account, purpose="read", allow_send=False), ResolvedAccount)

    def test_feature_off(self, conn, db_path):
        account, _ = _ensure(conn)
        _password(conn, account)
        assert _resolve(db_path, account, enabled=False) == Unresolved("mail_accounts_disabled")

    def test_a_disabled_account_reads_as_not_found(self, conn, db_path):
        account, _ = _ensure(conn, enabled=False)
        _password(conn, account)
        assert _resolve(db_path, account) == Unresolved("account_not_found")

    def test_a_deleted_account_reads_as_not_found(self, conn, db_path):
        account, _ = _ensure(conn)
        _password(conn, account)
        accounts.delete_account(conn, "alice", account.id)
        conn.commit()
        assert _resolve(db_path, account) == Unresolved("account_not_found")

    def test_missing_password(self, conn, db_path):
        account, _ = _ensure(conn)
        assert _resolve(db_path, account) == Unresolved("password_missing")

    def test_no_master_key_reads_as_password_missing(self, conn, db_path, monkeypatch):
        account, _ = _ensure(conn)
        _password(conn, account)
        monkeypatch.delenv("ISTOTA_SECRET_KEY")
        assert _resolve(db_path, account) == Unresolved("password_missing")

    def test_unknown_purpose_raises(self, conn, db_path):
        account, _ = _ensure(conn)
        with pytest.raises(ValueError):
            _resolve(db_path, account, purpose="delete")


class TestConfig:
    def test_defaults(self):
        from istota.config import Config
        settings = Config().mail_accounts
        assert (settings.enabled, settings.allow_send, settings.max_per_user, settings.private_hosts) == (
            True, True, 10, [],
        )

    def test_the_section_loads(self, tmp_path):
        from istota.config import load_config
        path = tmp_path / "config.toml"
        path.write_text(
            "[mail_accounts]\nenabled = false\nallow_send = false\n"
            'max_per_user = 3\nprivate_hosts = ["mail.lan"]\n'
        )
        settings = load_config(path).mail_accounts
        assert (settings.enabled, settings.allow_send, settings.max_per_user, settings.private_hosts) == (
            False, False, 3, ["mail.lan"],
        )


class TestMigration:
    def test_outbound_drafts_gains_account_id(self, db_path):
        with db.get_db(db_path) as c:
            columns = {r[1] for r in c.execute("PRAGMA table_info(outbound_drafts)")}
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "account_id" in columns
        assert "mail_accounts" in tables
