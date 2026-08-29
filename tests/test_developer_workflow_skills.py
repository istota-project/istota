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
        assert repos.sensitive is False, "a directory path is not a credential"
        assert repos.source == "setup_env", (
            "the value is the caller's own subtree of `developer.repos_dir`, "
            "which only the developer skill's setup_env hook can derive — and "
            "a `from: config` entry here would outrank that hook silently"
        )
        assert repos.config_path == "", (
            "a `from: setup_env` spec resolves to None by design; a config_path "
            "on it reads as a value that is never used"
        )


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


class TestReviewerEnvWiring:
    """ISSUE-289. The env manifest is what puts a value in front of the model,
    and the value `glab mr create --reviewer` needs is a username. The old
    `GITLAB_REVIEWER_ID` spec was fed by `developer.gitlab_reviewer_id`, whose
    name asked operators for the numeric user id `glab` rejects."""

    def _specs(self, bundled_index):
        return {spec.var: spec for spec in bundled_index["developer"].env_specs}

    def test_the_reviewer_variable_is_named_for_a_username(self, bundled_index):
        specs = self._specs(bundled_index)
        assert "GITLAB_REVIEWER_ID" not in specs
        assert "GITLAB_REVIEWER" in specs

    def test_the_reviewer_variable_reads_the_username_field(self, bundled_index):
        spec = self._specs(bundled_index)["GITLAB_REVIEWER"]
        assert spec.source == "config"
        assert spec.config_path == "developer.gitlab_reviewer"

    def test_the_reviewer_variable_is_gated_on_being_configured(self, bundled_index):
        """`--reviewer ""` is an error rather than a no-op, so an unconfigured
        reviewer has to be an absent variable, not an empty one."""
        spec = self._specs(bundled_index)["GITLAB_REVIEWER"]
        when = spec.when if isinstance(spec.when, list) else [spec.when]
        assert "developer.enabled" in when
        assert "developer.gitlab_reviewer" in when

    def test_the_retired_id_field_is_no_longer_exported(self, bundled_index):
        """`gitlab_reviewer_id` survives as config — it holds the numeric id,
        which is what its name always claimed — but nothing puts it in the
        model's environment any more."""
        paths = {spec.config_path for spec in bundled_index["developer"].env_specs}
        assert "developer.gitlab_reviewer_id" not in paths


class TestReviewerReachesTheTaskEnvironment:
    """The seam, rather than another read of the manifest.

    Every other test here asserts against the frontmatter or the body text.
    Neither proves the value arrives: the resolution runs through
    ``build_skill_env``, which applies the ``when`` gates and stringifies, and
    that is the path an MR actually depends on.
    """

    def _env(self, bundled_index, tmp_path, **developer_overrides):
        from unittest.mock import MagicMock

        from istota.config import Config, DeveloperConfig
        from istota.skills._env import EnvContext, build_skill_env

        fields = {"enabled": True, "repos_dir": str(tmp_path)}
        fields.update(developer_overrides)
        config = Config()
        config.developer = DeveloperConfig(**fields)
        ctx = EnvContext(
            config=config,
            task=MagicMock(id=1, user_id="alice", conversation_token="room1"),
            user_resources=[],
            user_config=None,
            user_temp_dir=tmp_path / "temp",
            is_admin=True,
        )
        return build_skill_env(["developer"], bundled_index, ctx)

    def test_a_configured_username_arrives_as_gitlab_reviewer(
        self, bundled_index, tmp_path
    ):
        env = self._env(bundled_index, tmp_path, gitlab_reviewer="reviewer-user")
        assert env["GITLAB_REVIEWER"] == "reviewer-user"

    def test_an_unset_reviewer_leaves_the_variable_absent(self, bundled_index, tmp_path):
        """Not empty — absent. `glab mr create --reviewer ""` is an error rather
        than a no-op, and the recipe tests the variable to decide whether to
        build the flag at all."""
        env = self._env(bundled_index, tmp_path, gitlab_reviewer="")
        assert "GITLAB_REVIEWER" not in env

    def test_the_retired_id_reaches_nothing(self, bundled_index, tmp_path):
        env = self._env(
            bundled_index, tmp_path, gitlab_reviewer="", gitlab_reviewer_id="1234567"
        )
        assert "GITLAB_REVIEWER" not in env
        assert "GITLAB_REVIEWER_ID" not in env


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

    def test_the_mr_recipe_reads_the_username_reviewer_variable(self):
        """ISSUE-289. `glab mr create --reviewer` resolves by username, and the
        variable the recipe read was called `GITLAB_REVIEWER_ID`, so operators
        set a numeric user id in it. `glab` answers `failed to find user by
        name` and — because the recipe builds the flag rather than failing —
        every agent-authored MR opened with nobody assigned. The variable now
        carries a username and is named for one."""
        body = self._body("developer")
        assert "GITLAB_REVIEWER_ID" not in body, (
            "developer still reads $GITLAB_REVIEWER_ID, which the executor no "
            "longer exports; the flag would be built from an empty value"
        )
        assert "${GITLAB_REVIEWER:-}" in body
        assert "--reviewer $GITLAB_REVIEWER" in body

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

    def test_a_zero_exit_from_an_install_is_not_evidence_it_completed(self):
        """ISSUE-318. `npm install` exits 0 when a package's postinstall binary
        fetch is refused at the network boundary: npm suppresses lifecycle
        script output by default, so the run prints `added N packages`, exits
        0, and leaves an empty cache directory where the browser should be. The
        boundary rule above it is unreachable from there — nothing was printed,
        so there is no host to name and no reason to stop — and the failure
        resurfaces much later as a missing binary in a test run."""
        body = self._body("developer")
        assert "not evidence it completed" in body, (
            "developer must say a zero exit from an install does not prove a "
            "postinstall fetch succeeded"
        )
        assert "--foreground-scripts" in body, (
            "the only way to see a suppressed postinstall error has to be named"
        )
        assert "PUPPETEER_SKIP_DOWNLOAD" in body, (
            "a skip variable is what makes the outcome honest rather than "
            "silently empty; name at least one"
        )

    def test_the_boundary_is_named_in_the_shape_plain_node_reports_it(self):
        """ISSUE-318, second half. The sandbox exports `HTTPS_PROXY`, so npm,
        `uv`, `git` and `curl` are refused at the proxy against a CONNECT they
        name themselves.
        Plain Node does not read proxy environment, so a postinstall script
        written in JS never reaches the proxy: it resolves DNS inside
        `--unshare-net` and fails with `EAI_AGAIN`. That reads as a transient
        DNS problem and invites exactly the retry the boundary rule forbids, so
        the rule has to name the shape as well as the answer."""
        body = self._body("developer")
        assert "EAI_AGAIN" in body, (
            "the boundary rule must name the DNS error shape a plain Node "
            "postinstall reports, or the model reads the refusal as a flake"
        )

    @pytest.mark.parametrize("name", ["developer", "commit", "code_review"])
    def test_no_recipe_runs_a_command_under_a_near_ceiling_timeout(self, name):
        """D3. `timeout 590` inside a 600-second tool cap is the pattern the
        2026-08-20 incident kept hitting: it guarantees a kill at the moment a
        long run might have finished, and discards everything the run produced.
        Four such attempts in forty minutes yielded no coverage at all. The
        body now prescribes a detached run instead, and this is what keeps the
        old pattern from being written back in as an obvious-looking fix.

        Scoped to fenced code, because the prose has to be free to *name* the
        pattern it forbids — a check over the whole body would fail on the
        rule's own explanation of itself. Extraction goes through
        `_fenced_lines`, not a local regex: the D3 recipe's fence is indented
        under a list item, and an anchored `^```` misses every such block while
        still finding enough unindented ones to look like it worked."""
        import re

        near_ceiling = [
            m.group(0)
            for line in _fenced_lines(self._body(name))
            for m in re.finditer(r"\btimeout\s+(\d{3,})\b", line)
            if int(m.group(1)) >= 300
        ]
        assert near_ceiling == [], (
            f"a {name} recipe wraps a command in a near-ceiling timeout: {near_ceiling}"
        )

    def test_the_detached_run_recipe_records_its_exit_status(self):
        """D3, and the rule three bullets above it: the exit status is the
        result. A detached run reports nothing back through the tool call, so
        if the recipe does not write its status to a file there is no way to
        read one — leaving the model to infer pass or fail from log text, which
        is exactly what the pipefail rule exists to forbid.

        Also pins `setsid`. The Bash tool kills its whole process group in a
        `finally` (`session/tools/bash.py`), on the normal return as much as on
        an interrupt, so a merely backgrounded run is dead the moment the call
        that started it finishes — and the recipe would then be a slower way of
        getting nothing."""
        recipe = "\n".join(_fenced_lines(self._body("developer")))
        assert ".check.status" in recipe, "the detached run records no exit status"
        assert "echo $? >" in recipe, "the status file is written without $?"
        assert "setsid" in recipe, (
            "a backgrounded run without its own session is killed by the group "
            "kill when the starting bash call returns"
        )

    def test_the_detached_run_recipe_actually_backgrounds_and_reports(self, tmp_path):
        """Run the recipe's own line rather than describe it.

        Two drafts of this recipe were wrong in the same way and both looked
        right: `&` binds looser than `&&` and looser than `;`, so
        `cd "$D" && cmd &` backgrounds the `cd`, and `cmd > log; echo $? > st &`
        backgrounds only the `echo` and runs the suite in the foreground —
        which is precisely the blocking behaviour the bullet exists to avoid,
        while still producing a correct-looking status file at the end.

        `setsid` is stripped here: it is absent on the macOS dev machines and
        the property it buys (escaping the Bash tool's process-group kill) is
        not observable from a plain pytest run. The pinning assertion above
        covers it."""
        import re
        import subprocess

        line = next(
            (ln for ln in _fenced_lines(self._body("developer")) if "setsid" in ln),
            None,
        )
        assert line is not None, "no detached-run line found in the body"
        # A one-second command exiting 7 stands in for the suite.
        line = line.replace("setsid ", "").strip()
        line = re.sub(r"uv run pytest[^>]*", 'sh -c "sleep 1; exit 7" ', line)
        assert "sleep 1" in line, f"substitution missed the runner: {line!r}"

        poll = f'cd "{tmp_path}" && cat .check.status 2>/dev/null || echo still running'
        script = (
            f'cd "{tmp_path}" || exit 1\n'
            "rm -f .check.status\n"
            f"{line}\n"
            "sleep 0.3\n"
            f'echo "during=$({poll})"\n'
            "sleep 1.5\n"
            f'echo "after=$({poll})"\n'
        )
        out = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, timeout=30
        ).stdout

        # Backgrounded: the poll taken 0.3s in must not already have the answer.
        assert "during=still running" in out, out
        # And the status file carries the exit code, not the log text.
        assert "after=7" in out, out
        assert (tmp_path / ".check.log").exists()

    def test_the_worker_cap_names_the_variable_xdist_actually_reads(self):
        """D2. The cap is only worth stating if it takes effect, and an env var
        the runner does not read fails silently — the suite claims every core
        exactly as before and nothing says so. Asserted against the installed
        xdist rather than against the string, so a rename upstream fails here
        instead of on a shared host under load."""
        import inspect

        from xdist import plugin

        source = inspect.getsource(plugin)
        assert "PYTEST_XDIST_AUTO_NUM_WORKERS" in source, (
            "xdist no longer reads this variable; the skill's worker cap is inert"
        )
        assert "PYTEST_XDIST_AUTO_NUM_WORKERS" in self._body("developer")

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


# ---------------------------------------------------------------------------
# ISSUE-337: the workflow defers, the deployment's mechanics do not.

# Anything naming a run wider than the change, so a new line has to be classified
# rather than slipping past a pattern aimed at today's wording. Two arms are
# deliberate. `verification pass` is here because this document introduced the
# phrase, and a detector knowing only the vocabulary it removed would not see the
# next mandate written in the vocabulary it installed. The quantifier arm is here
# because the likeliest next mandate says "every test in the repository" or "the
# whole test run" and never says "suite" at all. Fenced lines are in scope on
# purpose: a comment in a recipe the model copies is an instruction, and the
# report template's `Tests:` field is inside a fence.
#
# Two arms are forward-looking rather than witnessed by today's body, and are
# marked so nobody removes them as dead. `[\s-]` matches nothing the plain `\s`
# did not, because the body spells it "over the whole repository"; the spec that
# installed the vocabulary spells it "whole-repository", so the next editor may
# too. And `run everything` is here because *this* document now uses
# "everything" as its own word for the wide run ("a reason to widen to
# everything"), which makes it the phrasing a future mandate reaches for while
# saying neither "suite" nor "full". The verb is required so the two existing
# negations, and "everything below applies as written" in the lifecycle, do not
# have to be classified.
_SUITE_MENTION = re.compile(
    r"\bsuites?\b"
    r"|\b(full|whole|entire|complete)[\s-]+(\w+[\s-]+){0,2}(pass|run|suite|tests|repository|tree)\b"
    r"|\bverification pass\b"
    r"|\bevery test\b"
    r"|\ball (of )?(the )?tests\b"
    r"|\bin (its|their) entirety\b"
    r"|\b(run|runs|running|execute|executes)[\s-]+(\w+[\s-]+){0,2}everything\b",
    re.IGNORECASE,
)

# What a line has to carry to count as yielding to the user on the spot.
_DEFERENCE_MARKERS = ("user's own instructions", "USER.md", "CHANNEL.md")

# ...and what a line naming the pass must not do: restate its scope. A pointer
# that grows into "the full verification pass over the whole repository" is the
# restatement coming back.
_SCOPE_WORDS = re.compile(
    r"\b(full|whole|entire)[\s-]+(\w+[\s-]+){0,2}(pass|suite|run|repository|tree)\b|\bevery test\b",
    re.IGNORECASE,
)

_DEFERRED = "deferred default"  # states the default and yields on the same line
_POINTER = "pointer"  # names the pass section 7 defines, without a scope of its own
_NOT_A_MANDATE = "not a mandate"  # a negation, or a fact about the host


def _digest(line):
    import hashlib

    return hashlib.sha256(line.encode()).hexdigest()[:12]


# Every line of the `developer` body allowed to mention a pass over more than the
# change, keyed by a fragment that locates it and a digest that pins the whole of
# it. An allowlist and not a pattern: a regex looking for "must" or "always"
# passes the next mandate phrased differently, which is the failure ISSUE-337 was
# — one bullet stating the rule and six others restating it as an order. The
# digest is what closes the other half, since a fragment match alone lets a
# mandate be appended to a line already on the list; editing one of these lines
# fails here on purpose, and the message carries the replacement digest.
_SUITE_LINES_ALLOWED = [
    (
        _NOT_A_MANDATE,
        "dozens of unrelated red suites",
        "9016c19c3dcf",
        "the shape a shared node_modules fails in; names no run",
    ),
    (
        _NOT_A_MANDATE,
        "not a full suite. The base branch was green",
        "a775ea2767b0",
        "step 3 forbids the baseline pass outright",
    ),
    (
        _POINTER,
        "run the verification pass below once, commit",
        "e10891ce29a1",
        "Fast tier, naming the pass section 7 defines",
    ),
    (
        _POINTER,
        "the verification pass below green before the commit",
        "7b6c79754330",
        "Standard tier, naming the same pass",
    ),
    (
        _POINTER,
        "the verification pass below before and after",
        "f3260296e8cd",
        "Full tier, naming the same pass",
    ),
    (
        _DEFERRED,
        "The pass is the tests covering the change",
        "7647fc36eba9",
        "the single statement of the shipped default",
    ),
    (
        _NOT_A_MANDATE,
        "The static half stays whole",
        "86473d7f3096",
        "why the narrow selection is safe; names a repository-wide run to keep "
        "the linters in it and to decline the tests",
    ),
    (
        _NOT_A_MANDATE,
        "each suite claims the whole box",
        "c02be78e4544",
        "the worker cap: a property of the host, and one of the mechanics",
    ),
    (
        _NOT_A_MANDATE,
        "no longer exits 0 on a suite that failed",
        "426678448812",
        "how pipefail is read; correctness of the answer, not scope",
    ),
    (
        _NOT_A_MANDATE,
        "a long suite might have finished",
        "9f65a12ca012",
        "why a near-ceiling timeout is the wrong fix for a long run",
    ),
    (
        _NOT_A_MANDATE,
        "Say what you ran.",
        "699521a79ead",
        "the honesty rule: it requires the report to say the suite did not run",
    ),
    (
        _NOT_A_MANDATE,
        "prints the whole run and will bury the transcript",
        "65aaf08d4c59",
        "CI log output, not a test run",
    ),
    (
        _POINTER,
        "run a review after the work's verification pass",
        "e5bf1007bad9",
        "the review's place in the order, naming the same pass",
    ),
]

# The eight decisions the spec moves from mandate to default, and the lifecycle
# section each one lives in. Test authoring is the eighth, added during
# implementation: the spec's Context names it in the routine being deferred and
# its split table did not, which stages 1 and 2 each flagged in passing.
_DEFERRED_DECISIONS = {
    "worktree per task": "### 2. Create the worktree, then read back what was made",
    "change tiers": "### 5. Pick a change tier, and say which",
    "when a test gets written": "### 6. Implement, and verify as you go",
    "when tests run, and which": "### 7. The verification budget",
    "commit granularity": "### 8. Commit",
    "whether a review runs": "### 9. Review before landing",
    "an MR or PR rather than a merge": "### 10. Land",
    "report shape": "### 12. Report",
}


class TestWorkflowDeference:
    """ISSUE-337. The body carries deployment mechanics and a development
    routine under one voice, and the routine shipped as an order to every
    deployment — a task followed it into a full suite on a host that takes 70
    minutes to run one, was killed at 47, and committed nothing. The routine is
    now a default that yields to `USER.md` and `CHANNEL.md`; the mechanics are
    not.

    The audit here reads the `developer` body alone, while a menu pull
    delivers `commit` and `code_review` in the same response. Both still
    state their own workflow rules unconditionally — commit granularity in
    one, the Fast-tier review exemption in the other — and the spec puts
    those two bodies out of scope. Widening the scan is the fix if that
    decision changes."""

    def _body(self):
        return (_BUNDLED_SKILLS_DIR / "developer" / "skill.md").read_text()

    def _step_body(self, body, heading):
        """The text under one heading, the heading line itself excluded, up to
        the next `##` or `###`. Excluding it is the point: a deference marker
        written into a heading would otherwise satisfy a check about the text
        underneath. Not the module-level `_section` further down this file,
        which stops at `####` as well and returns a different slice."""
        # To the end of the heading *line*, not of the string searched for: a
        # marker appended to the heading is still in the heading.
        start = body.index("\n", body.index(heading)) + 1
        rest = body[start:]
        following = re.search(r"^#{2,3} ", rest, re.MULTILINE)
        return rest[: following.start()] if following else rest

    def test_the_lifecycle_states_the_deference_rule(self):
        """A1. The skill body arrives mid-task, after user and channel memory
        are already in the system prompt, so it is the later and more specific
        document — which is why an override written in `CHANNEL.md` lost to it
        on 2026-08-28. Deference is the skill spending that lateness on
        pointing back at the earlier document. It has to name both files: a
        rule naming only `USER.md` leaves per-project workflow nowhere to go,
        and one naming only `CHANNEL.md` reaches no task from the 1:1."""
        lifecycle = self._step_body(self._body(), "## The Job Lifecycle")
        paragraphs = [p for p in lifecycle.split("\n\n") if "USER.md" in p]
        assert len(paragraphs) == 1, (
            "the deference rule is one paragraph near the top of the lifecycle; "
            f"found {len(paragraphs)} mentioning USER.md"
        )
        rule = paragraphs[0]
        assert "CHANNEL.md" in rule, "the rule names USER.md but not CHANNEL.md"
        # The tie-break: the model has no other way to resolve two workflows.
        assert "`CHANNEL.md` wins" in rule, rule
        # And which set of instructions does not yield, since a user asking for
        # work in place under /srv/app collides with a pre-submission check.
        assert "does not yield" in rule and "mechanics" in rule, rule

    def test_no_surviving_full_suite_mandate(self):
        """A2. The failure this catches is the document contradicting itself —
        the deference rule stated once and the old orders left standing
        elsewhere, where they win on repetition. Every line that mentions a run
        wider than the change is classified here by hand and pinned by digest,
        so both a new mandate and one grafted onto an allowed line fail rather
        than passing on a pattern that did not anticipate the wording."""
        lines = [ln.strip() for ln in self._body().splitlines() if _SUITE_MENTION.search(ln)]
        assert lines, "the pattern matches nothing; it no longer detects anything"

        for line in lines:
            hits = [e for e in _SUITE_LINES_ALLOWED if e[1] in line]
            assert len(hits) == 1, (
                "a line naming a run wider than the change is not on the "
                f"allowlist, or matches more than one entry ({len(hits)}): {line[:160]!r}"
            )

        for kind, fragment, digest, reason in _SUITE_LINES_ALLOWED:
            matching = [ln for ln in lines if fragment in ln]
            assert len(matching) == 1, (
                f"allowlist entry ({reason}) matches {len(matching)} lines, not one: {fragment!r}"
            )
            line = matching[0]
            assert _digest(line) == digest, (
                f"the allowlisted line ({reason}) changed. Read the whole of it: if it "
                f"still yields, update its digest to {_digest(line)!r}; if it now demands "
                f"a wider run, that is the finding. Line: {line[:200]!r}"
            )
            if kind is _DEFERRED:
                assert any(m in line for m in _DEFERENCE_MARKERS), (
                    "a line stating the test-scope default has to yield to the "
                    f"user on the same line: {line[:160]!r}"
                )
            if kind is _POINTER:
                assert not _SCOPE_WORDS.search(line), (
                    "a line naming the section 7 pass must not restate its scope, "
                    f"which is the restatement this change removed: {line[:160]!r}"
                )

        defaults = [e for e in _SUITE_LINES_ALLOWED if e[0] is _DEFERRED]
        assert len(defaults) == 1, (
            "the default is stated once. Six restatements of it are what "
            f"ISSUE-337 cost; this allowlist carries {len(defaults)}"
        )

    def test_the_tier_bullets_name_one_pass_and_no_scope_of_their_own(self):
        """A3. A tier decides how much *process* a change gets, and section 7
        decides how much testing. Two of the three bullets used to decide both,
        which put the mandate in the section the model reads first and left
        section 7 arguing with it. Each bullet now names the one pass section 7
        defines and states no scope of its own. Deliberately overlapping the
        line-level guard above: these are the three lines a future "but Full
        tier should really run everything" lands on, and a duplicated check on
        the likeliest regression is cheap."""
        section = self._step_body(self._body(), "### 5. Pick a change tier, and say which")
        bullets = [ln for ln in section.splitlines() if ln.startswith("- **")]
        assert [b.split("**")[1] for b in bullets] == ["Fast", "Standard", "Full"], bullets
        for bullet in bullets:
            assert "the verification pass below" in bullet, (
                "a tier bullet does not name the pass section 7 defines, so it is "
                f"deciding a scope of its own: {bullet[:140]!r}"
            )
            assert not re.search(r"\bsuites?\b", bullet, re.IGNORECASE), (
                f"a tier escalates to a suite: {bullet[:140]!r}"
            )
            assert not _SCOPE_WORDS.search(bullet), (
                f"a tier bullet restates a run scope: {bullet[:140]!r}"
            )

    LADDER_LEAD = "take the first rung that yields something"

    def test_the_selection_ladder_is_present_and_ordered(self):
        """A4. The narrow default is only safe if the model can work out what
        "the tests covering the change" means in the repository in front of it,
        and this skill ships to every deployment rather than to this one. The
        drift to fear is a rewrite by someone working in a Python repository
        who keeps rung 3 and rung 4, which is all pytest needs, and quietly
        drops the runners that can answer the question themselves. So the four
        rungs are required in order, and rung 2 is required to still name the
        four ecosystems that have a selector.

        Order matters as much as membership: rung 4 is a grep, and a ladder
        that reached it before trying the runner's own selector would be
        slower and less accurate everywhere the selector exists."""
        section = self._step_body(self._body(), "### 7. The verification budget")
        assert self.LADDER_LEAD in section, (
            "the selection ladder is gone; the narrow default now names no way "
            "of arriving at a selection"
        )
        ladder = section[section.index(self.LADDER_LEAD) :]
        following = ladder.find("\n- **")
        if following != -1:
            ladder = ladder[:following]

        marks = []
        for rung in (1, 2, 3, 4):
            # Either form: `(2)` inline, or `2.` if the ladder is ever unfolded
            # into a nested list. What is pinned is the ladder, not its markup.
            # `[ \t]` and not `\s`, which would swallow the preceding newlines
            # and report an offset before the marker.
            found = list(
                re.finditer(rf"\({rung}\)|^[ \t]*{rung}\.[ \t]", ladder, re.MULTILINE)
            )
            assert len(found) == 1, (
                f"rung {rung} appears {len(found)} times in the ladder, not once. "
                "A second marker makes the ordering check below read whichever "
                "one comes first, which is how an out-of-order ladder passes"
            )
            marks.append(found[0].start())
        assert marks == sorted(marks), f"the four rungs are out of order: {marks}"

        # Each rung is checked for the content that makes it that rung. Without
        # this the ordering check passes on a ladder of four bare markers, and a
        # rung emptied of everything but its number is the likeliest way the
        # ladder degrades under a later edit.
        rungs = [ladder[a:b] for a, b in zip(marks, marks[1:] + [len(ladder)])]
        assert "already touches" in rungs[0], "rung 1 no longer names the tests the change edited"
        for selector in ("vitest related", "--findRelatedTests", "go test ./", "cargo test -p"):
            # `go test ./` and not `go test`: the latter is a substring of
            # `cargo test -p`, so the Go rung would be held up by the Rust one
            # and could be deleted with the assertion still green.
            assert selector in rungs[1], (
                f"rung 2 no longer names {selector!r}; a repository whose runner "
                "can select for itself is sent to the grep instead"
            )
        assert "pytest" in rungs[1], (
            "rung 2 does not say pytest has no selector, so a Python repository "
            "either stops here or invents one"
        )
        assert "mirroring" in rungs[2], "rung 3 no longer names path mirroring"
        assert "grep" in rungs[3].lower(), (
            "rung 4 is no longer the grep, which is the only rung that works in "
            "a repository the other three do not recognise"
        )

    def test_the_killed_run_rule_survives_and_is_not_about_suites(self):
        """The rule that outlived the mandate it was attached to. It read
        "never re-run a full suite that a timeout killed", and the run this
        default produces is not a suite — so a task that narrowed, was killed
        again, and reached for the same command would find nothing addressed to
        it. Generalizing it also takes it off the allowlist above, since the
        line no longer mentions a suite for the scan to catch; this assertion
        is what stops it being deleted along with the mandate."""
        section = self._step_body(self._body(), "### 7. The verification budget")
        killed = [ln for ln in section.splitlines() if "killed run" in ln.lower()]
        assert len(killed) == 1, (
            f"the killed-run rule is stated once in section 7; found {len(killed)}"
        )
        line = killed[0]
        assert not re.search(r"\bsuites?\b", line, re.IGNORECASE), (
            f"the rule is about any killed run, not only about suites: {line[:140]!r}"
        )
        # The negations, not the nouns. Asserting on "longer timeout" and "same
        # command" alone passes an inverted rule — "retry it with a longer
        # timeout, and then with the same command again" carries both phrases
        # and instructs the exact behaviour that cost forty minutes.
        lowered = line.lower()
        for forbidden in ("not with a longer timeout", "not with the same command"):
            assert forbidden in lowered, (
                f"the rule no longer forbids {forbidden[8:]!r}, so a re-run of the "
                f"killed command reads as sanctioned: {line[:140]!r}"
            )

    REPORT_HEADING = "### 12. Report"

    def _report_field(self, name):
        """The template lines starting `<name>: `, inside section 12's fence.

        A list rather than the line, so a duplicated field is a failure here
        instead of whichever copy the scan reached first. Scoped to the fenced
        block rather than to the section, because the fence is the part the
        model copies: a field moved out of it into the surrounding prose has
        left the template, and a scan of the whole section would not notice."""
        section = self._step_body(self._body(), self.REPORT_HEADING)
        blocks = section.split("```")
        assert len(blocks) >= 3, (
            f"section {self.REPORT_HEADING!r} has no fenced template block, so "
            "there is nothing for the model to copy"
        )
        return [ln for ln in blocks[1].splitlines() if ln.startswith(f"{name}: ")]

    def test_the_report_tests_field_admits_a_partial_pass(self):
        """A5. The field asked for "the final full pass, the exit status it
        was read from, which stacks it covered", and a template field is what
        the model fills in when the work is done — so a task that ran
        something narrower either widened it to produce that sentence or wrote
        a sentence its run did not support. Both were observed. It now asks
        for what was run and for what it left out, which is answerable
        honestly whatever the scope was.

        The `_SUITE_MENTION` scan is reused rather than a hand-written list of
        forbidden words: it is the same pattern the line-level audit uses, so
        a demand for a wider run reintroduced here fails on the phrasing that
        audit already knows about rather than on one guessed at here."""
        fields = self._report_field("Tests")
        assert len(fields) == 1, f"section 12 has {len(fields)} `Tests:` fields, not one"
        line = fields[0]
        assert not _SUITE_MENTION.search(line), (
            "the report template asks for a run wider than the change again, "
            f"which is where the demand was hardest to see: {line!r}"
        )
        for required in ("paths", "exit status", "did not cover"):
            assert required in line, (
                f"the `Tests:` field no longer asks for {required!r}, so a narrow "
                f"pass can be reported without saying what it was: {line!r}"
            )

    def test_the_report_records_which_workflow_was_in_force(self):
        """A6. The defaults now yield, so two tasks on one deployment can run
        under different rules and produce reports of the same shape. Without
        this field a reader cannot tell a narrow pass the user asked for from
        one nobody sanctioned, and the deference rule becomes unauditable from
        the only artifact anybody reads."""
        fields = self._report_field("Workflow")
        assert len(fields) == 1, f"section 12 has {len(fields)} `Workflow:` fields, not one"
        line = fields[0]
        for name in ("USER.md", "CHANNEL.md"):
            assert name in line, (
                f"the `Workflow:` field does not offer {name} as an answer, so a "
                f"task working under it has nowhere to say so: {line!r}"
            )
        assert "default" in line.lower(), (
            "the field offers no answer for the deployment that wrote neither "
            f"file, which is every new one: {line!r}"
        )

    @pytest.mark.parametrize(
        "decision,heading", sorted(_DEFERRED_DECISIONS.items())
    )
    def test_every_deferred_decision_says_so_where_it_is_stated(self, decision, heading):
        """A2, the other half. The rule at the top is not enough on its own:
        each decision it governs has to say it yields in the section where the
        decision is made, because that is what the model is reading when it
        acts. Scoped to the section, not to the line — the marker is what
        matters, not the wording, so a rewrite that keeps the deference and
        changes the sentence passes, and a new mandate bullet added elsewhere
        in the same section passes this one too. That gap is what the
        line-level guard above covers."""
        section = self._step_body(self._body(), heading)
        assert any(m in section for m in _DEFERENCE_MARKERS), (
            f"section {heading!r} decides {decision} and never says the user's "
            "own instructions may set it otherwise"
        )


class TestLoadBudget:
    """Goal 5: the split grows what a coding task loads, so the ceiling is
    stated rather than assumed. Parity with the pre-split single file is not
    achievable — `developer` sheds ~52 lines and gains more than that back.

    The ceiling is whatever `.claude/rules/skills.md` documents as the
    contract; this number tracks that one and never leads it. It sat at 675
    here, below the figure the rules file stated, and four fixes to the recipes
    landing in one week (ISSUE-264, -267, -268, -269) ran it out. Fitting them
    meant deleting reviewed content from a recipe the model executes, so the
    test moved to the documented number rather than the recipes shrinking to an
    undocumented one.

    700 → 715 for ISSUE-264, by the procedure the paragraph above prescribes:
    the rules file first, then this. The rule that a test runner's exit status
    is not readable through a pipe costs twelve net lines, and the same
    reasoning applies — a fifth recipe fix in the same week says the figure was
    set below what the recipes cost, not that this fix is too expensive. Raise
    it again the same way, and only that way.

    715 → 730 for Track D of the host-robustness spec, by the same procedure:
    the rules file first, then this. Three rules the 2026-08-20 outage paid
    for and the recipe had no equivalent of — cap the runner's worker count on
    a shared host, run anything that might outlast the 600-second tool call
    detached rather than under `timeout 590`, and never re-run a full suite a
    timeout killed. Twelve net lines, the same order as ISSUE-264's.

    730 → 735 for ISSUE-291, same procedure again: the rules file first, then
    this. The clone recipe never set `core.hooksPath`, so every bare clone the
    skill made ran no pre-commit hooks — the repository's own credential scan
    was inert in exactly the checkouts the agent commits from. Five net lines,
    the smallest of these raises, and the one where a recipe kept short would
    have left a security control silently absent rather than merely unstated.

    735 -> 760 for ISSUE-288, same procedure, and the largest of these raises --
    the only one adding a policy rather than a recipe line. Nothing reaped a
    task's worktree, so `repos_dir` accumulated gigabyte checkouts and "when
    does this go away" was a judgement call each time. The body now states the
    retention rule `worktree_reaper.py` applies, says never to clean up after
    another task (the task that made it may still be running, which the model
    cannot see and the reaper can), names `git worktree lock` as the hatch, and
    corrects the cleanup recipe from `branch -d` -- which consults HEAD, and this
    clone's HEAD deliberately points at a deleted ref (ISSUE-125), so it failed
    on every branch including the merged ones. Twenty-five net lines. Cutting it
    further means dropping either the criteria, and then uncommitted work in a
    worktree looks safe when it is not, or the `--main` warning, which is the
    recurrence guard for the stray checkout that prompted the entry.

    760 -> 761 for ISSUE-304, one net line and the smallest raise there can be,
    by the same procedure: the rules file first, then this. npm, PyPI and
    crates.io are now reachable from a task and nothing else is, so an install
    that fetches from elsewhere -- `node-gyp` headers, Playwright browsers, a
    GitHub release asset -- is refused at the CONNECT boundary. From where the
    model sits that is indistinguishable from a flaky network, and the recipe
    directly above the new line tells it to install per worktree, so without it
    the model retries an install that cannot succeed until the task's budget is
    gone. `docs/deployment/security.md` carries the operator-facing half; the
    model never reads that file.

    761 -> 763 for ISSUE-318, two net lines against that same paragraph,
    because naming the boundary turned out not to be enough to reach the model.
    `npm install` exits 0 when a package's postinstall binary fetch is refused
    there: npm hides lifecycle-script output, so the run prints `added N
    packages` and leaves an empty cache directory, and the ISSUE-304 line never
    fires because nothing was printed and there is no host to name. The second
    of the two covers the error's shape rather than the answer -- the sandbox
    exports `HTTPS_PROXY`, so npm, `uv`, `git` and `curl` are refused at the
    proxy against a `CONNECT` they name themselves, while plain Node ignores
    proxy environment and fails at DNS with
    `EAI_AGAIN`, which reads as a flake and invites exactly the retry the rule
    forbids. Neither point is deducible from the line already there, and both
    were paid for by a task that installed Puppeteer, saw exit 0, and found the
    browser missing at run time.

    763 -> 776 for the development container, same procedure again: the rules
    file first, then this. Thirteen net lines, and the first raise that adds a
    section rather than a rule. On a `[developer.container] backend = "devbox"`
    deployment the package managers and language runtimes run inside the user's
    container, and each of the four consequences is a failure the model cannot
    read out of the error it gets. A file outside the repos root is "no such
    file or directory" from a command that worked one directory over. An
    absolute `.venv/bin/<tool>` is a missing interpreter. A backgrounded dev
    server is gone with no message at all. And 120-123 are statuses no program
    produces, one of which (123) means the command's fate is unknown and has to
    be reported as neither success nor failure. Cutting the section means
    picking which of those the model finds out by guessing.

    The two raises above are independent and both landed: 761 -> 763 on main
    for ISSUE-318, and this branch's +13 on top of it. The number is the sum
    rather than either one, which is what the merge of the two bodies measures.

    ISSUE-337 is the first change to come through without a raise. It removed
    the mandates and added the deference rule, the selection ladder and the
    report's `Workflow:` field for no net lines, which is the trade a change
    that deletes orders should be able to make. What it leaves behind is worth
    knowing before the next edit: the bundle is at 776 of 776, so the next line
    added anywhere in the three bodies raises the budget, rules file first.
    """

    BUDGET_LINES = 776

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

    def test_the_rules_file_states_the_same_number(self):
        """A7. The procedure every raise above followed — the rules file
        first, then this — was a convention nothing checked, and a number
        raised only here is a budget that has stopped meaning anything. The
        reason for a raise lives in prose that cannot be asserted on; that the
        prose was written at all can be."""
        rules = Path(__file__).resolve().parents[1] / ".claude" / "rules" / "skills.md"
        stated = re.findall(r"(\d+)-line budget", rules.read_text())
        assert stated == [str(self.BUDGET_LINES)], (
            f"`.claude/rules/skills.md` states {stated or 'no'} budget(s), not "
            f"exactly [{self.BUDGET_LINES!r}]. Raise it there first, with the reason "
            "beside the other raises, and only then here. A second figure left "
            "behind by an earlier raise is the same defect as a missing one: the "
            "file then contradicts itself about what the contract is"
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


class TestTheCloneEnablesTheRepositorysOwnHooks:
    """ISSUE-291. `core.hooksPath` is per-clone local config, so a bare clone
    made by the recipe used `$GIT_DIR/hooks` — nothing but `.sample` files —
    and a repository whose committed hooks scan staged content for credentials
    got none of that in the checkouts the agent actually commits from. The
    gitleaks work is inert without this line, which is why it is run here
    rather than read."""

    FRAGMENT = ("config core.hooksPath", "config core.hooksPath")

    def test_the_recipe_sets_it_on_the_bare_clone(self, bare_clone):
        _run_fragment(_extract(*self.FRAGMENT), bare_clone)

        assert _git(bare_clone, "config", "--get", "core.hooksPath").strip() == ".githooks"

    def test_a_worktree_inherits_it(self, bare_clone, tmp_path):
        """Worktrees are where commits happen, and they read the bare repo's
        config — so setting it once at clone time covers every branch."""
        _run_fragment(_extract(*self.FRAGMENT), bare_clone)
        tree = tmp_path / "wt"
        _git(bare_clone, "worktree", "add", "-q", str(tree), "main")

        assert _git(tree, "config", "--get", "core.hooksPath").strip() == ".githooks"

    def test_an_existing_clone_gets_it_on_a_later_pass(self, bare_clone):
        """`docs/development/secret-scanning.md` tells the reader that a clone
        predating this step repairs itself rather than needing the config
        applied by hand. That holds only because the line sits outside the
        `if [ ! -d "$BARE_DIR" ]` guard, which the fragment test above cannot
        see — so run the whole block against a directory that already exists."""
        assert subprocess.run(
            ["git", "-C", str(bare_clone), "config", "--get", "core.hooksPath"],
            capture_output=True,
        ).returncode != 0, "precondition: the fixture clone has no hooksPath set"

        fragment = _extract('FRESH=""', "config core.hooksPath")
        assert 'if [ ! -d "$BARE_DIR" ]' in fragment, (
            "the extracted range has to span the guard, or this passes for free"
        )
        _run_fragment(fragment, bare_clone)

        assert _git(bare_clone, "config", "--get", "core.hooksPath").strip() == ".githooks"


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


class TestCredentialFreeConfigs:
    """ISSUE-270. A credential in a git config routes around the credential
    helper the skill registers, and every worktree cut from the clone inherits
    it. `git remote -v` and `git config --list` then print it into the model's
    context as a matter of routine. The daemon strips these on the way in
    (`istota.git_remote_scrub`); the body has to state the invariant and give
    the model a check too, because the daemon's sweep runs at setup and the
    model can be handed a repository at any point after that."""

    def _body(self) -> str:
        return (_BUNDLED_SKILLS_DIR / "developer" / "skill.md").read_text()

    def _preflight(self) -> str:
        return _extract("git config --list --includes | awk", "# end of the credential check")

    def test_the_invariant_is_stated(self):
        assert "**A remote URL never carries a credential.**" in self._body(), (
            "the clone/push recipes are the step that would otherwise bake one in"
        )

    def test_the_body_never_shows_a_credentialed_url(self):
        """The cheapest way to teach the model the wrong thing is an example.
        No line of the body may carry a `scheme://user:secret@host` shape."""
        for i, line in enumerate(self._body().splitlines(), 1):
            assert not re.search(r"://[^/@\s]+:[^/@\s]+@", line), (
                f"developer/skill.md:{i} shows a credentialed URL: {line!r}"
            )

    def test_flags_a_credentialed_remote_by_name_only(self, bare_clone):
        """The fragment is *run*, not restated. It must print the setting's
        name and never the value — echoing the value is the leak it looks for."""
        _git(bare_clone, "remote", "add", "leaky",
             "https://oauth2:glpat-xxxxxxxxxxxxxxxxxxxx@gitlab.com/ns/p.git")

        out = _run_fragment(f'cd "$BARE_DIR"\n{self._preflight()}', bare_clone)

        assert out.split() == ["remote.leaky.url"]
        assert "glpat-xxxxxxxxxxxxxxxxxxxx" not in out

    def test_is_silent_on_credential_free_remotes(self, bare_clone):
        """A bare-username https remote and an scp-style ssh remote both
        contain an `@` and neither carries a secret. Flagging them would make
        the check noise the model learns to skip."""
        _git(bare_clone, "remote", "add", "ssh", "git@github.com:ns/p.git")
        _git(bare_clone, "remote", "add", "user", "https://oauth2@gitlab.com/ns/p.git")
        _git(bare_clone, "remote", "set-url", "origin", "https://gitlab.com/ns/p.git")

        out = _run_fragment(f'cd "$BARE_DIR"\n{self._preflight()}', bare_clone)

        assert out.strip() == "", f"false positive: {out!r}"

    def test_catches_a_pushurl(self, bare_clone):
        """`git remote -v` prints the pushurl on its own line, so a credential
        there leaks exactly the same way."""
        _git(bare_clone, "remote", "set-url", "origin", "https://gitlab.com/ns/p.git")
        _git(bare_clone, "config", "remote.origin.pushurl",
             "https://oauth2:glpat-xxxxxxxxxxxxxxxxxxxx@gitlab.com/ns/p.git")

        out = _run_fragment(f'cd "$BARE_DIR"\n{self._preflight()}', bare_clone)

        assert out.split() == ["remote.origin.pushurl"]

    def test_catches_an_empty_username(self, bare_clone):
        """`https://:tok@host/x` is a credential the daemon strips. A check
        that misses it disagrees with the sweep it is documented to back up,
        and the model-facing one is the weaker of the two."""
        _git(bare_clone, "remote", "add", "leaky", "https://:glpat-xxxxxxxxxxxxxxxxxxxx@h/x.git")

        out = _run_fragment(f'cd "$BARE_DIR"\n{self._preflight()}', bare_clone)

        assert out.split() == ["remote.leaky.url"]

    def test_catches_a_credential_riding_in_a_key(self, bare_clone):
        """`url.<base>.insteadOf` puts the secret in the key, so `remote -v`
        shows something clean while every fetch is rewritten through it."""
        _git(bare_clone, "config",
             "url.https://oauth2:glpat-xxxxxxxxxxxxxxxxxxxx@example.com/.insteadOf",
             "https://example.com/")

        out = _run_fragment(f'cd "$BARE_DIR"\n{self._preflight()}', bare_clone)

        assert "credential embedded in a config key" in out
        assert "glpat-xxxxxxxxxxxxxxxxxxxx" not in out, "the check printed the secret"

    def test_catches_an_extraheader(self, bare_clone):
        """An Authorization header never appears in a URL at all."""
        _git(bare_clone, "config", "http.https://gitlab.com/.extraheader",
             "AUTHORIZATION: basic eHh4eHh4eHh4")

        out = _run_fragment(f'cd "$BARE_DIR"\n{self._preflight()}', bare_clone)

        assert out.split() == ["http.https://gitlab.com/.extraheader"]
        assert "eHh4eHh4eHh4" not in out

    def test_does_not_print_a_port_as_a_credential(self, bare_clone):
        """`https://gitlab.com:8443/ns/p.git` has a colon before no `@` of its
        own — a naive pattern reads the port as a password."""
        _git(bare_clone, "remote", "set-url", "origin", "https://gitlab.com:8443/ns/p.git")

        out = _run_fragment(f'cd "$BARE_DIR"\n{self._preflight()}', bare_clone)

        assert out.strip() == "", f"false positive on a port: {out!r}"


def _section(body: str, heading: str) -> str:
    """One `##`/`###` section: the heading through the next heading of any depth."""
    start = body.index(heading)
    rest = body[start + len(heading) :]
    nxt = re.search(r"^#{2,4} ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


# Anything whose exit status *is* the verification result. A pipe on one of
# these is the failure this class exists to stop.
_TEST_RUNNERS = (
    "pytest",
    "vitest",
    "jest",
    "go test",
    "npm test",
    "npm run check",
    "npm run lint",
    "npm --prefix",
    "make check",
    "just check",
    "tox",
    "ruff check",
    "tsc",
    "svelte-check",
)


class TestVerificationBudgetKeepsTheExitStatus:
    """ISSUE-264. Section 7 is entirely about *shrinking* test output — quiet
    flags, `-x`, one command one output — and every one of those instructions
    pushes toward a pipeline. A pipeline reports the status of its last command,
    so `pytest … | tail` exits 0 on a suite that failed and the run reads as
    green. That happened twice in one month; the real result was caught by
    reading the output, which is luck rather than method.

    The document already knows this — the worktree recipe carries the same
    lesson about `cmd | sed || echo main` — but it is attached to a different
    command five sections away from the one where it costs the most. A rule of
    this shape only holds where the tempting command is written.
    """

    # Prints *and* fails. A stand-in that only failed would let a non-zero exit
    # caused by the plumbing — an unwritable path, a missing `tail` — pass for
    # the status the recipe is supposed to be carrying.
    FAILING_RUN = "sh -c 'echo SUITE_OUTPUT_MARKER; exit 1'"

    def _body(self) -> str:
        return (_BUNDLED_SKILLS_DIR / "developer" / "skill.md").read_text()

    def _run_fence(self, marker: str, work_dir: Path) -> subprocess.CompletedProcess:
        """The fence containing `marker`, run with the suite swapped out.

        `WORK_DIR` is passed because the capture recipe writes under it: two
        developer tasks can be in section 7 at once, so an absolute shared path
        would show one job the other's failures — and under `-n auto` the same
        collision lands inside this suite.
        """
        block = _fenced_block(self._body(), marker)
        return _run_recipe(
            block.replace("uv run pytest -q --no-header", self.FAILING_RUN),
            Path("/nonexistent"),
            WORK_DIR=str(work_dir),
        )

    def test_the_budget_says_a_pipe_discards_the_test_status(self):
        """The one line the entry asked for, in section 7 rather than in a
        general principles section elsewhere."""
        budget = _section(self._body(), "### 7. The verification budget")
        assert "pipefail" in budget, (
            "the verification budget tells the model to trim test output and "
            "never says a pipe discards the status it is trimming"
        )

    def test_the_budget_shows_a_pipeline_that_carries_the_runners_status(self, tmp_path):
        """Run rather than pattern-matched. Asserting that `set -o pipefail`
        appears above a pipe would equally accept it appearing in a *different*
        Bash call, which buys the pipeline nothing — options do not survive
        between fences."""
        proc = self._run_fence("| tail -n 20", tmp_path)

        assert "SUITE_OUTPUT_MARKER" in proc.stdout, (
            f"the recipe never produced the run's output: {proc.stderr!r}"
        )
        assert proc.returncode != 0, (
            "the recipe trims a failing suite's output and still exits 0 — "
            "which is the defect it is supposed to demonstrate away"
        )

    def test_the_budget_shows_a_capture_that_survives_a_shell_without_pipefail(self, tmp_path):
        """The second remedy. `pipefail` is not set in every shell the model
        gets, so the entry asked for the capture-and-check form too."""
        proc = self._run_fence("STATUS=$?", tmp_path)

        assert "SUITE_OUTPUT_MARKER" in proc.stdout, (
            f"the recipe never read the log back: {proc.stderr!r}"
        )
        assert proc.returncode != 0, "the capture recipe swallowed a failing suite's status"

    def test_the_report_names_the_status_the_pass_was_read_from(self):
        """Second line of defence. `Tests: full suite green` is the sentence a
        human acts on, so the template has to ask where "green" came from."""
        report = _section(self._body(), "### 12. Report")
        assert "exit status" in report, (
            "the report template accepts a claim of a green suite without "
            "naming the exit status it was read from"
        )

    def test_no_recipe_pipes_a_test_runner_without_pipefail(self):
        """The document must not model the habit it forbids. Scoped to runners,
        because their exit status *is* the answer — unlike a `git` read whose
        output is the answer.

        Backslash continuations are joined first, as
        `test_no_runnable_glab_line_uses_a_gh_only_filter_flag` does: section 7
        tells the model to chain the linters and the tests into one invocation,
        which is written across lines, and a runner parked on a line carrying no
        `|` would slip a per-line scan.
        """
        inspected = []
        for block in _fenced_blocks(self._body()):
            lines = re.sub(r"\\\n\s*", " ", block).splitlines()
            pipefail = next(
                (i for i, line in enumerate(lines) if "set -o pipefail" in line), None
            )
            for i, line in enumerate(lines):
                if line.lstrip().startswith("#") or "|" not in line:
                    continue
                left = line.split("|")[0]
                runner = next((r for r in _TEST_RUNNERS if r in left), None)
                if runner is None:
                    continue
                inspected.append(line.strip())
                assert pipefail is not None and pipefail < i, (
                    f"developer pipes `{runner}` with no pipefail above it, so "
                    f"the suite's status is discarded: {line.strip()!r}"
                )

        # Without this the guard is green on a document that deleted the very
        # recipe ISSUE-264 asked it to keep. Same reason as the `assert piping`
        # in `test_every_piping_recipe_sets_pipefail_before_it_pipes`.
        assert inspected, "no piped test-runner line found — the guard is inspecting nothing"
