"""Immutable edition and model-output contracts."""

from copy import deepcopy
import json

import pytest

from istota.briefings.document import document_to_markdown, normalize_briefing_document
from istota.briefings.models import presentation_for_options, validate_block_options, validate_briefing_document
from istota.skills.briefing import parse_briefing_json


@pytest.fixture
def manifest():
    return {"version": 1, "title": "Morning edition", "blocks": [
        {"block_id": 42, "title": "Same title", "render_mode": "synthesis",
         "presentation": {"width": "wide"}, "source_count": 2, "source_status": "partial"},
        {"block_id": 17, "title": "Same title", "render_mode": "structured",
         "presentation": {"width": "auto"}, "source_count": 1, "source_status": "complete"},
    ]}


def output(blocks):
    return json.dumps({"version": 2, "blocks": blocks})


def test_two_blocks_keep_snapshot_identity_order_and_authority(manifest):
    result = normalize_briefing_document(output([
        {"block_id": 17, "body_md": "Calendar content", "title": "Forged title"},
        {"block_id": 42, "body_md": "[Story](https://example.com/story)",
         "presentation": {"width": "full"}, "source_status": "complete"},
    ]), manifest)
    assert result == {"version": 1, "title": "Morning edition", "blocks": [
        {"block_id": 42, "title": "Same title", "kind": "markdown",
         "body_md": "[Story](https://example.com/story)",
         "presentation": {"width": "wide"}, "source_status": "partial"},
        {"block_id": 17, "title": "Same title", "kind": "markdown",
         "body_md": "Calendar content", "presentation": {"width": "auto"}, "source_status": "complete"},
    ]}
    manifest["blocks"][0]["presentation"]["width"] = "full"
    assert result["blocks"][0]["presentation"] == {"width": "wide"}


@pytest.mark.parametrize("blocks", [
    [{"block_id": 42, "body_md": "one"}],
    [{"block_id": 42, "body_md": "one"}, {"block_id": 42, "body_md": "two"}],
    [{"block_id": 42, "body_md": "one"}, {"block_id": 999, "body_md": "two"}],
    [{"block_id": True, "body_md": "one"}, {"block_id": 17, "body_md": "two"}],
    [{"block_id": "42", "body_md": "one"}, {"block_id": 17, "body_md": "two"}],
    [{"block_id": 42, "body_md": None}, {"block_id": 17, "body_md": "two"}], [None],
])
def test_invalid_model_blocks_are_rejected(manifest, blocks):
    with pytest.raises(ValueError, match="briefing_output_"):
        normalize_briefing_document(output(blocks), manifest)


@pytest.mark.parametrize("value", [
    {"version": 3, "blocks": []}, {"version": True, "blocks": []},
    {"version": 2, "blocks": {}}, {"body": "Legacy body"},
])
def test_wrong_envelope_is_rejected_for_a_manifest_run(manifest, value):
    with pytest.raises(ValueError, match="briefing_output_"):
        normalize_briefing_document(json.dumps(value), manifest)


def test_explicit_empty_omits_only_that_block(manifest):
    result = normalize_briefing_document(output([
        {"block_id": 42, "body_md": "  "}, {"block_id": 17, "body_md": "Useful"},
    ]), manifest)
    assert [block["block_id"] for block in result["blocks"]] == [17]
    with pytest.raises(ValueError, match="^briefing_no_content$"):
        normalize_briefing_document(output([
            {"block_id": 42, "body_md": ""}, {"block_id": 17, "body_md": ""},
        ]), manifest)


@pytest.mark.parametrize("mutation", [
    lambda m: m.update(version=2),
    lambda m: m["blocks"].append(deepcopy(m["blocks"][0])),
    lambda m: m["blocks"][0].update(block_id=True),
    lambda m: m["blocks"][0].update(source_status="exception details"),
    lambda m: m["blocks"][0].update(presentation={"width": "unknown"}),
])
def test_invalid_manifest_is_rejected(manifest, mutation):
    mutation(manifest)
    with pytest.raises(ValueError, match="briefing_manifest_"):
        normalize_briefing_document(output([]), manifest)


def test_linear_projection_escapes_titles_and_preserves_complete_bodies(manifest):
    manifest["blocks"][0]["title"] = "# [News] *today*\nsecond line"
    document = normalize_briefing_document(output([
        {"block_id": 42, "body_md": "  Full [story](https://example.com/a_b?q=x_y)\n\nLast paragraph.  "},
        {"block_id": 17, "body_md": "- First\n- Second"},
    ]), manifest)
    assert document_to_markdown(document) == (
        r"\# \[News\] \*today\* second line" + "\n\n"
        "  Full [story](https://example.com/a_b?q=x_y)\n\nLast paragraph.  \n\n"
        "Same title\n\n- First\n- Second"
    )


def test_parser_reuses_legacy_extraction_paths_and_preserves_document(manifest):
    raw = output([{"block_id": 42, "body_md": "News"}, {"block_id": 17, "body_md": "Agenda"}])
    for wrapped in (raw, "```json\n" + raw + "\n```", "Result:\n" + raw + "\nDone"):
        assert len(normalize_briefing_document(wrapped, manifest)["blocks"]) == 2
    assert parse_briefing_json('Prefix {"body": "Old body", "subject": "Old title"} suffix') == {
        "body": "Old body", "subject": "Old title",
    }
    document = normalize_briefing_document(raw, manifest)
    parsed = parse_briefing_json(json.dumps({"body": "Linear", "briefing_document": document}))
    assert parsed["body"] == "Linear" and parsed["briefing_document"] == document


def test_document_validator_allows_future_kinds_with_text_fallback(manifest):
    document = normalize_briefing_document(output([
        {"block_id": 42, "body_md": "News"}, {"block_id": 17, "body_md": "Agenda"},
    ]), manifest)
    document["blocks"][0]["kind"] = "future-agenda"
    assert validate_briefing_document(document) == document
    for mutation in (
        lambda d: d.update(version=99), lambda d: d["blocks"][0].update(body_md=5),
        lambda d: d["blocks"][0].update(block_id=False),
        lambda d: d["blocks"].append(deepcopy(d["blocks"][0])),
    ):
        invalid = deepcopy(document)
        mutation(invalid)
        with pytest.raises(ValueError, match="briefing_document_"):
            validate_briefing_document(invalid)


@pytest.mark.parametrize("presentation", [None, [], "wide", {"width": "giant"}, {"width": True}])
def test_invalid_new_presentation_rejected_old_reads_default(presentation, caplog):
    with pytest.raises(ValueError, match="presentation"):
        validate_block_options({"presentation": presentation})
    assert presentation_for_options({"presentation": presentation}) == {"width": "auto"}
    assert "briefing_presentation_invalid" in caplog.text


def test_presentation_defaults_and_unrelated_options_survive():
    assert presentation_for_options({}) == {"width": "auto"}
    options = {"tone": "neutral", "presentation": {"width": "wide"}, "custom": {"a": 1}}
    validated = validate_block_options(options)
    assert validated == options and validated is not options
    assert validate_block_options({}) == {}


def test_linear_title_cannot_become_html_or_an_entity(manifest):
    manifest["blocks"][0]["title"] = "News <script> &copy;"
    document = normalize_briefing_document(output([
        {"block_id": 42, "body_md": "News"}, {"block_id": 17, "body_md": "Agenda"},
    ]), manifest)
    assert document_to_markdown(document).splitlines()[0] == r"News \<script\> \&copy;"


def test_edition_preview_reads_block_bodies_without_titles_or_the_notice():
    from istota.briefings.document import PREVIEW_CHARS, edition_preview
    document = {"version": 1, "title": "Daily", "blocks": [
        {"block_id": 0, "title": "Overview", "kind": "markdown",
         "body_md": "Incomplete sources: some selected sources were unavailable or empty.\n\n**Rates** held [today](https://example.com/rates).",
         "presentation": {"width": "auto"}, "source_status": "partial"}]}
    assert edition_preview("Overview\n\nignored", document) == "Rates held today."
    assert edition_preview("## Heading\n\n- one\n- two") == "Heading one two"
    long = edition_preview("word " * 100)
    assert len(long) == PREVIEW_CHARS and long.endswith("…")
