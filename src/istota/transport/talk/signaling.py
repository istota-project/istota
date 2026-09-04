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

**Every reader here is total and every builder refuses rather than guesses.**
That split is deliberate. What arrives off the socket is decoded by something
that cannot raise into the watcher's event loop, because a server istota does
not control decides what arrives. What istota *sends* is built from values it
chose, so a value that cannot produce a correct frame is a bug in the caller
and is raised at the builder, where the cause can still be named — the
alternative is a frame the server refuses with an error that means four
different things.
"""

import logging
import math
import random
from dataclasses import dataclass
from urllib.parse import urlsplit

logger = logging.getLogger("istota.transport.talk.signaling")

# The one feature istota declares. It is what gets the relayed comment instead
# of a bare refresh (`clientsession.go:1437-1446`); the server consults the
# client's declared features and nothing else on that path — `ClientType()` is
# never read — which is what makes a user-authenticated session receive
# byte-for-byte what an internal client would.
CHAT_RELAY_FEATURE = "chat-relay"

CLIENT_FEATURES = (CHAT_RELAY_FEATURE,)

# Where Talk's signaling backend lives, relative to the Nextcloud base URL.
# This is the `auth.url` of the hello frame: the HPB matches it against its own
# `[backend]` block, and a mismatch is the fatal `invalid_backend`.
BACKEND_PATH = "/ocs/v2.php/apps/spreed/api/v3/signaling/backend"

# Recovery classes. The branches are not guessable from the code alone, which
# is why the taxonomy is data rather than a chain of `if code ==`.
RECOVERY_FRESH_TOKEN = "fresh_token"      # discard the token, re-fetch settings
RECOVERY_FRESH_SESSION = "fresh_session"  # re-POST participants/active
RECOVERY_FRESH_HELLO = "fresh_hello"      # resume failed, send a full hello
RECOVERY_FATAL = "fatal"                  # will not fix itself; stop the watcher

RETRY_FRESH_TOKEN = {"token_expired", "token_not_valid_yet"}
RETRY_FRESH_SESSION = {"no_such_room"}
RETRY_FRESH_HELLO = {"no_such_session"}
FATAL = {"invalid_backend", "invalid_client_type", "invalid_hello_version"}

# How many times one recovery may be taken before the code is treated as
# permanent. **A budget rather than a comment, because "once" is a rule a
# caller cannot express against a stateless classifier** — and the failure it
# prevents is the one this module's fatal-by-default rule exists for: a
# watcher that reconnects forever while delivering nothing, with the socket up
# and `doctor` reporting it healthy.
#
# `no_such_room` is 1 because Nextcloud returns it for a stale Talk session, a
# room that is gone and a bot that was removed, and only the first is fixed by
# a fresh `participants/active`; a second means the room is gone or istota was
# removed, and the reconciliation pass — not a retry — is what decides that.
# `token_not_valid_yet` is why the token budget is not unbounded: it means the
# two hosts' clocks disagree by more than the minute of leeway, and a re-fetch
# mints a token with a *later* `iat`, so the refusal is reproduced exactly.
RECOVERY_BUDGET = {
    RECOVERY_FRESH_TOKEN: 2,
    RECOVERY_FRESH_SESSION: 1,
    RECOVERY_FRESH_HELLO: 1,
}

# Codes that mean the deployment is misconfigured rather than that this
# connection was unlucky. A re-fetch cannot fix a clock, so the watcher warns
# naming both hosts and backs off; it does not churn credentials.
CLOCK_SKEW_CODES = {"token_not_valid_yet"}

_WS_SCHEMES = {"https": "wss", "http": "ws", "wss": "wss", "ws": "ws"}

# The first reconnect window. The spec's backoff runs "from 1s", and the delay
# is drawn from the window's upper half, so the first attempt waits 1-2s.
_BASE_WINDOW_SECONDS = 2.0

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
    # Diagnostics only. **Never build an auth params object out of this**: the
    # v1 form's `userid` comes from `hello_auth_params["1.0"]` verbatim, and
    # rebuilding it here is the mistake the module docstring warns about.
    user_id: str | None
    backend_url: str


@dataclass
class ChatEvent:
    """A room chat event, reduced to what the ingest path can act on.

    No defaults: an event that is neither a payload nor a trigger is a state
    no branch of `parse_event` produces, and a consumer reading `refresh_only`
    to decide whether to fetch would silently do nothing with one.
    """

    room_token: str
    comments: list      # empty when the server sent refresh-only
    refresh_only: bool


class SignalingError(Exception):
    """A refusal from the signaling server, carrying its recovery class.

    ``recovery`` is the class the *code* belongs to, which is the answer for a
    first occurrence. A caller holding a per-connection count asks
    ``classify_error(err.code, attempt=n)`` instead, which is where the budget
    above is applied.

    ``message`` is the server's own text. Upstream's are fixed strings and
    never echo the credential that was sent, so it is safe to log — unlike a
    frame, which this module never logs.
    """

    def __init__(self, code, message: str = ""):
        self.code = code if isinstance(code, str) else ""
        self.message = " ".join(str(message or "").split())[:200]
        self.recovery = classify_error(self.code)
        label = self.code or "unknown"
        super().__init__(f"{label}: {self.message}" if self.message else label)


def classify_error(code, *, attempt: int = 0) -> str:
    """Map a server error code to exactly one recovery class.

    ``attempt`` is how many recoveries of this kind have already been taken
    **since the last successful hello** — the watcher resets it on success, so
    a long-lived connection that meets an expired token once a day is not
    counting up to a ceiling. Past the budget the code is fatal, which is what
    makes the spec's "re-run the join once, guarded against a loop" a
    mechanism rather than a comment.

    An unrecognised code is fatal rather than retried. Retrying a refusal
    nobody understands is the failure that looks healthy: the socket
    reconnects on its backoff, `doctor` reports a live watcher, and nothing is
    ever delivered.
    """
    if not isinstance(code, str):
        return RECOVERY_FATAL

    if code in FATAL:
        return RECOVERY_FATAL

    recovery = None
    if code in RETRY_FRESH_TOKEN:
        recovery = RECOVERY_FRESH_TOKEN
    elif code in RETRY_FRESH_SESSION:
        recovery = RECOVERY_FRESH_SESSION
    elif code in RETRY_FRESH_HELLO:
        recovery = RECOVERY_FRESH_HELLO

    if recovery is None:
        return RECOVERY_FATAL

    try:
        taken = max(0, int(attempt))
    except (TypeError, ValueError, OverflowError):
        taken = 0
    if taken >= RECOVERY_BUDGET.get(recovery, 1):
        return RECOVERY_FATAL

    return recovery


def is_clock_skew(code) -> bool:
    """Does this refusal mean the two hosts disagree about the time?

    Separate from the recovery class on purpose: the remedy is the same
    (discard the token) but the *diagnosis* is not, and only this one is an
    operator problem that a retry cannot fix.
    """
    return isinstance(code, str) and code in CLOCK_SKEW_CODES


def parse_settings(data, *, nextcloud_url: str) -> SignalingSettings:
    """Read the OCS settings payload. Tolerates anything; validates nothing.

    A payload it cannot read yields empty fields rather than an exception.
    ``hpb_unavailable_reason`` is what turns that into a sentence, so a caller
    never has to tell "Talk is in internal mode" from "the settings call
    returned nothing readable" by comparing against the empty string.
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


def signaling_mode_reason(mode) -> str | None:
    """Why a deployment reporting this signaling mode has no HPB, or ``None``.

    The mode half of :func:`hpb_unavailable_reason`, reachable on its own
    because **two readers get at the same fact through different payloads**.
    The runtime reads ``GET /v3/signaling/settings``, which also names a
    server URL and so has a third question to ask. ``doctor``'s
    ``talk.signaling_auth`` reads ``/cloud/capabilities``, which names no URL
    and mints no token: doctor runs on a scheduler interval and from the admin
    Health pane, so a check asking Talk for a credential on every dashboard
    load would mint one per page view for nothing. That saving is per check
    rather than per run — its sibling ``talk.signaling_reachable`` still has to
    fetch the settings to discover the HPB URL unless an operator configured
    one — so what the split buys is that the *configuration* question is
    answerable with no credential at all, on a deployment where the settings
    call is the thing that is failing.

    Three states, not two, and collapsing them is how a startup refusal ends
    up naming the wrong cause. ``"internal"`` means Talk has no signaling
    server registered — the deployment cannot do what it was configured to do.
    Anything *unreadable* means the call answered with nothing this client
    could make sense of, which is a different fault with a different fix, and
    it is what a plain ``== "internal"`` test would let through.
    """
    if not isinstance(mode, str) or not mode:
        return (
            "Talk reported no signaling mode — the call returned nothing this "
            "client could read"
        )
    if mode == "internal":
        return (
            "Talk is in internal signaling mode: no high-performance backend "
            "is registered with it (occ talk:signaling:list)"
        )
    return None


def hpb_unavailable_reason(settings: SignalingSettings) -> str | None:
    """``None`` when this deployment has an HPB to connect to, else why not.

    :func:`signaling_mode_reason` answers the mode question — the same
    predicate the ``talk.signaling_auth`` check reads through capabilities, so
    the startup refusal and the check cannot come to different conclusions
    about whether a deployment is configured. What is added here is the
    question only the settings payload can answer: an ``external`` deployment
    that named no server URL is configured and still unusable.
    """
    if not isinstance(settings, SignalingSettings):
        return "Talk signaling settings could not be read"

    reason = signaling_mode_reason(settings.signaling_mode)
    if reason is not None:
        return reason
    if not settings.server:
        return (
            f"Talk reported {settings.signaling_mode} signaling but named no "
            "server URL"
        )
    return None


class SignalingUnavailable(RuntimeError):
    """``[talk.signaling] enabled = true`` on a deployment that cannot do it.

    A refusal, not a degradation, and the two ways to reach it are the two
    ways an operator can ask for push on a deployment that has none: Talk with
    no high-performance backend registered, and the ``websockets`` library
    absent. Neither falls back to the poller.

    Both predicates below are raised from the supervisor's start-up gate, which
    is where the daemon decides whether to run watchers or the poll loop.
    ``doctor``'s ``talk.signaling_reachable`` and ``talk.signaling_auth`` are
    the operator's warning that this is about to happen; they report, and
    refuse nothing.

    Falling back would be the worse failure and it is worth naming why, since
    "degrade gracefully" is the reflex. The poller is a *capability floor* for
    a deployment with no HPB, not a redundant branch — so a daemon that silently
    took it would report every signaling counter healthy for want of any
    watchers to be unhealthy, while the operator who set ``enabled = true``
    believes messages arrive within a second and they arrive within a poll
    cycle. There is no third failure mode, deliberately: a missing *secret*
    used to be one, and is not, because this design has no secret.
    """


def require_websockets():
    """The WebSocket client library, or a refusal naming the extra.

    Imported here rather than at module scope for the reason every heavy
    import in ``src/`` is inside a function — nothing that merely reads a frame
    should pay for it — and because that is what lets a deployment with
    signaling off run without the dependency at all.
    """
    try:
        import websockets
    except ImportError as exc:
        raise SignalingUnavailable(
            "[talk.signaling] enabled = true but the websockets library is "
            "not installed, so no connection can be opened. Install the "
            "signaling extra (`uv sync --extra signaling`, or "
            "`pip install 'istota[signaling]'`), or set "
            "[talk.signaling] enabled = false to keep the Talk poller."
        ) from exc
    return websockets


def require_hpb(settings: "SignalingSettings") -> None:
    """Refuse when this deployment has no high-performance backend.

    Phrased entirely in terms of :func:`hpb_unavailable_reason`, so the
    refusal and ``doctor``'s ``talk.signaling_auth`` cannot come to different
    conclusions about the same deployment — the check is the operator's
    warning that this is about to happen, and a second copy of the predicate
    is how the two start disagreeing.
    """
    reason = hpb_unavailable_reason(settings)
    if reason is None:
        return None

    # One remedy per reason. `hpb_unavailable_reason` answers three different
    # questions, and only one of them is fixed by registering a server: the
    # other two are a Nextcloud that answered with nothing readable, and a
    # server that is registered and named no URL. A single "run
    # occ talk:signaling:add" sentence would be wrong twice out of three.
    mode = getattr(settings, "signaling_mode", "") or ""
    if mode == "internal":
        remedy = (
            "Register a signaling server with Talk (occ talk:signaling:add, "
            "then occ talk:signaling:list to confirm)"
        )
    elif not mode:
        remedy = (
            "Check that Nextcloud is reachable and that the bot account can "
            "read GET /ocs/v2.php/apps/spreed/api/v3/signaling/settings"
        )
    else:
        remedy = (
            "Talk has a signaling server registered but named no URL for it; "
            "check occ talk:signaling:list, or set [talk.signaling] url to the "
            "route this host should take"
        )

    raise SignalingUnavailable(
        f"[talk.signaling] enabled = true but {reason}. {remedy}, or set "
        "[talk.signaling] enabled = false to keep the Talk poller."
    )


def parse_welcome(frame) -> tuple[str, ...]:
    """The server's advertised feature list. ``()`` for anything unreadable.

    Beside ``parse_error`` rather than left to the caller, and the reason is
    the one asymmetry worth naming: getting this extraction wrong is silent.
    Handing ``build_hello`` a dict instead of the list inside it would select
    the v1 ticket — which never expires and is never rotated — on a connection
    that then works perfectly, so no counter, no reconnect and no `doctor`
    check would ever show it. ``build_hello`` therefore refuses a shape it
    cannot read rather than treating it as "no features".
    """
    if not isinstance(frame, dict):
        return ()

    welcome = frame.get("welcome")
    if not isinstance(welcome, dict):
        return ()

    features = welcome.get("features")
    if not isinstance(features, (list, tuple)):
        return ()

    return tuple(f for f in features if isinstance(f, str))


def websocket_url(server) -> str:
    """`https`->`wss`, `http`->`ws`, strip trailing slash, append `/spreed`.

    The rule Talk's own client uses (``signaling.js:1008-1010``). A value it
    cannot turn into a WebSocket URL is refused rather than mangled: this is
    the connect target and an operator may have typed it, so
    ``"" -> "/spreed"`` or a stray query string appended before ``/spreed``
    would be a connection to nothing with no message saying why.
    """
    value = server.strip() if isinstance(server, str) else ""
    if not value:
        raise ValueError("signaling: no server URL to connect to")

    # Checked on the raw value: `urlsplit` follows the WHATWG rule and strips
    # ASCII tab and newline *before* parsing, so a tab inside the host would
    # otherwise be silently removed rather than refused — which is the one
    # thing this function says it does not do.
    if any(c.isspace() for c in value):
        raise ValueError(
            f"signaling: server URL contains whitespace: {value!r}"
        )

    parts = urlsplit(value)
    ws_scheme = _WS_SCHEMES.get(parts.scheme.lower())
    if not ws_scheme:
        raise ValueError(
            f"signaling: server URL is not http(s) or ws(s): {value!r}"
        )
    if not parts.netloc:
        raise ValueError(f"signaling: server URL has no usable host: {value!r}")
    if parts.query or parts.fragment:
        raise ValueError(
            "signaling: server URL carries a query or fragment, which cannot "
            f"be extended with /spreed: {value!r}"
        )

    path = parts.path.rstrip("/")
    return f"{ws_scheme}://{parts.netloc}{path}/spreed"


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

    ``welcome_features`` is what ``parse_welcome`` returned. A shape this
    cannot read is refused rather than read as an empty list, because the
    fallback that would follow is the credential above.
    """
    if not settings.backend_url:
        raise ValueError(
            "signaling: no Nextcloud backend URL for the hello frame"
        )
    if not isinstance(welcome_features, (list, tuple, set, frozenset)):
        raise ValueError(
            "signaling: welcome features must be a sequence from "
            f"parse_welcome, got {type(welcome_features).__name__}"
        )

    params_by_version = settings.hello_auth_params or {}

    version = None
    params = params_by_version.get("2.0")
    if "hello-v2" in welcome_features and isinstance(params, dict) and params:
        version = "2.0"
    else:
        params = params_by_version.get("1.0")
        if isinstance(params, dict) and params:
            version = "1.0"
            # Logged on every hello rather than once: this module holds no
            # per-process state by design, so deduping a deployment that is
            # permanently on v1 (N rooms x every reconnect) belongs to the
            # supervisor, which has somewhere to keep the flag. The line names
            # the version, never the ticket.
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
    except Exception as e:
        # Every access below is isinstance-guarded, so no JSON-decodable frame
        # reaches this. It is here for what a JSON decoder is not the only
        # source of — a mapping whose own `get` raises — and it is driven by a
        # test, because an unreachable backstop nothing exercises is a claim
        # rather than a contract.
        #
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


def backoff_delay(attempt: int, *, maximum: float,
                  base: float = _BASE_WINDOW_SECONDS, rand=None) -> float:
    """Exponential backoff with jitter, in seconds, bounded by ``maximum``.

    The window doubles from ``base`` and the delay is drawn from its upper
    half, so a first reconnect waits 1-2s and no attempt can draw a near-zero
    delay: a watcher failing at connect (bad URL, refused TLS) must not
    restart as fast as the loop can schedule it. Half-jitter rather than full
    keeps that floor while still spreading a burst — though what bounds the
    spread at the first attempt is the window itself, one second wide, not the
    jitter fraction. N watchers coming back from the hourly ingress drop are
    therefore spread across a second, which is what the per-room
    ``participants/active`` POST can absorb; it is not a substitute for a
    startup stagger if that ever proves too tight.

    ``rand`` is injectable so the bounds are testable without seeding the
    global RNG. Nothing here raises: ``maximum`` comes from operator config,
    and a watcher's reconnect must not die on a bad value — a nonsense one
    falls back to the base window rather than removing the ceiling, which is
    what ``min(window, nan)`` would silently do.
    """
    draw = rand if callable(rand) else random.random

    try:
        exponent = min(max(0, int(attempt)), _MAX_BACKOFF_EXPONENT)
    except (TypeError, ValueError, OverflowError):
        exponent = 0

    try:
        floor = float(base)
    except (TypeError, ValueError):
        floor = _BASE_WINDOW_SECONDS
    if not math.isfinite(floor) or floor <= 0:
        floor = _BASE_WINDOW_SECONDS

    try:
        ceiling = float(maximum)
    except (TypeError, ValueError):
        ceiling = floor
    if not math.isfinite(ceiling):
        ceiling = floor
    ceiling = max(ceiling, floor)

    window = min(floor * (2 ** exponent), ceiling)
    half = window / 2.0
    return half + draw() * half


# --- Where doctor reads the supervisor's counters from ---------------------
#
# A process-local registration rather than an import, and that is the whole
# point of it. The supervisor runs in the scheduler daemon; `doctor` also runs
# in the web process, in the CLI and behind `!check`, where there is no
# supervisor and never will be. An importable singleton would make those
# processes report every watcher down — a page for a process that was never
# supposed to have any — where "nothing is registered here" is the honest
# answer and the one `talk.signaling_watchers` skips on.
#
# A callable rather than the supervisor object, so this module states no
# opinion about the supervisor's shape; Stage 3 registers a bound `stats`.
_STATS_SOURCE = None


def set_stats_source(source) -> None:
    """Register what ``read_stats`` should call. The supervisor's own hook.

    A non-callable is refused loudly rather than quietly: registering the
    stats *dict* instead of the bound method is the obvious mistake, and
    swallowing it leaves ``talk.signaling_watchers`` reporting "no supervisor
    in this process" for the life of the daemon with nothing anywhere saying
    why.
    """
    global _STATS_SOURCE
    if source is not None and not callable(source):
        logger.warning(
            "signaling: stats source must be callable, got %s; "
            "talk.signaling_watchers will report no supervisor",
            type(source).__name__,
        )
        _STATS_SOURCE = None
        return
    _STATS_SOURCE = source


def clear_stats_source() -> None:
    """Unregister. Called on supervisor shutdown, and by test teardown."""
    global _STATS_SOURCE
    _STATS_SOURCE = None


def read_stats() -> dict | None:
    """The supervisor's counters, or ``None`` when this process has none.

    **The shape the supervisor owes this**, since ``doctor``'s
    ``talk.signaling_watchers`` is the only reader and every key it wants is
    one the supervisor alone can produce:

    ``watchers``       int — rooms the supervisor means to be watching
    ``connected``      int — watchers with a live, joined session right now
    ``disconnected``   list[str] — the Talk tokens behind the gap, a fresh
                       list per call; may be empty even while
                       ``connected < watchers``, and the reader treats the
                       counts as authoritative for that reason
    ``rooms_behind``   int — rooms whose ``lastMessage.id`` was ahead of their
                       cursor at the last reconciliation, which is the one
                       number that distinguishes a stream that is delivering
                       from one the safety-net fetch is carrying. Rooms with
                       **no** cursor to compare against are not in it: that
                       state is permanent for a room nobody has spoken in, and
                       a diagnostic with a permanent false positive is worse
                       than none
    ``never_connected`` list[str] — Talk tokens whose watcher the supervisor
                       cancelled for never once having connected, and for which
                       none has connected since. **Not a subset of
                       ``disconnected``** and not comparable to it: that one is
                       a moment, which a healthy watcher joins for a second
                       between reconnects, while this is a room nothing has
                       ever delivered for. It reads identically to a healthy
                       room on every other key here, which is the whole reason
                       it is a key
    ``stale_dirty``    int — rooms owed a triggered fetch for longer than one
                       ``room_sync_interval``. A fetch that raises preserves
                       the room's dirty bit rather than retrying into a
                       transaction that just failed, so this is the only
                       counter that can show one nothing has come back to:
                       the watcher is connected and every other number is
                       healthy

    A missing key reads as ``0`` or empty rather than as an error: this is a
    diagnostic, and half an answer beats none.

    Never raises and never propagates a shape it cannot use. The caller is a
    ``doctor`` check, and a diagnostic that fails on its own instrument
    reports the wrong subsystem: a supervisor mid-restart raising out of
    ``stats()`` is not a signaling outage, and neither is one that returns
    something that is not a mapping.
    """
    source = _STATS_SOURCE
    if source is None:
        return None
    try:
        stats = source()
    except Exception as e:  # noqa: BLE001 — a diagnostic must not raise
        logger.debug("signaling: stats source raised (%s)", type(e).__name__)
        return None
    if not isinstance(stats, dict):
        return None
    # A copy, because the supervisor is on the loop thread and the reader is
    # not: handing back its live mapping means a check can be iterating a list
    # the supervisor is rewriting. Shallow is enough for the scalars; the
    # sequence values are copied where they are read, since only the reader
    # knows which ones it walks.
    return dict(stats)
