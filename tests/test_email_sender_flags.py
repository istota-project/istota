"""The per-sender record of suspected injection attempts (#897)."""

import pytest

from istota import db
from istota.config import Config, EmailConfig, SchedulerConfig, UserConfig
from istota.mail import sender_flags


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
