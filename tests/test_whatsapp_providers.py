"""The WhatsApp provider seam: the adapter record, the caps and the registry.

`tests/test_sms_foundation.py::TestSmsProviderSeam` is the file this mirrors,
and the places where it deliberately does not are where the two surfaces
differ: an adapter that receives over no HTTP callback at all, and one whose
credential lives nowhere in `config.toml`.
"""

from __future__ import annotations

import dataclasses

import pytest

from istota.config import (
    Config,
    WHATSAPP_PROVIDER_NAMES,
    load_config,
    whatsapp_missing_credentials,
    whatsapp_provider_has_values,
    whatsapp_provider_missing_fields,
    whatsapp_structural_config_errors,
)
from istota.transport.whatsapp._types import (
    WhatsAppSendFailure,
    WhatsAppSendResult,
    WhatsAppWebhookRequest,
    WhatsAppWebhookResult,
)
from istota.transport.whatsapp.providers._types import (
    WhatsAppProviderAdapter,
    WhatsAppProviderCaps,
)
from istota.transport.whatsapp.providers.registry import (
    WhatsAppProviderRegistry,
    make_provider_registry,
)

CLOUD_CAPS = WhatsAppProviderCaps(
    metered=True,
    has_service_window=True,
    supports_templates=True,
    delivery_receipts=True,
)
BAILEYS_CAPS = WhatsAppProviderCaps(
    metered=False,
    has_service_window=False,
    supports_templates=False,
    delivery_receipts=True,
)


def _adapter(name, caps, *, webhook=True) -> WhatsAppProviderAdapter:
    return WhatsAppProviderAdapter(
        name=name,
        caps=caps,
        parse_webhook=(lambda request: request) if webhook else None,
        send=lambda request: WhatsAppSendResult(message_id="wamid.1"),
        verify_signature=(lambda request: True) if webhook else None,
    )


def _cloud_config(*, enabled: bool = True, provider: str = "whatsapp_cloud") -> Config:
    cfg = Config()
    cfg.whatsapp.enabled = enabled
    cfg.whatsapp.provider = provider
    cfg.whatsapp.access_token = "wa-access-token"
    cfg.whatsapp.app_secret = "wa-app-secret"
    cfg.whatsapp.verify_token = "wa-verify-token"
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
        assert isinstance(adapter.send(object()), WhatsAppSendResult)

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


class TestTheProviderFieldValidators:
    def test_the_cloud_block_reports_its_own_blanks(self):
        cfg = Config()
        cfg.whatsapp.access_token = "wa-access-token"

        assert whatsapp_provider_missing_fields(cfg, "whatsapp_cloud") == (
            "app_secret", "verify_token",
        )
        assert whatsapp_provider_has_values(cfg, "whatsapp_cloud") is True

    def test_whitespace_is_blank(self):
        cfg = _cloud_config()
        cfg.whatsapp.app_secret = "   "

        assert whatsapp_provider_missing_fields(cfg, "whatsapp_cloud") == ("app_secret",)

    def test_the_credential_report_is_the_cloud_arm_of_the_same_rule(self):
        """Delegated rather than restated, so the registry's gate and doctor's
        report cannot drift on what counts as blank."""
        cfg = Config()
        cfg.whatsapp.app_secret = " "

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

    def test_every_named_provider_declares_its_fields(self):
        """A name in the tuple with no entry in the map is indistinguishable
        from an unknown one — `()` missing and nothing populated — so it would
        be built unchecked when selected and never built when it is not, with
        nothing anywhere saying so.
        """
        from istota.config import _WHATSAPP_PROVIDER_FIELDS

        assert set(_WHATSAPP_PROVIDER_FIELDS) == set(WHATSAPP_PROVIDER_NAMES)


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
        cfg.whatsapp.app_secret = ""
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


class TestTheSeamCarriesTheExistingVocabulary:
    def test_a_send_outcome_is_the_surfaces_own_record(self):
        """No new event vocabulary is invented for the seam: the adapter's
        `send` returns what `outbound.py` already consumes."""
        adapter = WhatsAppProviderAdapter(
            name="whatsapp_cloud",
            caps=CLOUD_CAPS,
            parse_webhook=None,
            send=lambda request: WhatsAppSendFailure(
                definite=False, error_code=None, safe_reason="delivery state unknown",
            ),
            verify_signature=None,
        )

        outcome = adapter.send(object())

        assert isinstance(outcome, WhatsAppSendFailure)
        assert outcome.definite is False
