"""The wire protocol of the Nextcloud Talk standalone signaling server (HPB).

A leaf over the protocol: it builds the frames istota sends, reads the ones it
receives, and decides what a refusal means. It opens no socket, makes no OCS
call and imports nothing from ``istota`` — the two Nextcloud requests the
frames are built from (``get_signaling_settings``, ``join_room_session``) are
``TalkClient`` methods, so every Nextcloud request in this design goes through
the one client with its one auth path and its one error handling. The point of
the split is that the frames are decidable in a unit test with no daemon, no
container and no Nextcloud.

**istota authenticates as its own Nextcloud user, and that is the load-bearing
decision.** The protocol offers a second door — an *internal client*,
authenticated with the signaling server's shared secret — which is simpler and
needs no per-room join. It is not built, not behind a flag and not as a
fallback: that credential bypasses Nextcloud entirely (``hub.go:1922-1927``,
``// Internal clients can join any room``), so it would reach live chat in any
room on the instance, including rooms between two other people that istota's
own account has never been in — and invisibly, since an internal session
carries no room session id and ``Room.publishActiveSessions`` skips it
(``room.go:1170``). istota's whole relationship with Nextcloud is that its
reach is its user's reach, enforced by Nextcloud. So ``build_hello`` emits no
``auth.type`` (it defaults to ``client``, ``api/signaling.go:497-499``) and
there is no code path here that could emit ``internal``.

Three things here are easy to get wrong in a way that fails opaquely:

- **``auth.params`` is the ``helloAuthParams`` sub-object passed through
  verbatim** (``signaling.js:1046-1058``), never rebuilt field by field. Talk
  is free to add a field to either form, and a rebuild silently drops it.
- **``internal-incall`` is never declared.** It suppresses a phantom in-call
  flag the server sets only on internal sessions
  (``clientsession.go:151-156``); a user session that never joins a call has
  ``inCall: 0`` from Nextcloud's own participant record. It appears constantly
  in upstream examples, which is why its absence is asserted by a named test.
- **A room join with no Talk session id is refused rather than serialised.**
  Reading only the Nextcloud half of the code suggests it would degrade to a
  user-scoped join; it does not. The signaling server substitutes its own
  public session id for an empty one (``hub.go:1937-1943``), so Nextcloud takes
  the ``getParticipantBySession`` branch
  (``SignalingController.php:891-895``), finds nothing, and answers with the
  generic ``no_such_room`` that also means "no such room" and "not a
  participant". Failing at the frame builder is the only place the real cause
  can be named.

**The three per-connection credentials — the hello-v2 JWT, the v1 ticket and
the ``resumeid`` — never reach a log record at any level.** None of them is a
config value, so there is nothing for ``admin_config_view`` to redact and the
rule lives here instead: nothing in this module logs frame contents, and the
one line that reports the v1 fallback names the version and not the ticket.
The ``resumeid`` is the one most easily overlooked — it authenticates a full
session resume, room membership included, for its 30-second window
(``hub.go:119``). A server *error* message is carried on ``SignalingError``
and is safe to log: upstream's are fixed strings ("The token is expired"), not
echoes of what was sent.
"""

import logging
import random
from dataclasses import dataclass, field

logger = logging.getLogger("istota.transport.talk.signaling")

# The one feature istota declares. It is what gets the relayed comment instead
# of a bare refresh (`clientsession.go:1437-1446`); the server consults the
# client's declared features and nothing else on that path — `ClientType()` is
# never read — which is what makes a user-authenticated session receive
# byte-for-byte what an internal client would.
CLIENT_FEATURES = ("chat-relay",)

# Where Talk's signaling backend lives, relative to the Nextcloud base URL.
# This is the `auth.url` of the hello frame: the HPB matches it against its own
# `[backend]` block, and a mismatch is the fatal `invalid_backend`.
BACKEND_PATH = "/ocs/v2.php/apps/spreed/api/v3/signaling/backend"

# Recovery classes. The branches are not guessable from the code alone, which
# is why the taxonomy is data rather than a chain of `if code ==`.
RECOVERY_FRESH_TOKEN = "fresh_token"      # discard the token, re-fetch settings
RECOVERY_FRESH_SESSION = "fresh_session"  # re-POST participants/active, once
RECOVERY_FRESH_HELLO = "fresh_hello"      # resume failed, send a full hello
RECOVERY_FATAL = "fatal"                  # will not fix itself; stop the watcher

RETRY_FRESH_TOKEN = {"token_expired", "token_not_valid_yet"}
RETRY_FRESH_SESSION = {"no_such_room"}
RETRY_FRESH_HELLO = {"no_such_session"}
FATAL = {"invalid_backend", "invalid_client_type", "invalid_hello_version"}

_SCHEMES = (("https://", "wss://"), ("http://", "ws://"),
            ("wss://", "wss://"), ("ws://", "ws://"))

# Bounds the exponent so a watcher that has been down for a week cannot ask for
# 2**900 seconds before the ceiling is applied.
_MAX_BACKOFF_EXPONENT = 30


@dataclass(frozen=True)
class SignalingSettings:
    """One ``GET /v3/signaling/settings`` response, as the client needs it.

    ``backend_url`` is not in the payload — it is derived from the Nextcloud
    base URL and carried here because it is the hello frame's ``auth.url`` and
    ``build_hello`` has nowhere else to get it.
    """

    server: str                  # HPB base URL, as Nextcloud gave it
    signaling_mode: str          # "internal" | "external" | "conversation_cluster"
    hello_auth_params: dict      # {"1.0": {...}, "2.0": {...}} verbatim, never rebuilt
    user_id: str | None
    backend_url: str


@dataclass
class ChatEvent:
    """A room chat event, reduced to what the ingest path can act on."""

    room_token: str
    comments: list = field(default_factory=list)  # empty when refresh-only
    refresh_only: bool = False


class SignalingError(Exception):
    """A refusal from the signaling server, carrying its recovery class.

    ``message`` is the server's own text. Upstream's are fixed strings and
    never echo the credential that was sent, so it is safe to log — unlike a
    frame, which this module never logs.
    """

    def __init__(self, code, message: str = ""):
        self.code = code if isinstance(code, str) else ""
        self.message = " ".join(str(message or "").split())[:200]
        self.recovery = classify_error(self.code)
        super().__init__(f"{self.code or 'unknown'}: {self.message}".rstrip(": "))


def classify_error(code) -> str:
    """Map a server error code to exactly one recovery class.

    An unrecognised code is fatal rather than retried. Retrying a refusal
    nobody understands is the failure that looks healthy: the socket
    reconnects on its backoff, `doctor` reports a live watcher, and nothing is
    ever delivered.
    """
    if not isinstance(code, str):
        return RECOVERY_FATAL
    if code in RETRY_FRESH_TOKEN:
        return RECOVERY_FRESH_TOKEN
    if code in RETRY_FRESH_SESSION:
        return RECOVERY_FRESH_SESSION
    if code in RETRY_FRESH_HELLO:
        return RECOVERY_FRESH_HELLO
    return RECOVERY_FATAL


def parse_settings(data, *, nextcloud_url: str) -> SignalingSettings:
    """Read the OCS settings payload. Tolerates anything; validates nothing.

    A payload it cannot read yields empty fields rather than an exception, so
    the failure surfaces where it can be named — ``websocket_url`` on an empty
    server, ``build_hello`` on absent auth params.
    """
    payload = data if isinstance(data, dict) else {}

    params = payload.get("helloAuthParams")
    if not isinstance(params, dict):
        params = {}

    base = (nextcloud_url or "").rstrip("/") if isinstance(nextcloud_url, str) else ""
    user_id = payload.get("userId")

    return SignalingSettings(
        server=payload["server"] if isinstance(payload.get("server"), str) else "",
        signaling_mode=(
            payload["signalingMode"]
            if isinstance(payload.get("signalingMode"), str) else ""
        ),
        # Verbatim, by reference: nothing here rebuilds a sub-object.
        hello_auth_params=params,
        user_id=user_id if isinstance(user_id, str) and user_id else None,
        backend_url=f"{base}{BACKEND_PATH}" if base else "",
    )


def websocket_url(server) -> str:
    """`https`->`wss`, `http`->`ws`, strip trailing slash, append `/spreed`.

    The rule Talk's own client uses (``signaling.js:1008-1010``). A value it
    cannot turn into a WebSocket URL is refused rather than mangled: this is
    the connect target, and ``"" -> "/spreed"`` would be a connection to
    nothing with no message saying why.
    """
    value = server.strip() if isinstance(server, str) else ""
    if not value:
        raise ValueError("signaling: no server URL to connect to")

    lowered = value.lower()
    for prefix, ws_prefix in _SCHEMES:
        if lowered.startswith(prefix):
            rest = value[len(prefix):]
            if not rest.strip("/"):
                raise ValueError(f"signaling: server URL has no host: {value!r}")
            return f"{ws_prefix}{rest}".rstrip("/") + "/spreed"

    raise ValueError(
        f"signaling: server URL is not http(s) or ws(s): {value!r}"
    )


def build_hello(settings: SignalingSettings, welcome_features, request_id) -> dict:
    """The authenticating ``hello``, hello-v2 where the deployment allows it.

    v2 when the server advertised ``hello-v2`` *and* Talk minted a ``2.0``
    param, else v1 (``signaling.js:1030-1033``). Two reasons to prefer v2, and
    the second decides it: v2 is verified locally against a cached public key
    where v1 costs the signaling server a synchronous POST to Nextcloud per
    hello (``hub.go:1431-1453``) — and **the v1 ticket never expires and is
    never rotated**. ``validateSignalingTicket`` recomputes the HMAC and checks
    nothing else, the source still carries ``// TODO(fancycode): Should we
    reject tickets that are too old?`` (``lib/Config.php:851-856``), and the
    per-user secret behind it is generated once and kept forever. A leaked v1
    ticket is a permanent credential for the bot's signaling identity; a leaked
    JWT is worthless in two minutes.
    """
    if not settings.backend_url:
        raise ValueError(
            "signaling: no Nextcloud backend URL for the hello frame"
        )

    features = welcome_features if isinstance(welcome_features, (list, tuple, set)) else ()
    params_by_version = settings.hello_auth_params or {}

    version = None
    params = params_by_version.get("2.0")
    if "hello-v2" in features and isinstance(params, dict) and params:
        version = "2.0"
    else:
        params = params_by_version.get("1.0")
        if isinstance(params, dict) and params:
            version = "1.0"
            # Logged every time rather than once per process: this module holds
            # no per-process state, and a deployment on the non-expiring
            # credential should be saying so in its journal. The line names the
            # version, never the ticket.
            logger.warning(
                "signaling: no hello-v2 token available, authenticating with "
                "the v1 ticket — it does not expire and is never rotated; "
                "check that Nextcloud publishes spreed.config.signaling."
                "hello-v2-token-key"
            )

    if version is None:
        raise ValueError(
            "signaling: settings carried no usable helloAuthParams "
            f"(versions offered: {sorted(params_by_version)})"
        )

    return {
        "id": str(request_id),
        "type": "hello",
        "hello": {
            "version": version,
            # `chat-relay` and nothing else. No `internal-incall`, and no
            # `auth.type` — it defaults to `client`, and this design has no
            # other kind.
            "features": list(CLIENT_FEATURES),
            "auth": {"url": settings.backend_url, "params": params},
        },
    }


def build_resume(resume_id, request_id) -> dict:
    """Resume a dropped session. Version is hardcoded ``1.0``, always.

    Not a typo carried over from the v1 auth path: the resume frame carries no
    auth block at all, and the reference client sends ``1.0`` whatever the
    original hello used (``signaling.js:1013-1023``,
    ``api/signaling.go:488-495``). Worth trying on every reconnect and expected
    to fail past the first backoff step — a disconnected session is resumable
    for 30 seconds (``hub.go:119``).
    """
    value = resume_id.strip() if isinstance(resume_id, str) else ""
    if not value:
        raise ValueError("signaling: refusing to resume with no resume id")

    return {
        "id": str(request_id),
        "type": "hello",
        "hello": {"version": "1.0", "resumeid": value},
    }


def build_room_join(token, talk_session_id, request_id) -> dict:
    """Join one room with the Talk session id from ``participants/active``.

    Both arguments are required and a falsy one is refused. See the module
    docstring for why an absent session id does not degrade — it comes back as
    an undiagnosable ``no_such_room``, and the Talk session would never be kept
    alive even if the join had worked, since ``Room.publishActiveSessions``
    skips a session with an empty id (``room.go:1170-1172``).
    """
    room = token.strip() if isinstance(token, str) else ""
    session = talk_session_id.strip() if isinstance(talk_session_id, str) else ""
    if not room:
        raise ValueError("signaling: refusing to join with no room token")
    if not session:
        raise ValueError(
            f"signaling: refusing to join {room} with no Talk session id — "
            "the server substitutes its own and Nextcloud answers no_such_room"
        )

    return {
        "id": str(request_id),
        "type": "room",
        "room": {"roomid": room, "sessionid": session},
    }


def parse_event(frame) -> ChatEvent | None:
    """``None`` for every frame that is not a room chat event. Never raises.

    Total by contract: a watcher must not take an exception into its event
    loop because a server it does not control sent something unexpected. The
    caller counts what comes back ``None``; those counters are what `doctor`
    and the admin Health pane read.

    Three branches on the ``chat`` block, and the third is the one that keeps
    the design honest: a block that named a comment but carried nothing usable
    is reported as refresh-only, so the room is *fetched* rather than the
    message dropped.
    """
    try:
        return _parse_chat_event(frame)
    except Exception as e:  # pragma: no cover - the contract's backstop
        # Never the frame itself: it carries message text, and on the relay
        # path that is somebody's chat.
        logger.debug("signaling: unreadable event frame (%s)", type(e).__name__)
        return None


def _parse_chat_event(frame) -> ChatEvent | None:
    if not isinstance(frame, dict) or frame.get("type") != "event":
        return None

    event = frame.get("event")
    if not isinstance(event, dict):
        return None
    if event.get("target") != "room" or event.get("type") != "message":
        return None

    message = event.get("message")
    if not isinstance(message, dict):
        return None

    room_token = message.get("roomid")
    if not isinstance(room_token, str) or not room_token:
        return None

    data = message.get("data")
    if not isinstance(data, dict) or data.get("type") != "chat":
        return None

    chat = data.get("chat")
    if not isinstance(chat, dict):
        return None

    named_a_comment = "comment" in chat or "comments" in chat
    candidates = chat.get("comments")
    if not isinstance(candidates, list):
        candidates = [chat.get("comment")]
    comments = [c for c in candidates if isinstance(c, dict) and c]

    if comments:
        return ChatEvent(room_token=room_token, comments=comments, refresh_only=False)

    # Talk sends a bare refresh for a message with no visible rendering and for
    # system messages outside SYSTEM_MESSAGE_TYPE_RELAY
    # (`Listener.php:522-527`, `:570-577`); the server sends one to any client
    # that did not declare `chat-relay` (`clientsession.go:1441-1445`). Either
    # way it is a trigger. So is a comment we could not read — losing a message
    # is worse than one extra fetch.
    if chat.get("refresh") or named_a_comment:
        return ChatEvent(room_token=room_token, comments=[], refresh_only=True)

    return None


def parse_error(frame) -> SignalingError | None:
    """``None`` for every frame that is not a server error. Never raises."""
    if not isinstance(frame, dict) or frame.get("type") != "error":
        return None

    error = frame.get("error")
    if not isinstance(error, dict):
        return None

    return SignalingError(error.get("code"), error.get("message") or "")


def backoff_delay(attempt: int, *, maximum: float, base: float = 1.0,
                  rand=None) -> float:
    """Exponential backoff with jitter, in seconds, bounded by ``maximum``.

    Half the window is fixed and half is jittered, rather than the full range:
    a watcher failing at connect (bad URL, refused TLS) must not be able to
    draw a near-zero delay and restart as fast as the loop can schedule it,
    and N watchers reconnecting after the hourly ingress drop must not all
    arrive at once. ``rand`` is injectable so the bounds are testable without
    seeding the global RNG.
    """
    draw = rand if callable(rand) else random.random

    try:
        exponent = min(max(0, int(attempt)), _MAX_BACKOFF_EXPONENT)
    except (TypeError, ValueError):
        exponent = 0

    floor = max(float(base), 0.001)
    ceiling = max(float(maximum), floor)
    window = min(floor * (2 ** exponent), ceiling)
    half = window / 2.0
    return half + draw() * half
