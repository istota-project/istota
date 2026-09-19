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
that it refuses on both of the two questions it asks.

Stage 3 extends this file with the lock-race and the rebind tests.
"""

from __future__ import annotations

import sqlite3

import pytest

from istota import db, sqlite_util
from istota.transport.whatsapp import media
from istota.transport.whatsapp._types import WhatsAppUserIdentity

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
