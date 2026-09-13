"""The WhatsApp provider seam: the adapter record, the caps and the registry.

`tests/test_sms_foundation.py::TestSmsProviderContract` is what this mirrors,
and the places where it deliberately does not are where the two surfaces
differ: an adapter that receives over no HTTP callback at all, and one whose
credential lives nowhere in `config.toml`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import importlib
import itertools
import json
from datetime import datetime, timedelta, timezone
from typing import get_args

import pytest

from istota import db
from istota.config import (
    Config,
    WHATSAPP_PROVIDER_NAMES,
    WHATSAPP_WEBHOOK_PROVIDERS,
    UserConfig,
    WhatsAppBaileysConfig,
    WhatsAppCloudConfig,
    WhatsAppConfig,
    WhatsAppTemplateConfig,
    load_config,
    whatsapp_missing_credentials,
    whatsapp_provider_has_values,
    whatsapp_provider_missing_fields,
    whatsapp_structural_config_errors,
    whatsapp_webhooks_enabled,
)
from .support.whatsapp_config import build_whatsapp_config

from istota.transport.whatsapp import outbound
from istota.transport.whatsapp._types import (
    WhatsAppSendFailure,
    WhatsAppSendRequest,
    WhatsAppSendResult,
    WhatsAppWebhookRequest,
    WhatsAppWebhookResult,
)
from istota.transport.whatsapp.providers import whatsapp_cloud
from istota.transport.whatsapp.providers._types import (
    WhatsAppProviderAdapter,
    WhatsAppProviderCaps,
    WhatsAppProviderName,
)
from istota.transport.whatsapp.providers.registry import (
    WhatsAppProviderRegistry,
    make_provider_registry,
)
from istota.transport.whatsapp.webhook import WhatsAppWebhookError
from tests.support.drift import source_of

WABA_ID = "123456789012345"
PHONE_NUMBER_ID = "223456789012345"
APP_SECRET = "wa-app-secret"
USER_NUMBER = "+15551234567"
USER_BSUID = "US.9876543210"

CLOUD_CAPS = WhatsAppProviderCaps(
    metered=True,
    has_service_window=True,
    supports_templates=True,
    delivery_receipts=True,
    address_field="send_id",
    service_body_limit=4096,
    interactive_body_limit=1024,
)
BAILEYS_CAPS = WhatsAppProviderCaps(
    metered=False,
    has_service_window=False,
    supports_templates=False,
    delivery_receipts=True,
    address_field="jid",
    service_body_limit=4096,
    interactive_body_limit=4096,
)


async def _stub_send(request):
    """An adapter's `send` is awaitable, and a stub that is not costs a row.

    `_send_claimed` awaits it, so a synchronous stub raises `TypeError` inside
    the claim-to-settle region and the backstop settles `unknown` — the one
    state an operator can never resolve, spent on a message nobody sent. The
    registry refuses one at build for that reason; these stubs have to be the
    shape it accepts or the refusal would fire on the test's own fixture.
    """
    return WhatsAppSendResult(message_id="wamid.1")


def _adapter(name, caps, *, webhook=True) -> WhatsAppProviderAdapter:
    return WhatsAppProviderAdapter(
        name=name,
        caps=caps,
        parse_webhook=(lambda request: request) if webhook else None,
        send=_stub_send,
        verify_signature=(lambda request: True) if webhook else None,
    )


def _cloud_config(*, enabled: bool = True, provider: str = "whatsapp_cloud") -> Config:
    cfg = Config()
    cfg.whatsapp.enabled = enabled
    cfg.whatsapp.provider = provider
    cfg.whatsapp.cloud.access_token = "wa-access-token"
    cfg.whatsapp.cloud.app_secret = "wa-app-secret"
    cfg.whatsapp.cloud.verify_token = "wa-verify-token"
    return cfg


class TestTheAdapterRecord:
    def test_the_adapter_and_its_caps_are_frozen(self):
        adapter = _adapter("whatsapp_cloud", CLOUD_CAPS)

        with pytest.raises(dataclasses.FrozenInstanceError):
            adapter.name = "baileys"
        with pytest.raises(dataclasses.FrozenInstanceError):
            adapter.caps.metered = False

    def test_caps_take_no_defaults(self):
        """A third adapter answers all four questions rather than inheriting
        whichever answers happened to suit the first two."""
        with pytest.raises(TypeError):
            WhatsAppProviderCaps(metered=False)  # type: ignore[call-arg]

    def test_an_adapter_may_carry_no_webhook_at_all(self):
        """The SMS seam has no such adapter, so this is the one shape of the
        record a copied test would not have covered."""
        adapter = _adapter("baileys", BAILEYS_CAPS, webhook=False)

        assert adapter.parse_webhook is None
        assert adapter.verify_signature is None

    def test_the_webhook_types_carry_a_batch_rather_than_one_event(self):
        request = WhatsAppWebhookRequest(raw_body=b"{}", headers={"Content-Type": "x"})
        result = WhatsAppWebhookResult(
            events=(),
            response_status=200,
            response_content_type=None,
            response_body=b"",
        )

        assert request.raw_body == b"{}"
        assert result.events == ()
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.response_status = 500


class TestNothingSelectsAProviderYet:
    """What a config that names no provider gets, and it is now two answers.

    The class was written when the two were one — every deployment ran Cloud,
    so the dataclass default and the legacy file resolved alike. Flipping the
    default to `baileys` split them, and the split is the whole compatibility
    claim: a config written *today* with no provider key gets the new adapter,
    while one carrying the pre-adapter flat block goes on running the adapter
    it has always run.
    """

    def test_the_default_is_the_adapter_a_fresh_install_should_get(self):
        """Deliberately the opposite of what this asserted at Stage 1.

        A fresh install writes no Meta account object and cannot be given one
        by a default, so the only value that can work unattended is the one
        whose credential is a QR scan.
        """
        assert Config().whatsapp.provider == "baileys"

    def test_a_config_naming_no_provider_loads_as_cloud(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(
            "[site]\n"
            'hostname = "assistant.example.com"\n'
            "\n"
            "[whatsapp]\n"
            "enabled = true\n"
            'waba_id = "123456789012345"\n'
            'phone_number_id = "223456789012345"\n'
            'business_phone_number = "+15551234567"\n'
            'access_token = "wa-access-token"\n'
            'app_secret = "wa-app-secret"\n'
            'verify_token = "wa-verify-token"\n'
        )

        cfg = load_config(path)

        assert cfg.whatsapp.provider == "whatsapp_cloud"
        assert whatsapp_structural_config_errors(cfg) == []
        assert whatsapp_missing_credentials(cfg) == ()


class TestTheProviderFieldValidators:
    def test_the_cloud_block_reports_its_own_blanks(self):
        cfg = Config()
        cfg.whatsapp.cloud.access_token = "wa-access-token"

        assert whatsapp_provider_missing_fields(cfg, "whatsapp_cloud") == (
            "app_secret", "verify_token",
        )
        assert whatsapp_provider_has_values(cfg, "whatsapp_cloud") is True

    def test_whitespace_is_blank(self):
        cfg = _cloud_config()
        cfg.whatsapp.cloud.app_secret = "   "

        assert whatsapp_provider_missing_fields(cfg, "whatsapp_cloud") == ("app_secret",)

    def test_the_credential_report_is_the_cloud_arm_of_the_same_rule(self):
        """Delegated rather than restated, so the registry's gate and doctor's
        report cannot drift on what counts as blank."""
        cfg = Config()
        cfg.whatsapp.cloud.app_secret = " "

        assert whatsapp_missing_credentials(cfg) == whatsapp_provider_missing_fields(
            cfg, "whatsapp_cloud"
        )

    def test_a_provider_with_no_declared_fields_is_missing_nothing_and_holds_nothing(self):
        cfg = Config()

        assert whatsapp_provider_missing_fields(cfg, "baileys") == ()
        assert whatsapp_provider_has_values(cfg, "baileys") is False

    def test_an_unknown_provider_answers_emptily_rather_than_raising(self):
        cfg = _cloud_config()

        assert whatsapp_provider_missing_fields(cfg, "signal") == ()
        assert whatsapp_provider_has_values(cfg, "signal") is False

    def test_an_unknown_provider_name_fails_the_config_load(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('[whatsapp]\nprovider = "sinal"\n')

        with pytest.raises(ValueError, match="provider must be one of"):
            load_config(path)

    def test_the_provider_name_is_checked_while_the_transport_is_disabled(self):
        """`make_provider_registry` raises on an unknown name and a process may
        build one at startup, so a misspelling in a disabled block must not load
        cleanly."""
        cfg = Config()
        cfg.whatsapp.enabled = False
        cfg.whatsapp.provider = "sinal"

        assert any(
            "provider must be one of" in error
            for error in whatsapp_structural_config_errors(cfg)
        )

    def test_a_missing_block_reports_every_field_rather_than_none(self):
        """Fail closed. Unreachable while both callers dereference
        `config.whatsapp.enabled` first, and the wrong direction to be
        careless in: `()` here reads as fully configured."""
        cfg = Config()
        cfg.whatsapp = None  # type: ignore[assignment]

        assert whatsapp_provider_missing_fields(cfg, "whatsapp_cloud") == (
            "access_token", "app_secret", "verify_token",
        )
        assert whatsapp_provider_has_values(cfg, "whatsapp_cloud") is False

    def test_every_named_provider_names_its_own_block(self):
        """The block map is the other half of the fields map, and a name
        missing from it makes every field read resolve to `None` — which
        `whatsapp_provider_missing_fields` fails closed on and
        `whatsapp_provider_has_values` reads as unconfigured, so the provider
        would be reported as holding nothing whatever the operator wrote."""
        from istota.config import _WHATSAPP_PROVIDER_BLOCKS

        assert set(_WHATSAPP_PROVIDER_BLOCKS) == set(WHATSAPP_PROVIDER_NAMES)
        for provider, attribute in _WHATSAPP_PROVIDER_BLOCKS.items():
            block = getattr(Config().whatsapp, attribute)
            assert dataclasses.is_dataclass(block), provider

    def test_no_field_name_appears_at_two_levels_of_the_block_tree(self):
        """What `tests/support/whatsapp_config.py` rests on, pinned here.

        That helper routes a flat keyword to a block by looking the name up in
        the nested dataclasses first, so a name appearing at two levels would
        be silently claimed by the wrong one. `enabled` is the live hazard: add
        it to `WhatsAppCloudConfig` and `build_whatsapp_config(enabled=True)`
        starts building a *disabled* transport with an enabled Cloud block, and
        four converted suites go on passing while asserting about a surface
        that is switched off.
        """
        from istota.config import _WHATSAPP_PROVIDER_BLOCKS

        levels = {
            "whatsapp": {f.name for f in dataclasses.fields(WhatsAppConfig)},
            "cloud": {f.name for f in dataclasses.fields(WhatsAppCloudConfig)},
            "baileys": {f.name for f in dataclasses.fields(WhatsAppBaileysConfig)},
        }
        # The block fields themselves are the names of the nested blocks, and
        # those are how a caller addresses a whole block rather than a field.
        levels["whatsapp"] -= set(_WHATSAPP_PROVIDER_BLOCKS.values())

        for left, right in itertools.combinations(sorted(levels), 2):
            assert not levels[left] & levels[right], (left, right)

    def test_the_helper_refuses_a_field_and_its_whole_block_at_once(self):
        with pytest.raises(TypeError, match="pass one or the other"):
            build_whatsapp_config(
                access_token="wa-access-token",
                cloud=WhatsAppCloudConfig(access_token="other"),
            )

    def test_every_named_provider_has_a_structural_validator(self):
        """Otherwise the whole of that provider's config goes unvalidated: the
        dispatch answers with a single error naming the gap rather than
        returning `[]`, which would read as a config with nothing wrong."""
        from istota.config import _WHATSAPP_PROVIDER_VALIDATORS

        assert set(_WHATSAPP_PROVIDER_VALIDATORS) == set(WHATSAPP_PROVIDER_NAMES)

    def test_a_provider_with_no_validator_reports_rather_than_passing(self, monkeypatch):
        from istota import config as config_module

        monkeypatch.delitem(config_module._WHATSAPP_PROVIDER_VALIDATORS, "baileys")
        cfg = Config()
        cfg.whatsapp.provider = "baileys"

        assert whatsapp_structural_config_errors(cfg) == [
            "provider 'baileys' has no structural validation"
        ]

    def test_every_named_provider_declares_its_fields(self):
        """A name in the tuple with no entry in the map is indistinguishable
        from an unknown one — `()` missing and nothing populated — so it would
        be built unchecked when selected and never built when it is not, with
        nothing anywhere saying so.
        """
        from istota.config import _WHATSAPP_PROVIDER_FIELDS

        assert set(_WHATSAPP_PROVIDER_FIELDS) == set(WHATSAPP_PROVIDER_NAMES)

    def test_the_adapter_literal_and_the_config_tuple_are_the_same_set(self):
        """Two spellings of the provider list, and a typechecker run over this
        diff would not notice them diverging: the registry annotates its return
        with the `Literal` while iterating the tuple, so a third name added to
        one alone makes `names()` return values outside its own declared type.
        """
        assert set(get_args(WhatsAppProviderName)) == set(WHATSAPP_PROVIDER_NAMES)


class TestTheRegistry:
    def test_it_distinguishes_active_and_callback_only_adapters(self):
        cloud = _adapter("whatsapp_cloud", CLOUD_CAPS)
        baileys = _adapter("baileys", BAILEYS_CAPS, webhook=False)
        registry = WhatsAppProviderRegistry(
            active_name="baileys",
            adapters={"whatsapp_cloud": cloud, "baileys": baileys},
        )

        assert registry.active() is baileys
        assert registry.get("whatsapp_cloud") is cloud
        assert registry.callback_only_names() == ("whatsapp_cloud",)
        assert registry.names() == ("whatsapp_cloud", "baileys")

    def test_an_unknown_name_gets_no_adapter_rather_than_a_key_error(self):
        registry = WhatsAppProviderRegistry(active_name="baileys", adapters={})

        assert registry.get("signal") is None
        assert registry.active() is None

    def test_a_disabled_transport_has_no_active_adapter_and_only_callbacks(self):
        cloud = _adapter("whatsapp_cloud", CLOUD_CAPS)
        registry = WhatsAppProviderRegistry(
            active_name="whatsapp_cloud",
            adapters={"whatsapp_cloud": cloud},
            active_enabled=False,
        )

        assert registry.active() is None
        assert registry.callback_only_names() == ("whatsapp_cloud",)

    def test_it_builds_the_selected_adapter(self):
        cfg = _cloud_config()
        built = _adapter("whatsapp_cloud", CLOUD_CAPS)

        registry = make_provider_registry(
            cfg, builders={"whatsapp_cloud": lambda config: built},
        )

        assert registry.active() is built
        assert registry.names() == ("whatsapp_cloud",)

    def test_it_refuses_an_unsupported_provider_name(self):
        cfg = _cloud_config(provider="signal")

        with pytest.raises(ValueError, match="unsupported WhatsApp provider"):
            make_provider_registry(cfg, builders={})

    def test_it_refuses_an_adapter_registered_under_another_name(self):
        cfg = _cloud_config()
        wrong = _adapter("baileys", BAILEYS_CAPS, webhook=False)

        with pytest.raises(ValueError, match="expected 'whatsapp_cloud'"):
            make_provider_registry(cfg, builders={"whatsapp_cloud": lambda c: wrong})

    def test_an_incomplete_cloud_block_yields_no_active_adapter(self):
        """The ISSUE-058 shape: a missing secret is refused at use and recorded
        `unconfigured`, never raised at load."""
        cfg = _cloud_config()
        cfg.whatsapp.cloud.app_secret = ""
        built = _adapter("whatsapp_cloud", CLOUD_CAPS)

        registry = make_provider_registry(
            cfg, builders={"whatsapp_cloud": lambda config: built},
        )

        assert registry.active() is None
        assert registry.names() == ()

    def test_a_selected_baileys_is_built_despite_holding_no_config_values(self):
        """The gate the SMS loop cannot express: a populated-block test reads
        False for this adapter forever, so gating the active role on it leaves
        the deployment with no adapter at all."""
        cfg = Config()
        cfg.whatsapp.enabled = True
        cfg.whatsapp.provider = "baileys"
        built = _adapter("baileys", BAILEYS_CAPS, webhook=False)

        registry = make_provider_registry(
            cfg, builders={"baileys": lambda config: built},
        )

        assert registry.active() is built
        assert registry.names() == ("baileys",)

    def test_a_baileys_deployment_keeps_a_complete_cloud_block_for_late_callbacks(self):
        cfg = _cloud_config(provider="baileys")
        cloud = _adapter("whatsapp_cloud", CLOUD_CAPS)
        baileys = _adapter("baileys", BAILEYS_CAPS, webhook=False)

        registry = make_provider_registry(
            cfg,
            builders={
                "whatsapp_cloud": lambda config: cloud,
                "baileys": lambda config: baileys,
            },
        )

        assert registry.active() is baileys
        assert registry.callback_only_names() == ("whatsapp_cloud",)
        # The loop follows `WHATSAPP_PROVIDER_NAMES`, which is what makes the
        # tuple's order the thing `names()` reports. Asserted here rather than
        # on a hand-built registry, where the dict literal supplies the order
        # and the assertion is about the test's own input: reversing the tuple
        # leaves every such assertion green.
        assert registry.names() == ("baileys", "whatsapp_cloud")

    def test_an_unselected_provider_holding_nothing_is_not_built(self):
        """Not merely tidiness: the only reason to keep a non-active adapter is
        a late callback authenticating against credentials the deployment still
        holds, and this one has none to authenticate with."""
        cfg = _cloud_config()
        baileys_builds = []

        def _baileys(config):
            baileys_builds.append(config)
            return _adapter("baileys", BAILEYS_CAPS, webhook=False)

        registry = make_provider_registry(
            cfg,
            builders={
                "whatsapp_cloud": lambda config: _adapter("whatsapp_cloud", CLOUD_CAPS),
                "baileys": _baileys,
            },
        )

        assert baileys_builds == []
        assert registry.callback_only_names() == ()

    def test_a_disabled_transport_still_keeps_a_complete_block(self):
        cfg = _cloud_config(enabled=False)
        built = _adapter("whatsapp_cloud", CLOUD_CAPS)

        registry = make_provider_registry(
            cfg, builders={"whatsapp_cloud": lambda config: built},
        )

        assert registry.active() is None
        assert registry.get("whatsapp_cloud") is built
        assert registry.callback_only_names() == ("whatsapp_cloud",)

    def test_the_callback_only_list_is_not_the_route_gate(self):
        """The one place the SMS shape and this surface's own rule disagree,
        pinned so a later stage does not wire the mount to the wrong one.

        SMS keeps a switched-away provider's routes alive; WhatsApp's rule is
        that turning the block off turns the account off. So a disabled
        deployment holding complete Cloud credentials names an adapter here
        and still serves no webhook — `whatsapp_webhooks_enabled` reads
        `enabled` and the *active* provider, never this list.
        """
        cfg = _cloud_config(enabled=False)
        registry = make_provider_registry(
            cfg,
            builders={"whatsapp_cloud": lambda config: _adapter(
                "whatsapp_cloud", CLOUD_CAPS,
            )},
        )

        assert registry.callback_only_names() == ("whatsapp_cloud",)
        assert whatsapp_webhooks_enabled(cfg) is False


class TestTheSeamCarriesTheExistingVocabulary:
    def test_a_send_outcome_is_the_surfaces_own_record(self):
        """No new event vocabulary is invented for the seam: the adapter's
        `send` returns what `outbound.py` already consumes.

        Awaited, because that is what the field declares and what every real
        adapter is — the whole send path from `deliver_whatsapp` down is
        `async`. Stage 1 stubbed it synchronously, before there was an adapter
        to check the declaration against.
        """
        async def _send(request):
            return WhatsAppSendFailure(
                definite=False, error_code=None, safe_reason="delivery state unknown",
            )

        adapter = WhatsAppProviderAdapter(
            name="whatsapp_cloud",
            caps=CLOUD_CAPS,
            parse_webhook=None,
            send=_send,
            verify_signature=None,
        )

        outcome = asyncio.run(adapter.send(object()))

        assert isinstance(outcome, WhatsAppSendFailure)
        assert outcome.definite is False


# ---------------------------------------------------------------------------
# Stage 2: the Cloud adapter, and the caps the gate order reads
# ---------------------------------------------------------------------------


def _deployment(tmp_path, **overrides) -> Config:
    """A Cloud deployment with a database, for the paths that claim a row."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "istota.db"
    db.init_db(path)
    fields = dict(
        enabled=True,
        waba_id=WABA_ID,
        phone_number_id=PHONE_NUMBER_ID,
        business_phone_number="+15551230000",
        access_token="wa-access-token",
        app_secret=APP_SECRET,
        verify_token="wa-verify-token",
        business_timezone="UTC",
    )
    fields.update(overrides)
    cfg = Config(
        db_path=path,
        temp_dir=tmp_path / "tmp",
        whatsapp=build_whatsapp_config(**fields),
        users={"alice": UserConfig()},
    )
    cfg.site.hostname = "assistant.example.com"
    return cfg


def _bind(cfg, *, window: timedelta = timedelta()) -> None:
    with db.get_db(cfg.db_path) as conn:
        db.set_whatsapp_binding(
            conn, "alice", bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID,
        )
        conn.execute(
            "UPDATE whatsapp_user_bindings SET send_id = ?, "
            "last_user_message_at = ? WHERE user_id = ?",
            (
                USER_BSUID,
                (datetime.now(timezone.utc) - window).strftime("%Y-%m-%d %H:%M:%S"),
                "alice",
            ),
        )


def _signed(body: bytes, secret: str = APP_SECRET) -> dict[str, str]:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {
        "content-type": "application/json",
        "X-Hub-Signature-256": "sha256=" + digest,
    }


def _meta_payload() -> bytes:
    return json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{
            "id": WABA_ID,
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                    "contacts": [{"wa_id": "15551234567", "user_id": USER_BSUID}],
                    "messages": [{
                        "id": "wamid.inbound.1",
                        "from": USER_BSUID,
                        "type": "text",
                        "text": {"body": "hello"},
                        "timestamp": "1760000000",
                    }],
                },
            }],
        }],
    }).encode()


_SEND_IDS = itertools.count(1)


class _FakeClient:
    """The Cloud client, at the one boundary the adapter owns.

    The default id is minted per call and from a counter shared by every
    instance, because `sent_whatsapp.meta_message_id` is unique across the
    table: a fake handing back one id would settle every send after the first
    as `unknown`, and a case that sends twice would then be asserting on a
    collision rather than on the gate it is about. Found by the control that
    turned the caps reads off — one of the four went red on `unknown` rather
    than on the state under test.
    """

    def __init__(self, outcome=None):
        self.requests: list[WhatsAppSendRequest] = []
        self.closed = 0
        self._outcome = outcome

    async def send(self, request):
        self.requests.append(request)
        if self._outcome is not None:
            return self._outcome
        return WhatsAppSendResult(f"wamid.sent.{next(_SEND_IDS)}")

    async def aclose(self) -> None:
        self.closed += 1


class TestTheCloudAdapter:
    """The port's own subject: Meta's API reachable only through the record."""

    def test_it_declares_every_constraint_the_rules_file_records(self):
        """Every one is a Cloud API fact rather than a WhatsApp fact, which is
        the premise the whole seam rests on.

        Spelled out rather than compared against `whatsapp_cloud.CLOUD_CAPS`,
        which would be tautological: the literal is what goes red when a field
        is added to the record and this adapter's answer is guessed at rather
        than decided.
        """
        adapter = whatsapp_cloud.build_adapter(_cloud_config())

        assert adapter.name == "whatsapp_cloud"
        assert adapter.caps == WhatsAppProviderCaps(
            metered=True,
            has_service_window=True,
            supports_templates=True,
            delivery_receipts=True,
            address_field="send_id",
            service_body_limit=4096,
            interactive_body_limit=1024,
        )

    def test_it_satisfies_its_own_registry_contract(self):
        """The checks `_contract_fault` makes, driven against the real adapter
        rather than against a stub built to pass them."""
        adapter = whatsapp_cloud.build_adapter(_cloud_config())

        assert asyncio.iscoroutinefunction(adapter.send)
        assert (adapter.parse_webhook is None) == (adapter.verify_signature is None)

    def test_building_one_makes_no_client(self, monkeypatch):
        """`make_registry`'s no-I/O-on-construction rule, one surface down.

        The registry is built on every send and on the enrollment probe, so a
        session per construction would be a connection pool almost nothing
        uses — and `client.py` already argues that lifetime. Driven by making
        the construction fatal rather than by reading the source.
        """
        def _explode(_config):
            raise AssertionError("build_adapter must not construct a client")

        monkeypatch.setattr(
            "istota.transport.whatsapp.client.make_client", _explode,
        )

        adapter = whatsapp_cloud.build_adapter(_cloud_config())

        assert adapter.name == "whatsapp_cloud"

    def test_the_registry_builds_it_with_no_builders_supplied(self):
        """The `builders=None` arm, which is the only one production uses and
        which nothing could drive at stage 1 because no adapter module
        existed."""
        registry = make_provider_registry(_cloud_config())

        active = registry.active()
        assert active is not None
        assert active.name == "whatsapp_cloud"
        assert active.caps.metered is True

    def test_parse_webhook_turns_a_signed_request_into_the_batch(self):
        """The declared webhook contract, driven end to end rather than
        asserted as a non-None field."""
        cfg = _cloud_config()
        cfg.whatsapp.cloud.waba_id = WABA_ID
        cfg.whatsapp.cloud.phone_number_id = PHONE_NUMBER_ID
        adapter = whatsapp_cloud.build_adapter(cfg)
        body = _meta_payload()

        result = adapter.parse_webhook(
            WhatsAppWebhookRequest(raw_body=body, headers=_signed(body)),
        )

        assert isinstance(result, WhatsAppWebhookResult)
        assert len(result.events) == 1
        assert result.events[0].message_id == "wamid.inbound.1"
        assert result.events[0].text == "hello"
        # An acknowledged batch answers an empty 200; the route's own contract.
        assert (result.response_status, result.response_body) == (200, b"")

    def test_parse_webhook_authenticates_before_it_reads(self):
        """The chain is one call rather than a signature check the caller has
        to remember: a forged body is refused with nothing parsed."""
        cfg = _cloud_config()
        cfg.whatsapp.cloud.waba_id = WABA_ID
        cfg.whatsapp.cloud.phone_number_id = PHONE_NUMBER_ID
        adapter = whatsapp_cloud.build_adapter(cfg)
        body = _meta_payload()

        with pytest.raises(WhatsAppWebhookError) as caught:
            adapter.parse_webhook(WhatsAppWebhookRequest(
                raw_body=body, headers=_signed(body, "another-app-secret"),
            ))

        assert caught.value.status_code == 403

    def test_verify_signature_is_the_same_answer_the_parse_rests_on(self):
        adapter = whatsapp_cloud.build_adapter(_cloud_config())
        body = _meta_payload()

        assert adapter.verify_signature(
            WhatsAppWebhookRequest(raw_body=body, headers=_signed(body)),
        ) is True
        assert adapter.verify_signature(
            WhatsAppWebhookRequest(
                raw_body=body, headers=_signed(body, "another-app-secret"),
            ),
        ) is False

    def test_a_client_it_builds_itself_is_closed(self, monkeypatch):
        client = _FakeClient()
        monkeypatch.setattr(
            "istota.transport.whatsapp.client.make_client", lambda _config: client,
        )
        adapter = whatsapp_cloud.build_adapter(_cloud_config())

        asyncio.run(adapter.send(
            WhatsAppSendRequest(to="US.1", text="hi", kind="service"),
        ))

        assert client.closed == 1

    def test_a_client_that_cannot_be_built_is_a_definite_failure(self, monkeypatch):
        """The ledger outcome `_send_claimed`'s pre-send arm used to produce.

        A missing or renamed PyWa raises inside `WhatsAppClient.__init__`,
        before a session exists — so nothing reached the network and the row
        must read `failed`. `unknown` is the one state an operator can never
        resolve and is not spent on a case whose answer is known.
        """
        def _explode(_config):
            raise ImportError("no module named pywa_async")

        monkeypatch.setattr(
            "istota.transport.whatsapp.client.make_client", _explode,
        )
        adapter = whatsapp_cloud.build_adapter(_cloud_config())

        outcome = asyncio.run(adapter.send(
            WhatsAppSendRequest(to="US.1", text="hi", kind="service"),
        ))

        assert isinstance(outcome, WhatsAppSendFailure)
        assert outcome.definite is True
        assert outcome.error_code is None
        # Local text from a fixed table; no provider prose, no request URL.
        assert "pywa" not in outcome.safe_reason


class TestTheMountNameSetTracksTheAdapters:
    """`WHATSAPP_WEBHOOK_PROVIDERS` is a second spelling of
    `adapter.parse_webhook is not None`, and drift is silent both ways.

    A name missing from the set never gets a route; a name in it whose adapter
    declares no webhook mounts `webhook.py` — Meta's handler — for a non-Meta
    provider, because `webhook_receiver` calls that module directly and never
    the adapter. The set exists because the three readers outside this process
    cannot ask an adapter, not because the two answers may differ.
    """

    def test_every_importable_provider_agrees_with_the_name_set(self):
        checked = []
        for name in WHATSAPP_PROVIDER_NAMES:
            try:
                module = importlib.import_module(
                    f"istota.transport.whatsapp.providers.{name}",
                )
            except ModuleNotFoundError:
                # Not yet implemented. The guard covers what exists rather
                # than asserting a count, so the next adapter is covered by
                # arriving rather than by anybody remembering this file.
                continue
            adapter = module.build_adapter(_cloud_config(provider=name))
            checked.append(name)
            assert (name in WHATSAPP_WEBHOOK_PROVIDERS) == (
                adapter.parse_webhook is not None
            ), f"{name} disagrees with WHATSAPP_WEBHOOK_PROVIDERS"

        assert checked, "no provider module imported, so nothing was checked"


class TestTheWebhookFieldsAreDeclaredTogether:
    """`_types` calls a webhook with no signature scheme "a thing to refuse
    loudly rather than to express"; this is where it is refused."""

    @pytest.mark.parametrize("parse, verify", [
        (lambda request: None, None),
        (None, lambda request: True),
    ])
    def test_a_half_declared_webhook_is_refused_at_build(self, parse, verify):
        cfg = _cloud_config()
        half = WhatsAppProviderAdapter(
            name="whatsapp_cloud", caps=CLOUD_CAPS,
            parse_webhook=parse,
            send=_stub_send,
            verify_signature=verify,
        )

        with pytest.raises(ValueError, match="parse_webhook and verify_signature"):
            make_provider_registry(cfg, builders={"whatsapp_cloud": lambda c: half})

    def test_a_synchronous_send_is_refused_before_it_can_cost_a_row(self):
        """The check that earns its place: `_send_claimed` awaits `send`, so a
        synchronous one raises inside the claim-to-settle region and the
        backstop settles `unknown` for a message nobody sent."""
        cfg = _cloud_config()
        sync = WhatsAppProviderAdapter(
            name="whatsapp_cloud", caps=CLOUD_CAPS,
            parse_webhook=lambda request: None,
            send=lambda request: WhatsAppSendResult("wamid.1"),
            verify_signature=lambda request: True,
        )

        with pytest.raises(ValueError, match="awaitable send"):
            make_provider_registry(cfg, builders={"whatsapp_cloud": lambda c: sync})

    def test_a_broken_callback_only_adapter_does_not_take_the_surface_down(self):
        """Dropped and logged rather than raised.

        The only caller in `src/` catches everything and returns `None`, so a
        raise for an adapter nobody selected would record every send on the
        active provider `unconfigured` — a whole-surface outage caused by a
        provider the deployment does not use.
        """
        cfg = _cloud_config(provider="baileys")
        broken = WhatsAppProviderAdapter(
            name="whatsapp_cloud", caps=CLOUD_CAPS,
            parse_webhook=lambda request: None,
            send=_stub_send,
            verify_signature=None,
        )
        active = _adapter("baileys", BAILEYS_CAPS, webhook=False)

        registry = make_provider_registry(cfg, builders={
            "whatsapp_cloud": lambda c: broken,
            "baileys": lambda c: active,
        })

        assert registry.active() is active
        assert registry.get("whatsapp_cloud") is None

    def test_declaring_neither_is_the_shape_a_socket_provider_takes(self):
        cfg = _cloud_config(provider="baileys")
        cfg.whatsapp.cloud.access_token = ""
        cfg.whatsapp.cloud.app_secret = ""
        cfg.whatsapp.cloud.verify_token = ""
        built = _adapter("baileys", BAILEYS_CAPS, webhook=False)

        registry = make_provider_registry(cfg, builders={"baileys": lambda c: built})

        assert registry.active() is built


class TestCapsDriveTheGates:
    """Three of the six gates are the provider's rules, not WhatsApp's.

    Each case runs one fixture through both capability records and requires
    opposite answers, so the Cloud half is the negative control for the other:
    restoring a Baileys capability to Cloud's value brings the refusal back in
    the same test, which is what says the field is read rather than carried.
    """

    def _send(self, monkeypatch, cfg, caps, *, key):
        # The double goes in by replacing `send` on a real Cloud adapter,
        # which is exactly how `deliver_whatsapp` injects one — rather than
        # through a second seam on `build_adapter`, which would exercise a
        # path production never takes.
        client = _FakeClient()
        monkeypatch.setattr(
            outbound, "active_adapter",
            lambda _config: dataclasses.replace(
                whatsapp_cloud.build_adapter(cfg), caps=caps, send=client.send,
            ),
        )
        record = asyncio.run(outbound.deliver_whatsapp(
            cfg, logical_key=key, user_id="alice", text="the answer",
        ))
        return record, client

    def test_a_closed_window_refuses_a_metered_provider_and_not_a_socket_one(
        self, tmp_path, monkeypatch,
    ):
        cfg = _deployment(tmp_path)
        _bind(cfg, window=timedelta(hours=48))

        blocked, cloud_client = self._send(monkeypatch, cfg, CLOUD_CAPS, key="cloud")
        sent, socket_client = self._send(monkeypatch, cfg, BAILEYS_CAPS, key="socket")

        assert blocked.status == "window_closed"
        assert cloud_client.requests == []
        assert sent.status == "accepted"
        assert len(socket_client.requests) == 1

    def test_an_open_billing_circuit_refuses_only_a_metered_provider(
        self, tmp_path, monkeypatch,
    ):
        cfg = _deployment(tmp_path)
        _bind(cfg)
        with db.get_db(cfg.db_path) as conn:
            db.block_whatsapp_billing(conn, "wamid.billed")

        blocked, _ = self._send(monkeypatch, cfg, CLOUD_CAPS, key="cloud")
        sent, socket_client = self._send(monkeypatch, cfg, BAILEYS_CAPS, key="socket")

        assert blocked.status == "billing_blocked"
        assert sent.status == "accepted"
        assert len(socket_client.requests) == 1

    def test_a_spent_monthly_cap_refuses_only_a_metered_provider(
        self, tmp_path, monkeypatch,
    ):
        cfg = _deployment(tmp_path, monthly_service_attempt_limit=1)
        _bind(cfg)
        with db.get_db(cfg.db_path) as conn:
            conn.execute(
                "INSERT INTO sent_whatsapp (logical_key, user_id, send_kind, "
                "status, body_chars, body_sha256, quota_month, claimed_at, "
                "created_at, updated_at) "
                "VALUES ('spent', 'alice', 'service', 'accepted', 4, ?, ?, "
                "datetime('now'), datetime('now'), datetime('now'))",
                (hashlib.sha256(b"spent").hexdigest(), outbound.quota_month(cfg)),
            )

        blocked, _ = self._send(monkeypatch, cfg, CLOUD_CAPS, key="cloud")
        sent, socket_client = self._send(monkeypatch, cfg, BAILEYS_CAPS, key="socket")

        assert blocked.status == "budget_exhausted"
        assert sent.status == "accepted"
        assert len(socket_client.requests) == 1

    def test_a_provider_with_no_templates_cannot_fall_through_a_closed_window(
        self, tmp_path, monkeypatch,
    ):
        """Two conditions, deliberately not folded into `template_available`:
        that one answers about the operator's config and this one about what
        the provider can express at all. A paid deployment with an approved
        template is exactly where they come apart.
        """
        cfg = _deployment(
            tmp_path,
            billing_policy="allow_paid",
            proactive_template=WhatsAppTemplateConfig(
                enabled=True, name="istota_notice", language="en_US",
            ),
        )
        _bind(cfg, window=timedelta(hours=48))
        no_templates = dataclasses.replace(CLOUD_CAPS, supports_templates=False)

        via_template, _ = self._send(monkeypatch, cfg, CLOUD_CAPS, key="cloud")
        refused, client = self._send(monkeypatch, cfg, no_templates, key="plain")

        assert (via_template.status, via_template.send_kind) == ("accepted", "template")
        assert refused.status == "window_closed"
        assert client.requests == []

    def test_no_adapter_at_all_is_unconfigured_rather_than_unknown(
        self, tmp_path, monkeypatch,
    ):
        """A provider that cannot be built must never spend `unknown`: nothing
        was sent, and `unknown` is the state an operator cannot resolve."""
        cfg = _deployment(tmp_path)
        _bind(cfg)
        monkeypatch.setattr(outbound, "active_adapter", lambda _config: None)

        record = asyncio.run(outbound.deliver_whatsapp(
            cfg, logical_key="k", user_id="alice", text="the answer",
        ))

        assert record.status == "unconfigured"

    def test_a_client_that_cannot_be_built_settles_the_row_failed(
        self, tmp_path, monkeypatch,
    ):
        """The claim the port rests on, driven to the row rather than read.

        `_send_claimed` used to construct the client inside its pre-send arm,
        so a construction failure settled `failed`. That moved behind
        `adapter.send`, and the equivalence instrument structurally could not
        have caught a change: every existing test injects a client, so none
        reaches the construction branch at all — the pre-change behaviour was
        just as untested. Asserted on the ledger row and the alert, not on the
        adapter's return value.
        """
        def _explode(_config):
            raise ImportError("no module named pywa_async")

        cfg = _deployment(tmp_path)
        _bind(cfg)
        monkeypatch.setattr(
            "istota.transport.whatsapp.client.make_client", _explode,
        )

        record = asyncio.run(outbound.deliver_whatsapp(
            cfg, logical_key="k", user_id="alice", text="the answer",
        ))

        assert record.status == "failed"
        with db.get_db(cfg.db_path) as conn:
            row = conn.execute(
                "SELECT status, meta_message_id FROM sent_whatsapp "
                "WHERE logical_key = 'k'",
            ).fetchone()
            alerts = conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE user_id = 'alice'",
            ).fetchone()[0]
        assert row["status"] == "failed"
        assert row["meta_message_id"] is None
        assert alerts == 1

    def test_a_provider_that_cannot_be_resolved_is_never_raised(self, tmp_path):
        """`active_adapter` sits outside the claim-to-settle region precisely
        so a bad provider name does not escape into the wrapper that settles
        `unknown`."""
        cfg = _deployment(tmp_path)
        cfg.whatsapp.provider = "signal"

        assert outbound.active_adapter(cfg) is None

    def test_enrollment_reads_the_same_capabilities(self, tmp_path, monkeypatch):
        """`is_whatsapp_configured` restates the cost gates rather than calling
        `_gate`, because it deliberately leaves the window out — so it has to
        read the same fields."""
        cfg = _deployment(tmp_path)
        _bind(cfg)
        with db.get_db(cfg.db_path) as conn:
            db.block_whatsapp_billing(conn, "wamid.billed")

        monkeypatch.setattr(
            outbound, "active_adapter",
            lambda _config: _adapter("whatsapp_cloud", CLOUD_CAPS),
        )
        assert outbound.is_whatsapp_configured(cfg, "alice") is False

        monkeypatch.setattr(
            outbound, "active_adapter",
            lambda _config: _adapter("baileys", BAILEYS_CAPS, webhook=False),
        )
        assert outbound.is_whatsapp_configured(cfg, "alice") is True


class TestTheCommonSendPathHoldsNoProviderKnowledge:
    def test_outbound_names_no_meta_module_at_run_time(self):
        """The checkable half of "moved behind the seam".

        Before the port `outbound.py` imported `.client` — the PyWa boundary —
        to build a sender. It now receives an adapter and reaches the provider
        through the record alone, so the surface's common send path names no
        provider module at all.

        The assertions are about *imports* and not about prose: `_observe_
        pricing`'s comment names PyWa's `Pricing.from_dict` to say what this
        module refuses to do, and a source scan that banned the word would
        make that explanation unwritable.
        """
        source = source_of(outbound)
        imports = [
            line.strip() for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]

        assert not [line for line in imports if ".client" in line]
        assert "make_client" not in source

    def test_the_only_seam_imports_are_the_annotation_and_the_registry(self):
        source = source_of(outbound)
        provider_imports = sorted(
            line.strip() for line in source.splitlines()
            if ".providers" in line and "import" in line
        )

        assert provider_imports == [
            "from .providers._types import WhatsAppProviderAdapter, "
            "WhatsAppProviderCaps",
            "from .providers.registry import make_provider_registry  # noqa: PLC0415",
        ]
