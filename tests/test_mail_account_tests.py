"""The connection test's request queue and the daemon gate that answers it.

The web route leaves a request in the reserved ``_mail_account_test``
namespace; the daemon's ``mail-account-test-request`` gate runs the check and
records the answer in the same transaction that removes the request. A user
holds one pending request and twenty an hour, and another user's request id
reads ``unknown``.
"""

from __future__ import annotations

import json

import pytest

from istota import db
from istota import scheduler as sched
from istota.config import Config, MailAccountsConfig, UserConfig
from istota.mail import account_tests, accounts, connect

PASSWORD = "synthetic-imap-password-77c1"
SMTP_PASSWORD = "synthetic-smtp-password-0b2d"


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    path = tmp_path / "istota.db"
    db.init_db(path)
    return Config(
        db_path=path,
        users={"alice": UserConfig(display_name="Alice"), "bob": UserConfig(display_name="Bob")},
        mail_accounts=MailAccountsConfig(private_hosts=["imap.example.com", "smtp.example.com"]),
    )


def _account(config, user="alice", slug="work", **fields):
    values = {"imap_host": "imap.example.com", "imap_username": f"{user}@example.com"}
    values.update(fields)
    with db.get_db(config.db_path) as conn:
        account, _ = accounts.ensure_account(conn, user, slug=slug, **values)
        accounts.set_password(conn, user, account.id, "imap", PASSWORD)
        if values.get("smtp_host") and not values.get("smtp_same_login", True):
            accounts.set_password(conn, user, account.id, "smtp", SMTP_PASSWORD)
    return account


def _sender(config, user="alice"):
    return _account(
        config, user, permission="read_send", smtp_host="smtp.example.com",
        from_address=f"{user}@example.com", smtp_same_login=False, smtp_username="smtp-user",
    )


def _rows(config):
    with db.get_db(config.db_path) as conn:
        return {r["key"]: json.loads(r["value"])
                for r in db.shared_kv_list(conn, account_tests.TEST_NAMESPACE)}


class Recorder:
    """Stands in for `mail.connect`'s openers, recording what signed in where."""

    def __init__(self, monkeypatch, *, imap_error=None):
        self.calls: list[tuple] = []
        recorder = self

        class Mailbox:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def login(self, user, password):
                recorder.calls.append(("imap_login", user, password))
                if imap_error is not None:
                    raise imap_error

        class Smtp:
            def login(self, user, password):
                recorder.calls.append(("smtp_login", user, password))

            def quit(self):
                pass

        def open_imap(email, *, check_public=False, private_hosts=()):
            self.calls.append(("open_imap", email.imap_host, check_public, tuple(private_hosts)))
            return Mailbox()

        def open_smtp(email, *, timeout=30.0, check_public=False, private_hosts=()):
            self.calls.append(("open_smtp", email.smtp_host, check_public))
            return Smtp()

        monkeypatch.setattr(connect, "open_imap", open_imap)
        monkeypatch.setattr(connect, "open_smtp", open_smtp)


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


def test_one_pending_request_per_user(config):
    alices = _account(config)
    bobs = _account(config, "bob")
    account_tests.request_test(config, "alice", alices.id)
    with pytest.raises(account_tests.TooManyRequests):
        account_tests.request_test(config, "alice", alices.id)
    # Another user's pending request does not count against this one.
    account_tests.request_test(config, "bob", bobs.id)


def test_the_hourly_cap_names_when_to_try_again(config, monkeypatch):
    account = _account(config)
    Recorder(monkeypatch)
    start = 1_800_000_000.0
    for i in range(account_tests.MAX_PER_HOUR):
        account_tests.request_test(config, "alice", account.id, now=start + i)
        account_tests.answer_pending(config)
    with pytest.raises(account_tests.RateLimited) as refused:
        account_tests.request_test(config, "alice", account.id, now=start + 100)
    # The oldest of the twenty ages out at start + 3600.
    assert refused.value.retry_after == 3500
    # Another user is not capped by alice's tests.
    bobs = _account(config, "bob")
    account_tests.request_test(config, "bob", bobs.id, now=start + 100)
    # An hour after the oldest, one slot is free again.
    account_tests.request_test(config, "alice", account.id, now=start + 3600)


def test_a_request_carries_no_password_and_no_host(config):
    account = _account(config)
    account_tests.request_test(config, "alice", account.id)
    text = json.dumps(_rows(config))
    assert PASSWORD not in text and "imap.example.com" not in text


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_the_gate_answers_and_removes_the_request(config, monkeypatch):
    recorder = Recorder(monkeypatch)
    account = _account(config)
    request_id = account_tests.request_test(config, "alice", account.id)
    assert account_tests.requests_pending(config) is True

    account_tests.answer_pending(config)

    assert account_tests.requests_pending(config) is False
    assert set(_rows(config)) == {f"answered:{request_id}", "recent:alice"}
    assert recorder.calls == [
        ("open_imap", "imap.example.com", True, ("imap.example.com", "smtp.example.com")),
        ("imap_login", "alice@example.com", PASSWORD),
    ]
    state = account_tests.read_answer(config, "alice", request_id)
    assert state["status"] == "answered"
    assert state["result"]["ok"] is True
    assert state["result"]["lines"] == [
        {"step": "imap", "ok": True, "message": "Signed in to imap.example.com."},
    ]
    assert PASSWORD not in json.dumps(state)
    # Collected: a second read finds nothing.
    assert account_tests.read_answer(config, "alice", request_id) == {"status": "unknown"}


def test_answer_and_removal_are_one_transaction(config, monkeypatch):
    Recorder(monkeypatch)
    account = _account(config)
    request_id = account_tests.request_test(config, "alice", account.id)

    def broken_delete(*a, **k):
        raise RuntimeError("disk went away")

    monkeypatch.setattr(db, "shared_kv_delete", broken_delete)
    account_tests.answer_pending(config)
    monkeypatch.undo()

    rows = _rows(config)
    assert f"request:{request_id}" in rows
    assert f"answered:{request_id}" not in rows


def test_another_users_request_id_reads_unknown_and_is_not_consumed(config, monkeypatch):
    Recorder(monkeypatch)
    account = _account(config)
    request_id = account_tests.request_test(config, "alice", account.id)
    assert account_tests.read_answer(config, "bob", request_id) == {"status": "unknown"}
    assert account_tests.read_answer(config, "alice", request_id) == {"status": "pending"}
    account_tests.answer_pending(config)
    assert account_tests.read_answer(config, "bob", request_id) == {"status": "unknown"}
    assert account_tests.read_answer(config, "alice", request_id)["status"] == "answered"


@pytest.mark.parametrize("request_id", ["", "x" * 16, "../etc", "%", "0123456789abcdef0"])
def test_a_malformed_request_id_reads_unknown(config, request_id):
    assert account_tests.read_answer(config, "alice", request_id) == {"status": "unknown"}


def test_a_send_account_is_tested_for_sending_too(config, monkeypatch):
    recorder = Recorder(monkeypatch)
    account = _sender(config)
    result = account_tests.run_test(config, "alice", account.id)
    assert result["ok"] is True
    assert [line["step"] for line in result["lines"]] == ["imap", "smtp"]
    assert ("open_smtp", "smtp.example.com", True) in recorder.calls
    assert ("smtp_login", "smtp-user", SMTP_PASSWORD) in recorder.calls


def test_with_sending_off_a_send_account_is_tested_for_reading_only(config, monkeypatch):
    recorder = Recorder(monkeypatch)
    account = _sender(config)
    config.mail_accounts.allow_send = False
    result = account_tests.run_test(config, "alice", account.id)
    assert result["ok"] is True
    assert [line["step"] for line in result["lines"]] == ["imap"]
    assert not any(call[0] == "open_smtp" for call in recorder.calls)


@pytest.mark.parametrize("case", ["deleted", "other_user", "disabled", "no_password", "feature_off"])
def test_an_account_that_does_not_resolve_answers_plainly(config, monkeypatch, case):
    recorder = Recorder(monkeypatch)
    account = _account(config)
    user = "alice"
    with db.get_db(config.db_path) as conn:
        if case == "deleted":
            accounts.delete_account(conn, "alice", account.id)
        elif case == "disabled":
            accounts.update_account(conn, "alice", account.id, enabled=False)
        elif case == "no_password":
            accounts.clear_password(conn, "alice", account.id, "imap")
    if case == "other_user":
        user = "bob"
    if case == "feature_off":
        config.mail_accounts.enabled = False
    result = account_tests.run_test(config, user, account.id)
    assert result["ok"] is False
    assert result["lines"] and result["lines"][0]["step"] == "account"
    assert result["message"]
    assert recorder.calls == []


def test_a_failed_sign_in_is_a_plain_message(config, monkeypatch):
    from imap_tools import MailboxLoginError

    Recorder(monkeypatch, imap_error=MailboxLoginError(f"LOGIN failed for {PASSWORD}", "NO"))
    account = _account(config)
    result = account_tests.run_test(config, "alice", account.id)
    assert result["ok"] is False
    assert result["lines"][0] == {
        "step": "imap", "ok": False,
        "message": "The mail server didn't accept that username and password.",
    }
    assert PASSWORD not in json.dumps(result)


def test_a_test_that_raises_is_answered_rather_than_retried_forever(config, monkeypatch):
    account = _account(config)
    request_id = account_tests.request_test(config, "alice", account.id)

    def explode(*a, **k):
        raise RuntimeError(PASSWORD)

    monkeypatch.setattr(account_tests, "run_test", explode)
    account_tests.answer_pending(config)
    state = account_tests.read_answer(config, "alice", request_id)
    assert state["status"] == "answered"
    assert state["result"]["ok"] is False
    assert PASSWORD not in json.dumps(state)


def test_an_answer_nobody_collected_is_pruned(config, monkeypatch):
    Recorder(monkeypatch)
    account = _account(config)
    old_id = account_tests.request_test(config, "alice", account.id)
    account_tests.answer_pending(config)
    with db.get_db(config.db_path) as conn:
        conn.execute(
            "UPDATE shared_kv SET updated_at = datetime('now', '-2 days') "
            "WHERE namespace = ? AND key = ?",
            (account_tests.TEST_NAMESPACE, f"answered:{old_id}"),
        )
    account_tests.request_test(config, "alice", account.id)
    account_tests.answer_pending(config)
    assert f"answered:{old_id}" not in _rows(config)


# ---------------------------------------------------------------------------
# The scheduler's gate
# ---------------------------------------------------------------------------


def test_the_scheduler_gate_spawns_only_when_a_request_waits(config, monkeypatch):
    spawned = []
    monkeypatch.setattr(
        sched, "_spawn_background_check",
        lambda name, fn, inflight, **kw: spawned.append(name),
    )
    sched.poll_mail_account_test_requests(config, {})
    assert spawned == []

    account = _account(config)
    account_tests.request_test(config, "alice", account.id)
    sched.poll_mail_account_test_requests(config, {})
    assert spawned == ["mail-account-test-request"]


def test_the_scheduler_gate_is_registered_every_tick():
    gates = {g.name: g for g in sched.build_interval_gates(Config())}
    gate = gates["mail-account-test-request"]
    assert gate.fixed_interval == 1 and not gate.one_shot
