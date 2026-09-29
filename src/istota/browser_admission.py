"""Bounded cross-process admission to the single-threaded browser API."""

from contextlib import contextmanager
import os
from pathlib import Path

import httpx

from istota.file_lock import exclusive_lock

QUEUE_WAIT_TIMEOUT = 90.0


class BrowserQueueTimeout(TimeoutError):
    """The browser stayed busy; no HTTP request was sent."""


def _queue_timeout(_path):
    return BrowserQueueTimeout(
        "Browser busy: timed out waiting for admission; no page request was sent."
    )


@contextmanager
def browser_admission(*, db_path=None, queue_timeout=QUEUE_WAIT_TIMEOUT):
    # The daemon supplies its config path; host-side skill processes inherit
    # that same path through ISTOTA_DB_PATH. Standalone calls use Config's default.
    database = Path(db_path or os.environ.get("ISTOTA_DB_PATH") or "data/istota.db")
    lock_path = database.resolve().parent / "browser-admission.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Never unlink: waiters must continue to contend on the same inode.
    with exclusive_lock(
        lock_path, timeout_seconds=queue_timeout, on_timeout=_queue_timeout,
    ):
        yield


class BrowserIdentityMissing(ValueError):
    """A browser request named no user, so the API would refuse it."""


def browser_call(method, url, **kwargs):
    """One browser API request, for a caller already holding admission.

    Refuses a request carrying no ``X-Istota-User``. The API answers one with
    400 ``user_scope_required``, and every caller reads a non-ok answer as an
    empty page, so a caller that forgot the header lost its content silently
    (ISSUE-557: every briefing browse block, and the evening FinViz data).
    A ``ValueError`` so the handlers that already catch ``browser_headers``'
    refusal catch this one too.
    """
    headers = kwargs.get("headers") or {}
    if not headers.get("X-Istota-User"):
        raise BrowserIdentityMissing(
            f"browser request to {url} names no user (X-Istota-User); "
            "pass browser_headers() or browser_headers(user_id)"
        )
    # httpx.delete deliberately has no body parameter; the state endpoint
    # needs a JSON selection while retaining the same admission lock.
    if method == "delete" and "json" in kwargs:
        return httpx.request(method, url, **kwargs)
    return getattr(httpx, method)(url, **kwargs)


def browser_request(method, url, *, db_path=None, queue_timeout=QUEUE_WAIT_TIMEOUT, **kwargs):
    """Start the HTTP timeout only after the browser admits this caller."""
    with browser_admission(db_path=db_path, queue_timeout=queue_timeout):
        return browser_call(method, url, **kwargs)
