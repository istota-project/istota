"""The Cloud half of inbound media: the Graph fetch and the route's wiring.

Two layers, and the split is the one `.claude/rules/whatsapp.md` already draws
for this surface. `TestTheGraphFetch` drives `WhatsAppClient.fetch_media`
against a stubbed httpx transport, because every bound the fetcher owns — the
declared-size gate, the in-stream cap, a Graph refusal — is a fact about bytes
and status codes. `TestTheCloudStagingStep` drives `stage_cloud_media`, which
is what the route calls between `parse_webhook` and the transaction, because
the ordering it exists for (pre-check, fetch, consume, all outside
`BEGIN IMMEDIATE`) is a fact about calls rather than about HTTP.

**Nothing here connects to WhatsApp**, and the module says so rather than
implying otherwise: the fetch is pinned against a transport this file writes,
the same way the sidecar's download is pinned by parse and pure-function
execution. Meta's own tolerance for a callback that streams a photograph inside
the request is not measurable from here.

Two properties are asserted on the *transport's recorded requests* rather than
on a return value, because they are absences: a stranger's media is never
fetched, and a file larger than Meta itself declares is refused before a byte
of it moves.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from istota import db
from istota.config import Config, UserConfig
from istota.transport.whatsapp import media
from istota.transport.whatsapp.client import (
    MEDIA_FETCH_FAILED_REASON,
    MEDIA_OVER_CAP_REASON,
    WhatsAppMediaError,
)
from istota.transport.whatsapp.providers.whatsapp_cloud import stage_cloud_media
from istota.transport.whatsapp.webhook import (
    MEDIA_FAILED_REPLY,
    handle_whatsapp_batch,
    normalize_payload,
)

from .support.drift import source_of
from .support.graph_media import (
    MEDIA_ID,
    MEDIA_URL,
    PNG,
    Graph,
    build_client,
    install_client,
)
from .support.whatsapp_config import build_whatsapp_config

WABA_ID = "123456789012345"
PHONE_NUMBER_ID = "223456789012345"
USER_WA_ID = "15551234567"
USER_NUMBER = "+15551234567"
USER_BSUID = "US.9876543210"

SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>'


def _config(tmp_path, **overrides) -> Config:
    path = tmp_path / "db" / "istota.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db.init_db(path)
    fields = dict(
        enabled=True,
        waba_id=WABA_ID,
        phone_number_id=PHONE_NUMBER_ID,
        business_phone_number="+15551230000",
        access_token="wa-access-token",
        app_secret="wa-app-secret",
        verify_token="wa-verify-token",
        business_timezone="UTC",
    )
    fields.update(overrides)
    config = Config(
        db_path=path,
        temp_dir=tmp_path / "tmp",
        workspace_path=tmp_path / "mount",
        whatsapp=build_whatsapp_config(**fields),
        users={"alice": UserConfig(), "bob": UserConfig()},
    )
    config.site.hostname = "assistant.example.com"
    return config


def _install_client(monkeypatch, config, graph: Graph) -> None:
    install_client(monkeypatch, graph)


def _bind(config, *, user_id="alice", bsuid=USER_BSUID):
    with db.get_db(config.db_path) as conn:
        db.set_whatsapp_binding(
            conn, user_id, bootstrap_phone_number=USER_NUMBER, bsuid=bsuid,
        )


def _image_message(*, message_id="wamid.img", caption="what is this?",
                   media_id=MEDIA_ID, sender=USER_BSUID):
    image: dict = {"id": media_id, "mime_type": "image/jpeg", "sha256": "0" * 64}
    if caption is not None:
        image["caption"] = caption
    return {
        "id": message_id,
        "from": sender,
        "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
        "type": "image",
        "image": image,
    }


def _payload(*messages, contacts=None):
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": WABA_ID,
            "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {
                    "display_phone_number": "15551230000",
                    "phone_number_id": PHONE_NUMBER_ID,
                },
                "contacts": contacts if contacts is not None else [
                    {"user_id": USER_BSUID, "wa_id": USER_WA_ID,
                     "profile": {"name": "Alice"}},
                ],
                "messages": list(messages),
            }}],
        }],
    }


def _events(config, *messages, **kwargs):
    return normalize_payload(config, _payload(*messages, **kwargs))


def _staged(config, events):
    return asyncio.run(stage_cloud_media(config, events))


def _media_dir(config) -> Path:
    return media.default_media_dir(config)


# ---------------------------------------------------------------------------
# The route's own wiring, end to end
# ---------------------------------------------------------------------------


class TestACloudImageBecomesATaskCarryingIt:
    """The stage's deliverable, driven through all three calls the route makes.

    `normalize_payload` produces the event, `stage_cloud_media` fetches the
    bytes and puts them in the user's inbox, and `handle_whatsapp_batch` takes
    the write lock for the first time with a path already in hand.
    """

    def test_the_caption_is_the_prompt_and_the_inbox_copy_is_the_attachment(
        self, tmp_path, monkeypatch,
    ):
        config = _config(tmp_path)
        _bind(config)
        graph = Graph()
        _install_client(monkeypatch, config, graph)

        events = _staged(config, _events(config, _image_message()))
        with db.get_db(config.db_path) as conn:
            results = handle_whatsapp_batch(
                conn, config, events, provider=db.WHATSAPP_LEGACY_PROVIDER,
            )

        assert [result.disposition for result in results] == ["task"]
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, results[0].task_id)
        assert task.prompt == "what is this?"
        assert task.attachments == [events[0].media.staged_path]
        assert task.attachments[0].startswith("/Users/alice/inbox/")
        # Named from the sniff rather than from the staged `.bin`, which is
        # what keeps it past `prepare_image_attachments`' suffix screen.
        assert task.attachments[0].endswith(".png")
        assert (config.workspace_path / task.attachments[0].lstrip("/")).exists()
        # The staging directory's steady state is empty.
        assert list(_media_dir(config).iterdir()) == []

    def test_an_uncaptioned_image_is_a_task_rather_than_an_empty_message(
        self, tmp_path, monkeypatch,
    ):
        config = _config(tmp_path)
        _bind(config)
        _install_client(monkeypatch, config, Graph())

        events = _staged(config, _events(config, _image_message(caption=None)))
        with db.get_db(config.db_path) as conn:
            results = handle_whatsapp_batch(
                conn, config, events, provider=db.WHATSAPP_LEGACY_PROVIDER,
            )

        assert [result.disposition for result in results] == ["task"]
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, results[0].task_id)
        assert task.prompt == "The user sent an image with no caption."
        assert task.attachments[0].startswith("/Users/alice/inbox/")


# ---------------------------------------------------------------------------
# The fetch itself
# ---------------------------------------------------------------------------


class TestTheGraphFetch:
    """`fetch_media`, against a transport this file owns.

    Every bound the *fetcher* owns lives here, because the daemon only ever
    sees a file that already exists: past this call the per-file cap has no
    enforcement point left.
    """

    def _fetch(self, config, graph, *, max_bytes=media.MAX_MEDIA_BYTES,
               media_id=MEDIA_ID, dest=None):
        client = build_client(config, graph)

        async def run():
            try:
                return await client.fetch_media(
                    media_id, dest, max_bytes=max_bytes,
                )
            finally:
                await client.aclose()

        return asyncio.run(run())

    def _dest(self, tmp_path):
        path = tmp_path / "staged.bin"
        return path, os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)

    def test_the_bytes_land_on_the_descriptor_the_caller_opened(
        self, tmp_path,
    ):
        config = _config(tmp_path)
        graph = Graph()
        path, fd = self._dest(tmp_path)
        try:
            mime, written = self._fetch(config, graph, dest=fd)
        finally:
            os.close(fd)

        assert (mime, written) == ("image/jpeg", len(PNG))
        assert path.read_bytes() == PNG
        assert graph.downloads == [MEDIA_URL]

    def test_metas_declared_size_is_refused_before_any_stream(self, tmp_path):
        """The gate that keeps a large file off the wire entirely — Meta
        reports the size before a byte moves, so a refusal here costs one
        round trip rather than a whole download."""
        config = _config(tmp_path)
        graph = Graph(declared_size=media.MAX_MEDIA_BYTES + 1)
        path, fd = self._dest(tmp_path)
        try:
            with pytest.raises(WhatsAppMediaError) as caught:
                self._fetch(config, graph, dest=fd)
        finally:
            os.close(fd)

        assert caught.value.reason == MEDIA_OVER_CAP_REASON
        assert graph.downloads == []
        assert path.read_bytes() == b""

    def test_a_server_that_lies_about_its_size_is_cut_off_mid_stream(
        self, tmp_path,
    ):
        """A declared size is a claim. The cap is enforced again against the
        bytes that actually arrive, and the overage chunk is never written.

        The body is deliberately several chunks long and the cap falls between
        two of them: a body under one chunk makes `written` zero when the cap
        fires, and "nothing was written" then satisfies a bound that is meant
        to be about *stopping*, which is the vacuous shape
        `.claude/rules/testbed.md` catalogues.
        """
        config = _config(tmp_path)
        chunk = 64 * 1024
        graph = Graph(body=b"\x89PNG" + b"\x00" * (4 * chunk), declared_size=16)
        path, fd = self._dest(tmp_path)
        try:
            with pytest.raises(WhatsAppMediaError) as caught:
                self._fetch(config, graph, dest=fd, max_bytes=2 * chunk + 1)
        finally:
            os.close(fd)

        assert caught.value.reason == MEDIA_OVER_CAP_REASON
        assert graph.downloads == [MEDIA_URL]
        written = len(path.read_bytes())
        # Cut off, rather than never started: two whole chunks landed and the
        # third, which would have passed the cap, did not.
        assert written == 2 * chunk

    def test_a_graph_refusal_is_a_fetch_failure(self, tmp_path):
        config = _config(tmp_path)
        graph = Graph(lookup_status=400)
        path, fd = self._dest(tmp_path)
        try:
            with pytest.raises(WhatsAppMediaError) as caught:
                self._fetch(config, graph, dest=fd)
        finally:
            os.close(fd)

        assert caught.value.reason == MEDIA_FETCH_FAILED_REASON
        assert graph.downloads == []
        assert path.read_bytes() == b""

    def test_a_download_that_fails_is_a_fetch_failure(self, tmp_path):
        config = _config(tmp_path)
        graph = Graph(download_status=404)
        path, fd = self._dest(tmp_path)
        try:
            with pytest.raises(WhatsAppMediaError) as caught:
                self._fetch(config, graph, dest=fd)
        finally:
            os.close(fd)

        assert caught.value.reason == MEDIA_FETCH_FAILED_REASON

    @pytest.mark.parametrize(
        "media_id",
        ["", "../123", "123/me", "1122?fields=x", "1122#frag", "x" * 300],
        ids=["empty", "traversal", "separator", "query", "fragment", "oversized"],
    )
    def test_a_media_id_that_is_not_one_path_segment_never_reaches_graph(
        self, tmp_path, media_id,
    ):
        """PyWa interpolates the id into a Graph path on a session carrying the
        access token, so the join is the containment story — `is_staged_name`'s
        rule, applied to the other value off the wire that becomes a path."""
        config = _config(tmp_path)
        graph = Graph()
        path, fd = self._dest(tmp_path)
        try:
            with pytest.raises(WhatsAppMediaError) as caught:
                self._fetch(config, graph, dest=fd, media_id=media_id)
        finally:
            os.close(fd)

        assert caught.value.reason == MEDIA_FETCH_FAILED_REASON
        assert graph.requests == []

    def test_a_descriptor_that_cannot_be_written_is_its_own_reason(
        self, tmp_path,
    ):
        """One of the three reasons the module publishes, and the only one no
        other case reaches: the bytes arrived and the disk refused them."""
        from istota.transport.whatsapp.client import MEDIA_WRITE_FAILED_REASON

        config = _config(tmp_path)
        graph = Graph()
        path = tmp_path / "staged.bin"
        path.write_bytes(b"")
        # Opened read-only, so `os.write` fails with EBADF — a stand-in for the
        # full disk or the revoked mount this arm is really about.
        fd = os.open(path, os.O_RDONLY)
        try:
            with pytest.raises(WhatsAppMediaError) as caught:
                self._fetch(config, graph, dest=fd)
        finally:
            os.close(fd)

        assert caught.value.reason == MEDIA_WRITE_FAILED_REASON

    def test_a_transfer_that_never_ends_is_bounded_in_time(
        self, tmp_path, monkeypatch,
    ):
        """`httpx.Timeout` is per operation, so each chunk read restarts it and
        a trickling peer would hold open the request Meta is waiting on. The
        deadline is the whole-transfer bound; `max_bytes` cannot be, since a
        peer that sends nothing never reaches it."""
        from istota.transport.whatsapp import client as client_module

        config = _config(tmp_path)
        client = build_client(config, Graph())
        path, fd = self._dest(tmp_path)

        async def never_ends(*_args, **_kwargs):
            await asyncio.sleep(3600)
            yield b""  # pragma: no cover - unreachable

        # The stall is in PyWa's generator rather than in the transport,
        # because a `MockTransport` answers instantly by construction and what
        # is under test is the loop around it.
        monkeypatch.setattr(client._client, "stream_media", never_ends)
        monkeypatch.setattr(
            client_module, "_MEDIA_FETCH_DEADLINE_SECONDS", 0.05,
        )

        async def run():
            try:
                return await client.fetch_media(
                    MEDIA_ID, fd, max_bytes=media.MAX_MEDIA_BYTES,
                )
            finally:
                await client.aclose()

        try:
            with pytest.raises(WhatsAppMediaError) as caught:
                asyncio.run(run())
        finally:
            os.close(fd)

        assert caught.value.reason == MEDIA_FETCH_FAILED_REASON
        assert path.read_bytes() == b""

    @pytest.mark.parametrize(
        "value, expected",
        [
            (61901, 61901),
            ("61901", 61901),
            ("  61901  ", 61901),
            (True, None),
            (-1, None),
            ("-1", None),
            ("abc", None),
            (None, None),
            (1.5, None),
        ],
    )
    def test_metas_declared_size_is_read_or_disbelieved(self, value, expected):
        """`None` means Meta said nothing about the size, which the in-stream
        cap covers — reading an unparseable value as zero would silently retire
        the cheaper gate, and reading `True` as 1 is the coercion that makes a
        gate pass on a value nobody sent."""
        from istota.transport.whatsapp.client import _as_byte_count

        assert _as_byte_count(value) == expected

    def test_the_declared_type_cannot_forge_a_log_line(self, tmp_path):
        """The value is the uploader's, echoed back by Graph, and it reaches a
        log line. One rule for it, in `media.py`, shared with the normalizer
        that reads the same field off the callback."""
        config = _config(tmp_path)
        graph = Graph(mime="image/jpeg\nWARNING forged " + "x" * 400)
        path, fd = self._dest(tmp_path)
        try:
            declared, _written = self._fetch(config, graph, dest=fd)
        finally:
            os.close(fd)

        assert "\n" not in declared
        assert len(declared) <= media.MAX_DECLARED_MIME_CHARS
        assert path.read_bytes() == PNG

    def test_nothing_on_this_path_calls_download_media(self):
        """A source assertion, because the point is filename provenance.

        PyWa's `download_media` names the file from `Content-Disposition` or
        from a hash of the URL — both server-chosen. The daemon names its own
        files, so the only two Graph calls this module may make are
        `get_media_url` and `stream_media`.

        Read through `source_of`, per AGENTS.md: a guard that reads lines
        rather than running them is invisible to testmon, so `scripts/qt` would
        never re-select it after the very edit it exists to catch.
        """
        from istota.transport.whatsapp import client as client_module
        from istota.transport.whatsapp.providers import whatsapp_cloud

        for module in (client_module, whatsapp_cloud):
            body = "\n".join(
                line for line in source_of(module).splitlines()
                if not line.strip().startswith("#")
            )
            assert "download_media(" not in body
            assert "get_media_bytes" not in body

    def test_the_two_adapters_describe_a_failed_fetch_in_the_same_words(self):
        """The failure record is built once per adapter, so what must not drift
        is what it *says*: the same four fields and the same prose.

        The reasons themselves have one home (`media.py`) and this is the other
        half — that both constructors produce the same record, since
        `WhatsAppInboundMedia` gained a field in this very stage and a second
        constructor is how the two answers separate.
        """
        from istota.transport.whatsapp.baileys_bridge import _media_failed as baileys
        from istota.transport.whatsapp.providers.whatsapp_cloud import (
            _media_failed as cloud,
        )

        assert cloud(media.MEDIA_NOT_PLACED) == baileys(media.MEDIA_NOT_PLACED)
        # And the prose is the common module's, not each adapter's own.
        from istota.transport.whatsapp import baileys_protocol

        assert set(baileys_protocol._MEDIA_ERRORS.values()) == {
            media.MEDIA_FETCH_FAILED,
            media.MEDIA_OVER_CAP,
            media.MEDIA_WRITE_FAILED,
        }


# ---------------------------------------------------------------------------
# The staging step the route calls
# ---------------------------------------------------------------------------


class TestTheCloudStagingStep:
    """Pre-check, then fetch, then consume — in that order and outside the lock.

    The pre-check runs *before* the fetch here and *after* it on Baileys, which
    is the whole of the difference between the two adapters. From
    `stage_to_attachment` onwards it is one code path.
    """

    def test_a_stranger_costs_no_request_at_all(self, tmp_path, monkeypatch):
        """The disk-fill vector, closed at its cheapest point. Asserted on the
        transport rather than on the record, because "nothing was fetched" is
        an absence."""
        config = _config(tmp_path)
        graph = Graph()
        _install_client(monkeypatch, config, graph)

        events = _staged(config, _events(config, _image_message()))

        assert graph.requests == []
        assert events[0].media.error == media.MEDIA_UNATTRIBUTED
        assert events[0].media.staged_path == ""
        assert not (config.workspace_path / "Users").exists()

    def test_a_message_id_already_claimed_is_never_fetched_again(
        self, tmp_path, monkeypatch,
    ):
        """The redelivery loop: Meta retries a callback it got no 200 for. The
        authoritative claim is inside the transaction and the fetch is in front
        of it, so without this read every replay would pay for the bytes
        again."""
        config = _config(tmp_path)
        _bind(config)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "INSERT INTO processed_whatsapp (message_id, user_id, task_id, "
                " disposition, message_type, received_at) "
                "VALUES ('wamid.img', 'alice', NULL, 'task', 'image', "
                " datetime('now'))"
            )
        graph = Graph()
        _install_client(monkeypatch, config, graph)

        events = _staged(config, _events(config, _image_message()))

        assert graph.requests == []
        assert events[0].media.error == media.MEDIA_UNATTRIBUTED

    def test_an_opted_out_sender_is_never_fetched_for(self, tmp_path, monkeypatch):
        """The spec's ruling: the image is not retained for somebody who asked
        not to be messaged, which on this adapter means the bytes never move."""
        config = _config(tmp_path)
        _bind(config)
        with db.get_db(config.db_path) as conn:
            db.set_whatsapp_opt_out(conn, "alice", True)
        graph = Graph()
        _install_client(monkeypatch, config, graph)

        events = _staged(
            config, _events(config, _image_message(caption="START")),
        )

        assert graph.requests == []
        assert events[0].media.error == media.MEDIA_UNATTRIBUTED
        # The caption is a message and is untouched by the media gate.
        assert events[0].text == "START"

    def test_a_file_over_the_cap_leaves_nothing_staged(self, tmp_path, monkeypatch):
        config = _config(tmp_path)
        _bind(config)
        graph = Graph(declared_size=media.MAX_MEDIA_BYTES + 1)
        _install_client(monkeypatch, config, graph)

        events = _staged(config, _events(config, _image_message()))

        assert events[0].media.error == MEDIA_OVER_CAP_REASON
        assert events[0].media.staged_path == ""
        assert list(_media_dir(config).iterdir()) == []

    def test_a_partial_download_is_discarded_at_the_decision(
        self, tmp_path, monkeypatch,
    ):
        """The sweep is the backstop, not the mechanism: a fetch that failed
        after the file was opened unlinks it where it is decided."""
        config = _config(tmp_path)
        _bind(config)
        graph = Graph(download_status=500)
        _install_client(monkeypatch, config, graph)

        events = _staged(config, _events(config, _image_message()))

        assert events[0].media.error == MEDIA_FETCH_FAILED_REASON
        assert list(_media_dir(config).iterdir()) == []

    def test_bytes_that_are_not_a_decodable_image_drop_the_record(
        self, tmp_path, monkeypatch,
    ):
        """The SVG-named-`.png` case, arriving as `image/jpeg` off Meta's own
        API. The declared type decides nothing; the signature does."""
        config = _config(tmp_path)
        _bind(config)
        graph = Graph(body=SVG)
        _install_client(monkeypatch, config, graph)

        events = _staged(config, _events(config, _image_message()))

        assert events[0].media is None
        assert list(_media_dir(config).iterdir()) == []

        with db.get_db(config.db_path) as conn:
            results = handle_whatsapp_batch(
                conn, config, events, provider=db.WHATSAPP_LEGACY_PROVIDER,
            )
        assert [result.disposition for result in results] == ["unsupported_type"]

    def test_a_client_that_cannot_be_built_costs_the_media_and_not_the_batch(
        self, tmp_path, monkeypatch,
    ):
        config = _config(tmp_path)
        _bind(config)
        from istota.transport.whatsapp import client as client_module

        def boom(_config):
            raise RuntimeError("pywa went away")

        monkeypatch.setattr(client_module, "make_client", boom)

        events = _staged(config, _events(config, _image_message()))

        assert events[0].media.error is not None
        with db.get_db(config.db_path) as conn:
            results = handle_whatsapp_batch(
                conn, config, events, provider=db.WHATSAPP_LEGACY_PROVIDER,
            )
        assert [result.disposition for result in results] == ["media_failed"]
        assert results[0].response_text == MEDIA_FAILED_REPLY

    def test_a_batch_with_no_media_touches_nothing(self, tmp_path, monkeypatch):
        """A delivery-status callback is the common case, and it must not make
        a directory or run a sweep."""
        config = _config(tmp_path)
        _bind(config)
        graph = Graph()
        _install_client(monkeypatch, config, graph)
        text = {
            "id": "wamid.text", "from": USER_BSUID,
            "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
            "type": "text", "text": {"body": "hello"},
        }

        events = _staged(config, _events(config, text))

        assert graph.requests == []
        assert events[0].media is None
        assert not _media_dir(config).exists()

    def test_the_directory_is_swept_when_it_is_touched(self, tmp_path, monkeypatch):
        config = _config(tmp_path)
        _bind(config)
        _install_client(monkeypatch, config, Graph())
        staging = media.ensure_media_dir(_media_dir(config))
        orphan = staging / media.staged_name("wamid.old", "bin")
        orphan.write_bytes(PNG)
        old = time.time() - media.MEDIA_ORPHAN_SECONDS - 60
        os.utime(orphan, (old, old))

        _staged(config, _events(config, _image_message()))

        assert not orphan.exists()

    def test_a_full_staging_directory_refuses_the_fetch(
        self, tmp_path, monkeypatch,
    ):
        """The backpressure that bounds the stranger case: prune first, then
        refuse. Asserted on the transport, since the point is that the bytes
        never move."""
        config = _config(tmp_path)
        _bind(config)
        graph = Graph()
        _install_client(monkeypatch, config, graph)
        staging = media.ensure_media_dir(_media_dir(config))
        hog = staging / media.staged_name("wamid.hog", "bin")
        hog.write_bytes(b"\x00" * 1024)
        monkeypatch.setattr(media, "MEDIA_STAGING_CEILING_BYTES", 1024)

        events = _staged(config, _events(config, _image_message()))

        assert graph.requests == []
        assert events[0].media.error is not None
        assert hog.exists()


class TestGroupMessagesAreNeverFetchedFor:
    """Refused above everything, on both adapters.

    The group gate lives inside the transaction and the fetch happens before
    it, so the record is built after `_inbound_event`'s own group arm has
    reset the type — a group image carries no media record at all and there is
    nothing for the staging step to act on.
    """

    def test_a_group_image_carries_no_media_and_costs_no_request(
        self, tmp_path, monkeypatch,
    ):
        config = _config(tmp_path)
        _bind(config)
        graph = Graph()
        _install_client(monkeypatch, config, graph)
        message = _image_message()
        message["group_id"] = "120363000000000000@g.us"

        events = _staged(config, _events(config, message))

        assert events[0].message_type == "group"
        assert events[0].media is None
        assert graph.requests == []


class TestABindingThatChangedUnderneathThePreCheck:
    """The Cloud arm of the stale pre-check, which the transaction catches."""

    def test_the_media_is_dropped_and_the_message_takes_the_failed_path(
        self, tmp_path, monkeypatch, caplog,
    ):
        config = _config(tmp_path)
        _bind(config)
        _install_client(monkeypatch, config, Graph())

        events = _staged(config, _events(config, _image_message()))
        assert events[0].media.attached_for_user == "alice"
        inbox_path = events[0].media.staged_path

        # The window: bob's row takes the BSUID this message is from, and
        # alice's keeps a number nobody is writing from.
        with db.get_db(config.db_path) as conn:
            db.set_whatsapp_binding(
                conn, "alice", bootstrap_phone_number="+15557654321",
            )
            db.set_whatsapp_binding(
                conn, "bob", bootstrap_phone_number=USER_NUMBER,
                bsuid=USER_BSUID,
            )

        with caplog.at_level("WARNING"):
            with db.get_db(config.db_path) as conn:
                results = handle_whatsapp_batch(
                    conn, config, events, provider=db.WHATSAPP_LEGACY_PROVIDER,
                )

        assert [result.disposition for result in results] == ["media_failed"]
        logged = "\n".join(
            record.getMessage() for record in caplog.records
            if record.name.startswith("istota")
        )
        assert "media_misattached" in logged
        assert inbox_path in logged


class TestTheSignedRouteStagesAndAnswers:
    """The wiring itself, driven through the real FastAPI route with real bytes.

    What it pins is that `receive_whatsapp` calls the staging step at all — a
    route that skipped it would answer 200 with a `media_failed` disposition
    and no task, which is exactly what this refuses.

    It deliberately does **not** pin the *ordering*, and a route-level version
    could not: `handle_whatsapp_batch` legitimately takes the write lock, so a
    second thread holding one makes this request wait however the call is
    arranged. The ordering property is
    `tests/test_whatsapp_media_precheck.py::TestNothingStagesUnderTheWriteLock`,
    which drives the staging step directly for that reason.
    """

    def test_the_signed_route_answers_200_and_creates_the_task(
        self, tmp_path, monkeypatch,
    ):
        import hashlib
        import hmac

        from fastapi.testclient import TestClient

        from istota import webhook_receiver

        config = _config(tmp_path)
        _bind(config)
        _install_client(monkeypatch, config, Graph())
        monkeypatch.setattr(webhook_receiver, "_config", config)
        raw = json.dumps(_payload(_image_message())).encode()
        signature = "sha256=" + hmac.new(
            config.whatsapp.cloud.app_secret.encode(), raw, hashlib.sha256,
        ).hexdigest()

        response = TestClient(webhook_receiver.app).post(
            "/webhooks/whatsapp", content=raw,
            headers={
                "content-type": "application/json",
                "X-Hub-Signature-256": signature,
            },
        )

        assert response.status_code == 200
        with db.get_db(config.db_path) as conn:
            rows = conn.execute(
                "SELECT prompt, attachments FROM tasks"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "what is this?"
        assert "/Users/alice/inbox/" in rows[0][1]
