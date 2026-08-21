"""The developer / commit / code_review document split.

These run against the *real* bundled skill tree rather than synthetic
``SkillMeta`` fixtures. The thing under test is what actually ships in
``src/istota/skills/``, so a fixture that restates the frontmatter would pass
whatever the shipped files happen to say.
"""

import os
import subprocess
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

    def test_code_review_declares_its_cli(self, bundled_index):
        """The flag is what puts the module in `cli_skills` and so in the
        proxy's allowlist. Stage 1 deliberately withheld it; the module exists
        now, and `test_cli_skills_are_runnable_as_modules` above carries the
        invariant that it stays runnable."""
        assert bundled_index["code_review"].cli is True

    def test_code_review_declares_the_api_key_it_needs(self, bundled_index):
        """A native-brain deployment authenticates from
        `ISTOTA_BRAIN_NATIVE_API_KEY`, which `config.py` reads at load time. The
        CLI does not run in the daemon process, so it does not inherit it — the
        env spec is the only thing that gets the key there.

        It must be `sensitive` and **not** `proxy_only`. The executor splits the
        proxy-only set out of the env *before* the credential set, so a var
        flagged both lands in `proxy_only_env` and never reaches
        `credential_env` — and `proxy_only_env` is handed to every skill CLI
        unscoped, on the stated grounds that those vars are not secrets. An API
        key is. Declared `sensitive` alone, injection is scoped to the skills
        whose own manifest asked for it, so only `code_review` sees it.
        """
        specs = {spec.var: spec for spec in bundled_index["code_review"].env_specs}
        assert set(specs) == {"DEVELOPER_REPOS_DIR", "ISTOTA_BRAIN_NATIVE_API_KEY"}

        api_key = specs["ISTOTA_BRAIN_NATIVE_API_KEY"]
        assert api_key.source == "config"
        assert api_key.config_path == "brain.native.api_key"
        assert api_key.sensitive is True
        assert api_key.proxy_only is False, (
            "proxy_only would leak the key to every other skill CLI and skip "
            "the scoped credential path entirely"
        )
        assert api_key.when == ["developer.enabled", "brain.native.api_key"]

        repos = specs["DEVELOPER_REPOS_DIR"]
        assert repos.config_path == "developer.repos_dir"
        assert repos.sensitive is False, "a directory path is not a credential"


def _fenced_lines(body: str):
    """Lines inside ``` blocks — the ones a model will copy and run.

    Toggling on `line.startswith` misses an indented fence, which is the normal
    way to put a recipe inside a list item; `developer` has two, in the Error
    Handling bullets. Miss those and every following line is classified
    inversely, so a guard that only inspects runnable lines silently starts
    inspecting prose instead.
    """
    in_fence = False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            yield line


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

    def test_a_runnable_skill_does_not_call_itself_unavailable(self, bundled_index):
        """Stage 1 shipped a `code_review` body headed "Status: not yet
        available", telling the model to skip the review and land the work. Turn
        `cli: true` on without rewriting that and the skill is simultaneously
        advertised in the proxy allowlist and documented as nonexistent — the
        model reads the body, so the body wins.
        """
        for name, meta in sorted(bundled_index.items()):
            if not meta.cli:
                continue
            body = self._body(name).lower()
            for claim in ("does not exist yet", "not yet available", "until it lands"):
                assert claim not in body, (
                    f"{name} declares `cli: true` but its body still says {claim!r}"
                )

    def test_code_review_tells_the_model_to_extend_the_bash_timeout(self):
        """The sandboxed model calls the CLI through its own Bash tool, whose
        limit is 120s. A two-agent review on a real diff exceeds that routinely,
        so the tool call dies while the review runs on and is paid for. The
        feature does not work at default settings without this instruction, which
        is why it is a spec requirement and not an implementation detail."""
        body = self._body("code_review")
        assert "timeout" in body.lower()
        assert "istota-skill code_review run" in body

    def test_no_retired_api_wrapper_variables(self):
        """`setup_env` stopped exporting `GITLAB_API_CMD` / `GITHUB_API_CMD`
        when the forge wrapper landed. A body still naming them tells the model
        to run `$GITHUB_API_CMD GET ...`, which expands to the empty string and
        executes `GET` as a command — an unset variable in a shell recipe fails
        as a typo, not as a missing feature."""
        body = self._body("developer")
        for var in ("GITLAB_API_CMD", "GITHUB_API_CMD"):
            assert var not in body, f"developer still references ${var}"

    def test_forge_verbs_are_the_real_cli(self):
        """The point of the wrapper: the model types what a person would type,
        and the full flag surface is reachable."""
        body = self._body("developer")
        for verb in ("gh pr create", "glab mr create", "gh pr checks"):
            assert verb in body, f"developer does not document `{verb}`"

    def test_reviewer_is_requested_not_reviewed(self):
        """The old recipe posted `{"reviewers": [...]}` to
        `POST /repos/:o/:r/pulls/:n/reviews`, which creates a *review*.
        Requesting a reviewer is `.../requested_reviewers`, which the endpoint
        allowlist did not admit — so the correct call was blocked and the wrong
        one never requested anybody. `gh pr create --reviewer` replaces both."""
        body = self._body("developer")
        assert "--reviewer" in body
        for line in _fenced_lines(body):
            assert "/reviews" not in line, (
                f"developer still posts to the pull-request reviews endpoint: {line!r}"
            )

    def test_does_not_advertise_run_download(self):
        """`gh run download` redirects to a per-request Azure blob shard, and
        the only allowlist entry covering it is `*.blob.core.windows.net` — all
        of Azure Blob Storage, reachable from the sandbox. So the verb must not
        appear in a runnable recipe: it would die at the CONNECT proxy. Naming
        it in prose is required rather than forbidden — the spec asks for the
        reason in writing so the next reader does not "fix" the allowlist."""
        body = self._body("developer")
        for line in _fenced_lines(body):
            assert "gh run download" not in line, (
                f"developer puts `gh run download` in a runnable block: {line!r}"
            )
        assert "gh run download" in body, (
            "developer should say why the verb is unavailable, not stay silent"
        )

    def test_credential_conduct_rule_describes_the_real_mechanism(self):
        """Defect 4. The old rule said tokens "are embedded in helper scripts
        and never exposed as environment variables", then told the model not to
        read them out. Both halves were wrong — there are no token-bearing
        helper scripts any more — and a rule that misdescribes its own mechanism
        teaches the model a false model of the boundary."""
        body = self._body("developer")
        assert "embedded in helper scripts" not in body
        # And the replacement is actually there. Without this, deleting the
        # whole Credentials paragraph leaves the test green, which is the
        # opposite of what defect 4 asks for — the spec says *replace* the
        # rule, so replacement is the property under test.
        assert "not a claim that you would be stopped" in body, (
            "the conduct rule must not imply the model would be prevented"
        )
        assert "accident guard, not a security boundary" in body, (
            "the refused-verb list must not be described as a boundary"
        )

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

    BUDGET_LINES = 675

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


# ---------------------------------------------------------------------------
# The bare-clone recipe, executed rather than described.

GIT_ISOLATION = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd), capture_output=True, text=True,
        env={**os.environ, **GIT_ISOLATION},
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{proc.stderr}")
    return proc.stdout


def _extract(marker: str, stop: str) -> str:
    """Lift a shell fragment out of the shipped `developer` body.

    The fragment is *run*, not restated, so a body edited back to the broken
    form fails these tests instead of quietly passing against a copy kept here.
    """
    body = (_BUNDLED_SKILLS_DIR / "developer" / "skill.md").read_text().splitlines()
    starts = [i for i, line in enumerate(body) if marker in line]
    assert len(starts) == 1, (
        f"expected exactly one {marker!r} in developer/skill.md, got {len(starts)}"
    )
    start = starts[0]
    end = next(i for i in range(start, len(body)) if stop in body[i])
    return "\n".join(body[start:end + 1])


def _run_fragment(fragment: str, bare: Path) -> str:
    proc = subprocess.run(
        ["bash", "-c", fragment],
        capture_output=True, text=True,
        env={**os.environ, **GIT_ISOLATION, "BARE_DIR": str(bare)},
    )
    assert proc.returncode == 0, f"fragment failed:\n{fragment}\n{proc.stderr}"
    return proc.stdout


@pytest.fixture
def bare_clone(tmp_path) -> Path:
    """A bare clone in the shape `developer/skill.md` documents: remote-tracking
    refspec configured, fetched, HEAD repointed at `refs/remotes/origin/*`."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main", ".")
    _git(upstream, "commit", "-q", "--allow-empty", "-m", "init")

    bare = tmp_path / "project.git"
    _git(tmp_path, "clone", "-q", "--bare", str(upstream), str(bare))
    _git(bare, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    _git(bare, "fetch", "-q", "origin")
    _git(bare, "symbolic-ref", "HEAD", "refs/remotes/origin/main")
    return bare


class TestBareCloneRecipe:
    """Two shell defects that shipped in the body and that no test could see,
    because the body was only ever read as text. Both surfaced by running the
    documented recipe against a real repository."""

    def test_fossil_deletion_survives_the_repointed_head(self, bare_clone):
        """ISSUE-125 deletes the clone-day `refs/heads/*` fossils, but the step
        before it points HEAD at `refs/remotes/origin/main`. Every `git branch`
        subcommand then fails with `fatal: HEAD not found below refs/heads!`
        before deleting anything, so the fossils the loop exists to remove
        survived every clone."""
        assert "refs/heads/main" in _git(bare_clone, "for-each-ref", "--format=%(refname)")

        _run_fragment(_extract("CHECKED_OUT=$(git -C", "done"), bare_clone)

        refs = _git(bare_clone, "for-each-ref", "--format=%(refname)")
        assert "refs/heads/main" not in refs, f"clone-day fossil survived: {refs}"
        assert "refs/remotes/origin/main" in refs, "the remote-tracking ref must remain"

    def test_branch_d_would_not_have_worked(self, bare_clone):
        """The test above passes trivially if someone swaps the delete back to
        `branch -D`, so pin the reason: `branch -D` really does fail here."""
        proc = subprocess.run(
            ["git", "-C", str(bare_clone), "branch", "-D", "main"],
            capture_output=True, text=True, env={**os.environ, **GIT_ISOLATION},
        )
        assert proc.returncode != 0
        assert "HEAD not found below refs/heads" in proc.stderr

    def test_default_branch_falls_back_when_origin_head_is_absent(self, bare_clone):
        """`symbolic-ref ... | sed ... || echo "main"` takes the *pipeline's*
        exit status, which is sed's, and sed succeeds on empty input. So the
        fallback never fired: DEFAULT_BRANCH came out empty and the worktree was
        created from `origin/`, an unknown revision. `refs/remotes/origin/HEAD`
        is absent on any clone made before git 2.48, so this was the ordinary
        path rather than an edge case."""
        _git(bare_clone, "symbolic-ref", "-d", "refs/remotes/origin/HEAD")

        fragment = _extract(
            "symbolic-ref --short refs/remotes/origin/HEAD", "DEFAULT_BRANCH:-main"
        )
        out = _run_fragment(fragment + '\necho "$DEFAULT_BRANCH"', bare_clone)

        assert out.strip() == "main", f"fallback did not fire, got {out.strip()!r}"
        # The point of a fallback: the ref it names has to actually resolve.
        _git(bare_clone, "rev-parse", f"origin/{out.strip()}")

    def test_default_branch_reads_origin_head_when_present(self, bare_clone):
        """The fallback must not shadow a repository that is on `master`."""
        _git(bare_clone, "update-ref", "refs/remotes/origin/master", "refs/remotes/origin/main")
        _git(bare_clone, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master")

        fragment = _extract(
            "symbolic-ref --short refs/remotes/origin/HEAD", "DEFAULT_BRANCH:-main"
        )
        out = _run_fragment(fragment + '\necho "$DEFAULT_BRANCH"', bare_clone)

        assert out.strip() == "master", f"origin/HEAD ignored, got {out.strip()!r}"
