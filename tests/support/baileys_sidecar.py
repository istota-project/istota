"""A fake Baileys sidecar: a real client on a real Unix socket.

The daemon listens and the sidecar dials, so the fake is a **client**. The
spec's test-strategy line calls it "an in-process asyncio server"; the
direction follows the design's own `devbox_proxy` citation and its "a local
Unix socket the daemon owns, 0600" wording, and the fake follows the code.

Real sockets rather than a double, deliberately. Every property this stage is
about is a property of the link — that a dropped connection settles a send
`unknown`, that a line past the cap is refused rather than truncated, that a
second sidecar is turned away, that the reader keeps reading while the worker
is waiting for a `send_result`. A double that answered calls would assert none
of them, and `.claude/rules/testbed.md` has eight entries about exactly that
shape of test.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

from istota.transport.whatsapp import baileys_protocol as proto

#: `sun_path` is 104 bytes on macOS, and pytest's `tmp_path` is regularly
#: longer than that on its own. The failure is an `OSError` naming neither the
#: path nor the limit, so the sockets get a short directory of their own.
SOCKET_ROOT = "/tmp"


class SocketDir:
    """A short-lived directory under `/tmp` for a socket and a session."""

    def __init__(self) -> None:
        self.path = Path(tempfile.mkdtemp(prefix="istota-wa-", dir=SOCKET_ROOT))

    @property
    def socket(self) -> Path:
        return self.path / "wa.sock"

    @property
    def session(self) -> Path:
        return self.path / "session"

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


class FakeSidecar:
    """One connection to the bridge, driven line by line from a test."""

    def __init__(self, socket_path: Path) -> None:
        self._socket_path = socket_path
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.received: list[dict] = []

    async def connect(self, *, hello: bool = True, version: int | None = None):
        self.reader, self.writer = await asyncio.open_unix_connection(
            str(self._socket_path), limit=proto.MAX_LINE_BYTES + 4096,
        )
        if hello:
            await self.say(
                proto.MSG_HELLO,
                protocol_version=proto.PROTOCOL_VERSION if version is None else version,
            )
        return self

    async def say(self, message_type: str, /, **fields) -> None:
        # Positional-only: an `inbound` line carries a `message_type` field of
        # its own, and a keyword parameter of the same name would collide with
        # the envelope's.
        await self.write_raw(proto.encode(message_type, **fields))

    async def write_raw(self, raw: bytes) -> None:
        assert self.writer is not None
        self.writer.write(raw)
        await self.writer.drain()

    async def next_message(self, timeout: float = 2.0) -> dict:
        """The next line the bridge wrote, decoded."""
        assert self.reader is not None
        line = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
        if not line:
            raise EOFError("the bridge closed the connection")
        payload = json.loads(line)
        self.received.append(payload)
        return payload

    async def expect(self, message_type: str, timeout: float = 2.0) -> dict:
        payload = await self.next_message(timeout=timeout)
        assert payload["type"] == message_type, payload
        return payload

    async def answer_send(self, *, ok: bool = True, **fields) -> dict:
        """Read one `send` and answer it with a `send_result` for its id."""
        request = await self.expect(proto.MSG_SEND)
        await self.say(
            proto.MSG_SEND_RESULT,
            request_id=request["request_id"], ok=ok, **fields,
        )
        return request

    async def wait_closed(self, timeout: float = 2.0) -> None:
        """Block until the bridge closes its end."""
        assert self.reader is not None
        await asyncio.wait_for(self.reader.read(), timeout=timeout)

    async def drop(self) -> None:
        """Close the socket without a `shutdown`, the way a crash does."""
        if self.writer is None:
            return
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass
        self.writer = None

    async def close(self) -> None:
        await self.drop()


async def wait_for(predicate, *, timeout: float = 2.0, interval: float = 0.005):
    """Poll a predicate until it holds, or fail the test with what it saw.

    The bridge dispatches on its own tasks, so a test that asserts immediately
    after writing a line is asserting against a race. Polling rather than
    sleeping a fixed interval keeps the suite fast on a quiet machine and
    correct on a loaded one.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never held")
        await asyncio.sleep(interval)


def mode_of(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
