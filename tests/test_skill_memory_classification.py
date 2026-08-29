"""Assertion tests on the rendered memory skill body to prevent regressions
on the classification gate and on the `echo >>` ban.
"""

from __future__ import annotations

import re
from pathlib import Path


SKILL_MD = Path(__file__).parent.parent / "src" / "istota" / "skills" / "memory" / "skill.md"


def _body() -> str:
    text = SKILL_MD.read_text()
    # Strip the YAML frontmatter — only test the body.
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3:]
    return text


class TestClassificationGate:
    def test_three_branch_labels_present(self):
        body = _body()
        for label in ["Temporal event", "Stable factual claim", "Behavioral instruction"]:
            assert label in body, f"missing classification branch: {label!r}"

    def test_the_skill_overlay_branch_is_present(self):
        body = _body()
        assert "Skill-specific instruction" in body
        assert "memory append --skill" in body

    def test_the_overlay_branch_carries_the_prohibition_test(self):
        """The gate that decides USER.md vs overlay, not just a mention of the
        target. An overlay only reaches the prompt when its skill is selected,
        and selection is heuristic — so a rule that must hold on a task where
        the skill did not load has to be routed to USER.md by name, or the
        branch above quietly turns every prohibition into a conditional one.
        """
        body = _body()
        assert "would it be *wrong* to ignore this rule" in body.lower()
        assert "not loaded" in body
        assert "merely irrelevant" in body

    def test_the_overlay_section_states_the_flat_document_rules(self):
        """`--heading` means something different under `--skill`, and three
        verbs are refused outright. A model that guesses either wastes a turn
        on a JSON refusal it could have read here.
        """
        body = _body()
        assert "### Per-skill overlays" in body
        assert "memory skills" in body
        for verb in ["add-heading", "remove-heading", "headings"]:
            assert verb in body
        assert "sensitive_actions" in body and "untrusted_input" in body

    def test_routes_to_both_cli_targets(self):
        body = _body()
        assert "add-fact" in body
        assert "memory append" in body

    def test_no_echo_redirect_to_user_md(self):
        body = _body()
        # Forbidden patterns: any `echo "..." >>` or `>> ` aimed at USER.md.
        assert not re.search(r"echo\s+\".+\"\s*>>\s*\S*USER\.md", body)
        assert not re.search(r">>\s*\S*USER\.md", body)
        # Same for CHANNEL.md — runtime CLI is the only path.
        assert not re.search(r"echo\s+\".+\"\s*>>\s*\S*CHANNEL\.md", body)

    def test_explicit_dont_bypass_callout(self):
        body = _body()
        assert "echo >>" in body
        assert "Never write to USER.md" in body or "Never write" in body

    def test_stable_fact_branch_lists_categories(self):
        body = _body()
        # The branch should mention at least three of: family, medical,
        # allergies, biography, relationship.
        present = sum(
            1 for kw in ["family", "medical", "allergies", "biography", "relationship"]
            if kw in body.lower()
        )
        assert present >= 3, f"only {present} stable-fact categories present"

    def test_skill_is_cli_enabled(self):
        front = SKILL_MD.read_text().split("---", 2)[1]
        assert "cli: true" in front
