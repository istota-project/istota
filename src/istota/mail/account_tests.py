"""A connected mail account's connection test, handed from the web process to
the daemon.

A test is a sign-in to a host a user typed, so it runs in the daemon, where the
egress posture is decided, never in the web process: the route leaves a request
here and the daemon's ``mail-account-test-request`` gate answers it on its next
tick, the shape of ``health/extract_requests.py``. A request stays until it is
answered, so a daemon restarted mid-test answers it on the next tick.

A reserved ``shared_kv`` namespace, written by the web route and the daemon and
never by a task. Keys are ``request:<id>``, ``answered:<id>`` and
``recent:<user_id>``, the last holding the times of that user's requests in the
past hour for the hourly cap. A request names its user and account id and
nothing else: no host, no username, no password. The answer is the plain
message lines of ``setup_checks.check_mailbox``, which the setup wizard and
``istota mail-account test`` share, and is removed when its owner reads it.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from istota.config import Config

logger = logging.getLogger(__name__)

TEST_NAMESPACE = "_mail_account_test"
_REQUEST_PREFIX = "request:"
_ANSWERED_PREFIX = "answered:"
_RECENT_PREFIX = "recent:"
# The poll runs on the scheduler's dispatch loop every tick, so a held write
# lock skips the tick rather than stalling it.
_POLL_BUSY_MS = 2000
_ANSWER_RETENTION = "-1 day"
_REQUEST_ID = re.compile(r"^[0-9a-f]{16}$")

MAX_PENDING_PER_USER = 1
MAX_PER_HOUR = 20
_WINDOW_SECONDS = 3600

PENDING = "pending"
ANSWERED = "answered"
UNKNOWN = "unknown"

# Plain messages for an account the daemon will not hand over. The reasons are
# `accounts.resolve_for_daemon`'s closed set.
_UNRESOLVED_MESSAGES = {
    "mail_accounts_disabled": "Connected mail accounts are turned off on this instance.",
    "account_not_found": "This account is turned off or no longer exists.",
    "permission_denied": "This account is not allowed to send.",
    "password_missing": "No password is stored for this account.",
}


class TooManyRequests(Exception):
    """The user already has a test waiting for the daemon."""


class RateLimited(Exception):
    """The user has asked for ``MAX_PER_HOUR`` tests in the past hour."""

    def __init__(self, retry_after: int):
        super().__init__(f"retry after {retry_after}s")
        self.retry_after = retry_after


def _now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _recent_times(value: str | None, now: float) -> list[float]:
    try:
        times = json.loads(value) if value else []
    except ValueError:
        times = []
    if not isinstance(times, list):
        return []
    return sorted(t for t in times
                  if isinstance(t, (int, float)) and now - _WINDOW_SECONDS < t <= now)


def request_test(
    config: "Config", user_id: str, account_id: int, *, now: float | None = None,
) -> str:
    """Ask the daemon to test one account on its next tick. Returns the id.

    Raises `TooManyRequests` while the user has one waiting, and `RateLimited`
    past ``MAX_PER_HOUR`` in the past hour. The caller has already checked the
    account is the user's.
    """
    from istota import db

    now = time.time() if now is None else now
    request_id = uuid.uuid4().hex[:16]
    request = {"id": request_id, "user_id": user_id, "account_id": int(account_id),
               "at": _now_text()}
    with db.get_db(config.db_path) as conn:
        # One write transaction from the counts, so two clicks cannot both
        # take the last slot.
        conn.execute("BEGIN IMMEDIATE")
        for row in conn.execute(
            "SELECT value FROM shared_kv WHERE namespace = ? AND key LIKE ?",
            (TEST_NAMESPACE, _REQUEST_PREFIX + "%"),
        ):
            try:
                owner = json.loads(row["value"]).get("user_id")
            except (ValueError, AttributeError):
                continue
            if owner == user_id:
                raise TooManyRequests(user_id)
        recent = db.shared_kv_get(conn, TEST_NAMESPACE, _RECENT_PREFIX + user_id)
        times = _recent_times(recent["value"] if recent else None, now)
        if len(times) >= MAX_PER_HOUR:
            oldest = times[len(times) - MAX_PER_HOUR]
            raise RateLimited(max(1, int(oldest + _WINDOW_SECONDS - now)))
        times.append(now)
        db.shared_kv_set(conn, TEST_NAMESPACE, _RECENT_PREFIX + user_id,
                         json.dumps(times), "web")
        db.shared_kv_set(conn, TEST_NAMESPACE, _REQUEST_PREFIX + request_id,
                         json.dumps(request), "web")
    return request_id


def requests_pending(config: "Config") -> bool:
    """Whether any test is waiting. One indexed read, for the dispatch loop."""
    if not Path(config.db_path).exists():
        return False
    from istota import db

    with db.get_db(config.db_path, busy_timeout_ms=_POLL_BUSY_MS) as conn:
        row = conn.execute(
            "SELECT 1 FROM shared_kv WHERE namespace = ? AND key LIKE ? LIMIT 1",
            (TEST_NAMESPACE, _REQUEST_PREFIX + "%"),
        ).fetchone()
    return row is not None


def take_pending(config: "Config") -> dict | None:
    """The oldest waiting request, left in place until it is answered.

    A value that is not a request is removed rather than returned, so one bad
    row cannot block the queue behind it.
    """
    if not Path(config.db_path).exists():
        return None
    from istota import db

    with db.get_db(config.db_path) as conn:
        while True:
            row = conn.execute(
                "SELECT key, value FROM shared_kv WHERE namespace = ? AND key LIKE ? "
                "ORDER BY updated_at, key LIMIT 1",
                (TEST_NAMESPACE, _REQUEST_PREFIX + "%"),
            ).fetchone()
            if row is None:
                return None
            try:
                request = json.loads(row["value"])
            except ValueError:
                request = None
            if (isinstance(request, dict) and request.get("id")
                    and isinstance(request.get("user_id"), str)
                    and type(request.get("account_id")) is int):
                return request
            logger.warning("mail_account_test_request_dropped key=%s", row["key"])
            db.shared_kv_delete(conn, TEST_NAMESPACE, row["key"])


def record_answer(config: "Config", request: dict, result: dict) -> None:
    """Record a request's answer and remove the request, in one transaction,
    pruning answers nobody collected. Never raises."""
    from istota import db

    request_id = request["id"]
    value = json.dumps({
        "id": request_id, "user_id": request.get("user_id", ""),
        "at": _now_text(), "result": result,
    })
    try:
        with db.get_db(config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM shared_kv WHERE namespace = ? AND key LIKE ? "
                "AND updated_at < datetime('now', ?)",
                (TEST_NAMESPACE, _ANSWERED_PREFIX + "%", _ANSWER_RETENTION),
            )
            db.shared_kv_set(conn, TEST_NAMESPACE, _ANSWERED_PREFIX + request_id,
                             value, "daemon")
            db.shared_kv_delete(conn, TEST_NAMESPACE, _REQUEST_PREFIX + request_id)
    except Exception as exc:  # noqa: BLE001 - a record of the test, not the test
        logger.warning("mail_account_test_answer_unwritten id=%s err=%s",
                       request_id, type(exc).__name__)


def read_answer(config: "Config", user_id: str, request_id: str) -> dict:
    """Where one of ``user_id``'s tests stands: ``{"status": "pending"}``,
    ``{"status": "answered", "result": ...}`` (removed once read), or
    ``{"status": "unknown"}``, which is also what another user's id reads."""
    from istota import db

    if not isinstance(request_id, str) or not _REQUEST_ID.match(request_id):
        return {"status": UNKNOWN}
    with db.get_db(config.db_path) as conn:
        for state, prefix in ((ANSWERED, _ANSWERED_PREFIX), (PENDING, _REQUEST_PREFIX)):
            row = db.shared_kv_get(conn, TEST_NAMESPACE, prefix + request_id)
            if row is None:
                continue
            try:
                value = json.loads(row["value"])
            except ValueError:
                continue
            if not isinstance(value, dict) or value.get("user_id") != user_id:
                continue
            if state == ANSWERED:
                db.shared_kv_delete(conn, TEST_NAMESPACE, prefix + request_id)
                return {"status": ANSWERED, "result": value.get("result")}
            return {"status": PENDING}
    return {"status": UNKNOWN}


def _refused(message: str) -> dict:
    return {"ok": False, "message": message,
            "lines": [{"step": "account", "ok": False, "message": message}]}


def run_test(config: "Config", user_id: str, account_id: int) -> dict:
    """Sign in to one of ``user_id``'s accounts and answer ``{ok, message, lines}``.

    IMAP always; SMTP too when the account may send. Through
    ``setup_checks.check_mailbox``, so the classifier and the messages are the
    setup wizard's, and the connection is ``mail.connect``'s with the address
    checked as every read of the account checks it. Sends nothing, changes
    nothing, and no line carries server text or a password.
    """
    from istota import db
    from istota.mail import accounts
    from istota.skills.email import account_email_config
    from istota.webui.setup_checks import check_mailbox

    settings = config.mail_accounts
    with db.get_db(config.db_path) as conn:
        account = accounts.get_account_by_id(conn, user_id, account_id)
    sends = bool(
        account is not None and account.permission == "read_send"
        and account.smtp_host and settings.allow_send
    )
    resolved = accounts.resolve_for_daemon(
        Path(config.db_path), user_id, account_id,
        purpose="send" if sends else "read", settings=settings,
    )
    if isinstance(resolved, accounts.Unresolved):
        return _refused(_UNRESOLVED_MESSAGES.get(resolved.reason, "This account cannot be tested."))
    email = account_email_config(
        resolved.account, accounts.login_blocks(resolved),
        private_hosts=settings.private_hosts, readonly=True,
    )
    result = check_mailbox(
        email, smtp=sends, check_public=True, private_hosts=tuple(settings.private_hosts),
    )
    return {
        "ok": result.ok,
        "message": result.message,
        "lines": [{"step": line.key, "ok": line.ok, "message": line.message}
                  for line in result.lines or []],
    }


def answer_pending(config: "Config") -> None:
    """Answer every waiting test, oldest first. Never raises."""
    answered: set[str] = set()
    while True:
        try:
            request = take_pending(config)
        except Exception:  # noqa: BLE001 - the gate retries on the next tick
            logger.warning("mail_account_test_queue_unreadable", exc_info=True)
            return
        # A request seen twice is one whose answer could not be written; leave
        # it for the next tick rather than spinning on it.
        if request is None or request["id"] in answered:
            return
        answered.add(request["id"])
        try:
            result = run_test(config, request["user_id"], request["account_id"])
        except Exception as exc:  # noqa: BLE001 - the next request must still be answered
            logger.error(
                "mail_account_test_failed id=%s account_id=%s err=%s",
                request["id"], request["account_id"], type(exc).__name__,
            )
            result = _refused("The test could not be run.")
        logger.info(
            "mail_account_test_answered id=%s user=%s account_id=%s ok=%s",
            request["id"], request["user_id"], request["account_id"], result.get("ok"),
        )
        record_answer(config, request, result)
