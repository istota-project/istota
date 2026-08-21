"""The developer / commit / code_review document split.

These run against the *real* bundled skill tree rather than synthetic
``SkillMeta`` fixtures. The thing under test is what actually ships in
``src/istota/skills/``, so a fixture that restates the frontmatter would pass
whatever the shipped files happen to say.
"""

import os
import re
import subprocess
import sys
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


def _fenced_blocks(body: str):
    """Each ``` block, as its own string. The one fence parser in this file.

    Toggling on `line.startswith` misses an indented fence, which is the normal
    way to put a recipe inside a list item; `developer` has two, in the Error
    Handling bullets. Miss those and every following line is classified
    inversely, so a guard that only inspects runnable lines silently starts
    inspecting prose instead.

    The toggle assumes fences nest nowhere and come in pairs — true of this
    document, and it would break on a heredoc that printed three backticks.
    Kept in one place so that assumption has one home rather than three.
    """
    current, in_fence = [], False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            if in_fence:
                yield "\n".join(current)
                current = []
            in_fence = not in_fence
            continue
        if in_fence:
            current.append(line)


def _fenced_lines(body: str):
    """Lines inside ``` blocks — the ones a model will copy and run."""
    for block in _fenced_blocks(body):
        yield from block.splitlines()


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
    achievable — `developer` sheds ~52 lines and gains more than that back.

    The ceiling is 700 because that is the figure `.claude/rules/skills.md`
    already documents as the contract ("held under a 700-line budget"). It sat
    at 675 here, below the number the rules file states, and four fixes to the
    recipes landing in one week (ISSUE-264, -267, -268, -269) ran it out. Fitting
    them meant deleting reviewed content from a recipe the model executes, so
    the test moved to the documented number rather than the recipes shrinking to
    an undocumented one. Raise this again only by changing the rules file first.
    """

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


def _run_fragment(fragment: str, bare: Path, **env: str) -> str:
    proc = subprocess.run(
        ["bash", "-c", fragment],
        capture_output=True, text=True,
        env={**os.environ, **GIT_ISOLATION, "BARE_DIR": str(bare), **env},
    )
    assert proc.returncode == 0, f"fragment failed:\n{fragment}\n{proc.stderr}"
    return proc.stdout


# The always-run block that brings any clone to the invariant: origin/HEAD
# resolves, HEAD is a refs/heads/ ref that does not. Extracted as one piece
# because its three steps depend on each other's variables.
_INVARIANT_BLOCK = ("rev-parse -q --verify origin/HEAD", "done")


def _drop_origin_head(bare: Path) -> None:
    """Remove `refs/remotes/origin/HEAD` if this git created one on fetch.

    Git 2.48 learned to write it during `fetch`; the devbox runs git 2.39,
    which does not. Normalising to *absent* is what makes these tests say the
    same thing on both, rather than passing on the developer's machine because
    a newer git quietly did the recipe's job for it.
    """
    subprocess.run(
        ["git", "-C", str(bare), "symbolic-ref", "-d", "refs/remotes/origin/HEAD"],
        capture_output=True, text=True, env={**os.environ, **GIT_ISOLATION},
    )


@pytest.fixture
def bare_clone(tmp_path) -> Path:
    """A bare clone in the shape clones made before ISSUE-269 are still in:
    remote-tracking refspec configured, fetched, HEAD pointed into
    `refs/remotes/origin/*`. The clone block overwrites HEAD when a test runs
    it; the repair path is what has to cope with a clone left like this."""
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
        before it used to point HEAD at `refs/remotes/origin/main`. Every `git
        branch` subcommand then failed with `fatal: HEAD not found below
        refs/heads!` before deleting anything, so the fossils the loop exists to
        remove survived every clone."""
        assert "refs/heads/main" in _git(bare_clone, "for-each-ref", "--format=%(refname)")

        _run_fragment(_extract(*_INVARIANT_BLOCK), bare_clone, FRESH="1")

        refs = _git(bare_clone, "for-each-ref", "--format=%(refname)")
        assert "refs/heads/main" not in refs, f"clone-day fossil survived: {refs}"
        assert "refs/remotes/origin/main" in refs, "the remote-tracking ref must remain"

    def test_the_loop_deletes_the_head_that_head_names(self, bare_clone):
        """The block points HEAD at `refs/heads/$DEFAULT` *before* the loop
        runs, so the ref the loop must drop is the one HEAD names. Deleting it
        is the step that leaves HEAD unborn; a delete that refused here would
        put the fossil back within reach of `git show`."""
        _run_fragment(_extract(*_INVARIANT_BLOCK), bare_clone, FRESH="1")

        assert "refs/heads/main" not in _git(bare_clone, "for-each-ref", "--format=%(refname)")
        assert _git(bare_clone, "symbolic-ref", "HEAD").strip() == "refs/heads/main"

    def test_a_task_branch_survives_the_loop_on_an_existing_clone(self, bare_clone, tmp_path):
        """On a clone that already exists, `refs/heads/` holds the branch of
        every worktree ever made in it — including one whose worktree was
        pruned, which may be the only copy of that work. Only the ref HEAD
        names is a fossil there, and a live worktree's branch is skipped on
        both paths."""
        live = tmp_path / "live-worktree"
        _git(bare_clone, "symbolic-ref", "HEAD", "refs/heads/main")
        _git(bare_clone, "worktree", "add", "-q", "-b", "istota/9-live", str(live), "origin/main")
        _git(bare_clone, "branch", "istota/8-pruned", "origin/main")

        _run_fragment(_extract(*_INVARIANT_BLOCK), bare_clone)

        refs = _git(bare_clone, "for-each-ref", "--format=%(refname)", "refs/heads/")
        assert "refs/heads/istota/8-pruned" in refs, f"unpushed task branch deleted: {refs}"
        assert "refs/heads/istota/9-live" in refs, f"live worktree branch deleted: {refs}"
        assert "refs/heads/main" not in refs, f"the fossil HEAD names survived: {refs}"

    def test_default_branch_falls_back_when_origin_head_is_absent(self, bare_clone):
        """`symbolic-ref ... | sed ... || echo "main"` takes the *pipeline's*
        exit status, which is sed's, and sed succeeds on empty input. So the
        fallback never fired: DEFAULT_BRANCH came out empty and the worktree was
        created from `origin/`, an unknown revision. `refs/remotes/origin/HEAD`
        is absent on any clone made before git 2.48, so this was the ordinary
        path rather than an edge case."""
        _drop_origin_head(bare_clone)

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

    def _assert_invariant(self, bare: Path, branch: str = "main") -> None:
        """Both halves, stated once: origin/HEAD resolves, HEAD is a
        `refs/heads/` ref that does not."""
        assert _git(bare, "rev-parse", "--verify", "origin/HEAD").strip()
        assert _git(bare, "symbolic-ref", "HEAD").strip() == f"refs/heads/{branch}"
        proc = subprocess.run(
            ["git", "-C", str(bare), "rev-parse", "--verify", branch],
            capture_output=True, text=True, env={**os.environ, **GIT_ISOLATION},
        )
        assert proc.returncode != 0, f"a local `{branch}` resolved; the fossil is readable"

    def test_worktree_add_survives_the_block(self, bare_clone, tmp_path):
        """ISSUE-269. `worktree add -b` writes a new local head and resolves
        HEAD while doing it, so a HEAD under `refs/remotes/` aborts it with
        `fatal: HEAD not found below refs/heads!` and no worktree is created —
        the very next step of the lifecycle has nothing to work in."""
        _run_fragment(_extract(*_INVARIANT_BLOCK), bare_clone, FRESH="1")

        work = tmp_path / "project--task"
        proc = subprocess.run(
            ["git", "-C", str(bare_clone), "worktree", "add", "-b", "istota/1-slug",
             str(work), "origin/main"],
            capture_output=True, text=True, env={**os.environ, **GIT_ISOLATION},
        )
        assert proc.returncode == 0, f"worktree add failed:\n{proc.stderr}"
        assert (work / ".git").exists(), "worktree directory was not created"

    def test_a_fresh_clone_reaches_the_invariant(self, bare_clone):
        """Both halves at once. Moving HEAD back below `refs/heads/` must not
        resurrect a *readable* fossil — it stays unborn, so naming a local
        branch still errors instead of returning clone-day bytes (ISSUE-125),
        and `origin/HEAD` is established because `clone --bare` never writes
        it and git only started doing so on fetch in 2.48."""
        _drop_origin_head(bare_clone)

        _run_fragment(_extract(*_INVARIANT_BLOCK), bare_clone, FRESH="1")

        self._assert_invariant(bare_clone)

    def test_an_existing_clone_reaches_the_invariant(self, bare_clone, tmp_path):
        """The clone step sits inside `if [ ! -d "$BARE_DIR" ]`, so a clone that
        already exists — the production one did — is never revisited by it. The
        block runs on every pass for that reason, and has to land the same
        invariant from the broken shape, fossil included."""
        _drop_origin_head(bare_clone)
        assert _git(bare_clone, "symbolic-ref", "HEAD").strip() == "refs/remotes/origin/main"
        assert "refs/heads/main" in _git(bare_clone, "for-each-ref", "--format=%(refname)")

        _run_fragment(_extract(*_INVARIANT_BLOCK), bare_clone)

        self._assert_invariant(bare_clone)
        work = tmp_path / "project--task"
        _git(bare_clone, "worktree", "add", "-b", "istota/1-slug", str(work), "origin/main")

    def test_a_dangling_origin_head_is_refreshed(self, bare_clone):
        """`origin/HEAD` survives the upstream default branch being renamed, so
        it can name a ref that no longer exists — `code_review`'s `_default_base`
        carries the same note. A presence check reads that as healthy, and the
        block would then point HEAD at a branch nothing can resolve and hand the
        worktree step a base that does not exist."""
        _git(bare_clone, "update-ref", "refs/remotes/origin/gone", "refs/remotes/origin/main")
        _git(bare_clone, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/gone")
        _git(bare_clone, "update-ref", "-d", "refs/remotes/origin/gone")

        _run_fragment(_extract(*_INVARIANT_BLOCK), bare_clone)

        self._assert_invariant(bare_clone)

    def test_a_detached_head_is_repaired_quietly(self, bare_clone):
        """A bare HEAD holding a raw sha is a state to repair. `symbolic-ref`
        without `-q` prints `fatal: ref HEAD is not a symbolic ref` while doing
        it, and the lifecycle tells the model to stop on failure output."""
        _git(bare_clone, "update-ref", "--no-deref", "HEAD", "refs/remotes/origin/main")

        out = _run_fragment(_extract(*_INVARIANT_BLOCK), bare_clone)

        self._assert_invariant(bare_clone)
        assert "fatal:" not in out

    def test_the_block_is_idempotent_and_respects_master(self, bare_clone):
        """It runs on every pass, so a second pass over a healthy clone has to
        change nothing — and must read the default branch rather than assume
        `main` on a repository using `master`."""
        _git(bare_clone, "update-ref", "refs/remotes/origin/master", "refs/remotes/origin/main")
        _git(bare_clone, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master")

        _run_fragment(_extract(*_INVARIANT_BLOCK), bare_clone)
        self._assert_invariant(bare_clone, branch="master")
        _run_fragment(_extract(*_INVARIANT_BLOCK), bare_clone)
        self._assert_invariant(bare_clone, branch="master")

def _fenced_block(body: str, marker: str) -> str:
    """The whole ``` block containing `marker`.

    `_extract` takes a start line and a stop line, which needs a stop token
    appearing nowhere earlier in the block. The GitLab recipe has none worth
    relying on — `fi` is a substring of `confirm`, `exit 1` occurs twice — so
    the fence is the safer unit to lift.
    """
    hits = [b for b in _fenced_blocks(body) if marker in b]
    assert len(hits) == 1, (
        f"expected exactly one fenced block containing {marker!r}, got {len(hits)}"
    )
    return hits[0]


def _stanza_through(block: str, marker: str, closer: str) -> str:
    """From the line containing `marker` through the first line that is exactly
    `closer`. The guards in this document end in one of two ways: a `fi` closing
    an `if`, or a `}` closing a `|| { … }` block."""
    lines = block.splitlines()
    start = next(i for i, line in enumerate(lines) if marker in line)
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == closer)
    return "\n".join(lines[start:end + 1])


def _stanza_through_fi(block: str, marker: str) -> str:
    """From the line containing `marker` through the `fi` closing its guard.

    Equality on the stripped line rather than a substring test: `confirm`
    contains `fi`, and matching that would lift a fragment stopping two lines
    before the guard it is meant to exercise — passing while proving nothing.
    """
    lines = block.splitlines()
    start = next(i for i, line in enumerate(lines) if marker in line)
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "fi")
    return "\n".join(lines[start:end + 1])


_NAMESPACE_CHECK_MARKER = "RESOLVED=$(glab repo view"


@pytest.fixture
def glab_153(tmp_path) -> Path:
    """A `glab` shaped like the 1.53 in the Debian archive: `-F json`, no `--jq`.

    The Docker image pins glab 1.114, which does have `--jq`; the Ansible path
    installs whatever trixie ships, which does not. A recipe in the body has to
    run on both, so the stub is the older one — and it rejects `--jq` the way
    the real binary does, so reverting a recipe to the `gh` idiom fails here
    and not only in the text guard.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "glab"
    stub.write_text(
        "#!/bin/sh\n"
        "json=\n"
        'for arg in "$@"; do\n'
        "    case \"$arg\" in\n"
        "        --jq|--jq=*|-q)\n"
        '            echo "unknown flag: $arg" >&2\n'
        "            exit 1 ;;\n"
        "        json) json=1 ;;\n"
        "    esac\n"
        "done\n"
        # Real glab defaults to `-F text`, so a recipe that stopped asking for
        # JSON would get prose and the parse would die. Refuse it here too,
        # rather than handing back well-formed JSON the real binary never sent.
        'if [ -z "$json" ]; then\n'
        '    echo "glab stub: expected -F json" >&2\n'
        "    exit 1\n"
        "fi\n"
        'if [ -n "${GLAB_STUB_FAIL:-}" ]; then\n'
        '    echo "glab: could not reach the instance" >&2\n'
        "    exit 1\n"
        "fi\n"
        'printf "%s" "${GLAB_STUB_JSON:-}"\n'
    )
    stub.chmod(0o755)
    # The recipes shell out to `python3`; pin it to the interpreter running the
    # suite rather than depending on what the host happens to have on PATH.
    (bin_dir / "python3").symlink_to(sys.executable)
    return bin_dir


def _run_recipe(fragment: str, bin_dir: Path, **env) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", fragment],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", **env},
    )


def _namespace_check(body: str) -> str:
    return _stanza_through_fi(
        _fenced_block(body, _NAMESPACE_CHECK_MARKER), _NAMESPACE_CHECK_MARKER
    )


class TestGlabFieldReads:
    """ISSUE-268. `glab` grew `--jq` well after the version the Ansible path
    installs, so every glab field read written in the `gh` idiom exits `unknown
    flag` — including the namespace check whose whole job is to stop a push
    reaching the wrong project."""

    def _body(self) -> str:
        return (_BUNDLED_SKILLS_DIR / "developer" / "skill.md").read_text()

    def test_no_runnable_glab_line_uses_a_gh_only_filter_flag(self):
        """The seven originals. `gh` keeps `--jq`/`-q`; glab must not borrow it.

        Comments are skipped deliberately: the fix puts the reason *why* glab
        has no `--jq` in a comment beside the recipe, and a guard that could not
        tell an explanation from an invocation would forbid saying so.
        """
        # Join backslash continuations first. The document writes multi-line glab
        # invocations that way (`glab mr create`), so a flag parked on a
        # continuation line sits on a line with no `glab` in it and would slip
        # past a per-line scan — the exact regression this guard exists to catch.
        for block in _fenced_blocks(self._body()):
            for line in re.sub(r"\\\n\s*", " ", block).splitlines():
                if "glab" not in line or line.lstrip().startswith("#"):
                    continue
                assert "--jq" not in line, (
                    f"developer filters glab output with gh's flag: {line.strip()!r}"
                )
                # `-q` is the short form and fails the same way. Match it as a
                # whole argument, not a substring, so `--quiet` and a `-q` inside
                # a quoted expression are not mistaken for it.
                assert "-q" not in line.split(), (
                    f"developer filters glab output with gh's short flag: "
                    f"{line.strip()!r}"
                )

    def test_prose_does_not_promise_glab_a_jq_flag(self):
        """The generalization the six lesser recipes were written from. Left
        standing it regenerates them the next time someone adds a field read."""
        assert "`--json`/`-F json` plus `--jq`/`-q`" not in self._body(), (
            "the claim that produced all seven broken recipes is back in the body"
        )

    def test_namespace_check_reads_the_project_without_jq(self, glab_153):
        """The check has to actually resolve a namespace on the old glab.
        `unknown flag` assigned an empty string, which is how a guard that never
        ran still looked like it was running."""
        proc = _run_recipe(
            "set -o pipefail\n"
            + _namespace_check(self._body()).replace("namespace/project", "acme/widget")
            + '\necho "RESOLVED=$RESOLVED"',
            glab_153,
            GLAB_STUB_JSON='{"path_with_namespace": "acme/widget"}',
        )

        assert proc.returncode == 0, f"aborted on a matching project:\n{proc.stderr}"
        assert "RESOLVED=acme/widget" in proc.stdout

    def test_namespace_check_aborts_on_the_wrong_project(self, glab_153):
        """The case the check exists for."""
        proc = _run_recipe(
            "set -o pipefail\n"
            + _namespace_check(self._body()).replace("namespace/project", "acme/widget"),
            glab_153,
            GLAB_STUB_JSON='{"path_with_namespace": "someone-else/widget"}',
        )

        assert proc.returncode != 0, "a push to the wrong project was not stopped"
        assert "someone-else/widget" in proc.stdout + proc.stderr

    def test_namespace_check_fails_closed_when_glab_fails(self, glab_153):
        """The property the entry asked for by name. A tool error must not
        become a value: the old shape turned `unknown flag` into an empty string
        and then compared it, so the abort was an accident of the comparison
        rather than a decision — and a recipe whose expected value was itself
        empty would have sailed through."""
        proc = _run_recipe(
            "set -o pipefail\n"
            + _namespace_check(self._body()).replace("namespace/project", ""),
            glab_153,
            GLAB_STUB_FAIL="1",
        )

        assert proc.returncode != 0, (
            "glab failed and the check passed — the empty result compared equal"
        )

    def test_every_piping_recipe_sets_pipefail_before_it_pipes(self):
        """Reading a glab field means a pipeline, and a pipeline's exit status is
        the last command's — so a glab that exits non-zero *after* printing is
        masked by a python3 that parsed what it printed.

        Every fence that pipes, not just the namespace check: each fence is a
        separate Bash tool call and shell options do not survive between them, so
        `set -o pipefail` in one buys the others nothing.
        """
        piping = [
            block
            for block in _fenced_blocks(self._body())
            if any("| python3" in line for line in block.splitlines())
        ]
        assert piping, "no piped recipe found — the guard is inspecting nothing"

        for block in piping:
            lines = block.splitlines()
            pipefail = next(
                (i for i, line in enumerate(lines) if "set -o pipefail" in line), None
            )
            first_pipe = next(i for i, line in enumerate(lines) if "| python3" in line)
            assert pipefail is not None, (
                f"a recipe pipes without setting pipefail: {lines[first_pipe].strip()!r}"
            )
            assert pipefail < first_pipe, (
                f"pipefail is set after the pipeline it governs: "
                f"{lines[first_pipe].strip()!r}"
            )

    @pytest.mark.parametrize(
        "marker", ["MR_IID=$(glab mr view", "PIPELINE_ID=$(glab ci list"]
    )
    def test_captures_feeding_a_later_command_abort_on_failure(self, marker, glab_153):
        """`MR_IID` and `PIPELINE_ID` are read here and consumed later. An empty
        one is not inert: `glab mr view ""` and `glab mr merge "" --yes` fall back
        to the current branch's merge request, so a swallowed read acts on
        something nobody named — the namespace check's original defect again.

        Run rather than pattern-matched: asserting the line ends in `|| {` would
        equally accept `|| { echo "oops"; }`, a guard that announces the failure
        and then carries on.
        """
        stanza = _stanza_through(
            _fenced_block(self._body(), marker), marker, "}"
        )
        proc = _run_recipe("set -o pipefail\n" + stanza, glab_153, GLAB_STUB_FAIL="1")

        assert proc.returncode != 0, (
            f"glab failed and the capture carried on: {stanza!r}"
        )

    def test_merge_fence_rechecks_the_id_it_did_not_set(self, glab_153):
        """The capture and the merge live in different fences, and a fence is its
        own `bash -c`. A guard in the capturing shell therefore protects nothing
        at the point of use, so the merging fence has to re-check for itself."""
        block = _fenced_block(self._body(), 'glab mr merge "$MR_IID"')
        guard = next(
            (line for line in block.splitlines() if "MR_IID" in line and "-n " in line),
            None,
        )
        assert guard is not None, "the merging fence never checks that MR_IID is set"

        proc = _run_recipe(guard, glab_153)
        assert proc.returncode != 0, (
            "an unset MR_IID did not stop the fence that merges on it"
        )
