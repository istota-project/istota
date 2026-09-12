"""The PyWa boundary: signature validation and the dependency contract.

Two different kinds of test live here and the difference matters. The signature
cases exercise istota's own wrapper against a fixed HMAC vector — they would
pass against any correct implementation. The dependency contract pins the
*shape* of what PyWa exposes, so a future 4.x release that renames the
validator, changes its argument order, or changes the header constant fails
here rather than by quietly refusing (or, far worse, quietly accepting) every
webhook on a deployment.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect

import pytest

from istota.transport.whatsapp import client

APP_SECRET = "wa-app-secret"
BODY = b'{"object":"whatsapp_business_account","entry":[]}'


def _signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256,
    ).hexdigest()


class TestSignatureValidation:
    def test_a_correct_signature_over_the_exact_bytes_is_accepted(self):
        assert client.verify_signature(APP_SECRET, BODY, _signature(APP_SECRET, BODY))

    def test_the_vector_is_fixed_rather_than_recomputed_by_the_helper(self):
        # A test that signs with the same call the product verifies with proves
        # only that one function is its own inverse. This vector was computed
        # once, by hand, from the documented rule: hex HMAC-SHA256 of the raw
        # body under the app secret, prefixed `sha256=`.
        assert client.verify_signature(
            "abc123",
            b"hello",
            "sha256=33963716627d0d59617a4bed91c7f13b"
            "cf3b312e79b946a73937000fa910e3f5",
        )

    @pytest.mark.parametrize(
        "secret, body, signature",
        [
            (APP_SECRET, b'{"object":"x"}', _signature(APP_SECRET, BODY)),
            (APP_SECRET, BODY, _signature("another-app-secret", BODY)),
            (APP_SECRET, BODY, ""),
            (APP_SECRET, BODY, _signature(APP_SECRET, BODY).removeprefix("sha256=")),
            (APP_SECRET, BODY, "sha1=" + _signature(APP_SECRET, BODY)[7:]),
            (APP_SECRET, BODY, "sha256=" + _signature(APP_SECRET, BODY)[7:].upper()),
            (APP_SECRET, BODY, "sha256=zz"),
            (APP_SECRET, BODY, "sha256=é" * 8),
        ],
    )
    def test_every_wrong_signature_shape_is_refused(self, secret, body, signature):
        assert client.verify_signature(secret, body, signature) is False

    def test_an_absent_app_secret_refuses_rather_than_signing_with_an_empty_key(self):
        """The case PyWa's own helper gets wrong for our purposes.

        `webhook_updates_validator` takes the secret as a plain string and
        happily computes an HMAC under `b""`, so with no app secret configured
        it *validates* — against a key every reader of this repository knows.
        A caller could then sign their own payload and be believed. The wrapper
        refuses before reaching it, and the transport refuses to start without
        the secret besides.
        """
        assert client.verify_signature("", BODY, _signature("", BODY)) is False
        assert client.verify_signature("", BODY, "sha256=anything") is False


class TestThePyWaDependencyContract:
    """What a future PyWa minor release must not change under us.

    Every assertion here names something istota calls or will call. The point
    is not that PyWa is fragile; it is that this dependency sits on an
    authentication boundary, and the failure mode of a renamed validator is a
    deployment that stops verifying signatures with nothing in the suite going
    red.
    """

    def test_the_validator_is_the_public_name_with_the_argument_order_we_pass(self):
        from pywa import utils as pywa_utils

        signature = inspect.signature(pywa_utils.webhook_updates_validator)
        assert list(signature.parameters) == [
            "app_secret", "request_body", "x_hub_signature",
        ]
        assert signature.return_annotation in (bool, "bool")

    def test_the_signature_header_constant_is_the_meta_header(self):
        from pywa import utils as pywa_utils

        assert pywa_utils.HUB_SIG == "X-Hub-Signature-256"
        assert client.SIGNATURE_HEADER == pywa_utils.HUB_SIG

    def test_the_wrapper_calls_pywa_rather_than_reimplementing_hmac(self, monkeypatch):
        """A control against the wrapper drifting into a local copy.

        Reimplementing thirty characters of `hmac` here would pass every case
        above and would be a second implementation of the boundary — the thing
        `client.py` exists to prevent.
        """
        from pywa import utils as pywa_utils

        seen: list[tuple] = []

        def spy(app_secret, request_body, x_hub_signature):
            seen.append((app_secret, request_body, x_hub_signature))
            return False

        monkeypatch.setattr(pywa_utils, "webhook_updates_validator", spy)
        assert client.verify_signature(APP_SECRET, BODY, "sha256=00") is False
        assert seen == [(APP_SECRET, BODY, "sha256=00")]

    def test_the_async_send_methods_keep_the_parameters_stage_three_passes(self):
        from pywa_async import WhatsApp

        send_message = inspect.signature(WhatsApp.send_message).parameters
        assert {"to", "text", "reply_to_message_id", "sender"} <= set(send_message)

        send_template = inspect.signature(WhatsApp.send_template).parameters
        assert {"to", "name", "language", "params", "sender"} <= set(send_template)

    def test_the_client_constructor_keeps_the_fields_the_adapter_configures(self):
        from pywa_async import WhatsApp

        params = inspect.signature(WhatsApp.__init__).parameters
        assert {
            "phone_id", "token", "waba_id", "app_secret", "api_version", "session",
        } <= set(params)
        # No `server`/`webhook_endpoint` is ever passed: istota owns FastAPI and
        # the endpoint, so PyWa must never be handed either. That they exist is
        # PyWa's business; that istota's adapter names neither is ours, and the
        # module-text guard below is what holds it.
        assert {"server", "webhook_endpoint", "callback_url"} <= set(params)


class _FakePyWa:
    """Stands in for `pywa_async.WhatsApp` inside the adapter.

    Coarse, at the one boundary `_types.py` exists to bound, and nowhere else:
    everything above `client.py` speaks in local records, so this is the only
    place a double is needed at all.
    """

    def __init__(self, result=None, raises=None):
        self.calls: list[dict] = []
        self.template_calls: list[dict] = []
        self._result = result
        self._raises = raises

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return self._result

    async def send_template(self, **kwargs):
        self.template_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return self._result


class _Sent:
    def __init__(self, message_id):
        self.id = message_id


@pytest.fixture
async def adapters():
    """Real adapters, closed at teardown.

    Each one owns an `httpx.AsyncClient`, so building one per test and never
    closing it leaks a session per test. Async rather than a sync fixture
    calling `asyncio.run`: that would stand up and tear down a second loop
    beside the one pytest-asyncio already runs these tests on.
    """
    built: list = []
    yield built
    for adapter in built:
        await adapter.aclose()


def _adapter(monkeypatch, fake, adapters) -> "client.WhatsAppClient":
    from istota.config import Config, WhatsAppConfig

    config = Config(whatsapp=WhatsAppConfig(
        enabled=True, waba_id="123456789012345",
        phone_number_id="223456789012345",
        business_phone_number="+15551230000",
        access_token="wa-access-token", app_secret=APP_SECRET,
        verify_token="wa-verify-token", request_timeout_seconds=7,
    ))
    adapter = client.WhatsAppClient(config)
    adapters.append(adapter)
    monkeypatch.setattr(adapter, "_client", fake)
    return adapter


def _meta_error(code: int, status: int | None, *, transient=False):
    """A PyWa API error carrying the status an HTTP response would have."""
    import httpx
    from pywa import errors as pywa_errors

    response = None
    if status is not None:
        response = httpx.Response(status, request=httpx.Request("POST", "https://x"))
    return pywa_errors.WhatsAppError(
        raw={"code": code},
        code=code,
        message="Meta's own prose, which must never be echoed",
        details="nor these details",
        fbtrace_id="ABC",
        href=None,
        raw_response=response,
        subcode=None,
        type="OAuthException",
        is_transient=transient,
        user_title=None,
        user_msg=None,
    )


class TestTheSendClassification:
    """Definite versus ambiguous, which is the one bit the ledger reads.

    Definite means Meta processed the request and refused it, so nothing was
    queued and the row is `failed`. Ambiguous means the request may have been
    accepted, so the row is `unknown` and — since the Cloud API has no
    application idempotency token — is never resent. Erring towards ambiguous
    costs an operator a puzzled look; erring towards definite would be a claim
    that nothing was delivered when something may have been.
    """

    async def test_an_accepted_send_returns_metas_message_id(self, monkeypatch, adapters):
        from istota.transport.whatsapp._types import (
            WhatsAppSendRequest, WhatsAppSendResult,
        )

        fake = _FakePyWa(result=_Sent("wamid.ok"))
        adapter = _adapter(monkeypatch, fake, adapters)

        outcome = await adapter.send(WhatsAppSendRequest(
            to="US.1", text="hello", kind="service",
        ))

        assert outcome == WhatsAppSendResult("wamid.ok")
        assert fake.calls[0]["to"] == "US.1"
        assert fake.calls[0]["text"] == "hello"
        assert fake.calls[0]["sender"] == "223456789012345"
        assert fake.calls[0]["buttons"] is None

    async def test_buttons_become_pywa_buttons_only_here(self, monkeypatch, adapters):
        from pywa.types import Button

        from istota.transport.whatsapp._types import WhatsAppSendRequest

        fake = _FakePyWa(result=_Sent("wamid.ok"))
        adapter = _adapter(monkeypatch, fake, adapters)

        await adapter.send(WhatsAppSendRequest(
            to="US.1", text="Proceed?", kind="service",
            buttons=(("confirm:7:yes", "Yes"), ("confirm:7:no", "No")),
        ))

        buttons = fake.calls[0]["buttons"]
        assert [type(b) for b in buttons] == [Button, Button]
        assert [(b.callback_data, b.title) for b in buttons] == [
            ("confirm:7:yes", "Yes"), ("confirm:7:no", "No"),
        ]

    @pytest.mark.parametrize(
        "status, definite",
        [(400, True), (401, True), (403, True), (429, True),
         (500, False), (503, False), (None, False)],
    )
    async def test_a_client_error_is_definite_and_nothing_else_is(
        self, monkeypatch, adapters, status, definite,
    ):
        from istota.transport.whatsapp._types import WhatsAppSendRequest

        adapter = _adapter(
            monkeypatch, _FakePyWa(raises=_meta_error(131047, status)), adapters,
        )

        outcome = await adapter.send(WhatsAppSendRequest(
            to="US.1", text="hi", kind="service",
        ))

        assert outcome.definite is definite
        assert outcome.error_code == "131047"

    async def test_a_transient_client_error_stays_definite(self, monkeypatch, adapters):
        # `is_transient` answers whether retrying *later* might work, not
        # whether this attempt was applied. Reading it as ambiguity would put a
        # rate-limited send — which certainly reached nobody — into the state
        # that means "may have been delivered".
        from istota.transport.whatsapp._types import WhatsAppSendRequest

        adapter = _adapter(
            monkeypatch, _FakePyWa(raises=_meta_error(4, 429, transient=True)), adapters,
        )

        outcome = await adapter.send(WhatsAppSendRequest(
            to="US.1", text="hi", kind="service",
        ))

        assert outcome.definite is True

    @pytest.mark.parametrize(
        "error",
        [
            "timeout", "connect", "read", "pool", "value",
        ],
    )
    async def test_every_transport_failure_is_ambiguous(self, monkeypatch, adapters, error):
        import httpx

        from istota.transport.whatsapp._types import WhatsAppSendRequest

        raises = {
            "timeout": httpx.TimeoutException("t"),
            "connect": httpx.ConnectError("c"),
            "read": httpx.ReadTimeout("r"),
            "pool": httpx.PoolTimeout("p"),
            "value": ValueError("unparseable body"),
        }[error]
        adapter = _adapter(monkeypatch, _FakePyWa(raises=raises), adapters)

        outcome = await adapter.send(WhatsAppSendRequest(
            to="US.1", text="hi", kind="service",
        ))

        assert outcome.definite is False
        assert outcome.error_code is None

    @pytest.mark.parametrize("value", [None, "", 12345, object()])
    async def test_a_success_with_no_readable_id_is_ambiguous(
        self, monkeypatch, adapters, value,
    ):
        # Meta may well have queued the message; a 200 we cannot read is not
        # evidence that it did not.
        from istota.transport.whatsapp._types import WhatsAppSendRequest

        adapter = _adapter(monkeypatch, _FakePyWa(result=_Sent(value)), adapters)

        outcome = await adapter.send(WhatsAppSendRequest(
            to="US.1", text="hi", kind="service",
        ))

        assert outcome.definite is False

    async def test_a_template_send_names_the_configured_template(
        self, monkeypatch, adapters,
    ):
        from pywa.types.templates import TemplateLanguage

        from istota.transport.whatsapp._types import (
            WhatsAppSendRequest, WhatsAppSendResult,
        )

        fake = _FakePyWa(result=_Sent("wamid.tmpl"))
        adapter = _adapter(monkeypatch, fake, adapters)

        outcome = await adapter.send(WhatsAppSendRequest(
            to="US.1", text="the late answer", kind="template",
            template_name="istota_result", template_language="en_US",
        ))

        assert outcome == WhatsAppSendResult("wamid.tmpl")
        assert fake.calls == []
        call = fake.template_calls[0]
        assert call["to"] == "US.1"
        assert call["name"] == "istota_result"
        assert call["language"] is TemplateLanguage.ENGLISH_US
        assert call["sender"] == "223456789012345"
        assert [param.to_dict() for param in call["params"]] == [
            {
                "type": "BODY",
                "parameters": [{"type": "text", "text": "the late answer"}],
            }
        ]

    @pytest.mark.parametrize(
        "name, language",
        [
            ("", "en_US"),
            ("istota_result", ""),
            ("istota_result", "zz_ZZ"),
        ],
        ids=["no-name", "no-language", "unknown-language"],
    )
    async def test_an_unusable_template_is_definite_and_never_calls(
        self, monkeypatch, adapters, name, language,
    ):
        """`definite`, because nothing opened a socket.

        The unknown-language case is the one worth having: PyWa's
        `TemplateLanguage` has no `UNKNOWN` member, so its `_missing_` hook
        raises `TypeError` rather than returning one — which the generic
        handler would classify as *ambiguous*, i.e. "may have reached Meta".
        Resolving the code before the call is what keeps a local
        misconfiguration out of the one state an operator cannot resolve.
        """
        from istota.transport.whatsapp._types import WhatsAppSendRequest

        fake = _FakePyWa(result=_Sent("wamid.never"))
        adapter = _adapter(monkeypatch, fake, adapters)

        outcome = await adapter.send(WhatsAppSendRequest(
            to="US.1", text="hi", kind="template",
            template_name=name, template_language=language,
        ))

        assert outcome.definite is True
        assert fake.calls == [] and fake.template_calls == []
        assert "zz_ZZ" not in outcome.safe_reason

    @pytest.mark.parametrize("status", [400, 500, None])
    async def test_no_failure_reason_carries_provider_text(self, monkeypatch, adapters, status):
        # Meta's prose, PyWa's exception text and an httpx repr all carry the
        # request URL, which carries the recipient and the token's path
        # segment. The reason is chosen from a fixed table by classification.
        from istota.transport.whatsapp._types import WhatsAppSendRequest

        adapter = _adapter(
            monkeypatch, _FakePyWa(raises=_meta_error(131047, status)), adapters,
        )

        outcome = await adapter.send(WhatsAppSendRequest(
            to="US.1", text="hi", kind="service",
        ))

        assert "prose" not in outcome.safe_reason
        assert "details" not in outcome.safe_reason
        assert "US.1" not in outcome.safe_reason
        assert "wa-access-token" not in outcome.safe_reason

    async def test_the_configured_timeout_bounds_the_session(self, monkeypatch):
        from istota.config import Config, WhatsAppConfig

        config = Config(whatsapp=WhatsAppConfig(
            enabled=True, waba_id="1", phone_number_id="2",
            access_token="t", app_secret="s", verify_token="v",
            request_timeout_seconds=7,
        ))
        adapter = client.WhatsAppClient(config)
        try:
            assert adapter._session.timeout.read == 7
        finally:
            await adapter.aclose()

    async def test_closing_a_session_the_caller_supplied_is_the_callers_job(
        self, monkeypatch,
    ):
        import httpx

        from istota.config import Config, WhatsAppConfig

        session = httpx.AsyncClient()
        config = Config(whatsapp=WhatsAppConfig(
            enabled=True, waba_id="1", phone_number_id="2",
            access_token="t", app_secret="s", verify_token="v",
        ))
        adapter = client.WhatsAppClient(config, session=session)

        await adapter.aclose()

        assert session.is_closed is False
        await session.aclose()


class TestTheAdapterStaysNarrow:
    def test_the_client_module_registers_no_callback_url_and_no_pywa_server(self):
        from tests.support.drift import source_of

        source = source_of(client)
        for forbidden in (
            "callback_url=", "webhook_endpoint=", "server=", "handlers_modules=",
            "set_callback_url(", "on_message", ".run(", "flask", "Flask",
        ):
            assert forbidden not in source, (
                f"client.py names {forbidden!r}: istota owns the webhook endpoint "
                "and must never let PyWa register or serve one"
            )
