"""The admin browser console: tickets, the relay count and the close codes.

An admin opens a user's live browser from Admin > Browsers. The ticket route
redeems a step-up proof and mints a ticket here; the websocket relay redeems
the ticket, since a websocket has no request body to carry a proof, and then
copies RFB bytes between the page and the user's x11vnc Unix socket.

Both stores are process memory: the web service runs one uvicorn process, and
a restart losing a 60-second ticket costs one more step-up.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Callable, NamedTuple

logger = logging.getLogger(__name__)

CONSOLE_DIR = Path(os.environ.get("ISTOTA_BROWSER_CONSOLE_DIR") or "/run/istota-browser-console")

TICKET_TTL_SECONDS = 60
MAX_TICKETS = 64
MAX_RELAYS = 8


class _Ticket(NamedTuple):
    admin: str
    session_id: str
    target: str
    expires_at: float


class TicketStore:
    """Single-use console tickets, bound to the admin and session that minted them."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._tickets: dict[str, _Ticket] = {}

    def mint(self, admin: str, session_id: str, target: str) -> str:
        ticket = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
        now = self._clock()
        with self._lock:
            expired = [key for key, held in self._tickets.items() if held.expires_at <= now]
            for key in expired:
                del self._tickets[key]
            # Insertion order is mint order, so the first key is the oldest.
            while len(self._tickets) >= MAX_TICKETS:
                del self._tickets[next(iter(self._tickets))]
            self._tickets[ticket] = _Ticket(admin, session_id, target, now + TICKET_TTL_SECONDS)
        return ticket

    def redeem(self, ticket: str, admin: str, session_id: str) -> str | None:
        """The target user id, or None. The ticket is gone after any attempt."""
        with self._lock:
            held = self._tickets.pop(ticket, None)
        if held is None or held.expires_at <= self._clock():
            return None
        if not secrets.compare_digest(held.admin.encode(), admin.encode()):
            return None
        if not secrets.compare_digest(held.session_id.encode(), session_id.encode()):
            return None
        return held.target


class RelayCounter:
    """The open relays in this process, capped at `MAX_RELAYS`.

    The ticket route reads `full()` to refuse before the step-up is spent;
    the relay takes a slot with `try_acquire()` and gives it back on close.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._open = 0

    def full(self) -> bool:
        with self._lock:
            return self._open >= MAX_RELAYS

    def try_acquire(self) -> bool:
        with self._lock:
            if self._open >= MAX_RELAYS:
                return False
            self._open += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._open = max(0, self._open - 1)


TICKETS = TicketStore()
RELAYS = RelayCounter()


class CloseCode(NamedTuple):
    when: str
    retry: bool


CLOSE_NORMAL = 1000
CLOSE_ERROR = 1011
CLOSE_BAD_FRAME = 4400
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_NOT_RUNNING = 4404
CLOSE_TIME_LIMIT = 4408
CLOSE_TOO_MANY = 4429

# The codes the relay sends. 1005 and 1006 are seen only by the page, which
# holds the wording for every row in `web/src/lib/admin/browsers/consoleClose.ts`.
CLOSE_CODES: dict[int, CloseCode] = {
    CLOSE_NORMAL: CloseCode("x11vnc ended the stream", retry=True),
    CLOSE_ERROR: CloseCode("unexpected relay error", retry=True),
    CLOSE_BAD_FRAME: CloseCode("no ticket text frame within 5s, or malformed", retry=True),
    CLOSE_UNAUTHORIZED: CloseCode("ticket unknown, expired, used or for another session; "
                                  "or the periodic re-check failed", retry=False),
    CLOSE_FORBIDDEN: CloseCode("not signed in, not admin, or bad origin", retry=False),
    CLOSE_NOT_RUNNING: CloseCode("no socket for this user, refused, not a socket, or a symlink", retry=False),
    CLOSE_TIME_LIMIT: CloseCode("the one-hour limit", retry=True),
    CLOSE_TOO_MANY: CloseCode("relay cap reached", retry=True),
}
