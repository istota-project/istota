"""What the owner is told when an edit drops a job's approval (#925).

The drop stays. What matters: the request says what was approved and when, the diff is
against the text the grant actually named, it cannot read as no change when the text
moved, and an answer from the owner ends the record.
"""

import sqlite3

import pytest

from istota import db
from istota.cron_loader import CronJob, sync_cron_jobs_to_db
from istota.rooms import egress

USER = "alice"
SUBJECT = "cron:j"


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "t.db"
    db.init_db(path)
    with db.get_db(path) as c:
        yield c


def _job(prompt="read the news\nsend it", **fields):
    return CronJob(name="j", cron="0 9 * * *", prompt=prompt, **fields)


def _sync(conn, job):
    return sync_cron_jobs_to_db(conn, USER, [job])


def _row(conn):
    return egress.job_row_by_subject(conn, USER, SUBJECT)


def _approve(conn, kind="open"):
    egress.add_grant(
        conn, USER, SUBJECT, f"{kind}:{egress.job_digest(_row(conn))}", decided_via="web",
    )


def _dropped(conn, entry="open"):
    view = egress.describe_subject(conn, USER, SUBJECT)
    return next(row for row in view["refused"] if row["entry"] == entry)["dropped_approval"]


def _prompt_lines(record):
    return next(change for change in record["changes"] if change["field"] == "prompt")["lines"]


def test_an_edit_says_what_was_approved_and_shows_the_change(conn):
    _sync(conn, _job())
    _approve(conn)
    conn.execute(
        "UPDATE egress_grants SET decided_at = '2026-10-30 12:00:00' WHERE subject = ?", (SUBJECT,),
    )
    assert _sync(conn, _job("read the news\nsend it\nand post it")) == [SUBJECT]
    assert egress.grants_for(conn, USER, SUBJECT) == frozenset()
    record = _dropped(conn)
    assert record["approved_at"] == "2026-10-30T12:00:00Z"
    assert record["text_changed_at"].endswith("Z")
    assert "+and post it" in _prompt_lines(record)
    assert " send it" in _prompt_lines(record)
    assert not any(line.startswith(("---", "+++")) for line in _prompt_lines(record))


def test_a_second_edit_is_compared_with_the_approved_text_not_the_first_edit(conn):
    """Otherwise a harmless edit, then a hostile one, shows only the second as new, and
    the first never shows at all."""
    _sync(conn, _job("approved"))
    _approve(conn)
    _sync(conn, _job("first edit"))
    conn.execute("UPDATE egress_refused SET text_changed_at = '2020-01-01 00:00:00'")
    _sync(conn, _job("second edit"))
    record = _dropped(conn)
    lines = _prompt_lines(record)
    assert "-approved" in lines and "+second edit" in lines
    assert "-first edit" not in lines
    assert not record["text_changed_at"].startswith("2020")


def test_the_text_returning_to_what_was_approved_reads_as_no_change(conn):
    _sync(conn, _job("approved"))
    _approve(conn)
    _sync(conn, _job("edited"))
    _sync(conn, _job("approved"))
    assert _dropped(conn)["changes"] == []
    assert egress.grants_for(conn, USER, SUBJECT) == frozenset()


@pytest.mark.parametrize("before, after", [
    ("line", "line\n"),
    ("a\nb", "a b"),
    ("a\nb", "a\r\nb"),
])
def test_text_that_moved_never_reads_as_no_change(conn, before, after):
    """`splitlines` reads each pair as equal, and the digest does not."""
    _sync(conn, _job(before))
    _approve(conn)
    _sync(conn, _job(after))
    assert _dropped(conn)["changes"] != []


@pytest.mark.parametrize("edited, shown", [
    ("run​", "+run\\u{200b}"),
    ("run\U000e0041", "+run\\u{e0041}"),
    ("run\r", "+run\\u{000d}"),
    ("run more", "+run\\u{2028}more"),
    ("a\tb", "+a\tb"),
])
def test_what_a_browser_would_not_draw_is_spelled_out(conn, edited, shown):
    _sync(conn, _job("run"))
    _approve(conn)
    _sync(conn, _job(edited))
    assert shown in _prompt_lines(_dropped(conn))


def test_access_the_upgrade_seeded_does_not_read_as_approved(conn):
    _sync(conn, _job("first"))
    egress.add_grant(
        conn, USER, SUBJECT, f"open:{egress.job_digest(_row(conn))}", decided_via="migration",
    )
    _sync(conn, _job("second"))
    assert _dropped(conn)["carried_over"] is True


def test_access_a_person_approved_reads_as_approved(conn):
    _sync(conn, _job("first"))
    _approve(conn)
    _sync(conn, _job("second"))
    assert _dropped(conn)["carried_over"] is False


@pytest.mark.parametrize("field, approved, edited", [
    ("publish_shared_kv", {"publish_shared_kv": "a/b"}, {"publish_shared_kv": "c/d"}),
    ("publish_shared_kv_trusted", {"publish_shared_kv": "a/b"},
     {"publish_shared_kv": "a/b", "publish_shared_kv_trusted": True}),
])
def test_where_the_answer_goes_shows_too(conn, field, approved, edited):
    _sync(conn, _job(**approved))
    _approve(conn)
    _sync(conn, _job(**edited))
    assert field in {change["field"] for change in _dropped(conn)["changes"]}


def test_every_digest_column_is_compared():
    approved = {column: "" for column in egress.DIGEST_COLUMNS}
    current = {column: "x" for column in egress.DIGEST_COLUMNS}
    changes = egress.job_text_changes(approved, current)
    assert {change["field"] for change in changes} == set(egress.DIGEST_COLUMNS)


def test_text_too_long_to_compare_says_so_rather_than_nothing(conn):
    long_text = "x\n" * 5000
    _sync(conn, _job(long_text))
    _approve(conn)
    _sync(conn, _job(long_text + "y"))
    record = _dropped(conn)
    assert record["changes"] is None
    assert record["approved_at"]


def test_a_grant_for_other_text_is_not_reported_as_approving_the_row(conn):
    """The record's text is the row's, so it holds only if the grant named that text."""
    _sync(conn, _job("row text"))
    egress.add_grant(conn, USER, SUBJECT, "open:" + "a" * 64, decided_via="web")
    _sync(conn, _job("edited"))
    assert _dropped(conn) is None


def test_a_job_that_held_no_approval_has_nothing_to_explain(conn):
    _sync(conn, _job("first", reach="public"))
    _sync(conn, _job("second", reach="public"))
    assert _dropped(conn, "public") is None


def test_public_is_recorded_as_public(conn):
    _sync(conn, _job("first", reach="public"))
    _approve(conn, "public")
    _sync(conn, _job("second", reach="public"))
    assert "+second" in _prompt_lines(_dropped(conn, "public"))


def test_reapproving_ends_the_record(conn):
    _sync(conn, _job("first"))
    _approve(conn)
    _sync(conn, _job("second"))
    egress.approve_job(
        conn, USER, SUBJECT, "open", egress.job_digest(_row(conn)), decided_via="web",
    )
    _sync(conn, _job("second"))
    assert egress.describe_subject(conn, USER, SUBJECT)["refused"] == []
    # The next drop reports the text approved last, not the one before it.
    _sync(conn, _job("third"))
    assert "-second" in _prompt_lines(_dropped(conn))


def test_dismissing_ends_the_record_so_a_later_request_does_not_claim_it(conn):
    _sync(conn, _job("first", reach="full"))
    _approve(conn)
    _sync(conn, _job("second", reach="full"))
    egress.dismiss_refused(conn, USER, SUBJECT, "open")
    _sync(conn, _job("third", reach="full"))
    assert _dropped(conn) is None


def test_an_upgraded_database_gains_the_columns(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as old:
        old.execute(
            "CREATE TABLE egress_refused (user_id TEXT NOT NULL, subject TEXT NOT NULL, "
            "entry TEXT NOT NULL, hits INTEGER NOT NULL DEFAULT 1, "
            "last_seen TEXT NOT NULL DEFAULT (datetime('now')), "
            "dismissed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (user_id, subject, entry))"
        )
        old.execute("INSERT INTO egress_refused (user_id, subject, entry) VALUES ('alice', 'cron:j', 'open')")
    db.init_db(path)
    with db.get_db(path) as conn:
        assert egress.list_refused(conn, "alice", "cron:j")[0]["approved_text"] is None


def test_a_job_going_takes_the_record(conn):
    _sync(conn, _job("first"))
    _approve(conn)
    _sync(conn, _job("second"))
    egress.forget_subject(conn, USER, SUBJECT)
    assert conn.execute("SELECT COUNT(*) FROM egress_refused").fetchone()[0] == 0
