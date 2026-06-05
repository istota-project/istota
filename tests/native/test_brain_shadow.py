"""Tier-4 shadow-compare — the pure result-diff core (scripts/brain_shadow.py).

The brain-spawning glue needs the live `claude` CLI + a real API key, so it's
manual. The comparison logic — text similarity, tool-call sequence diff, usage
delta — is pure and tested here against hand-built BrainResults.
"""

import importlib.util
from pathlib import Path

from istota.brain._types import BrainResult
from istota.session.usage import TaskUsage

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "brain_shadow.py"


def _load():
    spec = importlib.util.spec_from_file_location("brain_shadow", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _result(text, actions, *, usage=None, stop="completed"):
    import json

    return BrainResult(
        success=True,
        result_text=text,
        actions_taken=json.dumps(actions),
        execution_trace=None,
        stop_reason=stop,
        usage=usage,
    )


class TestCompareBrainResults:
    def test_identical_text_and_actions(self):
        mod = _load()
        a = _result("hello", ["Read x", "Bash ls"])
        b = _result("hello", ["Read x", "Bash ls"])
        cmp = mod.compare_brain_results(a, b)
        assert cmp["text_identical"] is True
        assert cmp["actions_match"] is True
        assert cmp["text_similarity"] == 1.0

    def test_divergent_text(self):
        mod = _load()
        a = _result("the answer is 42", ["Read x"])
        b = _result("the answer is 43", ["Read x"])
        cmp = mod.compare_brain_results(a, b)
        assert cmp["text_identical"] is False
        assert 0.0 < cmp["text_similarity"] < 1.0
        assert cmp["actions_match"] is True

    def test_divergent_tool_sequence(self):
        mod = _load()
        a = _result("done", ["Read x", "Bash ls"])
        b = _result("done", ["Grep foo", "Read x"])
        cmp = mod.compare_brain_results(a, b)
        assert cmp["actions_match"] is False
        assert cmp["actions_a"] == ["Read x", "Bash ls"]
        assert cmp["actions_b"] == ["Grep foo", "Read x"]

    def test_usage_surfaced_when_present(self):
        mod = _load()
        a = _result("x", [], usage=None)  # claude_code leaves usage None
        b = _result("x", [], usage=TaskUsage(input_tokens=100, output_tokens=20, cost_usd=0.5))
        cmp = mod.compare_brain_results(a, b)
        assert cmp["usage_a"] is None
        assert cmp["usage_b"]["input_tokens"] == 100
        assert cmp["usage_b"]["cost_usd"] == 0.5

    def test_format_comparison_is_printable(self):
        mod = _load()
        a = _result("hello", ["Read x"])
        b = _result("hi", ["Read x"])
        text = mod.format_comparison(mod.compare_brain_results(a, b))
        assert isinstance(text, str)
        assert "similarity" in text.lower()
