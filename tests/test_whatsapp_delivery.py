"""Outbound WhatsApp: rendering, the service window, the ledger, delivery state.

The shape mirrors `tests/test_sms_core.py`'s outbound half, because the two
surfaces make the same promise — one provider message per logical output, no
automatic resend, a failure reported off the failing surface. What differs is
what stands between a rendered answer and a Cloud API call: SMS asks only
whether the number is opted out, and WhatsApp asks whether Meta's 24-hour
customer service window is still open, because a send outside it either costs
money or is refused.

Three things every case here holds. Nothing sends twice: every ledger state but
an unclaimed `pending` refuses a second attempt, including the ambiguous
`unknown` a timeout leaves. Nothing reaches Meta before a row is committed
saying it is about to. And no failure is reported over WhatsApp — the one
surface that has just proved it cannot reach the user.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import unicodedata
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from istota import db, notifications, surfaces
from istota.config import Config, UserConfig, WhatsAppConfig
from istota.transport import Destination, make_registry
from istota.transport.routing import (
    origin_descriptor,
    parse_output_target,
    resolve_delivery_plan,
)
from istota.transport.whatsapp import (
    LOCAL_TERMINAL_STATES,
    WhatsAppTransport,
    whatsapp_conversation_token,
)
from istota.transport.whatsapp._types import (
    WhatsAppDeliveryEvent,
    WhatsAppSendFailure,
    WhatsAppSendRequest,
    WhatsAppSendResult,
)
from istota.transport.whatsapp.outbound import (
    SERVICE_WINDOW,
    TEMPLATE_PARAMETER_LIMIT,
    WHATSAPP_TEXT_LIMIT,
    apply_delivery_event,
    deliver_whatsapp,
    is_whatsapp_configured,
    render_template_parameter,
    render_whatsapp,
    service_window_open,
)

WABA_ID = "123456789012345"
PHONE_NUMBER_ID = "223456789012345"
USER_NUMBER = "+15551234567"
USER_BSUID = "US.9876543210"
TRUNCATION_SUFFIX = "\n\n[Reply shortened. Send a narrower follow-up.]"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _config(tmp_path, **overrides) -> Config:
    path = tmp_path / "istota.db"
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
        whatsapp=WhatsAppConfig(**fields),
        users={"alice": UserConfig()},
    )
    config.site.hostname = "assistant.example.com"
    return config


def _sql_now(offset: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + offset).strftime("%Y-%m-%d %H:%M:%S")


def _bind(config, user_id="alice", *, window: timedelta | None = timedelta(),
          **kwargs):
    """One enrolled binding whose service window is `window` old (open by default)."""
    fields = dict(bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)
    fields.update(kwargs)
    with db.get_db(config.db_path) as conn:
        binding = db.set_whatsapp_binding(conn, user_id, **fields)
        if not fields.get("send_id") and fields.get("bsuid"):
            conn.execute(
                "UPDATE whatsapp_user_bindings SET send_id = ? WHERE user_id = ?",
                (fields["bsuid"], user_id),
            )
        if window is not None:
            conn.execute(
                "UPDATE whatsapp_user_bindings SET last_user_message_at = ? "
                "WHERE user_id = ?",
                (_sql_now(-window), user_id),
            )
    return binding


class _FakeClient:
    """The one coarse fake, at the PyWa adapter boundary and nowhere else."""

    def __init__(self, outcome=None, *, on_send=None):
        self.requests: list[WhatsAppSendRequest] = []
        self.closed = 0
        self._outcome = outcome or WhatsAppSendResult("wamid.sent.1")
        self._on_send = on_send

    async def send(self, request: WhatsAppSendRequest):
        self.requests.append(request)
        if self._on_send is not None:
            return await self._on_send(request) or self._outcome
        return self._outcome

    async def aclose(self) -> None:
        self.closed += 1


def _rows(config, logical_key=None):
    with db.get_db(config.db_path) as conn:
        if logical_key is None:
            return conn.execute(
                "SELECT * FROM sent_whatsapp ORDER BY id"
            ).fetchall()
        return conn.execute(
            "SELECT * FROM sent_whatsapp WHERE logical_key = ?", (logical_key,),
        ).fetchall()


def _alerts(config):
    with db.get_db(config.db_path) as conn:
        return conn.execute(
            "SELECT source, dedup_key, title FROM notifications ORDER BY id"
        ).fetchall()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestTheRenderer:
    def test_a_fence_loses_its_markers_and_keeps_its_text(self):
        rendered = render_whatsapp("Here:\n```python\nx = 1\n```\ndone")

        assert rendered == "Here:\nx = 1\ndone"

    def test_a_table_loses_its_rule_and_keeps_its_cells(self):
        rendered = render_whatsapp("| a | b |\n|---|---|\n| 1 | 2 |")

        assert rendered == "| a | b |\n| 1 | 2 |"

    def test_a_url_survives_intact(self):
        url = "https://example.com/path?a=1&b=2#frag"

        assert url in render_whatsapp(f"See {url} for more.")

    def test_whatsapp_emphasis_markers_survive(self):
        # WhatsApp's own vocabulary is single `*` and `_`. Stripping them, as
        # the SMS renderer does, would take formatting the recipient actually
        # sees rendered.
        assert render_whatsapp("*bold* and _italic_") == "*bold* and _italic_"

    def test_markdown_strong_becomes_whatsapp_bold(self):
        # `**bold**` renders in WhatsApp as a bold word wearing two stray
        # asterisks. The model writes Markdown whatever the guideline says.
        assert render_whatsapp("**bold** and __also__") == "*bold* and _also_"

    def test_nul_and_lone_surrogates_are_replaced(self):
        rendered = render_whatsapp("a\x00b\ud800c")

        assert rendered == "a�b�c"

    def test_more_than_two_blank_lines_collapse(self):
        assert render_whatsapp("a\n\n\n\n\nb") == "a\n\nb"

    def test_exactly_the_limit_is_not_truncated(self):
        text = "a" * WHATSAPP_TEXT_LIMIT

        rendered = render_whatsapp(text)

        assert rendered == text
        assert len(rendered) == 4096

    def test_one_character_over_the_limit_truncates_within_it(self):
        rendered = render_whatsapp("a" * (WHATSAPP_TEXT_LIMIT + 1))

        assert rendered.endswith(TRUNCATION_SUFFIX)
        assert len(rendered) <= WHATSAPP_TEXT_LIMIT

    def test_the_suffix_is_accounted_for_inside_the_limit(self):
        # The failure this catches is a truncation that cuts to the limit and
        # then appends, producing a message Meta refuses.
        rendered = render_whatsapp("b" * 10_000)

        assert len(rendered) <= WHATSAPP_TEXT_LIMIT
        assert len(rendered) > WHATSAPP_TEXT_LIMIT - len(TRUNCATION_SUFFIX) - 8

    def test_truncation_does_not_split_a_combining_sequence(self):
        # A base character and its combining marks are one grapheme; cutting
        # between them leaves a mark attached to whatever precedes it.
        text = "é" * 4000
        rendered = render_whatsapp(text)

        body = rendered[: -len(TRUNCATION_SUFFIX)]
        assert body and len(rendered) <= WHATSAPP_TEXT_LIMIT
        # The discriminating property is where the cut fell, not what the body
        # ends with: a cluster kept whole still ends in a combining mark. A
        # naive cut at the character limit lands between the base and its mark,
        # which is what this catches.
        tail = text[len(body):]
        assert tail and unicodedata.combining(tail[0]) == 0

    def test_a_limit_below_the_suffix_still_holds(self):
        # Both entry points expose `limit`, and the naive arithmetic
        # (`limit - len(suffix)`, floored at zero) returns the whole suffix —
        # a string *longer* than the limit, from a function whose contract is
        # that it is not.
        for limit in (0, 1, 10, len(TRUNCATION_SUFFIX) - 1):
            assert len(render_whatsapp("z" * 200, limit=limit)) <= limit

    def test_an_emoji_sequence_is_the_stated_limitation_not_a_claim(self):
        # `"🙂" * 5000` asserts nothing about the code: an astral character is
        # one code point, so no `str` slice can halve it and `"�" not in
        # rendered` is true of `text[:limit]` too. What `_truncate` actually
        # guards is `unicodedata.combining`, which is zero for a zero-width
        # joiner — so a ZWJ sequence *is* cut, and the docstring says so. This
        # states the real behaviour rather than dressing a tautology up as a
        # guarantee.
        family = "\U0001f468‍\U0001f469‍\U0001f467"
        rendered = render_whatsapp(family * 2000)

        assert len(rendered) <= WHATSAPP_TEXT_LIMIT
        body = rendered[: -len(TRUNCATION_SUFFIX)]
        assert (family * 2000).startswith(body)

    def test_the_template_parameter_is_capped_at_nine_hundred(self):
        rendered = render_template_parameter("c" * 5000)

        assert len(rendered) <= TEMPLATE_PARAMETER_LIMIT == 900
        assert rendered.endswith("]")

    def test_the_template_parameter_carries_no_newline_tab_or_run_of_spaces(self):
        rendered = render_template_parameter("one\ntwo\tthree     four\r\nfive")

        assert "\n" not in rendered and "\t" not in rendered and "\r" not in rendered
        assert "    " not in rendered
        assert "one two three four five" in rendered

    def test_the_template_parameter_drops_control_characters(self):
        rendered = render_template_parameter("a\x07b\x00c")

        assert "\x07" not in rendered and "\x00" not in rendered


# ---------------------------------------------------------------------------
# The customer service window
# ---------------------------------------------------------------------------


class TestTheServiceWindow:
    def test_the_local_window_is_five_minutes_short_of_metas(self):
        assert SERVICE_WINDOW == timedelta(hours=23, minutes=55)

    @pytest.mark.parametrize(
        "age, expected",
        [
            (timedelta(0), True),
            (timedelta(hours=23, minutes=54), True),
            (timedelta(hours=23, minutes=56), False),
            (timedelta(hours=48), False),
        ],
    )
    def test_the_window_closes_at_the_local_margin(self, tmp_path, age, expected):
        config = _config(tmp_path)
        _bind(config, window=age)
        with db.get_db(config.db_path) as conn:
            binding = db.get_whatsapp_binding(conn, "alice")

        assert service_window_open(binding) is expected

    def test_a_binding_that_never_wrote_in_has_no_window(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, window=None)
        with db.get_db(config.db_path) as conn:
            binding = db.get_whatsapp_binding(conn, "alice")

        assert service_window_open(binding) is False

    def test_no_binding_has_no_window(self):
        assert service_window_open(None) is False


# ---------------------------------------------------------------------------
# The outbound ledger
# ---------------------------------------------------------------------------


class TestTheLedgerClaim:
    async def test_one_logical_output_is_claimed_once_under_concurrency(
        self, tmp_path,
    ):
        started = threading.Event()
        release = threading.Event()

        async def blocking(_request):
            started.set()
            await asyncio.to_thread(release.wait, 2)
            return WhatsAppSendResult("wamid.only")

        config = _config(tmp_path)
        _bind(config)
        client = _FakeClient(on_send=blocking)

        first = asyncio.create_task(deliver_whatsapp(
            config, logical_key="task-result:1", user_id="alice",
            text="done", client=client,
        ))
        assert await asyncio.to_thread(started.wait, 2)
        second = await deliver_whatsapp(
            config, logical_key="task-result:1", user_id="alice",
            text="done", client=client,
        )
        release.set()
        first_record = await first

        assert first_record.status == "accepted"
        assert second.status == "pending"
        assert len(client.requests) == 1
        assert len(_rows(config)) == 1

    @pytest.mark.parametrize(
        "status",
        ["accepted", "sent", "delivered", "read", "failed", *sorted(LOCAL_TERMINAL_STATES)],
    )
    @pytest.mark.parametrize("claimed", [True, False], ids=["claimed", "unclaimed"])
    async def test_no_state_but_an_unclaimed_pending_is_ever_resent(
        self, tmp_path, status, claimed,
    ):
        """Both halves of the settled test, because they cover different rows.

        With `claimed_at` set, `row["claimed_at"] is not None` alone answers,
        so the `_NO_RESEND` half of the condition is never reached — delete it
        and every case here stays green. That matters because `_claim` writes
        `claimed_at = None` for every *blocked* outcome, so a `window_closed`,
        `opted_out`, `billing_blocked`, `budget_exhausted` or `unconfigured`
        row exists with a NULL claim and is protected by `_NO_RESEND` and
        nothing else. The unclaimed half is the one that exercises it.
        """
        config = _config(tmp_path)
        _bind(config)
        stamp = "datetime('now')" if claimed else "NULL"
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "INSERT INTO sent_whatsapp (logical_key, user_id, send_kind, status, "
                f"body_chars, body_sha256, claimed_at, attempted_at, created_at, "
                f"updated_at) VALUES (?, 'alice', 'service', ?, 4, 'x', "
                f"{stamp}, {stamp}, datetime('now'), datetime('now'))",
                ("task-result:9", status),
            )
        client = _FakeClient()

        record = await deliver_whatsapp(
            config, logical_key="task-result:9", user_id="alice",
            text="done", client=client,
        )

        assert record.status == status
        assert client.requests == []
        assert len(_rows(config)) == 1

    async def test_an_unclaimed_pending_row_is_claimable(self, tmp_path):
        config = _config(tmp_path)
        _bind(config)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "INSERT INTO sent_whatsapp (logical_key, user_id, send_kind, status, "
                "body_chars, body_sha256, created_at, updated_at) "
                "VALUES ('task-result:4', 'alice', 'service', 'pending', 0, '', "
                "datetime('now'), datetime('now'))"
            )
        client = _FakeClient()

        record = await deliver_whatsapp(
            config, logical_key="task-result:4", user_id="alice",
            text="done", client=client,
        )

        assert record.status == "accepted"
        assert len(client.requests) == 1

    async def test_the_row_is_committed_before_the_first_api_call(self, tmp_path):
        config = _config(tmp_path)
        _bind(config)
        seen: list[str] = []

        async def observe(_request):
            seen.extend(row["status"] for row in _rows(config, "task-result:2"))
            return WhatsAppSendResult("wamid.x")

        await deliver_whatsapp(
            config, logical_key="task-result:2", user_id="alice",
            text="done", client=_FakeClient(on_send=observe),
        )

        assert seen == ["pending"]
        row = _rows(config, "task-result:2")[0]
        assert row["claimed_at"] and row["attempted_at"]

    async def test_a_raise_between_the_claim_and_the_send_settles_the_row(
        self, tmp_path, monkeypatch,
    ):
        """The stuck-row failure the claim makes possible.

        `claimed_at` is committed before anything reaches the network, so from
        that instant every later call reads the row as settled and returns
        without sending. A raise in between would leave the answer lost with
        no row saying so and no alert — worse than any state the ledger can
        record, because nothing anywhere knows it happened.
        """
        config = _config(tmp_path)
        _bind(config)

        def explode(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(
            "istota.transport.whatsapp.outbound.current_destination", explode,
        )
        client = _FakeClient()

        record = await deliver_whatsapp(
            config, logical_key="task-result:20", user_id="alice",
            text="done", client=client,
        )

        # `failed`, not `unknown`: the binding read runs before the first byte,
        # so this is provably a message that never left — and `unknown` is the
        # one state that says it may have and that nobody can resolve.
        assert record.status == "failed"
        assert client.requests == []
        assert _alerts(config)

    async def test_a_cancellation_mid_send_still_settles_the_row(
        self, tmp_path,
    ):
        # `deliver_event_responses` runs as a FastAPI background task and
        # `send_record` runs inside `run_coro`, so a shutdown delivers
        # `CancelledError` — which is not an `Exception` and would otherwise
        # leave the claimed row stuck on the one path where the send may
        # already be on the wire.
        config = _config(tmp_path)
        _bind(config)

        async def cancelled(_request):
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await deliver_whatsapp(
                config, logical_key="task-result:21", user_id="alice",
                text="done", client=_FakeClient(on_send=cancelled),
            )

        assert [row["status"] for row in _rows(config)] == ["unknown"]

    async def test_a_duplicate_meta_message_id_settles_unknown(self, tmp_path):
        # `sent_whatsapp` has UNIQUE(meta_message_id). Nothing here resends, so
        # a collision is a Meta or a clock anomaly rather than our doing, and
        # the honest record is that we do not know what became of this one.
        config = _config(tmp_path)
        _bind(config)
        await deliver_whatsapp(
            config, logical_key="task-result:22", user_id="alice", text="a",
            client=_FakeClient(WhatsAppSendResult("wamid.same")),
        )

        record = await deliver_whatsapp(
            config, logical_key="task-result:23", user_id="alice", text="b",
            client=_FakeClient(WhatsAppSendResult("wamid.same")),
        )

        assert record.status == "unknown"
        assert record.meta_message_id is None
        assert _rows(config, "task-result:22")[0]["status"] == "accepted"

    async def test_the_ledger_stores_no_body_and_no_destination(self, tmp_path):
        config = _config(tmp_path)
        _bind(config)

        await deliver_whatsapp(
            config, logical_key="task-result:3", user_id="alice",
            text="the private answer", client=_FakeClient(),
        )

        row = _rows(config, "task-result:3")[0]
        stored = " ".join(str(value) for value in tuple(row))
        assert "the private answer" not in stored
        assert USER_BSUID not in stored and USER_NUMBER not in stored
        assert row["body_chars"] == len("the private answer")
        assert len(row["body_sha256"]) == 64


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


class TestTheSend:
    async def test_an_accepted_send_records_metas_message_id(self, tmp_path):
        config = _config(tmp_path)
        _bind(config)
        client = _FakeClient(WhatsAppSendResult("wamid.abc"))

        record = await deliver_whatsapp(
            config, logical_key="task-result:1", user_id="alice",
            text="done", task_id=None, client=client,
        )

        assert record.status == "accepted"
        assert record.meta_message_id == "wamid.abc"
        assert _rows(config, "task-result:1")[0]["meta_message_id"] == "wamid.abc"

    async def test_the_destination_is_the_binding_read_immediately_before_the_call(
        self, tmp_path,
    ):
        config = _config(tmp_path)
        _bind(config)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "UPDATE whatsapp_user_bindings SET send_id = 'US.newdestination' "
                "WHERE user_id = 'alice'"
            )
        client = _FakeClient()

        await deliver_whatsapp(
            config, logical_key="task-result:1", user_id="alice",
            text="done", client=client,
        )

        assert client.requests[0].to == "US.newdestination"
        assert client.requests[0].kind == "service"

    async def test_a_binding_with_no_send_id_falls_back_to_the_bootstrap_number(
        self, tmp_path,
    ):
        config = _config(tmp_path)
        _bind(config, bsuid="", send_id="")
        client = _FakeClient()

        await deliver_whatsapp(
            config, logical_key="task-result:1", user_id="alice",
            text="done", client=client,
        )

        assert client.requests[0].to == USER_NUMBER

    async def test_a_definite_rejection_fails_the_row_and_alerts_off_whatsapp(
        self, tmp_path,
    ):
        config = _config(tmp_path)
        _bind(config)
        client = _FakeClient(WhatsAppSendFailure(True, "131047", "meta rejected"))

        record = await deliver_whatsapp(
            config, logical_key="task-result:5", user_id="alice",
            text="done", task_id=None, client=client,
        )

        assert record.status == "failed"
        assert record.error_code == "131047"
        assert [row["title"] for row in _alerts(config)] == [
            "WhatsApp delivery failed — a notification"
        ]

    async def test_an_ambiguous_outcome_is_unknown_and_is_never_resent(
        self, tmp_path,
    ):
        config = _config(tmp_path)
        _bind(config)
        client = _FakeClient(WhatsAppSendFailure(False, None, "delivery outcome unknown"))

        record = await deliver_whatsapp(
            config, logical_key="task-result:6", user_id="alice",
            text="done", client=client,
        )
        again = await deliver_whatsapp(
            config, logical_key="task-result:6", user_id="alice",
            text="done", client=client,
        )

        assert record.status == "unknown"
        assert again.status == "unknown"
        assert len(client.requests) == 1

    async def test_a_send_that_raises_is_ambiguous_rather_than_failed(
        self, tmp_path,
    ):
        config = _config(tmp_path)
        _bind(config)

        async def explode(_request):
            raise RuntimeError("boom")

        record = await deliver_whatsapp(
            config, logical_key="task-result:7", user_id="alice",
            text="done", client=_FakeClient(on_send=explode),
        )

        assert record.status == "unknown"

    async def test_an_opted_out_binding_blocks_the_send(self, tmp_path):
        config = _config(tmp_path)
        _bind(config)
        with db.get_db(config.db_path) as conn:
            db.set_whatsapp_opt_out(conn, "alice", True)
        client = _FakeClient()

        record = await deliver_whatsapp(
            config, logical_key="task-result:8", user_id="alice",
            text="done", client=client,
        )

        assert record.status == "opted_out"
        assert client.requests == []
        # Alerted, and off WhatsApp. A confirmation prompt blocked here has to
        # reach the user somewhere, or the task parks until it expires.
        assert [row["title"] for row in _alerts(config)] == [
            "WhatsApp delivery blocked by an opt-out — a notification"
        ]

    async def test_the_stop_acknowledgement_is_the_one_send_an_opt_out_allows(
        self, tmp_path,
    ):
        config = _config(tmp_path)
        _bind(config)
        with db.get_db(config.db_path) as conn:
            db.set_whatsapp_opt_out(conn, "alice", True)
        client = _FakeClient()

        record = await deliver_whatsapp(
            config, logical_key="opt-out:wamid.1", user_id="alice",
            text="You will get no further WhatsApp messages.",
            client=client, ignore_opt_out=True,
        )

        assert record.status == "accepted"
        assert len(client.requests) == 1

    async def test_a_user_with_no_binding_is_unconfigured(self, tmp_path):
        config = _config(tmp_path)
        client = _FakeClient()

        record = await deliver_whatsapp(
            config, logical_key="task-result:10", user_id="alice",
            text="done", client=client,
        )

        assert record.status == "unconfigured"
        assert client.requests == []
        assert _alerts(config)

    async def test_a_closed_window_blocks_the_send_and_alerts_off_whatsapp(
        self, tmp_path,
    ):
        config = _config(tmp_path)
        _bind(config, window=timedelta(hours=30))
        client = _FakeClient()

        record = await deliver_whatsapp(
            config, logical_key="task-result:11", user_id="alice",
            text="done", task_id=None, client=client,
        )

        assert record.status == "window_closed"
        assert client.requests == []
        titles = [row["title"] for row in _alerts(config)]
        assert titles == ["WhatsApp delivery blocked by a closed service window "
                          "— a notification"]

    async def test_a_configured_template_does_not_reopen_the_window_yet(
        self, tmp_path,
    ):
        # The paid template path is stage four's. Until it lands, a closed
        # window blocks in `allow_paid` too — visibly, as `window_closed`, and
        # never by falling through to a free-form send Meta would either refuse
        # or price as a template. `free_guard` cannot reach this case at all:
        # a template under that policy fails the config load, which
        # `tests/test_whatsapp_core.py` holds.
        config = _config(tmp_path, billing_policy="allow_paid")
        config.whatsapp.proactive_template.enabled = True
        config.whatsapp.proactive_template.name = "istota_result"
        _bind(config, window=timedelta(hours=30))
        client = _FakeClient()

        record = await deliver_whatsapp(
            config, logical_key="task-result:12", user_id="alice",
            text="done", client=client,
        )

        assert record.status == "window_closed"
        assert client.requests == []

    async def test_an_open_billing_circuit_blocks_every_send(self, tmp_path):
        config = _config(tmp_path)
        _bind(config)
        with db.get_db(config.db_path) as conn:
            db.block_whatsapp_billing(conn, "wamid.billable")
        client = _FakeClient()

        record = await deliver_whatsapp(
            config, logical_key="task-result:13", user_id="alice",
            text="done", client=client,
        )

        assert record.status == "billing_blocked"
        assert client.requests == []

    async def test_a_disabled_transport_records_unconfigured_without_calling(
        self, tmp_path,
    ):
        config = _config(tmp_path, enabled=False)
        _bind(config)
        client = _FakeClient()

        record = await deliver_whatsapp(
            config, logical_key="task-result:14", user_id="alice",
            text="done", client=client,
        )

        assert record.status == "unconfigured"
        assert client.requests == []

    async def test_the_body_reaching_meta_is_the_rendered_one(self, tmp_path):
        config = _config(tmp_path)
        _bind(config)
        client = _FakeClient()

        await deliver_whatsapp(
            config, logical_key="task-result:15", user_id="alice",
            text="```\nplain\n```", client=client,
        )

        assert client.requests[0].text == "plain"

    async def test_confirmation_buttons_reach_the_adapter_as_plain_data(
        self, tmp_path,
    ):
        config = _config(tmp_path)
        _bind(config)
        client = _FakeClient()

        await deliver_whatsapp(
            config, logical_key="confirmation:1", user_id="alice",
            text="Proceed?", client=client,
            buttons=(("confirm:7:yes", "Yes"), ("confirm:7:no", "No")),
        )

        assert client.requests[0].buttons == (
            ("confirm:7:yes", "Yes"), ("confirm:7:no", "No"),
        )

    async def test_a_message_with_buttons_is_capped_at_the_interactive_limit(
        self, tmp_path,
    ):
        """The limit that is not 4,096.

        A message carrying quick-reply buttons is an `interactive` Cloud API
        object, whose body caps at 1,024 characters. Rendering a confirmation
        prompt at the plain-text limit means Meta refuses every question longer
        than that with a 4xx — which `_classify` reads as definite, so the row
        goes `failed`, the question is asked nowhere, and the task parks until
        it expires. The model's answer is unbounded, so it is the ordinary case.
        """
        config = _config(tmp_path)
        _bind(config)
        client = _FakeClient()

        await deliver_whatsapp(
            config, logical_key="confirmation:2", user_id="alice",
            text="q" * 5000, client=client,
            buttons=(("confirm:7:yes", "Yes"),),
        )
        await deliver_whatsapp(
            config, logical_key="task-result:2", user_id="alice",
            text="q" * 5000, client=client,
        )

        with_buttons, without = client.requests
        assert len(with_buttons.text) <= 1024
        assert 1024 < len(without.text) <= WHATSAPP_TEXT_LIMIT


# ---------------------------------------------------------------------------
# Delivery and status callbacks
# ---------------------------------------------------------------------------


def _delivery(message_id="wamid.abc", status="sent", **overrides):
    fields = dict(
        message_id=message_id,
        waba_id=WABA_ID,
        phone_number_id=PHONE_NUMBER_ID,
        recipient_id="15551234567",
        status=status,
        occurred_at=datetime.now(timezone.utc),
        error_code=None,
        billable=None,
        pricing_model=None,
        pricing_category=None,
        pricing_type=None,
    )
    fields.update(overrides)
    return WhatsAppDeliveryEvent(**fields)


async def _accepted_row(config, logical_key="task-result:1", meta_id="wamid.abc"):
    _bind(config)
    await deliver_whatsapp(
        config, logical_key=logical_key, user_id="alice", text="done",
        client=_FakeClient(WhatsAppSendResult(meta_id)),
    )


class TestDeliveryStates:
    async def test_the_ladder_advances_and_never_regresses(self, tmp_path):
        config = _config(tmp_path)
        await _accepted_row(config)

        seen = []
        with db.get_db(config.db_path) as conn:
            for status in ("sent", "delivered", "read", "delivered", "sent"):
                disposition, record, _alert = apply_delivery_event(
                    conn, _delivery(status=status),
                )
                seen.append((disposition, record.status))

        assert seen == [
            ("delivery_updated", "sent"),
            ("delivery_updated", "delivered"),
            ("delivery_updated", "read"),
            ("delivery_stale", "read"),
            ("delivery_stale", "read"),
        ]

    async def test_read_before_delivered_wins(self, tmp_path):
        config = _config(tmp_path)
        await _accepted_row(config)

        with db.get_db(config.db_path) as conn:
            apply_delivery_event(conn, _delivery(status="read"))
            disposition, record, _alert = apply_delivery_event(
                conn, _delivery(status="delivered"),
            )

        assert disposition == "delivery_stale"
        assert record.status == "read"

    async def test_failed_is_terminal_and_raises_one_alert(self, tmp_path):
        config = _config(tmp_path)
        await _accepted_row(config)

        with db.get_db(config.db_path) as conn:
            apply_delivery_event(conn, _delivery(status="sent"))
            failed, record, alert = apply_delivery_event(
                conn, _delivery(status="failed", error_code="131026"),
            )
            after, later, second_alert = apply_delivery_event(
                conn, _delivery(status="delivered"),
            )

        assert failed == "delivery_updated"
        assert record.status == "failed" and record.error_code == "131026"
        assert alert is not None
        assert after == "delivery_duplicate" and later.status == "failed"
        assert second_alert is None

    async def test_an_unknown_message_id_is_acknowledged(self, tmp_path, caplog):
        config = _config(tmp_path)
        _bind(config)

        with caplog.at_level("WARNING"), db.get_db(config.db_path) as conn:
            disposition, record, alert = apply_delivery_event(
                conn, _delivery(message_id="wamid.nothing"),
            )

        assert (disposition, record, alert) == ("delivery_unknown", None, None)
        assert "wamid.nothing" not in caplog.text

    async def test_a_status_never_reaches_a_local_terminal_row(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, window=timedelta(hours=30))
        await deliver_whatsapp(
            config, logical_key="task-result:1", user_id="alice", text="done",
            client=_FakeClient(),
        )
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "UPDATE sent_whatsapp SET meta_message_id = 'wamid.abc' "
                "WHERE logical_key = 'task-result:1'"
            )
            disposition, record, _alert = apply_delivery_event(
                conn, _delivery(status="delivered"),
            )

        assert disposition == "delivery_duplicate"
        assert record.status == "window_closed"

    async def test_the_webhook_batch_applies_a_status(self, tmp_path):
        from istota.transport.whatsapp.webhook import handle_whatsapp_batch

        config = _config(tmp_path)
        await _accepted_row(config)

        with db.get_db(config.db_path) as conn:
            results = handle_whatsapp_batch(conn, config, [_delivery(status="sent")])

        assert [r.disposition for r in results] == ["delivery_updated"]
        assert _rows(config, "task-result:1")[0]["status"] == "sent"

    async def test_stage_three_observes_no_pricing(self, tmp_path):
        # Pricing observation and the billable circuit are stage four's. This
        # states what stage three leaves untouched so the later change is a
        # visible diff rather than an accident.
        config = _config(tmp_path)
        await _accepted_row(config)

        with db.get_db(config.db_path) as conn:
            apply_delivery_event(
                conn, _delivery(status="sent", billable=True, pricing_category="utility"),
            )

        row = _rows(config, "task-result:1")[0]
        assert row["billable"] is None
        assert row["pricing_category"] is None


# ---------------------------------------------------------------------------
# Surface, registry and routing
# ---------------------------------------------------------------------------


class TestTheSurface:
    def test_the_capabilities_are_the_declared_ones(self, tmp_path):
        transport = WhatsAppTransport(_config(tmp_path))
        capabilities = transport.capabilities

        assert transport.name == "whatsapp"
        assert capabilities.supports_edit is False
        assert capabilities.supports_threading is False
        assert capabilities.supports_progress_ack is False
        assert capabilities.supports_typing is False
        assert capabilities.max_message_length is None
        assert capabilities.surface_class == "push"
        assert capabilities.user_routable is True
        assert capabilities.room_view is None
        assert capabilities.inbound_room_role is None
        assert capabilities.user_turn_mirror is None

    def test_the_room_facts_are_all_absent(self):
        assert surfaces.room_role("whatsapp") is None
        assert surfaces.room_view("whatsapp") is None
        assert surfaces.user_turn_mirror("whatsapp") is None
        assert surfaces.is_room_member("whatsapp") is False
        assert surfaces.is_room_view("whatsapp") is False
        assert surfaces.origin_surface_for_source_type("whatsapp") == "whatsapp"

    def test_the_transport_is_registered_only_when_enabled(self, tmp_path):
        enabled = _config(tmp_path)
        disabled = _config(tmp_path, enabled=False)

        assert make_registry(enabled).get("whatsapp") is not None
        assert make_registry(disabled).get("whatsapp") is None
        assert "whatsapp" in make_registry(enabled).routable_names()

    def test_the_legacy_aliases_gain_nothing(self):
        assert parse_output_target("both") == [
            Destination("talk"), Destination("email"),
        ]
        assert parse_output_target("all") == [
            Destination("talk"), Destination("email"), Destination("ntfy"),
        ]
        assert parse_output_target("all,whatsapp") == [
            Destination("talk"), Destination("email"), Destination("ntfy"),
            Destination("whatsapp"),
        ]

    def test_a_bare_route_resolves_the_current_binding_and_an_explicit_one_does_not(
        self, tmp_path,
    ):
        config = _config(tmp_path)
        _bind(config)
        registry = make_registry(config)
        task = db.Task(1, "completed", "whatsapp", "alice", "hi")
        token = whatsapp_conversation_token("alice")

        assert resolve_delivery_plan(config, task, registry) == [
            Destination("whatsapp", token, "push")
        ]
        assert resolve_delivery_plan(
            config, replace(task, output_target="whatsapp:+15559999999"), registry,
        ) == [Destination("whatsapp", token, "push")]

    def test_the_origin_descriptor_is_the_bare_surface(self):
        task = db.Task(1, "completed", "whatsapp", "alice", "hi")

        assert origin_descriptor(task) == "whatsapp"

    def test_an_unresolvable_binding_keeps_the_destination(self, tmp_path):
        # Dropping it empties the plan, which discards a finished answer and
        # completes a WhatsApp-origin confirmation instead of parking it.
        config = _config(tmp_path)
        registry = make_registry(config)
        task = db.Task(1, "completed", "whatsapp", "alice", "hi", output_target="missing")

        plan = resolve_delivery_plan(config, task, registry)

        assert [d.surface for d in plan] == ["whatsapp"]

    def test_the_target_carries_no_meta_identifier(self, tmp_path):
        config = _config(tmp_path)
        _bind(config)
        transport = WhatsAppTransport(config)
        task = db.Task(1, "completed", "whatsapp", "alice", "hi")

        target = transport.resolve_target(task)

        assert target == whatsapp_conversation_token("alice")
        assert USER_BSUID not in (target or "")
        assert "1555" not in (target or "")


class TestConfiguredProbe:
    def test_a_bound_user_inside_the_window_is_configured(self, tmp_path):
        config = _config(tmp_path)
        _bind(config)

        assert is_whatsapp_configured(config, "alice") is True
        assert notifications.is_channel_configured(config, "alice", "whatsapp")

    @pytest.mark.parametrize("reason", ["disabled", "unbound", "opted_out", "blocked"])
    def test_every_gate_makes_the_user_unconfigured(self, tmp_path, reason):
        config = _config(tmp_path, enabled=reason != "disabled")
        if reason != "unbound":
            _bind(config)
        if reason == "opted_out":
            with db.get_db(config.db_path) as conn:
                db.set_whatsapp_opt_out(conn, "alice", True)
        if reason == "blocked":
            with db.get_db(config.db_path) as conn:
                db.block_whatsapp_billing(conn, "wamid.billable")

        assert is_whatsapp_configured(config, "alice") is False

    def test_a_closed_window_still_counts_as_configured(self, tmp_path):
        # The window is a per-send policy, not an enrollment fact: a user whose
        # window has closed is still routable, and the send records
        # `window_closed` and alerts. Answering False here would make
        # `heartbeat` skip the alert entirely.
        config = _config(tmp_path)
        _bind(config, window=timedelta(hours=30))

        assert is_whatsapp_configured(config, "alice") is True


class TestTheAfterCommitReplies:
    """What a committed batch owes the sender, sent through the same ledger.

    Stage two computed these and delivered them nowhere; the seam is
    `deliver_event_responses`, which runs in the route's background task after
    the 200 has gone out. The payload builders come from the webhook suite
    rather than being written twice — the wire shapes are that file's subject.
    """

    @staticmethod
    def _run(config, payload, client, monkeypatch):
        from istota.transport.whatsapp.webhook import (
            deliver_event_responses,
            handle_whatsapp_batch,
            normalize_payload,
        )

        monkeypatch.setattr(
            "istota.transport.whatsapp.client.make_client", lambda _config: client,
        )
        events = normalize_payload(config, payload)
        with db.get_db(config.db_path) as conn:
            results = handle_whatsapp_batch(conn, config, events)
        return results, deliver_event_responses(config, results)

    async def test_help_is_answered_through_the_ledger(self, tmp_path, monkeypatch):
        from tests.test_whatsapp_webhook import (
            _contact, _payload, _text_message, _value,
        )

        config = _config(tmp_path)
        _bind(config)
        client = _FakeClient()
        payload = _payload(_value(
            contacts=[_contact()],
            messages=[_text_message(message_id="wamid.help", text="HELP")],
        ))

        results, pending = self._run(config, payload, client, monkeypatch)
        await pending

        assert [r.disposition for r in results] == ["help"]
        assert len(client.requests) == 1
        assert "Send STOP" in client.requests[0].text
        assert _rows(config, "help:wamid.help")[0]["status"] == "accepted"

    async def test_the_stop_acknowledgement_survives_its_own_opt_out(
        self, tmp_path, monkeypatch,
    ):
        # The order is what makes this worth a test: the opt-out is written in
        # the same transaction, so by the time the ack is sent the binding it
        # is addressed to is already refusing sends.
        from tests.test_whatsapp_webhook import (
            _contact, _payload, _text_message, _value,
        )

        config = _config(tmp_path)
        _bind(config)
        client = _FakeClient()
        payload = _payload(_value(
            contacts=[_contact()],
            messages=[_text_message(message_id="wamid.stop", text="STOP")],
        ))

        _results, pending = self._run(config, payload, client, monkeypatch)
        await pending

        assert len(client.requests) == 1
        assert _rows(config, "opt-out:wamid.stop")[0]["status"] == "accepted"
        with db.get_db(config.db_path) as conn:
            assert db.get_whatsapp_binding(conn, "alice").opted_out_at is not None

    async def test_an_unsupported_type_gets_one_fixed_reply(
        self, tmp_path, monkeypatch,
    ):
        from tests.test_whatsapp_webhook import _contact, _payload, _value

        config = _config(tmp_path)
        _bind(config)
        client = _FakeClient()
        image = {
            "id": "wamid.img",
            "from": USER_BSUID,
            "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
            "type": "image",
            "image": {"id": "media-1", "caption": "what is this"},
        }
        payload = _payload(_value(contacts=[_contact()], messages=[image]))

        _results, pending = self._run(config, payload, client, monkeypatch)
        await pending

        assert len(client.requests) == 1
        assert "not supported yet" in client.requests[0].text
        # The caption is not treated as a request, and nothing downloads media.
        with db.get_db(config.db_path) as conn:
            assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0

    async def test_a_redelivered_batch_sends_nothing_a_second_time(
        self, tmp_path, monkeypatch,
    ):
        from tests.test_whatsapp_webhook import (
            _contact, _payload, _text_message, _value,
        )

        config = _config(tmp_path)
        _bind(config)
        client = _FakeClient()
        payload = _payload(_value(
            contacts=[_contact()],
            messages=[_text_message(message_id="wamid.help", text="HELP")],
        ))

        _first, pending = self._run(config, payload, client, monkeypatch)
        await pending
        second, pending_again = self._run(config, payload, client, monkeypatch)
        await pending_again

        assert [r.disposition for r in second] == ["duplicate"]
        assert len(client.requests) == 1
        assert len(_rows(config)) == 1

    async def test_a_command_reply_is_sent_exactly_once(self, tmp_path, monkeypatch):
        """The double-send this surface is one accident away from.

        `commands.dispatch` pushes its own result through the registry for any
        **push** transport, and WhatsApp is one — so a `!command` could be
        delivered by `_deliver_result` *and* again by the ledger under
        `command:<message id>`, as two separately billed messages. What stops
        it is that `WhatsAppTransport.send_record` resolves the user from the
        task alone: `dispatch` passes the conversation token as the target and
        no task, so nobody is resolved and nothing is sent. That is structural
        — the token is a one-way hash of the user id and cannot be turned back
        into one — but it is invisible at either call site, so it is pinned.
        """
        from tests.test_whatsapp_webhook import (
            _contact, _payload, _text_message, _value,
        )

        config = _config(tmp_path)
        _bind(config)
        client = _FakeClient()
        payload = _payload(_value(
            contacts=[_contact()],
            messages=[_text_message(message_id="wamid.cmd", text="!help")],
        ))

        results, pending = self._run(config, payload, client, monkeypatch)
        await pending

        assert [r.disposition for r in results] == ["command"]
        assert len(client.requests) == 1
        assert [row["logical_key"] for row in _rows(config)] == ["command:wamid.cmd"]

    async def test_an_ordinary_task_owes_no_immediate_reply(
        self, tmp_path, monkeypatch,
    ):
        # The answer arrives when the task finishes, through the scheduler's
        # own leg. A reply here would be a second billed message saying nothing.
        from tests.test_whatsapp_webhook import (
            _contact, _payload, _text_message, _value,
        )

        config = _config(tmp_path)
        _bind(config)
        client = _FakeClient()
        payload = _payload(_value(
            contacts=[_contact()],
            messages=[_text_message(message_id="wamid.task", text="check backups")],
        ))

        results, pending = self._run(config, payload, client, monkeypatch)
        await pending

        assert [r.disposition for r in results] == ["task"]
        assert client.requests == []
        assert _rows(config) == []


class TestSchedulerDelivery:
    """The scheduler's own leg, driven through `process_one_task`.

    The two arms that had no coverage anywhere else: a completed WhatsApp task
    delivering its result once through the ledger, and a WhatsApp-origin
    confirmation parking with a prompt carrying Yes and No buttons. Both go
    through `send_record` rather than `deliver`, because `Transport.deliver`
    returns a message id WhatsApp has none of and would discard the record the
    owed-confirmation arm reads.
    """

    @staticmethod
    def _task(config, monkeypatch, client, answer):
        monkeypatch.setattr(
            "istota.transport.whatsapp.client.make_client", lambda _config: client,
        )
        monkeypatch.setattr(
            "istota.scheduler.execute_task",
            lambda *_args, **_kwargs: (True, answer, None, None),
        )
        with db.get_db(config.db_path) as conn:
            return db.create_task(
                conn, prompt="check", user_id="alice", source_type="whatsapp",
                conversation_token=whatsapp_conversation_token("alice"),
                output_target="whatsapp",
            )

    def test_a_completed_task_delivers_once_through_the_ledger(
        self, tmp_path, monkeypatch,
    ):
        from istota.scheduler import process_one_task

        config = _config(tmp_path)
        _bind(config)
        client = _FakeClient()
        task_id = self._task(config, monkeypatch, client, "Finished the check.")

        assert process_one_task(config) == (task_id, True)

        assert [r.text for r in client.requests] == ["Finished the check."]
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_id).status == "completed"
            # No room, no membership, no canonical transcript row: WhatsApp is
            # its own external conversation and is never a view of a room.
            for table in ("rooms", "room_bindings", "room_members", "messages"):
                assert conn.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0] == 0
        assert [row["logical_key"] for row in _rows(config)] == [
            f"task-result:{task_id}"
        ]

    async def test_a_long_question_keeps_the_task_id_it_is_answered_at(
        self, tmp_path, monkeypatch,
    ):
        # The truncation the interactive limit forces has to cut the question,
        # never the sentence: the task id is the address `!confirm <id>` and a
        # typed YES resolve against, and the only route left on a client that
        # renders no buttons.
        from istota.scheduler import process_one_task

        config = _config(tmp_path)
        _bind(config)
        client = _FakeClient()
        task_id = self._task(
            config, monkeypatch, client,
            "Should I proceed? " + ("detail " * 400),
        )

        assert process_one_task(config) == (task_id, True)

        request = client.requests[0]
        assert len(request.text) <= 1024
        assert request.text.endswith(f"Task #{task_id}. Reply YES or NO.")

    def test_a_confirmation_parks_and_carries_yes_and_no_buttons(
        self, tmp_path, monkeypatch,
    ):
        from istota.scheduler import process_one_task

        config = _config(tmp_path)
        _bind(config)
        client = _FakeClient()
        task_id = self._task(
            config, monkeypatch, client,
            "I need your confirmation before deleting the file.",
        )

        assert process_one_task(config) == (task_id, True)

        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_id).status == "pending_confirmation"
            notification_id = conn.execute(
                "SELECT id FROM notifications WHERE source = 'confirmation'"
            ).fetchone()[0]
        assert len(client.requests) == 1
        request = client.requests[0]
        # The buttons carry the answer; the sentence carries the task id, which
        # is what makes `!confirm <id>` and a typed YES work on a client that
        # renders no buttons. The callback data holds the id and the choice and
        # nothing else — never an authorization secret.
        assert request.buttons == (
            (f"confirm:{task_id}:yes", "Yes"), (f"confirm:{task_id}:no", "No"),
        )
        assert f"Task #{task_id}. Reply YES or NO." in request.text
        assert len(request.text) <= 1024
        assert [row["logical_key"] for row in _rows(config)] == [
            f"confirmation:{notification_id}"
        ]

    def test_a_blocked_confirmation_still_reaches_the_user_off_whatsapp(
        self, tmp_path, monkeypatch,
    ):
        # The owed debt: the park withholds the notification because the
        # WhatsApp prompt was going to carry the question, so a send that never
        # reached Meta would leave it pushed nowhere and the task parked until
        # `expire_stale_confirmations` kills it two hours later.
        from istota.scheduler import process_one_task

        config = _config(tmp_path)
        _bind(config, window=timedelta(hours=30))
        client = _FakeClient()
        task_id = self._task(
            config, monkeypatch, client,
            "I need your confirmation before deleting the file.",
        )
        # The push itself is asserted on the call rather than on
        # `last_delivered_at`: this user has no configured destination, so a
        # real push reaches nobody and stamps nothing, and the question under
        # test is whether the scheduler owed it back at all.
        pushed = []
        monkeypatch.setattr(
            "istota.scheduler.deliver_pending",
            lambda _config, results: pushed.extend(
                r for r in results if r is not None
            ),
        )

        assert process_one_task(config) == (task_id, True)

        assert client.requests == []
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_id).status == "pending_confirmation"
            sources = [
                row[0] for row in conn.execute("SELECT source FROM notifications")
            ]
        assert "confirmation" in sources
        assert pushed, "the withheld confirmation was never owed back"
        # The blocked prompt is recorded, so a retry cannot send it twice.
        assert [row["status"] for row in _rows(config)] == ["window_closed"]


class TestTheNotificationLeg:
    async def test_a_notification_routed_to_whatsapp_goes_through_the_ledger(
        self, tmp_path, monkeypatch,
    ):
        config = _config(tmp_path)
        _bind(config)
        client = _FakeClient()
        monkeypatch.setattr(
            "istota.transport.whatsapp.client.make_client", lambda _config: client,
        )

        sent = await asyncio.to_thread(
            notifications.send_notification,
            config, "alice", "the alert body",
            surface="whatsapp", title="Alert", reference_id="alert:1",
        )

        assert sent is True
        assert len(client.requests) == 1
        assert _rows(config, "alert:1")[0]["status"] == "accepted"
