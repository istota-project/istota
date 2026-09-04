"""istota's signaling protocol module, against a real signaling server.

Everything in `tests/test_talk_signaling.py` asserts on what `build_hello`,
`parse_welcome` and `parse_event` do to a dictionary the test wrote. That is the
right shape for a wire-format module and it cannot answer the question this file
exists for: whether the dictionaries are the ones a real server sends. Four of
the assertions below would pass against a server that relayed nothing, gated on
nothing and dropped every idle connection — which is exactly the class of test
this repo has now documented eight times — so each of them is paired with
something only the mechanism can produce.

**The honest limit of this shape, stated before the tests rather than
discovered in them.** There is no Nextcloud here, so hello-v2 is unreachable:
the token is a JWT Talk mints and signs, and the server verifies it against a
public key it fetches from Talk's capabilities. Neither end exists. So the
daemon in this stack never connects to the server at all — `[talk.signaling]
enabled` stays false, because `require_hpb` would otherwise refuse to boot the
container — and every client here runs in the pytest process. `participants/
active`, the room join, the authorization boundary and the payload diff are all
`tests/full/test_signaling_e2e.py`'s.

**The internal client is the harness's door and never istota's.** The harness
authenticates with the server's `[clients] internalsecret` to publish messages
the way Talk's `BackendNotifier` does and to observe a room. That credential
joins any room on the instance, invisibly to everyone in it (`hub.go:1922-1927`),
which is the whole reason istota authenticates as its own Nextcloud user
instead. `test_istotas_own_hello_can_never_take_the_internal_door` is what keeps
the two apart, and it is not decoration: an implementer reading upstream
examples meets `auth.type = "internal"` constantly.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import time

import pytest

from istota.transport.talk import signaling as sig

pytestmark = pytest.mark.smoke

#: Applied per class. The module-level marker stays a bare `pytest.mark.smoke`
#: because `tests/test_smoke_tier.py` greps for that exact line.
SIGNALING = pytest.mark.profile("signaling")

#: The room every scenario publishes into.
#:
#: An arbitrary string, and on this shape that is all a room token is: the
#: server creates a room the moment a session joins one and forgets it when the
#: last session leaves, with no backend lookup on the internal path. On the full
#: shape the same field is a real Talk token and Nextcloud authorizes the join.
ROOM = "lean-signaling-room"

#: A comment shaped the way `Listener.php:531` serializes one.
#:
#: Fabricated rather than captured. The field-by-field comparison against what
#: Talk really produces is `tests/full/test_signaling_e2e.py`'s job — it needs a
#: Talk to produce one — and a fixture copied from a real instance would be the
#: private-data rule broken for no gain.
def _comment(message: str, comment_id: int = 4201) -> dict:
    return {
        "id": comment_id,
        "token": ROOM,
        "actorType": "users",
        "actorId": "alice",
        "actorDisplayName": "Alice",
        "timestamp": 1_700_000_000,
        "message": message,
        "messageParameters": [],
        "systemMessage": "",
        "messageType": "comment",
        "reactions": [],
        "expirationTimestamp": 0,
    }


async def _hello(service, websockets, *, features=("chat-relay",)):
    """Connect, read the welcome, send an internal hello, join `ROOM`.

    The whole handshake in one helper because every scenario needs it and none
    of them is *about* it. `features` is the axis two of them vary: it is what
    the client declares in its own hello, and `ClientSession.filterMessage`
    consults exactly that and nothing else when deciding whether to hand over a
    relayed comment or strip it to a bare refresh.
    """
    # Retried on a refused connection only. Nothing orders the signaling
    # container ahead of the readiness wait — that waits on the istota
    # container — and `open_timeout` does not cover an immediate `ECONNREFUSED`,
    # so the first scenario of the profile can land in the window before the Go
    # binary has bound. Every later failure is raised as it comes.
    deadline = time.monotonic() + 30
    while True:
        try:
            connection = await websockets.connect(service.ws_url, open_timeout=20)
            break
        except OSError:
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(0.5)
    welcome = json.loads(await asyncio.wait_for(connection.recv(), timeout=20))
    server_features = sig.parse_welcome(welcome)

    await connection.send(json.dumps(service.internal_hello(features=features)))
    hello = json.loads(await asyncio.wait_for(connection.recv(), timeout=20))
    if hello.get("type") != "hello":
        await connection.close()
        raise AssertionError(f"the server refused the harness's hello: {hello}")

    await connection.send(
        json.dumps({"id": "2", "type": "room", "room": {"roomid": ROOM}})
    )
    joined = json.loads(await asyncio.wait_for(connection.recv(), timeout=20))
    if joined.get("type") != "room":
        await connection.close()
        raise AssertionError(f"the server refused the harness's join: {joined}")

    return connection, server_features, hello["hello"]


async def _next_chat_event(connection, *, timeout: float = 20.0):
    """The first frame `parse_event` recognises, or None if the deadline passes.

    A room carries `join`, `leave` and `participants` traffic that has nothing
    to do with chat, and `parse_event` returning None for each of those is
    itself part of what this file checks: a scenario that read only the next
    frame would be asserting against a participant-list update.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            raw = await asyncio.wait_for(connection.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            return None
        event = sig.parse_event(json.loads(raw))
        if event is not None:
            return event


@SIGNALING
class TestTheServerNegotiates:
    """`welcome` is read before authenticating, and it decides two things."""

    def test_the_advertised_features_are_read_off_a_real_welcome_frame(self, stack):
        """`chat-relay` and `hello-v2`, from the server rather than a version string.

        This is the runtime feature detection the design depends on, and reading
        it here is what makes the `chat-relay` gate below meaningful: a server
        that did not advertise it would send a bare refresh to every client and
        the gate test would pass for the wrong reason.
        """
        service = stack.service("signaling")
        websockets = sig.require_websockets()

        async def run():
            async with websockets.connect(service.ws_url, open_timeout=20) as ws:
                return sig.parse_welcome(
                    json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
                )

        features = asyncio.run(run())

        assert sig.CHAT_RELAY_FEATURE in features
        assert "hello-v2" in features
        # The frame is real, so the list is long. A `parse_welcome` that had
        # quietly started returning its input, or an empty tuple, would satisfy
        # both assertions above against a mangled frame but not this one.
        assert len(features) > 5, features

    def test_istotas_own_hello_can_never_take_the_internal_door(self, stack):
        """`build_hello` has no branch that emits `auth.type`, and that is load-bearing.

        The harness holds the server's internal secret and uses it three lines
        away, so the two doors are adjacent in this very file. istota's is the
        one Nextcloud authorizes per room, per session; the other one joins any
        room on the instance and leaves no participant row behind. A test that
        let `build_hello` produce the second would be testing the door this
        design exists to refuse.

        Driven against the *real* server's advertised feature list rather than a
        hand-written one, so the v2 branch is the branch actually taken.
        """
        service = stack.service("signaling")
        websockets = sig.require_websockets()

        async def run():
            async with websockets.connect(service.ws_url, open_timeout=20) as ws:
                return sig.parse_welcome(
                    json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
                )

        features = asyncio.run(run())
        settings = sig.SignalingSettings(
            server="http://signaling:8080",
            signaling_mode="external",
            hello_auth_params={"2.0": {"token": "fabricated.jwt.value"}},
            user_id="istota",
            backend_url="http://nextcloud",
        )

        frame = sig.build_hello(settings, features, "1")

        assert frame["hello"]["version"] == "2.0"
        assert "type" not in frame["hello"]["auth"]
        # The harness's own frame, for contrast, in the same assertion block —
        # so a reader can see that the thing being excluded is reachable and is
        # being excluded rather than being absent from the protocol.
        assert service.internal_hello()["hello"]["auth"]["type"] == "internal"


@SIGNALING
class TestTheChatRelayGate:
    """Two clients, one frame published, two different payloads delivered.

    This is the discriminating pair. "The comment arrived" on its own is equally
    true of a server that relays unconditionally, of one that relays to nobody
    while the test reads its own echo, and of a `parse_event` that fabricates a
    comment from a refresh. Only a server that consults the client's declared
    feature list can give two simultaneously-connected sessions two different
    answers to one published message.
    """

    def test_a_declaring_client_gets_the_comment_and_a_silent_one_gets_a_refresh(
        self, stack
    ):
        service = stack.service("signaling")
        websockets = sig.require_websockets()

        async def run():
            relaying, _, _ = await _hello(service, websockets)
            plain, _, _ = await _hello(service, websockets, features=())
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: service.publish_chat(ROOM, _comment("the relayed one")),
                )
                return await asyncio.gather(
                    _next_chat_event(relaying), _next_chat_event(plain)
                )
            finally:
                await plain.close()
                await relaying.close()

        relayed, refreshed = asyncio.run(run())

        assert relayed is not None, "the declaring client received no chat event"
        assert refreshed is not None, "the silent client received no chat event"

        assert relayed.refresh_only is False
        assert [c["message"] for c in relayed.comments] == ["the relayed one"]
        assert relayed.room_token == ROOM

        # `clientsession.go:1441-1445`: without the feature the server nulls
        # `Comment` and leaves `Refresh` true. The client is expected to fetch,
        # which is exactly what trigger mode does anyway.
        assert refreshed.refresh_only is True
        assert refreshed.comments == []

    def test_a_message_talk_withheld_still_arrives_as_a_trigger(self, stack):
        """`Listener.php:522-527` sends `refresh` with no comment for a message
        with no visible rendering, and for a system message outside the relay
        set. There is nothing to consume, so the only correct behaviour is to
        fetch — and a watcher that dropped these would lose a whole message
        class silently."""
        service = stack.service("signaling")
        websockets = sig.require_websockets()

        async def run():
            connection, _, _ = await _hello(service, websockets)
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: service.publish_chat(ROOM, refresh_only=True)
                )
                return await _next_chat_event(connection)
            finally:
                await connection.close()

        event = asyncio.run(run())

        assert event is not None
        assert event.refresh_only is True
        assert event.comments == []
        assert event.room_token == ROOM


@SIGNALING
class TestReconnect:
    """What the ingress drop and a server restart actually produce."""

    def test_a_resume_past_the_session_window_is_refused_as_no_such_session(
        self, stack
    ):
        """The expected answer past 30 seconds, not an error.

        `sessionExpireDuration` is 30 seconds (`hub.go:119`), so resume succeeds
        for a transient blip and never for anything past the first backoff step.
        Restarting the server is the shortest way to reach that state honestly —
        a real disconnect, a real resume attempt, and the server's own refusal —
        and it also exercises the frame `build_resume` produces against a server
        that has never seen the session.

        The classification is asserted alongside the code, because the code on
        its own says nothing about what a watcher then does: `fresh_hello` is
        what makes it re-fetch settings and send a full hello rather than
        retrying the resume forever.
        """
        service = stack.service("signaling")
        websockets = sig.require_websockets()

        async def open_session():
            connection, _, hello = await _hello(service, websockets)
            await connection.close()
            return hello["resumeid"]

        resume_id = asyncio.run(open_session())
        assert resume_id, "the server returned no resume id to try"

        stack.restart("signaling")

        async def resume():
            deadline = time.monotonic() + 60
            while True:
                try:
                    connection = await websockets.connect(
                        service.ws_url, open_timeout=10
                    )
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise
                    await asyncio.sleep(0.5)
            try:
                await asyncio.wait_for(connection.recv(), timeout=20)  # welcome
                await connection.send(json.dumps(sig.build_resume(resume_id, "1")))
                return json.loads(await asyncio.wait_for(connection.recv(), timeout=20))
            finally:
                await connection.close()

        frame = asyncio.run(resume())
        error = sig.parse_error(frame)

        assert error is not None, f"the restarted server accepted the resume: {frame}"
        assert error.code == "no_such_session"
        assert sig.classify_error(error.code) == sig.RECOVERY_FRESH_HELLO

    def test_the_room_is_rejoinable_after_the_restart(self, stack):
        """The other half of the recovery, and the one that says the server came
        back rather than merely refusing things.

        It restarts the server itself rather than relying on the test above
        having done it. Sharing that side effect by source order made this pass
        against a server that was never restarted whenever it was selected
        alone or the previous test failed early — the tier's own
        "indistinguishable from a no-op" rule, against a scenario whose whole
        claim is about recovery.
        """
        stack.restart("signaling")
        service = stack.service("signaling")
        websockets = sig.require_websockets()

        async def run():
            connection, _, _ = await _hello(service, websockets)
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: service.publish_chat(ROOM, _comment("after restart"))
                )
                return await _next_chat_event(connection)
            finally:
                await connection.close()

        event = asyncio.run(run())

        assert event is not None
        assert [c["message"] for c in event.comments] == ["after restart"]


# -- the control-ping requirement, and its negative control ------------------


def _raw_handshake(host: str, port: int, path: str) -> tuple[socket.socket, bytes]:
    """A WebSocket connection with no library behind it, for one purpose.

    The library requirement this file's last test exists for is that the client
    *answers* the server's control pings, and `websockets` has no switch to turn
    that off — which is fortunate for the product and inconvenient for a
    control. So the control is a socket that speaks just enough of the protocol
    to get a session established and then ignores every ping it is sent.
    """
    connection = socket.create_connection((host, port), timeout=20)
    key = base64.b64encode(os.urandom(16)).decode()
    connection.sendall(
        (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    buffered = b""
    while b"\r\n\r\n" not in buffered:
        chunk = connection.recv(4096)
        if not chunk:
            raise AssertionError("the server closed during the WebSocket handshake")
        buffered += chunk
    header, leftover = buffered.split(b"\r\n\r\n", 1)
    if b" 101 " not in header.split(b"\r\n", 1)[0]:
        raise AssertionError(f"the server refused the upgrade: {header[:120]!r}")
    # **The leftover is returned, not discarded**, and that is the whole reason
    # this returns a pair. The server writes its `welcome` immediately, so it
    # routinely arrives in the same TCP segment as the 101 response — and a
    # reader that started from the socket would begin one frame downstream,
    # block until the server's 2-second hello deadline expired, and read the
    # `bye` as though it were the first frame. Measured: the first text frame
    # arriving after 2.57 seconds and being `{"type": "bye", "reason":
    # "hello_timeout"}`.
    return connection, leftover


class _RawFrames:
    """A buffered reader for server text frames on a hand-rolled socket.

    Buffered rather than one `recv` per frame, because the server's `welcome`
    and its `hello` answer routinely arrive in one TCP read — so a reader that
    threw away whatever came with the frame it wanted would silently discard the
    answer the control below has to assert on.

    Server-to-client frames are unmasked by the protocol, so there is no
    unmasking here; anything but an unfragmented text frame is a server this
    helper does not model and is reported rather than skipped.
    """

    def __init__(self, connection: socket.socket, buffered: bytes = b"") -> None:
        self._connection = connection
        self._buffer = buffered

    def _need(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self._connection.recv(65536)
            if not chunk:
                raise AssertionError("the server closed while a frame was expected")
            self._buffer += chunk
        head, self._buffer = self._buffer[:count], self._buffer[count:]
        return head

    def text(self) -> dict:
        """The next text frame, skipping control frames without answering them.

        Skipping is not tidiness: the server sends a **ping immediately on
        connect** to measure round-trip time (it logs "Client from … has RTT of
        0 ms"), so the very first frame after the handshake is opcode 0x9 rather
        than the `welcome`. Not answering it is the entire point of this
        socket — a pong here would make the control a second well-behaved
        client and it would survive the wait like the library one.
        """
        while True:
            first, second = self._need(2)
            opcode = first & 0x0F
            if second & 0x80:
                raise AssertionError("the server masked a frame, which it must not")
            length = second & 0x7F
            if length == 126:
                length = int.from_bytes(self._need(2), "big")
            elif length == 127:
                length = int.from_bytes(self._need(8), "big")
            body = self._need(length)
            if opcode == 0x8:
                raise AssertionError(f"the server closed the connection: {body!r}")
            if opcode in (0x9, 0xA):
                continue
            if first != 0x81:
                raise AssertionError(
                    f"expected an unfragmented text frame, got {first:#x}"
                )
            return json.loads(body.decode())


def _masked_text_frame(payload: str) -> bytes:
    """One FIN text frame, masked. A client frame that is not masked is a
    protocol violation the server closes on, which would make the control fail
    for a reason unrelated to pings."""
    body = payload.encode()
    if len(body) > 0xFFFF:
        # Refused rather than encoded wrongly: the 127 branch is not written,
        # and truncating the length into two bytes produces a frame the server
        # rejects as a protocol error, which reads as a server fault.
        raise ValueError(
            f"{len(body)} bytes needs the 64-bit length branch, which this "
            "helper does not implement"
        )
    mask = os.urandom(4)
    masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(body))
    if len(body) < 126:
        header = bytes([0x81, 0x80 | len(body)])
    else:
        header = bytes([0x81, 0x80 | 126, len(body) >> 8, len(body) & 0xFF])
    return header + mask + masked


@SIGNALING
class TestAnIdleConnectionOutlivesTheServersReadDeadline:
    """The one library requirement that is easy to break by configuration.

    There is no client-to-server application ping in this protocol. Liveness
    runs the other way: the server sends a WebSocket *control* ping every 54
    seconds and drops a client that has not answered within 60
    (`client/client.go:52-55`). `websockets` replies automatically, so the
    requirement on istota is to leave that alone — and a connection that stops
    answering dies every 60 seconds with nothing in the protocol saying why.

    Both halves run against one 75-second wait rather than two, because a test
    that costs a minute and a quarter is one somebody skips.
    """

    def test_a_library_client_survives_and_a_silent_socket_does_not(self, stack):
        service = stack.service("signaling")
        websockets = sig.require_websockets()
        port = service.host_port

        silent, buffered = _raw_handshake("127.0.0.1", port, "/spreed")
        silent.settimeout(20.0)
        frames = _RawFrames(silent, buffered)
        welcome = frames.text()
        assert welcome.get("type") == "welcome", welcome

        # The server disconnects a client that has not said hello within two
        # seconds (`hub.go:113`), so the control has to establish a session or
        # it would die of the wrong thing.
        silent.sendall(_masked_text_frame(json.dumps(service.internal_hello("9"))))

        # **Read the answer, and assert on it.** Without this the control passes
        # for the wrong reason and the positive half proves nothing with it: a
        # wrong length branch, a bad mask, an unmasked frame or a rejected
        # secret each makes the server close *immediately*, and seventy-five
        # seconds later `closed` is True through a mechanism that has nothing to
        # do with a read deadline. A session id in hand is what separates the
        # two.
        answered = frames.text()
        assert answered.get("type") == "hello", (
            "the hand-rolled client's hello was refused, so anything the "
            f"control observes afterwards is about that instead: {answered}"
        )
        assert answered["hello"].get("sessionid"), answered

        async def run():
            connection, _, _ = await _hello(service, websockets)
            try:
                await asyncio.sleep(75)
                # A round trip, not `connection.state`: a closed TCP connection
                # can look open until something is written to it, and the
                # published comment coming back is proof the session is still
                # in its room rather than merely attached.
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: service.publish_chat(
                        ROOM, _comment("still here", comment_id=4202)
                    ),
                )
                return await _next_chat_event(connection)
            finally:
                await connection.close()

        try:
            event = asyncio.run(run())

            assert event is not None, (
                "the library client received nothing after 75 idle seconds; if "
                "it was dropped, the pong reply is off"
            )
            assert [c["message"] for c in event.comments] == ["still here"]

            # The control. The server's read deadline is 60 seconds and it pings
            # at 54, so a client that never answers is gone by now. Drain
            # whatever is buffered; an empty read is a closed connection.
            silent.settimeout(20.0)
            closed = False
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    if not silent.recv(65536):
                        closed = True
                        break
                except socket.timeout:
                    break
                except OSError:
                    closed = True
                    break
            # Anything still buffered in `frames` is irrelevant here: the
            # question is whether the *socket* ended, and an empty read is the
            # only thing that answers it.
            assert closed, (
                "the server kept a client that answered no control ping for 75 "
                "seconds, so the read deadline this test's positive half "
                "depends on is not being enforced and that half proves nothing"
            )
        finally:
            silent.close()
