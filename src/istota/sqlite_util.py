"""One SQLite open, with every caller's pragma set expressed as parameters.

Fourteen helpers in this tree opened a connection and then issued some subset
of the same four pragmas, and the five-line explanation of *why* ``journal_mode``
is not among them was pasted into four of them verbatim. This is that open,
once, with the pragmas as arguments so each caller's set stays visible at the
call site rather than being buried in a body a reader has to go and compare.

**There is no ``journal_mode`` parameter, and adding one would be a defect.**
WAL is persistent in the SQLite file header, so it is issued once by each
store's ``init_db`` and never re-issued per connection: re-issuing takes a
write lock that races sibling readers, which is the recorded cause of a
dispatch-loop stall. ``money/config_store``'s ``init`` says so in its own
comment and has twenty-odd ``with _connect(...)`` sites behind it. A
``journal_mode`` argument here — or a default that set WAL — would reintroduce
that across all of them, and nothing in the suite would go red.

**``timeout`` already is a busy timeout.** Python's ``sqlite3.connect(timeout=T)``
calls ``sqlite3_busy_timeout(T * 1000)``, so a connection opened with
``timeout=30.0`` reads back ``PRAGMA busy_timeout = 30000`` having issued no
pragma at all. ``busy_timeout_ms`` is therefore an *override* of that value,
not the thing that supplies it — which is why passing ``None`` is not the same
as "no busy timeout", and why a test asserting ``busy_timeout == 30000`` on a
``timeout=30.0`` connection proves nothing about whether the pragma ran.

Stdlib-only leaf: ``sqlite3``, ``pathlib``, ``contextlib``, ``os`` and
``urllib.parse``. Imports nothing from the package, so a module DB helper or a
skill subprocess can reach it.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

__all__ = ["add_columns", "connect", "connect_read_only", "open_db"]


def connect(
    path: Path | str,
    *,
    timeout: float = 30.0,
    row_factory: bool = True,
    busy_timeout_ms: int | None = 30_000,
    foreign_keys: bool = True,
    synchronous: str | None = None,
) -> sqlite3.Connection:
    """Open a connection and apply the requested pragmas. Caller closes it.

    The bare form, for the two callers that hand a live connection back to
    something else rather than wrapping a block: ``money/cli._get_db_conn`` and
    ``money/routes._portfolio_conn``. Everything else wants :func:`open_db`.

    ``busy_timeout_ms=None`` issues no ``PRAGMA busy_timeout``, which leaves the
    handler ``timeout`` already installed — see the module docstring.
    ``synchronous`` is a per-connection setting (unlike ``journal_mode``) and is
    passed as the literal SQLite keyword, e.g. ``"NORMAL"``.
    """
    conn = sqlite3.connect(str(path), timeout=timeout)
    try:
        if busy_timeout_ms is not None:
            conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        if foreign_keys:
            conn.execute("PRAGMA foreign_keys = ON")
        if synchronous is not None:
            conn.execute(f"PRAGMA synchronous = {synchronous}")
        if row_factory:
            conn.row_factory = sqlite3.Row
    except BaseException:
        # A pragma that raised leaves a connection nobody holds a name for.
        conn.close()
        raise
    return conn


def _has_hot_journal(path: Path | str) -> bool:
    """Whether ``path`` has WAL or rollback-journal content to recover.

    The question :func:`connect_read_only` asks before choosing its mode, and it
    is asked of the filesystem because SQLite has no way to answer it without
    opening — and opening read-write is the act being decided.

    A non-empty ``-wal`` or ``-journal`` beside the database means either a
    process is holding it open right now or one died without checkpointing.
    Both are cases where a read-write open would *write*: recovery, then a
    checkpoint into the main file on last close. Zero-length counts as cold,
    which is what a cleanly closed WAL database leaves behind on some platforms.

    Racy by construction and safe in both directions, which is why a check this
    weak is worth having: the file can appear or vanish between this call and
    the open, and the worst either way is one run behaving as the other branch
    would have — a checkpoint that had nothing to do, or a sidecar pair left
    beside a database that already had one. Neither is new behaviour.
    """
    base = Path(path)
    for suffix in ("-wal", "-journal"):
        sidecar = base.with_name(base.name + suffix)
        try:
            if sidecar.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


def connect_read_only(path: Path | str) -> sqlite3.Connection:
    """Open ``path`` for reading without writing to it. Caller closes it.

    ``doctor`` is the only caller, five times, and what it needs is a connection
    that does not change what it is inspecting and strands nothing beside it.

    **The mode is chosen per database, and neither mode is right for both
    shapes** (ISSUE-458). A database with a hot journal is opened ``mode=ro``; a
    database with none is opened ``mode=rw`` with ``PRAGMA query_only``. Both
    halves are counter-intuitive and both were measured, against sqlite 3.47.

    **Why ``mode=ro`` is wrong for a cold database.** It is the obvious spelling
    and it cannot clean up after itself: opening a WAL database ``mode=ro``
    *creates* the ``-wal`` / ``-shm`` sidecars and then leaves them, because
    deleting them on last close is itself a write. The read-write open the five
    call sites were avoiding is the one that removes them, so their comments had
    the mechanism exactly backwards. The strays were an outage rather than
    clutter: ``sudo istota doctor`` against a stopped daemon left both owned by
    root while the daemon runs as its own user, and a database whose sidecars
    the caller cannot write refuses a read-write open outright — the diagnostic
    locked the daemon out of its own database.

    **Why ``mode=rw`` is wrong for a hot one.** A read-write connection that is
    the *last* one out checkpoints an un-checkpointed WAL into the main database
    file and unlinks the sidecars, and ``query_only`` does not stop it — that
    pragma bounds statements, not SQLite's own close-time housekeeping. Measured
    against a database left by a ``SIGKILL``ed writer, the main file went from
    4096 to 28672 bytes with its mtime moved. That database is exactly what
    :func:`~istota.doctor.check_framework_db` exists to inspect, and its remedy
    is ``python -m istota.db_restore``, so recovering and rewriting it before
    the operator has decided anything is a diagnostic altering the evidence.
    Hence :func:`_has_hot_journal`, and hence the read-only branch — which
    strands nothing, because a database with a hot journal already has its
    sidecars.

    **``immutable=1`` is the only URI that creates no sidecars, and it is
    unusable here.** It tells SQLite the file never changes, so the WAL is
    ignored entirely: against a database whose table has not been checkpointed
    out of the WAL yet it reports ``no such table`` rather than stale rows, and
    all five checks would then call a healthy running daemon broken. Doctor runs
    inside the daemon at boot, on the scheduler's interval and behind the admin
    dashboard, so a live database is its ordinary case rather than its edge one.
    That is the question ISSUE-458 posed — what a diagnostic may assume of a
    database another process is using — and the answer is nothing.

    **``query_only`` is weaker than ``mode=ro`` and strong enough.** It refuses
    ``INSERT``, ``CREATE``, ``DROP`` and a ``PRAGMA`` write with SQLite's own
    ``attempt to write a readonly database``, so a check that grew a write fails
    loudly at the statement — but the pragma is reversible by SQL, where
    ``mode=ro`` was structural. That is a guard against a mistake rather than a
    boundary, which is the right size here: all five call sites execute fixed
    SQL this package writes, none of it model-supplied. It must also stay
    **header-free**: it is the first statement the connection runs, and a pragma
    that touched the database header would move a corrupt-file failure off
    ``check_framework_db``'s body branch and onto its "could not be opened" one.
    ``PRAGMA query_only`` does not read a page, which is measured and pinned.

    Two properties of ``mode=ro`` are deliberately kept on the read-write
    branch. ``rw`` rather than the default ``rwc`` means a **missing database
    still raises rather than being created** — ``_secret_row_count`` names that
    zero-byte file as something that later reads as corruption rather than as
    absence, and four of the five call sites guard it by hand as well, so the
    structural form is what keeps them agreeing. And a read-only *directory*
    fails exactly as it did before, since ``mode=ro`` could not create the
    sidecars it needs there either.

    **Three residuals, none of them closed by this.** ``mode=rw`` is not a
    promise the handle is writable: SQLite retries read-only when ``O_RDWR`` is
    refused, so against a database file the process can read and not write the
    open succeeds and the sidecars are created and left exactly as before — the
    likelier permission shape of the two, and the reason that branch is a
    best-effort cleanup rather than a guarantee. The sidecars also exist *for
    the duration* of every read-write open, so a ``SIGKILL`` or an OOM between
    open and close strands them again; every call site closes in a ``finally``,
    which covers an exception but not process death. And the branch is chosen
    from a filesystem check that can be stale by the time SQLite opens the file.
    None of the three is worse than the behaviour this replaced.

    **The path is percent-encoded into the URI, and it has to be** (ISSUE-461).
    All five call sites interpolated it raw, so `?` or `#` ended the path early
    and `%41` decoded to `A`. The reported symptom was reading the wrong file;
    the worse one is that the truncated remainder is not a ``mode`` parameter
    SQLite recognises, so the mode was dropped with it and the open fell back to
    ``rwc`` on a path the caller never named — measured, and it materialized
    that file. The encoding is what stops that, and it carries at least as much
    weight under ``rw`` as it did under ``ro``.

    ``os.fsencode`` rather than ``str``: the encode goes through the
    filesystem encoding, so a name carrying undecodable bytes round-trips
    instead of raising. ``safe="/"`` keeps the separators and encodes the rest,
    which is inert for a path of ordinary characters — a space or a non-ASCII
    name worked before and still does, since SQLite decodes ``%HH`` back.
    """
    mode = "ro" if _has_hot_journal(path) else "rw"
    conn = sqlite3.connect(
        "file:" + quote(os.fsencode(Path(path)), safe="/") + f"?mode={mode}",
        uri=True,
    )
    if mode == "ro":
        return conn
    try:
        conn.execute("PRAGMA query_only = ON")
    except BaseException:
        # Without the pragma this is an ordinary writable connection, which is
        # the one thing the caller must not be handed.
        conn.close()
        raise
    return conn


@contextmanager
def open_db(
    path: Path | str,
    *,
    timeout: float = 30.0,
    row_factory: bool = True,
    busy_timeout_ms: int | None = 30_000,
    foreign_keys: bool = True,
    synchronous: str | None = None,
    commit: bool = False,
    rollback_on_error: bool = False,
) -> Iterator[sqlite3.Connection]:
    """:func:`connect`, plus the close/commit/rollback block around it.

    ``commit`` commits on a clean exit; ``rollback_on_error`` rolls back before
    re-raising. The two are independent because the callers are: the framework
    stores commit and do not roll back, the module ``connect`` helpers do
    neither, and only the money pair does both.

    ``rollback_on_error`` catches ``Exception``, not ``BaseException``, which is
    exactly what both money callers had: on a ``KeyboardInterrupt`` or a
    ``GeneratorExit`` the ``finally`` below closes the connection and SQLite
    rolls back implicitly, so an explicit ``rollback()`` there buys nothing and
    can raise ``ProgrammingError`` over the in-flight exception if the body
    closed the connection itself. The ``finally`` is the real guarantee.
    """
    conn = connect(
        path,
        timeout=timeout,
        row_factory=row_factory,
        busy_timeout_ms=busy_timeout_ms,
        foreign_keys=foreign_keys,
        synchronous=synchronous,
    )
    try:
        yield conn
        if commit:
            conn.commit()
    except Exception:
        if rollback_on_error:
            conn.rollback()
        raise
    finally:
        conn.close()


def add_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
    *,
    commit: bool = False,
    tolerate_errors: bool = False,
) -> list[str]:
    """Add each missing column to ``table``, tolerating a rival that wins one.

    ``columns`` maps a column name to the rest of its ``ADD COLUMN`` clause —
    ``{"brain": "TEXT", "once": "INTEGER DEFAULT 0"}``. Returns the names this
    call actually added, in the order it added them, so a caller with follow-up
    work gated on a column being new (an index to rebuild, a one-shot backfill)
    can ask rather than re-reading the schema.

    **The race is the reason this exists.** ``ensure_schema`` and ``init_db``
    run on every money web request, scheduler cron and skill invocation, so the
    first post-upgrade moment is routinely several connections at once.
    Check-then-ALTER lets both see the column absent and the loser raise
    ``duplicate column name`` — a *schema* error, so the 30s busy handler does
    not help — surfacing as a one-off 500 or a failed task. So the ``ALTER`` is
    re-checked on the way out: the column being present now is the race and
    nothing else, and the loser returns having done its job.

    **A table that does not exist yet is skipped, not an error**, and that is a
    second condition rather than a nicety. ``db._run_migrations`` runs *before*
    ``schema.sql``, so on a first boot every table it names is absent; its bare
    ``except sqlite3.OperationalError: pass`` was carrying that case and the
    duplicate-column one with one handler, and a helper that guarded only the
    column would break first-boot ordering with nothing in the suite to catch
    it. ``PRAGMA table_info`` on a missing table yields no rows, which would
    otherwise read as "column absent" and point the ``ALTER`` at nothing.

    The skip is **unconditional** — it does not consult ``tolerate_errors`` —
    which is a widening for the module-database callers, whose check-then-ALTER
    would have raised ``no such table``. It is inert at all of them and it is
    worth knowing why rather than assuming: ``health``, ``location`` and
    ``money`` run their migrations *after* ``executescript``, so the table is
    always there, and ``feeds._read_schema_version`` returns the current
    version when ``feed_entries`` is absent, so its chain does not run at all.
    A caller that could genuinely meet a missing table wants an argument here,
    not this default.

    ``tolerate_errors`` swallows an ``OperationalError`` that is neither of
    those — a lock, in practice. It exists because that is what
    ``db._run_migrations``' bare handler did at every one of its sites, and
    this stage is a consolidation rather than a change of failure mode; the
    argument for why degrading there is safe is per-table and is written at the
    ``user_profiles`` block, not re-made here. **Do not pass it where a missing
    column raises at read time** — ``outbound_drafts.reply_to`` is the site
    that says so, and it keeps the default, because there a swallowed lock
    leaves every draft read raising ``IndexError`` and stops all outbound mail
    on the instance.

    ``commit`` commits after each column it adds, which is what ``money``'s
    ``_alter_once`` did for its two callers. It is off by default because
    ``db._run_migrations`` owns its own transaction boundaries and states the
    contract for a migration that wants one.

    ``table`` and the column names and clauses are interpolated into DDL, which
    SQLite gives no way to parameterize. Every caller passes a code literal, so
    this is a latent sharp edge rather than a live one — the one
    :func:`connect_read_only` used to carry for its URI, which ISSUE-461 closed
    — and narrowing it is a change with its own argument to make. The two are
    not the same problem: a URI has an encoding, and SQLite DDL has none, so
    the answer here is a caller rule rather than a quote function. ``PRAGMA
    table_info`` is read positionally because both connection
    shapes reach here: ``feeds`` migrates under a ``sqlite3.Row`` connection
    and ``health``, ``location`` and ``db.init_db``'s own pass do not.
    """
    added: list[str] = []
    try:
        present = _column_names(conn, table)
    except sqlite3.OperationalError:
        if tolerate_errors:
            return added
        raise
    if not present:
        return added  # Table not created yet — see the docstring.

    for name, clause in columns.items():
        if name in present:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {clause}")
        except sqlite3.OperationalError:
            try:
                lost_the_race = name in _column_names(conn, table)
            except sqlite3.OperationalError:
                lost_the_race = False
            if not lost_the_race and not tolerate_errors:
                raise
            continue
        added.append(name)
        if commit:
            conn.commit()
    return added


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    # (cid, name, type, notnull, dflt_value, pk) — indexed by position, not by
    # name, because half the callers migrate under a row factory and half do not.
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
