"""The `notifications` table migration.

Run against a DB that already holds rows, not a fresh one: the fresh-install
shape is what let ISSUE-261 ship green. `_run_migrations` shares one connection
in legacy `isolation_level` mode, so whether a transaction is already open when
this migration runs depends on which tables the DB being upgraded happens to
have — and a migration that assumes otherwise fails only on upgraded DBs.

The backfill half of this file — everything under `TestBackfill` — runs against a DB that also holds
a live held queue, which is what the two chat-pane strips used to render and
what would otherwise vanish when they came out.
"""

import sqlite3

import pytest

from istota import db
from istota import notification_sources as sources
from istota import notification_store as store
from istota.config import Config, UserConfig
from istota.notification_resolvers import confirmation as confirmation_source
from istota.notification_resolvers import outbound_draft as draft_source

# The one-shot guard on the backfill, restated rather than imported so a rename
# of the marker has to be made deliberately in both places.
BACKFILL_MARKER = "notifications_backfill_v1"


def _table_names(conn):
    return {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def _index_names(conn):
    return {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }


def _columns(conn):
    """`{name: (type, notnull, default)}` for the notifications table."""
    return {
        r[1]: (r[2], r[3], r[4])
        for r in conn.execute("PRAGMA table_info(notifications)").fetchall()
    }


def _indexed_columns(conn):
    indexes = {}
    for index in conn.execute("PRAGMA index_list(notifications)").fetchall():
        name = index[1]
        indexes[name] = [
            r[2] for r in conn.execute(f"PRAGMA index_info({name})").fetchall()
        ]
    return indexes


@pytest.fixture
def upgraded_db(tmp_path):
    """A DB with history, then stripped of `notifications` as an old deploy is."""
    path = tmp_path / "istota.db"
    db.init_db(path)
    with db.get_db(path) as conn:
        conn.execute(
            "INSERT INTO tasks (prompt, user_id, source_type, status) "
            "VALUES ('done thing', 'alice', 'talk', 'completed')"
        )
        conn.execute(
            "INSERT INTO outbound_drafts (user_id, status, to_addrs, subject, body) "
            "VALUES ('alice', 'discarded', '[\"x@y.invalid\"]', 'Re: hi', 'body')"
        )
        conn.execute("DROP TABLE notifications")
    return path


class TestFreshInstall:
    def test_schema_creates_the_table_and_indexes(self, tmp_path):
        path = tmp_path / "fresh.db"
        db.init_db(path)
        with db.get_db(path) as conn:
            assert "notifications" in _table_names(conn)
            assert "idx_notifications_user_state" in _index_names(conn)
            assert "idx_notifications_object" in _index_names(conn)

    def test_the_unique_key_is_user_source_dedup(self, tmp_path):
        path = tmp_path / "fresh.db"
        db.init_db(path)
        with db.get_db(path) as conn:
            conn.execute(
                "INSERT INTO notifications (user_id, source, dedup_key, title) "
                "VALUES ('alice', 'confirmation', 'task:7', 't')"
            )
            # A different user with the same key is a different row.
            conn.execute(
                "INSERT INTO notifications (user_id, source, dedup_key, title) "
                "VALUES ('bob', 'confirmation', 'task:7', 't')"
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO notifications (user_id, source, dedup_key, title) "
                    "VALUES ('alice', 'confirmation', 'task:7', 't')"
                )

    def test_defaults_match_the_declared_shape(self, tmp_path):
        path = tmp_path / "fresh.db"
        db.init_db(path)
        with db.get_db(path) as conn:
            conn.execute(
                "INSERT INTO notifications (user_id, source, dedup_key, title) "
                "VALUES ('alice', 'confirmation', 'task:7', 't')"
            )
            row = conn.execute("SELECT * FROM notifications").fetchone()
        assert row["state"] == "open"
        assert row["severity"] == "info"
        assert row["actionable"] == 0
        assert row["occurrences"] == 1
        assert row["body"] == ""
        assert row["params"] == "{}"
        assert row["seen_at"] is None
        assert row["last_delivered_at"] is None
        # The ISO-Z millisecond form `db.iso_utc_now` writes, so Python-built
        # bounds and SQL defaults compare lexicographically.
        assert row["created_at"].endswith("Z") and "T" in row["created_at"]
        assert len(row["created_at"]) == len(db.iso_utc_now())


class TestUpgradedInstall:
    def test_migration_creates_the_table_on_a_db_with_history(self, upgraded_db):
        """Driven through the migration alone.

        `init_db` runs the migrations and *then* executes schema.sql, which
        carries the same CREATE IF NOT EXISTS statements — so an `init_db`-driven
        assertion here passes with `_migrate_notifications` deleted outright and
        proves nothing about it.
        """
        conn = sqlite3.connect(upgraded_db)
        conn.row_factory = sqlite3.Row
        try:
            assert "notifications" not in _table_names(conn)

            db._migrate_notifications(conn)
            conn.commit()

            assert "notifications" in _table_names(conn)
            assert "idx_notifications_user_state" in _index_names(conn)
            assert "idx_notifications_object" in _index_names(conn)
            # The history the migration ran over is untouched.
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM outbound_drafts"
            ).fetchone()[0] == 1
        finally:
            conn.close()

    def test_the_migrated_table_matches_the_schema_file(self, upgraded_db, tmp_path):
        """The two copies of the DDL must not drift apart.

        `_migrate_notifications` is what an upgraded deploy gets and schema.sql
        is what a fresh install gets; a column present in one and not the other
        is a defect that surfaces only on whichever half nobody tested.
        """
        migrated = sqlite3.connect(upgraded_db)
        migrated.row_factory = sqlite3.Row
        fresh_path = tmp_path / "fresh.db"
        db.init_db(fresh_path)
        try:
            db._migrate_notifications(migrated)
            migrated.commit()
            with db.get_db(fresh_path) as fresh:
                assert _columns(migrated) == _columns(fresh)
                assert _indexed_columns(migrated) == _indexed_columns(fresh)
        finally:
            migrated.close()

    def test_migration_runs_standalone_on_an_inherited_transaction(self, upgraded_db):
        """The shape `_run_migrations` actually calls it in: DML already open."""
        conn = sqlite3.connect(upgraded_db)
        conn.row_factory = sqlite3.Row
        try:
            # A zero-row UPDATE is enough to open the implicit transaction.
            conn.execute("UPDATE tasks SET status = 'x' WHERE id = -1")
            db._migrate_notifications(conn)
            conn.commit()
            assert "notifications" in _table_names(conn)
        finally:
            conn.close()

    def test_migration_is_idempotent(self, upgraded_db):
        """Re-run directly, not through `init_db` — see the note above."""
        conn = sqlite3.connect(upgraded_db)
        conn.row_factory = sqlite3.Row
        try:
            db._migrate_notifications(conn)
            conn.execute(
                "INSERT INTO notifications (user_id, source, dedup_key, title) "
                "VALUES ('alice', 'confirmation', 'task:7', 't')"
            )
            conn.commit()

            db._migrate_notifications(conn)
            db._migrate_notifications(conn)
            conn.commit()

            assert conn.execute(
                "SELECT COUNT(*) FROM notifications"
            ).fetchone()[0] == 1
        finally:
            conn.close()

        # And the whole init path stays clean over an already-migrated DB.
        db.init_db(upgraded_db)
        with db.get_db(upgraded_db) as check:
            assert check.execute(
                "SELECT COUNT(*) FROM notifications"
            ).fetchone()[0] == 1

    def test_migration_tolerates_a_very_early_database(self, tmp_path):
        """No tables at all — the migration must not be what breaks init."""
        path = tmp_path / "bare.db"
        conn = sqlite3.connect(path)
        try:
            db._migrate_notifications(conn)
            conn.commit()
            assert "notifications" in _table_names(conn)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# The backfill
#
# Without it, the held queue a user could see in the chat-pane strips vanishes
# the moment those strips come out. Everything below runs against a DB that
# already holds history — completed tasks, closed drafts, another user's rows —
# because the fresh-install shape is what let ISSUE-261 ship green.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _registry():
    """`list_open` resolves through the process-wide registry."""
    sources.reset_registry()
    yield
    sources.reset_registry()


def _held_task(conn, user_id, prompt, question, *, source_type="web"):
    task_id = db.create_task(
        conn, prompt=prompt, user_id=user_id, source_type=source_type,
        conversation_token="a1b2c3d4e5f60718",
    )
    db.set_task_confirmation(conn, task_id, question)
    return task_id


def _pending_draft(conn, user_id, *, to="stranger@example.invalid",
                   subject="Re: Invite", cc="[]", bcc="[]", room_token=None):
    cursor = conn.execute(
        "INSERT INTO outbound_drafts "
        "(user_id, status, to_addrs, cc_addrs, bcc_addrs, subject, body, room_token) "
        "VALUES (?, 'pending', ?, ?, ?, ?, 'the body', ?)",
        (user_id, f'["{to}"]', cc, bcc, subject, room_token),
    )
    return cursor.lastrowid


def _rows(conn, user_id=None):
    if user_id is None:
        return conn.execute("SELECT * FROM notifications ORDER BY id").fetchall()
    return conn.execute(
        "SELECT * FROM notifications WHERE user_id = ? ORDER BY id", (user_id,),
    ).fetchall()


def _marked(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM _migration_state WHERE name = ?", (BACKFILL_MARKER,),
    ).fetchone() is not None


@pytest.fixture
def held_db(tmp_path):
    """A DB with real history, with the inbox unwritten.

    This is the upgrade shape: the deploy has been running for weeks, so there
    are completed tasks and closed drafts to step over, and anything genuinely
    waiting has no row because the producers did not exist when it was parked.
    """
    path = tmp_path / "istota.db"
    db.init_db(path)
    with db.get_db(path) as conn:
        # History the backfill must step over.
        db.create_task(conn, prompt="done thing", user_id="alice", source_type="talk")
        conn.execute(
            "INSERT INTO outbound_drafts (user_id, status, to_addrs, subject, body) "
            "VALUES ('alice', 'discarded', '[\"x@y.invalid\"]', 'Re: old', 'b')"
        )
        conn.execute(
            "INSERT INTO outbound_drafts (user_id, status, to_addrs, subject, body) "
            "VALUES ('alice', 'sent', '[\"x@y.invalid\"]', 'Re: gone', 'b')"
        )
        # `init_db` has already run the backfill over an empty queue; rewind it
        # so each test drives the real first pass.
        conn.execute("DELETE FROM notifications")
        conn.execute(
            "DELETE FROM _migration_state WHERE name = ?", (BACKFILL_MARKER,),
        )
        conn.commit()
    return path


@pytest.fixture
def config(held_db, tmp_path):
    return Config(
        db_path=held_db,
        nextcloud_mount_path=tmp_path / "mount",
        users={"alice": UserConfig(display_name="Alice")},
    )


class TestBackfill:
    def test_a_held_task_and_a_pending_draft_each_get_a_row(self, held_db):
        with db.get_db(held_db) as conn:
            task_id = _held_task(conn, "alice", "do the thing", "Do the thing?")
            draft_id = _pending_draft(conn, "alice")
            conn.commit()

            db._backfill_notifications(conn)

            keyed = {(r["source"], r["dedup_key"]): r for r in _rows(conn)}
        assert set(keyed) == {
            ("confirmation", f"task:{task_id}"),
            ("outbound_draft", f"draft:{draft_id}"),
        }
        held = keyed[("confirmation", f"task:{task_id}")]
        assert held["user_id"] == "alice"
        assert held["state"] == "open"
        assert held["actionable"] == 1
        assert held["object_type"] == "task"
        assert held["object_id"] == str(task_id)
        assert held["occurrences"] == 1
        # Nothing was sent. A migration must not push, and stamping a delivery
        # that never happened would suppress the first real one.
        assert held["last_delivered_at"] is None
        draft = keyed[("outbound_draft", f"draft:{draft_id}")]
        assert draft["object_type"] == "draft"
        assert draft["object_id"] == str(draft_id)
        assert draft["actionable"] == 1
        assert draft["last_delivered_at"] is None

    def test_the_keys_are_the_producers_own(self, held_db):
        """The whole idempotency claim rests on this.

        A backfill key one character off the producer's means every held item
        shows twice, permanently, with only one of the two closable — and the
        UNIQUE constraint that is supposed to prevent it never fires, because
        the two keys differ.
        """
        with db.get_db(held_db) as conn:
            task_id = _held_task(conn, "alice", "do the thing", "Do the thing?")
            draft_id = _pending_draft(conn, "alice")
            # The producers run first, exactly as they do on a deploy where the
            # daemon parked something before `istota init` was next run.
            confirmation_source.write(
                conn, "alice", task_id=task_id, title="held", body="b",
            )
            draft_source.write(
                conn, "alice", draft_id=draft_id, title="draft", body="b",
            )
            conn.commit()
            assert len(_rows(conn)) == 2

            db._backfill_notifications(conn)

            rows = _rows(conn)
        assert len(rows) == 2, "the backfill duplicated the producers' rows"
        # Not merely deduped — untouched. A backfill is not a second occurrence
        # of the thing being notified about, and the producer's own text is the
        # newer of the two.
        assert [r["occurrences"] for r in rows] == [1, 1]
        assert [r["title"] for r in rows] == ["held", "draft"]

    def test_closed_objects_are_not_backfilled(self, held_db):
        """Completed tasks and sent/discarded drafts are history, not an inbox."""
        with db.get_db(held_db) as conn:
            db._backfill_notifications(conn)
            assert _rows(conn) == []

    def test_a_sending_draft_is_not_backfilled(self, held_db):
        """Scoped to `pending`, and this is the honest reason rather than a
        safety one.

        The resolver *does* render a `sending` row — with a status note and no
        actions, because nobody can say whether the mail went out — so "it would
        offer an approval for mail that may already be gone" is not true and
        must not be written here as though it were. Seeding one would need a
        per-row `actionable` and a stored body that does not read "Nothing was
        sent", which the spec scoped to `pending`. A pre-upgrade row stuck in
        `sending` therefore reaches no web surface at all; that is a known gap
        recorded in `_backfill_notifications`, not a property worth defending.
        """
        with db.get_db(held_db) as conn:
            conn.execute(
                "INSERT INTO outbound_drafts "
                "(user_id, status, to_addrs, subject, body) "
                "VALUES ('alice', 'sending', '[\"x@y.invalid\"]', 'Re: mid', 'b')"
            )
            conn.commit()
            db._backfill_notifications(conn)
            assert _rows(conn) == []

    def test_each_row_goes_to_the_object_s_own_user(self, held_db):
        with db.get_db(held_db) as conn:
            mine = _held_task(conn, "alice", "mine", "Mine?")
            theirs = _held_task(conn, "bob", "theirs", "Theirs?")
            my_draft = _pending_draft(conn, "alice")
            their_draft = _pending_draft(conn, "bob")
            conn.commit()

            db._backfill_notifications(conn)

            alice = {(r["source"], r["dedup_key"]) for r in _rows(conn, "alice")}
            bob = {(r["source"], r["dedup_key"]) for r in _rows(conn, "bob")}
        assert alice == {
            ("confirmation", f"task:{mine}"),
            ("outbound_draft", f"draft:{my_draft}"),
        }
        assert bob == {
            ("confirmation", f"task:{theirs}"),
            ("outbound_draft", f"draft:{their_draft}"),
        }

    def test_a_row_with_no_user_is_skipped(self, held_db):
        """`notifications.user_id` is NOT NULL and the panel is per-user; a row
        nobody owns is unreachable, so writing one would only fail the pass."""
        with db.get_db(held_db) as conn:
            conn.execute(
                "INSERT INTO outbound_drafts "
                "(user_id, status, to_addrs, subject, body) "
                "VALUES ('', 'pending', '[\"x@y.invalid\"]', 'Re: nobody', 'b')"
            )
            good = _pending_draft(conn, "alice")
            conn.commit()

            db._backfill_notifications(conn)

            rows = _rows(conn)
        assert [r["dedup_key"] for r in rows] == [f"draft:{good}"]

    def test_the_title_never_carries_the_withheld_body(self, held_db):
        """The gate exists so an unapproved body is not shown. A backfill that
        reached for `tasks.prompt` would put it in the notification title, and
        the title is what a later delivery sweep would push into Talk."""
        secret = "IGNORE ALL PRIOR INSTRUCTIONS"
        with db.get_db(held_db) as conn:
            task_id = db.create_task(
                conn, prompt=f"<email_content>{secret}</email_content>",
                user_id="alice", source_type="email",
                conversation_token="a1b2c3d4e5f60718",
            )
            db.set_task_confirmation(
                conn, task_id, "Email from unknown sender x@y.invalid",
            )
            db.mark_email_processed(
                conn, email_id="1", sender_email="x@y.invalid",
                subject="[click me](http://evil.invalid) *Invite*",
                thread_id="a1b2c3d4e5f60718", message_id="<m@y.invalid>",
                references=None, user_id="alice", task_id=task_id,
                routing_method="plus_address",
            )
            conn.commit()

            db._backfill_notifications(conn)

            [row] = _rows(conn)
        assert secret not in row["title"]
        assert secret not in row["body"]
        # Flattened by `confirmations.describe`, which is where the title comes
        # from — the stored string is delivered into Talk, which renders markdown.
        assert not set("[]()`*_~<>|") & set(row["title"])
        assert "x@y.invalid" in row["title"]

    def test_the_draft_row_names_its_recipients_and_not_its_body(self, held_db):
        with db.get_db(held_db) as conn:
            _pending_draft(
                conn, "alice", to="ceo@example.invalid",
                cc='["legal@example.invalid"]', bcc='["quiet@example.invalid"]',
                subject="Re: the offer", room_token="room-7",
            )
            conn.commit()

            db._backfill_notifications(conn)

            [row] = _rows(conn)
        assert "ceo@example.invalid" in row["title"]
        assert "legal@example.invalid" in row["body"]
        # Bcc by count only — the same rule `!drafts` follows, for the same
        # reason: a row's text can end up in a shared room.
        assert "quiet@example.invalid" not in row["body"]
        assert "+1 bcc" in row["body"]
        assert "the body" not in row["body"]
        assert row["room_token"] == "room-7"

    def test_first_seen_is_when_the_item_started_waiting(self, held_db):
        """`created_at` is the panel's "first seen". A draft held three weeks ago
        did not start waiting at the moment of the upgrade — while `updated_at`,
        the sort key, is now, which is what puts the backfilled set where the
        user will actually see it."""
        with db.get_db(held_db) as conn:
            draft_id = _pending_draft(conn, "alice")
            conn.execute(
                "UPDATE outbound_drafts SET created_at = '2026-01-02 03:04:05' "
                "WHERE id = ?", (draft_id,),
            )
            conn.commit()

            db._backfill_notifications(conn)

            [row] = _rows(conn)
        assert row["created_at"] == "2026-01-02T03:04:05.000Z"
        assert row["updated_at"] > row["created_at"]
        # Same shape as every other value in the column, so the sweeps' bounds
        # and `mark_seen`'s version check compare against a like string.
        assert len(row["updated_at"]) == len(db.iso_utc_now())
        assert len(row["created_at"]) == len(db.iso_utc_now())

    def test_an_unreadable_timestamp_falls_back_rather_than_failing_the_row(
        self, held_db,
    ):
        with db.get_db(held_db) as conn:
            draft_id = _pending_draft(conn, "alice")
            conn.execute(
                "UPDATE outbound_drafts SET created_at = 'sometime' WHERE id = ?",
                (draft_id,),
            )
            conn.commit()

            db._backfill_notifications(conn)

            [row] = _rows(conn)
        assert row["dedup_key"] == f"draft:{draft_id}"
        assert len(row["created_at"]) == len(db.iso_utc_now())

    def test_it_runs_once(self, held_db):
        """Markered. A user who dismissed a backfilled row must not have it
        raised again by the next `istota init`."""
        with db.get_db(held_db) as conn:
            task_id = _held_task(conn, "alice", "do the thing", "Do the thing?")
            conn.commit()

            db._backfill_notifications(conn)
            assert _marked(conn)
            conn.execute("DELETE FROM notifications")
            conn.commit()

            db._backfill_notifications(conn)

            assert _rows(conn) == []
            assert db.get_task(conn, task_id).status == "pending_confirmation"

    def test_a_second_run_before_the_marker_lands_is_still_one_row(self, held_db):
        """Structurally idempotent as well as markered, so a pass that failed
        part way replays cleanly rather than doubling what it already wrote."""
        with db.get_db(held_db) as conn:
            _held_task(conn, "alice", "do the thing", "Do the thing?")
            conn.commit()

            db._backfill_notifications(conn)
            conn.execute(
                "DELETE FROM _migration_state WHERE name = ?", (BACKFILL_MARKER,),
            )
            conn.commit()
            db._backfill_notifications(conn)

            rows = _rows(conn)
        assert len(rows) == 1
        assert rows[0]["occurrences"] == 1

    def test_it_commits_the_transaction_it_inherited(self, held_db):
        """The ISSUE-261 shape, which shipped green on a fresh install and
        killed inbound email for two days on every upgraded one.

        `_run_migrations` shares one connection in legacy `isolation_level`
        mode, where a DML statement — a zero-row UPDATE is enough — opens an
        implicit transaction and holds it. A migration that wants its own has to
        commit that one first.
        """
        conn = sqlite3.connect(held_db)
        conn.row_factory = sqlite3.Row
        try:
            task_id = _held_task(conn, "alice", "do the thing", "Do the thing?")
            conn.commit()
            # Exactly what an earlier migration leaves behind.
            conn.execute("UPDATE tasks SET status = status WHERE id = -1")
            assert conn.in_transaction

            db._backfill_notifications(conn)

            assert [r["dedup_key"] for r in _rows(conn)] == [f"task:{task_id}"]
            assert _marked(conn)
        finally:
            conn.close()
        # And it is durable, not sitting in a transaction nobody committed.
        with db.get_db(held_db) as check:
            assert len(_rows(check)) == 1
            assert _marked(check)

    def test_a_raising_read_re_arms_instead_of_aborting_init(
        self, held_db, monkeypatch,
    ):
        """The blast radius is the whole of `init_db`, not the inbox.

        Building a row calls `get_task` and `confirmations.describe`, and
        `describe` reads `processed_emails` — whose `uidvalidity` column is
        created by a migration that logs and re-arms on failure rather than
        raising. So the documented retry state of *that* migration used to make
        this one raise "no such column" straight out of `_run_migrations`, which
        runs before `schema.sql` and would therefore take every migration after
        it down too. Any read here has to be caught, not just the two queries.
        """
        from istota import confirmations

        def boom(*a, **kw):
            raise sqlite3.OperationalError("no such column: uidvalidity")

        with db.get_db(held_db) as conn:
            _held_task(conn, "alice", "do the thing", "Do the thing?")
            conn.commit()
            monkeypatch.setattr(confirmations, "describe", boom)

            db._backfill_notifications(conn)  # must not raise

            assert _rows(conn) == []
            assert not _marked(conn), "a pass that wrote nothing must re-arm"

    def test_a_raising_read_does_not_abort_the_whole_init(self, held_db, monkeypatch):
        """The same thing through the real entry point, which is where it bit."""
        from istota import confirmations

        def boom(*a, **kw):
            raise sqlite3.OperationalError("no such column: uidvalidity")

        with db.get_db(held_db) as conn:
            _held_task(conn, "alice", "do the thing", "Do the thing?")
            conn.commit()
        monkeypatch.setattr(confirmations, "describe", boom)

        db.init_db(held_db)  # must not raise

        with db.get_db(held_db) as conn:
            # `schema.sql` still ran: the migration after this one is not dead.
            assert "notifications" in _table_names(conn)
            assert not _marked(conn)

    def test_a_very_early_database_leaves_the_marker_unset(self, tmp_path):
        """No `tasks` table yet. The backfill must not be what breaks init, and
        must re-arm rather than marking a job it never did — the queue it exists
        to rescue is written by the very tables it could not read."""
        path = tmp_path / "bare.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS _migration_state ("
                "  name TEXT PRIMARY KEY, applied_at TEXT)"
            )
            db._migrate_notifications(conn)
            conn.commit()

            db._backfill_notifications(conn)

            assert _rows(conn) == []
            assert not _marked(conn)
        finally:
            conn.close()

    def test_init_db_backfills_an_upgraded_database(self, held_db):
        """End to end, through the entry point an upgrade actually runs."""
        with db.get_db(held_db) as conn:
            task_id = _held_task(conn, "alice", "do the thing", "Do the thing?")
            draft_id = _pending_draft(conn, "alice")
            conn.commit()

        db.init_db(held_db)

        with db.get_db(held_db) as conn:
            keys = {(r["source"], r["dedup_key"]) for r in _rows(conn)}
            assert _marked(conn)
        assert keys == {
            ("confirmation", f"task:{task_id}"),
            ("outbound_draft", f"draft:{draft_id}"),
        }

    def test_a_backfilled_row_renders_with_its_real_actions(self, config, held_db):
        """The point of matching the producers' object ids: a backfilled row has
        to resolve through the ordinary resolver, not merely exist."""
        with db.get_db(held_db) as conn:
            task_id = _held_task(conn, "alice", "do the thing", "Do the thing?")
            conn.commit()
            db._backfill_notifications(conn)

            rendered, total_open = store.list_open(config, conn, "alice")

        assert total_open == 1
        [item] = rendered
        assert item.source == "confirmation"
        assert [a.id for a in item.actions] == ["confirm", "discard"]
        assert [a.endpoint for a in item.actions] == [
            f"/chat/tasks/{task_id}/confirm", f"/chat/tasks/{task_id}/cancel",
        ]

    def test_a_backfilled_row_closes_through_the_ordinary_path(self, config, held_db):
        """The other half of the key having to match: the close path finds the
        row by object, so a backfilled row must be findable by it."""
        from istota import confirmations

        with db.get_db(held_db) as conn:
            task_id = _held_task(conn, "alice", "do the thing", "Do the thing?")
            conn.commit()
            db._backfill_notifications(conn)

            confirmations.approve(
                conn, db.get_task(conn, task_id), config=config, by="web",
            )

            [row] = _rows(conn)
        assert row["state"] == "resolved"
        assert row["resolved_by"] == "web"
