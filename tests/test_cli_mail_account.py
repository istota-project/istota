"""`istota mail-account` through the real parser and database.

The smoke scenario provisions an account this way (`ensure`, then
`set-password` on stdin), so the shape that matters is the operator's: the
STATE line Ansible reads, the password taken from stdin and never from argv,
and no password in anything printed.
"""

import io
import json

import pytest

from istota import cli, db
from istota.config import Config, UserConfig
from istota.credentials import store
from istota.mail import accounts, drafts

PASSWORD = "imap pass with spaces"


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    path = tmp_path / "istota.db"
    db.init_db(path)
    config = Config(db_path=path, users={"alice": UserConfig(display_name="Alice")})
    monkeypatch.setattr(cli, "load_config", lambda *a: config)
    monkeypatch.setattr(cli, "setup_logging", lambda *a, **kw: None)
    return config


@pytest.fixture
def invoke(config, monkeypatch, capsys):
    def run(*args, stdin=""):
        monkeypatch.setattr("sys.argv", ["istota", "mail-account", *args])
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        code = 0
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
        captured = capsys.readouterr()
        return code, captured.out, captured.err
    return run


ENSURE = ["ensure", "alice", "work", "--imap-host", "imap.example.com",
          "--imap-username", "alice@example.com"]


def _account(config, slug="work"):
    with db.get_db(config.db_path) as conn:
        return accounts.get_account(conn, "alice", slug)


def test_ensure_reports_created_then_noop_then_updated(config, invoke):
    code, out, _ = invoke(*ENSURE)
    assert code == 0 and "STATE: created" in out
    code, out, _ = invoke(*ENSURE)
    assert code == 0 and "STATE: noop" in out
    code, out, _ = invoke("ensure", "alice", "work", "--label", "Work")
    assert code == 0 and "STATE: updated" in out
    assert _account(config).label == "Work"


def test_ensure_sending_account(config, invoke):
    code, out, err = invoke(
        *ENSURE, "--permission", "read_send", "--smtp-host", "smtp.example.com",
        "--no-smtp-same-login", "--smtp-username", "alice-smtp",
        "--from-address", "alice@example.com", "--sent-folder", "Sent",
    )
    assert code == 0, err
    account = _account(config)
    assert account.permission == "read_send"
    assert not account.smtp_same_login and account.smtp_username == "alice-smtp"

    code, out, _ = invoke("ensure", "alice", "work", "--no-smtp")
    assert code == 0
    assert _account(config).permission == "read"


def test_ensure_refusal_exits_one_and_names_the_field(invoke):
    code, out, err = invoke(*ENSURE, "--permission", "read_send")
    assert code == 1
    assert "smtp_host" in err
    assert "STATE" not in out


def test_ensure_refuses_an_unknown_user(invoke):
    code, _, err = invoke("ensure", "mallory", "work", "--imap-host", "h.example.com",
                          "--imap-username", "u")
    assert code == 1 and "mallory" in err


def test_ensure_rename_keeps_the_password(config, invoke):
    invoke(*ENSURE)
    invoke("set-password", "alice", "work", "--imap", stdin=PASSWORD + "\n")
    code, out, _ = invoke("ensure", "alice", "work", "--rename-to", "office")
    assert code == 0 and "STATE: updated" in out
    renamed = _account(config, "office")
    assert renamed.has_imap_password


def test_set_password_reads_stdin_and_never_echoes(config, invoke):
    invoke(*ENSURE)
    code, out, err = invoke("set-password", "alice", "work", "--imap", stdin=PASSWORD + "\n")
    assert code == 0 and "STATE: created" in out
    assert PASSWORD not in out + err
    account = _account(config)
    # The trailing newline is the line ending, not part of the password.
    assert store.get_secret(config.db_path, "alice", "mail_account",
                            f"{account.id}.imap_password") == PASSWORD

    code, out, _ = invoke("set-password", "alice", "work", "--imap", stdin=PASSWORD)
    assert "STATE: noop" in out


def test_set_password_has_no_value_argument(invoke):
    invoke(*ENSURE)
    code, _, _ = invoke("set-password", "alice", "work", "--imap", PASSWORD)
    assert code == 2  # argparse: the password is not an argument


def test_set_password_requires_imap_or_smtp(invoke):
    invoke(*ENSURE)
    code, _, _ = invoke("set-password", "alice", "work", stdin=PASSWORD)
    assert code == 2


def test_set_password_empty_stdin_is_refused(config, invoke):
    invoke(*ENSURE)
    code, _, err = invoke("set-password", "alice", "work", "--imap", stdin="\n")
    assert code == 1
    assert not _account(config).has_imap_password


def test_set_password_unknown_slug(invoke):
    code, _, err = invoke("set-password", "alice", "nope", "--imap", stdin=PASSWORD)
    assert code == 1 and "nope" in err


def test_set_password_without_a_master_key(config, invoke, monkeypatch):
    invoke(*ENSURE)
    monkeypatch.delenv("ISTOTA_SECRET_KEY")
    code, _, err = invoke("set-password", "alice", "work", "--imap", stdin=PASSWORD)
    assert code == 1 and "ISTOTA_SECRET_KEY" in err
    assert PASSWORD not in err


def test_clear_password(config, invoke):
    invoke(*ENSURE)
    invoke("set-password", "alice", "work", "--imap", stdin=PASSWORD)
    code, out, _ = invoke("clear-password", "alice", "work", "--imap")
    assert code == 0 and "STATE: removed" in out
    assert not _account(config).has_imap_password
    code, out, _ = invoke("clear-password", "alice", "work", "--imap")
    assert "STATE: noop" in out


def test_list_never_prints_a_password(invoke):
    invoke(*ENSURE)
    invoke("set-password", "alice", "work", "--imap", stdin=PASSWORD)
    code, out, _ = invoke("list", "alice")
    assert code == 0 and "work" in out and "imap_password=set" in out
    assert PASSWORD not in out
    code, out, _ = invoke("list", "alice", "--json")
    [row] = json.loads(out)
    assert row["slug"] == "work" and row["imap_password_set"] is True
    assert PASSWORD not in out


def test_remove_deletes_secrets_and_discards_drafts(config, invoke):
    invoke(*ENSURE, "--permission", "read_send", "--smtp-host", "smtp.example.com",
           "--from-address", "alice@example.com")
    invoke("set-password", "alice", "work", "--imap", stdin=PASSWORD)
    account = _account(config)
    with db.get_db(config.db_path) as conn:
        draft_id = drafts.hold(
            conn, user_id="alice", task_id=None, room_token=None,
            to_addrs=["dana@example.org"], cc_addrs=[], bcc_addrs=[],
            subject="s", body="b", html=False, in_reply_to=None, references=None,
            attachments=[], origin_target=None, hold_reason="untrusted_recipient",
        )
        conn.execute("UPDATE outbound_drafts SET account_id = ? WHERE id = ?",
                     (account.id, draft_id))

    code, out, _ = invoke("remove", "alice", "work")
    assert code == 0 and "STATE: removed" in out and "1 pending draft" in out
    assert _account(config) is None
    with db.get_db(config.db_path) as conn:
        assert drafts.get(conn, draft_id).status == drafts.STATUS_DISCARDED
        left = conn.execute(
            "SELECT count(*) FROM secrets WHERE service = 'mail_account'"
        ).fetchone()[0]
    assert left == 0

    code, out, _ = invoke("remove", "alice", "work")
    assert code == 0 and "STATE: noop" in out


def test_test_signs_in_through_the_shared_check_and_prints_no_password(config, invoke, monkeypatch):
    from istota.mail import connect

    logins = []

    class Mailbox:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, user, password):
            logins.append((user, password))

    monkeypatch.setattr(connect, "open_imap", lambda email, **kw: Mailbox())
    config.mail_accounts.private_hosts = ["imap.example.com"]
    invoke(*ENSURE)
    invoke("set-password", "alice", "work", "--imap", stdin=PASSWORD)

    code, out, err = invoke("test", "alice", "work")
    assert code == 0, err
    assert "imap: ok: Signed in to imap.example.com." in out and "RESULT: ok" in out
    assert logins == [("alice@example.com", PASSWORD)]
    assert PASSWORD not in out and PASSWORD not in err


def test_test_fails_plainly_without_a_password_and_on_an_unknown_slug(config, invoke):
    invoke(*ENSURE)
    code, out, _ = invoke("test", "alice", "work")
    assert code == 1
    assert "account: failed: No password is stored for this account." in out
    assert "RESULT: failed" in out

    code, _, err = invoke("test", "alice", "nope")
    assert code == 1 and "no mail account 'nope'" in err
