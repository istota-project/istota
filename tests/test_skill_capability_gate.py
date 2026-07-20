"""Tests for the declarative skill-capability gate.

A skill may declare ``requires_capability: [name]`` in its frontmatter. When the
backing service capability is not available in the loaded config (e.g. the
headless browser or the devbox container isn't deployed — the default in the
standalone install), the skill is folded into the effective disabled set so it
appears in neither eager selection nor the on-demand menu.

This generalizes the former hardcoded ``if not config.devbox.enabled``
special-case (which was duplicated in the executor and the skills CLI).
"""

from pathlib import Path

from istota.config import BrowserConfig, DevboxConfig
from istota.skills._loader import (
    _parse_frontmatter,
    capability_disabled_skills,
    effective_disabled_skills,
    load_skill_index,
)
from istota.skills._types import SkillMeta


def _write_skill_md(base_dir: Path, name: str, frontmatter: dict) -> Path:
    skill_dir = base_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in frontmatter.items():
        if isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, list):
            lines.append(f"{key}: [{', '.join(str(v) for v in value)}]")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append(f"# {name}\n")
    (skill_dir / "skill.md").write_text("\n".join(lines) + "\n")
    return skill_dir


def _empty_bundled(tmp_path: Path) -> Path:
    d = tmp_path / "bundled"
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestFrontmatterParsing:
    def test_requires_capability_parsed_from_frontmatter(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _write_skill_md(skills_dir, "browse", {
            "description": "Web browsing",
            "requires_capability": ["browser"],
        })
        index = load_skill_index(skills_dir, bundled_dir=_empty_bundled(tmp_path))
        assert index["browse"].requires_capability == ["browser"]

    def test_missing_field_defaults_empty(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _write_skill_md(skills_dir, "notes", {"description": "Notes"})
        index = load_skill_index(skills_dir, bundled_dir=_empty_bundled(tmp_path))
        assert index["notes"].requires_capability == []


class TestBlockStyleListParsing:
    """A gate keyed on a list field must not fail OPEN when the frontmatter is
    written as a block-style YAML list. The minimal parser now consumes them.
    """

    def test_block_style_requires_capability(self, tmp_path):
        skills_dir = tmp_path / "skills"
        d = skills_dir / "browse"
        d.mkdir(parents=True)
        (d / "skill.md").write_text(
            "---\n"
            "name: browse\n"
            "description: Web browsing\n"
            "requires_capability:\n"
            "  - browser\n"
            "  - vnc\n"
            "---\n# body\n"
        )
        index = load_skill_index(skills_dir, bundled_dir=_empty_bundled(tmp_path))
        assert index["browse"].requires_capability == ["browser", "vnc"]

    def test_block_style_parsed_by_frontmatter(self, tmp_path):
        p = tmp_path / "skill.md"
        p.write_text(
            "---\n"
            "name: x\n"
            "companion_skills:\n"
            "  - untrusted_input\n"
            "cli: true\n"
            "---\nbody\n"
        )
        fm = _parse_frontmatter(p)
        assert fm["companion_skills"] == ["untrusted_input"]
        assert fm["cli"] is True  # parsing resumes correctly after the block

    def test_empty_scalar_still_empty(self, tmp_path):
        p = tmp_path / "skill.md"
        p.write_text("---\nname: x\ndescription:\ncli: true\n---\nbody\n")
        fm = _parse_frontmatter(p)
        assert fm["description"] == ""
        assert fm["cli"] is True


class TestConfigAvailableCapabilities:
    def test_all_off_by_default(self, make_config):
        config = make_config()
        assert config.available_capabilities() == set()

    def test_browser_enabled(self, make_config):
        config = make_config(browser=BrowserConfig(enabled=True))
        assert "browser" in config.available_capabilities()
        assert "devbox" not in config.available_capabilities()

    def test_devbox_enabled(self, make_config):
        config = make_config(devbox=DevboxConfig(enabled=True))
        assert "devbox" in config.available_capabilities()
        assert "browser" not in config.available_capabilities()

    def test_both_enabled(self, make_config):
        config = make_config(
            browser=BrowserConfig(enabled=True),
            devbox=DevboxConfig(enabled=True),
        )
        assert config.available_capabilities() == {"browser", "devbox"}


class TestCapabilityDisabledSkills:
    def _index(self):
        return {
            "browse": SkillMeta(name="browse", description="", requires_capability=["browser"]),
            "devbox": SkillMeta(name="devbox", description="", requires_capability=["devbox"]),
            "notes": SkillMeta(name="notes", description=""),
        }

    def test_no_capabilities_disables_all_gated(self):
        disabled = capability_disabled_skills(self._index(), set())
        assert disabled == {"browse", "devbox"}

    def test_partial_capabilities(self):
        disabled = capability_disabled_skills(self._index(), {"browser"})
        assert disabled == {"devbox"}

    def test_all_capabilities_disables_none(self):
        disabled = capability_disabled_skills(self._index(), {"browser", "devbox"})
        assert disabled == set()

    def test_ungated_skill_never_disabled(self):
        disabled = capability_disabled_skills(self._index(), set())
        assert "notes" not in disabled

    def test_multi_capability_needs_all(self):
        index = {"x": SkillMeta(name="x", description="", requires_capability=["a", "b"])}
        assert capability_disabled_skills(index, {"a"}) == {"x"}
        assert capability_disabled_skills(index, {"a", "b"}) == set()


class TestEffectiveDisabledSkills:
    def _index(self):
        return {
            "browse": SkillMeta(name="browse", description="", requires_capability=["browser"]),
            "devbox": SkillMeta(name="devbox", description="", requires_capability=["devbox"]),
            "notes": SkillMeta(name="notes", description=""),
        }

    def test_capability_gate_folds_in(self, make_config):
        config = make_config()  # browser + devbox off
        disabled = effective_disabled_skills(config, "alice", self._index())
        assert "browse" in disabled
        assert "devbox" in disabled

    def test_available_capability_not_disabled(self, make_config):
        config = make_config(browser=BrowserConfig(enabled=True))
        disabled = effective_disabled_skills(config, "alice", self._index())
        assert "browse" not in disabled
        assert "devbox" in disabled

    def test_unions_instance_disabled(self, make_config):
        config = make_config(disabled_skills=["notes"])
        disabled = effective_disabled_skills(config, "alice", self._index())
        assert "notes" in disabled

    def test_unions_per_user_disabled(self, make_config, make_user_config):
        config = make_config(
            browser=BrowserConfig(enabled=True),
            devbox=DevboxConfig(enabled=True),
            users={"alice": make_user_config(disabled_skills=["notes"])},
        )
        disabled = effective_disabled_skills(config, "alice", self._index())
        assert disabled == {"notes"}

    def test_unknown_user_no_crash(self, make_config):
        config = make_config()
        disabled = effective_disabled_skills(config, "nobody", self._index())
        assert "browse" in disabled  # capability gate still applies


class TestRealBundledSkills:
    """End-to-end against the REAL browse/devbox skill.md frontmatter (not
    synthetic fixtures), proving the requires_capability edits took effect and
    that a standalone-shaped config (both services off) gates them.
    """

    def _real_index(self, tmp_path):
        ops = tmp_path / "ops"
        ops.mkdir()
        # bundled_dir=None → the real bundled skills shipped in the package.
        return load_skill_index(ops, bundled_dir=None)

    def test_real_skills_declare_capabilities(self, tmp_path):
        index = self._real_index(tmp_path)
        assert index["browse"].requires_capability == ["browser"]
        assert index["devbox"].requires_capability == ["devbox"]

    def test_standalone_config_gates_both(self, make_config, tmp_path):
        index = self._real_index(tmp_path)
        config = make_config()  # browser + devbox off = standalone shape
        assert config.available_capabilities() == set()
        disabled = effective_disabled_skills(config, "alice", index)
        assert "browse" in disabled
        assert "devbox" in disabled

    def test_services_on_ungates_both(self, make_config, tmp_path):
        index = self._real_index(tmp_path)
        config = make_config(
            browser=BrowserConfig(enabled=True),
            devbox=DevboxConfig(enabled=True),
        )
        disabled = effective_disabled_skills(config, "alice", index)
        assert "browse" not in disabled
        assert "devbox" not in disabled

    def test_per_capability_independence(self, make_config, tmp_path):
        index = self._real_index(tmp_path)
        config = make_config(browser=BrowserConfig(enabled=True))  # devbox off
        disabled = effective_disabled_skills(config, "alice", index)
        assert "browse" not in disabled
        assert "devbox" in disabled
