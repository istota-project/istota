"""Stage 1: the normalized usage vocabulary.

Pure logic — no DB, no subprocess. The numbers here come from one capture of a
two-turn Bash-using task against `claude` 2.1.227, run with the exact flag set
the daemon passes (`--output-format stream-json --verbose
--include-partial-messages`). They are asserted verbatim rather than recomputed,
because the point of most of these tests is which field the implementation read.
"""

import pytest

from istota.session.usage import TaskUsage
from istota.usage import (
    BrainUsage,
    ModelUsage,
    RequestUsage,
    cache_hit_rate,
    context_headroom_pct,
    cost_basis_from_api_key_source,
    from_cli_result,
    from_task_usage,
    total_prompt_tokens,
)

# The captured terminal `result` frame, identifiers scrubbed. `usage` and
# `modelUsage` disagree deliberately — see test_totals_come_from_model_usage.
CAPTURED_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "num_turns": 2,
    "duration_ms": 2916,
    "duration_api_ms": 3655,
    "ttft_ms": 1424,
    "stop_reason": "end_turn",
    "terminal_reason": "completed",
    "session_id": "test-session",
    "uuid": "test-uuid",
    "total_cost_usd": 0.0319275,
    "usage": {
        "input_tokens": 17,
        "cache_creation_input_tokens": 14565,
        "cache_read_input_tokens": 14425,
        "output_tokens": 147,
        "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
        "service_tier": "standard",
        "cache_creation": {
            "ephemeral_1h_input_tokens": 14565,
            "ephemeral_5m_input_tokens": 0,
        },
    },
    "modelUsage": {
        "claude-haiku-4-5-20251001": {
            "inputTokens": 550,
            "outputTokens": 161,
            "cacheReadInputTokens": 14425,
            "cacheCreationInputTokens": 14565,
            "webSearchRequests": 0,
            "costUSD": 0.0319275,
            "contextWindow": 200000,
            "maxOutputTokens": 32000,
            "canonicalModel": "claude-haiku-4-5",
            "provider": "firstParty",
        }
    },
    "permission_denials": [],
}

# The two main-agent requests, as measured off their `message_delta` frames.
CAPTURED_REQUESTS = [
    RequestUsage(prompt_tokens=14434, output_tokens=119),
    RequestUsage(prompt_tokens=14573, output_tokens=28),
]

# Published per-mtok rates for claude-haiku-4-5, used to settle which of the two
# token sources the CLI actually billed.
HAIKU_INPUT = 1.0
HAIKU_OUTPUT = 5.0
HAIKU_CACHE_READ = 0.10
HAIKU_CACHE_WRITE = 2.0


def _price(billed_input, output, cache_read, cache_write):
    return (
        billed_input / 1e6 * HAIKU_INPUT
        + output / 1e6 * HAIKU_OUTPUT
        + cache_read / 1e6 * HAIKU_CACHE_READ
        + cache_write / 1e6 * HAIKU_CACHE_WRITE
    )


def test_totals_come_from_model_usage_not_result_usage():
    """The billing-basis assertion. Fails against a `result.usage` implementation.

    `result.usage` reports 17 input / 147 output; `modelUsage` reports 550 / 161.
    The gap is the CLI's own out-of-band calls, which the run still paid for.
    """
    u = from_cli_result(CAPTURED_RESULT, CAPTURED_REQUESTS, "ANTHROPIC_API_KEY")

    assert u.billed_input_tokens == 550
    assert u.output_tokens == 161
    assert u.billed_input_tokens != 17
    assert u.output_tokens != 147

    assert u.cache_read_tokens == 14425
    assert u.cache_write_tokens == 14565


def test_pricing_from_model_usage_reproduces_reported_cost():
    """Pricing the run both ways is what proves which source is the billing basis."""
    u = from_cli_result(CAPTURED_RESULT, CAPTURED_REQUESTS, "ANTHROPIC_API_KEY")

    from_model_usage = _price(
        u.billed_input_tokens,
        u.output_tokens,
        u.cache_read_tokens,
        u.cache_write_tokens,
    )
    assert from_model_usage == pytest.approx(0.0319275, abs=1e-9)
    assert from_model_usage == pytest.approx(u.cost_usd, abs=1e-9)

    from_result_usage = _price(17, 147, 14425, 14565)
    assert from_result_usage != pytest.approx(0.0319275, abs=1e-9)


def test_parent_totals_equal_sum_of_children():
    """The invariant `--by model` depends on."""
    u = from_cli_result(CAPTURED_RESULT, CAPTURED_REQUESTS, "ANTHROPIC_API_KEY")

    assert len(u.models) == 1
    assert u.billed_input_tokens == sum(m.billed_input_tokens for m in u.models)
    assert u.output_tokens == sum(m.output_tokens for m in u.models)
    assert u.cache_read_tokens == sum(m.cache_read_tokens for m in u.models)
    assert u.cache_write_tokens == sum(m.cache_write_tokens for m in u.models)


def test_parent_totals_equal_sum_of_children_multi_model():
    frame = {
        "total_cost_usd": 0.5,
        "modelUsage": {
            "model-a": {
                "inputTokens": 100,
                "outputTokens": 10,
                "cacheReadInputTokens": 1000,
                "cacheCreationInputTokens": 50,
                "costUSD": 0.1,
                "contextWindow": 200000,
            },
            "model-b": {
                "inputTokens": 7,
                "outputTokens": 3,
                "cacheReadInputTokens": 20,
                "cacheCreationInputTokens": 5,
                "costUSD": 0.4,
                "contextWindow": 1000000,
            },
        },
    }
    u = from_cli_result(frame, [], None)

    assert u.billed_input_tokens == 107
    assert u.output_tokens == 13
    assert u.cache_read_tokens == 1020
    assert u.cache_write_tokens == 55
    assert u.billed_input_tokens == sum(m.billed_input_tokens for m in u.models)
    # Parent model is the largest cost share, not the largest token count.
    assert u.model == "model-b"
    # And the window is the largest any participating model offered.
    assert u.context_window == 1000000


def test_dominant_model_ties_break_deterministically():
    frame = {
        "modelUsage": {
            "zeta": {"inputTokens": 1, "outputTokens": 5, "costUSD": 0.2},
            "alpha": {"inputTokens": 1, "outputTokens": 5, "costUSD": 0.2},
        }
    }
    assert from_cli_result(frame, [], None).model == "zeta"


def test_context_measures_from_request_frames():
    u = from_cli_result(CAPTURED_RESULT, CAPTURED_REQUESTS, "ANTHROPIC_API_KEY")

    assert u.initial_context_tokens == 14434
    assert u.peak_context_tokens == 14573
    assert u.model_requests == 2
    assert u.context_window == 200000


def test_sum_and_peak_are_different_quantities():
    """A peak implemented as a sum passes every other context assertion but not this."""
    u = from_cli_result(CAPTURED_RESULT, CAPTURED_REQUESTS, "ANTHROPIC_API_KEY")

    assert total_prompt_tokens(u) == 29540
    assert u.peak_context_tokens == 14573
    assert total_prompt_tokens(u) != u.peak_context_tokens


def test_scalar_fields_from_result_frame():
    u = from_cli_result(CAPTURED_RESULT, CAPTURED_REQUESTS, "ANTHROPIC_API_KEY")

    assert u.turns == 2
    assert u.duration_ms == 2916
    assert u.duration_api_ms == 3655
    assert u.cost_usd == pytest.approx(0.0319275)
    assert u.service_tier == "standard"
    assert u.session_id == "test-session"
    assert u.has_totals is True
    assert u.totals_source == "model_usage"


def test_no_requests_leaves_context_null_not_zero():
    """NULL, not 0 — SQL AVG skips NULL, and a zero would halve a mixed average."""
    u = from_cli_result(CAPTURED_RESULT, [], "ANTHROPIC_API_KEY")

    assert u.initial_context_tokens is None
    assert u.peak_context_tokens is None
    assert u.model_requests == 0
    # Totals are unaffected by the absence of per-request frames.
    assert u.has_totals is True
    assert u.billed_input_tokens == 550


def test_absent_model_usage_leaves_has_totals_false():
    u = from_cli_result({"total_cost_usd": 0.0, "num_turns": 1}, CAPTURED_REQUESTS)

    assert u.has_totals is False
    assert u.totals_source == "unknown"
    assert u.models == []
    assert u.model == ""
    # Context is measured independently of totals.
    assert u.initial_context_tokens == 14434


def test_empty_model_usage_leaves_has_totals_false():
    u = from_cli_result({"modelUsage": {}}, [])

    assert u.has_totals is False
    assert u.models == []


@pytest.mark.parametrize(
    "frame",
    [
        {},
        None,
        {"usage": None},
        {"modelUsage": {}},
        {"modelUsage": None},
        {"modelUsage": {"m": {"inputTokens": "many"}}},
        {"modelUsage": {"m": None}},
        {"modelUsage": []},
        {"total_cost_usd": "free", "num_turns": None},
        {"usage": {"service_tier": 7}},
        {"session_id": 12},
    ],
)
def test_from_cli_result_raises_nothing(frame):
    """A renamed or retyped CLI field is a zero in one column, never an exception."""
    u = from_cli_result(frame, [], None)
    assert isinstance(u, BrainUsage)


def test_malformed_model_entry_yields_zeros_not_an_exception():
    u = from_cli_result({"modelUsage": {"m": {"inputTokens": "many"}}}, [])

    assert u.has_totals is True
    assert u.billed_input_tokens == 0
    assert u.models[0].model == "m"


def test_bools_are_not_counted_as_ints():
    u = from_cli_result({"modelUsage": {"m": {"inputTokens": True}}}, [])
    assert u.billed_input_tokens == 0


class TestFromTaskUsage:
    def test_billed_input_excludes_cache_reads(self):
        """OpenAI-compat prompt_tokens is inclusive; Anthropic's input is not."""
        u = from_task_usage(TaskUsage(input_tokens=1000, cache_read_tokens=800))

        assert u.billed_input_tokens == 200
        assert u.cache_read_tokens == 800

    def test_billed_input_clamps_at_zero(self):
        u = from_task_usage(TaskUsage(input_tokens=10, cache_read_tokens=800))

        assert u.billed_input_tokens == 0

    def test_derived_and_context_free(self):
        u = from_task_usage(TaskUsage(input_tokens=1000, cache_read_tokens=800))

        assert u.totals_source == "derived"
        assert u.has_totals is True
        assert u.initial_context_tokens is None
        assert u.peak_context_tokens is None
        assert u.context_window is None

    def test_carries_the_remaining_totals(self):
        u = from_task_usage(
            TaskUsage(
                input_tokens=1000,
                output_tokens=50,
                cache_read_tokens=800,
                cache_write_tokens=30,
                cost_usd=1.25,
                turns=3,
            )
        )

        assert u.output_tokens == 50
        assert u.cache_write_tokens == 30
        assert u.cost_usd == pytest.approx(1.25)
        assert u.turns == 3

    def test_provider_reported_cost_is_api(self):
        u = from_task_usage(TaskUsage(cost_usd=1.0), cost_reported=True)
        assert u.cost_basis == "api"

    def test_catalog_fallback_is_estimated(self):
        """The catalog prices an unknown model at zero; that is not real spend."""
        u = from_task_usage(TaskUsage(cost_usd=0.0), cost_reported=False)
        assert u.cost_basis == "estimated"

    def test_none_yields_an_empty_usage(self):
        u = from_task_usage(None)
        assert u.has_totals is False
        assert u.billed_input_tokens == 0


@pytest.mark.parametrize(
    "source,expected",
    [
        ("ANTHROPIC_API_KEY", "api"),
        ("apiKeyHelper", "api"),
        ("none", "subscription"),
        ("/login managed key", "subscription"),
        (None, "unknown"),
        ("something-new", "unknown"),
        ("", "unknown"),
    ],
)
def test_cost_basis_from_api_key_source(source, expected):
    """An unrecognized spelling is never guessed into `api`."""
    assert cost_basis_from_api_key_source(source) == expected


def test_cost_basis_flows_through_from_cli_result():
    assert from_cli_result(CAPTURED_RESULT, [], "none").cost_basis == "subscription"
    assert from_cli_result(CAPTURED_RESULT, [], None).cost_basis == "unknown"


class TestHelpers:
    def test_total_prompt_tokens(self):
        u = BrainUsage(
            billed_input_tokens=550, cache_read_tokens=14425, cache_write_tokens=14565
        )
        assert total_prompt_tokens(u) == 29540

    def test_cache_hit_rate(self):
        u = BrainUsage(billed_input_tokens=100, cache_read_tokens=300)
        assert cache_hit_rate(u) == pytest.approx(0.75)

    def test_cache_hit_rate_is_zero_when_no_prompt_tokens(self):
        assert cache_hit_rate(BrainUsage()) == 0.0

    def test_cache_hit_rate_stays_in_unit_interval(self):
        u = from_cli_result(CAPTURED_RESULT, CAPTURED_REQUESTS, "ANTHROPIC_API_KEY")
        assert 0.0 <= cache_hit_rate(u) <= 1.0

    def test_context_headroom_pct(self):
        u = BrainUsage(peak_context_tokens=50000, context_window=200000)
        assert context_headroom_pct(u) == pytest.approx(75.0)

    def test_context_headroom_none_without_a_window(self):
        assert context_headroom_pct(BrainUsage(peak_context_tokens=100)) is None
        assert (
            context_headroom_pct(
                BrainUsage(peak_context_tokens=100, context_window=0)
            )
            is None
        )

    def test_context_headroom_none_without_a_peak(self):
        assert context_headroom_pct(BrainUsage(context_window=200000)) is None

    def test_context_headroom_clamps_at_zero_when_over_window(self):
        u = BrainUsage(peak_context_tokens=250000, context_window=200000)
        assert context_headroom_pct(u) == 0.0


def test_model_usage_defaults():
    m = ModelUsage(model="m")
    assert m.billed_input_tokens == 0
    assert m.cost_usd == 0.0
