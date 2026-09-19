"""The unlocked pre-check: who a staged file is for, asked without the lock.

The media fetch and the inbox copy both happen before `handle_whatsapp_batch`
opens `BEGIN IMMEDIATE`, because a network round trip under that write lock
stalls the receiver and — under `istota serve` — the web UI with it. So
something has to resolve identity in front of the transaction, on both
adapters: Cloud needs it to keep a stranger's media off the wire, and Baileys
needs it because the sidecar has already written the file and it has nowhere
to go without a user.

It is a pre-filter rather than a boundary. The authoritative resolution and the
authoritative claim still happen inside the transaction; what this file pins is
that the cheap answer is right in the common cases, that it writes nothing, and
that it refuses on every question it asks.

The second half is the Baileys staging step built on it: the two calls the
inbound worker makes before the batch, the property the whole ordering rests on
(neither of them waits on the write lock), and the price of a pre-check that is
allowed to be stale — a binding that changed underneath it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

import pytest

from istota import db, sqlite_util
from istota.config import Config, UserConfig
from istota.transport.whatsapp import media
from istota.transport.whatsapp._types import (
    InboundWhatsAppEvent,
    WhatsAppInboundMedia,
    WhatsAppUserIdentity,
)
from istota.transport.whatsapp.baileys_bridge import stage_inbound_media
from istota.transport.whatsapp.providers.whatsapp_cloud import stage_cloud_media
from istota.transport.whatsapp.webhook import (
    MEDIA_FAILED_REPLY,
    handle_whatsapp_batch,
    normalize_payload,
)

from .support.graph_media import MEDIA_ID, Graph, install_client
from .support.whatsapp_config import build_whatsapp_config

USER_NUMBER = "+15551234567"
USER_JID = "15551234567@s.whatsapp.net"
OTHER_JID = "15559990000@s.whatsapp.net"
USER_BSUID = "US.9876543210"
OTHER_BSUID = "US.1111111111"
CLOUD = db.WHATSAPP_LEGACY_PROVIDER
BAILEYS = db.WHATSAPP_BAILEYS_PROVIDER


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


def _factory(path):
    """What both callers pass: `sqlite_util.connect_read_only`.

    Used rather than a writable connection deliberately — a pre-check that
    reached the authoritative resolver would try to latch a bootstrap binding
    and raise against this connection, so the bootstrap cases below are also
    the assertion that it does not.
    """
    return lambda: sqlite_util.connect_read_only(path)


def _identity(*, bsuid="", wa_id=None, jid=None):
    return WhatsAppUserIdentity(
        bsuid=bsuid, wa_id=wa_id, username=None, jid=jid,
    )


def _bind_identity(path, *, user_id="alice", bsuid="", jid=""):
    """Bind `user_id` to whichever adapter's identity was named.

    The two are latched by different calls — `set_whatsapp_binding` carries the
    BSUID and `latch_whatsapp_jid` is the only writer of the JID column — and
    only the first is a *holder* change, which discards `opted_out_at`. So a
    case about the opt-out has to latch before it opts out, whichever adapter
    it is about.
    """
    with db.get_db(path) as conn:
        db.set_whatsapp_binding(
            conn, user_id, bootstrap_phone_number=USER_NUMBER, bsuid=bsuid,
        )
        if jid:
            db.latch_whatsapp_jid(conn, user_id, jid=jid, username="")


def _claim(path, message_id, user_id="alice"):
    with db.get_db(path) as conn:
        conn.execute(
            "INSERT INTO processed_whatsapp "
            "(message_id, user_id, task_id, disposition, message_type, "
            " received_at) VALUES (?, ?, NULL, 'task', 'image', "
            " datetime('now'))",
            (message_id, user_id),
        )


class TestABoundIdentityResolves:
    def test_a_bound_bsuid_names_its_user(self, db_path):
        with db.get_db(db_path) as conn:
            db.set_whatsapp_binding(
                conn, "alice",
                bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID,
            )

        assert media.precheck(
            _factory(db_path),
            identity=_identity(bsuid=USER_BSUID),
            message_id="wamid.001",
            provider=CLOUD,
        ) == "alice"

    def test_a_bound_jid_names_its_user(self, db_path):
        with db.get_db(db_path) as conn:
            db.set_whatsapp_binding(
                conn, "alice", bootstrap_phone_number=USER_NUMBER,
            )
            db.latch_whatsapp_jid(conn, "alice", jid=USER_JID, username="")

        assert media.precheck(
            _factory(db_path),
            identity=_identity(jid=USER_JID),
            message_id="BAE5F00D",
            provider=BAILEYS,
        ) == "alice"

    def test_the_adapter_decides_which_field_is_read(self, db_path):
        """A Baileys event must not resolve through a Cloud BSUID, and the
        other way round — reading whichever field happens to be populated is
        the cross-adapter takeover the bindings table exists to prevent."""
        with db.get_db(db_path) as conn:
            db.set_whatsapp_binding(
                conn, "alice",
                bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID,
            )
        # A JID from a different line, so the Baileys arm cannot fall back to
        # the bootstrap number and resolve the same row by another route.
        both = _identity(bsuid=USER_BSUID, jid=OTHER_JID)

        assert media.precheck(
            _factory(db_path), identity=both,
            message_id="wamid.001", provider=CLOUD,
        ) == "alice"
        assert media.precheck(
            _factory(db_path), identity=both,
            message_id="wamid.001", provider=BAILEYS,
        ) is None

    def test_the_pre_check_covers_every_adapter_the_authoritative_arms_do(self):
        """Two hand-maintained dicts keyed on the same provider constants.

        A third adapter added to `_ARMS` alone makes `resolve_for_precheck`
        answer `None` for every message on it — every attachment dropped while
        the messages still arrive, so nothing goes red and nothing surfaces.
        """
        from istota.transport.whatsapp import identity as identity_rules

        assert set(identity_rules._PRECHECK_ARMS) == set(identity_rules._ARMS)

    def test_an_unrecognised_provider_resolves_nobody(self, db_path):
        with db.get_db(db_path) as conn:
            db.set_whatsapp_binding(
                conn, "alice",
                bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID,
            )

        assert media.precheck(
            _factory(db_path),
            identity=_identity(bsuid=USER_BSUID),
            message_id="wamid.001",
            provider="signal",
        ) is None


class TestAnUnknownSenderGoesNoFurther:
    @pytest.mark.parametrize(
        "provider,identity_kwargs",
        [
            (CLOUD, {"bsuid": OTHER_BSUID}),
            (BAILEYS, {"jid": OTHER_JID}),
            (CLOUD, {}),
            (BAILEYS, {}),
        ],
        ids=["cloud-stranger", "baileys-stranger", "cloud-empty", "baileys-empty"],
    )
    def test_nobody_is_named(self, db_path, provider, identity_kwargs):
        with db.get_db(db_path) as conn:
            db.set_whatsapp_binding(
                conn, "alice",
                bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID,
            )

        assert media.precheck(
            _factory(db_path),
            identity=_identity(**identity_kwargs),
            message_id="wamid.999",
            provider=provider,
        ) is None

    def test_a_number_whose_row_holds_another_identity_is_refused(self, db_path):
        """The recycled line. The authoritative arm refuses it and alerts, and
        a pre-filter that named the user anyway would copy a stranger's photo
        into their inbox before the transaction ever got to say no."""
        with db.get_db(db_path) as conn:
            db.set_whatsapp_binding(
                conn, "alice",
                bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID,
            )

        assert media.precheck(
            _factory(db_path),
            identity=_identity(bsuid=OTHER_BSUID, wa_id=USER_NUMBER.lstrip("+")),
            message_id="wamid.001",
            provider=CLOUD,
        ) is None


class TestTheBootstrapAnswerIsReadOnly:
    """A user's first message on an adapter is enrolled by number, and it is
    also the case where the authoritative resolver *writes* — so the pre-check
    has to answer it without latching, or the first photo anybody sends is
    refused."""

    def test_a_bootstrap_number_names_its_user_without_latching(self, db_path):
        with db.get_db(db_path) as conn:
            db.set_whatsapp_binding(
                conn, "alice", bootstrap_phone_number=USER_NUMBER,
            )

        answer = media.precheck(
            _factory(db_path),
            identity=_identity(jid=USER_JID),
            message_id="BAE5F00D",
            provider=BAILEYS,
        )

        assert answer == "alice"
        with db.get_db(db_path) as conn:
            assert db.get_whatsapp_binding(conn, "alice").jid == ""

    def test_the_cloud_arm_does_the_same(self, db_path):
        with db.get_db(db_path) as conn:
            db.set_whatsapp_binding(
                conn, "alice", bootstrap_phone_number=USER_NUMBER,
            )

        answer = media.precheck(
            _factory(db_path),
            identity=_identity(
                bsuid=USER_BSUID, wa_id=USER_NUMBER.lstrip("+"),
            ),
            message_id="wamid.001",
            provider=CLOUD,
        )

        assert answer == "alice"
        with db.get_db(db_path) as conn:
            assert db.get_whatsapp_binding(conn, "alice").bsuid == ""


class TestAClaimedMessageIdGoesNoFurther:
    """The redelivery loop, on both adapters: Meta retries a callback it got no
    200 for and the Baileys inbound worker has a bounded retry of its own.
    Without this, each replay re-fetches on Cloud and makes a second inbox copy
    on Baileys, since the authoritative claim is inside the transaction and the
    copy is in front of it."""

    @pytest.mark.parametrize(
        "provider,identity_kwargs,message_id",
        [
            (CLOUD, {"bsuid": USER_BSUID}, "wamid.001"),
            (BAILEYS, {"jid": USER_JID}, "BAE5F00D"),
        ],
        ids=["cloud", "baileys"],
    )
    def test_a_replay_is_refused(
        self, db_path, provider, identity_kwargs, message_id
    ):
        with db.get_db(db_path) as conn:
            db.set_whatsapp_binding(
                conn, "alice",
                bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID,
            )
            db.latch_whatsapp_jid(conn, "alice", jid=USER_JID, username="")

        identity = _identity(**identity_kwargs)
        assert media.precheck(
            _factory(db_path), identity=identity,
            message_id=message_id, provider=provider,
        ) == "alice"

        _claim(db_path, message_id)

        assert media.precheck(
            _factory(db_path), identity=identity,
            message_id=message_id, provider=provider,
        ) is None

    @pytest.mark.parametrize(
        "message_id", ["", "x" * 256], ids=["empty", "oversized"],
    )
    def test_a_message_id_the_surface_would_refuse_is_refused_here(
        self, db_path, message_id
    ):
        with db.get_db(db_path) as conn:
            db.set_whatsapp_binding(
                conn, "alice",
                bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID,
            )

        assert media.precheck(
            _factory(db_path),
            identity=_identity(bsuid=USER_BSUID),
            message_id=message_id,
            provider=CLOUD,
        ) is None


class TestItNeverRaises:
    """A failure here costs the attachment; the message goes on without it."""

    def test_a_connection_that_cannot_be_opened_answers_nobody(self, tmp_path):
        missing = tmp_path / "nowhere" / "istota.db"

        assert media.precheck(
            lambda: sqlite_util.connect_read_only(missing),
            identity=_identity(bsuid=USER_BSUID),
            message_id="wamid.001",
            provider=CLOUD,
        ) is None

    def test_a_query_that_fails_answers_nobody(self, tmp_path):
        """A half-upgraded host with no `processed_whatsapp` table is the live
        shape of this: refusing the media is right, raising into the caller is
        not."""
        path = tmp_path / "empty.db"
        sqlite3.connect(path).close()

        assert media.precheck(
            lambda: sqlite_util.connect_read_only(path),
            identity=_identity(bsuid=USER_BSUID),
            message_id="wamid.001",
            provider=CLOUD,
        ) is None


# ---------------------------------------------------------------------------
# The opt-out column, read one call earlier than the transaction reads it
# ---------------------------------------------------------------------------


class TestAnOptedOutSenderIsNamedByNobody:
    """The ruling the spec added after Stage 1, on both adapters.

    "The image is not retained for somebody who asked not to be messaged" is
    unsatisfiable downstream: the inbox copy happens before `BEGIN IMMEDIATE`,
    so by the time `_dispatch_inbound` reads `opted_out_at` the photograph is
    already in that user's workspace. So the pre-check reads it too — one more
    field on a row both arms already fetch — and the file is never copied.

    It gates the media alone. The *message* is untouched, which the caption
    cases in `tests/test_whatsapp_webhook.py` hold from the other end.
    """

    @pytest.mark.parametrize(
        "provider, binding_kwargs, identity_kwargs",
        [
            (CLOUD, {"bsuid": USER_BSUID}, {"bsuid": USER_BSUID}),
            (BAILEYS, {"jid": USER_JID}, {"jid": USER_JID}),
        ],
    )
    def test_a_bound_identity_that_opted_out_names_nobody(
        self, db_path, provider, binding_kwargs, identity_kwargs,
    ):
        _bind_identity(db_path, **binding_kwargs)
        with db.get_db(db_path) as conn:
            db.set_whatsapp_opt_out(conn, "alice", True)

        assert media.precheck(
            _factory(db_path),
            identity=_identity(**identity_kwargs),
            message_id="wamid.001",
            provider=provider,
        ) is None

    @pytest.mark.parametrize(
        "provider, identity_kwargs",
        [
            (CLOUD, {"bsuid": USER_BSUID, "wa_id": "15551234567"}),
            (BAILEYS, {"jid": USER_JID}),
        ],
    )
    def test_a_bootstrap_number_on_an_opted_out_row_names_nobody(
        self, db_path, provider, identity_kwargs,
    ):
        """The other return in each arm. A row with a bootstrap number and no
        identity yet is equally somebody who has said STOP."""
        with db.get_db(db_path) as conn:
            db.set_whatsapp_binding(
                conn, "alice", bootstrap_phone_number=USER_NUMBER,
            )
            db.set_whatsapp_opt_out(conn, "alice", True)

        assert media.precheck(
            _factory(db_path),
            identity=_identity(**identity_kwargs),
            message_id="wamid.001",
            provider=provider,
        ) is None

    @pytest.mark.parametrize(
        "provider, binding_kwargs, identity_kwargs",
        [
            (CLOUD, {"bsuid": USER_BSUID}, {"bsuid": USER_BSUID}),
            (BAILEYS, {"jid": USER_JID}, {"jid": USER_JID}),
        ],
    )
    def test_the_same_sender_after_start_is_named_again(
        self, db_path, provider, binding_kwargs, identity_kwargs,
    ):
        """The control. Without it the two cases above are equally satisfied
        by a pre-check that has stopped naming anybody at all."""
        _bind_identity(db_path, **binding_kwargs)
        with db.get_db(db_path) as conn:
            db.set_whatsapp_opt_out(conn, "alice", True)
            db.set_whatsapp_opt_out(conn, "alice", False)


        assert media.precheck(
            _factory(db_path),
            identity=_identity(**identity_kwargs),
            message_id="wamid.001",
            provider=provider,
        ) == "alice"


# ---------------------------------------------------------------------------
# The Baileys staging step: the two calls the worker makes before the batch
# ---------------------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 32
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>'


def _config(tmp_path) -> Config:
    path = tmp_path / "db" / "istota.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db.init_db(path)
    return Config(
        db_path=path,
        temp_dir=tmp_path / "tmp",
        workspace_path=tmp_path / "mount",
        whatsapp=build_whatsapp_config(
            enabled=True, provider="baileys",
            business_phone_number="+15551230000",
        ),
        users={"alice": UserConfig(), "bob": UserConfig()},
    )


def _media_dir(config):
    return media.ensure_media_dir(media.default_media_dir(config))


def _stage_a_file(config, payload=PNG, *, ext="jpg", message_id="BAE5F00D"):
    """Write one file where the sidecar would have written it."""
    staging = _media_dir(config)
    name = media.staged_name(message_id, ext)
    fd = media.open_staged_write(staging, name)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    return name


def _image_event(name, *, caption="what is this?", message_id="BAE5F00D"):
    """What `baileys_protocol.inbound_event` hands the worker.

    `staged_path` is the bare component the sidecar minted and
    `attached_for_user` is `""`, because nothing has resolved a user yet — the
    staging step is what fills both.
    """
    return InboundWhatsAppEvent(
        message_id=message_id,
        waba_id="", phone_number_id="",
        from_user=WhatsAppUserIdentity(
            bsuid="", wa_id=None, username=None, jid=USER_JID,
        ),
        message_type="image",
        text=caption,
        callback_data=None,
        reply_to_message_id=None,
        sent_at=datetime.now(timezone.utc),
        media=WhatsAppInboundMedia(
            staged_path=name, mime_type="image/jpeg", byte_count=len(PNG),
            attached_for_user="", error=None,
        ),
    )


def _cloud_config(tmp_path) -> Config:
    """The same deployment as `_config`, configured for the Cloud adapter."""
    path = tmp_path / "db" / "istota.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db.init_db(path)
    return Config(
        db_path=path,
        temp_dir=tmp_path / "tmp",
        workspace_path=tmp_path / "mount",
        whatsapp=build_whatsapp_config(
            enabled=True,
            waba_id="123456789012345",
            phone_number_id="223456789012345",
            business_phone_number="+15551230000",
            access_token="wa-access-token",
            app_secret="wa-app-secret",
            verify_token="wa-verify-token",
        ),
        users={"alice": UserConfig(), "bob": UserConfig()},
    )


def _cloud_image_events(config):
    """One normalized `image` event, as the signed route produces it."""
    return normalize_payload(config, {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "123456789012345",
            "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {
                    "display_phone_number": "15551230000",
                    "phone_number_id": "223456789012345",
                },
                "contacts": [{
                    "user_id": USER_BSUID, "wa_id": USER_NUMBER.lstrip("+"),
                    "profile": {"name": "Alice"},
                }],
                "messages": [{
                    "id": "wamid.img", "from": USER_BSUID,
                    "timestamp": str(
                        int(datetime.now(timezone.utc).timestamp())
                    ),
                    "type": "image",
                    "image": {"id": MEDIA_ID, "mime_type": "image/jpeg",
                              "caption": "what is this?"},
                }],
            }}],
        }],
    })


class TestTheBaileysStagingStep:
    """Everything between the frame and the transaction, on the worker thread.

    The sidecar has already fetched the bytes; what is left is a read-only
    identity lookup and a copy into Nextcloud, neither of which may happen
    under `BEGIN IMMEDIATE`.
    """

    def test_a_staged_image_becomes_an_attachment_and_leaves_nothing_behind(
        self, tmp_path,
    ):
        config = _config(tmp_path)
        _bind_identity(config.db_path, jid=USER_JID)
        name = _stage_a_file(config)

        staged = stage_inbound_media(config, _media_dir(config),
                                     _image_event(name))

        assert staged.media.attached_for_user == "alice"
        assert staged.media.staged_path.startswith("/Users/alice/inbox/")
        assert staged.media.error is None
        assert staged.text == "what is this?"
        assert not (_media_dir(config) / name).exists()

    def test_an_unknown_sender_gets_no_copy_and_leaves_no_file(self, tmp_path):
        config = _config(tmp_path)
        name = _stage_a_file(config)

        staged = stage_inbound_media(config, _media_dir(config),
                                     _image_event(name))

        assert staged.media.error == media.MEDIA_UNATTRIBUTED
        assert staged.media.staged_path == ""
        assert not (_media_dir(config) / name).exists()
        assert not (config.workspace_path / "Users").exists()

    def test_an_opted_out_sender_gets_no_copy_and_leaves_no_file(self, tmp_path):
        """The ruling, from the staging side: the photograph is never copied,
        and the caption still reaches the text path as a message."""
        config = _config(tmp_path)
        _bind_identity(config.db_path, jid=USER_JID)
        with db.get_db(config.db_path) as conn:
            db.set_whatsapp_opt_out(conn, "alice", True)
        name = _stage_a_file(config)

        staged = stage_inbound_media(config, _media_dir(config),
                                     _image_event(name, caption="START"))

        assert staged.media.error == media.MEDIA_UNATTRIBUTED
        assert staged.text == "START"
        assert not (_media_dir(config) / name).exists()
        assert not (config.workspace_path / "Users").exists()

    def test_a_message_id_already_claimed_makes_no_second_copy(self, tmp_path):
        """The redelivery loop. The authoritative claim is inside the
        transaction and the copy is in front of it, so without this read every
        replayed frame would put another file in the user's inbox."""
        config = _config(tmp_path)
        _bind_identity(config.db_path, jid=USER_JID)
        _claim(config.db_path, "BAE5F00D")
        name = _stage_a_file(config)

        staged = stage_inbound_media(config, _media_dir(config),
                                     _image_event(name))

        assert staged.media.error == media.MEDIA_UNATTRIBUTED
        assert not (_media_dir(config) / name).exists()
        assert not (config.workspace_path / "Users").exists()

    def test_a_file_that_is_not_a_decodable_image_drops_the_record(
        self, tmp_path,
    ):
        """The SVG-named-`.png` case, and the one refusal that drops the
        record rather than marking it failed: this is not an image at all, so
        the message earns the unsupported reply the surface already had."""
        config = _config(tmp_path)
        _bind_identity(config.db_path, jid=USER_JID)
        name = _stage_a_file(config, SVG, ext="png")

        staged = stage_inbound_media(config, _media_dir(config),
                                     _image_event(name))

        assert staged.media is None
        assert not (_media_dir(config) / name).exists()

    def test_a_staged_file_that_is_gone_is_a_failure_and_not_a_refusal(
        self, tmp_path,
    ):
        """`sniff_staged` answers `None` for a file it could not open exactly
        as it does for one whose bytes are not an image, and the two owe
        different replies: a missing file is istota's own failure, while "not
        an image" is the unsupported answer that also throws the caption away.

        Reachable rather than theoretical — a worker far enough behind used to
        meet its own file's orphan window, which is why the sweep moved to a
        `finally` after the consume.
        """
        config = _config(tmp_path)
        _bind_identity(config.db_path, jid=USER_JID)
        name = media.staged_name("BAE5F00D", "jpg")

        staged = stage_inbound_media(config, _media_dir(config),
                                     _image_event(name))

        assert staged.media.error == media.MEDIA_NOT_PLACED
        assert staged.text == "what is this?"

    def test_the_sweep_never_takes_the_file_this_call_is_about(self, tmp_path):
        """The prune runs after the consume, not before it. Staged 600 seconds
        ago and still the subject of this event, the file must be attached
        rather than swept."""
        config = _config(tmp_path)
        _bind_identity(config.db_path, jid=USER_JID)
        name = _stage_a_file(config)
        old = time.time() - media.MEDIA_ORPHAN_SECONDS - 60
        os.utime(_media_dir(config) / name, (old, old))

        staged = stage_inbound_media(config, _media_dir(config),
                                     _image_event(name))

        assert staged.media.error is None
        assert staged.media.staged_path.startswith("/Users/alice/inbox/")

    def test_a_name_that_is_not_one_component_is_never_joined(self, tmp_path):
        config = _config(tmp_path)
        _bind_identity(config.db_path, jid=USER_JID)

        staged = stage_inbound_media(
            config, _media_dir(config), _image_event("../../etc/passwd"),
        )

        assert staged.media.error == media.MEDIA_UNATTRIBUTED
        assert staged.media.staged_path == ""

    def test_a_record_that_already_failed_is_carried_through_untouched(
        self, tmp_path,
    ):
        """The sidecar's own failure marker. Nothing was staged, so there is
        nothing to look up and nothing to unlink."""
        config = _config(tmp_path)
        failed = WhatsAppInboundMedia(
            staged_path="", mime_type="", byte_count=0,
            attached_for_user="", error="the image could not be downloaded",
        )
        event = dataclasses.replace(_image_event("unused"), media=failed)

        staged = stage_inbound_media(config, _media_dir(config), event)

        assert staged.media is failed

    def test_a_raising_storage_layer_costs_the_media_and_not_the_message(
        self, tmp_path, monkeypatch,
    ):
        """Wider than `stage_to_attachment`'s own contract, deliberately.

        That one catches `OSError` and `ValueError`; anything else out of the
        storage layer would reach the worker's handler and cost the whole
        message — a media failure taking the caption with it, which is this
        path's rule inverted.
        """
        config = _config(tmp_path)
        _bind_identity(config.db_path, jid=USER_JID)
        name = _stage_a_file(config)

        def boom(*args, **kwargs):
            raise RuntimeError("the workspace exploded")

        monkeypatch.setattr(
            "istota.storage.ensure_user_directories_v2", boom,
        )

        staged = stage_inbound_media(config, _media_dir(config),
                                     _image_event(name))

        assert staged.media.error == media.MEDIA_NOT_PLACED
        assert staged.text == "what is this?"

    def test_the_directory_is_swept_on_every_touch(self, tmp_path):
        """`_prune_parked_statuses`' arrangement, on the one adapter where the
        daemon cannot refuse a fetch the sidecar has already made."""
        config = _config(tmp_path)
        _bind_identity(config.db_path, jid=USER_JID)
        orphan = _media_dir(config) / media.staged_name("BAE5DEAD", "jpg")
        orphan.write_bytes(PNG)
        old = time.time() - media.MEDIA_ORPHAN_SECONDS - 60
        os.utime(orphan, (old, old))
        name = _stage_a_file(config)

        stage_inbound_media(config, _media_dir(config), _image_event(name))

        assert not orphan.exists()


class TestNothingStagesUnderTheWriteLock:
    """The property the whole design rests on, driven against a real lock.

    A media fetch or an inbox copy inside `BEGIN IMMEDIATE` waits out the
    30-second busy timeout against the lock the caller holds, and under
    `istota serve` that router is on the web app's event loop — so it stalls
    the receiver and the web UI together. The pre-check is read-only and the
    copy touches no database at all, so both complete while another connection
    holds the write lock.

    **Both adapters, because the claim is about both.** Baileys does its
    staging on the bridge's worker thread and Cloud does its whole fetch inside
    the route, which is the harder case: the Graph round trip is there as well
    as the pre-check and the copy.
    """

    def test_the_stage_completes_while_a_second_thread_holds_the_lock(
        self, tmp_path,
    ):
        config = _config(tmp_path)
        _bind_identity(config.db_path, jid=USER_JID)
        name = _stage_a_file(config)
        holding = threading.Event()
        release = threading.Event()
        held: list[Exception] = []

        def hold_the_write_lock():
            try:
                conn = sqlite3.connect(config.db_path, timeout=30.0)
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "INSERT INTO processed_whatsapp "
                        "(message_id, user_id, task_id, disposition, "
                        " message_type, received_at) "
                        "VALUES ('lock.holder', 'alice', NULL, 'task', "
                        " 'text', datetime('now'))"
                    )
                    holding.set()
                    release.wait(timeout=30)
                    conn.rollback()
                finally:
                    conn.close()
            except Exception as exc:  # pragma: no cover - reported below
                held.append(exc)
                holding.set()

        writer = threading.Thread(target=hold_the_write_lock)
        writer.start()
        try:
            assert holding.wait(timeout=10)
            assert not held, held
            started = time.monotonic()
            staged = stage_inbound_media(config, _media_dir(config),
                                         _image_event(name))
            elapsed = time.monotonic() - started
        finally:
            release.set()
            writer.join(timeout=30)

        # The timing assertion comes first because it is the property, and
        # because it is what names the failure: a stage that took the write
        # lock would *also* finish — after its busy timeout, or once the
        # holder let go — so "it did not raise" asserts nothing here. The
        # bound is far above what the work costs (milliseconds) and far below
        # any busy timeout on the path.
        assert elapsed < 2.0
        assert staged.media.attached_for_user == "alice"
        assert staged.media.staged_path.startswith("/Users/alice/inbox/")

    def test_the_cloud_route_fetches_while_a_second_thread_holds_the_lock(
        self, tmp_path, monkeypatch,
    ):
        """The Cloud arm: the Graph round trip is inside the request, and the
        request must not be inside the transaction.

        The stub answers instantly, so this cannot measure Meta — what it
        measures is whether the staging step waits on a lock, which is the only
        thing on this path that could take seconds against a local database.
        """
        config = _cloud_config(tmp_path)
        with db.get_db(config.db_path) as conn:
            db.set_whatsapp_binding(
                conn, "alice",
                bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID,
            )
        install_client(monkeypatch, Graph())
        events = _cloud_image_events(config)
        holding = threading.Event()
        release = threading.Event()
        held: list[Exception] = []

        def hold_the_write_lock():
            try:
                conn = sqlite3.connect(config.db_path, timeout=30.0)
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "INSERT INTO processed_whatsapp "
                        "(message_id, user_id, task_id, disposition, "
                        " message_type, received_at) "
                        "VALUES ('lock.holder', 'alice', NULL, 'task', "
                        " 'text', datetime('now'))"
                    )
                    holding.set()
                    release.wait(timeout=30)
                    conn.rollback()
                finally:
                    conn.close()
            except Exception as exc:  # pragma: no cover - reported below
                held.append(exc)
                holding.set()

        writer = threading.Thread(target=hold_the_write_lock)
        writer.start()
        try:
            assert holding.wait(timeout=10)
            assert not held, held
            started = time.monotonic()
            staged = asyncio.run(stage_cloud_media(config, events))
            elapsed = time.monotonic() - started
        finally:
            release.set()
            writer.join(timeout=30)

        assert elapsed < 2.0
        assert staged[0].media.attached_for_user == "alice"
        assert staged[0].media.staged_path.startswith("/Users/alice/inbox/")


class TestABindingThatChangedUnderneathThePreCheck:
    """The price of the pre-check being allowed to be stale.

    The file was copied on the strength of an answer read outside the lock. If
    the binding moved in the window, the authoritative resolution names
    somebody else — and the photograph is already in the first user's
    workspace, where nothing can un-copy it.
    """

    def test_the_media_is_dropped_and_the_stranded_copy_is_named(
        self, tmp_path, caplog,
    ):
        config = _config(tmp_path)
        _bind_identity(config.db_path, jid=USER_JID)
        name = _stage_a_file(config)

        staged = stage_inbound_media(config, _media_dir(config),
                                     _image_event(name))
        assert staged.media.attached_for_user == "alice"
        inbox_path = staged.media.staged_path

        # The window: alice's row takes a different number, which discards the
        # JID with it, and bob's row takes the one the sender is writing from.
        # The batch's own resolution then bootstrap-latches onto bob.
        with db.get_db(config.db_path) as conn:
            db.set_whatsapp_binding(
                conn, "alice", bootstrap_phone_number="+15557654321",
            )
            db.set_whatsapp_binding(
                conn, "bob", bootstrap_phone_number=USER_NUMBER,
            )

        with caplog.at_level("WARNING"):
            with db.get_db(config.db_path) as conn:
                results = handle_whatsapp_batch(
                    conn, config, [staged], provider=BAILEYS,
                )

        assert [result.disposition for result in results] == ["media_failed"]
        assert results[0].response_text == MEDIA_FAILED_REPLY
        with db.get_db(config.db_path) as conn:
            assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        logged = "\n".join(
            record.getMessage() for record in caplog.records
            if record.name.startswith("istota")
        )
        assert "media_misattached" in logged
        assert inbox_path in logged
        # Nothing can un-copy it, which is why the warning exists.
        assert (
            config.workspace_path / inbox_path.lstrip("/")
        ).exists()

    def test_the_same_message_with_the_binding_intact_becomes_a_task(
        self, tmp_path,
    ):
        """The control. Without it the case above is equally satisfied by a
        comparison that refuses every staged file."""
        config = _config(tmp_path)
        _bind_identity(config.db_path, jid=USER_JID)
        name = _stage_a_file(config)

        staged = stage_inbound_media(config, _media_dir(config),
                                     _image_event(name))
        with db.get_db(config.db_path) as conn:
            results = handle_whatsapp_batch(
                conn, config, [staged], provider=BAILEYS,
            )

        assert [result.disposition for result in results] == ["task"]
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, results[0].task_id)
        assert task.attachments == [staged.media.staged_path]
