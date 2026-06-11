"""Tests for the single-axis skill model wiring in the executor.

A selected skill (an eager pick by a deterministic select_skills rule) gets its
full body inline; every other eligible skill is surfaced as a one-line menu
entry the model pulls in full via ``istota-skill skills show``. The menu is the
full eligible catalogue minus the eager set (and minus anything they exclude).
"""

import logging
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from istota import db
from istota.config import Config, UserConfig
from istota.executor import build_prompt, execute_task


def _task(**kw):
    defaults = dict(
        id=1, status="running", source_type="talk", user_id="alice",
        prompt="please deploy the service", conversation_token="room1",
    )
    defaults.update(kw)
    return db.Task(**defaults)


# ---- build_prompt: index injection / omission -----------------------------


class TestBuildPromptSkillsIndex:
    def _config(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(parents=True)
        return Config(
            db_path=tmp_path / "t.db",
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty",
            temp_dir=tmp_path / "temp",
        )

    def test_index_injected_after_cli_tools(self, tmp_path):
        config = self._config(tmp_path)
        prompt = build_prompt(
            _task(), [], config,
            cli_skills_text="- Skill CLI tools\n  - `istota-skill foo` — does foo",
            skills_index="- Available skills (load on demand)...\n  - developer: Git",
        )
        assert "Available skills (load on demand)" in prompt
        assert "developer: Git" in prompt
        # Comes after the CLI tools line.
        assert prompt.index("istota-skill foo") < prompt.index("Available skills (load on demand)")

    def test_index_omitted_when_none(self, tmp_path):
        config = self._config(tmp_path)
        prompt = build_prompt(_task(), [], config, cli_skills_text="- Skill CLI tools")
        assert "load on demand" not in prompt


# ---- execute_task dry-run: real selection + menu path ----------------------


_HEAVY_PATCHES = [
    ("istota.executor.select_relevant_context", []),
    ("istota.executor.read_user_memory_v2", None),
    ("istota.executor.ensure_user_directories_v2", None),
    ("istota.executor.read_channel_memory", None),
    ("istota.executor.ensure_channel_directories", None),
    ("istota.executor.get_caldav_client", None),
    ("istota.executor.get_calendars_for_user", []),
]


def _write_skill(
    bundled: Path, name: str, body: str, *, source_types=None,
    cli=True, exclude_skills=None, always_include=False, admin_only=False,
    experimental=False, dependencies=None, companion_skills=None, file_types=None,
):
    d = bundled / name
    d.mkdir(parents=True, exist_ok=True)
    fm = ["---", f"name: {name}", "description: the {0} skill".format(name), f"cli: {'true' if cli else 'false'}"]
    if source_types:
        fm.append(f"source_types: [{', '.join(source_types)}]")
    if file_types:
        fm.append(f"file_types: [{', '.join(file_types)}]")
    if exclude_skills:
        fm.append(f"exclude_skills: [{', '.join(exclude_skills)}]")
    if companion_skills:
        fm.append(f"companion_skills: [{', '.join(companion_skills)}]")
    if always_include:
        fm.append("always_include: true")
    if admin_only:
        fm.append("admin_only: true")
    if experimental:
        fm.append("experimental: true")
    if dependencies:
        fm.append(f"dependencies: [{', '.join(dependencies)}]")
    fm.append("---")
    (d / "skill.md").write_text("\n".join(fm) + "\n" + body)


def _base_config(tmp_path, *, devbox_enabled=True) -> Config:
    """Bundled set:

    - calendar    — selected eager (source_types=[talk]), excludes devbox
    - developer   — unselected, eligible → menu only
    - bookmarks   — unselected, eligible → menu only
    - devbox      — unselected, excluded by calendar → absent everywhere
    """
    bundled = tmp_path / "bundled"
    _write_skill(
        bundled, "calendar", "CALENDAR_BODY_MARKER scheduling instructions",
        source_types=["talk"], exclude_skills=["devbox"],
    )
    _write_skill(bundled, "developer", "DEVELOPER_BODY_MARKER detailed git instructions")
    _write_skill(bundled, "bookmarks", "BOOKMARKS_BODY_MARKER")
    _write_skill(bundled, "devbox", "DEVBOX_BODY_MARKER")
    skills_dir = tmp_path / "ops_skills"
    skills_dir.mkdir(parents=True)
    db.init_db(tmp_path / "t.db")
    config = Config(
        db_path=tmp_path / "t.db",
        skills_dir=skills_dir,
        bundled_skills_dir=bundled,
        temp_dir=tmp_path / "temp",
        users={"alice": UserConfig()},
    )
    config.devbox.enabled = devbox_enabled
    return config


def _run_dry(config) -> str:
    with ExitStack() as stack:
        for target, ret in _HEAVY_PATCHES:
            stack.enter_context(patch(target, return_value=ret))
        success, result, _a, _t = execute_task(_task(), config, [], dry_run=True)
    assert success
    return result


class TestExecuteTaskSelectionAndMenu:
    def test_selected_skill_body_inline(self, tmp_path):
        prompt = _run_dry(_base_config(tmp_path))
        # calendar is selected (source_type) → full body present, not in menu.
        assert "CALENDAR_BODY_MARKER" in prompt
        assert "  - calendar:" not in prompt

    def test_unselected_eligible_skill_in_menu_only(self, tmp_path):
        prompt = _run_dry(_base_config(tmp_path))
        # developer/bookmarks were never selected → menu entry, no body.
        assert "Available skills (load on demand)" in prompt
        assert "  - developer:" in prompt
        assert "DEVELOPER_BODY_MARKER" not in prompt
        assert "  - bookmarks:" in prompt
        assert "BOOKMARKS_BODY_MARKER" not in prompt

    def test_excluded_skill_absent_everywhere(self, tmp_path):
        prompt = _run_dry(_base_config(tmp_path))
        # calendar excludes devbox → not eager, not in menu.
        assert "  - devbox:" not in prompt
        assert "DEVBOX_BODY_MARKER" not in prompt

    def test_skills_log_line_emitted(self, tmp_path, caplog):
        config = _base_config(tmp_path)
        with caplog.at_level(logging.INFO, logger="istota.executor"):
            _run_dry(config)
        msgs = [r.message for r in caplog.records if r.message.startswith("skills: eager=")]
        assert msgs, "expected a 'skills: eager=N menu=M' log line"
        assert any("menu=" in m for m in msgs)


# ---- ingest companion: untrusted_input rides along an attachment-selected skill ----


def _ingest_config(tmp_path) -> Config:
    bundled = tmp_path / "bundled"
    _write_skill(
        bundled, "whisper", "WHISPER_BODY_MARKER audio instructions",
        file_types=["mp3", "wav"], companion_skills=["untrusted_input"],
    )
    _write_skill(bundled, "untrusted_input", "UNTRUSTED_INPUT_BODY_MARKER safety rules", cli=False)
    skills_dir = tmp_path / "ops_skills"
    skills_dir.mkdir(parents=True)
    db.init_db(tmp_path / "t.db")
    config = Config(
        db_path=tmp_path / "t.db",
        skills_dir=skills_dir,
        bundled_skills_dir=bundled,
        temp_dir=tmp_path / "temp",
        users={"alice": UserConfig()},
    )
    config.devbox.enabled = True
    return config


class TestIngestCompanionEager:
    def test_attachment_selected_ingest_pulls_untrusted_input_eager(self, tmp_path):
        config = _ingest_config(tmp_path)
        task = _task(attachments=["/path/to/memo.mp3"])
        with ExitStack() as stack:
            for target, ret in _HEAVY_PATCHES:
                stack.enter_context(patch(target, return_value=ret))
            success, prompt, _a, _t = execute_task(task, config, [], dry_run=True)
        assert success
        # whisper selected by file_type → body inline; its untrusted_input
        # companion is pulled in eager (body present, not just a menu entry).
        assert "WHISPER_BODY_MARKER" in prompt
        assert "UNTRUSTED_INPUT_BODY_MARKER" in prompt
        assert "  - untrusted_input:" not in prompt


# ---- devbox gating: disabled → absent from eager AND menu ------------------


class TestDevboxDisabledGate:
    def test_devbox_disabled_absent_from_eager_and_menu(self, tmp_path):
        # A bundled set where devbox is NOT excluded by any selected skill, so
        # the only thing keeping it out of the menu is the devbox-disabled gate.
        bundled = tmp_path / "bundled"
        _write_skill(bundled, "calendar", "CALENDAR_BODY_MARKER", source_types=["talk"])
        _write_skill(bundled, "devbox", "DEVBOX_BODY_MARKER")
        skills_dir = tmp_path / "ops_skills"
        skills_dir.mkdir(parents=True)
        db.init_db(tmp_path / "t.db")
        config = Config(
            db_path=tmp_path / "t.db",
            skills_dir=skills_dir,
            bundled_skills_dir=bundled,
            temp_dir=tmp_path / "temp",
            users={"alice": UserConfig()},
        )
        config.devbox.enabled = False
        prompt = _run_dry(config)
        assert "  - devbox:" not in prompt
        assert "DEVBOX_BODY_MARKER" not in prompt

    def test_devbox_enabled_appears_in_menu(self, tmp_path):
        bundled = tmp_path / "bundled"
        _write_skill(bundled, "calendar", "CALENDAR_BODY_MARKER", source_types=["talk"])
        _write_skill(bundled, "devbox", "DEVBOX_BODY_MARKER")
        skills_dir = tmp_path / "ops_skills"
        skills_dir.mkdir(parents=True)
        db.init_db(tmp_path / "t.db")
        config = Config(
            db_path=tmp_path / "t.db",
            skills_dir=skills_dir,
            bundled_skills_dir=bundled,
            temp_dir=tmp_path / "temp",
            users={"alice": UserConfig()},
        )
        config.devbox.enabled = True
        prompt = _run_dry(config)
        # devbox not selected, not excluded, devbox enabled → menu entry.
        assert "  - devbox:" in prompt
