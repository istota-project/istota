"""Tests for progressive disclosure executor wiring (Part A, Stage 3)."""

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from istota import db
from istota.config import Config, SkillsConfig, UserConfig
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


# ---- execute_task dry-run: real partition path -----------------------------


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
    bundled: Path, name: str, body: str, *, disclosure: str = "", triggers=None,
    cli=True, exclude_skills=None, always_include=False, admin_only=False,
    experimental=False, dependencies=None,
):
    d = bundled / name
    d.mkdir(parents=True, exist_ok=True)
    fm = ["---", f"name: {name}", "description: the {0} skill".format(name), f"cli: {'true' if cli else 'false'}"]
    if disclosure:
        fm.append(f"disclosure: {disclosure}")
    if triggers:
        fm.append(f"triggers: [{', '.join(triggers)}]")
    if exclude_skills:
        fm.append(f"exclude_skills: [{', '.join(exclude_skills)}]")
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


def _disclosure_config(tmp_path, *, progressive: bool) -> Config:
    bundled = tmp_path / "bundled"
    _write_skill(
        bundled, "developer", "DEVELOPER_BODY_MARKER detailed git instructions",
        disclosure="lazy", triggers=["deploy"],
    )
    _write_skill(
        bundled, "calendar", "CALENDAR_BODY_MARKER scheduling instructions",
        triggers=["deploy"],
    )
    skills_dir = tmp_path / "ops_skills"
    skills_dir.mkdir(parents=True)
    db.init_db(tmp_path / "t.db")
    return Config(
        db_path=tmp_path / "t.db",
        skills_dir=skills_dir,
        bundled_skills_dir=bundled,
        temp_dir=tmp_path / "temp",
        users={"alice": UserConfig()},
        skills=SkillsConfig(
            progressive_disclosure=progressive,
            always_eager=[],
        ),
    )


def _run_dry(config) -> str:
    with ExitStack() as stack:
        for target, ret in _HEAVY_PATCHES:
            stack.enter_context(patch(target, return_value=ret))
        success, result, _a, _t = execute_task(_task(), config, [], dry_run=True)
    assert success
    return result


class TestExecuteTaskDisclosure:
    def test_lazy_skill_deferred_when_progressive_on(self, tmp_path):
        config = _disclosure_config(tmp_path, progressive=True)
        prompt = _run_dry(config)
        # developer is lazy → only an index line, no body.
        assert "Available skills (load on demand)" in prompt
        assert "  - developer:" in prompt
        assert "DEVELOPER_BODY_MARKER" not in prompt
        # calendar is eager → full body present.
        assert "CALENDAR_BODY_MARKER" in prompt

    def test_all_eager_when_progressive_off(self, tmp_path):
        config = _disclosure_config(tmp_path, progressive=False)
        prompt = _run_dry(config)
        # No index section; both bodies fully present.
        assert "load on demand" not in prompt
        assert "DEVELOPER_BODY_MARKER" in prompt
        assert "CALENDAR_BODY_MARKER" in prompt


# ---- widened catalogue index (Pass 2 replacement) --------------------------


def _catalogue_config(tmp_path, *, always_eager=None) -> Config:
    """Bundled set with selected (developer/calendar) + unselected eligible
    (bookmarks), an excluded skill (devbox, excluded by calendar), a pinned
    always_eager skill (safety), and a gated-off experimental skill (labrat)."""
    bundled = tmp_path / "bundled"
    _write_skill(bundled, "developer", "DEVELOPER_BODY_MARKER", disclosure="lazy", triggers=["deploy"])
    _write_skill(
        bundled, "calendar", "CALENDAR_BODY_MARKER", triggers=["deploy"],
        exclude_skills=["devbox"],
    )
    _write_skill(bundled, "bookmarks", "BOOKMARKS_BODY_MARKER")        # unselected, eligible
    _write_skill(bundled, "devbox", "DEVBOX_BODY_MARKER")              # unselected, excluded by calendar
    _write_skill(bundled, "safety", "SAFETY_BODY_MARKER")              # unselected, always_eager
    _write_skill(bundled, "labrat", "LABRAT_BODY_MARKER", experimental=True)  # gated off
    skills_dir = tmp_path / "ops_skills"
    skills_dir.mkdir(parents=True)
    db.init_db(tmp_path / "t.db")
    return Config(
        db_path=tmp_path / "t.db",
        skills_dir=skills_dir,
        bundled_skills_dir=bundled,
        temp_dir=tmp_path / "temp",
        users={"alice": UserConfig()},
        skills=SkillsConfig(
            progressive_disclosure=True,
            always_eager=always_eager if always_eager is not None else ["safety"],
        ),
    )


class TestWidenedCatalogueIndex:
    def test_unselected_eligible_skill_in_index(self, tmp_path):
        prompt = _run_dry(_catalogue_config(tmp_path))
        # bookmarks was never selected, but appears in the on-demand catalogue.
        assert "Available skills (load on demand)" in prompt
        assert "  - bookmarks:" in prompt
        assert "BOOKMARKS_BODY_MARKER" not in prompt  # index entry only, no body

    def test_selected_lazy_skill_still_indexed(self, tmp_path):
        prompt = _run_dry(_catalogue_config(tmp_path))
        assert "  - developer:" in prompt
        assert "DEVELOPER_BODY_MARKER" not in prompt

    def test_eager_selected_skill_full_body_not_indexed(self, tmp_path):
        prompt = _run_dry(_catalogue_config(tmp_path))
        assert "CALENDAR_BODY_MARKER" in prompt
        assert "  - calendar:" not in prompt

    def test_excluded_skill_absent_from_index(self, tmp_path):
        prompt = _run_dry(_catalogue_config(tmp_path))
        # calendar excludes devbox → devbox is not surfaced anywhere.
        assert "  - devbox:" not in prompt
        assert "DEVBOX_BODY_MARKER" not in prompt

    def test_always_eager_skill_absent_from_index(self, tmp_path):
        prompt = _run_dry(_catalogue_config(tmp_path))
        # safety is pinned always_eager → never offered for on-demand load.
        assert "  - safety:" not in prompt

    def test_experimental_gated_skill_absent_from_index(self, tmp_path):
        prompt = _run_dry(_catalogue_config(tmp_path))
        assert "  - labrat:" not in prompt

    def test_skillsconfig_defaults(self):
        # Progressive disclosure is the default selection model; the removed
        # Pass-2 semantic-routing knobs are gone.
        cfg = SkillsConfig()
        assert cfg.progressive_disclosure is True
        assert not hasattr(cfg, "semantic_routing")
