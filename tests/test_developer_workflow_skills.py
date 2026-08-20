"""The developer / commit / code_review document split.

These run against the *real* bundled skill tree rather than synthetic
``SkillMeta`` fixtures. The thing under test is what actually ships in
``src/istota/skills/``, so a fixture that restates the frontmatter would pass
whatever the shipped files happen to say.
"""

from pathlib import Path

import pytest

from istota.skills._loader import (
    _BUNDLED_SKILLS_DIR,
    build_disclosure_index,
    expand_companions,
    load_skill_index,
)


@pytest.fixture(scope="module")
def bundled_index():
    """The real bundled skill index, with no operator overrides."""
    return load_skill_index(Path("/nonexistent-operator-skills-dir"))


class TestDocumentSplit:
    def test_both_new_skills_load(self, bundled_index):
        assert "commit" in bundled_index
        assert "code_review" in bundled_index

    def test_developer_declares_its_companions(self, bundled_index):
        declared = bundled_index["developer"].companion_skills
        assert set(declared) == {"commit", "code_review", "untrusted_input"}

    def test_expand_companions_returns_all_three(self, bundled_index):
        # Order is the resolver's business; membership is the contract.
        companions = expand_companions(["developer"], bundled_index)
        assert set(companions) == {"commit", "code_review", "untrusted_input"}

    def test_code_review_carries_untrusted_input(self, bundled_index):
        """Findings are model text about a possibly-foreign diff."""
        assert "untrusted_input" in bundled_index["code_review"].companion_skills

    def test_developer_declares_untrusted_input_directly(self, bundled_index):
        """Companion expansion is one level (``_loader.expand_companions``), so
        code_review's own companions are NOT resolved when `developer` is the
        thing being pulled. Declaring it on code_review alone would mean the
        guardrails never arrive on the main path."""
        second_level = expand_companions(["code_review"], bundled_index)
        assert "untrusted_input" in second_level, "precondition: code_review declares it"

        first_level = expand_companions(["developer"], bundled_index)
        assert "untrusted_input" in first_level, (
            "developer must declare untrusted_input itself; one-level expansion "
            "will not reach it through code_review"
        )

    def test_code_review_is_admin_only(self, bundled_index):
        assert bundled_index["code_review"].admin_only is True

    def test_commit_is_not_admin_only(self, bundled_index):
        """A non-admin who can commit still needs the scrub rules."""
        assert bundled_index["commit"].admin_only is False

    def test_non_admin_developer_pull_drops_code_review(self, bundled_index):
        """admin_only gates companion expansion, so a non-admin gets the two
        that apply to them rather than a body whose CLI would refuse them."""
        companions = expand_companions(["developer"], bundled_index, is_admin=False)
        assert set(companions) == {"commit", "untrusted_input"}
        assert "code_review" not in companions


class TestMenuEntries:
    """`triggers` no longer select anything; `description` is the menu line
    (``_loader.build_disclosure_index``). A skill without one is a blank row."""

    def test_every_bundled_skill_has_a_description(self, bundled_index):
        missing = sorted(n for n, m in bundled_index.items() if not (m.description or "").strip())
        assert missing == [], f"skills with no description (blank menu row): {missing}"

    def test_new_skills_render_a_menu_line(self, bundled_index):
        menu = build_disclosure_index(["commit", "code_review"], bundled_index)
        assert "- commit: " in menu
        assert "- code_review: " in menu
        # A blank description would render as a bare "- name:" with nothing after.
        for name in ("commit", "code_review"):
            entry = next(ln for ln in menu.splitlines() if ln.strip().startswith(f"- {name}:"))
            assert entry.split(f"- {name}:", 1)[1].strip(), f"blank menu line: {entry!r}"


class TestCliFlagMatchesReality:
    """A `cli: true` skill is dispatched as `python -m istota.skills.<name>`
    (``skill_client``) and is added to the proxy's allowlist off the flag alone
    (``executor``). Declaring the flag before the module exists turns a menu
    entry into a ModuleNotFoundError the first time anyone pulls it."""

    def test_cli_skills_are_runnable_as_modules(self, bundled_index):
        broken = []
        for name, meta in sorted(bundled_index.items()):
            if not meta.cli:
                continue
            skill_dir = _BUNDLED_SKILLS_DIR / name
            if not (skill_dir / "__main__.py").exists():
                broken.append(name)
        assert broken == [], (
            "skills declaring `cli: true` with no __main__.py, so "
            f"`python -m istota.skills.<name>` cannot run: {broken}"
        )

    def test_code_review_defers_its_cli_flag(self, bundled_index):
        """Stage 1 ships the document only. Delete this at Stage 4, when the
        module lands; the general test above carries the invariant from then on."""
        assert bundled_index["code_review"].cli is False, (
            "`cli: true` belongs at Stage 4, when __main__.py exists"
        )


class TestBodiesDoNotContradict:
    """The three bodies arrive in one response, so a rule stated in one and
    broken in another is a live contradiction the model has to resolve. These
    are the ones that are mechanically checkable."""

    def _body(self, name):
        return (_BUNDLED_SKILLS_DIR / name / "skill.md").read_text()

    @pytest.mark.parametrize("name", ["developer", "commit", "code_review"])
    def test_no_blanket_staging(self, name):
        """`commit` forbids blanket staging; a worktree holds copied .env files
        and stray fixtures, and this repo is public. The first cut of the split
        left a `git add -A` behind in an untouched section of `developer`."""
        body = self._body(name)
        # Only a line that *is* the command counts. Prose forbidding it ("Never
        # `git add -A`") is the point of the rule, not a violation of it.
        offending = [
            line.strip()
            for line in body.splitlines()
            if line.strip().startswith(("git add -A", "git add --all", "git add ."))
        ]
        assert offending == [], f"{name} instructs blanket staging: {offending}"

    def test_commit_precedes_review_in_the_lifecycle(self):
        """The review resolves a commit range, so a lifecycle that numbers the
        review before the commit reviews an empty diff and comes back clean."""
        body = self._body("developer")
        commit_step = body.index("### 8. Commit")
        review_step = body.index("### 9. Review before landing")
        assert commit_step < review_step
    """The whole reason the split is safe: pulling `developer` from the menu
    delivers all three bodies in the same response, so the commit scrub rules
    and the review gate are never a second round trip the model may skip."""

    def _config(self, tmp_path, monkeypatch, *, user="alice", admins=("alice",)):
        from istota.config import Config, UserConfig

        config = Config(
            db_path=tmp_path / "istota.db",
            temp_dir=tmp_path / "tmp",
            nextcloud_mount_path=tmp_path,
            bundled_skills_dir=_BUNDLED_SKILLS_DIR,
            skills_dir=tmp_path / "operator_skills",
            users={user: UserConfig()},
            admin_users=set(admins),
        )
        monkeypatch.setattr("istota.config.load_config", lambda *a, **kw: config)
        monkeypatch.setenv("ISTOTA_USER_ID", user)
        monkeypatch.delenv("ISTOTA_EXPERIMENTAL_FEATURES", raising=False)
        return config

    def test_show_developer_carries_both_companion_bodies(self, tmp_path, monkeypatch, capsys):
        import argparse

        from istota.skills.skills import cmd_show

        self._config(tmp_path, monkeypatch)
        cmd_show(argparse.Namespace(name="developer"))
        out = capsys.readouterr().out

        assert "<!-- companion: commit -->" in out
        assert "<!-- companion: code_review -->" in out
        # Not just the delimiters — the bodies behind them.
        assert "Never `git add -A`" in out
        assert "A `skipped` review is not a clean review." in out
        # And the spine itself.
        assert "The Job Lifecycle" in out

    def test_non_admin_gets_commit_and_a_marker_for_code_review(
        self, tmp_path, monkeypatch, capsys
    ):
        import argparse

        from istota.skills.skills import cmd_show

        self._config(tmp_path, monkeypatch, user="alice", admins=("boss",))
        cmd_show(argparse.Namespace(name="developer"))
        out = capsys.readouterr().out

        assert "<!-- companion: commit -->" in out
        # Gated off, but announced rather than silently dropped.
        assert "<!-- companion code_review: unavailable -->" in out


class TestLoadBudget:
    """Goal 5: the split grows what a coding task loads, so the ceiling is
    stated rather than assumed. Parity with the pre-split single file is not
    achievable — `developer` sheds ~52 lines and gains more than that back."""

    BUDGET_LINES = 700

    def test_three_bodies_fit_the_budget(self):
        total = 0
        per_skill = {}
        for name in ("developer", "commit", "code_review"):
            body = (_BUNDLED_SKILLS_DIR / name / "skill.md").read_text()
            lines = len(body.splitlines())
            per_skill[name] = lines
            total += lines
        assert total <= self.BUDGET_LINES, (
            f"three-body bundle is {total} lines, over the {self.BUDGET_LINES} "
            f"budget: {per_skill}"
        )
