"""Prompt construction for op-based USER.md curation, plus JSON-fence stripping."""

from __future__ import annotations

from .parser import serialize_sectioned_doc
from .types import SectionedDoc


def build_op_curation_prompt(
    user_id: str,
    doc: SectionedDoc,
    dated_memories: str,
    kg_facts_text: str | None,
) -> str:
    parts: list[str] = []

    parts.append(
        f"You are curating the durable memory file USER.md for user '{user_id}'.\n"
        "\n"
        "USER.md is the slow tier of memory: small, deliberate, almost append-only.\n"
        "Your job is to emit a JSON list of small operations — never to rewrite the file."
    )

    parts.append("## Current USER.md structure")
    serialized = serialize_sectioned_doc(doc)
    parts.append(serialized.rstrip("\n") if serialized else "(empty)")

    parts.append("## Recent dated memories (3 day window)")
    parts.append(dated_memories.rstrip("\n") if dated_memories else "(none)")

    if kg_facts_text and kg_facts_text.strip():
        parts.append("## Knowledge graph (already stored — do not duplicate to USER.md)")
        parts.append(kg_facts_text.rstrip("\n"))

    parts.append(
        "## Operations available\n"
        "\n"
        "- append: add a bullet under an EXISTING heading (optionally under one of its\n"
        '  `### subheadings` via "subheading")\n'
        "- add_heading: create a NEW heading with one or more bullets\n"
        "- remove: remove a bullet (heading + substring match; must be unique)\n"
        "- replace: rewrite the single matching bullet in place (heading + match + new line)\n"
        "- remove_heading: drop a whole `## ` section (heading)"
    )

    parts.append(
        "## How ops are applied\n"
        "\n"
        "- Headings are matched **case-sensitive exact** against the structure shown above.\n"
        "  Copy the heading text verbatim.\n"
        '- "Bullet" means a line starting with `-`, `*`, or `1.` (etc). Paragraphs and `### subheading`\n'
        "  lines themselves are NOT bullets and are NEVER matched or removed by these ops.\n"
        "- `remove` and `replace` match `match` as a case-insensitive substring against bullet text\n"
        "  across the WHOLE section (top region AND `### subsections`). If zero bullets match, the op\n"
        "  is a quiet no-op. If multiple match, the op is rejected — be more specific.\n"
        "- `append` without a subheading inserts at the end of the top region (before any `###`).\n"
        '  With "subheading" set, it appends under that subsection instead.\n'
        "- `append` deduplicates: identical bullet text in the target region produces no change.\n"
        "- `replace` preserves the matched bullet's indentation; an identical rewrite is a no-op.\n"
        "- `remove_heading` deletes the entire section — use it only for sections that are wholly stale.\n"
        "- `add_heading` rejects existing names; use `append` to add a bullet under an existing heading."
    )

    parts.append(
        "## Rules\n"
        "\n"
        "1. Only emit ops for DURABLE facts: long-lived preferences, projects, people, decisions.\n"
        "2. Skip anything in the knowledge graph above — it is already stored.\n"
        '3. Skip temporary or time-bound info ("meeting tomorrow", "ordered groceries").\n'
        "4. Skip task references (ref:NNNN).\n"
        '5. Most nights, the right answer is `{"ops": []}` — do not invent edits to seem useful.\n'
        "6. To remove an outdated entry, it must be clearly contradicted by newer information. If unsure,\n"
        "   leave it.\n"
        "7. For `remove`, the `match` substring must be specific enough that only ONE line matches."
    )

    parts.append(
        "## Output format\n"
        "\n"
        "Return ONLY a JSON object, no preamble:\n"
        "\n"
        '{"ops": [\n'
        '  {"op": "append", "heading": "Preferences", "line": "..."},\n'
        "  ...\n"
        "]}\n"
        "\n"
        'If nothing to change: {"ops": []}'
    )

    return "\n\n".join(parts) + "\n"


def strip_json_fences(text: str) -> str:
    """Strip ` ```json … ``` ` or ` ``` … ``` ` wrapping if present.

    Returns the content between the fences (stripped). Falls through to a
    plain `text.strip()` when no fences are present.
    """
    s = text.strip()
    if not s.startswith("```"):
        return s
    # Drop the opening fence line entirely (e.g. "```json" or just "```")
    nl = s.find("\n")
    if nl == -1:
        # Single-line ```...``` is degenerate; just strip backticks
        return s.strip("`").strip()
    body = s[nl + 1:]
    # Drop closing fence, if present.
    if body.endswith("```"):
        body = body[:-3]
    return body.strip()
