"""Tests for semantic skill routing (Pass 2 LLM-based skill classification)."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import subprocess

from istota.skills._loader import (
    build_skill_manifest,
    classify_skills,
    load_skill_index,
    load_skills,
    select_skills,
)
from istota.skills._types import SkillMeta
from istota.config import Config, SkillsConfig


def _empty_bundled(tmp_path: Path) -> Path:
    d = tmp_path / "bundled"
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestSkillsConfig:
    """Tests for the [skills] config section."""

    def test_defaults(self):
        cfg = SkillsConfig()
        assert cfg.semantic_routing is True
        assert cfg.semantic_routing_model == "haiku"
        assert cfg.semantic_routing_timeout == 3.0

    def test_config_has_skills_field(self):
        config = Config()
        assert isinstance(config.skills, SkillsConfig)

    def test_custom_values(self):
        cfg = SkillsConfig(
            semantic_routing=False,
            semantic_routing_model="sonnet",
            semantic_routing_timeout=5.0,
        )
        assert cfg.semantic_routing is False
        assert cfg.semantic_routing_model == "sonnet"
        assert cfg.semantic_routing_timeout == 5.0


class TestBuildSkillManifest:
    """Tests for the skill manifest builder used by Pass 2."""

    def _make_index(self) -> dict[str, SkillMeta]:
        return {
            "files": SkillMeta(
                name="files",
                description="File ops",
                always_include=True,
            ),
            "developer": SkillMeta(
                name="developer",
                description="Git workflows, merge requests, worktrees",
                keywords=["git", "gitlab", "commit", "branch"],
            ),
            "email": SkillMeta(
                name="email",
                description="Email sending and response formatting",
                keywords=["email", "mail", "send"],
            ),
            "money": SkillMeta(
                name="money",
                description="Accounting operations",
                keywords=["accounting", "ledger", "invoice"],
                resource_types=["money"],
            ),
        }

    def test_excludes_already_selected(self):
        index = self._make_index()
        manifest = build_skill_manifest(index, exclude={"email", "files"})
        assert "email" not in manifest
        assert "files" not in manifest
        assert "developer" in manifest
        assert "money" in manifest

    def test_excludes_always_include(self):
        index = self._make_index()
        manifest = build_skill_manifest(index, exclude=set())
        assert "files" not in manifest  # always_include skills are never in manifest

    def test_includes_description_and_triggers(self):
        index = self._make_index()
        manifest = build_skill_manifest(index, exclude=set())
        assert "Git workflows" in manifest
        assert "git, gitlab, commit, branch" in manifest

    def test_empty_when_all_excluded(self):
        index = self._make_index()
        all_names = set(index.keys())
        manifest = build_skill_manifest(index, exclude=all_names)
        assert "Available skills" in manifest
        # No skill lines
        lines = [l for l in manifest.split("\n") if l.startswith("- ")]
        assert len(lines) == 0

    def test_sorted_by_name(self):
        index = self._make_index()
        manifest = build_skill_manifest(index, exclude=set())
        lines = [l for l in manifest.split("\n") if l.startswith("- ")]
        names = [l.split(":")[0].lstrip("- ") for l in lines]
        assert names == sorted(names)

    def test_truncates_long_trigger_list(self):
        index = {
            "verbose": SkillMeta(
                name="verbose",
                description="Many triggers",
                keywords=[f"kw{i}" for i in range(20)],
            ),
        }
        manifest = build_skill_manifest(index, exclude=set())
        # Should only include first 10 triggers
        assert "kw9" in manifest
        assert "kw10" not in manifest

    def test_excludes_disabled_skills(self):
        index = self._make_index()
        manifest = build_skill_manifest(index, exclude=set(), disabled_skills={"developer"})
        assert "developer" not in manifest
        assert "email" in manifest

    def test_excludes_admin_only_for_non_admin(self):
        index = self._make_index()
        index["tasks"] = SkillMeta(
            name="tasks",
            description="Subtask creation",
            keywords=["subtask"],
            admin_only=True,
        )
        manifest = build_skill_manifest(index, exclude=set(), is_admin=False)
        assert "tasks" not in manifest

    def test_includes_admin_only_for_admin(self):
        index = self._make_index()
        index["tasks"] = SkillMeta(
            name="tasks",
            description="Subtask creation",
            keywords=["subtask"],
            admin_only=True,
        )
        manifest = build_skill_manifest(index, exclude=set(), is_admin=True)
        assert "tasks" in manifest

    def test_excludes_skills_with_missing_deps(self):
        index = self._make_index()
        index["broken"] = SkillMeta(
            name="broken",
            description="Broken skill",
            keywords=["broken"],
            dependencies=["nonexistent_pkg_xyz"],
        )
        manifest = build_skill_manifest(index, exclude=set())
        assert "broken" not in manifest


class TestClassifySkills:
    """Tests for the LLM-based skill classification (Pass 2)."""

    def _make_index(self) -> dict[str, SkillMeta]:
        return {
            "files": SkillMeta(
                name="files",
                description="File ops",
                always_include=True,
            ),
            "developer": SkillMeta(
                name="developer",
                description="Git workflows, merge requests, worktrees",
                keywords=["git", "gitlab", "commit", "branch"],
            ),
            "email": SkillMeta(
                name="email",
                description="Email sending and response formatting",
                keywords=["email", "mail", "send"],
            ),
            "location": SkillMeta(
                name="location",
                description="Location tracking and place recognition",
                keywords=["location", "gps", "where"],
            ),
        }

    @patch("istota.skills._loader.subprocess.run")
    def test_returns_additional_skills(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='["developer"]',
            stderr="",
        )
        result = classify_skills(
            prompt="fix the timezone bug in the codebase",
            skill_index=self._make_index(),
            already_selected={"files"},
        )
        assert result == ["developer"]

    @patch("istota.skills._loader.subprocess.run")
    def test_returns_empty_on_no_match(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="[]",
            stderr="",
        )
        result = classify_skills(
            prompt="hello, how are you?",
            skill_index=self._make_index(),
            already_selected={"files"},
        )
        assert result == []

    @patch("istota.skills._loader.subprocess.run")
    def test_returns_empty_on_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=3.0)
        result = classify_skills(
            prompt="fix the bug",
            skill_index=self._make_index(),
            already_selected={"files"},
        )
        assert result == []

    @patch("istota.skills._loader.subprocess.run")
    def test_returns_empty_on_nonzero_exit(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="API error",
        )
        result = classify_skills(
            prompt="fix the bug",
            skill_index=self._make_index(),
            already_selected={"files"},
        )
        assert result == []

    @patch("istota.skills._loader.subprocess.run")
    def test_returns_empty_on_invalid_json(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="not json at all",
            stderr="",
        )
        result = classify_skills(
            prompt="fix the bug",
            skill_index=self._make_index(),
            already_selected={"files"},
        )
        assert result == []

    @patch("istota.skills._loader.subprocess.run")
    def test_filters_hallucinated_skill_names(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='["developer", "nonexistent_skill", "magic"]',
            stderr="",
        )
        result = classify_skills(
            prompt="fix the bug",
            skill_index=self._make_index(),
            already_selected={"files"},
        )
        assert result == ["developer"]
        assert "nonexistent_skill" not in result
        assert "magic" not in result

    @patch("istota.skills._loader.subprocess.run")
    def test_filters_already_selected(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='["developer", "files"]',
            stderr="",
        )
        result = classify_skills(
            prompt="fix the bug",
            skill_index=self._make_index(),
            already_selected={"files"},
        )
        assert result == ["developer"]
        assert "files" not in result

    @patch("istota.skills._loader.subprocess.run")
    def test_uses_specified_model(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="[]",
            stderr="",
        )
        classify_skills(
            prompt="test",
            skill_index=self._make_index(),
            already_selected=set(),
            model="sonnet",
        )
        call_args = mock_run.call_args
        cmd = call_args[0][0] if call_args[0] else call_args.kwargs.get("args", [])
        assert "sonnet" in cmd

    @patch("istota.skills._loader.subprocess.run")
    def test_uses_specified_timeout(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="[]",
            stderr="",
        )
        classify_skills(
            prompt="test",
            skill_index=self._make_index(),
            already_selected=set(),
            timeout=5.0,
        )
        call_args = mock_run.call_args
        assert call_args.kwargs.get("timeout") == 5.0 or call_args[1].get("timeout") == 5.0

    @patch("istota.skills._loader.subprocess.run")
    def test_skips_when_no_unselected_skills(self, mock_run):
        """When all non-always-include skills are already selected, skip the LLM call."""
        index = self._make_index()
        all_names = set(index.keys())
        result = classify_skills(
            prompt="test",
            skill_index=index,
            already_selected=all_names,
        )
        assert result == []
        mock_run.assert_not_called()

    @patch("istota.skills._loader.subprocess.run")
    def test_parses_json_from_code_block(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='```json\n["developer"]\n```',
            stderr="",
        )
        result = classify_skills(
            prompt="fix the bug",
            skill_index=self._make_index(),
            already_selected={"files"},
        )
        assert result == ["developer"]

    @patch("istota.skills._loader.subprocess.run")
    def test_returns_empty_on_file_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError("claude not found")
        result = classify_skills(
            prompt="fix the bug",
            skill_index=self._make_index(),
            already_selected={"files"},
        )
        assert result == []

    @patch("istota.skills._loader.subprocess.run")
    def test_multiple_valid_skills(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='["developer", "location"]',
            stderr="",
        )
        result = classify_skills(
            prompt="fix the GPS tracking code",
            skill_index=self._make_index(),
            already_selected={"files"},
        )
        assert sorted(result) == ["developer", "location"]

    @patch("istota.skills._loader.subprocess.run")
    def test_disabled_skills_not_returned(self, mock_run):
        """classify_skills should not return disabled skills even if LLM suggests them."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='["developer", "email"]',
            stderr="",
        )
        result = classify_skills(
            prompt="fix the bug and send email",
            skill_index=self._make_index(),
            already_selected={"files"},
            disabled_skills={"developer"},
        )
        # developer is disabled, should not appear
        assert "developer" not in result
        assert "email" in result

    @patch("istota.skills._loader.subprocess.run")
    def test_admin_only_not_returned_for_non_admin(self, mock_run):
        """classify_skills should not return admin_only skills for non-admin users."""
        index = self._make_index()
        index["tasks"] = SkillMeta(
            name="tasks",
            description="Subtask creation",
            keywords=["subtask"],
            admin_only=True,
        )
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='["tasks", "developer"]',
            stderr="",
        )
        result = classify_skills(
            prompt="create subtasks for this",
            skill_index=index,
            already_selected={"files"},
            is_admin=False,
        )
        # tasks is admin_only and user is not admin — should not appear
        assert "tasks" not in result
        # developer is not admin_only — should still appear
        assert "developer" in result

    @patch("istota.skills._loader.subprocess.run")
    def test_skips_disabled_in_manifest(self, mock_run):
        """Disabled skills should not even appear in the manifest sent to the LLM."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="[]",
            stderr="",
        )
        classify_skills(
            prompt="test",
            skill_index=self._make_index(),
            already_selected=set(),
            disabled_skills={"developer"},
        )
        # Check the prompt sent to the subprocess doesn't contain developer
        call_args = mock_run.call_args
        prompt_sent = call_args.kwargs.get("input", "")
        assert "developer" not in prompt_sent


class TestExcludeSkillsReaddition:
    """Tests that Pass 2 can't re-add skills excluded by exclude_skills."""

    @patch("istota.skills._loader.subprocess.run")
    def test_pass2_cannot_readd_excluded_skill(self, mock_run):
        """If briefing excludes email via exclude_skills, Pass 2 can't re-add it."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='["email"]',
            stderr="",
        )
        index = {
            "files": SkillMeta(name="files", description="File ops", always_include=True),
            "briefing": SkillMeta(
                name="briefing", description="Briefing",
                source_types=["briefing"], exclude_skills=["email"],
            ),
            "email": SkillMeta(
                name="email", description="Email",
                keywords=["email", "mail"],
            ),
        }
        # Pass 1: briefing source_type match, email excluded by briefing
        pass1 = select_skills("generate briefing with email summary", "briefing", set(), index)
        assert "briefing" in pass1
        assert "email" not in pass1

        # Pass 2: LLM suggests email
        extra = classify_skills(
            prompt="generate briefing with email summary",
            skill_index=index,
            already_selected=set(pass1),
        )

        # Simulate what executor does: union + re-apply exclude_skills
        all_selected = set(pass1) | set(extra)
        excluded = set()
        for n in list(all_selected):
            m = index.get(n)
            if m:
                for ex in m.exclude_skills:
                    if ex in all_selected:
                        excluded.add(ex)
        all_selected -= excluded
        final = sorted(all_selected)

        # email should still be excluded
        assert "email" not in final
        assert "briefing" in final


class TestFrontmatterParsing:
    """Tests for YAML frontmatter in skill.md files."""

    def _make_skill_dir(self, tmp_path, name, toml_content, md_content):
        bundled = tmp_path / "bundled"
        skill_dir = bundled / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "skill.toml").write_text(toml_content)
        (skill_dir / "skill.md").write_text(md_content)
        return bundled

    def test_frontmatter_triggers_override_toml_keywords(self, tmp_path):
        bundled = self._make_skill_dir(
            tmp_path, "developer",
            toml_content='keywords = ["old_keyword"]\n',
            md_content=(
                "---\n"
                "name: developer\n"
                "triggers: [git, fix bug, patch, code change]\n"
                "description: Git workflows and code changes\n"
                "---\n"
                "\n"
                "# Developer\n\nInstructions here.\n"
            ),
        )
        config_skills = tmp_path / "config_skills"
        config_skills.mkdir()
        index = load_skill_index(config_skills, bundled_dir=bundled)
        assert "developer" in index
        assert index["developer"].keywords == ["git", "fix bug", "patch", "code change"]
        assert "old_keyword" not in index["developer"].keywords

    def test_frontmatter_description_overrides_toml(self, tmp_path):
        bundled = self._make_skill_dir(
            tmp_path, "developer",
            toml_content='description = "Old description"\n',
            md_content=(
                "---\n"
                "name: developer\n"
                "triggers: [git]\n"
                "description: New description from frontmatter\n"
                "---\n"
                "\n"
                "# Developer\n"
            ),
        )
        config_skills = tmp_path / "config_skills"
        config_skills.mkdir()
        index = load_skill_index(config_skills, bundled_dir=bundled)
        assert index["developer"].description == "New description from frontmatter"

    def test_no_frontmatter_falls_back_to_toml(self, tmp_path):
        bundled = self._make_skill_dir(
            tmp_path, "email",
            toml_content='description = "Email ops"\nkeywords = ["email", "mail"]\n',
            md_content="# Email\n\nInstructions here.\n",
        )
        config_skills = tmp_path / "config_skills"
        config_skills.mkdir()
        index = load_skill_index(config_skills, bundled_dir=bundled)
        assert index["email"].description == "Email ops"
        assert index["email"].keywords == ["email", "mail"]

    def test_frontmatter_stripped_from_skill_doc(self, tmp_path):
        """When loading skill docs, frontmatter should not appear in the output."""
        bundled = self._make_skill_dir(
            tmp_path, "developer",
            toml_content="",
            md_content=(
                "---\n"
                "name: developer\n"
                "triggers: [git]\n"
                "description: Git workflows\n"
                "---\n"
                "\n"
                "# Developer\n\nDo git things.\n"
            ),
        )
        config_skills = tmp_path / "config_skills"
        config_skills.mkdir()
        index = load_skill_index(config_skills, bundled_dir=bundled)
        result = load_skills(config_skills, ["developer"], skill_index=index, bundled_dir=bundled)
        assert "triggers:" not in result
        assert "Do git things." in result

    def test_partial_frontmatter_only_overrides_present_fields(self, tmp_path):
        """Frontmatter with only triggers should override keywords but keep toml description."""
        bundled = self._make_skill_dir(
            tmp_path, "calendar",
            toml_content='description = "CalDAV operations"\nkeywords = ["calendar"]\n',
            md_content=(
                "---\n"
                "triggers: [calendar, event, meeting, appointment]\n"
                "---\n"
                "\n"
                "# Calendar\n"
            ),
        )
        config_skills = tmp_path / "config_skills"
        config_skills.mkdir()
        index = load_skill_index(config_skills, bundled_dir=bundled)
        assert index["calendar"].keywords == ["calendar", "event", "meeting", "appointment"]
        assert index["calendar"].description == "CalDAV operations"  # from toml

    def test_frontmatter_with_invalid_yaml_ignored(self, tmp_path):
        """If YAML frontmatter is malformed, fall back to toml values."""
        bundled = self._make_skill_dir(
            tmp_path, "email",
            toml_content='description = "Email"\nkeywords = ["email"]\n',
            md_content=(
                "---\n"
                "triggers: [unclosed bracket\n"
                "---\n"
                "\n"
                "# Email\n"
            ),
        )
        config_skills = tmp_path / "config_skills"
        config_skills.mkdir()
        index = load_skill_index(config_skills, bundled_dir=bundled)
        # Should fall back gracefully
        assert index["email"].keywords == ["email"]
        assert index["email"].description == "Email"


class TestSemanticRoutingIntegration:
    """Tests for the end-to-end semantic routing flow."""

    def _make_index(self) -> dict[str, SkillMeta]:
        return {
            "files": SkillMeta(
                name="files",
                description="File ops",
                always_include=True,
            ),
            "developer": SkillMeta(
                name="developer",
                description="Git workflows, merge requests, worktrees",
                keywords=["git", "gitlab", "commit"],
            ),
            "email": SkillMeta(
                name="email",
                description="Email sending and response formatting",
                keywords=["email", "mail", "send"],
            ),
        }

    @patch("istota.skills._loader.subprocess.run")
    def test_pass2_adds_to_pass1_results(self, mock_run):
        """Semantic routing adds skills that keyword matching missed."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='["developer"]',
            stderr="",
        )
        index = self._make_index()
        # Pass 1: keyword matching
        pass1 = select_skills("fix the timezone bug", "talk", set(), index)
        assert "developer" not in pass1  # no keyword match

        # Pass 2: semantic routing
        extra = classify_skills(
            prompt="fix the timezone bug",
            skill_index=index,
            already_selected=set(pass1),
        )
        final = sorted(set(pass1) | set(extra))
        assert "developer" in final
        assert "files" in final  # still has always_include

    @patch("istota.skills._loader.subprocess.run")
    def test_pass2_failure_preserves_pass1(self, mock_run):
        """If semantic routing fails, keyword results are unchanged."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=3.0)
        index = self._make_index()
        pass1 = select_skills("send an email", "talk", set(), index)
        extra = classify_skills(
            prompt="send an email",
            skill_index=index,
            already_selected=set(pass1),
        )
        final = sorted(set(pass1) | set(extra))
        assert final == pass1  # unchanged
