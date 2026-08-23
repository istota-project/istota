"""The ntfy and feeds stubs, driven over a real socket with no container.

Same discipline as `tests/test_model_endpoint.py` and `tests/test_fake_gitlab.py`:
a stub that only ever runs behind a deselected marker rots, and its failures
then arrive inside a deployment scenario where they read as a subsystem fault.
Everything here binds loopback, answers one request and stops.

What is deliberately *not* here: the deployed behaviour. Whether the daemon
sends an encoded header or whether the poller honours a 304 belongs to
`tests/smoke/test_notify_e2e.py` and `tests/smoke/test_feeds_e2e.py`, which run
the real thing. This file only holds down the half of each pair that the
scenario has to be able to trust.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from testbed.services import feeds, ntfy


def _request(url: str, *, method: str = "GET", data=None, headers=None):
    """One request, returning `(status, headers, body)`; 3xx/4xx included.

    `urllib` raises on a non-2xx, and both stubs answer with statuses that are
    the point of the assertion — a 304 from the conditional-GET path, a 401
    from the token check — so a helper that let the exception through would
    make every interesting case a `pytest.raises`.
    """
    request = urllib.request.Request(
        url, method=method, data=data, headers=headers or {}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


@pytest.fixture
def push_server():
    stub = ntfy.serve(credential=ntfy.NTFY_TOKEN)
    try:
        yield stub
    finally:
        stub.close()


@pytest.fixture
def documents():
    stub = feeds.serve()
    try:
        yield stub
    finally:
        stub.close()


class TestTheNtfyStub:
    def test_a_push_is_recorded_with_its_headers(self, push_server):
        status, _, _ = _request(
            f"{push_server.url}/{push_server.topic}",
            method="POST",
            data=b"the body",
            headers={
                "Authorization": f"Bearer {ntfy.NTFY_TOKEN}",
                "Title": "=?utf-8?B?w6Q=?=",
            },
        )

        assert status == 200
        assert len(push_server.pushes()) == 1
        assert push_server.pushes()[0].body == b"the body"
        # Byte-identical, which is the property the deployed scenario rests on:
        # an RFC 2047 encoded word must survive the recording step unchanged,
        # or the scenario would be asserting against something this file did.
        assert push_server.header("Title") == "=?utf-8?B?w6Q=?="

    def test_the_header_lookup_is_case_insensitive(self, push_server):
        _request(
            f"{push_server.url}/{push_server.topic}",
            method="POST",
            data=b"x",
            headers={"Authorization": f"Bearer {ntfy.NTFY_TOKEN}", "title": "plain"},
        )

        assert push_server.header("Title") == "plain"
        assert push_server.header("TITLE") == "plain"

    def test_an_absent_header_is_empty_rather_than_a_raise(self, push_server):
        _request(
            f"{push_server.url}/{push_server.topic}",
            method="POST",
            data=b"x",
            headers={"Authorization": f"Bearer {ntfy.NTFY_TOKEN}"},
        )

        assert push_server.header("Title") == ""

    def test_a_wrong_token_is_refused_and_still_recorded(self, push_server):
        """Both halves. A stub that accepted anything would let the deployed
        scenario pass on a push that lost its `Authorization` entirely; a stub
        that dropped the record would leave that scenario unable to say what
        did arrive."""
        status, _, body = _request(
            f"{push_server.url}/{push_server.topic}",
            method="POST",
            data=b"x",
            headers={"Authorization": "Bearer wrong"},
        )

        assert status == 401
        assert json.loads(body)["error"] == "unauthorized"
        assert len(push_server.pushes()) == 1

    def test_the_recorded_auth_is_a_shape_and_not_the_value(self, push_server):
        _request(
            f"{push_server.url}/{push_server.topic}",
            method="POST",
            data=b"x",
            headers={"Authorization": f"Bearer {ntfy.NTFY_TOKEN}"},
        )

        recorded = push_server.pushes()[0].auth
        assert recorded == f"Bearer len={len(ntfy.NTFY_TOKEN)}"
        assert ntfy.NTFY_TOKEN not in recorded

    def test_another_topic_is_not_counted_as_this_one(self, push_server):
        """`pushes()` matches the exact path, so a topic that merely *contains*
        the configured one does not satisfy an assertion about it."""
        _request(
            f"{push_server.url}/{push_server.topic}-other",
            method="POST",
            data=b"x",
            headers={"Authorization": f"Bearer {ntfy.NTFY_TOKEN}"},
        )

        assert push_server.pushes() == []
        assert len(push_server.calls) == 1

    def test_a_malformed_length_is_recorded_rather_than_dropped(
        self, push_server
    ):
        """`handle_error` is silenced, so an exception here is a dropped
        connection with no record — and the scenario would then report a task
        that never reached the stub, which is the wrong diagnosis for a request
        that arrived."""
        import http.client

        connection = http.client.HTTPConnection(
            "127.0.0.1", push_server.port, timeout=5
        )
        try:
            connection.putrequest("POST", f"/{push_server.topic}")
            connection.putheader("Content-Length", "not-a-number")
            connection.putheader("Authorization", f"Bearer {ntfy.NTFY_TOKEN}")
            connection.endheaders()
            response = connection.getresponse()
            response.read()
        finally:
            connection.close()

        assert len(push_server.pushes()) == 1
        assert push_server.pushes()[0].body == b""

    def test_it_configures_nothing_through_the_generator(self, push_server):
        """ntfy lives in the secrets store, not in `config.toml`. Asserted so
        that adding a variable here has to be a deliberate change rather than
        something that slips in with a copied `config_env`."""
        assert push_server.config_env() == {}


class TestTheFeedsStub:
    def test_a_registered_document_is_served_with_its_validators(self, documents):
        documents.add(
            "/feed.xml", "<rss/>", etag='"v1"', last_modified="Wed, 01 Jan 2025 00:00:00 GMT"
        )

        status, headers, body = _request(f"{documents.url}/feed.xml")

        assert status == 200
        assert body == b"<rss/>"
        assert headers["ETag"] == '"v1"'
        assert headers["Last-Modified"] == "Wed, 01 Jan 2025 00:00:00 GMT"

    def test_add_returns_the_address_a_container_reaches(self, documents):
        """The scenario writes this straight into a feed row, so it has to be
        the container-facing name rather than loopback — a container reaching
        its own loopback finds nothing, and the symptom is a poll that failed
        for no stated reason."""
        url = documents.add("feed.xml", "<rss/>")

        assert url == f"{documents.container_url}/feed.xml"
        assert "host.docker.internal" in url

    def test_a_matching_etag_takes_the_304(self, documents):
        documents.add("/feed.xml", "<rss/>", etag='"v1"')

        status, _, body = _request(
            f"{documents.url}/feed.xml", headers={"If-None-Match": '"v1"'}
        )

        assert status == 304
        assert body == b""

    def test_a_matching_modification_date_takes_it_too(self, documents):
        """Either validator on its own, which is what RFC 9110 says and what
        the poller relies on: it sends both when it has both, and a server
        demanding they agree would serve a body to a client already current."""
        documents.add(
            "/feed.xml", "<rss/>", last_modified="Wed, 01 Jan 2025 00:00:00 GMT"
        )

        status, _, _ = _request(
            f"{documents.url}/feed.xml",
            headers={"If-Modified-Since": "Wed, 01 Jan 2025 00:00:00 GMT"},
        )

        assert status == 304

    def test_a_stale_etag_gets_the_body(self, documents):
        documents.add("/feed.xml", "<rss/>", etag='"v2"')

        status, _, body = _request(
            f"{documents.url}/feed.xml", headers={"If-None-Match": '"v1"'}
        )

        assert status == 200
        assert body == b"<rss/>"

    def test_the_toggle_turns_the_304_off(self, documents):
        """So a scenario can drive a server that ignores validators, which is
        what a feed host with a broken cache does and what the poller has to
        survive."""
        documents.add("/feed.xml", "<rss/>", etag='"v1"', conditional_get=False)

        status, _, body = _request(
            f"{documents.url}/feed.xml", headers={"If-None-Match": '"v1"'}
        )

        assert status == 200
        assert body == b"<rss/>"

    def test_an_unregistered_path_is_a_404(self, documents):
        status, _, _ = _request(f"{documents.url}/nothing.xml")

        assert status == 404

    def test_replace_changes_the_body_and_the_validator(self, documents):
        documents.add("/feed.xml", "<rss>one</rss>", etag='"v1"')
        documents.replace("/feed.xml", "<rss>two</rss>", etag='"v2"')

        status, headers, body = _request(
            f"{documents.url}/feed.xml", headers={"If-None-Match": '"v1"'}
        )

        assert status == 200
        assert body == b"<rss>two</rss>"
        assert headers["ETag"] == '"v2"'

    def test_replace_clears_a_validator_it_is_not_given(self, documents):
        """The trap that makes the previous test insufficient on its own.

        `_matches` answers 304 on *either* validator and the poller sends both
        when it has both, so a `replace` that set a new `ETag` and left the old
        `Last-Modified` in place would 304 a client asking correctly about a
        document that had changed — and the scenario would read as a poller
        that ignores new entries.
        """
        documents.add(
            "/feed.xml",
            "<rss>one</rss>",
            etag='"v1"',
            last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
        )
        documents.replace("/feed.xml", "<rss>two</rss>", etag='"v2"')

        status, _, body = _request(
            f"{documents.url}/feed.xml",
            headers={"If-Modified-Since": "Wed, 01 Jan 2025 00:00:00 GMT"},
        )

        assert status == 200
        assert body == b"<rss>two</rss>"

    def test_replacing_a_path_nobody_registered_says_so(self, documents):
        with pytest.raises(KeyError, match="never registered"):
            documents.replace("/feed.xml", "<rss/>")

    def test_the_request_headers_are_recorded(self, documents):
        documents.add("/feed.xml", "<rss/>")

        _request(f"{documents.url}/feed.xml", headers={"User-Agent": "istota-feeds/0.1"})

        assert documents.fetches("/feed.xml")[0].headers["User-Agent"] == (
            "istota-feeds/0.1"
        )

    def test_the_query_string_is_not_part_of_the_path(self, documents):
        """A scenario matches on the path it registered, and a feed URL with a
        query would otherwise never match it."""
        documents.add("/feed.xml", "<rss/>")

        status, _, _ = _request(f"{documents.url}/feed.xml?since=1")

        assert status == 200
        assert documents.fetches("/feed.xml")

    def test_reset_forgets_documents_as_well_as_calls(self, documents):
        documents.add("/feed.xml", "<rss/>")
        _request(f"{documents.url}/feed.xml")

        documents.reset()

        assert documents.calls == []
        assert _request(f"{documents.url}/feed.xml")[0] == 404

    def test_it_configures_nothing_through_the_generator(self, documents):
        """Feed URLs are rows, not config. Same guard as the ntfy stub's, and
        for the same reason: an empty `config_env` that quietly gains an entry
        is how a fixture starts side-loading configuration."""
        assert documents.config_env() == {}
