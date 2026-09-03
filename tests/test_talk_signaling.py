"""Protocol-level tests for the Talk signaling client (Stage 1).

Everything here runs against the wire-format helpers alone: no socket, no
daemon, no Nextcloud. That is the point of the module being a leaf — the
frames istota puts on the wire are decidable without any of it.

The load-bearing assertions, in the order the spec argues them:

- istota authenticates as its own Nextcloud user. ``build_hello`` must never
  produce an internal-client frame, and must never declare ``internal-incall``.
- ``auth.params`` is the ``helloAuthParams`` sub-object passed through, not
  rebuilt field by field.
- A room join without a Talk session id is refused rather than serialised,
  because the server substitutes its own public session id for an empty one
  and the failure comes back as an undiagnosable ``no_such_room``.
- ``parse_event`` never raises.
- The three per-connection credentials never reach a log record.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from istota.config import Config, NextcloudConfig
from istota.talk import TalkClient
from istota.transport.talk import signaling


# --- fixtures shared by several cases -------------------------------------

JWT = "eyJhbGciOiJFUzI1NiJ9.THIS-IS-THE-HELLO-V2-JWT.sig"
TICKET = "THIS-IS-THE-V1-TICKET-abcdef123456"
RESUME_ID = "THIS-IS-THE-RESUME-ID-987654"

NC_URL = "https://nc.example.com"
BACKEND_URL = f"{NC_URL}/ocs/v2.php/apps/spreed/api/v3/signaling/backend"


def _settings(*, v2=True, v1=True, server="https://hpb.example.com/standalone-signaling"):
    params = {}
    if v1:
        params["1.0"] = {"userid": "bot", "ticket": TICKET}
    if v2:
        params["2.0"] = {"token": JWT}
    return signaling.SignalingSettings(
        server=server,
        signaling_mode="external",
        hello_auth_params=params,
        user_id="bot",
        backend_url=BACKEND_URL,
    )


def _talk_response(status=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    resp.json.return_value = json_body if json_body is not None else {}
    return resp


def _wired_talk_client(json_body=None):
    client = TalkClient(
        Config(nextcloud=NextcloudConfig(
            url=NC_URL, username="bot", app_password="secret",
        )),
    )
    http = AsyncMock()
    http.get.return_value = _talk_response(json_body=json_body)
    http.post.return_value = _talk_response(json_body=json_body)
    client._client = http
    return client, http


def _chat_frame(chat, *, roomid="abc123"):
    return {
        "type": "event",
        "event": {
            "target": "room",
            "type": "message",
            "message": {
                "roomid": roomid,
                "data": {"type": "chat", "chat": chat},
            },
        },
    }


# --- websocket_url ---------------------------------------------------------


class TestWebsocketUrl:
    @pytest.mark.parametrize(
        "server,expected",
        [
            # The rule Talk's own client uses (signaling.js:1008-1010).
            ("https://h/x/", "wss://h/x/spreed"),
            ("https://h/x", "wss://h/x/spreed"),
            ("http://h/x/", "ws://h/x/spreed"),
            ("http://h/x", "ws://h/x/spreed"),
            ("wss://h/x", "wss://h/x/spreed"),
            ("ws://h/x/", "ws://h/x/spreed"),
            ("https://h", "wss://h/spreed"),
            ("  https://h/x/  ", "wss://h/x/spreed"),
        ],
    )
    def test_table(self, server, expected):
        assert signaling.websocket_url(server) == expected

    @pytest.mark.parametrize("server", ["", "   ", None])
    def test_empty_is_refused(self, server):
        # An empty server would build "/spreed" and connect to nothing. A
        # connect target is a boundary: refuse rather than emit a broken URL.
        with pytest.raises(ValueError):
            signaling.websocket_url(server)

    @pytest.mark.parametrize(
        "server", ["hpb.example.com/x", "ftp://h/x", "//h/x"],
    )
    def test_unknown_scheme_is_refused(self, server):
        with pytest.raises(ValueError):
            signaling.websocket_url(server)

    @pytest.mark.parametrize("server", ["https://", "wss://", "http:///"])
    def test_a_url_with_no_host_is_refused(self, server):
        with pytest.raises(ValueError):
            signaling.websocket_url(server)


# --- parse_settings --------------------------------------------------------


class TestParseSettings:
    def test_reads_the_ocs_payload(self):
        data = {
            "signalingMode": "external",
            "userId": "bot",
            "server": "https://hpb.example.com/sig",
            "helloAuthParams": {"1.0": {"userid": "bot", "ticket": TICKET},
                                "2.0": {"token": JWT}},
        }

        out = signaling.parse_settings(data, nextcloud_url=NC_URL + "/")

        assert out.server == "https://hpb.example.com/sig"
        assert out.signaling_mode == "external"
        assert out.user_id == "bot"
        assert out.backend_url == BACKEND_URL
        # Verbatim: the sub-objects are the ones Nextcloud sent, not copies
        # rebuilt field by field.
        assert out.hello_auth_params["2.0"] is data["helloAuthParams"]["2.0"]

    @pytest.mark.parametrize("data", [None, [], "nope", {}, {"helloAuthParams": 7}])
    def test_tolerates_a_payload_it_cannot_read(self, data):
        out = signaling.parse_settings(data, nextcloud_url=NC_URL)

        assert out.hello_auth_params == {}
        assert out.server == ""


# --- build_hello -----------------------------------------------------------


class TestBuildHelloVersionSelection:
    """The four-cell table: `hello-v2` advertised x a `2.0` param present.

    The rule is the reference client's (signaling.js:1030-1033): v2 only when
    both hold. v1 is a compatibility path for a Nextcloud publishing no
    hello-v2 token key, and it carries a credential that never expires
    (D3a), so it is never taken while v2 is available.
    """

    def test_feature_and_param_gives_v2(self):
        frame = signaling.build_hello(_settings(), ["hello-v2", "chat-relay"], "1")

        assert frame["hello"]["version"] == "2.0"
        assert frame["hello"]["auth"]["params"] == {"token": JWT}

    def test_feature_without_param_gives_v1(self):
        frame = signaling.build_hello(
            _settings(v2=False), ["hello-v2", "chat-relay"], "1",
        )

        assert frame["hello"]["version"] == "1.0"
        assert frame["hello"]["auth"]["params"] == {"userid": "bot", "ticket": TICKET}

    def test_param_without_feature_gives_v1(self):
        frame = signaling.build_hello(_settings(), ["chat-relay"], "1")

        assert frame["hello"]["version"] == "1.0"

    def test_neither_gives_v1(self):
        frame = signaling.build_hello(_settings(v2=False), ["chat-relay"], "1")

        assert frame["hello"]["version"] == "1.0"

    def test_no_params_at_all_is_refused(self):
        # Nothing to authenticate with. Connecting anyway would produce an
        # opaque server-side rejection.
        with pytest.raises(ValueError):
            signaling.build_hello(
                _settings(v1=False, v2=False), ["hello-v2"], "1",
            )


class TestBuildHelloShape:
    def test_auth_params_is_the_sub_object_verbatim(self):
        # Object identity, not field-by-field equality: rebuilding the params
        # is the plausible wrong implementation, and Talk is free to add a
        # field to either form (signaling.js:1046-1058).
        settings = _settings()
        settings.hello_auth_params["2.0"]["someFutureField"] = "keep me"

        frame = signaling.build_hello(settings, ["hello-v2"], "1")

        assert frame["hello"]["auth"]["params"] is settings.hello_auth_params["2.0"]

    def test_v1_auth_params_is_also_verbatim(self):
        settings = _settings(v2=False)

        frame = signaling.build_hello(settings, [], "1")

        assert frame["hello"]["auth"]["params"] is settings.hello_auth_params["1.0"]

    def test_declares_chat_relay(self):
        # Without it the server nulls the comment and sends a bare refresh
        # (clientsession.go:1441-1445).
        frame = signaling.build_hello(_settings(), ["hello-v2"], "1")

        assert frame["hello"]["features"] == ["chat-relay"]

    def test_does_not_declare_internal_incall(self):
        # `internal-incall` suppresses a phantom in-call flag that the server
        # sets only on *internal* sessions (clientsession.go:151-156). This
        # design has none, so sending it would be cargo from the rejected
        # internal-client path — which is exactly the kind of thing that gets
        # copied in from an upstream example and then rationalised (D3).
        frame = signaling.build_hello(_settings(), ["hello-v2"], "1")

        assert "internal-incall" not in frame["hello"]["features"]

    def test_never_authenticates_as_an_internal_client(self):
        # The whole security premise (D1): istota's reach is its Nextcloud
        # user's reach. An internal client can join any room on the instance
        # (hub.go:1922-1927) and is invisible in the participant list.
        for features in (["hello-v2"], [], ["hello-v2", "chat-relay"]):
            for settings in (_settings(), _settings(v2=False)):
                frame = signaling.build_hello(settings, features, "1")

                assert "type" not in frame["hello"]["auth"]
                assert "internalsecret" not in str(frame)
                assert "hmac" not in str(frame).lower()

    def test_carries_the_backend_url_and_request_id(self):
        frame = signaling.build_hello(_settings(), ["hello-v2"], "7")

        assert frame["type"] == "hello"
        assert frame["id"] == "7"
        assert frame["hello"]["auth"]["url"] == BACKEND_URL

    def test_a_missing_backend_url_is_refused(self):
        settings = signaling.SignalingSettings(
            server="https://h", signaling_mode="external",
            hello_auth_params={"2.0": {"token": JWT}}, user_id="bot",
            backend_url="",
        )

        with pytest.raises(ValueError):
            signaling.build_hello(settings, ["hello-v2"], "1")

    def test_a_non_dict_param_is_refused(self):
        settings = signaling.SignalingSettings(
            server="https://h", signaling_mode="external",
            hello_auth_params={"2.0": "not-an-object"}, user_id="bot",
            backend_url=BACKEND_URL,
        )

        with pytest.raises(ValueError):
            signaling.build_hello(settings, ["hello-v2"], "1")


# --- build_resume ----------------------------------------------------------


class TestBuildResume:
    def test_shape(self):
        # The version is hardcoded to 1.0 regardless of what the original
        # hello used (signaling.js:1013-1023, api/signaling.go:488-495), and
        # the frame carries no auth block at all.
        frame = signaling.build_resume(RESUME_ID, "3")

        assert frame == {
            "id": "3",
            "type": "hello",
            "hello": {"version": "1.0", "resumeid": RESUME_ID},
        }

    @pytest.mark.parametrize("resume_id", ["", None, "   "])
    def test_a_falsy_resume_id_is_refused(self, resume_id):
        with pytest.raises(ValueError):
            signaling.build_resume(resume_id, "3")


# --- build_room_join -------------------------------------------------------


class TestBuildRoomJoin:
    def test_shape(self):
        frame = signaling.build_room_join("abc123", "talk-session-1", "2")

        assert frame == {
            "id": "2",
            "type": "room",
            "room": {"roomid": "abc123", "sessionid": "talk-session-1"},
        }

    @pytest.mark.parametrize("session_id", ["", None, "   "])
    def test_a_falsy_talk_session_id_is_refused(self, session_id):
        # Omitting the session id does NOT degrade to a user-scoped join. The
        # signaling server substitutes its own public session id for an empty
        # one (hub.go:1937-1943), so Nextcloud takes the
        # getParticipantBySession branch (SignalingController.php:891-895),
        # finds nothing, and answers with the generic, undiagnosable
        # `no_such_room`. Refuse to build the frame instead.
        with pytest.raises(ValueError):
            signaling.build_room_join("abc123", session_id, "2")

    @pytest.mark.parametrize("token", ["", None, "   "])
    def test_a_falsy_room_token_is_refused(self, token):
        with pytest.raises(ValueError):
            signaling.build_room_join(token, "talk-session-1", "2")


# --- parse_event -----------------------------------------------------------


class TestParseEvent:
    def test_relayed_single_comment(self):
        comment = {"id": 42, "message": "hi", "actorId": "alice"}

        event = signaling.parse_event(_chat_frame({"comment": comment}))

        assert event.room_token == "abc123"
        assert event.comments == [comment]
        assert event.refresh_only is False

    def test_relayed_comments_batch(self):
        one = {"id": 42}
        two = {"id": 43}

        event = signaling.parse_event(_chat_frame({"comments": [one, two]}))

        assert event.comments == [one, two]
        assert event.refresh_only is False

    def test_refresh_only(self):
        # Talk sends this alone for a message with no visible rendering and
        # for system messages outside SYSTEM_MESSAGE_TYPE_RELAY
        # (Listener.php:522-527, :570-577). Always a trigger.
        event = signaling.parse_event(_chat_frame({"refresh": True}))

        assert event.room_token == "abc123"
        assert event.comments == []
        assert event.refresh_only is True

    def test_an_unreadable_comment_still_triggers_a_fetch(self):
        # A chat block that named a comment but carried nothing usable is a
        # message we would otherwise lose. Fetching is the safe direction.
        event = signaling.parse_event(_chat_frame({"comment": "not-an-object"}))

        assert event.refresh_only is True
        assert event.comments == []

    @pytest.mark.parametrize(
        "frame",
        [
            # Not a chat event: every one of these is ignored and counted.
            {"type": "event", "event": {"target": "room", "type": "join"}},
            {"type": "event", "event": {"target": "room", "type": "participants"}},
            {"type": "event", "event": {"target": "roomlist", "type": "update"}},
            {"type": "hello", "hello": {"sessionid": "s", "resumeid": RESUME_ID}},
            {"type": "welcome", "welcome": {"features": ["chat-relay"]}},
            {"type": "error", "error": {"code": "no_such_room"}},
            {"type": "event", "event": {"target": "room", "type": "message",
                                        "message": {"roomid": "abc123"}}},
            {"type": "event", "event": {"target": "room", "type": "message",
                                        "message": {"roomid": "abc123",
                                                    "data": {"type": "recording"}}}},
            # A chat block with nothing actionable in it.
            _chat_frame({}),
            _chat_frame({"refresh": False}),
            # No room to act on.
            _chat_frame({"refresh": True}, roomid=""),
            _chat_frame({"refresh": True}, roomid=None),
        ],
    )
    def test_returns_none_for_everything_that_is_not_a_room_chat_event(self, frame):
        assert signaling.parse_event(frame) is None

    @pytest.mark.parametrize(
        "frame",
        [
            None, [], "", 0, 3.5, {"type": "event"},
            {"type": "event", "event": None},
            {"type": "event", "event": {"target": "room", "type": "message",
                                        "message": None}},
            {"type": "event", "event": {"target": "room", "type": "message",
                                        "message": {"data": {"type": "chat",
                                                             "chat": None}}}},
            {"type": "event", "event": {"target": "room", "type": "message",
                                        "message": {"roomid": ["a"],
                                                    "data": {"type": "chat",
                                                             "chat": {"refresh": True}}}}},
            _chat_frame({"comments": "not-a-list"}),
            _chat_frame({"comments": [None, 7]}),
        ],
    )
    def test_never_raises(self, frame):
        # The contract is total: a frame it cannot read is dropped, never an
        # exception into the watcher's event loop.
        signaling.parse_event(frame)

    def test_a_junk_comment_in_a_batch_does_not_take_the_good_ones_with_it(self):
        good = {"id": 42}

        event = signaling.parse_event(_chat_frame({"comments": [good, None, 7]}))

        assert event.comments == [good]


# --- error taxonomy --------------------------------------------------------


class TestErrorTaxonomy:
    def test_the_recovery_classes_are_disjoint(self):
        sets = [
            signaling.RETRY_FRESH_TOKEN,
            signaling.RETRY_FRESH_SESSION,
            signaling.RETRY_FRESH_HELLO,
            signaling.FATAL,
        ]
        seen = set()
        for group in sets:
            assert not (seen & group), f"code in two recovery classes: {seen & group}"
            seen |= group

    @pytest.mark.parametrize(
        "code,recovery",
        [
            ("token_expired", signaling.RECOVERY_FRESH_TOKEN),
            ("token_not_valid_yet", signaling.RECOVERY_FRESH_TOKEN),
            ("no_such_room", signaling.RECOVERY_FRESH_SESSION),
            ("no_such_session", signaling.RECOVERY_FRESH_HELLO),
            ("invalid_backend", signaling.RECOVERY_FATAL),
            ("invalid_client_type", signaling.RECOVERY_FATAL),
            ("invalid_hello_version", signaling.RECOVERY_FATAL),
        ],
    )
    def test_each_code_maps_to_exactly_one_class(self, code, recovery):
        assert signaling.classify_error(code) == recovery

    @pytest.mark.parametrize(
        "code", ["", None, "something_upstream_added_last_tuesday", 7],
    )
    def test_an_unknown_code_is_fatal_rather_than_silently_retried(self, code):
        # Retrying an unrecognised refusal forever is the failure that looks
        # healthy: the socket reconnects, doctor reports a watcher, and
        # nothing is delivered.
        assert signaling.classify_error(code) == signaling.RECOVERY_FATAL

    def test_signaling_error_carries_its_code(self):
        err = signaling.SignalingError("no_such_room", "the message")

        assert err.code == "no_such_room"
        assert err.recovery == signaling.RECOVERY_FRESH_SESSION
        assert "no_such_room" in str(err)

    def test_parse_error_reads_a_server_error_frame(self):
        frame = {"type": "error", "error": {"code": "token_expired",
                                            "message": "The token is expired"}}

        err = signaling.parse_error(frame)

        assert err.code == "token_expired"
        assert err.recovery == signaling.RECOVERY_FRESH_TOKEN

    @pytest.mark.parametrize(
        "frame",
        [None, {}, {"type": "welcome"}, {"type": "error"}, {"type": "error", "error": None}],
    )
    def test_parse_error_returns_none_for_anything_that_is_not_an_error(self, frame):
        assert signaling.parse_error(frame) is None

    def test_parse_error_never_raises_on_a_malformed_error_frame(self):
        err = signaling.parse_error({"type": "error", "error": {"code": 7}})

        assert err.recovery == signaling.RECOVERY_FATAL


# --- backoff ---------------------------------------------------------------


class TestBackoff:
    def test_is_bounded_by_the_maximum(self):
        for attempt in range(0, 40):
            for r in (0.0, 0.5, 1.0):
                delay = signaling.backoff_delay(
                    attempt, maximum=60.0, rand=lambda: r,
                )
                assert 0 < delay <= 60.0

    def test_never_returns_a_hot_loop(self):
        # A watcher failing at connect must not restart as fast as the loop
        # can schedule it.
        for attempt in range(0, 10):
            delay = signaling.backoff_delay(attempt, maximum=60.0, rand=lambda: 0.0)
            assert delay >= 0.5

    def test_grows(self):
        flat = [
            signaling.backoff_delay(a, maximum=60.0, rand=lambda: 1.0)
            for a in range(0, 8)
        ]
        assert flat == sorted(flat)
        assert flat[0] < flat[-1]
        assert flat[-1] == 60.0

    def test_is_jittered(self):
        low = signaling.backoff_delay(5, maximum=60.0, rand=lambda: 0.0)
        high = signaling.backoff_delay(5, maximum=60.0, rand=lambda: 1.0)

        assert low < high

    def test_default_uses_real_randomness(self):
        values = {signaling.backoff_delay(6, maximum=60.0) for _ in range(20)}

        assert len(values) > 1

    def test_a_negative_attempt_is_clamped(self):
        assert signaling.backoff_delay(-5, maximum=60.0, rand=lambda: 1.0) == \
            signaling.backoff_delay(0, maximum=60.0, rand=lambda: 1.0)

    def test_a_nonsense_maximum_is_clamped_to_the_base(self):
        assert signaling.backoff_delay(3, maximum=0, rand=lambda: 1.0) > 0


# --- the three per-connection credentials ----------------------------------


class TestNoCredentialReachesALogRecord:
    """The hello-v2 JWT, the v1 ticket and the resumeid never reach a log line
    at any level.

    None of the three is a config value, so there is nothing for
    `admin_config_view` to redact and the rule lives with the protocol module
    instead. The resumeid is the one most easily missed: it authenticates a
    full session resume, including room membership, for its 30-second window.

    Asserted on the *values*, which are fixtures this test chose.
    """

    def test_nothing_this_module_logs_carries_a_credential(self, caplog):
        caplog.set_level(logging.DEBUG)

        settings = _settings()
        signaling.build_hello(settings, ["hello-v2"], "1")
        # The v1 fallback is logged (D3a says to log when it is taken), which
        # is the log line most likely to carry a ticket.
        signaling.build_hello(_settings(v2=False), ["hello-v2"], "1")
        signaling.build_hello(_settings(v2=False), [], "1")
        signaling.build_resume(RESUME_ID, "2")
        signaling.build_room_join("abc123", "talk-session-1", "3")
        signaling.parse_event(_chat_frame({"comment": {"id": 1}}))
        signaling.parse_event({"type": "hello", "hello": {"resumeid": RESUME_ID}})
        signaling.parse_error({"type": "error", "error": {"code": "token_expired",
                                                          "message": JWT}})
        signaling.parse_settings(
            {"helloAuthParams": {"2.0": {"token": JWT}}, "server": "https://h"},
            nextcloud_url=NC_URL,
        )

        blob = "\n".join(
            [r.getMessage() for r in caplog.records]
            + [str(r.args) for r in caplog.records]
        )
        for secret in (JWT, TICKET, RESUME_ID):
            assert secret not in blob

    def test_the_v1_fallback_is_logged_at_all(self):
        # The control for the test above: a log assertion over a module that
        # logs nothing proves nothing. D3a requires the v1 path to be visible
        # because the ticket behind it never expires.
        logger = logging.getLogger("istota.transport.talk.signaling")
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            signaling.build_hello(_settings(v2=False), ["hello-v2"], "1")
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

        assert any("v1" in r.getMessage() or "1.0" in r.getMessage() for r in records)


# --- the two TalkClient methods the frames are built from ------------------


class TestTalkClientSignalingMethods:
    """`signaling.py` makes no OCS calls of its own: every Nextcloud request
    in this design goes through the one client, with its one auth path and
    its one error handling."""

    def test_get_signaling_settings_reads_the_neutral_endpoint(self):
        payload = {"signalingMode": "external", "server": "https://hpb/sig",
                   "helloAuthParams": {"2.0": {"token": JWT}}}
        client, http = _wired_talk_client(json_body={"ocs": {"data": payload}})

        out = asyncio.run(client.get_signaling_settings())

        url, = http.get.call_args[0]
        assert url == (
            "https://nc.example.com/ocs/v2.php/apps/spreed/api/v3/signaling/settings"
        )
        # No `token` parameter: the non-room-specific call wants the neutral
        # point, and a per-room settings fetch would be N times the requests
        # for one shared, per-user credential.
        assert "params" not in http.get.call_args[1]
        assert out == payload

    def test_get_signaling_settings_uses_the_bot_credentials(self):
        client, http = _wired_talk_client(json_body={"ocs": {"data": {}}})

        asyncio.run(client.get_signaling_settings())

        kwargs = http.get.call_args[1]
        assert kwargs["auth"] == ("bot", "secret")
        assert kwargs["headers"]["OCS-APIRequest"] == "true"

    def test_get_signaling_settings_reports_a_non_ocs_answer(self):
        from istota.talk import TalkResponseError

        client, http = _wired_talk_client(json_body={"not": "ocs"})

        with pytest.raises(TalkResponseError):
            asyncio.run(client.get_signaling_settings())

    def test_join_room_session_posts_force_and_returns_the_session_id(self):
        client, http = _wired_talk_client(
            json_body={"ocs": {"data": {"sessionId": "talk-session-1"}}},
        )

        out = asyncio.run(client.join_room_session("abc123"))

        url, = http.post.call_args[0]
        assert url == (
            "https://nc.example.com/ocs/v2.php/apps/spreed/api/v4/room"
            "/abc123/participants/active"
        )
        # `force: true` is what supersedes a stale session left by a previous
        # connection (RoomController.php:2165-2172).
        assert http.post.call_args[1]["json"] == {"force": True}
        assert out == "talk-session-1"

    @pytest.mark.parametrize(
        "data", [{}, {"sessionId": ""}, {"sessionId": None}, []],
    )
    def test_join_room_session_refuses_an_empty_session_id(self, data):
        # Building a room-join frame without one fails opaquely as
        # `no_such_room`, so the refusal belongs at the source where the
        # message can name the real cause.
        from istota.talk import TalkResponseError

        client, http = _wired_talk_client(json_body={"ocs": {"data": data}})

        with pytest.raises(TalkResponseError):
            asyncio.run(client.join_room_session("abc123"))
