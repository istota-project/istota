"""Reading the framework DB back, from wherever it happens to live.

Two access modes, because the two callers genuinely differ. A local path is
direct `sqlite3` and is what a host-side or bind-mounted deployment gets — the
in-process wire suite reads that way, with no container at all. The lean stack
keeps its DB on a named volume (so the upgrade tier can retain it across an
upgrade), and a named volume has no host path, so those rows come back through
`docker compose exec`.

The remote reader is `python -c`, not the `sqlite3` CLI. `sqlite3` is not
installed in the shipped image and adding it would mean changing the artifact in
order to test it; Python with the stdlib `sqlite3` module is guaranteed, because
the daemon is written in it.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_PATH = "/data/db/istota.db"
QUERY_TIMEOUT = 60
POLL_INTERVAL = 0.5

#: Tables the watermark covers. Every one is keyed by an integer `id`, which is
#: what makes `MAX(id)` a usable high-water mark at all.
#:
#: Two deliberate absences. `rooms` is keyed by `token TEXT PRIMARY KEY`
#: (`schema.sql:797`), so it has no `id` to take a maximum of and a scenario
#: asserting "no room was created" has to scope by token instead.
#: `trusted_email_senders` is *reset* rather than watermarked: a test that
#: trusts a sender does not add a row a later test can filter past, it changes
#: what every later scenario means, and a watermark on it would invite exactly
#: the assertion shape that hides that.
#:
#: `notifications` has a caveat the others do not: a row that is *bumped* or
#: *reopened* keeps its id, so a watermark finds only rows created after it.
#: A scenario reads a fresh object's row (a new task, a new draft) by its dedup
#: key, and "no push happened" is read off the ntfy stub, not off this table.
WATERMARK_TABLES = (
    "tasks",
    "task_logs",
    "task_events",
    "processed_emails",
    "sent_emails",
    "messages",
    "notifications",
    "outbound_drafts",
    "room_participants",
)

#: The `surface_ref` of a user's private email room, restated rather than
#: imported, since this package imports nothing from `istota`:
#: `transport.email.private_room.email_conversation_token`. A guard in the
#: default suite holds the two equal.
PRIVATE_EMAIL_REF_PREFIX = "email-"


def private_email_ref(user_id: str) -> str:
    """`email_conversation_token(user_id)`, restated."""
    digest = hashlib.sha256(f"istota-email-v1\0{user_id}".encode()).hexdigest()[:24]
    return PRIVATE_EMAIL_REF_PREFIX + digest


#: The reference an email note's room row carries
#: (`rooms.private_replies.deliver_email_note`).
def email_note_reference(task_id: int) -> str:
    return f"private-pass_on:{task_id}:pass-on"


def _message_id_forms(message_id: str) -> list[str]:
    """A Message-ID as it was sent and in the other bracket form.

    The mail driver returns `<id@host>`; a column may hold either, and a probe
    that guessed wrong would read "no row" for a row that is there.
    """
    bare = message_id.strip().strip("<>")
    return [f"<{bare}>", bare]


def _decoded(row: dict, *columns: str) -> dict:
    """`row` with each JSON column parsed; an unparseable value stays a string."""
    out = dict(row)
    for column in columns:
        value = out.get(column)
        if isinstance(value, str):
            try:
                out[column] = json.loads(value)
            except ValueError:
                pass
    return out

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class ExecStats:
    """How much of a session went into `docker compose exec`.

    Open question 4 in the deployment-testbed spec: every remote `query()`
    shells out, and at session scope one stack serves every test, each polling
    `wait_for_task`. The answer decides whether a long-lived reader process is
    worth building — so the counters exist to *measure*, and nothing here
    optimizes on them.
    """

    calls: int = 0
    seconds: float = 0.0


_EXEC_STATS = ExecStats()
_EXEC_LOCK = threading.Lock()


def exec_stats() -> ExecStats:
    """A snapshot of the process-wide `docker compose exec` totals."""
    with _EXEC_LOCK:
        return ExecStats(calls=_EXEC_STATS.calls, seconds=_EXEC_STATS.seconds)


def reset_exec_stats() -> None:
    """Zero the totals, for a caller measuring one span."""
    with _EXEC_LOCK:
        _EXEC_STATS.calls = 0
        _EXEC_STATS.seconds = 0.0


@contextmanager
def counted_exec():
    """Count one `docker compose exec`, however it turned out.

    Used by `Probe.query` and by `Stack.exec`, because both are the thing
    Open question 4 asks about. Counting only the probe's polling would report
    a fraction and print it under a label that says `docker compose exec` —
    `Stack.exec` carries `submit`, `doctor`, the framework-state write and the
    container-state clearing, and several of those run once per test.

    The timing is taken in a `finally`, so a call that raised or timed out
    still counts. It is the slow ones the measurement is about, and dropping
    them would bias the total in the one direction that matters.

    Lives here rather than in `stack.py` because `stack` imports `probe` and
    not the other way round.
    """
    started = time.monotonic()
    try:
        yield
    finally:
        with _EXEC_LOCK:
            _EXEC_STATS.calls += 1
            _EXEC_STATS.seconds += time.monotonic() - started


# Read-only, and via a URI so SQLite enforces it. The daemon is writing this
# file concurrently; a probe that took a write lock could stall the thing it is
# observing and turn an assertion into a timeout somewhere else entirely.
_REMOTE_READER = """
import json, sqlite3, sys
connection = sqlite3.connect("file:%s?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
rows = connection.execute(sys.argv[1], json.loads(sys.argv[2])).fetchall()
print(json.dumps([dict(row) for row in rows]))
"""


@dataclass
class Probe:
    """Assertions against a framework DB, local or inside a container."""

    db_path: str = DEFAULT_DB_PATH
    local: Path | None = None
    compose_args: list[str] | None = None
    service: str = "istota"

    def __post_init__(self) -> None:
        if self.local is None and self.compose_args is None:
            raise ValueError("Probe needs either a local path or compose args")

    def query(self, sql: str, params: list | None = None) -> list[dict]:
        params = params or []
        if self.local is not None:
            connection = sqlite3.connect(f"file:{self.local}?mode=ro", uri=True)
            try:
                connection.row_factory = sqlite3.Row
                return [dict(row) for row in connection.execute(sql, params).fetchall()]
            finally:
                connection.close()

        with counted_exec():
            result = subprocess.run(
                (self.compose_args or [])
                + [
                    "exec",
                    "-T",
                    self.service,
                    # As the daemon's uid: an exec does not inherit the root
                    # phase's drop (see `Stack.exec`).
                    "istota-drop",
                    "python",
                    "-c",
                    _REMOTE_READER % self.db_path,
                    sql,
                    json.dumps(params),
                ],
                capture_output=True,
                text=True,
                timeout=QUERY_TIMEOUT,
            )
        if result.returncode != 0:
            raise RuntimeError(
                f"probe query failed ({result.returncode})\n{sql}\n"
                f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
            )
        return json.loads(result.stdout or "[]")

    def tasks(
        self,
        *,
        task_id: int | None = None,
        user_id: str | None = None,
        status: str | None = None,
        source_type: str | None = None,
        conversation_token: str | None = None,
        id_above: int | None = None,
    ) -> list[dict]:
        """Rows from `tasks`, narrowed by whichever filters are given.

        `task_id` exists because the others are not selective enough on a
        running daemon: the scheduler queues work of its own for the same user
        at startup (a feeds poll, a sleep cycle), so `user_id=` alone returns
        whichever task finished first — which is how the smoke tests first came
        back asserting against `source_type='scheduled'`.

        `conversation_token` is what a scenario has instead of a task id when
        the daemon made the task rather than the test: a Talk message produces
        a row nobody handed the test a handle for, and the room token is the
        only thing that discriminates it from the pollers' own work. It is as
        selective as `task_id` for that case, because a room this test created
        is a room nothing else has ever posted in.

        `id_above` is for the case with neither — inbound *email*, where the
        daemon makes the task and there is no room token to discriminate on.
        `source_type='email'` alone matches every earlier scenario's row on a
        session-scoped stack, and `wait_for_task` returns the first terminal one
        it sees, so it would answer with the previous test's task. Paired with
        the reset's watermark this is as selective as an id, and it is the same
        both-halves rule `rows_above` enforces one table over.
        """
        clauses, params = [], []
        for column, value in (
            ("id", task_id),
            ("user_id", user_id),
            ("status", status),
            ("source_type", source_type),
            ("conversation_token", conversation_token),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if id_above is not None:
            clauses.append("id > ?")
            params.append(id_above)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.query(f"SELECT * FROM tasks{where} ORDER BY id", params)

    # -- the watermark ----------------------------------------------------

    def watermark(self) -> dict[str, int]:
        """`MAX(id)` per table, in one round trip.

        The primitive a *negative* assertion needs. Scoping to the id
        `submit()` returned covers positive assertions and nothing else:
        "no reply was sent" has no id, because it is the absence of a row
        nobody handed the test a handle for. Under a session-scoped stack the
        naive form is worse than useless — `sent_emails` is a framework table
        that nothing resets, so once one scenario has posted a reply it is
        permanently non-empty and every later "no reply was sent" is reading
        the previous test's rows. `processed_emails`, `messages` and
        `task_events` behave the same way.

        One query rather than one per table, deliberately: on the lean and full
        shapes every query is a `docker compose exec`, and `Stack.reset` calls
        this once per test.

        An empty table reads 0 rather than `None`, so `id > mark[...]` is
        always a valid comparison and no caller has to write the null case.
        """
        columns = ", ".join(
            f"(SELECT MAX(id) FROM {table}) AS {table}" for table in WATERMARK_TABLES
        )
        rows = self.query(f"SELECT {columns}")
        first = rows[0] if rows else {}
        return {table: (first.get(table) or 0) for table in WATERMARK_TABLES}

    def rows_above(
        self, table: str, mark: dict[str, int], **filters
    ) -> list[dict]:
        """Rows in `table` newer than `mark`, narrowed by a discriminating column.

        **Both halves are required, and that is why `filters` is not optional.**
        A watermark alone still catches an unrelated poller's row — the daemon
        runs eleven of them for the whole session, so a table gains rows during
        every test whether or not the test caused them. A column filter alone
        still catches the previous test's. Only the pair means "this test
        caused this". A caller with nothing to filter on is about to write an
        assertion that will be diagnosed as flake, so it is refused here rather
        than later.

        `source_type`, `conversation_token` and `to_addr` are the columns that
        discriminate in practice.
        """
        if table not in WATERMARK_TABLES:
            raise ValueError(
                f"{table!r} is not watermarked; WATERMARK_TABLES holds "
                f"{list(WATERMARK_TABLES)}"
            )
        if not filters:
            raise ValueError(
                f"rows_above({table!r}) needs at least one column filter. A "
                "watermark alone still matches rows a background poller made "
                "during this test, so the assertion would fail for reasons "
                "unrelated to what it is about."
            )
        clauses = ["id > ?"]
        params: list = [mark.get(table, 0)]
        for column, value in filters.items():
            if not _IDENTIFIER.match(column):
                raise ValueError(f"{column!r} is not a column name")
            clauses.append(f"{column} = ?")
            params.append(value)
        return self.query(
            f"SELECT * FROM {table} WHERE {' AND '.join(clauses)} ORDER BY id",
            params,
        )

    def task_logs(self, task_id: int) -> list[dict]:
        return self.query(
            "SELECT * FROM task_logs WHERE task_id = ? ORDER BY id", [task_id]
        )

    def task_log(self, task_id: int, *, contains: str) -> list[dict]:
        """The task's `task_logs` rows whose message contains `contains`.

        `instr` rather than `LIKE`, so a `%` or `_` in the text is literal. The
        one reader today is the email note: `scheduler._write_email_note` logs
        `Private note to the host: <outcome>` when it writes one, which is the
        only record of the computed outcome a lean test can read.
        """
        return self.query(
            "SELECT * FROM task_logs WHERE task_id = ? AND instr(message, ?) > 0 "
            "ORDER BY id",
            [task_id, contains],
        )

    # -- email: the mail, its room and what the room holds -----------------

    def processed(self, message_id: str) -> dict | None:
        """The newest `processed_emails` row for an inbound Message-ID.

        `mail_meta` and `recipients` come back decoded from JSON.
        """
        forms = _message_id_forms(message_id)
        rows = self.query(
            "SELECT * FROM processed_emails WHERE message_id IN (?, ?) "
            "ORDER BY id DESC LIMIT 1",
            forms,
        )
        return _decoded(rows[0], "mail_meta", "recipients") if rows else None

    def _canonical_room(self, token: str) -> str:
        """`db._canonical_room_token(conn, token, cross_surface=False)`, in SQL.

        A live room is itself; a permanent alias to a deleted room is a
        tombstone and stays as given; any other alias resolves to its target.
        """
        rows = self.query(
            "SELECT (SELECT 1 FROM rooms WHERE token = ?1) AS live, "
            "m.new_token AS new_token, r.token AS target "
            "FROM (SELECT 1) LEFT JOIN room_token_migration m ON m.old_token = ?1 "
            "LEFT JOIN rooms r ON r.token = m.new_token",
            [token],
        )
        row = rows[0] if rows else {}
        if row.get("live") or not row.get("new_token") or not row.get("target"):
            return token
        return row["new_token"]

    def _thread_room(self, token: str | None) -> str | None:
        """`threads.thread_binding`: the token when its room is a thread room.

        Refused when the room has no email binding, or when the binding's ref
        is the room creator's private email ref.
        """
        if not token:
            return None
        rows = self.query(
            "SELECT r.user_id AS owner, b.surface_ref AS ref FROM rooms r "
            "JOIN room_bindings b ON b.room_token = r.token AND b.surface = 'email' "
            "WHERE r.token = ?",
            [token],
        )
        if not rows or rows[0]["ref"] == private_email_ref(rows[0]["owner"] or ""):
            return None
        return token

    def email_room(self, message_id: str) -> str | None:
        """The thread room holding a mail, as `threads.find_thread_room` finds it.

        For one Message-ID: the room bound to it, else the room a stored mail
        with that id was filed under (`processed_emails.thread_id`, then
        `sent_emails.conversation_token`), each canonicalised and each refused
        when it is a private email room. None for a mail in no thread room,
        which for the host's own mail to the bot is the private email room
        (`private_email_room`).
        """
        for form in _message_id_forms(message_id):
            bound = self.query(
                "SELECT room_token FROM room_bindings "
                "WHERE surface = 'email' AND surface_ref = ?",
                [form],
            )
            if bound:
                return self._thread_room(self._canonical_room(bound[0]["room_token"]))
        # `_token_by_stored_mail`'s order: processed mail, then sent mail, the
        # newest row of each first.
        for table, column in (("processed_emails", "thread_id"),
                              ("sent_emails", "conversation_token")):
            stored = self.query(
                f"SELECT {column} AS token FROM {table} WHERE message_id IN (?, ?) "
                "ORDER BY id DESC",
                _message_id_forms(message_id),
            )
            for row in stored:
                if not row["token"]:
                    continue
                found = self._thread_room(self._canonical_room(row["token"]))
                if found is not None:
                    return found
        return None

    def private_email_room(self, user_id: str) -> str | None:
        """The canonical token of a user's private email room, or None."""
        ref = private_email_ref(user_id)
        bound = self.query(
            "SELECT room_token FROM room_bindings WHERE surface = 'email' "
            "AND surface_ref = ?",
            [ref],
        )
        token = self._canonical_room(bound[0]["room_token"] if bound else ref)
        live = self.query("SELECT 1 FROM rooms WHERE token = ?", [token])
        return token if live else None

    def participants(self, room_token: str) -> list[dict]:
        """The room's `room_participants` rows, present and departed."""
        return self.query(
            "SELECT id, surface, surface_ref, user_id, kind, display_name, "
            "joined_at, left_at FROM room_participants WHERE room_token = ? "
            "ORDER BY id",
            [room_token],
        )

    def room_messages(self, room_token: str, id_above: int | None = None) -> list[dict]:
        """The room's `messages` rows, with the three mail cards decoded."""
        clauses, params = ["room_token = ?"], [room_token]
        if id_above is not None:
            clauses.append("id > ?")
            params.append(id_above)
        rows = self.query(
            f"SELECT * FROM messages WHERE {' AND '.join(clauses)} ORDER BY id",
            params,
        )
        return [
            _decoded(row, "outgoing_mail", "received_mail", "email_note")
            for row in rows
        ]

    def email_note(self, task_id: int) -> dict | None:
        """The email note's room row for a task, in any room, or None.

        None on the lean shape, where nobody has a private room and the note
        is the `private-note:<task>` bell row (`notifications`).
        """
        rows = self.query(
            "SELECT * FROM messages WHERE delivery_reference = ? ORDER BY id LIMIT 1",
            [email_note_reference(task_id)],
        )
        return _decoded(rows[0], "email_note") if rows else None

    def notifications(
        self,
        user_id: str,
        *,
        source: str | None = None,
        dedup_key: str | None = None,
        id_above: int | None = None,
    ) -> list[dict]:
        """A user's `notifications` rows, `params` decoded.

        `dedup_key` is how a scenario finds its own row: `private-note:<task>`
        for a note in the bell, `task:<id>` for a park. See the caveat on
        `WATERMARK_TABLES` before reading `id_above` as "written by this test".
        """
        clauses, params = ["user_id = ?"], [user_id]
        for column, value in (("source", source), ("dedup_key", dedup_key)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if id_above is not None:
            clauses.append("id > ?")
            params.append(id_above)
        rows = self.query(
            f"SELECT * FROM notifications WHERE {' AND '.join(clauses)} ORDER BY id",
            params,
        )
        return [_decoded(row, "params") for row in rows]

    def drafts(self, user_id: str, *, id_above: int | None = None) -> list[dict]:
        """A user's `outbound_drafts` rows, the address lists decoded."""
        clauses, params = ["user_id = ?"], [user_id]
        if id_above is not None:
            clauses.append("id > ?")
            params.append(id_above)
        rows = self.query(
            f"SELECT * FROM outbound_drafts WHERE {' AND '.join(clauses)} ORDER BY id",
            params,
        )
        return [
            _decoded(row, "to_addrs", "cc_addrs", "bcc_addrs", "attachments")
            for row in rows
        ]

    def wait_for_task(self, *, status: str, timeout: float = 60, **filters) -> dict:
        """Block until one task reaches `status`, and return it.

        The terminal statuses are watched alongside the requested one so that
        waiting for `completed` on a task that already failed returns
        immediately with the failure, rather than spending the whole timeout and
        then reporting "no task reached completed" — which says nothing about
        why. The caller's assertion on `status` is what still fails.

        `pending_confirmation` is in that set even though it is a *suspended*
        rather than a finished state: the task is parked waiting for a human and
        will not move on its own, so treating it as non-terminal reintroduces
        exactly the full-timeout-with-no-explanation this exists to prevent. The
        full list is in AGENTS.md under "Task Status".
        """
        terminal = {status, "completed", "failed", "cancelled", "pending_confirmation"}
        deadline = time.monotonic() + timeout
        seen: list[dict] = []
        while time.monotonic() < deadline:
            seen = self.tasks(**filters)
            for task in seen:
                if task.get("status") in terminal:
                    return task
            time.sleep(POLL_INTERVAL)

        raise TimeoutError(
            f"no task reached {status!r} within {timeout}s; "
            f"saw {[(t.get('id'), t.get('status')) for t in seen]}"
        )
