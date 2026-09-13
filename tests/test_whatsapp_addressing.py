"""Where a WhatsApp message goes, and how much of it fits.

Two questions the send path used to answer with Meta's numbers written as
though they were WhatsApp's, and that Stage 6 moved onto the capability
record — the only two places the common path still knew which provider it was
talking to.

The destination is the one with teeth. `outbound._destination` returned
``send_id or bootstrap_phone_number``, and a Baileys binding is latched by JID
and carries no `send_id` by design (`identity._resolve_baileys` returns
``send_id=None``), so a Baileys send resolved to a bare E.164 number its socket
cannot address. Both reviewers of Stage 4 found it independently and the
deferral was safe only while no Baileys adapter existed.

The body budget is the quieter one and its cost is on the record:
`.claude/rules/whatsapp.md` says a confirmation prompt rendered at the text
limit was refused by Meta past a quarter of the budget and the task parked
until it expired. Sizing a Baileys prompt to Meta's interactive cap would not
break anything — it would silently throw away three quarters of every question.
"""

from __future__ import annotations

import pytest

from istota import db
from istota.config import Config, UserConfig
from istota.transport.whatsapp import identity as identity_rules
from istota.transport.whatsapp import outbound
from istota.transport.whatsapp.providers._types import WhatsAppProviderCaps

from .support.whatsapp_config import build_whatsapp_config

USER = "alice"
USER_NUMBER = "+15551234567"
USER_JID = "15551234567@s.whatsapp.net"
USER_BSUID = "US.9876543210"

CLOUD_CAPS = WhatsAppProviderCaps(
    metered=True, has_service_window=True, supports_templates=True,
    delivery_receipts=True, address_field="send_id",
    service_body_limit=4096, interactive_body_limit=1024,
)
BAILEYS_CAPS = WhatsAppProviderCaps(
    metered=False, has_service_window=False, supports_templates=False,
    delivery_receipts=True, address_field="jid",
    service_body_limit=4096, interactive_body_limit=4096,
)


def _config(tmp_path, *, provider="baileys", **overrides) -> Config:
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
        users={USER: UserConfig()},
        whatsapp=build_whatsapp_config(**fields),
    )


def _binding(conn, *, number=USER_NUMBER, bsuid="", jid=""):
    db.set_whatsapp_binding(conn, USER, bootstrap_phone_number=number, bsuid=bsuid)
    if jid:
        db.latch_whatsapp_jid(conn, USER, jid=jid)
    return db.get_whatsapp_binding(conn, USER)


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    with db.get_db(path) as connection:
        yield connection


class TestTheAdapterNativeAddress:
    """`identity.address_for_binding`, one spelling per adapter."""

    def test_a_cloud_row_addresses_by_the_send_id_meta_handed_back(self, conn):
        binding = _binding(conn, bsuid=USER_BSUID)
        db.touch_whatsapp_binding(
            conn, USER, send_id=USER_BSUID,
            last_seen_at=db.sql_datetime_now(),
        )
        binding = db.get_whatsapp_binding(conn, USER)

        assert identity_rules.address_for_binding(binding, "send_id") == USER_BSUID

    def test_a_baileys_row_addresses_by_its_jid(self, conn):
        binding = _binding(conn, jid=USER_JID)

        assert identity_rules.address_for_binding(binding, "jid") == USER_JID

    def test_a_baileys_row_never_falls_back_to_metas_send_id(self, conn):
        """The case the deferral was about, from the other side.

        A row switched from Cloud has both columns for as long as it takes the
        first Baileys message to arrive — Stage 4's latch clears `send_id`, but
        a row that was *never* latched (an operator enrolling by number after
        the switch) still carries whatever Meta taught it. Reading `send_id`
        there hands the socket an opaque Meta identifier.
        """
        _binding(conn, bsuid=USER_BSUID)
        db.touch_whatsapp_binding(
            conn, USER, send_id=USER_BSUID,
            last_seen_at=db.sql_datetime_now(),
        )
        binding = db.get_whatsapp_binding(conn, USER)

        assert binding.send_id == USER_BSUID
        assert not binding.jid
        assert identity_rules.address_for_binding(binding, "jid") != USER_BSUID

    def test_a_number_only_row_is_rendered_into_each_adapters_spelling(self, conn):
        """The bootstrap fallback, which both adapters have and which is the
        one place the two spellings genuinely differ: Meta takes the E.164
        number as a destination, a socket takes a JID built from it."""
        binding = _binding(conn)

        assert identity_rules.address_for_binding(binding, "send_id") == USER_NUMBER
        assert identity_rules.address_for_binding(binding, "jid") == USER_JID

    def test_the_rendered_jid_is_the_one_a_later_inbound_message_matches(self, conn):
        """The drift this function is filed beside `normalize_jid` to prevent.

        A destination built by one rule and an identity parsed by another means
        a user enrolled by number can be messaged and can never be recognised
        when they answer.
        """
        binding = _binding(conn)
        addressed = identity_rules.address_for_binding(binding, "jid")

        assert identity_rules.normalize_jid(addressed) == addressed
        assert identity_rules.jid_number(addressed) == USER_NUMBER

    @pytest.mark.parametrize(
        "number", ["", "   ", "5551234567", "+1 555 123 4567", "not-a-number"],
    )
    def test_a_number_that_is_not_e164_yields_no_jid(self, number):
        """Refused rather than cleaned up. The value becomes a destination, and
        a number this guessed at addresses somebody."""
        assert identity_rules.jid_from_number(number) == ""

    def test_an_unknown_address_field_yields_nothing_rather_than_guessing(
        self, conn,
    ):
        binding = _binding(conn, jid=USER_JID)

        assert identity_rules.address_for_binding(binding, "lid") == ""

    def test_no_binding_addresses_nobody(self):
        assert identity_rules.address_for_binding(None, "jid") == ""
        assert identity_rules.any_identity(None) == ""


class TestTheDestinationTheSendPathResolves:
    def test_each_adapter_reads_its_own_column(self, conn):
        """The whole of the fix, in one assertion: one row, two adapters, two
        destinations."""
        _binding(conn, bsuid=USER_BSUID, jid=USER_JID)
        db.touch_whatsapp_binding(
            conn, USER, send_id=USER_BSUID,
            last_seen_at=db.sql_datetime_now(),
        )
        binding = db.get_whatsapp_binding(conn, USER)

        assert outbound._destination(binding, CLOUD_CAPS) == USER_BSUID
        assert outbound._destination(binding, BAILEYS_CAPS) == USER_JID

    def test_with_no_adapter_it_answers_the_existence_question(self, conn):
        """`WhatsAppTransport.resolve_target` asks whether the user is enrolled
        at all, on a deployment whose adapter could not be built. Answering
        `""` there would drop the WhatsApp leg from the plan for a reason about
        the adapter rather than about the user."""
        _binding(conn, jid=USER_JID)
        binding = db.get_whatsapp_binding(conn, USER)

        assert outbound._destination(binding, None) == USER_JID

    def test_a_bsuid_only_row_has_no_baileys_address(self, conn):
        """A Cloud user identified by BSUID alone — enrolled with no number,
        which `set_whatsapp_binding` permits because the BSUID is an identity —
        is unreachable on Baileys until they write in and latch a JID.

        The gate's `unconfigured` arm, and the right answer: the alternative is
        inventing a destination out of a Meta identifier. There is no case
        where *both* columns are empty; `set_whatsapp_binding` refuses a row
        that names a user with no way to reach or recognise them.
        """
        db.set_whatsapp_binding(
            conn, USER, bootstrap_phone_number="", bsuid=USER_BSUID,
        )
        db.touch_whatsapp_binding(
            conn, USER, send_id=USER_BSUID, last_seen_at=db.sql_datetime_now(),
        )
        binding = db.get_whatsapp_binding(conn, USER)

        assert outbound._destination(binding, CLOUD_CAPS) == USER_BSUID
        assert outbound._destination(binding, BAILEYS_CAPS) == ""


class TestTheConfirmationBodyBudget:
    """What `scheduler._whatsapp_confirmation_body` is now told, and by whom."""

    def test_cloud_keeps_metas_interactive_cap(self, tmp_path):
        config = _config(tmp_path, provider="whatsapp_cloud")

        assert outbound.confirmation_body_budget(config) == 1024

    def test_cloud_with_a_template_takes_the_smaller_parameter_cap(self, tmp_path):
        config = _config(
            tmp_path, provider="whatsapp_cloud", billing_policy="allow_paid",
        )
        config.whatsapp.cloud.proactive_template.enabled = True
        config.whatsapp.cloud.proactive_template.name = "istota_notice"
        config.whatsapp.cloud.proactive_template.language = "en"

        assert outbound.confirmation_body_budget(config) == (
            outbound.TEMPLATE_PARAMETER_LIMIT
        )

    def test_it_survives_an_adapter_that_cannot_be_built(self, tmp_path,
                                                         monkeypatch):
        """The scheduler calls this while parking a task; a raise here would
        leave the task parked with no question composed."""
        monkeypatch.setattr(outbound, "active_adapter", lambda config: None)
        config = _config(tmp_path, provider="baileys")

        assert outbound.confirmation_body_budget(config) == 1024


class TestTheRenderedServiceBody:
    @pytest.mark.parametrize(
        "caps,buttons,expected",
        [
            (CLOUD_CAPS, True, 1024),
            (CLOUD_CAPS, False, 4096),
            (BAILEYS_CAPS, True, 4096),
            (BAILEYS_CAPS, False, 4096),
            (None, True, 1024),
            (None, False, 4096),
        ],
    )
    def test_the_budget_follows_the_adapter_and_the_buttons(
        self, caps, buttons, expected,
    ):
        assert outbound._body_budget(caps, interactive=buttons) == expected
