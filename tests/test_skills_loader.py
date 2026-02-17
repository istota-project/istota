"""Configuration loading for istota.skills_loader module."""

from pathlib import Path

from pathlib import Path

from istota.skills_loader import (
    SkillMeta,
    compute_skills_fingerprint,
    load_skill_index,
    load_skills,
    load_skills_changelog,
    select_skills,
)

# Import from worktree source for new functions not yet in installed package
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    "istota.skills_loader_dev",
    Path(__file__).parent.parent / "src" / "istota" / "skills_loader.py",
)
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_get_attachment_extensions = _mod._get_attachment_extensions
# Override with worktree versions (have new file_types support)
select_skills = _mod.select_skills
SkillMeta = _mod.SkillMeta
load_skill_index = _mod.load_skill_index


def _write_index(skills_dir: Path, content: str) -> Path:
    """Helper to write _index.toml in a skills directory."""
    skills_dir.mkdir(parents=True, exist_ok=True)
    index = skills_dir / "_index.toml"
    index.write_text(content)
    return skills_dir


def _write_skill(skills_dir: Path, name: str, content: str) -> Path:
    """Helper to write a skill .md file."""
    skills_dir.mkdir(parents=True, exist_ok=True)
    p = skills_dir / f"{name}.md"
    p.write_text(content)
    return p


class TestLoadSkillIndex:
    def test_load_index_parses_skills(self, tmp_path):
        skills_dir = _write_index(tmp_path / "skills", (
            '[files]\n'
            'description = "Nextcloud file operations"\n'
            'always_include = true\n'
            '\n'
            '[calendar]\n'
            'description = "CalDAV operations"\n'
            'resource_types = ["calendar"]\n'
            '\n'
            '[email]\n'
            'description = "Email formatting"\n'
            'keywords = ["email", "mail"]\n'
        ))
        index = load_skill_index(skills_dir)
        assert len(index) == 3
        assert index["files"].name == "files"
        assert index["files"].description == "Nextcloud file operations"
        assert index["files"].always_include is True
        assert index["calendar"].resource_types == ["calendar"]
        assert index["email"].keywords == ["email", "mail"]

    def test_load_index_defaults(self, tmp_path):
        skills_dir = _write_index(tmp_path / "skills", (
            '[minimal]\n'
            'description = "Bare skill"\n'
        ))
        index = load_skill_index(skills_dir)
        meta = index["minimal"]
        assert meta.always_include is False
        assert meta.keywords == []
        assert meta.resource_types == []
        assert meta.source_types == []

    def test_load_index_missing_file(self, tmp_path):
        index = load_skill_index(tmp_path / "nonexistent")
        assert index == {}

    def test_load_index_empty_file(self, tmp_path):
        skills_dir = _write_index(tmp_path / "skills", "")
        index = load_skill_index(skills_dir)
        assert index == {}


class TestSelectSkills:
    def _make_index(self) -> dict[str, SkillMeta]:
        return {
            "files": SkillMeta(
                name="files",
                description="File ops",
                always_include=True,
            ),
            "calendar": SkillMeta(
                name="calendar",
                description="CalDAV",
                resource_types=["calendar"],
            ),
            "markets": SkillMeta(
                name="markets",
                description="Market data",
                source_types=["briefing"],
            ),
            "email": SkillMeta(
                name="email",
                description="Email",
                keywords=["email", "mail", "send"],
            ),
            "schedules": SkillMeta(
                name="schedules",
                description="Scheduled jobs",
                keywords=["schedule", "recurring", "cron"],
            ),
            "nextcloud": SkillMeta(
                name="nextcloud",
                description="Nextcloud OCS API",
                keywords=["share", "sharing", "shared", "public link", "unshare", "nextcloud", "permission", "access"],
            ),
        }

    def test_always_include(self):
        index = self._make_index()
        result = select_skills("hello", "talk", set(), index)
        assert "files" in result

    def test_source_type_match(self):
        index = self._make_index()
        result = select_skills("generate briefing", "briefing", set(), index)
        assert "markets" in result

    def test_resource_type_match(self):
        index = self._make_index()
        result = select_skills("what is next", "talk", {"calendar"}, index)
        assert "calendar" in result

    def test_keyword_match(self):
        index = self._make_index()
        result = select_skills("send an email to bob", "talk", set(), index)
        assert "email" in result

    def test_keyword_case_insensitive(self):
        index = self._make_index()
        result = select_skills("Send an EMAIL to bob", "talk", set(), index)
        assert "email" in result

    def test_keyword_match_nextcloud_share(self):
        index = self._make_index()
        result = select_skills("share my report with bob", "talk", set(), index)
        assert "nextcloud" in result

    def test_keyword_match_nextcloud_public_link(self):
        index = self._make_index()
        result = select_skills("create a public link for this file", "talk", set(), index)
        assert "nextcloud" in result

    def test_no_match(self):
        index = self._make_index()
        result = select_skills("hello there", "talk", set(), index)
        # Only always_include should be present
        assert result == ["files"]

    def test_multiple_criteria(self):
        index = self._make_index()
        result = select_skills(
            "send email about schedule",
            "briefing",
            {"calendar"},
            index,
        )
        assert "files" in result      # always_include
        assert "calendar" in result   # resource_type
        assert "markets" in result    # source_type
        assert "email" in result      # keyword
        assert "schedules" in result  # keyword

    def test_returns_sorted(self):
        index = self._make_index()
        result = select_skills(
            "send email about schedule",
            "briefing",
            {"calendar"},
            index,
        )
        assert result == sorted(result)


class TestLoadSkills:
    def test_load_existing_skills(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _write_skill(skills_dir, "files", "File operations guide.")
        result = load_skills(skills_dir, ["files"])
        assert "## Skills Reference" in result
        assert "### Files" in result
        assert "File operations guide." in result

    def test_load_missing_skill_skipped(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        result = load_skills(skills_dir, ["nonexistent"])
        assert result == ""

    def test_load_empty_returns_empty(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        result = load_skills(skills_dir, [])
        assert result == ""

    def test_load_formats_headers(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _write_skill(skills_dir, "sensitive-actions", "Be careful with destructive ops.")
        result = load_skills(skills_dir, ["sensitive-actions"])
        assert "### Sensitive Actions" in result

    def test_load_multiple_skills(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _write_skill(skills_dir, "files", "File ops.")
        _write_skill(skills_dir, "calendar", "Calendar ops.")
        result = load_skills(skills_dir, ["files", "calendar"])
        assert "### Files" in result
        assert "### Calendar" in result
        assert "File ops." in result
        assert "Calendar ops." in result

    def test_skill_title_formatting(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _write_skill(skills_dir, "my-cool-skill", "Content here.")
        result = load_skills(skills_dir, ["my-cool-skill"])
        assert "### My Cool Skill" in result

    def test_load_skills_includes_fingerprint_in_header(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _write_skill(skills_dir, "files", "File ops.")
        result = load_skills(skills_dir, ["files"])
        assert result.startswith("## Skills Reference (v: ")
        assert ")" in result.split("\n")[0]


class TestComputeSkillsFingerprint:
    def test_deterministic(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _write_index(skills_dir, '[files]\ndescription = "File ops"\n')
        _write_skill(skills_dir, "files", "File operations guide.")
        fp1 = compute_skills_fingerprint(skills_dir)
        fp2 = compute_skills_fingerprint(skills_dir)
        assert fp1 == fp2

    def test_returns_12_char_hex(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        fp = compute_skills_fingerprint(skills_dir)
        assert len(fp) == 12
        assert all(c in "0123456789abcdef" for c in fp)

    def test_changes_when_skill_content_changes(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _write_skill(skills_dir, "files", "Original content.")
        fp1 = compute_skills_fingerprint(skills_dir)
        _write_skill(skills_dir, "files", "Updated content.")
        fp2 = compute_skills_fingerprint(skills_dir)
        assert fp1 != fp2

    def test_changes_when_index_changes(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _write_index(skills_dir, '[files]\ndescription = "v1"\n')
        fp1 = compute_skills_fingerprint(skills_dir)
        _write_index(skills_dir, '[files]\ndescription = "v2"\n')
        fp2 = compute_skills_fingerprint(skills_dir)
        assert fp1 != fp2

    def test_changes_when_new_skill_added(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _write_skill(skills_dir, "files", "File ops.")
        fp1 = compute_skills_fingerprint(skills_dir)
        _write_skill(skills_dir, "calendar", "Calendar ops.")
        fp2 = compute_skills_fingerprint(skills_dir)
        assert fp1 != fp2

    def test_empty_dir(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        fp = compute_skills_fingerprint(skills_dir)
        assert len(fp) == 12


class TestLoadSkillsChangelog:
    def test_returns_content_when_exists(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "CHANGELOG.md").write_text("# Changelog\n\n## v1\n- New feature")
        result = load_skills_changelog(skills_dir)
        assert result is not None
        assert "# Changelog" in result

    def test_returns_none_when_missing(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        result = load_skills_changelog(skills_dir)
        assert result is None

    def test_returns_none_when_empty(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "CHANGELOG.md").write_text("")
        result = load_skills_changelog(skills_dir)
        assert result is None

    def test_returns_none_when_whitespace_only(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "CHANGELOG.md").write_text("   \n  \n  ")
        result = load_skills_changelog(skills_dir)
        assert result is None


class TestAdminOnlySkills:
    def _make_index(self) -> dict[str, SkillMeta]:
        return {
            "files": SkillMeta(
                name="files",
                description="File ops",
                always_include=True,
            ),
            "schedules": SkillMeta(
                name="schedules",
                description="Scheduled jobs",
                keywords=["schedule", "cron"],
                admin_only=True,
            ),
            "tasks": SkillMeta(
                name="tasks",
                description="Subtask creation",
                keywords=["subtask", "queue"],
                admin_only=True,
            ),
            "email": SkillMeta(
                name="email",
                description="Email",
                keywords=["email"],
            ),
        }

    def test_admin_only_included_for_admin(self):
        index = self._make_index()
        result = select_skills("set up a cron schedule", "talk", set(), index, is_admin=True)
        assert "schedules" in result

    def test_admin_only_excluded_for_non_admin(self):
        index = self._make_index()
        result = select_skills("set up a cron schedule", "talk", set(), index, is_admin=False)
        assert "schedules" not in result
        assert "tasks" not in result

    def test_non_admin_still_gets_always_include(self):
        index = self._make_index()
        result = select_skills("hello", "talk", set(), index, is_admin=False)
        assert "files" in result

    def test_non_admin_still_gets_keyword_match(self):
        index = self._make_index()
        result = select_skills("send email", "talk", set(), index, is_admin=False)
        assert "email" in result

    def test_admin_only_parsed_from_index(self, tmp_path):
        skills_dir = _write_index(tmp_path / "skills", (
            '[schedules]\n'
            'description = "Scheduled jobs"\n'
            'keywords = ["schedule"]\n'
            'admin_only = true\n'
        ))
        index = load_skill_index(skills_dir)
        assert index["schedules"].admin_only is True

    def test_admin_only_default_false(self, tmp_path):
        skills_dir = _write_index(tmp_path / "skills", (
            '[email]\n'
            'description = "Email"\n'
        ))
        index = load_skill_index(skills_dir)
        assert index["email"].admin_only is False


class TestGetAttachmentExtensions:
    def test_extracts_extensions(self):
        result = _get_attachment_extensions(["/path/to/file.mp3", "/path/to/image.PNG"])
        assert result == {"mp3", "png"}

    def test_handles_none(self):
        assert _get_attachment_extensions(None) == set()

    def test_handles_empty_list(self):
        assert _get_attachment_extensions([]) == set()

    def test_handles_no_extension(self):
        result = _get_attachment_extensions(["/path/to/Makefile"])
        assert result == set()

    def test_handles_relative_paths(self):
        result = _get_attachment_extensions(["Talk/recording.ogg"])
        assert result == {"ogg"}

    def test_handles_complex_filenames(self):
        result = _get_attachment_extensions([
            "/srv/mount/nextcloud/content/Talk/Talk recording from 2026-02-13 13-24-29 (#istotadev).mp3"
        ])
        assert result == {"mp3"}


class TestFileTypeSelection:
    def _make_index(self) -> dict[str, SkillMeta]:
        return {
            "files": SkillMeta(
                name="files",
                description="File ops",
                always_include=True,
            ),
            "whisper": SkillMeta(
                name="whisper",
                description="Audio transcription",
                keywords=["transcribe", "audio", "voice"],
                file_types=["mp3", "wav", "ogg", "flac", "m4a"],
            ),
            "transcribe": SkillMeta(
                name="transcribe",
                description="OCR transcription",
                keywords=["transcribe", "ocr", "screenshot"],
                file_types=["png", "jpg", "jpeg", "gif", "webp"],
            ),
            "email": SkillMeta(
                name="email",
                description="Email",
                keywords=["email", "mail"],
            ),
        }

    def test_audio_attachment_triggers_whisper(self):
        index = self._make_index()
        result = select_skills(
            "check this out", "talk", set(), index,
            attachments=["/path/to/recording.mp3"],
        )
        assert "whisper" in result

    def test_image_attachment_triggers_transcribe(self):
        index = self._make_index()
        result = select_skills(
            "", "talk", set(), index,
            attachments=["/path/to/screenshot.png"],
        )
        assert "transcribe" in result

    def test_no_attachments_no_file_type_match(self):
        index = self._make_index()
        result = select_skills("check this out", "talk", set(), index)
        assert "whisper" not in result
        assert "transcribe" not in result

    def test_unrelated_attachment_no_match(self):
        index = self._make_index()
        result = select_skills(
            "here's a file", "talk", set(), index,
            attachments=["/path/to/document.pdf"],
        )
        assert "whisper" not in result
        assert "transcribe" not in result

    def test_keyword_still_works_without_attachment(self):
        index = self._make_index()
        result = select_skills(
            "transcribe this audio", "talk", set(), index,
        )
        assert "whisper" in result

    def test_file_type_case_insensitive(self):
        index = self._make_index()
        result = select_skills(
            "", "talk", set(), index,
            attachments=["/path/to/RECORDING.MP3"],
        )
        assert "whisper" in result

    def test_multiple_attachments_mixed_types(self):
        index = self._make_index()
        result = select_skills(
            "", "talk", set(), index,
            attachments=["/path/to/audio.wav", "/path/to/image.jpg"],
        )
        assert "whisper" in result
        assert "transcribe" in result

    def test_file_types_parsed_from_index(self, tmp_path):
        skills_dir = _write_index(tmp_path / "skills", (
            '[whisper]\n'
            'description = "Audio transcription"\n'
            'keywords = ["audio"]\n'
            'file_types = ["mp3", "wav"]\n'
        ))
        index = load_skill_index(skills_dir)
        assert index["whisper"].file_types == ["mp3", "wav"]

    def test_file_types_default_empty(self, tmp_path):
        skills_dir = _write_index(tmp_path / "skills", (
            '[email]\n'
            'description = "Email"\n'
        ))
        index = load_skill_index(skills_dir)
        assert index["email"].file_types == []
