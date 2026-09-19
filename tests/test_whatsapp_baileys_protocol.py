"""The Baileys wire format: framing, bounds, and what each line means.

Two properties carry most of this file. **A frame this side cannot read is
refused rather than half-read** — every normalizer raises instead of filling a
gap, because the gap that matters is the sender, and a message attributed to a
guessed sender is a principal takeover rather than a lost message. And **an
ambiguous send outcome is never reported as a definite one**: `definite` is the
single bit deciding `failed` against `unknown` in the ledger, so it is read off
the line and never derived.

The cap is asserted on both ends. A cap only on the reader lets a writer build
a line it can never deliver; a cap only on the writer lets a garbled peer ask
for an unbounded buffer. `tool_server_protocol` states the same rule.
"""

from __future__ import annotations

import json
from datetime import timezone

import pytest

from istota.transport.whatsapp import baileys_protocol as proto
from istota.transport.whatsapp._types import (
    WhatsAppSendFailure,
    WhatsAppSendRequest,
    WhatsAppSendResult,
)

JID = "15551234567@s.whatsapp.net"


def _inbound(**overrides) -> dict:
    payload = {
        "type": proto.MSG_INBOUND,
        "message_id": "BAE5F00D",
        "jid": JID,
        "message_type": "text",
        "text": "check the backup",
        "timestamp": 1767225600,
    }
    payload.update(overrides)
    return payload


def _receipt(**overrides) -> dict:
    payload = {
        "type": proto.MSG_RECEIPT,
        "message_id": "BAE5F00D",
        "status": "delivered",
        "timestamp": 1767225600,
    }
    payload.update(overrides)
    return payload


class TestFraming:
    def test_a_line_round_trips(self):
        raw = proto.encode(proto.MSG_READY, jid=JID)
        assert raw.endswith(b"\n")
        assert proto.decode(raw) == {"type": proto.MSG_READY, "jid": JID}

    def test_an_embedded_newline_cannot_forge_a_frame(self):
        """The whole framing guarantee, in one case.

        A message body is model- and stranger-supplied text that routinely
        carries newlines. If one reached the wire unescaped it would end the
        line early and the remainder would be read as a second message —
        attacker-chosen, including its `type`.
        """
        raw = proto.encode(
            proto.MSG_INBOUND,
            text='first\n{"type":"fatal","reason":"logged_out"}',
        )
        assert raw.count(b"\n") == 1
        assert proto.decode(raw)["type"] == proto.MSG_INBOUND

    @pytest.mark.parametrize(
        "line",
        [b"", b"   \n", b"not json\n", b'"a string"\n', b"[1,2]\n", b"{}\n",
         b'{"type": ""}\n', b'{"type": 3}\n'],
    )
    def test_an_unusable_line_is_refused(self, line):
        with pytest.raises(proto.BaileysProtocolError):
            proto.decode(line)

    def test_the_cap_is_enforced_on_the_encoder(self):
        with pytest.raises(proto.BaileysProtocolError):
            proto.encode(proto.MSG_INBOUND, text="x" * (proto.MAX_LINE_BYTES + 1))

    def test_the_cap_is_enforced_on_the_decoder(self):
        line = json.dumps(
            {"type": proto.MSG_INBOUND, "text": "x" * proto.MAX_LINE_BYTES}
        ).encode()
        assert len(line) > proto.MAX_LINE_BYTES
        with pytest.raises(proto.BaileysProtocolError):
            proto.decode(line)

    def test_an_unserializable_field_is_refused_rather_than_raising_typeerror(self):
        with pytest.raises(proto.BaileysProtocolError):
            proto.encode(proto.MSG_SEND, buttons=object())

    def test_a_deeply_nested_line_does_not_escape_as_recursionerror(self):
        """`RecursionError` is not a `ValueError`, and an authenticated peer
        chooses the body. `webhook.parse_webhook` catches the same pair."""
        with pytest.raises(proto.BaileysProtocolError):
            proto.decode(b"[" * 60000)

    def test_the_two_directions_do_not_overlap(self):
        assert not (proto.UP_MESSAGES & proto.DOWN_MESSAGES)


class TestTheInboundEvent:
    def test_it_carries_the_jid_and_nothing_cloud_shaped(self):
        event = proto.inbound_event(_inbound())

        assert event.from_user.jid == JID
        assert event.message_id == "BAE5F00D"
        assert event.text == "check the backup"
        assert event.sent_at.tzinfo is timezone.utc

    def test_the_cloud_identity_fields_are_empty_rather_than_invented(self):
        """The Stage 4 deferral, settled.

        `waba_id` and `phone_number_id` name a Meta account a Baileys
        deployment does not have. `webhook.normalize_payload` checks both
        against the configured Cloud account before it builds the record, and
        nothing reads either past that point — so the empty string is the
        honest value, and the fields stay required so no Cloud path can drop
        one silently.
        """
        event = proto.inbound_event(_inbound())

        assert event.waba_id == ""
        assert event.phone_number_id == ""

    def test_the_bsuid_is_empty_so_no_cloud_arm_can_read_it(self):
        """The cross-adapter takeover, refused at the wire.

        `identity.resolve_inbound_identity` picks its arm from the provider, so
        a populated `bsuid` here could only ever be a value some later reader
        mistakes for a Cloud identity.
        """
        event = proto.inbound_event(_inbound(bsuid="US.9876543210"))

        assert event.from_user.bsuid == ""
        assert event.from_user.wa_id is None

    def test_a_group_message_is_typed_before_any_identity_lookup(self):
        event = proto.inbound_event(
            _inbound(group=True, text="third party text", callback_data="confirm:1:yes")
        )

        assert event.message_type == "group"
        assert event.text is None and event.callback_data is None

    @pytest.mark.parametrize("missing", ["message_id", "jid"])
    def test_a_message_with_no_sender_or_no_id_is_refused(self, missing):
        payload = _inbound()
        payload.pop(missing)
        with pytest.raises(proto.BaileysProtocolError):
            proto.inbound_event(payload)

    def test_an_over_long_message_id_is_refused_at_the_wire(self):
        """The id goes onto a uniquely-indexed ledger column, so it is bounded
        where it arrives rather than where it is stored."""
        with pytest.raises(proto.BaileysProtocolError):
            proto.inbound_event(
                _inbound(message_id="x" * (proto.MAX_MESSAGE_ID_CHARS + 1))
            )

    def test_a_body_longer_than_whatsapp_allows_is_refused(self):
        """The first surface where the body is not bounded upstream.

        Meta refuses a Cloud message past 4,096 characters before it ever
        reaches `normalize_payload`; a Baileys line is bounded only by the
        256 KiB frame cap, so a quarter-megabyte body would reach a task
        prompt. Refused rather than truncated — a message longer than WhatsApp
        itself permits did not come from WhatsApp, and cutting somebody's words
        silently is worse than dropping a frame that cannot be genuine.
        """
        with pytest.raises(proto.BaileysProtocolError):
            proto.inbound_event(_inbound(text="x" * (proto.MAX_INBOUND_TEXT_CHARS + 1)))

    def test_a_body_at_the_limit_is_taken(self):
        event = proto.inbound_event(_inbound(text="x" * proto.MAX_INBOUND_TEXT_CHARS))
        assert len(event.text) == proto.MAX_INBOUND_TEXT_CHARS

    def test_an_over_long_display_name_is_refused(self):
        with pytest.raises(proto.BaileysProtocolError):
            proto.inbound_event(
                _inbound(username="n" * (proto.MAX_USERNAME_CHARS + 1))
            )

    @pytest.mark.parametrize("timestamp", [None, "1767225600", True, 10**20])
    def test_an_unusable_timestamp_is_refused_rather_than_defaulted(self, timestamp):
        """It reaches `webhook._window_stamp`, which clamps a wrong value. A
        receiver inventing one would be asserting when somebody wrote."""
        payload = _inbound()
        if timestamp is None:
            payload.pop("timestamp")
        else:
            payload["timestamp"] = timestamp
        with pytest.raises(proto.BaileysProtocolError):
            proto.inbound_event(payload)


class TestTheReceipt:
    @pytest.mark.parametrize(
        ("wire", "mapped"),
        [("sent", "sent"), ("delivered", "delivered"), ("READ", "read"),
         ("failed", "failed"), ("error", "failed")],
    )
    def test_the_vocabulary_maps_onto_the_ledger(self, wire, mapped):
        event = proto.delivery_event(_receipt(status=wire))
        assert event is not None and event.status == mapped

    @pytest.mark.parametrize("status", ["played", "pending", "", None])
    def test_a_status_this_surface_does_not_model_is_dropped(self, status):
        payload = _receipt()
        if status is None:
            payload.pop("status")
        else:
            payload["status"] = status
        assert proto.delivery_event(payload) is None

    def test_pricing_is_silence_rather_than_a_claim_of_being_free(self):
        """`billable = False` would assert a pricing fact nobody observed.
        `None` is what the ledger reads as "the provider said nothing", and the
        free-guard circuit arms on `True` alone."""
        event = proto.delivery_event(_receipt())

        assert event.billable is None
        assert event.pricing_model is None and event.pricing_category is None

    def test_an_error_code_survives_as_text(self):
        event = proto.delivery_event(_receipt(status="failed", error_code=408))
        assert event.error_code == "408"

    @pytest.mark.parametrize("code", [True, False])
    def test_a_boolean_is_not_an_error_code(self, code):
        """`bool` is a subclass of `int`, so the obvious isinstance check
        renders `true` as the string `"True"` into `sent_whatsapp.error_code`
        and every operator surface reading it. `_event_time` and
        `hello_version` guard the same way."""
        event = proto.delivery_event(_receipt(status="failed", error_code=code))
        assert event.error_code is None

    def test_the_send_outcome_applies_the_same_rule(self):
        outcome = proto.send_outcome({"ok": False, "error_code": True})
        assert outcome.error_code is None


class TestTheSendOutcome:
    def test_an_accepted_send_carries_its_message_id(self):
        outcome = proto.send_outcome(
            {"ok": True, "message_id": "BAE5CAFE", "request_id": "abc"}
        )
        assert isinstance(outcome, WhatsAppSendResult)
        assert outcome.message_id == "BAE5CAFE"

    def test_a_definite_refusal_is_taken_as_definite(self):
        outcome = proto.send_outcome(
            {"ok": False, "definite": True, "reason": "not_on_whatsapp"}
        )
        assert isinstance(outcome, WhatsAppSendFailure)
        assert outcome.definite is True
        assert outcome.safe_reason == "the destination is not a WhatsApp user"

    @pytest.mark.parametrize("definite", [None, False, "true", 1, "yes"])
    def test_anything_but_a_true_boolean_is_ambiguous(self, definite):
        """The direction of the default is the point.

        `definite` settles the row `failed` and its absence settles `unknown`.
        A string `"true"` from a sidecar that spelled the field wrong must not
        become a claim that the message never left.
        """
        payload = {"ok": False, "reason": "rejected"}
        if definite is not None:
            payload["definite"] = definite
        assert proto.send_outcome(payload).definite is False

    def test_the_sidecars_own_words_never_become_the_reason(self):
        """Baileys' errors carry the destination JID and, on a Boom error, the
        whole request. `.claude/rules/whatsapp.md` records the same hazard for
        Meta's prose and answers it the same way: a fixed table."""
        leaked = "send to 15551234567@s.whatsapp.net failed: ECONNRESET"
        outcome = proto.send_outcome({"ok": False, "reason": leaked})

        assert "15551234567" not in outcome.safe_reason
        assert outcome.safe_reason == "the sidecar could not send the message"

    def test_an_accepted_send_with_no_id_is_refused(self):
        """An `ok` with no id is a claim with nothing to settle the row on, and
        nothing to match a later receipt against."""
        with pytest.raises(proto.BaileysProtocolError):
            proto.send_outcome({"ok": True, "request_id": "abc"})

    def test_a_local_failure_names_no_provider_code(self):
        outcome = proto.local_failure(proto.REASON_SEND_TIMEOUT, definite=False)
        assert outcome.error_code is None and outcome.definite is False


class TestTheSendPayload:
    def test_a_service_send_carries_its_buttons_and_reply(self):
        payload = proto.send_payload(
            "req-1",
            WhatsAppSendRequest(
                to=JID, text="Approve?", kind="service",
                reply_to_message_id="BAE5F00D",
                buttons=(("confirm:7:yes", "Yes"), ("confirm:7:no", "No")),
            ),
        )

        assert payload["request_id"] == "req-1"
        assert payload["buttons"] == [
            ["confirm:7:yes", "Yes"], ["confirm:7:no", "No"],
        ]
        assert payload["reply_to_message_id"] == "BAE5F00D"

    def test_a_template_crosses_as_its_kind_with_no_meta_account_state(self):
        """`kind` crosses so a sidecar can refuse a rendering it cannot
        honour; the template's name and language are Meta account state with
        no meaning here and are not sent."""
        payload = proto.send_payload(
            "req-2",
            WhatsAppSendRequest(
                to=JID, text="hello", kind="template",
                template_name="istota_notice", template_language="en",
            ),
        )

        assert payload["kind"] == "template"
        assert "template_name" not in payload
        assert "template_language" not in payload

    def test_the_flattened_request_encodes(self):
        request = WhatsAppSendRequest(to=JID, text="hi", kind="service")
        raw = proto.encode(proto.MSG_SEND, **proto.send_payload("r", request))
        assert proto.decode(raw)["to"] == JID


class TestHello:
    @pytest.mark.parametrize("value", [None, "1", True, 1.0])
    def test_a_hello_with_no_integer_version_is_refused(self, value):
        payload = {"type": proto.MSG_HELLO}
        if value is not None:
            payload["protocol_version"] = value
        with pytest.raises(proto.BaileysProtocolError):
            proto.hello_version(payload)

    def test_the_version_is_read_as_given(self):
        assert proto.hello_version({"protocol_version": 7}) == 7


class TestTheModuleBoundary:
    def test_it_imports_only_plain_data_and_the_containment_rule(self):
        """What this module may name, and what the pin is now worth.

        It used to be `._types` alone, asserted transitively on
        `session/session_log.py`'s line around `istota.llm.types`: a module of
        plain data importing nothing itself costs no import graph, and
        checking only the direct imports would pass for a `._types` that had
        since grown a `config` import.

        **`.media` breaks the transitive half and the claim behind it was
        already false.** Measured: importing this module executes
        `transport/whatsapp/__init__.py`, which imports `transport._types`,
        which pulls `db`, `storage` and `config` — so the graph arrives
        through the package whatever this file names, and `media.py`'s own
        docstring carries the same measurement in the other direction. What
        the pin actually buys, and still buys, is that the *named* set is
        small enough that a third entry is a decision somebody makes rather
        than an accident: a normalizer reaching for a brain, a transport or a
        database is the thing to catch.

        `.media` earns its place because `is_staged_name` is the rule for
        whether a value off the wire may be joined under the staging root, and
        this is the module that reads that value. The alternative was a second
        copy of a containment test, which is a worse trade than a name here.

        The `._types` half is unchanged and still transitive: that module must
        import nothing from the package at all.
        """
        import ast
        from pathlib import Path

        import istota.transport.whatsapp._types as types_module
        import istota.transport.whatsapp.baileys_protocol as protocol_module

        def package_imports(module) -> set[str]:
            tree = ast.parse(Path(module.__file__).read_text())
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level:
                    # A relative import naming no module — `from . import x` —
                    # contributes the *names* it binds. Collapsing it to `""`
                    # made `from . import media` and
                    # `from . import media, outbound` the identical set, so the
                    # guard stopped discriminating at exactly the moment it was
                    # widened to allow one sibling.
                    if node.module:
                        names.add(node.module)
                    else:
                        names.update(alias.name for alias in node.names)
                elif isinstance(node, ast.Import):
                    names.update(
                        alias.name for alias in node.names
                        if alias.name.split(".")[0] == "istota"
                    )
            return names

        assert package_imports(protocol_module) == {"_types", "media"}
        assert package_imports(types_module) == set()

        # The control for the widening: a second sibling on the same
        # `from . import` line has to move the set, or allowing one sibling
        # quietly allowed every sibling.
        import textwrap

        widened = ast.parse(textwrap.dedent('''
            from . import media, outbound
            from ._types import InboundWhatsAppEvent
        '''))
        names = set()
        for node in ast.walk(widened):
            if isinstance(node, ast.ImportFrom) and node.level:
                if node.module:
                    names.add(node.module)
                else:
                    names.update(alias.name for alias in node.names)
        assert names == {"_types", "media", "outbound"}
