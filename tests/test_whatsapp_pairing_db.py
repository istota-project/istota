"""The durable pairing request channel: the columns, the helpers, the restart.

`whatsapp_runtime` already carries the live billing circuit breaker on a
deployment where WhatsApp is in production, so every assertion here is about a
migration that only ever *adds*. The seven columns are nullable with no default,
nothing is backfilled, and a build rolled back to before them selects none of
them — which is what makes an upgrade harmless in both directions.

**The scheduler-restart case is the one most worth reading.** The pairing window
and its watchdog live in bridge memory; the request row lives here. Restart the
process mid-window and the two disagree in the worst direction: the new bridge
has no window so `_handle_qr` discards codes again, nothing will ever close the
row, the relay keeps rendering a frozen code, and `request_whatsapp_pairing` is
guarded on there being no open request — so **every later pairing request is
refused for the life of the deployment**, on a surface whose whole purpose is
recovery. `TestTheSchedulerRestart` is the durable path that stops it, and its
two negative controls are named there.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from istota import db
from istota.config import Config, UserConfig
from istota.transport.whatsapp import baileys_bridge
from istota.transport.whatsapp import baileys_protocol as proto
from istota.transport.whatsapp import pairing_relay
from istota.transport.whatsapp.baileys_bridge import BaileysBridge
from istota.transport.whatsapp.baileys_runtime import poll_pairing_request

from .support.baileys_sidecar import FakeSidecar, SocketDir, wait_for
from .support.whatsapp_config import build_whatsapp_config

USER = "alice"
BAILEYS = "baileys"

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schema.sql"

#: The shape `whatsapp_runtime` had before this work, for the upgrade case.
LEGACY_TABLE = """
CREATE TABLE whatsapp_runtime (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    billing_blocked_at TEXT,
    billing_message_id TEXT,
    updated_at TEXT NOT NULL
);
"""


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "state" / "istota.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db.init_db(path)
    return path


@pytest.fixture
def config(tmp_path, db_path) -> Config:
    return Config(
        db_path=db_path,
        temp_dir=tmp_path / "tmp",
        whatsapp=build_whatsapp_config(enabled=True, provider=BAILEYS),
        users={USER: UserConfig()},
    )


def columns_of(conn: sqlite3.Connection, table: str) -> dict:
    return {
        row[1]: {"type": row[2], "notnull": row[3], "default": row[4]}
        for row in conn.execute(f"PRAGMA table_info({table})")
    }


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------


class TestTheMigration:
    def test_a_fresh_install_gets_the_columns_from_schema_sql(self, db_path):
        """`_run_migrations` runs *before* `schema.sql`, and `_add_columns`
        no-ops on a table that does not exist yet — so on a fresh install the
        CREATE is the only thing that can supply them."""
        with db.get_db(db_path) as conn:
            present = columns_of(conn, "whatsapp_runtime")
        for name in db.WHATSAPP_PAIRING_COLUMNS:
            assert name in present, name

    def test_an_upgraded_database_gets_them_from_the_migration(self, tmp_path):
        """The other half, and the half a live deployment takes: the table is
        `CREATE TABLE IF NOT EXISTS`, so an existing row's table is never
        rewritten and only the ALTERs can add anything."""
        path = tmp_path / "old.db"
        conn = sqlite3.connect(path)
        conn.executescript(LEGACY_TABLE)
        conn.execute(
            "INSERT INTO whatsapp_runtime (singleton, billing_blocked_at, "
            "billing_message_id, updated_at) VALUES (1, ?, ?, ?)",
            ("2026-01-01 00:00:00", "wamid.LIVE", "2026-01-01 00:00:00"),
        )
        conn.commit()
        conn.close()

        db.init_db(path)

        with db.get_db(path) as conn:
            present = columns_of(conn, "whatsapp_runtime")
            block = db.whatsapp_billing_block(conn)
        for name in db.WHATSAPP_PAIRING_COLUMNS:
            assert name in present, name
        # The live circuit breaker is untouched by the upgrade. This table's
        # other tenant is in production and the migration may only add.
        assert block is not None
        assert block.billing_blocked_at == "2026-01-01 00:00:00"
        assert block.billing_message_id == "wamid.LIVE"

    def test_every_column_is_nullable_with_no_default(self, db_path):
        """What makes the migration additive in *both* directions: nothing is
        backfilled, so an un-paired deployment reads NULL everywhere and
        behaves exactly as it did, and an older build ignores seven columns it
        never selects."""
        with db.get_db(db_path) as conn:
            present = columns_of(conn, "whatsapp_runtime")
        for name in db.WHATSAPP_PAIRING_COLUMNS:
            assert present[name]["notnull"] == 0, name
            assert present[name]["default"] is None, name

    def test_the_two_declarations_agree(self):
        """The columns are stated twice — `db.WHATSAPP_PAIRING_COLUMNS` for the
        migration and `schema.sql` for a fresh install — so a column added to
        one alone is a deployment where half the flow writes to a column that
        is not there. Asserted over the *names in the CREATE*, which is the
        text a fresh install actually executes."""
        source = SCHEMA_PATH.read_text()
        create = re.search(
            r"CREATE TABLE IF NOT EXISTS whatsapp_runtime \((.*?)\n\);",
            source,
            re.DOTALL,
        )
        assert create is not None
        declared = {
            match.group(1)
            for match in re.finditer(
                # Any type, not `TEXT`: `pairing_force` is an INTEGER, and a
                # regex naming one type reads a column of another as absent —
                # which is a drift guard that goes quietly one-sided.
                r"^\s{4}(pairing_\w+)\s+\w+,?$", create.group(1), re.MULTILINE
            )
        }
        assert declared == set(db.WHATSAPP_PAIRING_COLUMNS)

    def test_running_the_migration_twice_is_safe(self, db_path):
        """`_add_columns` answers "already there" by reading the schema rather
        than by catching `OperationalError`, which is what closes the
        check-then-ALTER race two connections reach at a first post-upgrade
        boot. A second `init_db` is the reachable half of that."""
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            present = columns_of(conn, "whatsapp_runtime")
        for name in db.WHATSAPP_PAIRING_COLUMNS:
            assert name in present, name

    def test_two_connections_racing_the_migration_both_succeed(self, tmp_path):
        """The race itself, on a database that has the legacy table and not the
        columns.

        **What the control establishes, and what it does not.** A bare
        hand-written `ALTER TABLE` here raises "duplicate column name" in
        whichever connection loses, and turns this red — measured, and that is
        the shape an implementer reaches for. A variant that wraps the same
        ALTER in `except sqlite3.OperationalError: pass` survives it and is
        *not* distinguished by this test; `_add_columns` tolerates errors too
        (its own docstring says why, for the lock case), so the two differ in
        what they hide rather than in what they survive. What rules the
        swallowing form out is the fresh-install case above, where the table
        does not exist yet and the helper has to answer without an exception
        reaching anybody.

        Driven at `_add_columns` rather than at `init_db`: racing two whole
        `init_db` calls fails intermittently on `journal_mode = WAL`, which
        needs exclusive access and does not honour the busy timeout — a
        pre-existing property of that function and not the question here.
        """
        path = tmp_path / "race.db"
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(LEGACY_TABLE)
        conn.commit()
        conn.close()

        errors: list[BaseException] = []
        start = threading.Barrier(2)

        def run() -> None:
            try:
                link = sqlite3.connect(path, timeout=30.0)
                try:
                    start.wait(timeout=5)
                    db._add_columns(
                        link, "whatsapp_runtime", db.WHATSAPP_PAIRING_COLUMNS
                    )
                    link.commit()
                finally:
                    link.close()
            except BaseException as exc:  # noqa: BLE001 — recorded, not raised
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert errors == []
        with db.get_db(path) as conn:
            present = columns_of(conn, "whatsapp_runtime")
        for name in db.WHATSAPP_PAIRING_COLUMNS:
            assert name in present, name


# ---------------------------------------------------------------------------
# The state vocabulary
# ---------------------------------------------------------------------------


class TestTheStateVocabulary:
    def test_the_six_window_spellings_match_the_relay_module(self):
        """The row and the relay file say the same thing about a live window,
        and `db` sits below `transport` so it restates them rather than
        importing. A drift means the relay reader and the poll disagree about
        what state a window is in."""
        assert db.WHATSAPP_PAIRING_AWAITING_SIDECAR == (
            pairing_relay.STATE_AWAITING_SIDECAR
        )
        assert db.WHATSAPP_PAIRING_AWAITING_SCAN == pairing_relay.STATE_AWAITING_SCAN
        assert db.WHATSAPP_PAIRING_SIDECAR_ABSENT == pairing_relay.STATE_SIDECAR_ABSENT
        assert db.WHATSAPP_PAIRING_PAIRED == pairing_relay.STATE_PAIRED
        assert db.WHATSAPP_PAIRING_EXPIRED == pairing_relay.STATE_EXPIRED
        assert db.WHATSAPP_PAIRING_FAILED == pairing_relay.STATE_FAILED

    def test_the_terminal_set_matches_the_relay_modules(self):
        assert db.WHATSAPP_PAIRING_TERMINAL_STATES == pairing_relay.TERMINAL_STATES

    def test_the_request_ttl_matches_the_bridge_constant(self):
        assert (
            db.WHATSAPP_PAIRING_WINDOW_SECONDS
            == baileys_bridge.PAIRING_WINDOW_SECONDS
        )

    def test_the_pre_window_states_are_not_window_states(self):
        """The load-bearing exclusion. A fresh `requested` row has no window
        behind it *by construction* — the window opens at the last step of
        `repair_session`, up to a sidecar-return plus a stop timeout later — so
        an orphan arm that read these two would expire the request it exists to
        service."""
        assert db.WHATSAPP_PAIRING_REQUESTED not in db.WHATSAPP_PAIRING_WINDOW_STATES
        assert db.WHATSAPP_PAIRING_SERVICING not in db.WHATSAPP_PAIRING_WINDOW_STATES
        assert not (
            db.WHATSAPP_PAIRING_WINDOW_STATES & db.WHATSAPP_PAIRING_TERMINAL_STATES
        )


# ---------------------------------------------------------------------------
# request_whatsapp_pairing
# ---------------------------------------------------------------------------


class TestTheRequestHelper:
    def test_a_request_writes_a_requested_row_with_an_absolute_deadline(
        self, db_path
    ):
        before = db.sql_datetime_now()
        with db.get_db(db_path) as conn:
            request_id = db.request_whatsapp_pairing(conn, USER, window_seconds=300)
            row = db.read_whatsapp_pairing(conn)
        assert request_id
        assert row["state"] == db.WHATSAPP_PAIRING_REQUESTED
        assert row["window_id"] == request_id
        assert row["requested_by"] == USER
        # Stamped at *request* time, which is what lets the poll's deadline arm
        # close a row nobody ever picked up. Absolute, not derived: an operator
        # editing the TTL must not move the deadline of a window in flight.
        assert row["expires_at"] > before
        assert row["expires_at"] > row["requested_at"]

    def test_a_second_request_while_one_is_open_is_refused(self, db_path):
        with db.get_db(db_path) as conn:
            first = db.request_whatsapp_pairing(conn, USER)
            assert db.request_whatsapp_pairing(conn, "bob") is None
            row = db.read_whatsapp_pairing(conn)
        # Refused, and the open request is untouched — not overwritten by the
        # second caller's user id.
        assert row["window_id"] == first
        assert row["requested_by"] == USER

    @pytest.mark.parametrize("state", sorted(db.WHATSAPP_PAIRING_TERMINAL_STATES))
    def test_a_request_over_a_closed_row_succeeds(self, db_path, state):
        with db.get_db(db_path) as conn:
            first = db.request_whatsapp_pairing(conn, USER)
            assert db.record_whatsapp_pairing_state(conn, first, state, "done")
            second = db.request_whatsapp_pairing(conn, "bob")
            row = db.read_whatsapp_pairing(conn)
        assert second and second != first
        assert row["state"] == db.WHATSAPP_PAIRING_REQUESTED
        # The previous window's message does not survive onto the new request.
        assert row["message"] == ""

    @pytest.mark.parametrize(
        "state", sorted(
            db.WHATSAPP_PAIRING_WINDOW_STATES
            | {db.WHATSAPP_PAIRING_SERVICING}
        ),
    )
    def test_a_request_over_a_live_one_is_refused_in_every_open_state(
        self, db_path, state
    ):
        with db.get_db(db_path) as conn:
            first = db.request_whatsapp_pairing(conn, USER)
            assert db.record_whatsapp_pairing_state(conn, first, state)
            assert db.request_whatsapp_pairing(conn, "bob") is None

    def test_two_threads_racing_one_request_produce_exactly_one_id(self, db_path):
        """Two admins pressing the button at once. The guard is in SQL — an
        upsert whose `WHERE` carries the precondition, `rowcount` as the answer
        — because a read followed by a write lets both callers see "no request
        open" and both get an id. Driven with two real threads, the way
        `test_a_status_racing_the_id_write_is_never_lost` drives the delivery
        race.

        Negative control: dropping the `WHERE` from the upsert's conflict
        branch turns this red with two ids.
        """
        results: list[str | None] = []
        errors: list[BaseException] = []
        start = threading.Barrier(2)

        def request() -> None:
            try:
                start.wait(timeout=5)
                with db.get_db(db_path) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    results.append(db.request_whatsapp_pairing(conn, USER))
            except BaseException as exc:  # noqa: BLE001 — recorded, not raised
                errors.append(exc)

        threads = [threading.Thread(target=request) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert errors == []
        assert len(results) == 2
        granted = [value for value in results if value]
        assert len(granted) == 1, results
        with db.get_db(db_path) as conn:
            row = db.read_whatsapp_pairing(conn)
        assert row["window_id"] == granted[0]

    def test_a_request_leaves_the_billing_circuit_alone(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.block_whatsapp_billing(conn, "wamid.LIVE")
            db.request_whatsapp_pairing(conn, USER)
            block = db.whatsapp_billing_block(conn)
        assert block is not None
        assert block.billing_message_id == "wamid.LIVE"

    def test_no_pairing_written_reads_as_no_pairing(self, db_path):
        """A deployment whose only use of this table was the billing circuit
        has a row with NULL pairing columns, which is not a pairing."""
        with db.get_db(db_path) as conn:
            assert db.block_whatsapp_billing(conn, "wamid.LIVE")
            assert db.read_whatsapp_pairing(conn) is None


# ---------------------------------------------------------------------------
# record_whatsapp_pairing_state / clear_whatsapp_pairing
# ---------------------------------------------------------------------------


class TestTheStateWrite:
    def test_a_stale_window_id_does_not_land(self, db_path):
        """The guard against a late writer: a coroutine servicing a request the
        poll has since closed and replaced holds a stale id."""
        with db.get_db(db_path) as conn:
            db.request_whatsapp_pairing(conn, USER)
            assert not db.record_whatsapp_pairing_state(
                conn, "some-other-window", db.WHATSAPP_PAIRING_SERVICING,
            )
            row = db.read_whatsapp_pairing(conn)
        assert row["state"] == db.WHATSAPP_PAIRING_REQUESTED

    @pytest.mark.parametrize("state", sorted(db.WHATSAPP_PAIRING_TERMINAL_STATES))
    def test_a_closed_row_is_never_reopened(self, db_path, state):
        """The second guard, and the one the id check cannot stand in for. The
        poll's deadline arm can expire a row while its coroutine is still
        running, and that coroutine's id *matches* — so without the terminal
        clause it would reopen a window nothing owns. Same discipline
        `_set_outcome` applies to `sent_whatsapp`.

        Negative control: dropping `AND pairing_state NOT IN (...)` turns this
        red, the row coming back `awaiting_sidecar`.
        """
        with db.get_db(db_path) as conn:
            request_id = db.request_whatsapp_pairing(conn, USER)
            assert db.record_whatsapp_pairing_state(conn, request_id, state)
            assert not db.record_whatsapp_pairing_state(
                conn, request_id, db.WHATSAPP_PAIRING_AWAITING_SIDECAR,
            )
            row = db.read_whatsapp_pairing(conn)
        assert row["state"] == state

    @pytest.mark.parametrize("empty", ["", None])
    def test_an_empty_window_id_is_refused_rather_than_compared(
        self, db_path, empty
    ):
        """SQL equality against `''` never matches a NULL column, so an id-less
        row would be unwritable by every caller here. Refused explicitly so the
        caller sees a decision rather than a lost race.

        **This case is documentation, not a control**, and it says so because
        the distinction is the thing this repo keeps finding: removing the
        refusal turns nothing red, since the UPDATE's `pairing_window_id = ''`
        matches no ordinary row either way. What actually closes the hazard is
        in the poll — `tests/test_whatsapp_pairing_poll.py::TestTheClosureCleanup
        ::test_an_id_less_row_is_cleared_rather_than_left_stuck` — because a
        non-terminal id-less row blocks every later request and the refusal
        alone would only make that block explicit.
        """
        with db.get_db(db_path) as conn:
            db.request_whatsapp_pairing(conn, USER)
            assert not db.record_whatsapp_pairing_state(
                conn, empty, db.WHATSAPP_PAIRING_SERVICING,
            )
            assert (
                db.read_whatsapp_pairing(conn)["state"]
                == db.WHATSAPP_PAIRING_REQUESTED
            )

    def test_a_window_open_adopts_the_bridges_id_and_deadline(self, db_path):
        """The row's id at `requested` is the *request's* own, because the
        bridge mints its window id only when it actually opens one. The relay
        file carries the bridge's id and the reader validates the two against
        each other, so the row adopts it here — with that window's own deadline,
        or the sequence's wait would eat into the time somebody has to scan."""
        deadline = time.time() + 4000.0
        with db.get_db(db_path) as conn:
            request_id = db.request_whatsapp_pairing(conn, USER, window_seconds=60)
            before = db.read_whatsapp_pairing(conn)["expires_at"]
            assert db.record_whatsapp_pairing_state(
                conn, request_id,
                db.WHATSAPP_PAIRING_AWAITING_SIDECAR,
                "a pairing window is open",
                adopt_window_id="window-abc",
                expires_at=deadline,
            )
            row = db.read_whatsapp_pairing(conn)
            # The request's own id no longer matches, so a writer holding it
            # cannot land — which is what makes the adoption a rotation.
            assert not db.record_whatsapp_pairing_state(
                conn, request_id, db.WHATSAPP_PAIRING_PAIRED,
            )
        assert row["window_id"] == "window-abc"
        assert row["state"] == db.WHATSAPP_PAIRING_AWAITING_SIDECAR
        assert row["message"] == "a pairing window is open"
        assert row["expires_at"] > before
        assert row["expires_at"] == db.sql_datetime_from_epoch(deadline)
        # Request-time facts are not rewritten by the adoption.
        assert row["requested_by"] == USER

    def test_a_plain_state_write_leaves_the_id_and_deadline_alone(self, db_path):
        with db.get_db(db_path) as conn:
            request_id = db.request_whatsapp_pairing(conn, USER)
            before = db.read_whatsapp_pairing(conn)
            assert db.record_whatsapp_pairing_state(
                conn, request_id, db.WHATSAPP_PAIRING_SERVICING,
            )
            row = db.read_whatsapp_pairing(conn)
        assert row["window_id"] == before["window_id"]
        assert row["expires_at"] == before["expires_at"]
        # `message=None` clears rather than preserving, so a stale reason from
        # an earlier transition cannot be read as this one's.
        assert row["message"] == ""

    def test_clear_drops_the_request_and_keeps_the_circuit(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.block_whatsapp_billing(conn, "wamid.LIVE")
            db.request_whatsapp_pairing(conn, USER)
            assert db.clear_whatsapp_pairing(conn)
            assert db.read_whatsapp_pairing(conn) is None
            assert not db.clear_whatsapp_pairing(conn)
            block = db.whatsapp_billing_block(conn)
        assert block is not None
        assert block.billing_message_id == "wamid.LIVE"

    def test_the_epoch_converter_round_trips_into_the_stored_format(self):
        """The deadline crosses two clocks — the bridge's window publishes epoch
        seconds and the row stores `sql_datetime_now`'s text, which the deadline
        arm compares lexically. One converter, so the two cannot be compared by
        whichever caller got there first."""
        rendered = db.sql_datetime_from_epoch(time.time() + 3600.0)
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", rendered)
        assert rendered > db.sql_datetime_now()


# ---------------------------------------------------------------------------
# The scheduler restart
# ---------------------------------------------------------------------------


@pytest.fixture
def sockets():
    directory = SocketDir()
    yield directory
    directory.cleanup()


@pytest.fixture
def relay_file(tmp_path) -> Path:
    return tmp_path / "state" / "whatsapp-pairing.json"


@pytest.fixture
async def fresh_bridge(config, sockets, relay_file):
    """The bridge a *restarted* process holds: no window, no last outcome.

    Its relay path is the one the dead process published through, because that
    file is on disk and the row still names its window.
    """
    instance = BaileysBridge(
        config,
        socket_path=sockets.socket,
        session_dir=sockets.session,
        send_timeout=2.0,
        pairing_relay_path=relay_file,
        pairing_window_seconds=600.0,
    )
    await instance.start()
    baileys_bridge.set_active_bridge(instance)
    try:
        yield instance
    finally:
        baileys_bridge.clear_active_bridge()
        await instance.stop()


@pytest.fixture
async def sidecar(fresh_bridge, sockets):
    fake = FakeSidecar(sockets.socket)
    await fake.connect()
    await wait_for(lambda: fresh_bridge.status.connected is True)
    try:
        yield fake
    finally:
        await fake.close()


def arrange_orphaned_window(db_path: Path, relay: Path) -> str:
    """What a process killed mid-window leaves behind.

    A durable row in a window-implying state carrying the bridge's window id,
    and a relay file on disk under that same id. Both written through the
    product's own helpers. The deadline is deliberately far in the future, so
    **only the orphan arm can close this row** — with a past deadline the
    deadline arm would close it too and the test would pass with the orphan arm
    deleted.
    """
    window_id = "window-from-the-dead-process"
    deadline = time.time() + 3600.0
    with db.get_db(db_path) as conn:
        request_id = db.request_whatsapp_pairing(conn, USER, window_seconds=3600)
        assert db.record_whatsapp_pairing_state(
            conn, request_id,
            db.WHATSAPP_PAIRING_AWAITING_SCAN,
            "The previous session was moved aside to /srv/session.old-20260101.",
            adopt_window_id=window_id,
            expires_at=deadline,
        )
    relay.parent.mkdir(parents=True, exist_ok=True)
    assert pairing_relay.write_relay(
        relay,
        pairing_relay.build_payload(
            window_id=window_id,
            state=pairing_relay.STATE_AWAITING_SCAN,
            expires_at=deadline,
            qr="2@SENTINELqrPAYLOAD/9x+abcDEF==",
            qr_seq=3,
        ),
    )
    return window_id


class TestTheSchedulerRestart:
    """The stuck-row class, on the surface whose whole purpose is recovery.

    Two negative controls, and **they must fail on different tests**:

    - Removing the orphan arm from `_expire_stale_pairing` turns *this* test
      red. The row's deadline is an hour out, so the deadline arm cannot reach
      it and the orphan arm is the only thing that can close it.
    - Collapsing the two arms into the single "any non-terminal row with no
      in-memory window behind it" form leaves this test **green** — the row it
      closes is one it would have closed anyway — and turns
      `tests/test_whatsapp_pairing_poll.py::TestTheClaim::
      test_a_fresh_request_is_serviced` red instead, because that predicate
      expires a fresh `requested` row before anything can service it. That
      mistake does not break this test; it breaks the feature.
    """

    async def test_the_poll_closes_the_row_and_unbricks_pairing(
        self, config, fresh_bridge, relay_file
    ):
        window_id = arrange_orphaned_window(config.db_path, relay_file)
        assert relay_file.exists()
        assert fresh_bridge.pairing_window is None
        assert fresh_bridge.last_pairing_outcome is None

        await asyncio.to_thread(poll_pairing_request, config)

        with db.get_db(config.db_path) as conn:
            row = db.read_whatsapp_pairing(conn)
        # 1. The row is closed, and closed as `failed` rather than `expired`:
        #    the credential is already moved aside and an operator has to be
        #    told which of two directories to trust.
        assert row["state"] == db.WHATSAPP_PAIRING_FAILED
        assert row["window_id"] == window_id
        # 2. The relay is unlinked, so nothing keeps rendering a frozen code
        #    for a window that no longer exists.
        assert not relay_file.exists()
        # 3. And this is the assertion the durable expiry exists for: without
        #    it every later pairing request is refused for the life of the
        #    deployment.
        with db.get_db(config.db_path) as conn:
            assert db.request_whatsapp_pairing(conn, USER) is not None

    async def test_the_orphan_message_names_the_restart_and_the_archive(
        self, config, fresh_bridge, relay_file
    ):
        """The two closures are not the same outcome and do not share a
        message. The archived path is carried forward from the row the dead
        process wrote, because a later process has no `PairingResult` and no way
        to know which `.old-<timestamp>` sibling this attempt made."""
        arrange_orphaned_window(config.db_path, relay_file)

        await asyncio.to_thread(poll_pairing_request, config)

        with db.get_db(config.db_path) as conn:
            row = db.read_whatsapp_pairing(conn)
        assert "restarted" in row["message"]
        assert "/srv/session.old-20260101" in row["message"]

    async def test_a_window_this_process_still_holds_is_left_alone(
        self, config, fresh_bridge, relay_file
    ):
        """The orphan arm keys on there being no *live* window behind the row.
        A window this process holds is the watchdog's to close."""
        window = await fresh_bridge.open_pairing_window(USER)
        assert window is not None
        with db.get_db(config.db_path) as conn:
            request_id = db.request_whatsapp_pairing(conn, USER, window_seconds=3600)
            assert db.record_whatsapp_pairing_state(
                conn, request_id,
                db.WHATSAPP_PAIRING_AWAITING_SIDECAR,
                adopt_window_id=window.window_id,
                expires_at=window.expires_at_wall,
            )

        await asyncio.to_thread(poll_pairing_request, config)

        with db.get_db(config.db_path) as conn:
            row = db.read_whatsapp_pairing(conn)
        assert row["state"] not in db.WHATSAPP_PAIRING_TERMINAL_STATES
        assert fresh_bridge.pairing_window is not None

    async def test_a_window_this_process_closed_itself_is_not_an_orphan(
        self, config, fresh_bridge, sidecar, relay_file
    ):
        """`last_pairing_outcome` is why this is possible at all: a *paired*
        close and a process that died holding a window both present as
        `pairing_window is None`, so without it a successful pairing would be
        swept up and recorded `failed`."""
        window = await fresh_bridge.open_pairing_window(USER)
        assert window is not None
        with db.get_db(config.db_path) as conn:
            request_id = db.request_whatsapp_pairing(conn, USER, window_seconds=3600)
            assert db.record_whatsapp_pairing_state(
                conn, request_id,
                db.WHATSAPP_PAIRING_AWAITING_SCAN,
                adopt_window_id=window.window_id,
                expires_at=window.expires_at_wall,
            )
        # Close it the way a scan does: a relayed code, then `ready` from the
        # sidecar. Driven over the socket rather than by poking the window,
        # because `_dispatch`'s `ready` arm is what records the outcome — and a
        # `ready` with no code behind it closes the window `failed`.
        await sidecar.say(proto.MSG_QR, qr="2@SENTINELqrPAYLOAD/9x+abcDEF==")
        await wait_for(lambda: relay_file.exists())
        await sidecar.say(proto.MSG_READY)
        await wait_for(lambda: fresh_bridge.pairing_window is None)
        outcome = fresh_bridge.last_pairing_outcome
        assert outcome is not None and outcome.state == pairing_relay.STATE_PAIRED

        await asyncio.to_thread(poll_pairing_request, config)

        with db.get_db(config.db_path) as conn:
            row = db.read_whatsapp_pairing(conn)
        assert row["state"] == db.WHATSAPP_PAIRING_PAIRED
