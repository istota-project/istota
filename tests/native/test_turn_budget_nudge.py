"""Turn-budget awareness nudge (ISSUE-187 defect 3).

The native loop injects an environment notice as a tool-bearing run approaches
``max_turns`` so the model paces itself and delivers a partial answer instead of
getting capped mid-plan. Fires once at ~``early_percent`` of the cap, then once
each as absolute steps-remaining crosses each ``remaining`` level.
"""

from pathlib import Path

from istota.brain import BrainRequest
from istota.brain.native import (
    NativeBrain,
    _pick_turn_budget_nudge,
    _turn_budget_nudge_message,
    _turns_left_by_clock,
)
from istota.config import NativeBrainConfig
from istota.llm.types import AssistantMessage, TextContent, ToolCallContent, Usage

from ._mock_provider import MockProvider


def _req(prompt: str, cwd: Path, tools: list[str] | None = None) -> BrainRequest:
    return BrainRequest(
        prompt=prompt,
        allowed_tools=tools if tools is not None else [],
        cwd=cwd,
        env={},
        timeout_seconds=30,
        model="claude-sonnet-4-6",
    )


def _brain(provider, **cfg) -> NativeBrain:
    config = NativeBrainConfig(model="claude-sonnet-4-6", **cfg)
    return NativeBrain(config, provider=provider)


def _nudge_texts(provider) -> list[str]:
    """Every injected budget-notice string that reached the wire, in order."""
    seen: list[str] = []
    for call in provider.calls:
        for msg in call["messages"]:
            for block in getattr(msg, "content", []):
                text = getattr(block, "text", "")
                if "step budget" in text or "steps remain" in text:
                    if text not in seen:
                        seen.append(text)
    return seen


class TestPickThreshold:
    def test_no_cap_never_fires(self):
        fired: set = set()
        assert _pick_turn_budget_nudge(5, 0, 50, [15, 5], fired) is None
        assert fired == set()

    def test_early_fires_at_half(self):
        fired: set = set()
        # 39 turns of an 80 cap = below half; nothing yet.
        assert _pick_turn_budget_nudge(39, 80, 50, [15, 5], fired) is None
        # 40/80 = half → the early reminder, ~40 remaining.
        out = _pick_turn_budget_nudge(40, 80, 50, [15, 5], fired)
        assert out == (40, "early")
        # Fired once — a later turn still under the next threshold is silent.
        assert _pick_turn_budget_nudge(50, 80, 50, [15, 5], fired) is None

    def test_late_levels_escalate(self):
        fired: set = set()
        _pick_turn_budget_nudge(40, 80, 50, [15, 5], fired)  # early
        # remaining 15 → wrap-up notice.
        assert _pick_turn_budget_nudge(65, 80, 50, [15, 5], fired) == (15, "late")
        # remaining 5 → urgent notice.
        assert _pick_turn_budget_nudge(75, 80, 50, [15, 5], fired) == (5, "late")
        # both late levels already fired.
        assert _pick_turn_budget_nudge(78, 80, 50, [15, 5], fired) is None

    def test_each_level_fires_once(self):
        fired: set = set()
        assert _pick_turn_budget_nudge(66, 80, 50, [15, 5], fired) == (14, "late")
        # Still <=15 next turn, but already fired → silent (no re-fire, e.g.
        # after a compaction the count is monotonic from new_messages).
        assert _pick_turn_budget_nudge(67, 80, 50, [15, 5], fired) is None

    def test_small_cap_collapses_to_most_urgent(self):
        # A tiny cap where early and a late level cross on the same turn: the
        # most urgent (fewest remaining) wins, and the less-urgent crossed
        # threshold is marked fired so it can't fire stale later.
        fired: set = set()
        out = _pick_turn_budget_nudge(5, 10, 50, [15, 5], fired)
        assert out == (5, "late")
        assert "early" in fired  # marked fired, won't fire a stale halfway later
        assert _pick_turn_budget_nudge(6, 10, 50, [15, 5], fired) is None


class TestMessageText:
    def test_early_message_is_shrinking_frame(self):
        msg = _turn_budget_nudge_message(40, "early")
        text = msg.content[0].text
        assert "~40 steps remaining" in text
        assert "not from the user" in text  # environment-metadata framing
        # Non-numeric anchoring guard: no "budget of N" allotment phrasing.
        assert "budget of" not in text

    def test_urgent_message_says_deliver_now(self):
        text = _turn_budget_nudge_message(5, "late").content[0].text
        assert "~5 steps" in text
        assert "now" in text.lower()

    def test_wrapup_message_between_levels(self):
        text = _turn_budget_nudge_message(15, "late").content[0].text
        assert "~15 steps" in text
        assert "wrap" in text.lower()


def _tool_turns(n: int) -> list[AssistantMessage]:
    # Distinct arguments per turn so the loop-detection backstop doesn't trip
    # before max_turns — we're exercising the turn-budget path, not loop detect.
    return [
        AssistantMessage(
            content=[
                ToolCallContent(
                    id=f"c{i}", name="Read", arguments={"file_path": f"file_{i}"}
                )
            ],
            usage=Usage(input_tokens=10, output_tokens=1),
            stop_reason="tool_use",
        )
        for i in range(n)
    ]


class TestIntegration:
    def test_nudge_reaches_the_wire_before_the_cap(self, tmp_path):
        provider = MockProvider(_tool_turns(20))
        req = _req("go", tmp_path, tools=["Read"])
        _brain(
            provider,
            max_turns=10,
            turn_budget_nudge_early_percent=50,
            turn_budget_nudge_remaining=[3],
        ).execute(req)
        texts = _nudge_texts(provider)
        # Early (~5 remaining at turn 5) and the late level (~3 remaining) both
        # surfaced during the run.
        assert any("halfway" in t for t in texts), texts
        assert any("~3 steps" in t for t in texts), texts

    def test_disabled_injects_nothing(self, tmp_path):
        provider = MockProvider(_tool_turns(20))
        req = _req("go", tmp_path, tools=["Read"])
        _brain(provider, max_turns=6, turn_budget_nudge=False).execute(req)
        assert _nudge_texts(provider) == []

    def test_text_only_run_gets_no_nudge(self, tmp_path):
        # No tools → not an agentic task; the nudge machinery stays off even
        # though max_turns is set (matches the sleep-cycle text-only path).
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="done")], stop_reason="end_turn")]
        )
        req = _req("summarize", tmp_path)  # no tools
        _brain(provider, max_turns=2).execute(req)
        assert _nudge_texts(provider) == []

    def test_nudge_does_not_change_turn_count_or_stop_reason(self, tmp_path):
        # Injecting nudges must not perturb the max_turns accounting: a capped
        # run still stops at the cap with the real stop_reason.
        provider = MockProvider(_tool_turns(20))
        req = _req("go", tmp_path, tools=["Read"])
        result = _brain(provider, max_turns=4).execute(req)
        assert result.stop_reason == "max_turns"
        assert len(provider.calls) <= 5  # cap honored despite injected notices


class TestUpfrontSystemPrompt:
    def test_pacing_line_present_for_tool_task(self, tmp_path):
        provider = MockProvider(_tool_turns(2))
        req = _req("go", tmp_path, tools=["Read"])
        _brain(provider, max_turns=10).execute(req)
        sys_prompt = provider.calls[0]["system_prompt"]
        assert "mid-stream" in sys_prompt
        # Non-numeric: the upfront line must not state the cap value.
        assert "10" not in sys_prompt.split("mid-stream")[0][-200:]

    def test_no_pacing_line_when_disabled(self, tmp_path):
        provider = MockProvider(_tool_turns(2))
        req = _req("go", tmp_path, tools=["Read"])
        _brain(provider, max_turns=10, turn_budget_nudge=False).execute(req)
        assert "mid-stream" not in provider.calls[0]["system_prompt"]

    def test_no_pacing_line_for_text_only(self, tmp_path):
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="hi")], stop_reason="end_turn")]
        )
        _brain(provider, max_turns=10).execute(_req("hi", tmp_path))
        assert "mid-stream" not in provider.calls[0]["system_prompt"]


class TestClockAwareBudget:
    """ISSUE-373: on a slow brain the wall clock, not the cap, ends the run.

    The ladder counts turns, so with the shipped numbers it fires at turns 50,
    85 and 95 of a 100-turn cap. Which of those a run reaches depends entirely
    on how long a turn takes — at 60s/turn a 60-minute clock lands near turn 60
    and only the halfway reminder ever fires. Converting the time budget into a
    turn budget and running the ladder against whichever is scarcer is what
    makes the notices reachable.
    """

    def test_no_samples_means_no_estimate(self):
        assert _turns_left_by_clock(600.0, []) is None

    def test_one_sample_is_not_a_pace(self):
        # A single slow first turn (cold connection, large prompt) must not
        # fire the urgent notices on turn 2 of a run with ten minutes left.
        assert _turns_left_by_clock(600.0, [120.0]) is None

    def test_estimate_is_the_rolling_median(self):
        assert _turns_left_by_clock(100.0, [10.0, 10.0, 10.0]) == 10
        assert _turns_left_by_clock(100.0, [20.0, 20.0, 20.0]) == 5

    def test_one_slow_turn_does_not_collapse_the_budget(self):
        # Turn latency is heavy-tailed: one `npm install` or one full test run
        # is minutes where its neighbours are seconds. A mean lets that single
        # sample set the budget — four 10s turns and one 400s build average to
        # 88s, so a 22-minute remainder reads as 15 turns and spends the whole
        # ladder on a spike, telling the model "~15 steps remain" about a run
        # that then continues for 70 more. The thresholds are marked fired when
        # crossed, so nothing fires when the real crossing arrives.
        spiky = [10.0, 10.0, 400.0, 10.0, 10.0]
        assert _turns_left_by_clock(1320.0, spiky) == 132

    def test_a_genuinely_slow_window_still_collapses_it(self):
        # Three of the last five turns slow is a pace, not an outlier.
        assert _turns_left_by_clock(1320.0, [400.0, 10.0, 400.0, 10.0, 400.0]) == 3

    def test_a_spike_does_not_spend_the_ladder(self):
        # The same worked example through the ladder: the spike must leave both
        # thresholds unfired so the genuine crossing at turn 85 still lands.
        fired: set = set()
        spike_budget = _turns_left_by_clock(1320.0, [10.0, 10.0, 400.0, 10.0, 10.0])
        assert _pick_turn_budget_nudge(15, 100, 50, [15, 5], fired, spike_budget) is None
        assert fired == set()

    def test_no_deadline_means_no_estimate(self):
        assert _turns_left_by_clock(None, [10.0, 10.0, 10.0]) is None

    def test_expired_clock_estimates_zero(self):
        assert _turns_left_by_clock(-5.0, [10.0, 10.0, 10.0]) == 0

    def test_clock_budget_wins_when_scarcer(self):
        # Turn 20 of a 100-turn cap: 80 turns remain on the cap, but only 4 on
        # the clock. The urgent notice must fire now, not 60 turns from now.
        fired: set = set()
        picked = _pick_turn_budget_nudge(20, 100, 50, [15, 5], fired, 4)
        assert picked == (4, "late")

    def test_turn_budget_still_wins_when_it_is_scarcer(self):
        # Plenty of clock, nearly out of turns: the original ladder, unchanged.
        fired: set = set()
        picked = _pick_turn_budget_nudge(97, 100, 50, [15, 5], fired, 500)
        assert picked == (3, "late")

    def test_early_reminder_is_measured_against_the_effective_budget(self):
        # 30 turns in, 30 more on the clock: halfway through the run that will
        # actually happen, even though it is under a third of the turn cap. The
        # turn ladder alone would say nothing until turn 50.
        fired: set = set()
        assert _pick_turn_budget_nudge(30, 100, 50, [15, 5], fired) is None
        picked = _pick_turn_budget_nudge(30, 100, 50, [15, 5], set(), 30)
        assert picked == (30, "early")

    def test_a_threshold_crossed_on_the_clock_does_not_refire_on_turns(self):
        fired: set = set()
        assert _pick_turn_budget_nudge(20, 100, 50, [15, 5], fired, 12) == (12, "late")
        # Later, with the clock no longer the constraint, turn 88 crosses the
        # same level-15 threshold. It has already been spent.
        assert _pick_turn_budget_nudge(88, 100, 50, [15, 5], fired, 500) is None

    def test_an_absent_estimate_leaves_the_ladder_exactly_as_it_was(self):
        fired_a: set = set()
        fired_b: set = set()
        for turns in (40, 50, 86, 96):
            assert _pick_turn_budget_nudge(
                turns, 100, 50, [15, 5], fired_a
            ) == _pick_turn_budget_nudge(turns, 100, 50, [15, 5], fired_b, None)
