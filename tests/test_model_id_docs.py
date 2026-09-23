"""The documented model-id tables must match the ClaudeCodeBrain constants.

A model release is meant to be one constant edit, but three documents restate
the ids by hand. #548 was the failure this guards: the deployment default moved
to a new Opus while `OPUS` and all three tables still named the old one, and the
web room picker (built from the alias table) kept offering it.
"""

import re
from pathlib import Path

from istota.brain.claude_code import HAIKU, OPUS, SONNET

ROOT = Path(__file__).resolve().parents[1]
CONSTANTS = {"OPUS": OPUS, "SONNET": SONNET, "HAIKU": HAIKU}


def _constant_assignments(text: str) -> dict[str, str]:
    return dict(re.findall(r'`(OPUS|SONNET|HAIKU) = "([^"]+)"`', text))


def test_brain_rules_list_the_current_constants():
    text = (ROOT / ".claude/rules/brain.md").read_text()
    assert _constant_assignments(text) == CONSTANTS


def test_architecture_doc_lists_the_current_constants():
    text = (ROOT / "docs/architecture/brain.md").read_text()
    assert _constant_assignments(text) == CONSTANTS


def test_example_config_shipped_defaults_match_the_alias_table():
    text = (ROOT / "config/config.example.toml").read_text()
    table = dict(re.findall(r"^#\s+(fast|general|smart)\s+→\s+(\S+)", text, re.M))
    assert table == {"fast": HAIKU, "general": SONNET, "smart": OPUS}
