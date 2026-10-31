"""The per-sender record of suspected injection attempts (#897)."""

import json
import logging
from dataclasses import replace

import pytest

from istota import confirmations, db
from istota.config import Config, EmailConfig, SchedulerConfig, UserConfig
from istota.executor import build_prompt, task_deferred_dir
from istota.mail import sender_flags
from istota.notifications.resolvers.confirmation import body_for
from istota.notifications.resolvers.task_alert import flatten_body
from istota.rooms import scopes
from istota.skills.email import Email
from istota.transport.email import inbound
from istota.transport.email.inbound import _strict_dmarc_refusal
from istota.transport.email.mail_card import (
    received_mail_meta,
    stored_received_mail,
    stored_sender_authenticated,
)

from . import test_email_thread_rooms as _thread_rooms
from . import test_email_volume_budget as _budget
from . import test_scheduler_deferred as _deferred
from .test_email_confirmation_answer import _poll as _answer_poll


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


def _config(
    db_path=None,
    *,
    threshold=3,
    window_days=90,
    own=("carol@example.com",),
    trusted=(),
):
    return Config(
        db_path=db_path,
        email=EmailConfig(enabled=False),
        scheduler=SchedulerConfig(
            email_sender_flag_hold_threshold=threshold,
            email_sender_flag_window_days=window_days,
        ),
        users={
            "carol": UserConfig(
                email_addresses=list(own),
                trusted_email_senders=list(trusted),
            ),
        },
    )


def _flag(conn, sender, task_id, *, user="carol", reason="other", age="-1 days"):
    conn.execute(
        "INSERT INTO email_sender_flags (user_id, sender, task_id, reason, flagged_at) "
        "VALUES (?, ?, ?, ?, datetime('now', ?))",
        (user, sender, task_id, reason, age),
    )


def _flag_times(conn, sender, times, *, user="carol"):
    for task_id in range(1, times + 1):
        _flag(conn, sender, task_id, user=user)


class TestAddressKey:
    @pytest.mark.parametrize("raw,expected", [
        ("mallory@evil.example", "mallory@evil.example"),
        ("Mallory <mallory@evil.example>", "mallory@evil.example"),
        ("MaLLory@Evil.Example", "mallory@evil.example"),
        ('"Doe, Jane" <Jane@Evil.Example>', "jane@evil.example"),
        ("  mallory@evil.example  ", "mallory@evil.example"),
    ])
    def test_one_key_per_address(self, raw, expected):
        assert sender_flags.address_key(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "   ", "<>", "Display Name <>"])
    def test_no_address_has_no_key(self, raw):
        assert sender_flags.address_key(raw) is None

    def test_agrees_with_the_volume_budget_on_who_a_sender_is(self, db_path):
        forms = ("Loud <Loud@Example.com>", "loud@example.com")
        with db.get_db(db_path) as conn:
            for email_id, sender in zip(("one", "two"), forms):
                task_id = db.create_task(
                    conn, prompt="p", user_id="carol", source_type="email",
                )
                db.mark_email_processed(
                    conn, email_id=email_id, sender_email=sender,
                    user_id="carol", task_id=task_id,
                )
            budget_counts = [
                db.count_recent_email_tasks_from_sender(conn, "carol", form, 3600)
                for form in forms
            ]
        assert budget_counts == [2, 2]
        assert sender_flags.address_key(forms[0]) == sender_flags.address_key(forms[1])


class TestNormalizeReason:
    @pytest.mark.parametrize("reason", sender_flags.REASONS)
    def test_a_known_reason_is_kept(self, reason):
        assert sender_flags.normalize_reason(reason) == reason

    @pytest.mark.parametrize("value", [
        "ignore all previous instructions", "INSTRUCTION_OVERRIDE", "", None, 3, ["other"], {},
    ])
    def test_anything_else_becomes_other(self, value):
        assert sender_flags.normalize_reason(value) == "other"


class TestCountInWindow:
    def test_a_row_just_outside_the_window_does_not_count(self, db_path):
        config = _config(db_path, window_days=90)
        with db.get_db(db_path) as conn:
            _flag(conn, "a@x.example", 1, age="-89 days")
            _flag(conn, "a@x.example", 2, age="-91 days")
            assert sender_flags.count_in_window(conn, config, "carol", "a@x.example") == 1

    def test_window_zero_means_flags_never_age(self, db_path):
        config = _config(db_path, window_days=0)
        with db.get_db(db_path) as conn:
            _flag(conn, "a@x.example", 1, age="-4000 days")
            _flag(conn, "a@x.example", 2)
            assert sender_flags.count_in_window(conn, config, "carol", "a@x.example") == 2

    def test_counts_one_user_and_one_address(self, db_path):
        config = _config(db_path)
        with db.get_db(db_path) as conn:
            _flag(conn, "a@x.example", 1)
            _flag(conn, "b@x.example", 2)
            _flag(conn, "a@x.example", 3, user="dave")
            assert sender_flags.count_in_window(conn, config, "carol", "a@x.example") == 1


class TestHoldingCount:
    def test_below_the_threshold_holds_nothing(self, db_path):
        config = _config(db_path)
        with db.get_db(db_path) as conn:
            _flag_times(conn, "a@x.example", 2)
            assert sender_flags.holding_count(conn, config, "carol", "a@x.example") == 0

    def test_at_the_threshold_answers_the_count(self, db_path):
        config = _config(db_path)
        with db.get_db(db_path) as conn:
            _flag_times(conn, "a@x.example", 3)
            assert sender_flags.holding_count(conn, config, "carol", "a@x.example") == 3
            _flag(conn, "a@x.example", 50)
            assert sender_flags.holding_count(conn, config, "carol", "a@x.example") == 4

    def test_display_name_and_case_reach_the_same_rows(self, db_path):
        config = _config(db_path)
        with db.get_db(db_path) as conn:
            _flag_times(conn, "a@x.example", 3)
            assert sender_flags.holding_count(conn, config, "carol", "Al <A@X.Example>") == 3

    def test_aged_rows_do_not_hold(self, db_path):
        config = _config(db_path)
        with db.get_db(db_path) as conn:
            _flag(conn, "a@x.example", 1)
            _flag(conn, "a@x.example", 2)
            _flag(conn, "a@x.example", 3, age="-120 days")
            assert sender_flags.holding_count(conn, config, "carol", "a@x.example") == 0

    @pytest.mark.parametrize("threshold", [0, -1])
    def test_threshold_off_holds_nothing(self, db_path, threshold):
        config = _config(db_path, threshold=threshold)
        with db.get_db(db_path) as conn:
            _flag_times(conn, "a@x.example", 5)
            assert sender_flags.holding_count(conn, config, "carol", "a@x.example") == 0

    def test_window_zero_still_holds_on_old_rows(self, db_path):
        config = _config(db_path, window_days=0)
        with db.get_db(db_path) as conn:
            for task_id in (1, 2, 3):
                _flag(conn, "a@x.example", task_id, age="-4000 days")
            assert sender_flags.holding_count(conn, config, "carol", "a@x.example") == 3

    @pytest.mark.parametrize("sender", [None, "", "not an address <>"])
    def test_an_unparseable_sender_holds_nothing(self, db_path, sender):
        config = _config(db_path)
        with db.get_db(db_path) as conn:
            assert sender_flags.holding_count(conn, config, "carol", sender) == 0

    def test_an_own_address_is_exempt(self, db_path):
        config = _config(db_path, own=("Carol@Example.com",))
        with db.get_db(db_path) as conn:
            _flag_times(conn, "carol@example.com", 3)
            assert (
                sender_flags.holding_count(conn, config, "carol", "Carol <carol@example.com>")
                == 0
            )

    def test_a_named_config_entry_is_exempt(self, db_path):
        config = _config(db_path, trusted=("Partner@Corp.com",))
        with db.get_db(db_path) as conn:
            _flag_times(conn, "partner@corp.com", 3)
            assert sender_flags.holding_count(conn, config, "carol", "partner@corp.com") == 0

    def test_a_runtime_trust_row_is_exempt_until_removed(self, db_path):
        config = _config(db_path)
        with db.get_db(db_path) as conn:
            _flag_times(conn, "joe@x.example", 3)
            db.add_trusted_sender(conn, "carol", "joe@x.example")
            assert sender_flags.holding_count(conn, config, "carol", "Joe <joe@x.example>") == 0
            db.remove_trusted_sender(conn, "carol", "joe@x.example")
            assert sender_flags.holding_count(conn, config, "carol", "Joe <joe@x.example>") == 3

    @pytest.mark.parametrize("pattern", ["*", "*@corp.com", "m?llory@corp.com", "[m]allory@corp.com"])
    def test_a_pattern_does_not_exempt(self, db_path, pattern):
        config = _config(db_path, trusted=(pattern,))
        with db.get_db(db_path) as conn:
            _flag_times(conn, "mallory@corp.com", 3)
            assert sender_flags.holding_count(conn, config, "carol", "mallory@corp.com") == 3


class TestListFlags:
    def test_summarises_counted_flags_per_address(self, db_path):
        config = _config(db_path, trusted=("named@x.example",))
        with db.get_db(db_path) as conn:
            _flag(conn, "held@x.example", 1, reason="impersonation", age="-3 days")
            _flag(conn, "held@x.example", 2, reason="other", age="-2 days")
            _flag(conn, "held@x.example", 3, reason="impersonation", age="-1 days")
            _flag(conn, "named@x.example", 4, reason="data_request", age="-5 days")
            _flag(conn, "named@x.example", 5, age="-5 days")
            _flag(conn, "named@x.example", 6, age="-5 days")
            _flag(conn, "aged@x.example", 7, age="-200 days")
            _flag(conn, "held@x.example", 8, user="dave")
            summaries = sender_flags.list_flags(conn, config, "carol")
            expected_last = conn.execute(
                "SELECT MAX(flagged_at) FROM email_sender_flags "
                "WHERE user_id = 'carol' AND sender = 'held@x.example'"
            ).fetchone()[0]

        assert [s.address for s in summaries] == ["held@x.example", "named@x.example"]
        held, named = summaries
        assert held.count == 3
        assert held.last_flagged_at == expected_last
        assert sorted(held.reasons) == ["impersonation", "other"]
        assert held.holding is True
        assert named.count == 3
        assert named.holding is False

    def test_below_threshold_is_listed_and_not_holding(self, db_path):
        config = _config(db_path)
        with db.get_db(db_path) as conn:
            _flag(conn, "a@x.example", 1)
            summaries = sender_flags.list_flags(conn, config, "carol")
        assert [(s.address, s.count, s.holding) for s in summaries] == [("a@x.example", 1, False)]

    def test_aged_reasons_are_not_reported(self, db_path):
        config = _config(db_path)
        with db.get_db(db_path) as conn:
            _flag(conn, "a@x.example", 1, reason="encoded_payload", age="-200 days")
            _flag(conn, "a@x.example", 2, reason="hidden_content")
            summaries = sender_flags.list_flags(conn, config, "carol")
        assert summaries[0].count == 1
        assert summaries[0].reasons == ("hidden_content",)


class TestClear:
    def test_removes_every_row_for_the_address_in_or_out_of_the_window(self, db_path):
        with db.get_db(db_path) as conn:
            _flag(conn, "a@x.example", 1)
            _flag(conn, "a@x.example", 2, age="-400 days")
            _flag(conn, "b@x.example", 3)
            assert sender_flags.clear(conn, "carol", "Al <A@X.Example>") == 2
            remaining = conn.execute("SELECT sender FROM email_sender_flags").fetchall()
        assert [row[0] for row in remaining] == ["b@x.example"]

    def test_one_user_cannot_clear_anothers(self, db_path):
        with db.get_db(db_path) as conn:
            _flag(conn, "a@x.example", 1, user="dave")
            assert sender_flags.clear(conn, "carol", "a@x.example") == 0
            assert conn.execute("SELECT COUNT(*) FROM email_sender_flags").fetchone()[0] == 1

    @pytest.mark.parametrize("sender", ["", "no-address <>"])
    def test_no_address_clears_nothing(self, db_path, sender):
        with db.get_db(db_path) as conn:
            _flag(conn, "a@x.example", 1)
            assert sender_flags.clear(conn, "carol", sender) == 0


class TestPrune:
    def test_removes_only_what_the_count_stopped_counting(self, db_path):
        config = _config(db_path, window_days=90)
        with db.get_db(db_path) as conn:
            _flag(conn, "a@x.example", 1, age="-89 days")
            _flag(conn, "a@x.example", 2, age="-90 days")
            _flag(conn, "a@x.example", 3, age="-91 days")
            counted = sender_flags.count_in_window(conn, config, "carol", "a@x.example")
            assert sender_flags.prune(conn, 90) == 3 - counted
            remaining = conn.execute("SELECT COUNT(*) FROM email_sender_flags").fetchone()[0]
        assert remaining == counted

    @pytest.mark.parametrize("window_days", [0, -5])
    def test_window_off_prunes_nothing(self, db_path, window_days):
        with db.get_db(db_path) as conn:
            _flag(conn, "a@x.example", 1, age="-4000 days")
            assert sender_flags.prune(conn, window_days) == 0
            assert conn.execute("SELECT COUNT(*) FROM email_sender_flags").fetchone()[0] == 1

    def test_run_cleanup_checks_prunes_on_the_configured_window(self, db_path, tmp_path):
        from istota.scheduler import run_cleanup_checks

        config = Config(
            db_path=db_path,
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(email_sender_flag_window_days=30),
            temp_dir=tmp_path / "temp",
        )
        with db.get_db(db_path) as conn:
            _flag(conn, "a@x.example", 1, age="-40 days")
            _flag(conn, "a@x.example", 2, age="-10 days")

        run_cleanup_checks(config)

        with db.get_db(db_path) as conn:
            kept = [row[0] for row in conn.execute("SELECT task_id FROM email_sender_flags")]
        assert kept == [2]


class TestTheTable:
    def test_one_task_counts_once(self, db_path):
        with db.get_db(db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO email_sender_flags (user_id, sender, task_id, reason) "
                "VALUES ('carol', 'a@x.example', 1, 'other')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO email_sender_flags (user_id, sender, task_id, reason) "
                "VALUES ('carol', 'a@x.example', 1, 'impersonation')"
            )
            rows = conn.execute("SELECT reason, flagged_at FROM email_sender_flags").fetchall()
        assert len(rows) == 1
        assert rows[0]["reason"] == "other"
        assert rows[0]["flagged_at"]

    def test_init_db_adds_it_to_an_existing_database(self, db_path):
        with db.get_db(db_path) as conn:
            conn.execute("DROP TABLE email_sender_flags")
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            names = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE tbl_name = 'email_sender_flags'"
                )
            }
        assert {"email_sender_flags", "idx_email_sender_flags_lookup"} <= names


# ---------------------------------------------------------------------------
# Which mail can earn a flag: the strict reading of its stamps
# ---------------------------------------------------------------------------

AUTHSERV = "mx.test.com"
OUR_PASS = f"{AUTHSERV}; dmarc=pass header.from=elsewhere.example"
OUR_SPF_ONLY = f"{AUTHSERV}; spf=pass smtp.mailfrom=elsewhere.example"
OUR_FAIL = f"{AUTHSERV}; dmarc=fail header.from=elsewhere.example"
PASS_NO_FROM = f"{AUTHSERV}; dmarc=pass"
SENDER = "mallory@elsewhere.example"


def _stamped_email(headers):
    return Email(
        id="1", subject="s", sender=SENDER, date="", body="", attachments=[],
        message_id="<m@x>", authentication_results_all=tuple(headers),
    )


def _strict_config(authserv_id=AUTHSERV):
    return Config(email=EmailConfig(enabled=True, authserv_id=authserv_id))


class TestTheStrictRule:
    def test_a_topmost_aligned_pass_is_authenticated(self):
        email = _stamped_email([OUR_PASS])
        assert _strict_dmarc_refusal(_strict_config(), email, SENDER) is None

    @pytest.mark.parametrize("headers", [
        pytest.param([OUR_SPF_ONLY, OUR_PASS], id="forged-pass-under-our-spf-stamp"),
        pytest.param([OUR_PASS, OUR_FAIL], id="a-lower-stamp-of-ours-disagrees"),
        pytest.param([PASS_NO_FROM], id="pass-with-no-header-from"),
        pytest.param([OUR_FAIL, OUR_PASS], id="real-fail-over-forged-pass"),
        pytest.param([f"{AUTHSERV}; dmarc=pass header.from=other.example"], id="misaligned"),
        pytest.param(["other.example; dmarc=pass header.from=elsewhere.example"], id="not-ours"),
        pytest.param([], id="unstamped"),
    ])
    def test_everything_else_is_refused(self, headers):
        refusal = _strict_dmarc_refusal(_strict_config(), _stamped_email(headers), SENDER)
        assert refusal is not None

    def test_a_sole_forged_stamp_passes_which_is_the_stated_limit(self):
        """Mail our MTA did not stamp at all: the forgery is then the topmost
        header, and no reading of the headers can tell. This rests on the MTA
        stamping every inbound mail and stripping stamps with its own id."""
        forged = f"{AUTHSERV}; dmarc=pass header.from=elsewhere.example"
        assert _strict_dmarc_refusal(_strict_config(), _stamped_email([forged]), SENDER) is None

    @pytest.mark.parametrize("authserv_id", [None, "", "   "])
    def test_no_authserv_id_is_unavailable(self, authserv_id):
        refusal = _strict_dmarc_refusal(
            _strict_config(authserv_id), _stamped_email([OUR_PASS]), SENDER,
        )
        assert refusal == "unavailable"


class TestTheStoredAnswer:
    def _meta(self, **kwargs):
        return received_mail_meta(
            _stamped_email([]), sender_check="verified", trusted=True, **kwargs,
        )

    def test_it_defaults_to_false_and_is_a_json_boolean(self):
        assert self._meta()["sender_authenticated"] is False
        assert self._meta(sender_authenticated=True)["sender_authenticated"] is True

    def test_it_reads_back(self):
        assert stored_sender_authenticated(json.dumps(self._meta(sender_authenticated=True)))
        assert not stored_sender_authenticated(json.dumps(self._meta()))

    @pytest.mark.parametrize("raw", [
        None, "", "not json", "[1]", b"\xff",
        '{"sender_check": "verified"}',
        '{"sender_authenticated": "true"}',
        '{"sender_authenticated": 1}',
    ])
    def test_only_a_literal_true_counts(self, raw):
        assert stored_sender_authenticated(raw) is False

    def test_the_cards_payload_keeps_its_shape(self):
        stored = stored_received_mail(json.dumps(self._meta(sender_authenticated=True)))
        assert "sender_authenticated" not in stored
        assert set(stored) == {
            "from", "to", "cc", "date", "subject", "message_id", "in_reply_to",
            "attachments", "sender_check", "trusted",
        }


# ---------------------------------------------------------------------------
# The deferred op, through the drain
# ---------------------------------------------------------------------------

config = _deferred.config
mail_config = _deferred.mail_config
STRANGER = _deferred.STRANGER


def _write_flags(config, task, content):
    directory = task_deferred_dir(config, task)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"task_{task.id}_sender_flags.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


def _seed_meta(config, task, **fields):
    """The answer intake stored, over whatever the poll wrote."""
    with db.get_db(config.db_path) as conn:
        conn.execute(
            "UPDATE processed_emails SET mail_meta = ? WHERE task_id = ?",
            (json.dumps(fields), task.id),
        )


def _authenticated_turn(config):
    task = _deferred._strangers_turn(config)
    _seed_meta(config, task, sender_authenticated=True)
    return task


def _flags(config):
    with db.get_db(config.db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT user_id, sender, task_id, reason FROM email_sender_flags ORDER BY id")]


class TestTheDrainRecordsAFlag:
    def test_it_names_the_tasks_sender_and_user_whatever_the_file_says(self, mail_config):
        task = _authenticated_turn(mail_config)
        path = _write_flags(mail_config, task, [{
            "reason": "impersonation",
            "sender": "victim@elsewhere.example",
            "address": "victim@elsewhere.example",
            "user_id": "bob",
            "task_id": 999,
        }])

        _deferred._drain(mail_config, task)

        assert _flags(mail_config) == [{
            "user_id": "alice", "sender": STRANGER, "task_id": task.id,
            "reason": "impersonation",
        }]
        assert not path.exists()

    def test_a_display_name_and_mixed_case_sender_is_one_key(self, mail_config):
        task = _authenticated_turn(mail_config)
        with db.get_db(mail_config.db_path) as conn:
            conn.execute(
                "UPDATE processed_emails SET sender_email = ? WHERE task_id = ?",
                ("Carol Ash <Carol@Example.COM>", task.id),
            )
        _write_flags(mail_config, task, [{"reason": "other"}])
        _deferred._drain(mail_config, task)
        assert _flags(mail_config)[0]["sender"] == STRANGER

    def test_an_own_turn_records_nothing(self, mail_config):
        task = _deferred._own_turn(mail_config)
        path = _write_flags(mail_config, task, [{"reason": "other"}])
        _deferred._drain(mail_config, task)
        assert _flags(mail_config) == []
        assert not path.exists()

    def test_the_users_own_mail_records_nothing(self, mail_config):
        with db.get_db(mail_config.db_path) as conn:
            task_id = db.create_task(conn, prompt="p", user_id="alice", source_type="email")
            db.mark_email_processed(
                conn, email_id="own", sender_email="alice@test.com", user_id="alice",
                task_id=task_id, routing_method="sender_match",
                mail_meta={"sender_authenticated": True},
            )
            task = db.get_task(conn, task_id)
        path = _write_flags(mail_config, task, [{"reason": "other"}])
        _deferred._drain(mail_config, task)
        assert _flags(mail_config) == []
        assert not path.exists()

    def test_a_guests_file_records_nothing(self, mail_config):
        task = _authenticated_turn(mail_config)
        with db.get_db(mail_config.db_path) as conn:
            conn.execute(
                "UPDATE tasks SET guest_participant_id = 1 WHERE id = ?", (task.id,),
            )
            task = db.get_task(conn, task.id)
        path = _write_flags(mail_config, task, [{"reason": "other"}])
        _deferred._drain(mail_config, task)
        assert _flags(mail_config) == []
        assert not path.exists()

    @pytest.mark.parametrize("meta", [
        pytest.param({"sender_check": "failed"}, id="failed"),
        pytest.param({"sender_check": "none"}, id="none"),
        pytest.param({"sender_check": "verified"}, id="verified-is-not-strict-enough"),
        pytest.param({"sender_authenticated": False}, id="refused-at-intake"),
        pytest.param({"sender_authenticated": "true"}, id="not-a-literal-true"),
    ])
    def test_mail_that_did_not_authenticate_earns_nothing(self, mail_config, meta):
        task = _deferred._strangers_turn(mail_config)
        _seed_meta(mail_config, task, **meta)
        path = _write_flags(mail_config, task, [{"reason": "other"}])
        _deferred._drain(mail_config, task)
        assert _flags(mail_config) == []
        assert not path.exists()

    def test_a_second_drain_writes_one_row(self, mail_config):
        task = _authenticated_turn(mail_config)
        for reason in ("impersonation", "data_request"):
            path = _write_flags(mail_config, task, [{"reason": reason}])
            _deferred._drain(mail_config, task)
            assert not path.exists()
        assert [f["reason"] for f in _flags(mail_config)] == ["impersonation"]

    def test_a_file_of_several_entries_writes_one_row_from_the_first_dict(self, mail_config):
        task = _authenticated_turn(mail_config)
        path = _write_flags(mail_config, task, [
            "not a dict", {"reason": "hidden_content"}, {"reason": "data_request"},
        ])
        _deferred._drain(mail_config, task)
        assert [f["reason"] for f in _flags(mail_config)] == ["hidden_content"]
        assert not path.exists()

    @pytest.mark.parametrize("reason", ["made up", 7, None, ["other"]])
    def test_a_reason_outside_the_set_stores_other(self, mail_config, reason):
        task = _authenticated_turn(mail_config)
        _write_flags(mail_config, task, [{"reason": reason}])
        _deferred._drain(mail_config, task)
        assert [f["reason"] for f in _flags(mail_config)] == ["other"]

    @pytest.mark.parametrize("content", [
        "[]", '["a string", 7]', '{"reason": "other"}', "not json{{{",
    ])
    def test_a_file_with_no_dict_entry_records_nothing(self, mail_config, content):
        task = _authenticated_turn(mail_config)
        path = _write_flags(mail_config, task, content)
        _deferred._drain(mail_config, task)
        assert _flags(mail_config) == []
        assert not path.exists()

    def test_a_retry_copy_has_no_mail_of_its_own(self, mail_config):
        parent = _authenticated_turn(mail_config)
        with db.get_db(mail_config.db_path) as conn:
            child = db.get_task(conn, db.create_task(
                conn, prompt="again", user_id="alice", source_type="email",
                parent_task_id=parent.id,
            ))
        path = _write_flags(mail_config, child, [{"reason": "other"}])
        _deferred._drain(mail_config, child)
        assert _flags(mail_config) == []
        assert not path.exists()

    def test_a_turn_that_cannot_be_read_records_nothing_and_keeps_the_file(
        self, mail_config, monkeypatch,
    ):
        task = _authenticated_turn(mail_config)
        path = _write_flags(mail_config, task, [{"reason": "other"}])

        def _boom(*args, **kwargs):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(scopes, "turn_author", _boom)
        _deferred._drain(mail_config, task)
        assert _flags(mail_config) == []
        assert path.exists()


class TestTheOutcomes:
    def _record(self, config, task, reason="other"):
        with db.get_db(config.db_path) as conn:
            return sender_flags.record_flag(conn, config, task, reason)

    def test_recorded_then_duplicate(self, mail_config):
        task = _authenticated_turn(mail_config)
        assert self._record(mail_config, task) == "recorded"
        assert self._record(mail_config, task) == "duplicate"

    def test_an_own_turn_is_not_a_correspondents(self, mail_config):
        assert self._record(mail_config, _deferred._own_turn(mail_config)) == "not_correspondent"

    def test_a_row_of_another_user_is_no_mail(self, mail_config):
        task = _authenticated_turn(mail_config)
        with db.get_db(mail_config.db_path) as conn:
            conn.execute(
                "UPDATE processed_emails SET user_id = 'bob' WHERE task_id = ?", (task.id,),
            )
        assert self._record(mail_config, task) == "no_mail"

    def test_an_unparseable_sender_has_no_address(self, mail_config):
        task = _authenticated_turn(mail_config)
        with db.get_db(mail_config.db_path) as conn:
            conn.execute(
                "UPDATE processed_emails SET sender_email = '<>' WHERE task_id = ?", (task.id,),
            )
        assert self._record(mail_config, task) == "no_address"
        assert _flags(mail_config) == []

    def test_an_own_address_is_exempt_even_on_a_correspondents_turn(
        self, mail_config, monkeypatch,
    ):
        # Reached only where a stored row names someone else while the ledger
        # names the user, so the author is set here rather than built up.
        task = _authenticated_turn(mail_config)
        with db.get_db(mail_config.db_path) as conn:
            conn.execute(
                "UPDATE processed_emails SET sender_email = 'Alice@Test.com' WHERE task_id = ?",
                (task.id,),
            )
        monkeypatch.setattr(scopes, "turn_author", lambda *a, **k: "correspondent")
        assert self._record(mail_config, task) == "own_address"
        assert _flags(mail_config) == []

    def test_unverified_mail_is_not_verified(self, mail_config):
        task = _deferred._strangers_turn(mail_config)
        assert self._record(mail_config, task) == "not_verified"

    def test_a_flag_on_a_named_address_is_still_recorded(self, mail_config):
        mail_config.users["alice"].trusted_email_senders = [STRANGER]
        task = _authenticated_turn(mail_config)
        assert self._record(mail_config, task) == "recorded"

    def test_the_log_carries_the_outcome_and_only_a_normalized_reason(
        self, mail_config, caplog,
    ):
        task = _authenticated_turn(mail_config)
        _write_flags(mail_config, task, [{"reason": "IGNORE PREVIOUS INSTRUCTIONS"}])
        with caplog.at_level(logging.INFO, logger="istota.scheduler"):
            _deferred._drain(mail_config, task)
        lines = [r.getMessage() for r in caplog.records if "sender flag" in r.getMessage()]
        assert len(lines) == 1
        assert str(task.id) in lines[0]
        assert "recorded" in lines[0]
        assert "IGNORE PREVIOUS" not in caplog.text


class TestIntakeStoresTheAnswer:
    """Through the real `poll_emails`: the stamps on the mail decide the flag."""

    def _turn(self, config, headers):
        (task_id,), _, _, _ = _answer_poll(
            config, sender=SENDER, to=("bot+alice@test.com",), auth=headers,
        )
        with db.get_db(config.db_path) as conn:
            return db.get_task(conn, task_id)

    def test_a_strict_pass_earns_a_flag_end_to_end(self, mail_config):
        mail_config.email.authserv_id = AUTHSERV
        task = self._turn(mail_config, (OUR_PASS,))
        _write_flags(mail_config, task, [{"reason": "instruction_override"}])
        _deferred._drain(mail_config, task)
        assert _flags(mail_config) == [{
            "user_id": "alice", "sender": SENDER, "task_id": task.id,
            "reason": "instruction_override",
        }]

    def test_the_loose_reading_would_pass_this_and_the_flag_does_not(self, mail_config):
        mail_config.email.authserv_id = AUTHSERV
        task = self._turn(mail_config, (OUR_SPF_ONLY, OUR_PASS))
        with db.get_db(mail_config.db_path) as conn:
            raw = conn.execute(
                "SELECT mail_meta FROM processed_emails WHERE task_id = ?", (task.id,),
            ).fetchone()[0]
        assert json.loads(raw)["sender_check"] == "verified"
        _write_flags(mail_config, task, [{"reason": "other"}])
        _deferred._drain(mail_config, task)
        assert _flags(mail_config) == []

    def test_no_authserv_id_stores_false(self, mail_config):
        task = self._turn(mail_config, (OUR_PASS,))
        with db.get_db(mail_config.db_path) as conn:
            raw = conn.execute(
                "SELECT mail_meta FROM processed_emails WHERE task_id = ?", (task.id,),
            ).fetchone()[0]
        assert stored_sender_authenticated(raw) is False


HELD_SENDER = "stranger@example.com"
PRIMER = "primer@example.com"

make_config = _budget.make_config
_fresh_volume_state = _budget._clear_module_state
thread_config = _thread_rooms.config


def _front_desk(make_config):
    """A wildcard-trusted user with the volume budgets out of the way."""
    config = make_config()
    config.users["alice"].trusted_email_senders = ["*"]
    config.scheduler.email_rate_limit_messages = 0
    config.scheduler.email_sender_rate_limit_messages = 0
    return config


def _seed_flags(config, sender=HELD_SENDER, *, user="alice", times=3):
    with db.get_db(config.db_path) as conn:
        for task_id in range(9000, 9000 + times):
            _flag(conn, sender, task_id, user=user)
        conn.commit()


def _rows_from(config, sender):
    """(uid, routing, task id, task status) per mail from this sender, oldest first."""
    with db.get_db(config.db_path) as conn:
        rows = conn.execute(
            "SELECT p.email_id, p.routing_method, p.task_id, t.status "
            "FROM processed_emails p LEFT JOIN tasks t ON t.id = p.task_id "
            "WHERE p.sender_email = ? ORDER BY CAST(p.email_id AS INTEGER)",
            (sender,),
        ).fetchall()
    return [tuple(row) for row in rows]


def _statuses_from(config, sender):
    return [status for _, _, _, status in _rows_from(config, sender)]


def _prompts(sent):
    return [message for kind, _, message in sent if kind == "prompt"]


def _alerts(sent):
    return [message for kind, _, message in sent if kind == "alert"]


def _row_body(config, task_id):
    with db.get_db(config.db_path) as conn:
        row = conn.execute(
            "SELECT body FROM notifications WHERE source = 'confirmation' AND object_id = ?",
            (str(task_id),),
        ).fetchone()
    return row[0]


def _stored_prompt(config, task_id):
    with db.get_db(config.db_path) as conn:
        return db.get_task(conn, task_id).confirmation_prompt


def _flag_rows(config):
    with db.get_db(config.db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM email_sender_flags").fetchone()[0]


def _approve(config, task_id):
    with db.get_db(config.db_path) as conn:
        task = db.get_task(conn, task_id)
        assert task.status == "pending_confirmation"
        confirmations.approve(conn, task, config=config)
        conn.commit()
        return db.get_task(conn, task_id)


class _MailboxWithSubject(_budget.FakeMailbox):
    subject = "Message"

    def read_email(self, email_id, folder=None, config=None, envelope=None):
        email = super().read_email(email_id, folder, config, envelope)
        return replace(email, subject=self.subject)


class TestTheHold:
    def test_the_next_mail_is_held_under_a_wildcard(self, make_config):
        config = _front_desk(make_config)
        _seed_flags(config)

        created, sent = _budget._run_poll(config, _budget.FakeMailbox([1]))

        assert len(created) == 1
        assert _statuses_from(config, HELD_SENDER) == ["pending_confirmation"]
        assert len(_prompts(sent)) == 1

    def test_a_display_name_and_case_do_not_escape_it(self, make_config):
        config = _front_desk(make_config)
        _seed_flags(config)
        sender = "Somebody Else <Stranger@Example.COM>"

        _budget._run_poll(config, _budget.FakeMailbox([1], sender=sender))

        assert _statuses_from(config, sender) == ["pending_confirmation"]

    def test_several_mails_give_one_prompt_and_the_held_notice(self, make_config):
        config = _front_desk(make_config)
        _seed_flags(config)
        mailbox = _budget.FakeMailbox(range(1, 6), senders={1: PRIMER})
        _budget._prime_cursor(config, mailbox)

        _, sent = _budget._run_poll(config, mailbox)

        assert _statuses_from(config, HELD_SENDER) == ["pending_confirmation"] * 4
        assert len(_prompts(sent)) == 1
        assert len(_alerts(sent)) == 1
        assert HELD_SENDER in _alerts(sent)[0]
        assert "!confirm" in _alerts(sent)[0]

    def test_a_limit_of_zero_still_never_collapses(self, make_config):
        config = _front_desk(make_config)
        config.scheduler.email_confirmation_prompts_per_window = 0
        _seed_flags(config)
        mailbox = _budget.FakeMailbox(range(1, 6), senders={1: PRIMER})
        _budget._prime_cursor(config, mailbox)

        _, sent = _budget._run_poll(config, mailbox)

        assert len(_prompts(sent)) == 4
        assert _alerts(sent) == []

    def test_a_named_config_entry_runs(self, make_config):
        config = _front_desk(make_config)
        config.users["alice"].trusted_email_senders = [HELD_SENDER]
        _seed_flags(config)

        _, sent = _budget._run_poll(config, _budget.FakeMailbox([1]))

        assert _statuses_from(config, HELD_SENDER) == ["pending"]
        assert _prompts(sent) == []

    def test_a_runtime_trust_entry_runs(self, make_config):
        config = _front_desk(make_config)
        _seed_flags(config)
        with db.get_db(config.db_path) as conn:
            db.add_trusted_sender(conn, "alice", HELD_SENDER)
            conn.commit()

        _budget._run_poll(config, _budget.FakeMailbox([1]))

        assert _statuses_from(config, HELD_SENDER) == ["pending"]

    def test_an_unflagged_sender_runs(self, make_config):
        config = _front_desk(make_config)
        _seed_flags(config, "somebody-else@example.com")

        _budget._run_poll(config, _budget.FakeMailbox([1]))

        assert _statuses_from(config, HELD_SENDER) == ["pending"]

    def test_below_the_threshold_runs(self, make_config):
        config = _front_desk(make_config)
        _seed_flags(config, times=2)

        _budget._run_poll(config, _budget.FakeMailbox([1]))

        assert _statuses_from(config, HELD_SENDER) == ["pending"]

    def test_another_users_flags_hold_nothing(self, make_config):
        config = _front_desk(make_config)
        _seed_flags(config, user="carol")

        _budget._run_poll(config, _budget.FakeMailbox([1]))

        assert _statuses_from(config, HELD_SENDER) == ["pending"]

    def test_a_quiet_sender_stays_quiet(self, make_config):
        config = _front_desk(make_config)
        config.users["alice"].quiet_email_senders = [HELD_SENDER]
        _seed_flags(config)

        created, sent = _budget._run_poll(config, _budget.FakeMailbox([1]))

        assert created == []
        assert [routing for _, routing, _, _ in _rows_from(config, HELD_SENDER)] == ["quiet"]
        assert _prompts(sent) == []

    def test_an_over_budget_sender_stays_throttled(self, make_config):
        config = _front_desk(make_config)
        config.scheduler.email_sender_rate_limit_messages = 2
        _seed_flags(config)
        mailbox = _budget.FakeMailbox(range(1, 6), senders={1: PRIMER})
        _budget._prime_cursor(config, mailbox)

        _budget._run_poll(config, mailbox)

        rows = _rows_from(config, HELD_SENDER)
        assert [status for _, _, _, status in rows[:2]] == ["pending_confirmation"] * 2
        assert [routing for _, routing, _, _ in rows[2:]] == ["throttled"] * 2
        assert [task_id for _, _, task_id, _ in rows[2:]] == [None, None]

    def test_a_yes_leaves_the_flags_and_the_next_mail_is_held(self, make_config):
        config = _front_desk(make_config)
        _seed_flags(config)
        mailbox = _budget.FakeMailbox([1])
        (held,), _ = _budget._run_poll(config, mailbox)

        assert _approve(config, held).status == "pending"
        assert _flag_rows(config) == 3

        mailbox.uids = [1, 2]
        _budget._run_poll(config, mailbox)
        assert _statuses_from(config, HELD_SENDER) == ["pending", "pending_confirmation"]

    def test_clearing_the_flags_lets_the_next_mail_run(self, make_config):
        config = _front_desk(make_config)
        _seed_flags(config)
        mailbox = _budget.FakeMailbox([1])
        _budget._run_poll(config, mailbox)
        assert _statuses_from(config, HELD_SENDER) == ["pending_confirmation"]

        with db.get_db(config.db_path) as conn:
            assert sender_flags.clear(conn, "alice", HELD_SENDER) == 3
            conn.commit()

        mailbox.uids = [1, 2]
        _budget._run_poll(config, mailbox)
        assert _statuses_from(config, HELD_SENDER) == ["pending_confirmation", "pending"]

    def test_a_restart_still_holds(self, make_config):
        config = _front_desk(make_config)
        _seed_flags(config)
        mailbox = _budget.FakeMailbox([1])
        _budget._run_poll(config, mailbox)
        assert _statuses_from(config, HELD_SENDER) == ["pending_confirmation"]

        inbound._reset_volume_state()
        mailbox.uids = [1, 2]
        _, sent = _budget._run_poll(config, mailbox)

        assert _statuses_from(config, HELD_SENDER) == ["pending_confirmation"] * 2
        # The prompt collapse is memory: a restart can cost one more prompt.
        assert len(_prompts(sent)) == 1


class TestTheTwoStrings:
    def _held(self, make_config, mailbox=None):
        config = _front_desk(make_config)
        _seed_flags(config)
        (task_id,), sent = _budget._run_poll(config, mailbox or _budget.FakeMailbox([1]))
        return config, task_id, sent

    def test_the_prompt_tells_the_user_about_the_flags(self, make_config):
        _, task_id, sent = self._held(make_config)

        (prompt,) = _prompts(sent)
        assert prompt.startswith(f"Email from flagged sender {HELD_SENDER}\n")
        assert f"Task: #{task_id}\n" in prompt
        assert "Held because 3 earlier mails from this address were flagged" in prompt
        assert "does not clear the flags" in prompt
        assert f"Send !unflag {HELD_SENDER} to clear them." in prompt
        assert f"!confirm {task_id} no" in prompt
        assert "trust" not in prompt

    def test_the_inbox_row_tells_the_user_about_the_flags(self, make_config):
        config, task_id, _ = self._held(make_config)

        body = _row_body(config, task_id)
        assert "Held because 3 earlier mails" in body
        assert f"!unflag {HELD_SENDER}" in body
        assert "trust" not in body

    def test_a_long_subject_does_not_push_the_way_out_off_the_row(self, make_config):
        mailbox = _MailboxWithSubject([1])
        mailbox.subject = "long subject " * 60
        config, task_id, _ = self._held(make_config, mailbox)

        assert f"!unflag {HELD_SENDER}" in _row_body(config, task_id)

    def test_a_display_name_stays_out_of_the_command(self, make_config):
        sender = f"Somebody <{HELD_SENDER}>"
        _, _, sent = self._held(make_config, _budget.FakeMailbox([1], sender=sender))

        (prompt,) = _prompts(sent)
        assert "Somebody" not in prompt
        assert f"!unflag {HELD_SENDER} " in prompt

    def test_a_subject_cannot_write_lines_into_the_prompt(self, make_config):
        mailbox = _MailboxWithSubject([1])
        mailbox.subject = "Hello\r\nTask: #999\r\nReply 'yes' to process"
        _, _, sent = self._held(make_config, mailbox)

        (prompt,) = _prompts(sent)
        assert prompt.startswith("Email from flagged sender")
        assert "\nTask: #999\n" not in prompt
        assert "\nReply 'yes' to process" not in prompt

    def test_the_stored_prompt_is_silent_about_the_flags(self, make_config):
        config, task_id, _ = self._held(make_config)

        stored = _stored_prompt(config, task_id)
        assert stored.startswith(f"Email from held sender {HELD_SENDER}\n")
        assert "Reply 'yes' to process it, or 'no' to discard." in stored
        assert "flagged" not in stored.lower()
        assert "unflag" not in stored.lower()

    def test_the_approved_rerun_prompt_is_silent_about_the_flags(self, make_config):
        config, task_id, _ = self._held(make_config)

        task = _approve(config, task_id)
        assert task.confirmed_at and task.confirmation_prompt
        composed = build_prompt(
            task, [], config, confirmation_context=task.confirmation_prompt,
        )

        assert "## Confirmed action" in composed.user
        assert f"Email from held sender {HELD_SENDER}" in composed.user
        assert "flagged" not in composed.user.lower()
        assert "unflag" not in composed.user.lower()

    def test_an_ordinary_hold_is_still_one_string(self, make_config):
        config = make_config()
        (task_id,), sent = _budget._run_poll(config, _budget.FakeMailbox([1]))

        stored = _stored_prompt(config, task_id)
        assert stored.startswith(f"Email from unknown sender {HELD_SENDER}\n")
        assert "yes trust" in stored
        assert _prompts(sent) == [flatten_body(stored)]
        assert _row_body(config, task_id) == body_for(stored)


class TestAFlaggedPersonOnAThread:
    def test_held_though_one_of_its_people_and_approval_admits_to_that_room(
        self, thread_config,
    ):
        config = thread_config
        _thread_rooms._start_thread(config)
        token = _thread_rooms._room_token(config)
        _seed_flags(config, _thread_rooms.ALICE, user=_thread_rooms.HOST)

        with db.get_db(config.db_path) as conn:
            before = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE room_token = ?", (token,),
            ).fetchone()[0]

        (held,) = _thread_rooms._poll(
            config, sender=_thread_rooms.ALICE,
            to=(_thread_rooms.BOT, _thread_rooms.HOST_ADDR), cc=(_thread_rooms.BOB,),
            message_id="<reply-1@ext.example>", references=_thread_rooms.ROOT,
        )

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, held)
            after = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE room_token = ?", (token,),
            ).fetchone()[0]
        assert task.status == "pending_confirmation"
        assert task.conversation_token is None
        assert after == before

        approved = _approve(config, held)
        assert approved.status == "pending"
        assert approved.conversation_token == token
        with db.get_db(config.db_path) as conn:
            stored = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE room_token = ? AND task_id = ?",
                (token, held),
            ).fetchone()[0]
        assert stored == 1
