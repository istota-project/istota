"""One OCS unwrap, and every Nextcloud read outside `nextcloud/` goes through it.

`istota.ocs` is the mechanism. This file covers three things:

- the helper itself, over the three answers a Nextcloud read can give — a body
  that is not JSON, JSON that is not an OCS envelope, and a real envelope;
- every converted call site, because the point of the stage is that a silent
  `{}` / `[]` becomes a described failure *at each site*, and a helper test
  says nothing about whether a site still hand-rolls the unwrap;
- the callers whose handling had to change with it, since three of the fifteen
  sites sat under a caller that treated the empty default as normal.

The grep guard at the bottom is what fails when a sixteenth copy grows back.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from istota.config import Config, NextcloudConfig
from istota.ocs import OcsError, is_ocs_envelope, ocs_body_data, ocs_data
from istota.talk import TalkClient, TalkResponseError

SRC = Path(__file__).resolve().parent.parent / "src" / "istota"

NC_URL = "https://nc.example.com"


def _response(*, json_body=None, json_error=None, status=200, text="", headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers if headers is not None else {"content-type": "application/json"}
    resp.text = text
    resp.raise_for_status = MagicMock()
    if json_error is not None:
        resp.json.side_effect = json_error
    else:
        resp.json.return_value = json_body
    return resp


def _client(resp):
    client = TalkClient(
        Config(nextcloud=NextcloudConfig(
            url=NC_URL, username="bot", app_password="secret",
        )),
    )
    http = AsyncMock()
    http.get.return_value = resp
    http.post.return_value = resp
    http.put.return_value = resp
    client._client = http
    return client


# --- the helper ------------------------------------------------------------


class TestOcsData:
    def test_a_real_envelope_yields_its_data(self):
        resp = _response(json_body={"ocs": {"data": {"token": "abc"}}})
        assert ocs_data(resp, "read") == {"token": "abc"}

    def test_a_null_data_yields_the_default(self):
        """"Nothing there" is what the default is for, and it still works."""
        resp = _response(json_body={"ocs": {"data": None}})
        assert ocs_data(resp, "read", default=[]) == []

    def test_a_null_envelope_yields_the_default(self):
        resp = _response(json_body={"ocs": None})
        assert ocs_data(resp, "read", default={}) == {}

    def test_a_non_json_body_names_status_type_length_and_a_snippet(self):
        resp = _response(
            json_error=json.JSONDecodeError("Expecting value", "", 0),
            status=502,
            headers={"content-type": "text/html; charset=UTF-8"},
            text="<html>\n<body>502 Bad Gateway</body>\n</html>",
        )
        with pytest.raises(OcsError) as excinfo:
            ocs_data(resp, "poll room1")
        message = str(excinfo.value)
        assert "poll room1" in message
        assert "HTTP 502" in message
        assert "text/html" in message
        assert "43 chars" in message
        assert "502 Bad Gateway" in message

    def test_the_snippet_is_bounded(self):
        resp = _response(
            json_error=json.JSONDecodeError("Expecting value", "", 0),
            headers={"content-type": "text/html"},
            text="x" * 10_000,
        )
        with pytest.raises(OcsError) as excinfo:
            ocs_data(resp, "poll room1")
        assert len(str(excinfo.value)) < 400

    def test_a_body_whose_text_raises_still_reports_the_fault(self):
        """The diagnostic must not become the thing that raises.

        And it must not report an unreadable body as an empty one: "0 chars"
        for both is the same one-message-three-faults problem this module was
        written to remove, one level down.
        """
        resp = _response(json_error=json.JSONDecodeError("Expecting value", "", 0))
        type(resp).text = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("never read"))
        )
        with pytest.raises(OcsError) as excinfo:
            ocs_data(resp, "poll room1")
        message = str(excinfo.value)
        assert "body unreadable" in message
        assert "0 chars" not in message

    def test_an_empty_body_reports_its_length(self):
        """The other half of the pair above: empty is not unreadable."""
        resp = _response(
            json_error=json.JSONDecodeError("Expecting value", "", 0), text="",
        )
        with pytest.raises(OcsError) as excinfo:
            ocs_data(resp, "poll room1")
        message = str(excinfo.value)
        assert "0 chars" in message
        assert "body unreadable" not in message

    def test_the_snippet_can_be_withheld_without_losing_the_fault(self):
        """For a caller whose error text reaches a log and whose endpoint is
        configurable — the body may then be someone else's response."""
        resp = _response(json_body={"access_token": "must-not-appear"})
        with pytest.raises(OcsError) as excinfo:
            ocs_data(resp, "OCS userinfo", snippet=False)
        message = str(excinfo.value)
        assert "must-not-appear" not in message
        assert "body withheld" in message
        assert "HTTP 200" in message

    def test_a_withheld_snippet_still_names_status_and_type_on_a_non_json_body(self):
        resp = _response(
            json_error=json.JSONDecodeError("Expecting value", "", 0),
            status=502,
            headers={"content-type": "text/html"},
            text="<html>secret-page-content</html>",
        )
        with pytest.raises(OcsError) as excinfo:
            ocs_data(resp, "OCS userinfo", snippet=False)
        message = str(excinfo.value)
        assert "secret-page-content" not in message
        assert "HTTP 502" in message
        assert "text/html" in message
        assert "32 chars" in message

    def test_json_that_is_not_an_envelope_is_a_different_fault(self):
        resp = _response(json_body={"error": "nope"}, status=200)
        with pytest.raises(OcsError) as excinfo:
            ocs_data(resp, "poll room1")
        message = str(excinfo.value)
        assert "ocs envelope" in message
        assert "HTTP 200" in message
        assert "nope" in message

    def test_a_json_list_is_not_an_envelope(self):
        resp = _response(json_body=[1, 2, 3])
        with pytest.raises(OcsError):
            ocs_data(resp, "poll room1")

    def test_the_no_envelope_message_omits_a_status_it_does_not_have(self):
        """`ocs_body_data` is reached from sites holding a decoded dict only."""
        with pytest.raises(OcsError) as excinfo:
            ocs_body_data({"error": "nope"}, "talk send")
        assert "HTTP" not in str(excinfo.value)

    def test_is_ocs_envelope(self):
        assert is_ocs_envelope({"ocs": {"data": {}}})
        assert is_ocs_envelope({"ocs": None})
        assert not is_ocs_envelope({"data": {}})
        assert not is_ocs_envelope([{"ocs": {}}])
        assert not is_ocs_envelope("ocs")
        assert not is_ocs_envelope(None)


class TestTheExceptionFamily:
    def test_talk_response_error_is_the_shared_type(self):
        """An alias, so `except TalkResponseError` catches what the reads raise."""
        assert TalkResponseError is OcsError

    def test_the_nextcloud_package_error_is_a_subclass(self):
        from istota.nextcloud import OcsError as PackageOcsError

        assert issubclass(PackageOcsError, OcsError)

    def test_but_a_leaf_error_is_not_a_package_error(self):
        """`skills/nextcloud`'s `describe` calls `to_envelope()` on a package
        error and `str()` on everything else. A leaf error must take the
        second branch — it carries no endpoint and no OCS status code."""
        from istota.nextcloud import OcsError as PackageOcsError

        assert not isinstance(OcsError("boom"), PackageOcsError)


# --- every converted call site --------------------------------------------


#: (name, callable taking a wired TalkClient). One row per OCS read in
#: `talk.py`; a read added without a row here is a read with no failure case.
TALK_READS = [
    ("create_conversation", lambda c: c.create_conversation("room")),
    ("add_participant", lambda c: c.add_participant("tok", "alice")),
    ("search_mentions", lambda c: c.search_mentions("tok", "al")),
    ("share_file", lambda c: c.share_file("tok", "/notes.md")),
    ("search_messages", lambda c: c.search_messages("hello")),
    ("list_conversations", lambda c: c.list_conversations()),
    ("poll_messages", lambda c: c.poll_messages("tok", last_known_message_id=7)),
    ("get_latest_message_id", lambda c: c.get_latest_message_id("tok")),
    ("fetch_chat_history", lambda c: c.fetch_chat_history("tok")),
    ("get_signaling_settings", lambda c: c.get_signaling_settings()),
    ("join_room_session", lambda c: c.join_room_session("tok")),
    ("get_participants", lambda c: c.get_participants("tok")),
    ("get_conversation_info", lambda c: c.get_conversation_info("tok")),
    ("fetch_full_history", lambda c: c.fetch_full_history("tok")),
    ("fetch_messages_since", lambda c: c.fetch_messages_since("tok", since_id=7)),
]


@pytest.mark.parametrize("name,call", TALK_READS, ids=[n for n, _ in TALK_READS])
def test_every_talk_read_reports_a_non_json_body(name, call):
    resp = _response(
        json_error=json.JSONDecodeError("Expecting value", "", 0),
        headers={"content-type": "text/html"},
        text="<html>503 from the reverse proxy</html>",
    )
    with pytest.raises(OcsError) as excinfo:
        asyncio.run(call(_client(resp)))
    message = str(excinfo.value)
    assert "text/html" in message
    assert "reverse proxy" in message


@pytest.mark.parametrize("name,call", TALK_READS, ids=[n for n, _ in TALK_READS])
def test_every_talk_read_reports_json_without_an_envelope(name, call):
    """The half that used to be silent: valid JSON, no `ocs` key, empty default.

    A poll against a misconfigured endpoint read as "no new messages" rather
    than as a failure, for as long as the endpoint stayed wrong.
    """
    resp = _response(json_body={"message": "Current user is not logged in"})
    with pytest.raises(OcsError) as excinfo:
        asyncio.run(call(_client(resp)))
    assert "ocs envelope" in str(excinfo.value)
    assert "not logged in" in str(excinfo.value)


def test_a_talk_read_still_returns_its_data_on_a_real_envelope():
    """The inert half: nothing about the success path moved."""
    resp = _response(json_body={"ocs": {"data": [{"id": 3}, {"id": 2}]}})
    assert asyncio.run(_client(resp).list_conversations()) == [{"id": 3}, {"id": 2}]


def test_poll_messages_still_returns_empty_on_304():
    """304 is answered before the body is read, so it never reaches the unwrap."""
    resp = _response(json_body=None, status=304)
    assert asyncio.run(_client(resp).poll_messages("tok", last_known_message_id=7)) == []


# --- the callers whose handling changed with the sites --------------------


class TestBestEffortCallersKeepTheirOldAnswer:
    """The callers whose handling had to be decided site by site.

    Two get an explicit catch that logs and returns what they returned before,
    so the failure is visible in the log without moving what the caller sees.
    The Talk transport's post is the one that deliberately does not: it already
    owns a mechanism for this exact failure, and catching would disable it.
    """

    def test_talk_transport_post_part_reads_the_room_back(self):
        """This one is deliberately *not* caught at the unwrap.

        `_may_have_been_stored` names this exact case — "a 2xx whose body does
        not parse raises after Nextcloud has written the message" — and the
        readback is what turns it into the real id. Swallowing it into a `None`
        return would report a post the user can see as undelivered, which is
        the ambiguity ISSUE-404 removed.
        """
        from istota.transport.talk import TalkTransport

        config = Config(nextcloud=NextcloudConfig(
            url=NC_URL, username="bot", app_password="secret",
        ))
        config.talk.bot_username = "bot"
        client = MagicMock()
        client.send_message = AsyncMock(return_value={"message": "no envelope"})
        client.fetch_chat_history = AsyncMock(return_value=[
            {"id": 4242, "referenceId": "ref-1", "actorType": "users",
             "actorId": "bot"},
        ])
        transport = TalkTransport(config)

        posted = asyncio.run(transport._post_part(
            client, "tok", "hello",
            reply_to=None, reference_id="ref-1",
            readback_allowed=True, task=None,
        ))

        assert posted == 4242
        # Not re-posted: `_is_transient` is False for an OcsError, so the loop
        # breaks on the first attempt and nothing is doubled in the room.
        assert client.send_message.await_count == 1

    def test_talk_transport_post_part_raises_when_the_room_says_no(self):
        """The readback settles it the other way: nothing in the room, so the
        failure ends delivery loudly instead of being a silent `None`."""
        from istota.transport.talk import TalkTransport

        config = Config(nextcloud=NextcloudConfig(
            url=NC_URL, username="bot", app_password="secret",
        ))
        config.talk.bot_username = "bot"
        client = MagicMock()
        client.send_message = AsyncMock(return_value={"message": "no envelope"})
        client.fetch_chat_history = AsyncMock(return_value=[])
        transport = TalkTransport(config)

        with pytest.raises(OcsError):
            asyncio.run(transport._post_part(
                client, "tok", "hello",
                reply_to=None, reference_id="ref-1",
                readback_allowed=True, task=None,
            ))
        assert client.send_message.await_count == 1

    def test_an_unreadable_readback_holds_the_message_rather_than_reposting(self):
        """The readback's own `fetch_chat_history` now raises where it returned
        `[]`. An unanswerable question must hold the post back, not re-post it
        — a duplicate in the user's room is the outcome this module ranks
        worst."""
        from istota.transport.talk import TalkTransport

        config = Config(nextcloud=NextcloudConfig(
            url=NC_URL, username="bot", app_password="secret",
        ))
        config.talk.bot_username = "bot"
        client = MagicMock()
        client.send_message = AsyncMock(return_value={"message": "no envelope"})
        client.fetch_chat_history = AsyncMock(
            side_effect=OcsError("chat history tok: JSON without an ocs envelope"),
        )
        transport = TalkTransport(config)

        with pytest.raises(OcsError):
            asyncio.run(transport._post_part(
                client, "tok", "hello",
                reply_to=None, reference_id="ref-1",
                readback_allowed=True, task=None,
            ))
        assert client.send_message.await_count == 1

    def test_post_as_user_returns_none_and_logs(self, caplog, monkeypatch):
        from istota import web_app

        # A class, not a lambda or a MagicMock: `_post_as_user` resolves
        # `TalkClient` through `istota.talk`, and `transport/talk/inbound`
        # spells `TalkClient | None` in a signature it evaluates at import.
        # Substituting a non-type there makes an unrelated module fail to
        # import for whichever test happens to load it next.
        class FakeTalkClient:
            def __init__(self, *args, **kwargs):
                self.send_message = AsyncMock(
                    return_value={"message": "no envelope"},
                )
                self.aclose = AsyncMock()

        monkeypatch.setattr(web_app, "_config", Config(nextcloud=NextcloudConfig(
            url=NC_URL, username="bot", app_password="secret",
        )))
        monkeypatch.setattr("istota.talk.TalkClient", FakeTalkClient)

        with caplog.at_level("WARNING"):
            posted = asyncio.run(web_app._post_as_user(
                "access-token", "tok", "hello", 12, "alice",
            ))

        assert posted is None
        assert "id could not be read" in caplog.text

    def test_promote_answers_failed_rather_than_raising(self, tmp_path, monkeypatch):
        """An unreadable create answer has always produced the route's 502
        "Nextcloud created no conversation" — `{}` had no token. An escaping
        OcsError would make it a bare 500 instead."""
        from istota import db, web_app

        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        config = Config(nextcloud=NextcloudConfig(
            url=NC_URL, username="bot", app_password="secret",
        ))
        config.db_path = db_path
        monkeypatch.setattr(web_app, "_config", config)

        class FakeTalkClient:
            def __init__(self, *args, **kwargs):
                self.create_conversation = AsyncMock(
                    side_effect=OcsError("create conversation: JSON without an ocs envelope"),
                )
                self.aclose = AsyncMock()

        monkeypatch.setattr("istota.talk.TalkClient", FakeTalkClient)
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "Ideas")

        status, payload = asyncio.run(web_app._chat_promote_to_talk("alice", room.id))

        assert (status, payload) == ("failed", None)
        with db.get_db(db_path) as conn:
            assert db.get_room_binding(conn, room.token, "talk") is None


class TestOauthUserinfo:
    def test_a_body_with_no_envelope_raises_instead_of_denying_access(self):
        """It used to reduce to `{}`, so the login answered "user not
        configured" — a 403 naming the wrong fault. The caller catches this
        and answers 502."""
        from istota import web_app

        config = Config(nextcloud=NextcloudConfig(url=NC_URL))
        config.web.oauth2_userinfo_endpoint = f"{NC_URL}/ocs/v2.php/cloud/user"

        resp = _response(json_body={"message": "Current user is not logged in"})
        http = AsyncMock()
        http.get.return_value = resp
        http.__aenter__ = AsyncMock(return_value=http)
        http.__aexit__ = AsyncMock(return_value=False)

        original = web_app._config
        web_app._config = config
        try:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr("istota.web_app.httpx.AsyncClient", lambda **k: http)
                with pytest.raises(OcsError) as excinfo:
                    asyncio.run(web_app._nc_oauth2_userinfo(
                        {"access_token": "at"},
                    ))
        finally:
            web_app._config = original
        assert "OCS userinfo" in str(excinfo.value)

    def test_a_falsy_data_still_reads_as_user_not_configured(self):
        """PHP renders an empty associative array as `[]`, so `{"ocs":
        {"data": []}}` is a shape a working Nextcloud emits. It collapsed to
        `{}` and the login answered "user not configured"; switching that to a
        502 would be an unannounced change on the sign-in path."""
        from istota import web_app

        config = Config(nextcloud=NextcloudConfig(url=NC_URL))
        config.web.oauth2_userinfo_endpoint = f"{NC_URL}/ocs/v2.php/cloud/user"

        resp = _response(json_body={"ocs": {"data": []}})
        http = AsyncMock()
        http.get.return_value = resp
        http.__aenter__ = AsyncMock(return_value=http)
        http.__aexit__ = AsyncMock(return_value=False)

        original = web_app._config
        web_app._config = config
        try:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr("istota.web_app.httpx.AsyncClient", lambda **k: http)
                out = asyncio.run(web_app._nc_oauth2_userinfo({"access_token": "at"}))
        finally:
            web_app._config = original
        assert out == {}

    def test_the_error_withholds_the_body_it_would_otherwise_log(self):
        """The caller logs this error. A `oauth2_userinfo_endpoint` pointed at
        the token endpoint answers JSON with no `ocs` key and a bearer token in
        it, and the body prefix would carry that straight into the log."""
        from istota import web_app

        config = Config(nextcloud=NextcloudConfig(url=NC_URL))
        config.web.oauth2_userinfo_endpoint = f"{NC_URL}/ocs/v2.php/cloud/user"

        leaked = "sensitive-value-that-must-not-be-logged"
        resp = _response(
            json_body={"access_token": leaked, "token_type": "Bearer"},
            status=200,
        )
        http = AsyncMock()
        http.get.return_value = resp
        http.__aenter__ = AsyncMock(return_value=http)
        http.__aexit__ = AsyncMock(return_value=False)

        original = web_app._config
        web_app._config = config
        try:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr("istota.web_app.httpx.AsyncClient", lambda **k: http)
                with pytest.raises(OcsError) as excinfo:
                    asyncio.run(web_app._nc_oauth2_userinfo({"access_token": "at"}))
        finally:
            web_app._config = original
        message = str(excinfo.value)
        assert leaked not in message
        assert "access_token" not in message
        # The fault is still identified: what failed, and the status it failed on.
        assert "OCS userinfo" in message
        assert "HTTP 200" in message

    def test_a_real_envelope_still_yields_the_identity(self):
        from istota import web_app

        config = Config(nextcloud=NextcloudConfig(url=NC_URL))
        config.web.oauth2_userinfo_endpoint = f"{NC_URL}/ocs/v2.php/cloud/user"

        resp = _response(json_body={"ocs": {"data": {"id": "alice"}}})
        http = AsyncMock()
        http.get.return_value = resp
        http.__aenter__ = AsyncMock(return_value=http)
        http.__aexit__ = AsyncMock(return_value=False)

        original = web_app._config
        web_app._config = config
        try:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr("istota.web_app.httpx.AsyncClient", lambda **k: http)
                out = asyncio.run(web_app._nc_oauth2_userinfo({"access_token": "at"}))
        finally:
            web_app._config = original
        assert out == {"id": "alice"}


class TestSkillTalkSend:
    """`send_message` is the one method returning the raw body, so the skill
    tolerates a payload that is already unwrapped. That tolerance stays."""

    def test_an_envelope_is_unwrapped(self):
        from istota.skills.nextcloud import _ocs_data

        assert _ocs_data({"ocs": {"data": {"id": 9}}}) == {"id": 9}

    def test_an_already_unwrapped_payload_is_passed_through(self):
        from istota.skills.nextcloud import _ocs_data

        assert _ocs_data({"id": 9}) == {"id": 9}

    def test_a_non_dict_is_empty(self):
        from istota.skills.nextcloud import _ocs_data

        assert _ocs_data(["nope"]) == {}


# --- the guard -------------------------------------------------------------


class TestNoSecondUnwrap:
    """`istota.ocs` is the only place outside `nextcloud/` that names the key.

    A grep-shaped guard rather than a behaviour test, because the failure this
    stage prevents is a *new* copy: a sixteenth site reading
    `response.json().get("ocs", {}).get("data", {})` is green under every
    behaviour test in the tree and silently reintroduces the empty default.
    """

    #: Files allowed to name the envelope key, and why.
    EXEMPT = {
        # The mechanism itself.
        "ocs.py",
        # The package's own richer reader: it also checks `meta.statuscode`,
        # maps the 99x range and carries the endpoint. Deliberately not
        # migrated; its `OcsError` subclasses the leaf's.
        "nextcloud/_http.py",
    }

    @staticmethod
    def _hits(source: str) -> list[int]:
        """Line numbers of a bare ``"ocs"`` string constant.

        Catches all three spellings the tree had — ``.get("ocs", {})``,
        ``body["ocs"]`` and ``"ocs" in body`` — while a URL path like
        ``/ocs/v2.php/...`` is a different constant and does not match.
        """
        tree = ast.parse(source)
        found = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value == "ocs"
            ):
                found.append(node.lineno)
        return sorted(set(found))

    def _offenders(self) -> dict[str, list[int]]:
        found: dict[str, list[int]] = {}
        for path in sorted(SRC.rglob("*.py")):
            rel = path.relative_to(SRC).as_posix()
            if rel in self.EXEMPT:
                continue
            hits = self._hits(path.read_text())
            if hits:
                found[rel] = hits
        return found

    def test_no_module_unwraps_the_envelope_by_hand(self):
        offenders = self._offenders()
        assert offenders == {}, (
            "these reach into the OCS envelope instead of calling "
            f"istota.ocs.ocs_data / ocs_body_data: {offenders}"
        )

    def test_the_exemptions_are_still_real(self):
        """An exemption for a file that no longer names the key is a stale rule."""
        for rel in sorted(self.EXEMPT):
            path = SRC / rel
            assert path.exists(), f"{rel} is exempt but no longer exists"
            assert self._hits(path.read_text()), (
                f"{rel} is exempt but no longer names the ocs key"
            )

    def test_the_guard_can_see_a_reintroduced_copy(self, tmp_path, monkeypatch):
        """The control for the two tests above, in the suite rather than beside it.

        A grep guard that walks the wrong tree, or whose expression stopped
        matching, reports a clean result forever. This one points it at a tree
        holding exactly the copies it is meant to find — including one under a
        basename the exemption list carries, since a control planting only
        `regrown.py` never tests the exemption path at all.
        """
        fake = tmp_path / "istota"
        (fake / "nextcloud").mkdir(parents=True)
        (fake / "regrown.py").write_text(
            'data = response.json().get("ocs", {}).get("data", {})\n'
        )
        (fake / "nextcloud" / "_client.py").write_text(
            'if "ocs" in body:\n    data = body["ocs"]["data"]\n'
        )
        # Both exempt paths, carrying the very pattern the guard hunts. Without
        # these the `if rel in EXEMPT: continue` line is never taken during the
        # control and the whole exemption mechanism is untested — the test
        # would pass identically with `EXEMPT = set()`.
        for rel in sorted(self.EXEMPT):
            (fake / rel).parent.mkdir(parents=True, exist_ok=True)
            (fake / rel).write_text('data = body.get("ocs", {}).get("data")\n')
        monkeypatch.setattr("tests.test_ocs.SRC", fake, raising=False)
        offenders = self._offenders()
        assert set(offenders) == {"regrown.py", "nextcloud/_client.py"}
