"""The email skill reading a mail account the user connected (`--account`).

Driven through the CLI's own `main`, with the real skill proxy answering on the
private credential channel and only the IMAP connection faked. What matters:
an account is the user's own and nobody else's, a bare verb never picks one,
a `read` account is examined rather than selected, the bot box's ownership
scope does not apply to an account, and no password or server text reaches
the output.
"""

from __future__ import annotations

import json
import socket
import threading
from unittest.mock import MagicMock

import pytest

from istota import db
from istota.config import Config, MailAccountsConfig
from istota.config import EmailConfig as AppEmailConfig
from istota.mail import accounts
from istota.sandbox.skill_proxy import SkillProxy
from istota.skills import email as email_skill

WORK_SECRET = "work-imap-pass-5e1f"
HOME_SECRET = "home-imap-pass-a04c"
BOB_SECRET = "bob-imap-pass-77d2"
BOT_SECRET = "bot-imap-pass-0b0b"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    path = tmp_path / "istota.db"
    db.init_db(path)
    with db.get_db(path) as conn:
        task_id = db.create_task(conn, user_id="alice", prompt="mail?", source_type="talk")
        work, _ = accounts.ensure_account(
            conn, "alice", slug="work", label="Work", imap_host="imap.work.example",
            imap_username="alice@work.example",
        )
        home, _ = accounts.ensure_account(
            conn, "alice", slug="home", label="Home", imap_host="imap.home.example",
            imap_username="alice@home.example", permission="read_send",
            smtp_host="smtp.home.example", from_address="alice@home.example",
            default_folder="Inbox/Main",
        )
        bobs, _ = accounts.ensure_account(
            conn, "bob", slug="private", imap_host="imap.bob.example",
            imap_username="bob@bob.example",
        )
        conn.commit()
        accounts.set_password(conn, "alice", work.id, "imap", WORK_SECRET)
        accounts.set_password(conn, "alice", home.id, "imap", HOME_SECRET)
        accounts.set_password(conn, "bob", bobs.id, "imap", BOB_SECRET)
        conn.commit()
    config = Config(db_path=path)
    config.email = AppEmailConfig(enabled=True, bot_email="bot@bot.example")
    monkeypatch.setattr("istota.config.load_config", lambda *a, **k: config)
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.delenv("ISTOTA_CRED_FD", raising=False)
    for name in ("SMTP_HOST", "IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "SMTP_FROM"):
        monkeypatch.delenv(name, raising=False)
    return {"path": path, "config": config, "task_id": task_id,
            "work": work.id, "home": home.id, "bob": bobs.id}


@pytest.fixture
def bot_box(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.bot.example")
    monkeypatch.setenv("IMAP_HOST", "imap.bot.example")
    monkeypatch.setenv("IMAP_USER", "bot@bot.example")
    monkeypatch.setenv("IMAP_PASSWORD", BOT_SECRET)
    monkeypatch.setenv("SMTP_FROM", "bot@bot.example")


@pytest.fixture
def channel(store, tmp_path, monkeypatch):
    """The email skill's private credential channel, answered by a real proxy."""
    proxy = SkillProxy(tmp_path / "proxy.sock", {}, {}, config=store["config"],
                       user_id="alice", task_id=store["task_id"])
    client, server = socket.socketpair()
    worker = threading.Thread(target=proxy._serve_credential_channel, args=(server,),
                              kwargs={"skill": "email"})
    worker.start()
    monkeypatch.setenv("ISTOTA_CRED_FD", str(client.fileno()))
    yield proxy
    client.close()
    worker.join(3)
    server.close()
    assert not worker.is_alive()


def _message(uid="7", subject="Quarterly numbers", sender="dana@example.org"):
    msg = MagicMock()
    msg.uid = uid
    msg.subject = subject
    msg.from_ = sender
    msg.to = ("alice@work.example",)
    msg.cc = ()
    msg.date_str = "Mon, 01 Jun 2026 00:00:00 +0000"
    msg.text = "see attached"
    msg.html = ""
    msg.flags = []
    msg.attachments = []
    msg.headers = {}
    msg.from_values = None
    msg.to_values = ()
    msg.cc_values = ()
    return msg


@pytest.fixture
def imap(monkeypatch):
    """Every `open_imap` call recorded, each answered by one fake mailbox."""
    calls = []
    mailbox = MagicMock()
    mailbox.__enter__ = MagicMock(return_value=mailbox)
    mailbox.__exit__ = MagicMock(return_value=False)
    mailbox.fetch.return_value = [_message()]
    mailbox.client.untagged_responses = {"UIDVALIDITY": [b"9"]}

    def open_imap(config, **kwargs):
        calls.append((config, kwargs))
        return mailbox

    monkeypatch.setattr("istota.mail.connect.open_imap", open_imap)
    return {"calls": calls, "mailbox": mailbox}


def run(capsys, *argv):
    """`email <argv>` through the CLI's `main`: ``(exit_code, envelope)``."""
    code = 0
    try:
        email_skill.main(list(argv))
    except SystemExit as exc:
        code = exc.code or 0
    out = capsys.readouterr().out
    return code, json.loads(out)


def _secrets_absent(text: str) -> None:
    for secret in (WORK_SECRET, HOME_SECRET, BOB_SECRET, BOT_SECRET):
        assert secret not in text


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


class TestWhoseAccount:
    def test_a_bare_verb_reads_the_bot_box(self, store, bot_box, imap, capsys):
        code, out = run(capsys, "list")
        assert code == 0, out
        config, kwargs = imap["calls"][0]
        assert config.imap_host == "imap.bot.example"
        assert not kwargs.get("check_public")
        assert out["account"] == "bot"

    def test_account_bot_is_the_bot_box(self, store, bot_box, imap, capsys):
        code, out = run(capsys, "list", "--account", "bot")
        assert code == 0, out
        assert imap["calls"][0][0].imap_host == "imap.bot.example"

    def test_resolve_account_with_no_slug_is_the_bot_box(self, store, bot_box):
        account = email_skill.resolve_account(None, purpose="read")
        assert account.is_bot and account.slug == "bot"
        assert account.config.imap_host == "imap.bot.example"

    def test_no_bot_box_is_an_error_naming_the_users_own_slugs(self, store, imap, capsys):
        code, out = run(capsys, "list")
        assert code == 1
        assert out["reason"] == "bot_mailbox_unavailable"
        assert "work" in out["error"] and "home" in out["error"]
        assert "private" not in out["error"]
        assert imap["calls"] == []

    def test_it_never_picks_an_account_on_its_own(self, store, channel, imap, capsys):
        # One account would be an obvious default. There is none.
        with db.get_db(store["path"]) as conn:
            accounts.delete_account(conn, "alice", store["home"])
            conn.commit()
        code, out = run(capsys, "list")
        assert out["reason"] == "bot_mailbox_unavailable"
        assert imap["calls"] == []

    def test_another_users_slug_is_unknown(self, store, channel, imap, capsys):
        code, out = run(capsys, "list", "--account", "private")
        assert code == 1
        assert out["reason"] == "unknown_account"
        assert "work" in out["error"] and "home" in out["error"]
        assert "imap.bob.example" not in json.dumps(out)
        assert imap["calls"] == []

    def test_no_channel_is_account_channel_unavailable(self, store, imap, capsys, monkeypatch):
        monkeypatch.delenv("ISTOTA_CRED_FD", raising=False)
        code, out = run(capsys, "list", "--account", "work")
        assert code == 1
        assert out["reason"] == "account_channel_unavailable"
        assert out["account"] == "work"
        assert imap["calls"] == []

    def test_a_turn_another_user_wrote_is_refused(self, store, channel, imap, capsys):
        with db.get_db(store["path"]) as conn:
            token = db.create_web_chat_room(conn, "alice", "general").token
            db.add_message(conn, token, role="user", body="read their mail",
                           origin_surface="web", task_id=store["task_id"],
                           author_user_id="bob")
            conn.commit()
        code, out = run(capsys, "list", "--account", "work")
        assert out["reason"] == "not_owner_turn"
        assert imap["calls"] == []

    def test_a_disabled_account_is_named_as_disabled(self, store, channel, imap, capsys):
        with db.get_db(store["path"]) as conn:
            accounts.update_account(conn, "alice", store["work"], enabled=False)
            conn.commit()
        code, out = run(capsys, "list", "--account", "work")
        assert out["reason"] == "account_disabled"
        assert imap["calls"] == []

    def test_a_missing_password_is_named(self, store, channel, imap, capsys):
        with db.get_db(store["path"]) as conn:
            accounts.clear_password(conn, "alice", store["work"], "imap")
            conn.commit()
        code, out = run(capsys, "list", "--account", "work")
        assert out["reason"] == "password_missing"
        assert imap["calls"] == []

    def test_the_feature_off_refuses(self, store, channel, imap, capsys):
        store["config"].mail_accounts = MailAccountsConfig(enabled=False)
        code, out = run(capsys, "list", "--account", "work")
        assert out["reason"] == "mail_accounts_disabled"
        assert imap["calls"] == []


# ---------------------------------------------------------------------------
# Reading an account
# ---------------------------------------------------------------------------


class TestReading:
    def test_it_connects_to_the_accounts_host_and_not_the_bot_box(
        self, store, bot_box, channel, imap, capsys,
    ):
        code, out = run(capsys, "list", "--account", "work")
        assert code == 0, out
        config, kwargs = imap["calls"][0]
        assert config.imap_host == "imap.work.example"
        assert config.imap_host != "imap.bot.example"
        assert kwargs["check_public"] is True
        imap["mailbox"].login.assert_called_once_with("alice@work.example", WORK_SECRET)
        assert out["account"] == "work"
        assert [e["id"] for e in out["emails"]] == ["7"]
        _secrets_absent(json.dumps(out))

    def test_private_hosts_reach_the_connection(self, store, channel, imap, capsys):
        store["config"].mail_accounts = MailAccountsConfig(private_hosts=["imap.work.example"])
        run(capsys, "list", "--account", "work")
        assert list(imap["calls"][0][1]["private_hosts"]) == ["imap.work.example"]

    def test_a_read_account_is_examined(self, store, channel, imap, capsys):
        run(capsys, "list", "--account", "work")
        imap["mailbox"].folder.set.assert_called_once_with("INBOX", readonly=True)

    def test_a_sending_account_is_selected(self, store, channel, imap, capsys):
        run(capsys, "list", "--account", "home")
        imap["mailbox"].folder.set.assert_called_once_with("Inbox/Main")

    def test_with_sending_off_every_account_is_examined(self, store, channel, imap, capsys):
        store["config"].mail_accounts = MailAccountsConfig(allow_send=False)
        run(capsys, "list", "--account", "home")
        imap["mailbox"].folder.set.assert_called_once_with("Inbox/Main", readonly=True)

    def test_folder_overrides_the_default(self, store, channel, imap, capsys):
        code, out = run(capsys, "list", "--account", "work", "--folder", "Archive")
        assert code == 0, out
        imap["mailbox"].folder.set.assert_called_once_with("Archive", readonly=True)
        assert out["folder"] == "Archive"

    def test_the_password_is_boxed_until_connect(self, store, channel):
        account = email_skill.resolve_account("work", purpose="read")
        assert WORK_SECRET not in repr(account)
        assert WORK_SECRET not in repr(account.config)

    def test_no_ownership_is_read_for_an_account(self, store, channel, imap, capsys, monkeypatch):
        def no_ownership(*args, **kwargs):
            raise AssertionError("ownership was resolved for a connected account")
        monkeypatch.setattr("istota.mail.ownership.resolve_email_owner", no_ownership)
        monkeypatch.setattr(email_skill, "_scope_conn", no_ownership)
        monkeypatch.setattr(email_skill, "_scope_context", no_ownership)
        for argv in (["list"], ["read", "7"], ["search", "UNSEEN"], ["thread", "7"],
                     ["from-senders", "--senders", "dana@example.org"],
                     ["newsletters", "--sources", "example.org"]):
            code, out = run(capsys, *argv, "--account", "work")
            assert code == 0, (argv, out)
            assert out["account"] == "work"

    def test_read_returns_the_message(self, store, channel, imap, capsys):
        code, out = run(capsys, "read", "7", "--account", "work")
        assert code == 0, out
        assert out["email"]["subject"] == "Quarterly numbers"
        assert "UNTRUSTED EMAIL CONTENT" in out["email"]["body"]

    def test_a_missing_message_is_not_found(self, store, channel, imap, capsys):
        imap["mailbox"].fetch.return_value = []
        code, out = run(capsys, "read", "8", "--account", "work")
        assert out == {"status": "not_found", "id": "8", "account": "work"}

    def test_attachments_go_through_the_account(self, store, channel, imap, capsys,
                                                tmp_path, monkeypatch):
        dest = tmp_path / "work" / "attachments"
        monkeypatch.setattr("istota.skills._cli.resolve_parsed", lambda parser, args: None)
        code, out = run(capsys, "attachments", "7", "--dest", str(dest), "--account", "work")
        assert code == 0, out
        assert all(c[0].imap_host == "imap.work.example" for c in imap["calls"])


class TestScope:
    def test_an_explicit_scope_with_an_account_is_refused(self, store, channel, imap, capsys):
        code, out = run(capsys, "list", "--account", "work", "--scope", "mine")
        assert code == 1
        assert out["reason"] == "scope_not_applicable"
        assert imap["calls"] == []

    def test_scope_still_applies_to_the_bot_box(self, store, bot_box, imap, capsys):
        code, out = run(capsys, "list", "--scope", "mine")
        assert code == 0, out
        assert out["scope"] == "mine"

    def test_no_scope_on_the_bot_box_means_all(self, store, bot_box, imap, capsys):
        code, out = run(capsys, "list")
        assert out["scope"] == "all"


class TestTheBotBoxFolder:
    """The bot box is shared, and only its INBOX has an ownership rule.

    Its sent copies go to "Sent Items", where a message another user sent
    through the bot resolves to no owner, so it reads as shared.
    """

    @pytest.fixture
    def bobs_sent_copy(self, store, imap):
        with db.get_db(store["path"]) as conn:
            db.create_task(conn, user_id="bob", prompt="mail dana", source_type="talk")
            conn.commit()
        imap["mailbox"].fetch.return_value = [
            _message(uid="3", subject="Bob's private note", sender="bot@bot.example"),
        ]

    @pytest.mark.parametrize("argv", [
        ["list"],
        ["read", "3"],
        ["search", "ALL"],
        ["thread", "3"],
        ["from-senders", "--senders", "bot@bot.example"],
        ["newsletters", "--sources", "bot.example"],
        ["mark", "3", "read", "--confirmed"],
        ["reply", "3", "--body", "Yes."],
    ])
    def test_another_folder_is_refused_before_any_read(
        self, store, bot_box, imap, bobs_sent_copy, capsys, argv,
    ):
        code, out = run(capsys, *argv, "--folder", "Sent Items")
        assert code == 1, out
        assert out["reason"] == "folder_not_supported"
        assert out["account"] == "bot"
        assert imap["calls"] == []
        assert "Bob's private note" not in json.dumps(out)

    def test_attachments_with_another_folder_is_refused(
        self, store, bot_box, imap, bobs_sent_copy, capsys, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr("istota.skills._cli.resolve_parsed", lambda parser, args: None)
        code, out = run(capsys, "attachments", "3", "--dest", str(tmp_path / "a"),
                        "--folder", "Sent Items")
        assert out["reason"] == "folder_not_supported"
        assert imap["calls"] == []

    def test_account_bot_is_refused_too(self, store, bot_box, imap, capsys):
        code, out = run(capsys, "list", "--account", "bot", "--folder", "Sent Items")
        assert out["reason"] == "folder_not_supported"
        assert imap["calls"] == []

    def test_its_own_folder_is_accepted(self, store, bot_box, imap, capsys):
        code, out = run(capsys, "list", "--folder", "INBOX")
        assert code == 0, out
        imap["mailbox"].folder.set.assert_called_once_with("INBOX")

    def test_folders_names_only_what_it_reads(self, store, bot_box, imap, capsys):
        code, out = run(capsys, "folders")
        assert code == 0, out
        assert out == {"status": "ok", "account": "bot", "folders": ["INBOX"]}
        assert imap["calls"] == []


class TestControlCharactersInImapArguments:
    """Text that reaches an IMAP command line may not end the line.

    imaplib writes an argument as given, so a CR/LF in it starts a command of
    the model's own: a SELECT past the EXAMINE, a STORE past `mark`'s gate.
    """

    INJECTED = "ALL\r\nZ1 SELECT INBOX\r\nZ2 UID STORE 1:* +FLAGS (\\Deleted)"

    @pytest.mark.parametrize("argv", [
        ["search", INJECTED],
        ["list", "--from", "dana@example.org\r\nZ1 SELECT INBOX"],
        ["from-senders", "--senders", "dana@example.org\nZ1 SELECT INBOX"],
        ["newsletters", "--sources", "example.org\rZ1 SELECT INBOX"],
        ["list", "--folder", "Archive\r\nZ1 SELECT INBOX"],
        ["search", "ALL\x00"],
        ["search", "ALL\x7f"],
    ])
    @pytest.mark.parametrize("account", [None, "work"])
    def test_refused_before_any_connection(
        self, store, bot_box, channel, imap, capsys, argv, account,
    ):
        extra = ["--account", account] if account else []
        code, out = run(capsys, *argv, *extra)
        assert code == 1, out
        assert out["status"] == "error"
        assert imap["calls"] == []

    def test_a_plain_query_still_reaches_the_server(self, store, bot_box, imap, capsys):
        code, out = run(capsys, "search", 'FROM "dana@example.org" UNSEEN')
        assert code == 0, out
        assert imap["calls"]


class TestBotOnlyVerbs:
    @pytest.mark.parametrize("argv", [
        ["delete", "7", "--confirmed"],
        ["output", "--body", "hi"],
    ])
    def test_refused_with_an_account(self, store, channel, imap, capsys, argv):
        code, out = run(capsys, *argv, "--account", "work")
        assert code == 1
        assert out["reason"] == "not_supported_for_account"
        assert imap["calls"] == []


# ---------------------------------------------------------------------------
# Errors carry no server text
# ---------------------------------------------------------------------------


class TestErrors:
    def test_a_missing_folder_is_named(self, store, channel, imap, capsys):
        from imap_tools.errors import MailboxFolderSelectError
        imap["mailbox"].folder.set.side_effect = MailboxFolderSelectError(
            ("NO", [b"Mailbox doesn't exist: Archive-server-detail"]), "OK",
        )
        code, out = run(capsys, "list", "--account", "work", "--folder", "Archive")
        assert code == 1
        assert out["reason"] == "folder_not_found"
        assert "Archive" in out["error"]
        assert "server-detail" not in json.dumps(out)

    def test_a_failed_sign_in_is_classified(self, store, channel, imap, capsys):
        from imap_tools.errors import MailboxLoginError
        imap["mailbox"].login.side_effect = MailboxLoginError(
            ("NO", [b"login failed for alice@work.example with " + WORK_SECRET.encode()]), "OK",
        )
        code, out = run(capsys, "list", "--account", "work")
        assert code == 1
        assert out["reason"] == "auth_failed"
        assert out["account"] == "work"
        assert "alice@work.example" not in json.dumps(out)
        _secrets_absent(json.dumps(out))

    def test_a_private_address_names_the_host(self, store, channel, monkeypatch, capsys):
        from istota.mail import connect
        monkeypatch.setattr(connect.socket, "getaddrinfo", lambda *a, **k: [
            (connect.socket.AF_INET, connect.socket.SOCK_STREAM, 6, "", ("10.0.0.5", 993)),
        ])
        code, out = run(capsys, "list", "--account", "work")
        assert out["reason"] == "address_not_public"
        assert "imap.work.example" in out["error"]
        assert "10.0.0.5" not in out["error"]


# ---------------------------------------------------------------------------
# accounts and folders
# ---------------------------------------------------------------------------


class TestAccountsVerb:
    def test_lists_the_bot_box_and_the_users_accounts(self, store, bot_box, capsys):
        code, out = run(capsys, "accounts")
        assert code == 0, out
        assert out["accounts"] == [
            {"slug": "bot", "label": "Bot mailbox", "permission": "read_send", "available": True},
            {"slug": "home", "label": "Home", "permission": "read_send", "has_smtp": True,
             "default_folder": "Inbox/Main", "available": True},
            {"slug": "work", "label": "Work", "permission": "read", "has_smtp": False,
             "default_folder": "INBOX", "available": True},
        ]
        text = json.dumps(out)
        for private in ("imap.work.example", "alice@work.example", "smtp.home.example",
                        "alice@home.example", "private", "imap.bob.example"):
            assert private not in text
        _secrets_absent(text)

    def test_no_bot_row_without_the_bot_box(self, store, capsys):
        code, out = run(capsys, "accounts")
        assert [a["slug"] for a in out["accounts"]] == ["home", "work"]

    def test_sending_off_lists_every_account_as_read(self, store, capsys):
        store["config"].mail_accounts = MailAccountsConfig(allow_send=False)
        code, out = run(capsys, "accounts")
        assert {a["permission"] for a in out["accounts"]} == {"read"}

    def test_unavailable_accounts_say_why(self, store, capsys):
        with db.get_db(store["path"]) as conn:
            accounts.update_account(conn, "alice", store["work"], enabled=False)
            accounts.clear_password(conn, "alice", store["home"], "imap")
            conn.commit()
        code, out = run(capsys, "accounts")
        by_slug = {a["slug"]: a for a in out["accounts"]}
        assert by_slug["work"]["available"] is False
        assert by_slug["work"]["reason"] == "account_disabled"
        assert by_slug["home"]["reason"] == "password_missing"

    def test_the_feature_off_lists_the_bot_box_only(self, store, bot_box, capsys):
        store["config"].mail_accounts = MailAccountsConfig(enabled=False)
        code, out = run(capsys, "accounts")
        assert [a["slug"] for a in out["accounts"]] == ["bot"]


class TestFoldersVerb:
    def test_lists_an_accounts_folders(self, store, channel, imap, capsys):
        imap["mailbox"].folder.list.return_value = [
            MagicMock(name="INBOX"), MagicMock(name="Archive"),
        ]
        for info, name in zip(imap["mailbox"].folder.list.return_value, ("INBOX", "Archive")):
            info.name = name
        code, out = run(capsys, "folders", "--account", "work")
        assert code == 0, out
        assert out == {"status": "ok", "account": "work", "folders": ["INBOX", "Archive"]}
        assert imap["calls"][0][0].imap_host == "imap.work.example"


# ---------------------------------------------------------------------------
# Sending from an account
# ---------------------------------------------------------------------------


@pytest.fixture
def smtp(monkeypatch):
    """Every `open_smtp` call recorded, each answered by one fake server."""
    calls = []
    server = MagicMock()
    sent = []
    server.send_message.side_effect = lambda msg, to_addrs=None: sent.append((msg, to_addrs))

    def open_smtp(config, **kwargs):
        calls.append((config, kwargs))
        return server

    monkeypatch.setattr("istota.mail.connect.open_smtp", open_smtp)
    return {"calls": calls, "server": server, "sent": sent}


@pytest.fixture
def task_env(store, tmp_path, monkeypatch):
    """The env a task's skill call carries, with a deferred dir to watch."""
    deferred = tmp_path / "deferred"
    deferred.mkdir()
    monkeypatch.setenv("ISTOTA_TASK_ID", str(store["task_id"]))
    monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(deferred))
    return deferred


def _policy(store, floor):
    store["config"].email.outbound_approval_floor = floor


def _sent_rows(store):
    with db.get_db(store["path"]) as conn:
        return conn.execute("SELECT * FROM sent_emails").fetchall()


def _drafts(store):
    with db.get_db(store["path"]) as conn:
        return conn.execute("SELECT * FROM outbound_drafts").fetchall()


class TestSendingNeedsPermission:
    """Refused before any connection, and before the gate writes a draft."""

    @pytest.mark.parametrize("argv", [
        ["send", "--to", "dana@example.org", "--subject", "Hi", "--body", "Yes."],
        ["reply", "7", "--body", "Yes."],
        ["reply-all", "7", "--body", "Yes."],
        ["mark", "7", "read", "--confirmed"],
    ])
    def test_a_read_account_refuses(self, store, channel, imap, smtp, capsys, argv):
        _policy(store, "all")
        code, out = run(capsys, *argv, "--account", "work")
        assert code == 1, out
        assert out["reason"] == "permission_denied"
        assert out["account"] == "work"
        assert imap["calls"] == [] and smtp["calls"] == []
        assert _drafts(store) == []

    def test_sending_off_refuses_every_account(self, store, channel, imap, smtp, capsys):
        store["config"].mail_accounts = MailAccountsConfig(allow_send=False)
        code, out = run(capsys, "send", "--to", "dana@example.org", "--subject", "Hi",
                        "--body", "Yes.", "--account", "home")
        assert out["reason"] == "permission_denied"
        assert smtp["calls"] == []

    def test_mark_with_sending_off_refuses(self, store, channel, imap, capsys):
        store["config"].mail_accounts = MailAccountsConfig(allow_send=False)
        code, out = run(capsys, "mark", "7", "read", "--confirmed", "--account", "home")
        assert out["reason"] == "permission_denied"
        assert imap["calls"] == []


class TestDirectSend:
    def _send(self, capsys, *extra):
        return run(capsys, "send", "--to", "dana@example.org", "--subject", "Lunch",
                   "--body", "Tuesday works.", "--account", "home", *extra)

    def test_it_goes_out_through_the_accounts_smtp_with_its_from(
        self, store, bot_box, channel, imap, smtp, task_env, capsys,
    ):
        _policy(store, "off")
        code, out = self._send(capsys)
        assert code == 0, out
        config, kwargs = smtp["calls"][0]
        assert config.smtp_host == "smtp.home.example"
        assert kwargs["check_public"] is True
        smtp["server"].login.assert_called_once_with("alice@home.example", HOME_SECRET)
        msg, to_addrs = smtp["sent"][0]
        assert msg["From"] == "alice@home.example"
        assert to_addrs == ["dana@example.org"]
        assert out["status"] == "ok" and out["account"] == "home"
        _secrets_absent(json.dumps(out))

    def test_from_name_is_on_the_from_line(
        self, store, channel, imap, smtp, task_env, capsys,
    ):
        _policy(store, "off")
        with db.get_db(store["path"]) as conn:
            accounts.update_account(conn, "alice", store["home"], from_name="Alice Example")
            conn.commit()
        self._send(capsys)
        msg, _ = smtp["sent"][0]
        assert msg["From"] == "Alice Example <alice@home.example>"
        assert msg["Message-ID"].endswith("@home.example>")

    def test_no_sent_emails_file_and_no_row(
        self, store, channel, imap, smtp, task_env, capsys,
    ):
        _policy(store, "off")
        code, out = self._send(capsys)
        assert code == 0, out
        assert list(task_env.iterdir()) == []
        assert _sent_rows(store) == []

    def test_the_bot_box_still_writes_its_sent_emails_file(
        self, store, bot_box, smtp, imap, task_env, capsys,
    ):
        # The control: the same send from the bot box does write the file.
        _policy(store, "off")
        code, out = run(capsys, "send", "--to", "dana@example.org", "--subject", "Lunch",
                        "--body", "Tuesday works.")
        assert code == 0, out
        assert [p.name for p in task_env.iterdir()] == [
            f"task_{store['task_id']}_sent_emails.json",
        ]
        assert smtp["calls"][0][0].smtp_host == "smtp.bot.example"

    def test_no_sent_folder_means_no_copy(
        self, store, channel, imap, smtp, task_env, capsys,
    ):
        _policy(store, "off")
        code, out = self._send(capsys)
        assert out["sent_copy"] == "none"
        imap["mailbox"].append.assert_not_called()

    def test_a_sent_folder_gets_a_seen_copy(
        self, store, channel, imap, smtp, task_env, capsys,
    ):
        _policy(store, "off")
        with db.get_db(store["path"]) as conn:
            accounts.update_account(conn, "alice", store["home"], sent_folder="Sent")
            conn.commit()
        code, out = self._send(capsys)
        assert out["sent_copy"] == "saved"
        args, kwargs = imap["mailbox"].append.call_args
        assert args[1] == "Sent"
        assert kwargs["flag_set"] == ["\\Seen"]
        assert imap["calls"][0][0].imap_host == "imap.home.example"

    def test_a_failed_copy_does_not_fail_the_send(
        self, store, channel, imap, smtp, task_env, capsys,
    ):
        _policy(store, "off")
        with db.get_db(store["path"]) as conn:
            accounts.update_account(conn, "alice", store["home"], sent_folder="Sent")
            conn.commit()
        imap["mailbox"].append.side_effect = OSError("quota")
        code, out = self._send(capsys)
        assert code == 0, out
        assert out["status"] == "ok"
        assert out["sent_copy"] == "failed"

    def test_a_refused_sign_in_is_classified(
        self, store, channel, imap, smtp, task_env, capsys,
    ):
        import smtplib
        _policy(store, "off")
        smtp["server"].login.side_effect = smtplib.SMTPAuthenticationError(
            535, ("bad password for alice@home.example " + HOME_SECRET).encode(),
        )
        code, out = self._send(capsys)
        assert code == 1
        assert out["reason"] == "auth_failed"
        assert "alice@home.example" not in json.dumps(out)
        _secrets_absent(json.dumps(out))


class TestHeldSend:
    def test_the_draft_records_the_account(
        self, store, channel, imap, smtp, task_env, capsys,
    ):
        _policy(store, "all")
        code, out = run(capsys, "send", "--to", "dana@example.org", "--subject", "Lunch",
                        "--body", "Tuesday works.", "--account", "home")
        assert code == 0, out
        assert out["status"] == "held"
        [draft] = _drafts(store)
        assert draft["account_id"] == store["home"]
        assert smtp["calls"] == []

    def test_a_bot_box_draft_has_no_account(
        self, store, bot_box, imap, smtp, task_env, capsys,
    ):
        _policy(store, "all")
        code, out = run(capsys, "send", "--to", "dana@example.org", "--subject", "Lunch",
                        "--body", "Tuesday works.")
        assert out["status"] == "held"
        [draft] = _drafts(store)
        assert draft["account_id"] is None


class TestReplyFromAnAccount:
    def test_reply_reads_and_sends_through_the_account(
        self, store, channel, imap, smtp, task_env, capsys,
    ):
        _policy(store, "off")
        code, out = run(capsys, "reply", "7", "--body", "Yes.", "--account", "home")
        assert code == 0, out
        assert all(c[0].imap_host == "imap.home.example" for c in imap["calls"])
        imap["mailbox"].folder.set.assert_called_with("Inbox/Main")
        msg, _ = smtp["sent"][0]
        assert msg["From"] == "alice@home.example"
        assert msg["To"] == "dana@example.org"
        assert out["account"] == "home"
        assert list(task_env.iterdir()) == []

    def test_reply_all_leaves_out_the_accounts_own_address(
        self, store, channel, imap, smtp, task_env, capsys,
    ):
        _policy(store, "off")
        original = _message()
        original.to = ("alice@home.example", "lee@example.org")
        imap["mailbox"].fetch.return_value = [original]
        code, out = run(capsys, "reply-all", "7", "--body", "Yes.", "--account", "home")
        assert code == 0, out
        assert out["cc"] == ["lee@example.org"]

    def test_reply_with_a_folder(self, store, channel, imap, smtp, task_env, capsys):
        _policy(store, "off")
        code, out = run(capsys, "reply", "7", "--body", "Yes.", "--account", "home",
                        "--folder", "Archive")
        assert code == 0, out
        imap["mailbox"].folder.set.assert_called_with("Archive")

    def test_scope_with_an_account_is_refused(self, store, channel, imap, smtp, capsys):
        code, out = run(capsys, "reply", "7", "--body", "Yes.", "--account", "home",
                        "--scope", "mine")
        assert out["reason"] == "scope_not_applicable"
        assert imap["calls"] == [] and smtp["calls"] == []

    def test_a_missing_original_is_not_found(self, store, channel, imap, smtp, capsys):
        _policy(store, "off")
        imap["mailbox"].fetch.return_value = []
        code, out = run(capsys, "reply", "8", "--body", "Yes.", "--account", "home")
        assert out == {"status": "not_found", "id": "8", "account": "home"}
        assert smtp["calls"] == []


class TestMarkOnAnAccount:
    def test_mark_stores_the_flag_in_the_accounts_folder(
        self, store, channel, imap, capsys,
    ):
        imap["mailbox"].client.uid.return_value = ("OK", [b""])
        code, out = run(capsys, "mark", "7", "read", "--confirmed", "--account", "home")
        assert code == 0, out
        assert imap["calls"][0][0].imap_host == "imap.home.example"
        imap["mailbox"].folder.set.assert_called_once_with("Inbox/Main")
        imap["mailbox"].client.uid.assert_called_once_with("STORE", "7", "+FLAGS", "(\\Seen)")
        assert out["account"] == "home"

    def test_mark_with_a_folder(self, store, channel, imap, capsys):
        imap["mailbox"].client.uid.return_value = ("OK", [b""])
        code, out = run(capsys, "mark", "7", "flagged", "--confirmed", "--account", "home",
                        "--folder", "Archive")
        assert code == 0, out
        imap["mailbox"].folder.set.assert_called_once_with("Archive")
