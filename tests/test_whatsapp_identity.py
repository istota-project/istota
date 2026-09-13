"""Per-adapter WhatsApp identity: the columns, the arms, and what separates them.

The stage this covers adds a second durable identity to a table that had one.
Almost every case here is therefore about a *boundary between two adapters*
rather than about either adapter's own behaviour, which the existing suites
already cover: that a Baileys event cannot resolve through a Cloud BSUID and a
Cloud event cannot resolve through a JID; that a latch crossing from one to the
other discards what the previous holder earned; and that neither the migration
nor the operator's own re-run of `user ensure` can quietly unlatch a working
binding.

The Cloud arm's own five rules are asserted in `tests/test_whatsapp_webhook.py`
and are deliberately not restated here — they moved module without changing,
and that whole suite passing unmodified is the evidence. What *is* restated is
the pair of rules the move made newly checkable: that the arms answer
independently, and that the provider decides which one runs.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from istota import db
from istota.config import Config, UserConfig, WHATSAPP_PROVIDER_NAMES
from istota.transport.whatsapp import identity as identity_rules
from istota.transport.whatsapp._types import (
    InboundWhatsAppEvent,
    WhatsAppUserIdentity,
)
from istota.transport.whatsapp.webhook import handle_whatsapp_batch

from .support.whatsapp_config import build_whatsapp_config

USER_NUMBER = "+15551234567"
USER_JID = "15551234567@s.whatsapp.net"
OTHER_JID = "15559990000@s.whatsapp.net"
USER_BSUID = "US.9876543210"
CLOUD = db.WHATSAPP_LEGACY_PROVIDER
BAILEYS = db.WHATSAPP_BAILEYS_PROVIDER


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    with db.get_db(path) as connection:
        yield connection


def _config(tmp_path, *, provider=BAILEYS, **overrides) -> Config:
    path = tmp_path / "istota.db"
    db.init_db(path)
    fields = dict(
        enabled=True,
        provider=provider,
        waba_id="123456789012345",
        phone_number_id="223456789012345",
        business_phone_number="+15551230000",
        access_token="wa-access-token",
        app_secret="wa-app-secret",
        verify_token="wa-verify-token",
        business_timezone="UTC",
    )
    fields.update(overrides)
    return Config(
        db_path=path,
        temp_dir=tmp_path / "tmp",
        whatsapp=build_whatsapp_config(**fields),
        users={"alice": UserConfig()},
    )


def _event(*, jid=None, bsuid="", wa_id=None, username=None, message_id="wamid.001"):
    return InboundWhatsAppEvent(
        message_id=message_id,
        waba_id="123456789012345",
        phone_number_id="223456789012345",
        from_user=WhatsAppUserIdentity(
            bsuid=bsuid, wa_id=wa_id, username=username, jid=jid,
        ),
        message_type="text",
        text="check the backup",
        callback_data=None,
        reply_to_message_id=None,
        sent_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------


class TestTheColumnMigration:
    """Column-add only, on a table that already exists without them."""

    def _old_shape(self, path):
        """The `whatsapp_user_bindings` of the release before this stage.

        Written by hand rather than by an older `schema.sql`, because what the
        migration has to survive is a *table*, and reconstructing one is the
        only way to have a database that genuinely predates the columns.
        """
        with sqlite3.connect(path) as raw:
            raw.execute(
                """
                CREATE TABLE whatsapp_user_bindings (
                    user_id TEXT PRIMARY KEY,
                    bootstrap_phone_number TEXT NOT NULL DEFAULT '',
                    bsuid TEXT NOT NULL DEFAULT '',
                    send_id TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT '',
                    opted_out_at TEXT,
                    last_user_message_at TEXT,
                    enrolled_at TEXT,
                    last_seen_at TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            raw.execute(
                "INSERT INTO whatsapp_user_bindings "
                "(user_id, bootstrap_phone_number, bsuid, send_id, "
                " last_user_message_at, updated_at) "
                "VALUES ('alice', ?, ?, ?, '2026-01-01 00:00:00', "
                "'2026-01-01 00:00:00')",
                (USER_NUMBER, USER_BSUID, USER_BSUID),
            )

    def test_an_old_table_gains_the_columns_and_keeps_its_row(self, tmp_path):
        path = tmp_path / "istota.db"
        self._old_shape(path)

        db.init_db(path)

        with db.get_db(path) as conn:
            columns = {r[1] for r in conn.execute(
                "PRAGMA table_info(whatsapp_user_bindings)"
            )}
            binding = db.get_whatsapp_binding(conn, "alice")
        assert {"jid", "provider"} <= columns
        # No backfill: the row is untouched apart from gaining two NULLs.
        assert binding.bsuid == USER_BSUID
        assert binding.last_user_message_at == "2026-01-01 00:00:00"

    def test_the_stored_null_reads_as_the_legacy_provider(self, tmp_path):
        """The whole no-backfill argument in one assertion.

        The column is NULL on disk and `whatsapp_cloud` in Python. If those
        two ever disagree, every pre-stage row becomes a row belonging to no
        adapter — which the resolver reads as a foreign provider and whose
        derived state the next latch would discard.
        """
        path = tmp_path / "istota.db"
        self._old_shape(path)
        db.init_db(path)

        with db.get_db(path) as conn:
            raw = conn.execute(
                "SELECT jid, provider FROM whatsapp_user_bindings "
                "WHERE user_id = 'alice'"
            ).fetchone()
            binding = db.get_whatsapp_binding(conn, "alice")
        assert raw["jid"] is None and raw["provider"] is None
        assert binding.jid == ""
        assert binding.provider == CLOUD

    def test_a_second_init_db_is_a_no_op(self, tmp_path):
        """Idempotence, asserted on the schema rather than on "it did not raise".

        `sqlite_util.add_columns` re-checks on the way out, so a second run
        must add nothing and must not duplicate the index — and a run that
        raised `duplicate column name` would be caught by the call itself
        rather than by anything here.
        """
        path = tmp_path / "istota.db"
        self._old_shape(path)

        db.init_db(path)
        with db.get_db(path) as conn:
            before = [
                tuple(r) for r in conn.execute(
                    "PRAGMA table_info(whatsapp_user_bindings)"
                )
            ]
        db.init_db(path)
        db.init_db(path)
        with db.get_db(path) as conn:
            after = [
                tuple(r) for r in conn.execute(
                    "PRAGMA table_info(whatsapp_user_bindings)"
                )
            ]
            jid_indexes = [
                r[1] for r in conn.execute(
                    "PRAGMA index_list(whatsapp_user_bindings)"
                )
                if r[1] == "idx_whatsapp_binding_jid"
            ]
        assert before == after
        assert jid_indexes == ["idx_whatsapp_binding_jid"]

    def test_two_users_cannot_hold_one_jid(self, conn):
        """The fourth partial unique index, and the operator's wording for it.

        A second row claiming one JID is a principal takeover rather than a
        duplicate, which is the same reason the other three exist.
        """
        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number=USER_NUMBER)
        db.set_whatsapp_binding(conn, "bob", bootstrap_phone_number="+15557654321")
        assert db.latch_whatsapp_jid(conn, "alice", jid=USER_JID)

        with pytest.raises(sqlite3.IntegrityError):
            db.latch_whatsapp_jid(conn, "bob", jid=USER_JID)

    def test_an_unbound_row_is_not_in_the_jid_index(self, conn):
        """`WHERE jid <> ''` has to exclude a NULL as well as an empty string.

        A plain UNIQUE — or a partial index whose predicate a NULL satisfied —
        would let the *second* user with no Baileys identity fail to insert,
        which on an upgraded database is every user.
        """
        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number=USER_NUMBER)
        db.set_whatsapp_binding(conn, "bob", bootstrap_phone_number="+15557654321")
        db.set_whatsapp_binding(conn, "carol", bootstrap_phone_number="+15557654322")

        assert {b.user_id for b in db.list_whatsapp_bindings(conn)} == {
            "alice", "bob", "carol",
        }

    def test_the_two_provider_spellings_match_the_config_tuple(self):
        """The drift guard `db`'s own docstring promises.

        `db` cannot import `config` and `config` cannot import `db`, so the
        provider names are written twice on purpose. A rename on one side
        alone would leave the legacy default naming an adapter the registry
        cannot build, and every pre-stage row belonging to it.
        """
        assert set(WHATSAPP_PROVIDER_NAMES) == {CLOUD, BAILEYS}


# ---------------------------------------------------------------------------
# JID normalization
# ---------------------------------------------------------------------------


class TestJidNormalization:
    """One spelling, or a legitimate user trips the takeover alarm."""

    @pytest.mark.parametrize("raw", [
        USER_JID,
        "15551234567:42@s.whatsapp.net",          # a linked device
        "  15551234567@S.WhatsApp.Net  ",          # case and whitespace
    ])
    def test_every_spelling_of_one_sender_normalizes_alike(self, raw):
        assert identity_rules.normalize_jid(raw) == USER_JID

    @pytest.mark.parametrize("raw", [
        "",
        None,
        12345,
        "15551234567",                              # no server
        "15551234567@g.us",                         # a group
        "15551234567@broadcast",                    # a list
        "8613800138000@lid",                        # the number-hiding namespace
        "alice@s.whatsapp.net",                     # not a number
        "@s.whatsapp.net",
        "1" * 200 + "@s.whatsapp.net",
        # `str.isdigit()` alone is True for these. The value reaches a
        # uniquely-indexed identity column, and `is_e164` is explicit ASCII,
        # so such a JID could be stored as an identity that can never enroll.
        "١٢٣٤٥٦٧٨@s.whatsapp.net",
    ])
    def test_anything_this_surface_does_not_model_is_refused(self, raw):
        assert identity_rules.normalize_jid(raw) == ""

    def test_the_number_comes_out_as_exact_e164(self):
        assert identity_rules.jid_number("15551234567:3@s.whatsapp.net") == USER_NUMBER
        assert identity_rules.jid_number("15551234567@g.us") == ""
        # Too short to be E.164, so it is not a number to enroll against.
        assert identity_rules.jid_number("12@s.whatsapp.net") == ""

    def test_a_device_suffix_does_not_trip_the_recycled_number_refusal(self, conn):
        """The normalization rule's whole reason, driven end to end.

        The stored form is bare and the next message arrives device-suffixed.
        Without one spelling the lookup misses, the bootstrap arm finds the
        row already carrying a *different* JID, and a user who did nothing
        gets a takeover alert and a dropped message on every message.
        """
        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number=USER_NUMBER)
        first = identity_rules.resolve_inbound_identity(
            conn, _event(jid=USER_JID).from_user, provider=BAILEYS,
        )
        assert first.user_id == "alice"

        again = identity_rules.resolve_inbound_identity(
            conn, _event(jid="15551234567:7@s.whatsapp.net").from_user,
            provider=BAILEYS,
        )
        assert again.user_id == "alice"
        assert again.disposition is None
        assert again.pending_alert is None


# ---------------------------------------------------------------------------
# The Baileys arm
# ---------------------------------------------------------------------------


class TestTheBaileysArm:
    def test_a_bootstrap_number_latches_the_jid_and_stamps_the_provider(self, conn):
        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number=USER_NUMBER)

        resolution = identity_rules.resolve_inbound_identity(
            conn, _event(jid=USER_JID, username="alice-wa").from_user,
            provider=BAILEYS,
        )

        assert resolution.user_id == "alice"
        binding = db.get_whatsapp_binding(conn, "alice")
        assert binding.jid == USER_JID
        assert binding.provider == BAILEYS
        assert binding.username == "alice-wa"
        assert binding.enrolled_at is not None

    def test_a_latched_jid_wins_outright_on_the_next_message(self, conn):
        """A matching identity resolves without consulting the number at all.

        Asserted by removing the number the bootstrap arm would have used: if
        the lookup were not authoritative this would fall through to a row it
        can no longer find.
        """
        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number=USER_NUMBER)
        assert db.latch_whatsapp_jid(conn, "alice", jid=USER_JID)
        conn.execute(
            "UPDATE whatsapp_user_bindings SET bootstrap_phone_number = '+15550000001' "
            "WHERE user_id = 'alice'"
        )

        resolution = identity_rules.resolve_inbound_identity(
            conn, _event(jid=USER_JID).from_user, provider=BAILEYS,
        )
        assert resolution.user_id == "alice"

    def test_a_recycled_number_is_refused_and_alerted(self, conn):
        """The takeover guard, one adapter over.

        The row's number matches and its JID does not, which means the person
        holding the number now is not the person the row is about. Latching
        there would hand alice's Istota principal to a stranger.
        """
        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number=USER_NUMBER)
        assert db.latch_whatsapp_jid(conn, "alice", jid=OTHER_JID)
        conn.execute(
            "UPDATE whatsapp_user_bindings SET bootstrap_phone_number = ? "
            "WHERE user_id = 'alice'", (USER_NUMBER,),
        )

        resolution = identity_rules.resolve_inbound_identity(
            conn, _event(jid=USER_JID).from_user, provider=BAILEYS,
        )

        assert resolution.user_id is None
        assert resolution.disposition == "identity_mismatch"
        assert resolution.pending_alert is not None
        # Nothing was learned from the refused message.
        assert db.get_whatsapp_binding(conn, "alice").jid == OTHER_JID

    def test_the_two_adapters_alerts_do_not_share_a_dedup_row(self, conn):
        """Two adapters, two alarms — or the second one is silent.

        `notifications` dedups on `(user_id, source, dedup_key)` and a *bump*
        does not deliver, so a shared key would make the Baileys mismatch
        raise nothing the operator sees.
        """
        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number=USER_NUMBER)
        cloud_alert = identity_rules._write_identity_alert(conn, "alice", USER_BSUID)
        jid_alert = identity_rules._write_jid_identity_alert(conn, "alice", USER_JID)

        keys = [
            r["dedup_key"] for r in conn.execute(
                "SELECT dedup_key FROM notifications WHERE user_id = 'alice'"
            )
        ]
        assert cloud_alert is not None and jid_alert is not None
        assert len(set(keys)) == 2

    def test_no_alert_body_or_key_carries_the_number(self, conn):
        """A JID is a phone number in plain digits. Neither may be written down."""
        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number=USER_NUMBER)
        identity_rules._write_jid_identity_alert(conn, "alice", USER_JID)

        row = conn.execute(
            "SELECT dedup_key, title, body, params FROM notifications "
            "WHERE user_id = 'alice'"
        ).fetchone()
        rendered = " ".join(str(row[k]) for k in row.keys())
        assert "15551234567" not in rendered
        assert "s.whatsapp.net" not in rendered

    def test_a_latch_that_loses_its_race_fails_closed(self, conn, monkeypatch):
        """The conditional write is the arbiter, not the read above it."""
        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number=USER_NUMBER)
        monkeypatch.setattr(db, "latch_whatsapp_jid", lambda *a, **k: False)

        resolution = identity_rules.resolve_inbound_identity(
            conn, _event(jid=USER_JID).from_user, provider=BAILEYS,
        )
        assert resolution.user_id is None
        assert resolution.disposition == "identity_conflict"

    def test_a_jid_another_user_holds_resolves_to_that_user(self, conn):
        """The identity outranks the number, which is rule 1 and rule 2.

        Written the other way round first, expecting a refusal — wrongly. The
        JID *is* the identity, so a row holding it is the row the message
        belongs to whoever else's bootstrap number happens to match; treating
        that as a conflict would be the number deciding, which is the thing
        the ordering exists to prevent.
        """
        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number=USER_NUMBER)
        db.set_whatsapp_binding(conn, "bob", bootstrap_phone_number="+15557654321")
        assert db.latch_whatsapp_jid(conn, "bob", jid=USER_JID)

        resolution = identity_rules.resolve_inbound_identity(
            conn, _event(jid=USER_JID).from_user, provider=BAILEYS,
        )
        assert resolution.user_id == "bob"

    def test_a_latch_colliding_with_another_row_fails_closed(self, conn, monkeypatch):
        """The partial unique index is the arbiter and this side lost.

        Reachable only as a race — the lookup above would have found the other
        row — so the collision is injected rather than staged. What is being
        asserted is the disposition, not the sqlite mechanics: an
        `IntegrityError` must never become an identity this side takes.
        """
        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number=USER_NUMBER)

        def _collide(*args, **kwargs):
            raise sqlite3.IntegrityError(
                "UNIQUE constraint failed: whatsapp_user_bindings.jid"
            )

        monkeypatch.setattr(db, "latch_whatsapp_jid", _collide)
        resolution = identity_rules.resolve_inbound_identity(
            conn, _event(jid=USER_JID).from_user, provider=BAILEYS,
        )
        assert resolution.user_id is None
        assert resolution.disposition == "identity_conflict"

    def test_an_unknown_number_enrolls_nobody(self, conn):
        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number=USER_NUMBER)

        resolution = identity_rules.resolve_inbound_identity(
            conn, _event(jid=OTHER_JID).from_user, provider=BAILEYS,
        )
        assert resolution.user_id is None
        assert resolution.disposition == "unknown_sender"
        assert db.get_whatsapp_binding(conn, "alice").jid == ""


# ---------------------------------------------------------------------------
# Cross-adapter isolation
# ---------------------------------------------------------------------------


class TestTheArmsAnswerIndependently:
    """Neither adapter may read the other's identity. Both directions."""

    def test_a_baileys_event_never_resolves_through_a_bsuid(self, conn):
        db.set_whatsapp_binding(
            conn, "alice",
            bootstrap_phone_number="+15550000009",  # not the JID's number
            bsuid=USER_BSUID,
        )

        resolution = identity_rules.resolve_inbound_identity(
            conn, _event(jid=USER_JID, bsuid=USER_BSUID).from_user,
            provider=BAILEYS,
        )
        assert resolution.user_id is None
        assert resolution.disposition == "unknown_sender"

    def test_a_cloud_event_never_resolves_through_a_jid(self, conn):
        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number="+15550000009")
        assert db.latch_whatsapp_jid(conn, "alice", jid=USER_JID)

        resolution = identity_rules.resolve_inbound_identity(
            conn,
            _event(bsuid=USER_BSUID, wa_id="15551234567", jid=USER_JID).from_user,
            provider=CLOUD,
        )
        assert resolution.user_id is None
        assert resolution.disposition == "unknown_sender"

    def test_an_unrecognised_provider_resolves_nobody(self, conn):
        """No arm can read its identity, so falling back to either would
        resolve a principal from a field the event never carried."""
        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number=USER_NUMBER)

        resolution = identity_rules.resolve_inbound_identity(
            conn, _event(jid=USER_JID, bsuid=USER_BSUID).from_user,
            provider="signal",
        )
        assert resolution.user_id is None
        assert resolution.disposition == "unknown_sender"
        assert db.get_whatsapp_binding(conn, "alice").jid == ""


class TestTheCrossAdapterDiscard:
    """A latch onto the other adapter's row discards the previous holder's state.

    A row with no identity *of this adapter* used to be a row that had never
    carried an authenticated message, so its window and opt-out were NULL and
    the latch had nothing to discard. Two adapters break that: a Baileys row
    has no BSUID and a full history, and a Cloud row has no JID and the same.
    """

    def _row_with_history(self, conn, *, provider):
        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number=USER_NUMBER)
        conn.execute(
            "UPDATE whatsapp_user_bindings "
            "   SET provider = ?, last_user_message_at = '2026-01-01 00:00:00', "
            "       opted_out_at = '2026-01-01 00:00:00' "
            " WHERE user_id = 'alice'",
            (provider,),
        )

    def test_latching_baileys_onto_a_cloud_row_discards_the_window(self, conn):
        self._row_with_history(conn, provider=CLOUD)

        assert db.latch_whatsapp_jid(conn, "alice", jid=USER_JID)

        binding = db.get_whatsapp_binding(conn, "alice")
        assert binding.provider == BAILEYS
        assert binding.last_user_message_at is None

    def test_latching_cloud_onto_a_baileys_row_discards_the_window(self, conn):
        """The direction that actually authorizes something.

        `last_user_message_at` is the sole input to *Meta's* 24-hour window,
        and a Baileys message does not open one — so carrying the stamp into a
        Cloud latch would authorize a free-form send under a window Meta does
        not hold.
        """
        self._row_with_history(conn, provider=BAILEYS)

        assert db.latch_whatsapp_bsuid(
            conn, "alice", bsuid=USER_BSUID, send_id=USER_BSUID,
        )

        binding = db.get_whatsapp_binding(conn, "alice")
        assert binding.provider == CLOUD
        assert binding.last_user_message_at is None

    @pytest.mark.parametrize("stored, latch", [
        (CLOUD, "jid"),
        (BAILEYS, "bsuid"),
    ])
    def test_an_opt_out_survives_an_adapter_switch(self, conn, stored, latch):
        """The spec says so, and discarding is the unsafe direction.

        Design C: "switching providers preserves user bindings, routes,
        conversation history, opt-outs, and common ledger rows", and the
        adapter-switch edge case repeats it. The first version of this stage
        discarded it along with the window, on the reading that the column
        names a previous holder — which is wrong on a migration, where only
        the adapter changed. It is also the one field where being wrong
        resumes messaging somebody who sent STOP, rather than withholding
        from somebody who did not.
        """
        self._row_with_history(conn, provider=stored)

        if latch == "jid":
            assert db.latch_whatsapp_jid(conn, "alice", jid=USER_JID)
        else:
            assert db.latch_whatsapp_bsuid(
                conn, "alice", bsuid=USER_BSUID, send_id=USER_BSUID,
            )

        assert (
            db.get_whatsapp_binding(conn, "alice").opted_out_at
            == "2026-01-01 00:00:00"
        )

    def test_a_baileys_latch_drops_metas_learned_destination(self, conn):
        """`send_id` is Meta's, and `outbound._destination` prefers it.

        A row switched from Cloud keeps the opaque id Meta issued, which that
        function returns ahead of the bootstrap number — so a Baileys send
        would be handed a Meta identifier the socket cannot address. Clearing
        costs a switch back nothing: the Cloud resolver writes it again from
        the first message it authenticates.
        """
        self._row_with_history(conn, provider=CLOUD)
        conn.execute(
            "UPDATE whatsapp_user_bindings SET send_id = ? WHERE user_id = 'alice'",
            (USER_BSUID,),
        )

        assert db.latch_whatsapp_jid(conn, "alice", jid=USER_JID)

        assert db.get_whatsapp_binding(conn, "alice").send_id == ""

    def test_a_cloud_latch_on_a_cloud_row_keeps_everything(self, conn):
        """The equivalence half, and the reason the discard is provider-compared.

        Every row on every existing deployment reads as this adapter's, so the
        discard is a no-op there and the Cloud path is byte-for-byte what it
        was. An unconditional clear would turn this red.
        """
        self._row_with_history(conn, provider=CLOUD)

        assert db.latch_whatsapp_bsuid(
            conn, "alice", bsuid=USER_BSUID, send_id=USER_BSUID,
        )

        binding = db.get_whatsapp_binding(conn, "alice")
        assert binding.last_user_message_at == "2026-01-01 00:00:00"
        assert binding.opted_out_at == "2026-01-01 00:00:00"

    @pytest.mark.parametrize("stored, kwargs, arm", [
        (USER_BSUID, {"jid": USER_JID}, BAILEYS),
        (USER_JID, {"bsuid": USER_BSUID, "wa_id": "15551234567"}, CLOUD),
    ])
    def test_a_bootstrap_across_adapters_latches_and_alerts(
        self, conn, stored, kwargs, arm
    ):
        """The alarm on a trade this module makes deliberately.

        The bootstrap arm refuses a number whose row already carries a
        *different* identity — but only of the adapter being resolved. A row
        enrolled under the other adapter has an empty column here, so after a
        switch it is bootstrappable from the number alone, which under a single
        adapter is precisely the recycled-line case the table refuses.

        Refusing is not available: on a genuine migration every row is in that
        state, so a refusal locks out the deployment, and nothing on the row
        tells that apart from a recycled number. So the latch happens and the
        operator is told.
        """
        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number=USER_NUMBER)
        if arm == BAILEYS:
            db.set_whatsapp_binding(
                conn, "alice", bootstrap_phone_number=USER_NUMBER, bsuid=stored,
            )
        else:
            assert db.latch_whatsapp_jid(conn, "alice", jid=stored)

        resolution = identity_rules.resolve_inbound_identity(
            conn, _event(**kwargs).from_user, provider=arm,
        )

        assert resolution.user_id == "alice"
        assert resolution.pending_alert is not None
        row = conn.execute(
            "SELECT dedup_key, title FROM notifications WHERE user_id = 'alice'"
        ).fetchone()
        assert row["dedup_key"].startswith("whatsapp-cross-adapter:")

    def test_a_first_enrollment_raises_no_cross_adapter_alert(self, conn):
        """The control: an ordinary bootstrap onto an identity-free row.

        Without this, an alert fired on every user's first message and the
        alarm would mean nothing.
        """
        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number=USER_NUMBER)

        resolution = identity_rules.resolve_inbound_identity(
            conn, _event(jid=USER_JID).from_user, provider=BAILEYS,
        )

        assert resolution.user_id == "alice"
        assert resolution.pending_alert is None
        assert conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = 'alice'"
        ).fetchone()[0] == 0

    def test_the_cross_adapter_alert_is_pushed_not_just_filed(self, tmp_path):
        """It rides an accepted message, so the refusal path cannot carry it.

        `_handle_inbound` returns early with `pending_alerts` only when the
        resolution refused. A success-path alert not threaded through leaves
        the row in the inbox with nothing delivering it.
        """
        config = _config(tmp_path, provider=BAILEYS)
        with db.get_db(config.db_path) as conn:
            db.set_whatsapp_binding(
                conn, "alice",
                bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID,
            )

        with db.get_db(config.db_path) as conn:
            results = handle_whatsapp_batch(
                conn, config, [_event(jid=USER_JID)], provider=BAILEYS,
            )

        assert [r.disposition for r in results] == ["task"]
        assert results[0].pending_alerts != ()

    def test_a_legacy_null_provider_counts_as_cloud_for_the_discard(self, conn):
        """The same claim against the state an upgraded database is actually in."""
        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number=USER_NUMBER)
        conn.execute(
            "UPDATE whatsapp_user_bindings "
            "   SET provider = NULL, last_user_message_at = '2026-01-01 00:00:00' "
            " WHERE user_id = 'alice'"
        )

        assert db.latch_whatsapp_bsuid(
            conn, "alice", bsuid=USER_BSUID, send_id=USER_BSUID,
        )
        assert (
            db.get_whatsapp_binding(conn, "alice").last_user_message_at
            == "2026-01-01 00:00:00"
        )


# ---------------------------------------------------------------------------
# The operator's own writes
# ---------------------------------------------------------------------------


class TestTheOperatorWrites:
    def test_re_asserting_the_same_number_does_not_unlatch_the_jid(self, conn):
        """The Ansible converge, which re-runs `user ensure` on every deploy.

        `set_whatsapp_binding` rewrites the row unconditionally, so a carry
        the upsert forgot would silently unlatch every Baileys identity on the
        estate once a day — and the user's next message would re-enroll them,
        so nothing would look broken.
        """
        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number=USER_NUMBER)
        assert db.latch_whatsapp_jid(conn, "alice", jid=USER_JID)

        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number=USER_NUMBER)

        binding = db.get_whatsapp_binding(conn, "alice")
        assert binding.jid == USER_JID
        assert binding.provider == BAILEYS

    def test_the_change_detect_tuple_covers_both_new_columns(self, conn):
        """`user ensure` prints `noop` off this compare and Ansible keys on it.

        A re-assert must read as unchanged, and a reset must not.
        """
        from istota.cli import _whatsapp_binding_state

        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number=USER_NUMBER)
        db.latch_whatsapp_jid(conn, "alice", jid=USER_JID)
        latched = db.get_whatsapp_binding(conn, "alice")

        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number=USER_NUMBER)
        re_asserted = db.get_whatsapp_binding(conn, "alice")
        assert _whatsapp_binding_state(latched) == _whatsapp_binding_state(re_asserted)

        db.reset_whatsapp_identity(conn, "alice")
        after_reset = db.get_whatsapp_binding(conn, "alice")
        assert _whatsapp_binding_state(latched) != _whatsapp_binding_state(after_reset)

    def test_changing_the_number_discards_the_jid_too(self, conn):
        """Any identity change discards everything about the previous holder.

        There is no `jid=` parameter here — no operator types one — so the
        discard can only ever clear it, which is the safe direction: a stale
        Baileys identity on a row just repointed at somebody else would keep
        resolving the previous holder's messages to this user.
        """
        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number=USER_NUMBER)
        db.latch_whatsapp_jid(conn, "alice", jid=USER_JID)

        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number="+15557654321")

        binding = db.get_whatsapp_binding(conn, "alice")
        assert binding.jid == ""
        assert binding.provider == CLOUD

    def test_the_identity_reset_clears_both_adapters(self, conn):
        """Wider than the spec's "for that adapter", deliberately.

        The verb takes no adapter argument and is the operator saying the
        learned identity on this row is wrong. A half-reset leaves the other
        adapter's identity to resolve the previous holder the moment the
        deployment switches back.
        """
        db.set_whatsapp_binding(
            conn, "alice", bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID,
        )
        conn.execute(
            "UPDATE whatsapp_user_bindings SET jid = ?, provider = ? "
            "WHERE user_id = 'alice'", (USER_JID, BAILEYS),
        )

        db.reset_whatsapp_identity(conn, "alice")

        binding = db.get_whatsapp_binding(conn, "alice")
        assert binding.bsuid == ""
        assert binding.jid == ""
        assert binding.provider == CLOUD
        assert binding.bootstrap_phone_number == USER_NUMBER

    def test_the_jid_is_masked_on_the_general_operator_surface(self, tmp_path, capsys):
        """A JID embeds the number the line above it deliberately masks.

        Driven through the real `user ensure` output rather than by calling
        the masker. The first version of this called
        `mask_whatsapp_identifier` directly and asserted a sha256 digest did
        not contain its own input — true by construction, and green against a
        `cli.py` that printed the JID raw.
        """
        from istota.cli import cmd_user_ensure

        from .test_cli_user_ensure import _FakeArgs

        path = tmp_path / "istota.db"
        db.init_db(path)
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{path}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
        )
        with db.get_db(path) as conn:
            db.set_whatsapp_binding(
                conn, "alice", bootstrap_phone_number=USER_NUMBER,
            )
            db.latch_whatsapp_jid(conn, "alice", jid=USER_JID)

        capsys.readouterr()
        cmd_user_ensure(_FakeArgs(config=str(cfg), name="alice"))

        out = capsys.readouterr().out
        assert "whatsapp_jid:" in out
        assert "15551234567" not in out
        assert "s.whatsapp.net" not in out


# ---------------------------------------------------------------------------
# The batch handler
# ---------------------------------------------------------------------------


class TestTheProvenanceGate:
    """An inactive adapter's inbound message creates no task."""

    def test_a_baileys_deployment_refuses_a_cloud_inbound_message(self, tmp_path):
        config = _config(tmp_path, provider=BAILEYS)
        with db.get_db(config.db_path) as conn:
            db.set_whatsapp_binding(
                conn, "alice",
                bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID,
            )

        with db.get_db(config.db_path) as conn:
            results = handle_whatsapp_batch(
                conn, config,
                [_event(bsuid=USER_BSUID, wa_id="15551234567")],
                provider=CLOUD,
            )

        assert [r.disposition for r in results] == ["inactive_provider"]
        with db.get_db(config.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
            # No dedup row either: the gate is ahead of the claim, so a
            # deployment that switches back re-reads the message as new.
            assert conn.execute(
                "SELECT COUNT(*) FROM processed_whatsapp"
            ).fetchone()[0] == 0

    def test_the_active_adapter_is_unaffected(self, tmp_path):
        config = _config(tmp_path, provider=CLOUD)
        with db.get_db(config.db_path) as conn:
            db.set_whatsapp_binding(
                conn, "alice",
                bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID,
            )

        with db.get_db(config.db_path) as conn:
            results = handle_whatsapp_batch(
                conn, config,
                [_event(bsuid=USER_BSUID, wa_id="15551234567")],
                provider=CLOUD,
            )

        assert [r.disposition for r in results] == ["task"]

    def test_a_baileys_message_creates_a_task_on_a_baileys_deployment(self, tmp_path):
        """The arm Stage 5's receiver will drive, end to end through the batch."""
        config = _config(tmp_path, provider=BAILEYS)
        with db.get_db(config.db_path) as conn:
            db.set_whatsapp_binding(
                conn, "alice", bootstrap_phone_number=USER_NUMBER,
            )

        with db.get_db(config.db_path) as conn:
            results = handle_whatsapp_batch(
                conn, config, [_event(jid=USER_JID)], provider=BAILEYS,
            )

        assert [r.disposition for r in results] == ["task"]
        with db.get_db(config.db_path) as conn:
            binding = db.get_whatsapp_binding(conn, "alice")
            task = conn.execute("SELECT user_id, source_type FROM tasks").fetchone()
        assert binding.jid == USER_JID and binding.provider == BAILEYS
        assert (task["user_id"], task["source_type"]) == ("alice", "whatsapp")

    def test_a_baileys_message_writes_neither_send_id_nor_metas_window(self, tmp_path):
        """Two columns that belong to the Cloud API and not to WhatsApp.

        `send_id` is Meta's opaque destination, which Baileys does not issue —
        a JID written there would sit under a second uniqueness rule for no
        reader. `last_user_message_at` is the sole input to Meta's 24-hour
        service window, and a Baileys message opens no conversation Meta knows
        about, so stamping it would authorize a free-form send on a later
        switch back to Cloud.
        """
        config = _config(tmp_path, provider=BAILEYS)
        with db.get_db(config.db_path) as conn:
            db.set_whatsapp_binding(
                conn, "alice", bootstrap_phone_number=USER_NUMBER,
            )

        with db.get_db(config.db_path) as conn:
            handle_whatsapp_batch(
                conn, config, [_event(jid=USER_JID)], provider=BAILEYS,
            )

        with db.get_db(config.db_path) as conn:
            binding = db.get_whatsapp_binding(conn, "alice")
        assert binding.send_id == ""
        assert binding.last_user_message_at is None
        # `last_seen_at` is a fact about the row rather than about Meta's
        # window, and is still recorded.
        assert binding.last_seen_at is not None
