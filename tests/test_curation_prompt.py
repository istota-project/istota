"""Tests for curation prompt construction and JSON-fence stripping."""

import os
from unittest.mock import patch

import pytest

from istota.memory.curation.parser import parse_sectioned_doc
from istota.memory.curation.prompt import (
    build_op_curation_prompt,
    render_skill_overlay_inventory,
    strip_json_fences,
)
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
            "alice", doc, "dated", "alice works_at Acme [permanent]"
        )
        assert "Knowledge graph" in prompt
        assert "alice works_at Acme" in prompt

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

    def test_documents_widened_op_set(self):
        doc = parse_sectioned_doc("## A\n- a\n")
        prompt = build_op_curation_prompt("alice", doc, "dated", None)
        # The newer ops must be advertised so the model can use them.
        assert "replace" in prompt
        assert "remove_heading" in prompt
        assert "subheading" in prompt

    def test_does_not_claim_subsections_are_opaque(self):
        # remove/replace now reach into subsections; a stale "opaque" /
        # "top region only" instruction would mislead the model.
        doc = parse_sectioned_doc("## A\n- a\n### Sub\n- s\n")
        prompt = build_op_curation_prompt("alice", doc, "dated", None).lower()
        assert "opaque" not in prompt
        assert "only operate on the" not in prompt


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


class TestSkillOverlaySection:
    """Names and line counts, never bodies.

    The curator has to know a topic is handled elsewhere so it does not re-add
    the notes conventions to USER.md a week after they moved out. It is not
    asked to read those rules again, and it does not write overlays in v1.
    """

    DOC = "## A\n- a\n"

    def _prompt(self, overlays):
        doc = parse_sectioned_doc(self.DOC)
        return build_op_curation_prompt("alice", doc, "dated", None, overlays)

    def test_renders_a_row_per_overlay(self):
        prompt = self._prompt([("notes", 6), ("transcribe", 14)])
        assert (
            "## Skill overlays (already stored — do not duplicate to USER.md)"
            in prompt
        )
        assert "- notes: 6 lines" in prompt
        assert "- transcribe: 14 lines" in prompt

    def test_section_is_omitted_when_there_are_no_overlays(self):
        for empty in ([], (), None):
            assert "Skill overlays" not in self._prompt(empty)
        # The argument is optional, so the pre-overlay call shape still works.
        doc = parse_sectioned_doc(self.DOC)
        assert "Skill overlays" not in build_op_curation_prompt(
            "alice", doc, "dated", None
        )

    def test_one_line_is_singular(self):
        prompt = self._prompt([("notes", 1)])
        assert "- notes: 1 line" in prompt
        assert "- notes: 1 lines" not in prompt

    def test_a_malformed_row_is_dropped_rather_than_raising(self):
        """The nightly pass is the caller: a bad row costs a bullet, never the
        whole curation run. `float("inf")` is the one that bites — `int()`
        raises `OverflowError` on it, which is neither `TypeError` nor
        `ValueError` and would escape all the way out of the prompt build."""
        prompt = self._prompt([
            ("notes", 6),
            ("broken", None),
            ("floaty", float("inf")),
            ("boolish", True),
            ("negative", -3),
            "12",
            ("triple", 1, 2),
        ])
        assert "- notes: 6 lines" in prompt
        for dropped in ("broken", "floaty", "boolish", "negative", "triple"):
            assert dropped not in prompt
        # A two-character string unpacks as a pair without the shape check.
        assert "- 1: 2 lines" not in prompt

    def test_an_unbounded_row_cannot_fill_the_prompt(self):
        row = render_skill_overlay_inventory([("x" * 500, 10**400)])
        assert row == "- " + "x" * 60 + ": 100000 lines"

    def test_a_name_cannot_forge_a_line(self):
        """Belt and braces: the caller passes known skill names only, but a
        newline in one would otherwise write its own bullet or heading."""
        prompt = self._prompt([("notes\n## Knowledge graph", 6)])
        assert "- notes ## Knowledge graph: 6 lines" in prompt
        assert "\n## Knowledge graph" not in prompt
        assert "\x00" not in self._prompt([("no\x00tes", 6)])

    def test_says_the_bodies_are_not_shown_and_are_not_grounds_to_remove(self):
        """The heading is the knowledge graph's, and that section is grounds
        both to skip a fact and to drop one. Here the model is shown no
        content, so a bullet it thinks a listed skill covers is a bullet it
        cannot check itself against — and `remove` is destructive."""
        prompt = self._prompt([("notes", 6)])
        section = prompt.split("## Skill overlays")[1]
        assert "not shown" in section
        assert "never grounds to REMOVE" in section


class TestOverlayInventoryFromDisk:
    """`_load_skill_overlay_inventory` against a real directory.

    What it walks is under `{mount}/Users/{user_id}`, which is bound read-write
    into that user's own sandbox, so every component of it is model-plantable —
    and the sleep cycle reads it unsandboxed, with no timeout over the read.
    """

    SKILLS = ("developer", "notes", "sensitive_actions")

    @classmethod
    def _config(cls, tmp_path, **overrides):
        """A real Config: the reader takes `bot_dir_name`, `skills_dir`,
        `bundled_skills_dir`, the disabled sets and the mount off it, and a
        MagicMock answers all of them whether or not they exist."""
        from istota.config import Config, UserConfig

        bundled = tmp_path / "bundled"
        for skill in cls.SKILLS:
            d = bundled / skill
            d.mkdir(parents=True, exist_ok=True)
            (d / "skill.md").write_text(
                f"---\nname: {skill}\ndescription: the {skill} skill\n---\n\n# {skill}\n"
            )
        ops = tmp_path / "ops_skills"
        ops.mkdir(exist_ok=True)
        overrides.setdefault("nextcloud_mount_path", tmp_path / "mount")
        overrides.setdefault("users", {"alice": UserConfig()})
        return Config(
            db_path=tmp_path / "istota.db",
            temp_dir=tmp_path / "tmp",
            bundled_skills_dir=bundled,
            skills_dir=ops,
            **overrides,
        )

    @staticmethod
    def _overlays(config, user_id="alice"):
        d = (
            config.nextcloud_mount_path
            / "Users" / user_id / config.bot_dir_name / "config" / "skills"
        )
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _load(config, user_id="alice"):
        from istota.memory.sleep_cycle import _load_skill_overlay_inventory

        return _load_skill_overlay_inventory(config, user_id)

    def _prompt_for(self, config):
        return build_op_curation_prompt(
            "alice",
            parse_sectioned_doc("## A\n- a\n"),
            "dated",
            None,
            self._load(config),
        )

    def test_counts_lines_and_reaches_the_prompt_without_the_body(self, tmp_path):
        config = self._config(tmp_path)
        (self._overlays(config) / "developer.md").write_text(
            "- Never run the full suite in a foreground task here.\n"
            "\n"
            "- The box has 4 cores.\n"
        )
        assert self._load(config) == [("developer", 2)]

        prompt = self._prompt_for(config)
        assert "- developer: 2 lines" in prompt
        assert "foreground task" not in prompt

    def test_an_empty_directory_renders_no_section(self, tmp_path):
        config = self._config(tmp_path)
        self._overlays(config)
        assert self._load(config) == []
        assert "Skill overlays" not in self._prompt_for(config)

    def test_a_missing_directory_is_not_an_error(self, tmp_path):
        config = self._config(tmp_path)
        assert self._load(config) == []

    def test_no_mount_means_no_inventory(self, tmp_path):
        """Overlays are filesystem reads, so an rclone-remote deployment has
        none — the condition `load_persona` already applies to `PERSONA.md`."""
        config = self._config(tmp_path)
        (self._overlays(config) / "developer.md").write_text("- a rule\n")
        remote = self._config(tmp_path, nextcloud_mount_path=None)
        assert remote.use_mount is False
        assert self._load(remote) == []

    def test_a_file_that_will_never_bind_is_not_listed(self, tmp_path):
        """A misspelled, denylisted or effectively empty overlay reaches no
        prompt, so listing it would tell the curator a rule is stored live
        somewhere when it is not."""
        config = self._config(tmp_path)
        d = self._overlays(config)
        (d / "develper.md").write_text("- a typo, so it binds to nothing\n")
        (d / "sensitive_actions.md").write_text("- planted\n")
        (d / "notes.md").write_text("---\ntitle: x\n---\n\n   \n")
        (d / "developer.md").write_text("- a real rule\n")
        assert self._load(config) == [("developer", 1)]

    def test_a_disabled_skill_is_not_listed(self, tmp_path):
        from istota.config import UserConfig

        config = self._config(
            tmp_path,
            disabled_skills=["notes"],
            users={"alice": UserConfig(disabled_skills=["developer"])},
        )
        d = self._overlays(config)
        (d / "notes.md").write_text("- switched off instance-wide\n")
        (d / "developer.md").write_text("- switched off for alice\n")
        (d / "sensitive_actions.md").write_text("- and this one is denylisted\n")
        assert self._load(config) == []
        # The same three files with nothing switched off, so the empty list
        # above is the disabled set firing rather than the directory going
        # unread — and `sensitive_actions` stays out either way.
        assert self._load(self._config(tmp_path)) == [("developer", 1), ("notes", 1)]

    def test_a_planted_symlink_file_is_not_read(self, tmp_path):
        """A live overlay sits beside the planted one, so "the guard fired" is
        distinguishable from "nothing was read at all"."""
        config = self._config(tmp_path)
        secret = tmp_path / "credentials.json"
        secret.write_text("- TOP SECRET TOKEN value\n")
        d = self._overlays(config)
        (d / "developer.md").symlink_to(secret)
        (d / "notes.md").write_text("- a real rule\n")
        assert self._load(config) == [("notes", 1)]
        assert "TOP SECRET" not in self._prompt_for(config)

    def test_only_files_named_for_a_known_skill_are_opened(self, tmp_path):
        """The candidate names come from the skill index, not from the tree.
        Listing the directory would open and read every file a task planted —
        to the 32 KiB cap each, on the unsandboxed nightly path — before
        discarding it for its name."""
        from istota.skills import _loader

        config = self._config(tmp_path)
        d = self._overlays(config)
        (d / "developer.md").write_text("- a real rule\n")
        for i in range(50):
            (d / f"planted{i}.md").write_text("- planted\n")

        opened: list[str] = []
        real = _loader.inspect_overlay

        def counting(path, **kwargs):
            opened.append(path.name)
            return real(path, **kwargs)

        with patch.object(_loader, "inspect_overlay", counting):
            assert self._load(config) == [("developer", 1)]
        assert opened == sorted(f"{s}.md" for s in self.SKILLS)

    def test_a_redirected_overlay_directory_is_not_walked(self, tmp_path):
        """`O_NOFOLLOW` covers the last component only — the files behind a
        redirected `config/` or `skills/` are ordinary regular files that pass
        every leaf-level guard."""
        config = self._config(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "developer.md").write_text("- CONSTITUTIONAL SECRET TEXT\n")
        user_config = (
            config.nextcloud_mount_path
            / "Users" / "alice" / config.bot_dir_name / "config"
        )
        user_config.mkdir(parents=True)
        (user_config / "skills").symlink_to(elsewhere, target_is_directory=True)
        assert self._load(config) == []
        assert "CONSTITUTIONAL" not in self._prompt_for(config)

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no mkfifo on this platform")
    def test_a_fifo_does_not_hang_the_nightly_pass(self, tmp_path):
        """A FIFO with no writer blocks `open(2)`, and nothing times out this
        read: the prompt is assembled before any brain request exists."""
        config = self._config(tmp_path)
        d = self._overlays(config)
        os.mkfifo(d / "developer.md")
        (d / "notes.md").write_text("- a real rule\n")
        assert self._load(config) == [("notes", 1)]
