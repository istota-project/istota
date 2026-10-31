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
DIGEST_COLUMNS = ("prompt", "command", "skill", "skill_args")
DECIDERS = frozenset({"web", "cli", "migration"})
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
    digest = hashlib.sha256()
    for column in DIGEST_COLUMNS:
        value = row[column]
        data = ("" if value is None else str(value)).encode("utf-8", "surrogatepass")
        digest.update(f"{len(data)}:".encode())
        digest.update(data)
    return digest.hexdigest()


def cron_subject(job_name: str) -> str:
    return f"cron:{job_name}"


def _job_row(conn: sqlite3.Connection, task: "db.Task") -> sqlite3.Row | None:
    """The user's own job row behind a scheduled task, or None."""
    if task.source_type != "scheduled" or task.scheduled_job_id is None:
        return None
    return conn.execute(
        "SELECT name, prompt, command, skill, skill_args FROM scheduled_jobs "
        "WHERE id = ? AND user_id = ?",
        (task.scheduled_job_id, task.user_id),
    ).fetchone()


def subject_for(conn: sqlite3.Connection, root: "db.Task") -> str | None:
    """What grants hang on for this root task, or None where nothing can."""
    if root.source_type == "scheduled":
        row = _job_row(conn, root)
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
    `open:` or `public:` for that exact text. A digest that moved answers False,
    so a race with the sync cannot widen anything."""
    row = _job_row(conn, root)
    if row is None:
        return False
    grants = grants_for(conn, root.user_id, cron_subject(row["name"]))
    return f"{kind}:{job_digest(row)}" in grants


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


def raise_refused_bell(conn: sqlite3.Connection, user_id: str, subjects: list[str]) -> None:
    """Where the CRON.md sync hands over subjects with a new refusal row.

    Called after the sync's commit, with no transaction open. Stage 6 of the
    unified egress policy spec fills this in with
    `notifications/resolvers/egress_refused.py`. Logs a count: a job name is
    model-writable.
    """
    logger.info("egress_refused_pending user=%s subjects=%d", user_id, len(subjects))
