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
