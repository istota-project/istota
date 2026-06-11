"""Tests for the single-axis skill model (selection + on-demand menu).

The former two-axis "disclosure" machinery (resolve_disclosure_mode /
partition_skills_for_disclosure / SkillMeta.disclosure / SkillsConfig) was
removed when selection collapsed into a single axis. What remains here covers
the building blocks the executor wires together to build the on-demand menu:
``eligible_skill_names`` (membership + gating), ``build_disclosure_index``
(menu output shape), and ``expand_companions`` (companion resolution).
"""

from istota.skills._loader import (
    build_disclosure_index,
    eligible_skill_names,
    expand_companions,
)
from istota.skills._types import SkillMeta


def _index() -> dict[str, SkillMeta]:
    return {
        "files": SkillMeta(name="files", description="File ops", always_include=True),
        "developer": SkillMeta(name="developer", description="Git and repos"),
        "health": SkillMeta(name="health", description="Body stats"),
        "tasks": SkillMeta(name="tasks", description="Subtasks", admin_only=True),
        "broken": SkillMeta(
            name="broken", description="Broken", dependencies=["nonexistent_pkg_xyz"]
        ),
        "labrat": SkillMeta(name="labrat", description="Experimental", experimental=True),
    }


class TestEligibleSkillNames:
    def test_includes_plain_unselected_skill(self):
        names = eligible_skill_names(_index(), exclude=set())
        assert "developer" in names
        assert "health" in names

    def test_excludes_already_selected(self):
        names = eligible_skill_names(_index(), exclude={"developer"})
        assert "developer" not in names
        assert "health" in names

    def test_excludes_always_include(self):
        names = eligible_skill_names(_index(), exclude=set())
        assert "files" not in names

    def test_excludes_disabled(self):
        names = eligible_skill_names(_index(), exclude=set(), disabled_skills={"developer"})
        assert "developer" not in names

    def test_excludes_admin_only_for_non_admin(self):
        assert "tasks" not in eligible_skill_names(_index(), exclude=set(), is_admin=False)
        assert "tasks" in eligible_skill_names(_index(), exclude=set(), is_admin=True)

    def test_excludes_missing_deps(self):
        assert "broken" not in eligible_skill_names(_index(), exclude=set())

    def test_excludes_experimental_unless_flagged(self):
        assert "labrat" not in eligible_skill_names(_index(), exclude=set())
        flagged = eligible_skill_names(
            _index(), exclude=set(),
            enabled_experimental_features=frozenset({"skill_labrat"}),
        )
        assert "labrat" in flagged

    def test_sorted(self):
        names = eligible_skill_names(_index(), exclude=set())
        assert names == sorted(names)


class TestBuildDisclosureIndex:
    def test_empty_returns_empty_string(self):
        assert build_disclosure_index([], {}) == ""

    def test_format_one_line_per_skill(self):
        index = {
            "developer": SkillMeta(name="developer", description="Git and repos"),
            "health": SkillMeta(name="health", description="Body stats"),
        }
        out = build_disclosure_index(["health", "developer"], index)
        assert "istota-skill skills show <name>" in out
        # Sorted, one line each.
        assert "  - developer: Git and repos" in out
        assert "  - health: Body stats" in out
        assert out.index("developer") < out.index("health")

    def test_header_present_when_names_given(self):
        index = {"developer": SkillMeta(name="developer", description="Git")}
        out = build_disclosure_index(["developer"], index)
        assert out.startswith("- Available skills (load on demand)")

    def test_missing_meta_renders_empty_description(self):
        # A name absent from the index still renders a line (blank desc).
        out = build_disclosure_index(["ghost"], {})
        assert "  - ghost: " in out


class TestExpandCompanions:
    def _comp_index(self) -> dict[str, SkillMeta]:
        return {
            "ingest": SkillMeta(
                name="ingest", description="Ingest",
                companion_skills=["untrusted", "labrat"],
            ),
            "untrusted": SkillMeta(name="untrusted", description="Safety doc"),
            "labrat": SkillMeta(name="labrat", description="Experimental", experimental=True),
        }

    def test_one_level_resolution(self):
        assert "untrusted" in expand_companions(["ingest"], self._comp_index())

    def test_excludes_names_already_in_input(self):
        result = expand_companions(["ingest", "untrusted"], self._comp_index())
        assert "untrusted" not in result

    def test_gates_experimental(self):
        assert "labrat" not in expand_companions(["ingest"], self._comp_index())
        flagged = expand_companions(
            ["ingest"], self._comp_index(),
            enabled_experimental_features=frozenset({"skill_labrat"}),
        )
        assert "labrat" in flagged
