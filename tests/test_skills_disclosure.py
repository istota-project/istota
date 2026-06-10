"""Tests for progressive skill disclosure (Part A, Stage 1).

Covers resolve_disclosure_mode / partition_skills_for_disclosure /
build_disclosure_index in istota.skills._loader.
"""

from pathlib import Path

import pytest

from istota.config import SkillsConfig
from istota.skills._loader import (
    build_disclosure_index,
    partition_skills_for_disclosure,
    reset_disclosure_warnings,
    resolve_disclosure_mode,
)
from istota.skills._types import SkillMeta


@pytest.fixture(autouse=True)
def _clear_disclosure_warnings():
    """The always_eager-override warning is warn-once per process; clear the
    cache around each test so an assertion on the warning is deterministic
    regardless of test order under xdist."""
    reset_disclosure_warnings()
    yield
    reset_disclosure_warnings()


def _meta(name, *, cli=True, disclosure="", description="desc"):
    return SkillMeta(name=name, description=description, cli=cli, disclosure=disclosure)


def _write_skill_body(base: Path, name: str, body: str, disclosure: str = "", cli: bool = True) -> SkillMeta:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    fm = ["---", f"name: {name}", "description: a skill", f"cli: {'true' if cli else 'false'}"]
    if disclosure:
        fm.append(f"disclosure: {disclosure}")
    fm.append("---")
    (d / "skill.md").write_text("\n".join(fm) + "\n" + body)
    return SkillMeta(name=name, description="a skill", cli=cli, disclosure=disclosure, skill_dir=str(d))


class TestResolveDisclosureMode:
    def test_frontmatter_lazy_wins(self):
        cfg = SkillsConfig(always_eager=[])
        assert resolve_disclosure_mode("developer", _meta("developer", disclosure="lazy"), 20000, cfg) == "lazy"

    def test_frontmatter_eager_wins_over_threshold(self):
        cfg = SkillsConfig(auto_lazy_threshold_chars=100, always_eager=[])
        # Body well over threshold, but frontmatter says eager.
        assert resolve_disclosure_mode("developer", _meta("developer", disclosure="eager"), 20000, cfg) == "eager"

    def test_threshold_makes_cli_skill_lazy(self):
        cfg = SkillsConfig(auto_lazy_threshold_chars=100, always_eager=[])
        assert resolve_disclosure_mode("health", _meta("health"), 5000, cfg) == "lazy"

    def test_under_threshold_is_eager(self):
        cfg = SkillsConfig(auto_lazy_threshold_chars=100, always_eager=[])
        assert resolve_disclosure_mode("health", _meta("health"), 50, cfg) == "eager"

    def test_threshold_zero_means_no_auto_lazy(self):
        cfg = SkillsConfig(auto_lazy_threshold_chars=0, always_eager=[])
        assert resolve_disclosure_mode("health", _meta("health"), 999999, cfg) == "eager"

    def test_non_cli_can_be_lazy_via_frontmatter(self):
        # No-CLI carve-out was dropped: a doc-only skill (e.g. developer) that
        # explicitly opts into lazy is deferred; its body is pulled via
        # `istota-skill skills show`.
        cfg = SkillsConfig(always_eager=[])
        assert resolve_disclosure_mode("developer", _meta("developer", cli=False, disclosure="lazy"), 50000, cfg) == "lazy"

    def test_non_cli_not_auto_lazy_by_threshold(self):
        # The size-threshold path still requires a CLI — a no-CLI skill is not
        # silently deferred by size alone (only by explicit frontmatter).
        cfg = SkillsConfig(auto_lazy_threshold_chars=100, always_eager=[])
        assert resolve_disclosure_mode("sensitive_actions", _meta("sensitive_actions", cli=False), 50000, cfg) == "eager"

    def test_always_eager_forced_eager(self):
        cfg = SkillsConfig(always_eager=["memory"])
        assert resolve_disclosure_mode("memory", _meta("memory", disclosure="lazy"), 50000, cfg) == "eager"

    def test_default_skill_is_eager(self):
        cfg = SkillsConfig(always_eager=[])
        assert resolve_disclosure_mode("calendar", _meta("calendar"), 5000, cfg) == "eager"


class TestPartitionSkillsForDisclosure:
    def test_mixed_split(self, tmp_path):
        bundled = tmp_path / "bundled"
        m_dev = _write_skill_body(bundled, "developer", "x" * 5000, disclosure="lazy")
        m_cal = _write_skill_body(bundled, "calendar", "y" * 200)
        m_files = _write_skill_body(bundled, "files", "z" * 100)
        index = {"developer": m_dev, "calendar": m_cal, "files": m_files}
        cfg = SkillsConfig(always_eager=["files"])
        eager, lazy = partition_skills_for_disclosure(
            ["developer", "calendar", "files"], index, tmp_path / "ops", cfg, bundled_dir=bundled,
        )
        assert lazy == ["developer"]
        assert set(eager) == {"calendar", "files"}

    def test_order_preserved(self, tmp_path):
        bundled = tmp_path / "bundled"
        metas = {}
        for n in ("a", "b", "c"):
            metas[n] = _write_skill_body(bundled, n, "x" * 50)
        cfg = SkillsConfig(always_eager=[])
        eager, lazy = partition_skills_for_disclosure(
            ["c", "a", "b"], metas, tmp_path / "ops", cfg, bundled_dir=bundled,
        )
        assert eager == ["c", "a", "b"]
        assert lazy == []

    def test_unknown_skill_defaults_eager(self, tmp_path):
        cfg = SkillsConfig(always_eager=[])
        eager, lazy = partition_skills_for_disclosure(
            ["ghost"], {}, tmp_path / "ops", cfg, bundled_dir=tmp_path / "bundled",
        )
        assert eager == ["ghost"]
        assert lazy == []

    def test_body_read_failure_defaults_eager(self, tmp_path, monkeypatch):
        bundled = tmp_path / "bundled"
        m = _write_skill_body(bundled, "developer", "x" * 5000, disclosure="lazy")
        index = {"developer": m}
        cfg = SkillsConfig(always_eager=[])

        import istota.skills._loader as loader

        def _boom(*a, **k):
            raise OSError("disk gone")

        monkeypatch.setattr(loader, "_resolve_skill_doc_path", _boom)
        eager, lazy = partition_skills_for_disclosure(
            ["developer"], index, tmp_path / "ops", cfg, bundled_dir=bundled,
        )
        assert eager == ["developer"]
        assert lazy == []


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
