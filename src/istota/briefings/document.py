"""Join model text to daemon-authored block snapshots."""

from __future__ import annotations

import re

from istota.briefings.models import RENDER_MODES, validate_briefing_document
from istota.skills.briefing import parse_briefing_json


def validate_briefing_manifest(manifest: object) -> dict:
    """Validate the versioned snapshot before generation or completion."""
    if not isinstance(manifest, dict) or type(manifest.get("version")) is not int or manifest["version"] != 1:
        raise ValueError("briefing_manifest_version")
    if not isinstance(manifest.get("blocks"), list):
        raise ValueError("briefing_manifest_blocks")
    blocks = []
    for block in manifest["blocks"]:
        if not isinstance(block, dict):
            raise ValueError("briefing_manifest_block")
        if block.get("render_mode") not in RENDER_MODES:
            raise ValueError("briefing_manifest_render_mode")
        count = block.get("source_count")
        if type(count) is not int or count < 0:
            raise ValueError("briefing_manifest_source_count")
        blocks.append({**block, "kind": "markdown", "body_md": ""})
    try:
        validated = validate_briefing_document({
            "version": 1, "title": manifest.get("title"), "blocks": blocks,
        })
    except ValueError as error:
        raise ValueError(str(error).replace("briefing_document_", "briefing_manifest_")) from None
    for block, original in zip(validated["blocks"], manifest["blocks"]):
        del block["kind"], block["body_md"]
        block["render_mode"] = original["render_mode"]
        block["source_count"] = original["source_count"]
    return validated


def normalize_briefing_document(output: str, manifest: dict) -> dict:
    """Require one result per requested ID and use only snapshot authority."""
    manifest = validate_briefing_manifest(manifest)
    parsed = parse_briefing_json(output)
    if not parsed or type(parsed.get("version")) is not int or parsed["version"] != 2:
        raise ValueError("briefing_output_version")
    if not isinstance(parsed.get("blocks"), list):
        raise ValueError("briefing_output_blocks")
    expected = {block["block_id"] for block in manifest["blocks"]}
    bodies = {}
    for block in parsed["blocks"]:
        if not isinstance(block, dict):
            raise ValueError("briefing_output_block")
        block_id = block.get("block_id")
        if type(block_id) is not int or block_id not in expected or block_id in bodies:
            raise ValueError("briefing_output_block_id")
        if not isinstance(block.get("body_md"), str):
            raise ValueError("briefing_output_body_md")
        bodies[block_id] = block["body_md"]
    if set(bodies) != expected:
        raise ValueError("briefing_output_missing_block")
    blocks = []
    for block in manifest["blocks"]:
        body = bodies[block["block_id"]]
        if not body.strip():
            continue
        blocks.append({
            "block_id": block["block_id"], "title": block["title"], "kind": "markdown",
            "body_md": body, "presentation": block["presentation"],
            "source_status": block["source_status"],
        })
    if not blocks:
        raise ValueError("briefing_no_content")
    return validate_briefing_document({"version": 1, "title": manifest["title"], "blocks": blocks})


def document_to_markdown(document: dict) -> str:
    """Project complete ordered blocks with literal Markdown section labels."""
    document = validate_briefing_document(document)
    sections = []
    for block in document["blocks"]:
        title = " ".join(block["title"].split())
        title = re.sub(r"([\\`*_{}\[\]()#+.!|<>~&-])", r"\\\1", title)
        sections.append(title + "\n\n" + block["body_md"])
    return "\n\n".join(sections)
