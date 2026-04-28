"""Tests for curation prompt construction and JSON-fence stripping."""

from istota.memory.curation.parser import parse_sectioned_doc
from istota.memory.curation.prompt import build_op_curation_prompt, strip_json_fences
from istota.memory.curation.types import SectionedDoc


class TestBuildPrompt:
    def test_includes_current_doc_structure(self):
        doc = parse_sectioned_doc("## Preferences\n- Foo\n## Projects\n- Bar\n")
        prompt = build_op_curation_prompt("alice", doc, "dated content", None)
        assert "## Preferences" in prompt
        assert "- Foo" in prompt
        assert "## Projects" in prompt

    def test_includes_dated_memories(self):
        doc = parse_sectioned_doc("## A\n- a\n")
        prompt = build_op_curation_prompt("alice", doc, "today's notes", None)
        assert "today's notes" in prompt

    def test_includes_kg_facts_when_provided(self):
        doc = parse_sectioned_doc("## A\n- a\n")
        prompt = build_op_curation_prompt(
            "alice", doc, "dated", "alice works_at Cynium [permanent]"
        )
        assert "Knowledge graph" in prompt
        assert "alice works_at Cynium" in prompt

    def test_omits_kg_section_when_facts_empty(self):
        doc = parse_sectioned_doc("## A\n- a\n")
        prompt_with = build_op_curation_prompt("alice", doc, "dated", "facts here")
        prompt_without = build_op_curation_prompt("alice", doc, "dated", None)
        # The "Knowledge graph" header should not appear when facts are missing
        assert "Knowledge graph" in prompt_with
        assert "Knowledge graph" not in prompt_without

    def test_handles_empty_user_md(self):
        doc = SectionedDoc(preamble=[], sections=[])
        prompt = build_op_curation_prompt("alice", doc, "dated", None)
        # Should still produce a valid prompt with the user_id and dated content
        assert "alice" in prompt
        assert "dated" in prompt

    def test_specifies_json_output_format(self):
        doc = parse_sectioned_doc("## A\n- a\n")
        prompt = build_op_curation_prompt("alice", doc, "dated", None)
        assert "ops" in prompt.lower()
        # Must mention the available op kinds
        assert "append" in prompt
        assert "add_heading" in prompt
        assert "remove" in prompt


class TestStripJsonFences:
    def test_unwraps_json_block_with_lang(self):
        text = "```json\n{\"ops\": []}\n```"
        assert strip_json_fences(text) == '{"ops": []}'

    def test_unwraps_plain_fence(self):
        text = "```\n{\"ops\": []}\n```"
        assert strip_json_fences(text) == '{"ops": []}'

    def test_passes_through_unfenced(self):
        text = '{"ops": []}'
        assert strip_json_fences(text) == '{"ops": []}'

    def test_handles_surrounding_whitespace(self):
        text = "  \n```json\n{\"ops\": []}\n```\n  "
        assert strip_json_fences(text) == '{"ops": []}'
