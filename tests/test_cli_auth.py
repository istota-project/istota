"""Operator commands through the real parser and credential database."""

import io
from urllib.parse import parse_qs, urlsplit

import pytest

from istota import cli, db, user_profiles, web_auth
from istota.config import Config

PASSWORD = "a long example passphrase"
VERBS = ["list", "add", "set-password", "invite", "reset", "login-link",
         "disable", "enable", "logout-all", "remove"]


@pytest.fixture
def config(db_path, monkeypatch):
    config = Config(db_path=db_path)
    config.site.hostname = "bot.example.com"
    config.web.auth = ["email"]
    monkeypatch.setattr(cli, "load_config", lambda *a: config)
    monkeypatch.setattr(cli, "setup_logging", lambda *a, **kw: None)
    return config


@pytest.fixture
def invoke(config, monkeypatch, capsys):
    def run(*args, stdin=""):
        monkeypatch.setattr("sys.argv", ["istota", "auth", *args])
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        code = 0
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
        captured = capsys.readouterr()
        return code, captured.out, captured.err
    return run


@pytest.fixture
def identity(config):
    user_profiles.ensure_profile(config.db_path, "alice", display_name="Alice")
    return web_auth.upsert_identity(config.db_path, "alice", "alice@example.com")


def test_add_requires_profile_and_create_user_bootstraps(config, invoke):
    code, out, err = invoke("add", "alice", "--email", "Alice@Example.com")
    assert code == 1 and "--create-user" in err
    assert web_auth.get_identity(config.db_path, "alice") is None
    code, out, err = invoke("add", "alice", "--email", "Alice@Example.com", "--create-user")
    assert (code, err) == (0, "") and "state=created" in out
    assert user_profiles.get_profile(config.db_path, "alice")
    assert web_auth.get_identity(config.db_path, "alice").email == "alice@example.com"
    assert "state=unchanged" in invoke("add", "alice", "--email", "alice@example.com")[1]
    assert "state=updated" in invoke("add", "alice", "--email", "new@example.com")[1]


@pytest.mark.parametrize("user_id", ["..", "Alice", "a/b", "-alice", "a" * 33])
def test_create_user_rejects_invalid_id(config, invoke, user_id):
    assert invoke("add", "--email", "alice@example.com", "--create-user", "--", user_id)[0] == 1
    assert user_profiles.get_profile(config.db_path, user_id) is None


def test_existing_id_preserved_and_case_collision_refused(config, invoke):
    original = user_profiles.ensure_profile(config.db_path, "Alice", display_name="Original")
    assert invoke("add", "Alice", "--email", "alice@example.com")[0] == 0
    assert user_profiles.get_profile(config.db_path, "Alice") == original
    code, out, err = invoke("add", "alice", "--email", "other@example.com", "--create-user")
    assert code == 1 and "Alice" in err
    assert user_profiles.get_profile(config.db_path, "alice") is None


def test_duplicate_names_owner_without_creating_profile(config, identity, invoke):
    code, out, err = invoke("add", "bob", "--email", "ALICE@example.com", "--create-user")
    assert code == 1 and "alice" in err
    assert user_profiles.get_profile(config.db_path, "bob") is None


@pytest.mark.parametrize("verb,purpose", [("add", "enrol"), ("invite", "enrol"),
                                           ("reset", "reset"), ("login-link", "login")])
def test_print_link_is_usable(config, identity, invoke, verb, purpose):
    args = [verb, "alice", "--print-link"]
    if verb == "add":
        args += ["--email", identity.email]
    code, out, err = invoke(*args)
    assert (code, err) == (0, "")
    link = next(line for line in out.splitlines() if line.startswith("https://"))
    parsed = urlsplit(link)
    assert parsed.netloc == "bot.example.com"
    assert parsed.path == "/istota/auth/" + ("login-link" if purpose == "login" else "set-password")
    record = web_auth.peek_token(config.db_path, parse_qs(parsed.query)["token"][0], purpose)
    assert record.user_id == "alice" and "state=updated" in out


@pytest.mark.parametrize("verb", ["add", "set-password"])
def test_password_stdin_reads_one_line_never_echoes(config, identity, invoke, verb):
    args = [verb, "alice", "--password-stdin"]
    if verb == "add":
        args += ["--email", identity.email]
    code, out, err = invoke(*args, stdin=PASSWORD + "\nignored line\n")
    assert (code, err) == (0, "") and "state=updated" in out
    assert PASSWORD not in out + err
    updated = web_auth.get_identity(config.db_path, "alice")
    assert web_auth.verify_password(PASSWORD, updated.password_hash)[0]
    assert updated.credential_epoch > identity.credential_epoch


def test_password_policy_before_creating_account(config, invoke):
    config.web.auth_min_password_length = 30
    code, out, err = invoke("add", "alice", "--email", "alice@example.com", "--create-user",
                            "--password-stdin", stdin=PASSWORD + "\n")
    assert code == 1 and "30" in err
    assert user_profiles.get_profile(config.db_path, "alice") is None


def test_password_requires_explicit_stdin_flag(identity, invoke):
    code, out, err = invoke("set-password", "alice", stdin=PASSWORD)
    assert code == 1 and "--password-stdin" in err


def test_tty_uses_getpass(config, identity, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["istota", "auth", "set-password", "alice"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt: PASSWORD)
    cli.main()
    assert "state=updated" in capsys.readouterr().out
    assert web_auth.verify_password(PASSWORD, web_auth.get_identity(config.db_path, "alice").password_hash)[0]


def test_revocation_remove_preserve_profile(config, identity, invoke):
    epoch = identity.credential_epoch
    for verb, disabled in [("disable", True), ("enable", False), ("logout-all", False)]:
        code, out, err = invoke(verb, "alice")
        assert (code, err) == (0, "") and "state=updated" in out
        updated = web_auth.get_identity(config.db_path, "alice")
        assert updated.credential_epoch > epoch and updated.disabled is disabled
        epoch = updated.credential_epoch
    assert "state=updated" in invoke("remove", "alice")[1]
    assert "state=unchanged" in invoke("remove", "alice")[1]
    assert user_profiles.get_profile(config.db_path, "alice")


def test_list_marks_orphan_and_profile_without_identity(config, identity, invoke):
    user_profiles.ensure_profile(config.db_path, "bob")
    user_profiles.delete_profile(config.db_path, "alice")
    code, out, err = invoke("list")
    assert (code, err) == (0, "")
    assert "orphaned" in out and "alice@example.com" in out
    assert "bob" in out and "nextcloud_only" in out and "state=unchanged" in out


@pytest.mark.parametrize("verb", VERBS)
def test_parser_rejects_password_flag(invoke, verb):
    args = [verb] if verb == "list" else [verb, "alice"]
    if verb == "add":
        args += ["--email", "alice@example.com"]
    assert invoke(*args, "--password", PASSWORD)[0] == 2
    assert invoke(*args, "--password")[0] == 2


@pytest.mark.parametrize("verb", ["set-password", "invite", "reset", "login-link", "disable", "enable", "logout-all"])
def test_missing_identity_refused(config, invoke, verb):
    user_profiles.ensure_profile(config.db_path, "alice")
    assert invoke(verb, "alice")[0] == 1
    assert web_auth.get_identity(config.db_path, "alice") is None


def test_send_disabled_and_missing_origin_issue_nothing(config, identity, invoke):
    code, out, err = invoke("login-link", "alice", "--send")
    assert code == 1 and "email is not configured; use --print-link" in err
    assert "token=" not in out + err
    config.site.hostname = ""
    code, out, err = invoke("reset", "alice", "--print-link")
    assert code == 1 and "site.hostname" in err
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM web_auth_tokens").fetchone()[0] == 0


@pytest.mark.parametrize("verb,purpose", [("add", "enrol"), ("invite", "enrol"),
                                           ("reset", "reset"), ("login-link", "login")])
def test_send_uses_shared_mail(config, identity, invoke, monkeypatch, verb, purpose):
    from istota import web_auth_mail
    config.email.enabled = True
    calls = []
    monkeypatch.setattr(web_auth_mail, "send_auth_email", lambda *args: calls.append(args) or True)
    args = [verb, "alice"]
    args += ["--email", identity.email, "--send-invite"] if verb == "add" else ["--send"]
    code, out, err = invoke(*args)
    assert (code, err) == (0, "") and "state=updated" in out
    assert "token=" not in out + err
    assert len(calls) == 1 and calls[0][1] == identity.email
    link = next(line for line in calls[0][3].splitlines() if line.startswith("https://"))
    assert web_auth.peek_token(config.db_path, parse_qs(urlsplit(link).query)["token"][0], purpose)


def test_first_admin_printed_invite_is_created(config, invoke):
    code, out, err = invoke("add", "alice", "--email", "alice@example.com", "--create-user", "--print-link")
    assert (code, err) == (0, "") and "state=created" in out
    link = next(line for line in out.splitlines() if line.startswith("https://"))
    token = parse_qs(urlsplit(link).query)["token"][0]
    assert web_auth.peek_token(config.db_path, token, "enrol").user_id == "alice"


def test_smtp_failure_reports_failure_keeps_token(config, identity, invoke, monkeypatch):
    from istota import web_auth_mail
    config.email.enabled = True
    sent = []
    monkeypatch.setattr(web_auth_mail, "send_auth_email", lambda *args: sent.append(args) and False)
    code, out, err = invoke("login-link", "alice")
    assert code == 1 and "--print-link" in err
    assert "state=" not in out and "token=" not in out + err
    link = next(line for line in sent[0][3].splitlines() if line.startswith("https://"))
    token = parse_qs(urlsplit(link).query)["token"][0]
    assert web_auth.peek_token(config.db_path, token, "login")


def test_disabled_identity_cannot_get_link(config, identity, invoke):
    web_auth.set_disabled(config.db_path, "alice", True)
    assert invoke("invite", "alice", "--print-link")[0] == 1
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM web_auth_tokens").fetchone()[0] == 0


def test_rejected_invite_does_not_create_profile(config, invoke):
    code, out, err = invoke("add", "alice", "--email", "alice@example.com", "--create-user", "--send-invite")
    assert code == 1
    assert user_profiles.get_profile(config.db_path, "alice") is None


def test_link_lifetime_uses_config(config, identity, invoke):
    from datetime import datetime, timedelta, timezone
    config.web.auth_login_link_ttl_minutes = 7
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    code, out, err = invoke("login-link", "alice", "--print-link")
    assert code == 0
    with db.get_db(config.db_path) as conn:
        expires = datetime.fromisoformat(conn.execute("SELECT expires_at FROM web_auth_tokens").fetchone()[0])
    assert timedelta(minutes=7) <= expires - before < timedelta(minutes=7, seconds=5)


@pytest.mark.parametrize("host,scheme", [("bot.example.com", "https"), ("localhost:8766", "http"),
                                        ("127.0.0.1:8766", "http")])
def test_origin_shared_with_web_redirects(config, host, scheme):
    from istota.web_origin import external_origin
    config.site.hostname = host
    assert external_origin(config) == (host, scheme)


def test_missing_database_refuses_without_creating_file(config, invoke):
    config.db_path = config.db_path.parent / "missing.db"
    code, out, err = invoke("list")
    assert code == 1 and "istota init" in err
    assert not config.db_path.exists()


def test_database_error_is_operator_failure(config, invoke):
    with db.get_db(config.db_path) as conn:
        conn.execute("DROP TABLE web_auth_identities")
    code, out, err = invoke("list")
    assert code == 1 and "database operation failed" in err
    assert "state=" not in out
