"""No document outside the shipped catalogue names a current model id.

The ids used to be restated by hand in three documents, and #548 was the
failure that bred: the deployment default moved to a new Opus while `OPUS` and
all three tables still named the old one. The catalogue
(`src/istota/llm/data/anthropic.json`) is now the one place a model is named,
and the documents say "the newest Opus" instead. A current id written into a
document is the first step back to that drift, so it fails here: every family's
newest current model, by its Anthropic id and by OpenRouter's dotted spelling.
An older id may still appear as an example of the syntax.
"""

import re
from pathlib import Path

from istota.llm.catalog import latest_shipped_id, shipped_anthropic_models

ROOT = Path(__file__).resolve().parents[1]

DOCUMENTS = (
    sorted((ROOT / "docs").rglob("*.md"))
    + sorted((ROOT / ".claude" / "rules").glob("*.md"))
    + [
        ROOT / "config" / "config.example.toml",
        ROOT / "README.md",
        ROOT / "AGENTS.md",
    ]
)


def _current_ids() -> set[str]:
    families = {m.family for m in shipped_anthropic_models() if m.family}
    ids = {latest_shipped_id(family) for family in families}
    dotted = {re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", i) for i in ids}
    return ids | dotted


def _names(text: str, model_id: str) -> bool:
    # A whole id, not the prefix of a dated or longer one.
    return re.search(rf"(?<![\w.-]){re.escape(model_id)}(?![\w.]|-\d)", text) is not None


def test_the_current_ids_are_what_the_catalogue_says():
    # The guard below is only as good as this set: if the catalogue's newest
    # Opus is not in it, the scan checks nothing.
    current = _current_ids()
    for family in ("opus", "sonnet", "haiku"):
        assert latest_shipped_id(family) in current


def test_no_document_names_a_current_model_id():
    current = _current_ids()
    found = []
    for path in DOCUMENTS:
        if not path.exists():
            continue
        text = path.read_text()
        for model_id in sorted(current):
            if _names(text, model_id):
                found.append(f"{path.relative_to(ROOT)}: {model_id}")
    assert found == [], "name the family or an alias instead: " + ", ".join(found)


def test_the_matcher_finds_an_id_and_not_its_older_neighbour():
    assert _names('model = "claude-opus-9-9"', "claude-opus-9-9")
    assert _names("`anthropic/claude-opus-9.9:high`", "claude-opus-9.9")
    assert not _names('model = "claude-opus-9-9-20990101"', "claude-opus-9-9")
    assert not _names('model = "claude-opus-9"', "claude-opus-9-9")
    assert not _names("claude-opus-9-90", "claude-opus-9-9")


def test_the_example_config_names_the_tiers_by_family():
    text = (ROOT / "config/config.example.toml").read_text()
    table = dict(re.findall(r"^#\s+(fast|general|smart)\s+→\s+(\S+)", text, re.M))
    assert table == {"fast": "haiku", "general": "sonnet", "smart": "opus"}
