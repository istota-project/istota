"""What an owner approved for a task's network reach, and what it tried and could not do.

`rooms.scopes.reach_for_task` reads this once per task to fill `Reach.egress_hosts`.
A subject is what a grant hangs on: a cron job, a personal briefing, an assignment
or TASKS.md. A row in `egress_grants` means approved, and there is no pending or
denied state. `egress_refused` is a list of things the owner may want to add, and
grants nothing.

**Only an authenticated person writes a grant.** `add_grant`'s callers are the web
route, the operator CLI and the one-time seed; no skill CLI and no deferred handler
imports this module (`tests/test_egress_grants.py`). CRON.md and prompt files are
model-writable, so `open:` and `public:` name a digest of the job's exact text, and
a row whose text has moved no longer matches.

No `istota.db` import: `db._migrate_egress_seed_open` calls in here, and the module
sits below it.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from istota import db

logger = logging.getLogger(__name__)

#: `assignments.wakes.SOURCE_TYPE`, which imports the database layer this module
#: sits below. `tests/test_egress_reach.py` uses the real name.
_ASSIGNMENT_SOURCE_TYPE = "assignment"

TASKS_FILE_SUBJECT = "tasks-file"
#: What a job occurrence copies from its row, and so what it can be compared on.
TASK_TEXT_COLUMNS = ("prompt", "command", "skill", "skill_args")
DIGEST_COLUMNS = TASK_TEXT_COLUMNS
DECIDERS = frozenset({"web", "cli", "migration"})
POSTURE_PUBLIC = "public"
POSTURE_PRIVATE = "private"
#: Refusal entries that ask for a decision about a job, rather than name a host.
JOB_REQUESTS = ("open", "public")
MAX_REFUSED_PER_SUBJECT = 20
#: `egress-refused.jsonl` channels whose entry is a name somebody else can choose.
_HOST_CHANNELS = frozenset({"web_fetch", "connect"})
_MAX_HOST_CHARS = 253

_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_SKILL_RE = re.compile(r"[a-z0-9_-]{1,64}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_NUMERIC_LABEL_RE = re.compile(r"[0-9]+|0x[0-9a-f]*")


class EntryRefused(ValueError):
    """An entry, subject or decider no grant may hold. The text is for the person."""


class StaleJob(EntryRefused):
    """The job's text is not the text the person was shown."""


@dataclass(frozen=True)
class Entry:
    kind: str  # 'host' | 'skill' | 'public' | 'open'
    value: str

    @property
    def text(self) -> str:
        return self.value if self.kind == "host" else f"{self.kind}:{self.value}"


def parse_entry(text: object) -> Entry:
    """One grant entry in its normal form, or `EntryRefused`.

    Hosts are exact: at least two labels, no scheme, path, port, wildcard or IP
    literal. So a grant can never cover a subdomain, which is an exfiltration
    channel of its own, and no public-suffix list is needed.
    """
    if not isinstance(text, str):
        raise EntryRefused("An entry must be text.")
    stripped = text.strip()
    if not stripped:
        raise EntryRefused("An entry cannot be empty.")
    if stripped.startswith("skill:"):
        name = stripped[len("skill:"):]
        if not _SKILL_RE.fullmatch(name):
            raise EntryRefused("A skill entry is skill:<name>, in lowercase.")
        return Entry("skill", name)
    for kind in JOB_REQUESTS:
        if stripped.startswith(f"{kind}:"):
            digest = stripped[len(kind) + 1:]
            if not _DIGEST_RE.fullmatch(digest):
                raise EntryRefused(f"An {kind} entry names the digest of a job's text.")
            return Entry(kind, digest)
    return Entry("host", _normal_host(stripped))


def _normal_host(text: str) -> str:
    host = text.lower()
    if len(host) > _MAX_HOST_CHARS:
        raise EntryRefused("That host name is too long.")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        raise EntryRefused("A host is a name, not an IP address.")
    labels = host.split(".")
    if len(labels) < 2:
        raise EntryRefused("A host needs at least two labels, such as example.com.")
    if not all(_LABEL_RE.fullmatch(label) for label in labels):
        raise EntryRefused(
            "A host is an exact name with no scheme, path, port or wildcard, such as "
            "news.example.com."
        )
    # A resolver reads a name ending in a number as an address: 127.1, 0x7f.0.0.1.
    if _NUMERIC_LABEL_RE.fullmatch(labels[-1]):
        raise EntryRefused("A host is a name, not an IP address.")
    return host


def job_digest(row: "Mapping | sqlite3.Row") -> str:
    """The sha256 of what a job dispatches, length-prefixed so text cannot move
    between fields. A schedule, model or delivery edit does not change it.

    NULL and empty read the same: an older row can hold `''` where the sync writes
    NULL, and reading that as an edit would drop a seeded approval on the first tick.
    """
    return _digest_over(row, DIGEST_COLUMNS)


def _digest_over(row: "Mapping | sqlite3.Row", columns: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for column in columns:
        value = row[column]
        data = ("" if value is None else str(value)).encode("utf-8", "surrogatepass")
        digest.update(f"{len(data)}:".encode())
        digest.update(data)
    return digest.hexdigest()


def cron_subject(job_name: str) -> str:
    return f"cron:{job_name}"


def job_row(conn: sqlite3.Connection, task: "db.Task") -> sqlite3.Row | None:
    """The user's own job row behind a scheduled task, or None."""
    if task.source_type != "scheduled" or task.scheduled_job_id is None:
        return None
    return conn.execute(
        "SELECT name, reach, prompt, command, skill, skill_args FROM scheduled_jobs "
        "WHERE id = ? AND user_id = ?",
        (task.scheduled_job_id, task.user_id),
    ).fetchone()


def subject_for(conn: sqlite3.Connection, root: "db.Task") -> str | None:
    """What grants hang on for this root task, or None where nothing can."""
    if root.source_type == "scheduled":
        row = job_row(conn, root)
        return cron_subject(row["name"]) if row is not None else None
    if root.source_type == "briefing":
        return f"briefing:{root.briefing_name}" if root.briefing_name else None
    if root.source_type == _ASSIGNMENT_SOURCE_TYPE:
        return f"assignment:{root.assignment_id}" if root.assignment_id is not None else None
    if root.source_type == "istota_file":
        return TASKS_FILE_SUBJECT
    return None


def _valid_subject(subject: object) -> bool:
    if not isinstance(subject, str):
        return False
    if subject == TASKS_FILE_SUBJECT:
        return True
    kind, _, rest = subject.partition(":")
    if kind == "assignment":
        return rest.isascii() and rest.isdigit()
    return kind in ("cron", "briefing") and bool(rest)


def grants_for(conn: sqlite3.Connection, user_id: str, subject: str | None) -> frozenset[str]:
    """Every stored entry for a subject, as written. Raises what the read raises."""
    if subject is None:
        return frozenset()
    rows = conn.execute(
        "SELECT entry FROM egress_grants WHERE user_id = ? AND subject = ?",
        (user_id, subject),
    ).fetchall()
    return frozenset(row[0] for row in rows)


def _entries_of_kind(grants: frozenset[str], kind: str) -> frozenset[str]:
    """The values of one kind. A row written by hand that does not parse grants nothing."""
    values = set()
    for text in grants:
        try:
            entry = parse_entry(text)
        except EntryRefused:
            logger.warning("egress_grant_malformed: dropped on read")
            continue
        if entry.kind == kind:
            values.add(entry.value)
    return frozenset(values)


def approved_hosts(conn: sqlite3.Connection, user_id: str, subject: str | None) -> frozenset[str]:
    return _entries_of_kind(grants_for(conn, user_id, subject), "host")


def approved_skills(conn: sqlite3.Connection, user_id: str, subject: str | None) -> frozenset[str]:
    return _entries_of_kind(grants_for(conn, user_id, subject), "skill")


def job_approved(conn: sqlite3.Connection, root: "db.Task", kind: str) -> bool:
    """Whether this scheduled root's job, as its row reads now, has an approved
    `open:` or `public:` for that exact text, and the root is running that text. A
    digest that moved answers False, so a race with the sync cannot widen anything."""
    return _row_approved(conn, root, job_row(conn, root), kind)


def public_job_approved(conn: sqlite3.Connection, root: "db.Task") -> bool:
    """Whether this root's job runs public: its row asks for it now and its exact
    text is approved. Turning the job back to `private` takes effect without the
    owner removing the grant."""
    row = job_row(conn, root)
    if row is None or row["reach"] != "public":
        return False
    return _row_approved(conn, root, row, "public")


def _row_approved(
    conn: sqlite3.Connection, root: "db.Task", row: sqlite3.Row | None, kind: str,
) -> bool:
    if row is None or not _task_runs_row_text(conn, root, row):
        return False
    grants = grants_for(conn, root.user_id, cron_subject(row["name"]))
    return f"{kind}:{job_digest(row)}" in grants


def _task_runs_row_text(conn: sqlite3.Connection, root: "db.Task", row: sqlite3.Row) -> bool:
    """Whether the task is running the text its job row holds. An approval is of the
    row, and the task is what runs. From the table, since `execute_task` enriches the
    prompt on the object it holds. `db.create_task` is the one writer of these fields
    on an occurrence, and `_create_scheduled_job_task` copies them from the row."""
    stored = conn.execute(
        "SELECT prompt, command, skill, skill_args FROM tasks WHERE id = ?", (root.id,),
    ).fetchone()
    if stored is None:
        return False
    return _digest_over(stored, TASK_TEXT_COLUMNS) == _digest_over(row, TASK_TEXT_COLUMNS)


def stored_posture(conn: sqlite3.Connection, task_id: int) -> str | None:
    """A task row's `egress_posture`, read from the table: the `Task` a caller holds
    may have been loaded before the row was decided."""
    row = conn.execute("SELECT egress_posture FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row[0] if row is not None else None


def record_posture(conn: sqlite3.Connection, task_id: int, posture: str) -> None:
    """What this occurrence was decided as, once, on the caller's transaction.

    Daemon only, before the model runs. `tests/test_egress_grants.py` holds its one
    caller, and nothing a task writes reaches it: not a deferred file, not a skill.
    """
    conn.execute(
        "UPDATE tasks SET egress_posture = ? WHERE id = ? AND egress_posture IS NULL",
        (posture, task_id),
    )


def add_grant(
    conn: sqlite3.Connection,
    user_id: str,
    subject: str,
    entry: str,
    *,
    decided_via: str,
    skill_index: "Mapping[str, object] | None" = None,
) -> Entry:
    """Approve an entry for a subject, on the caller's transaction.

    The writer. A task reaching this could approve itself, so its callers are
    held to a short list by `tests/test_egress_grants.py`. `skill_index` is what a
    `skill:` entry is checked against: it must name an `external` skill.
    """
    if decided_via not in DECIDERS:
        raise EntryRefused("Unknown way of deciding.")
    if not _valid_subject(subject):
        raise EntryRefused("Unknown subject.")
    parsed = parse_entry(entry)
    if parsed.kind in JOB_REQUESTS and not subject.startswith("cron:"):
        raise EntryRefused("Only a scheduled job has fixed text to approve.")
    if parsed.kind == "skill":
        meta = (skill_index or {}).get(parsed.value)
        if meta is None:
            raise EntryRefused("There is no such skill.")
        if getattr(meta, "egress", "external") == "local":
            raise EntryRefused("That skill does not need approval.")
    conn.execute(
        "INSERT OR IGNORE INTO egress_grants (user_id, subject, entry, decided_via) "
        "VALUES (?, ?, ?, ?)",
        (user_id, subject, parsed.text, decided_via),
    )
    return parsed


def approve_job(
    conn: sqlite3.Connection,
    user_id: str,
    subject: str,
    kind: str,
    digest: str | None,
    *,
    decided_via: str,
) -> Entry:
    """Approve a job's exact text for `open` or `public`, on the caller's transaction.

    A writer, held to the same short list as `add_grant`. `digest` is the text the
    person was shown, and a row that has moved since refuses with `StaleJob`, so an
    approval can never reach text nobody read. `None` is for the operator CLI, which
    prints the row's text as it approves it.
    """
    if kind not in JOB_REQUESTS:
        raise EntryRefused("Unknown kind of approval.")
    row = job_row_by_subject(conn, user_id, subject)
    if row is None:
        raise EntryRefused("There is no such job.")
    current = job_digest(row)
    if digest is not None and digest != current:
        raise StaleJob("This job's text has changed. Reload the page and read it again.")
    if kind == "public" and row["reach"] != "public":
        raise EntryRefused('This job does not ask for public access: its reach is not "public".')
    parsed = add_grant(conn, user_id, subject, f"{kind}:{current}", decided_via=decided_via)
    clear_refused(conn, user_id, subject, kind)
    return parsed


def remove_grant(conn: sqlite3.Connection, user_id: str, subject: str, entry: str) -> None:
    conn.execute(
        "DELETE FROM egress_grants WHERE user_id = ? AND subject = ? AND entry = ?",
        (user_id, subject, entry),
    )


def drop_job_approvals(
    conn: sqlite3.Connection, user_id: str, subject: str, *, keep_digest: str,
) -> list[str]:
    """Delete the `open:` and `public:` rows that do not name this digest, and
    return which kinds went. Host and `skill:` entries stay."""
    dropped = []
    for kind in JOB_REQUESTS:
        cursor = conn.execute(
            "DELETE FROM egress_grants WHERE user_id = ? AND subject = ? "
            "AND substr(entry, 1, ?) = ? AND entry != ?",
            (user_id, subject, len(kind) + 1, f"{kind}:", f"{kind}:{keep_digest}"),
        )
        if cursor.rowcount > 0:
            dropped.append(kind)
    return dropped


def forget_subject(conn: sqlite3.Connection, user_id: str, subject: str) -> None:
    """The subject is gone: its grants and refusals go with it."""
    conn.execute(
        "DELETE FROM egress_grants WHERE user_id = ? AND subject = ?", (user_id, subject),
    )
    conn.execute(
        "DELETE FROM egress_refused WHERE user_id = ? AND subject = ?", (user_id, subject),
    )


def note_refused(
    conn: sqlite3.Connection,
    user_id: str,
    subject: str,
    entry: str,
    *,
    reopen: bool = False,
) -> None:
    """Record what a subject tried, on the caller's transaction. It grants nothing.

    A repeat counts hits and leaves `dismissed` alone, so a dismissed entry does
    not ask again. `reopen` is for a new fact about the job (its approval was just
    dropped, its text changed), which the owner has not dismissed.

    Past `MAX_REFUSED_PER_SUBJECT` the oldest goes. A host goes before a request
    for a decision about the job, or a flood of hosts could hide that request.
    """
    if len(entry) > _MAX_HOST_CHARS:
        logger.debug("egress_refused_too_long: not stored")
        return
    conn.execute(
        "INSERT INTO egress_refused (user_id, subject, entry) VALUES (?, ?, ?) "
        "ON CONFLICT (user_id, subject, entry) DO UPDATE SET "
        "hits = hits + 1, last_seen = datetime('now'), "
        "dismissed = CASE WHEN ? THEN 0 ELSE dismissed END",
        (user_id, subject, entry, 1 if reopen else 0),
    )
    conn.execute(
        "DELETE FROM egress_refused WHERE rowid IN ("
        "  SELECT rowid FROM egress_refused WHERE user_id = ? AND subject = ?"
        "  ORDER BY entry IN ('open', 'public') DESC, last_seen DESC, rowid DESC"
        "  LIMIT -1 OFFSET ?"
        ")",
        (user_id, subject, MAX_REFUSED_PER_SUBJECT),
    )


def entries_from_log(pairs: "Iterable[tuple[str, str]]") -> set[str]:
    """What the lines of `egress-refused.jsonl` may become.

    The channel decides. A page chooses a hostname, so a name a network seam
    refused can only ever be a host: `https://open/` must not read as a request for
    full access, nor `CONNECT skill:browse:443` as one for a skill. A name no grant
    could hold (an IP literal, one label) is dropped, since there is nothing to add.
    """
    entries = set()
    for channel, text in pairs:
        if channel in _HOST_CHANNELS:
            try:
                parsed = parse_entry(text)
            except EntryRefused:
                continue
            if parsed.kind == "host":
                entries.add(parsed.text)
        elif channel == "browse" and text == "open":
            entries.add(text)
        elif channel == "skill" and text.startswith("skill:"):
            try:
                entries.add(parse_entry(text).text)
            except EntryRefused:
                continue
    return entries


def record_log(
    conn: sqlite3.Connection, user_id: str, subject: str, pairs: "Iterable[tuple[str, str]]",
) -> bool:
    """File a finished task's log under its subject, on the caller's transaction.

    One hit per entry per read, however many times the task tried. Answers whether
    any of it still asks the owner: a dismissed entry does not ask again.
    """
    entries = entries_from_log(pairs)
    for entry in entries:
        note_refused(conn, user_id, subject, entry)
    if not entries:
        return False
    return any(
        row["entry"] in entries and not row["dismissed"]
        for row in list_refused(conn, user_id, subject)
    )


def clear_refused(conn: sqlite3.Connection, user_id: str, subject: str, entry: str) -> None:
    conn.execute(
        "DELETE FROM egress_refused WHERE user_id = ? AND subject = ? AND entry = ?",
        (user_id, subject, entry),
    )


def list_refused(conn: sqlite3.Connection, user_id: str, subject: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT entry, hits, last_seen, dismissed FROM egress_refused "
        "WHERE user_id = ? AND subject = ? ORDER BY last_seen DESC, rowid DESC",
        (user_id, subject),
    ).fetchall()


def dismiss_refused(conn: sqlite3.Connection, user_id: str, subject: str, entry: str) -> None:
    """The owner does not expect this: it stays recorded and does not ask again."""
    conn.execute(
        "UPDATE egress_refused SET dismissed = 1 WHERE user_id = ? AND subject = ? AND entry = ?",
        (user_id, subject, entry),
    )


def has_open_refusal(conn: sqlite3.Connection, user_id: str, subject: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM egress_refused WHERE user_id = ? AND subject = ? AND dismissed = 0 LIMIT 1",
        (user_id, subject),
    ).fetchone()
    return row is not None


def job_row_by_subject(
    conn: sqlite3.Connection, user_id: str, subject: str,
) -> sqlite3.Row | None:
    """The user's own job row a `cron:` subject names, or None."""
    if not subject.startswith("cron:"):
        return None
    return conn.execute(
        "SELECT name, reach, prompt, command, skill, skill_args FROM scheduled_jobs "
        "WHERE user_id = ? AND name = ?",
        (user_id, subject[len("cron:"):]),
    ).fetchone()


def subject_exists(conn: sqlite3.Connection, user_id: str, subject: object) -> bool:
    """Whether this user owns a live thing this subject names. A closed assignment
    will not wake again, so nothing is left to decide about it."""
    if not _valid_subject(subject):
        return False
    if subject == TASKS_FILE_SUBJECT:
        return True
    kind, _, rest = str(subject).partition(":")
    if kind == "cron":
        return job_row_by_subject(conn, user_id, str(subject)) is not None
    if kind == "briefing":
        query = "SELECT 1 FROM briefing_configs WHERE user_id = ? AND name = ?"
    else:
        query = (
            "SELECT 1 FROM assignments WHERE user_id = ? AND id = ? "
            "AND state NOT IN ('completed', 'abandoned')"
        )
    return conn.execute(query, (user_id, rest)).fetchone() is not None


def subject_label(subject: str) -> str:
    """A subject in words. A job or briefing name is the owner's, or a limited
    task's, so whoever draws this treats it as text."""
    if subject == TASKS_FILE_SUBJECT:
        return "TASKS.md"
    kind, _, rest = subject.partition(":")
    if kind == "cron":
        return f"Scheduled job: {rest}"
    if kind == "briefing":
        return f"Briefing: {rest}"
    return f"Assignment {rest}"


def job_state(grants: frozenset[str], row: "sqlite3.Row | None") -> str:
    """What a job runs as now, in `scopes._egress_for`'s order: an approved `public`
    the row asks for, then an approved `open`. Anything else is limited."""
    if row is None:
        return "limited"
    digest = job_digest(row)
    if row["reach"] == "public" and f"public:{digest}" in grants:
        return "public"
    if f"open:{digest}" in grants:
        return "open"
    return "limited"


def list_subject_names(conn: sqlite3.Connection, user_id: str) -> list[str]:
    """Every subject this user can see on the page, sorted.

    Their jobs and briefings, their assignments still running, and whatever else
    has a row. A `_module.*` job is the module's and shows only with a row, so a
    refusal filed under one has somewhere to be answered.
    """
    names = set()
    for row in conn.execute(
        "SELECT name FROM scheduled_jobs WHERE user_id = ? AND substr(name, 1, 8) != '_module.'",
        (user_id,),
    ):
        names.add(cron_subject(row[0]))
    for row in conn.execute("SELECT name FROM briefing_configs WHERE user_id = ?", (user_id,)):
        names.add(f"briefing:{row[0]}")
    for row in conn.execute(
        "SELECT id FROM assignments WHERE user_id = ? AND state NOT IN ('completed', 'abandoned')",
        (user_id,),
    ):
        names.add(f"assignment:{row[0]}")
    for table in ("egress_grants", "egress_refused"):
        for row in conn.execute(f"SELECT DISTINCT subject FROM {table} WHERE user_id = ?", (user_id,)):
            if subject_exists(conn, user_id, row[0]):
                names.add(row[0])
    return sorted(names)


def describe_subject(conn: sqlite3.Connection, user_id: str, subject: str) -> dict:
    """One subject as the page and `istota egress list` show it. Hostnames and job
    text are for the owner's own session or the operator's terminal, and are data."""
    grants = grants_for(conn, user_id, subject)
    row = job_row_by_subject(conn, user_id, subject)
    job = None
    if row is not None:
        job = {
            "digest": job_digest(row),
            "reach": row["reach"],
            "prompt": row["prompt"] or "",
            "command": row["command"] or "",
            "skill": row["skill"] or "",
            "skill_args": row["skill_args"] or "",
        }
    return {
        "subject": subject,
        "kind": subject.partition(":")[0],
        "label": subject_label(subject),
        "state": job_state(grants, row),
        "hosts": sorted(_entries_of_kind(grants, "host")),
        "skills": sorted(_entries_of_kind(grants, "skill")),
        "approvals": sorted(
            text for text in grants if text.partition(":")[0] in JOB_REQUESTS
        ),
        "job": job,
        "refused": [
            {"entry": item["entry"], "hits": item["hits"], "last_seen": item["last_seen"]}
            for item in list_refused(conn, user_id, subject)
            if not item["dismissed"]
        ],
    }


def open_job_count(conn: sqlite3.Connection) -> int:
    """How many live jobs run with full access, for `doctor`. A digest is computed
    here, so a stale grant does not count, nor does a module's own row, whose grant
    from the seed does nothing, nor a job whose approved `public` comes first."""
    grants: dict[tuple[str, str], set[str]] = {}
    for row in conn.execute("SELECT user_id, subject, entry FROM egress_grants"):
        grants.setdefault((row[0], row[1]), set()).add(row[2])
    count = 0
    for job in conn.execute(
        "SELECT user_id, name, reach, prompt, command, skill, skill_args FROM scheduled_jobs "
        "WHERE substr(name, 1, 8) != '_module.'"
    ):
        entries = frozenset(grants.get((job["user_id"], cron_subject(job["name"])), ()))
        if job_state(entries, job) == "open":
            count += 1
    return count


def raise_refused_bell(
    config, conn: sqlite3.Connection, user_id: str, subjects: list[str],
) -> None:
    """Open or bump one bell row per subject, then push what is new.

    Called by the CRON.md sync and at task end, both in the daemon, with no
    transaction open. Never raises and logs no name: a job name is model-writable.
    """
    from istota.notifications import store
    from istota.notifications.resolvers import egress_refused

    try:
        results = [egress_refused.write(conn, user_id, subject) for subject in subjects]
        conn.commit()
        store.deliver_pending(config, results)
    except Exception as exc:  # noqa: BLE001 — logging never decides
        logger.warning("egress_bell_failed user=%s: %s", user_id, type(exc).__name__)
