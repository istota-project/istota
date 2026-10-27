"""The admin browser console: tickets, the relay count and the close codes.

An admin opens a user's live browser from Admin > Browsers. The ticket route
redeems a step-up proof and mints a ticket here; the websocket relay redeems
the ticket, since a websocket has no request body to carry a proof, and then
copies RFB bytes between the page and the user's x11vnc Unix socket.

Both stores are process memory: the web service runs one uvicorn process, and
a restart losing a 60-second ticket costs one more step-up.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Awaitable, Callable, NamedTuple

from istota.lib.browser_console import console_socket_name
from istota.lib.unix_connect import connect_no_follow

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


# ---- The relay ----
#
# Module constants rather than parameters, so tests patch them.
TICKET_WAIT_SECONDS = 5.0
MAX_TICKET_LENGTH = 128
CONNECT_TIMEOUT_SECONDS = 5.0
RECHECK_SECONDS = 30.0
LIFETIME_SECONDS = 3600.0
READ_SIZE = 64 * 1024


def _id(value: str | None) -> str:
    return "-" if value is None else repr(value)


def log_refused(admin: str | None, target: str | None, code: int) -> None:
    logger.info("browser_console_refused admin=%s user=%s code=%d", _id(admin), _id(target), code)


async def read_ticket(websocket) -> str | None:
    """The first frame, if it is a text frame of ticket length within the wait."""
    try:
        message = await asyncio.wait_for(websocket.receive(), TICKET_WAIT_SECONDS)
    except asyncio.TimeoutError:
        return None
    if message.get("type") != "websocket.receive":
        return None
    text = message.get("text")
    if not isinstance(text, str) or not text or len(text) > MAX_TICKET_LENGTH:
        return None
    return text


def _connect_blocking(path: Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        # The timeout is on the socket, not a wait_for around the thread, so a
        # connect that lands late is closed here rather than leaked.
        sock.settimeout(CONNECT_TIMEOUT_SECONDS)
        connect_no_follow(sock, str(path))
        sock.setblocking(False)
    except BaseException:
        sock.close()
        raise
    return sock


async def _connect(target: str) -> socket.socket:
    # A blocking AF_UNIX connect waits while x11vnc's backlog is full.
    return await asyncio.to_thread(_connect_blocking, CONSOLE_DIR / console_socket_name(target))


class _ClientClosed(Exception):
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


async def _copy_to_socket(websocket, writer: asyncio.StreamWriter) -> None:
    """Page to x11vnc: binary frames only. Raises `_ClientClosed` on the page's close."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            raise _ClientClosed(message.get("code") or 1005)
        data = message.get("bytes")
        if data:
            writer.write(data)
            await writer.drain()


async def _copy_to_page(reader: asyncio.StreamReader, websocket) -> int:
    """x11vnc to the page; returns 1000 when x11vnc ends the stream."""
    while True:
        data = await reader.read(READ_SIZE)
        if not data:
            return CLOSE_NORMAL
        await websocket.send_bytes(data)


async def _recheck(authorized: Callable[[], Awaitable[bool]]) -> int:
    while True:
        await asyncio.sleep(RECHECK_SECONDS)
        if not await authorized():
            return CLOSE_UNAUTHORIZED


async def _close(websocket, code: int) -> None:
    try:
        await websocket.close(code)
    except Exception:
        # The page may already be gone; there is nobody left to tell.
        pass


async def relay(websocket, admin: str, target: str, *,
                authorized: Callable[[], Awaitable[bool]]) -> int:
    """Connect to `target`'s x11vnc socket and copy bytes both ways until one
    side ends it, the re-check fails or the lifetime runs out.

    The websocket is accepted and the ticket redeemed; this closes it with the
    code it returns, unless the page closed first.
    """
    try:
        sock = await _connect(target)
    except (OSError, asyncio.TimeoutError):
        log_refused(admin, target, CLOSE_NOT_RUNNING)
        await _close(websocket, CLOSE_NOT_RUNNING)
        return CLOSE_NOT_RUNNING

    started = time.monotonic()
    code = CLOSE_ERROR
    client_closed = False
    writer = None
    tasks: list[asyncio.Task] = []
    try:
        reader, writer = await asyncio.open_unix_connection(sock=sock)
        await websocket.send_text("ok")
        logger.info("browser_console_opened admin=%r user=%r", admin, target)
        tasks = [
            asyncio.create_task(_copy_to_socket(websocket, writer)),
            asyncio.create_task(_copy_to_page(reader, websocket)),
            asyncio.create_task(_recheck(authorized)),
        ]
        done, _ = await asyncio.wait(tasks, timeout=LIFETIME_SECONDS,
                                     return_when=asyncio.FIRST_COMPLETED)
        if not done:
            code = CLOSE_TIME_LIMIT
        else:
            # The page's close outranks anything that finished beside it.
            first = tasks[0] if tasks[0] in done else done.pop()
            error = first.exception()
            if isinstance(error, _ClientClosed):
                code, client_closed = error.code, True
            elif error is not None:
                logger.info("browser_console_error admin=%r user=%r err=%s",
                            admin, target, type(error).__name__)
                code = CLOSE_ERROR
            else:
                code = first.result()
    except Exception as exc:
        logger.info("browser_console_error admin=%r user=%r err=%s", admin, target, type(exc).__name__)
        code = CLOSE_ERROR
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if writer is not None:
            writer.close()
        else:
            sock.close()
    if not client_closed:
        await _close(websocket, code)
    logger.info("browser_console_closed admin=%r user=%r seconds=%d code=%d",
                admin, target, round(time.monotonic() - started), code)
    return code
