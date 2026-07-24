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
