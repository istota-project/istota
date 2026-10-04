"""`istota-dev`, the developer skill's in-sandbox repository helper.

Driven in-process through `main()` against real repositories: the forge URL in
the helper's config is a `file://` directory holding non-bare upstreams, so
`clone` runs end to end with no network. The bare-clone cases are the ones
`tests/test_developer_workflow_skills.py::TestBareCloneRecipe` ran against the
shell recipe, ported to the program that replaces it.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from istota.sandbox import git_remote_scrub
from istota.skills.developer import istota_dev

HELPER = Path(istota_dev.__file__)

GIT_ISOLATION = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}

# Placeholder-shaped so the pre-commit scans read them as documentation.
SECRET = "glpat-xxxxxxxxxxxxxxxxxxxx"
HEADER_SECRET = "eHh4eHh4eHh4"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{proc.stderr}")
    return proc.stdout


def _git_rc(cwd: Path, *args: str) -> int:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True).returncode


def _refs(bare: Path, prefix: str = "") -> list[str]:
    args = ["for-each-ref", "--format=%(refname)"] + ([prefix] if prefix else [])
    return _git(bare, *args).split()


def _drop_origin_head(bare: Path) -> None:
    """Git 2.48+ writes `refs/remotes/origin/HEAD` on fetch; the devbox's 2.39
    does not. Normalising to absent keeps these tests honest on both."""
    subprocess.run(["git", "-C", str(bare), "symbolic-ref", "-d", "refs/remotes/origin/HEAD"],
                   capture_output=True)


class Env:
    def __init__(self, tmp_path: Path, monkeypatch, capsys):
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        self.capsys = capsys
        self.forge = tmp_path / "forge"
        self.repos = tmp_path / "repos" / "alice"
        self.config = tmp_path / "istota-dev.json"
        self.forge.mkdir()
        self.repos.mkdir(parents=True)
        self.write_config({"gitlab": {"url": self.forge.as_uri()}})

    def write_config(self, forges: dict, **overrides) -> None:
        doc = {"version": 1, "repos_dir": str(self.repos), "bot_dir": "istota",
               "forges": forges}
        doc.update(overrides)
        self.config.write_text(json.dumps(doc))

    def upstream(self, path: str = "acme/widget", branch: str = "main",
                 files: dict | None = None, empty: bool = False) -> Path:
        repo = self.forge / f"{path}.git"
        repo.mkdir(parents=True)
        _git(repo, "init", "-q", "-b", branch, ".")
        if empty:
            return repo
        for name, text in (files or {"src/app.py": "print('v1')\n"}).items():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text(text)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")
        return repo

    def bare(self, path: str = "acme/widget") -> Path:
        return self.repos / f"{path}.git"

    def run(self, *argv: str, raw: bool = False):
        self.capsys.readouterr()
        code = istota_dev.main(list(argv), config_path=self.config)
        out, err = self.capsys.readouterr()
        if raw:
            return code, out, err
        assert out.count("\n") == 1, f"expected one JSON line on stdout, got {out!r}"
        return code, json.loads(out)

    def clone(self, repo: str = "acme/widget") -> dict:
        code, payload = self.run("clone", repo)
        assert code == 0, payload
        return payload


@pytest.fixture
def env(tmp_path, monkeypatch, capsys) -> Env:
    empty_global = tmp_path / "gitconfig"
    empty_global.write_text("")
    for key, value in GIT_ISOLATION.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_global))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setenv("ISTOTA_TASK_ID", "412")
    for key in ("DEVELOPER_REPOS_DIR", "GIT_DIR", "GIT_WORK_TREE"):
        monkeypatch.delenv(key, raising=False)
    return Env(tmp_path, monkeypatch, capsys)


def _assert_invariant(bare: Path, branch: str = "main") -> None:
    """origin/HEAD resolves; HEAD is a `refs/heads/` ref that does not."""
    assert _git(bare, "rev-parse", "--verify", "origin/HEAD").strip()
    assert _git(bare, "symbolic-ref", "HEAD").strip() == f"refs/heads/{branch}"
    assert _git_rc(bare, "rev-parse", "--verify", branch) != 0, (
        f"a local `{branch}` resolved; the clone-day fossil is readable"
    )


def _pre_269_clone(env: Env, path: str = "acme/widget") -> Path:
    """A bare clone in the shape clones made before ISSUE-269 are still in:
    refspec set, fetched, HEAD pointed into `refs/remotes/`, fossil present."""
    upstream = env.forge / f"{path}.git"
    bare = env.bare(path)
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(env.tmp, "clone", "-q", "--bare", str(upstream), str(bare))
    _git(bare, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    _git(bare, "fetch", "-q", "origin")
    _git(bare, "symbolic-ref", "HEAD", "refs/remotes/origin/main")
    _drop_origin_head(bare)
    return bare


# --------------------------------------------------------------------------
# clone
# --------------------------------------------------------------------------

class TestClone:
    def test_a_fresh_clone_reaches_the_documented_shape(self, env):
        upstream = env.upstream()
        _git(upstream, "branch", "feature")

        out = env.clone("gitlab:acme/widget")

        bare = env.bare()
        assert out == {
            "bare_dir": str(bare),
            "default_branch": "main",
            "fresh": True,
            "fossils_removed": ["feature", "main"],
            "hooks_path": ".githooks",
        }
        assert _git(bare, "config", "--get", "remote.origin.fetch").strip() == (
            "+refs/heads/*:refs/remotes/origin/*"
        )
        assert _git(bare, "config", "--get", "core.hooksPath").strip() == ".githooks"
        assert _refs(bare, "refs/heads/") == [], "a clone-day fossil survived"
        assert "refs/remotes/origin/main" in _refs(bare)
        assert "refs/remotes/origin/feature" in _refs(bare)
        _assert_invariant(bare)

    def test_the_single_configured_forge_needs_no_prefix(self, env):
        env.upstream()
        assert env.clone("acme/widget")["fresh"] is True

    def test_a_second_run_changes_nothing(self, env):
        env.upstream()
        env.clone()
        out = env.clone()
        assert out["fresh"] is False
        assert out["fossils_removed"] == []
        _assert_invariant(env.bare())

    # ISSUE-623: a clone interrupted between `clone --bare` and the refspec
    # write left a directory no later run repaired.
    def test_a_bare_clone_missing_its_refspec_is_repaired(self, env):
        upstream = env.upstream()
        bare = env.bare()
        bare.parent.mkdir(parents=True, exist_ok=True)
        _git(env.tmp, "clone", "-q", "--bare", str(upstream), str(bare))
        assert _git_rc(bare, "config", "--get", "remote.origin.fetch") != 0

        out = env.clone()

        assert out["fresh"] is False
        assert out["default_branch"] == "main"
        assert _git(bare, "config", "--get-all", "remote.origin.fetch").splitlines() == [
            "+refs/heads/*:refs/remotes/origin/*"
        ]
        assert "refs/remotes/origin/main" in _refs(bare)

    def test_a_healthy_clone_keeps_one_refspec(self, env):
        env.upstream()
        env.clone()
        env.clone()
        assert _git(env.bare(), "config", "--get-all", "remote.origin.fetch").splitlines() == [
            "+refs/heads/*:refs/remotes/origin/*"
        ]

    def test_a_second_refspec_survives_and_does_not_fail_the_run(self, env):
        env.upstream()
        env.clone()
        extra = "+refs/merge-requests/*/head:refs/remotes/origin/mr/*"
        _git(env.bare(), "config", "--add", "remote.origin.fetch", extra)

        env.clone()

        assert _git(env.bare(), "config", "--get-all", "remote.origin.fetch").splitlines() == [
            "+refs/heads/*:refs/remotes/origin/*", extra,
        ]

    def test_master_is_read_not_assumed(self, env):
        env.upstream(branch="master")
        out = env.clone()
        assert out["default_branch"] == "master"
        _assert_invariant(env.bare(), branch="master")

    def test_a_pre_269_clone_is_repaired(self, env):
        """ISSUE-269 and ISSUE-125 together: HEAD under `refs/remotes/`, the
        fossil readable, `origin/HEAD` absent. The helper never re-clones an
        existing directory, so the repair path is the only one that reaches it."""
        env.upstream()
        bare = _pre_269_clone(env)
        assert "refs/heads/main" in _refs(bare)

        out = env.clone()

        assert out["fresh"] is False
        assert out["fossils_removed"] == ["main"]
        _assert_invariant(bare)
        _git(bare, "worktree", "add", "-q", "-b", "istota/1-slug",
             str(env.tmp / "wt"), "origin/main")

    def test_an_existing_clone_gets_the_hooks_path(self, env):
        """ISSUE-291. A clone made before the step existed has no
        `core.hooksPath`, and the repository's committed credential scan never
        runs in its worktrees. `docs/development/secret-scanning.md` says such a
        clone repairs itself on the next run."""
        env.upstream()
        bare = _pre_269_clone(env)
        assert _git_rc(bare, "config", "--get", "core.hooksPath") != 0

        env.clone()

        assert _git(bare, "config", "--get", "core.hooksPath").strip() == ".githooks"

    def test_a_worktree_inherits_the_hooks_path(self, env):
        """Commits happen in worktrees, which read the bare clone's config."""
        env.upstream()
        env.clone()
        work_dir = env.run("worktree", "acme/widget", "hooks")[1]["work_dir"]
        assert _git(Path(work_dir), "config", "--get", "core.hooksPath").strip() == ".githooks"

    def test_a_task_branch_survives_on_an_existing_clone(self, env):
        """On an existing clone `refs/heads/` holds every task branch, including
        one whose worktree was pruned and which may be the only copy."""
        env.upstream()
        env.clone()
        bare = env.bare()
        _git(bare, "worktree", "add", "-q", "-b", "istota/9-live",
             str(env.tmp / "live"), "origin/main")
        _git(bare, "branch", "istota/8-pruned", "origin/main")

        env.clone()

        heads = _refs(bare, "refs/heads/")
        assert "refs/heads/istota/8-pruned" in heads
        assert "refs/heads/istota/9-live" in heads

    def test_a_checked_out_default_branch_is_not_deleted(self, env):
        """The worktree list is what decides what survives `update-ref -d`."""
        env.upstream()
        env.clone()
        bare = env.bare()
        _git(bare, "worktree", "add", "-q", "-b", "main", str(env.tmp / "on-main"), "origin/main")

        out = env.clone()

        assert out["fossils_removed"] == []
        assert "refs/heads/main" in _refs(bare, "refs/heads/")

    def test_a_dangling_origin_head_is_refreshed(self, env):
        env.upstream()
        bare = _pre_269_clone(env)
        _git(bare, "update-ref", "refs/remotes/origin/gone", "refs/remotes/origin/main")
        _git(bare, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/gone")
        _git(bare, "update-ref", "-d", "refs/remotes/origin/gone")

        env.clone()

        _assert_invariant(bare)

    def test_a_detached_head_is_repaired(self, env):
        env.upstream()
        bare = _pre_269_clone(env)
        _git(bare, "update-ref", "--no-deref", "HEAD", "refs/remotes/origin/main")

        env.clone()

        _assert_invariant(bare)

    def test_an_empty_upstream_has_no_default_branch(self, env):
        env.upstream(empty=True)
        code, out = env.run("clone", "acme/widget")
        assert code == istota_dev.EXIT_GIT
        assert out["error"] == "origin has no default branch"

    def test_a_missing_upstream_is_a_git_failure(self, env):
        code, out = env.run("clone", "acme/nothing")
        assert code == istota_dev.EXIT_GIT
        assert out["command"][:2] == ["git", "clone"]

    def test_a_forge_url_with_userinfo_is_refused_before_anything_runs(self, env):
        env.write_config({"gitlab": {"url": f"https://oauth2:{SECRET}@gitlab.example.com"}})
        code, out, err = env.run("clone", "acme/widget", raw=True)
        assert code == istota_dev.EXIT_CREDENTIAL
        assert SECRET not in out + err
        assert not env.bare().exists()

    def test_no_configured_forge_is_a_usage_error(self, env):
        env.write_config({})
        code, out = env.run("clone", "acme/widget")
        assert code == istota_dev.EXIT_USAGE

    def test_an_unconfigured_forge_is_a_usage_error(self, env):
        code, out = env.run("clone", "github:acme/widget")
        assert code == istota_dev.EXIT_USAGE
        assert out["configured"] == ["gitlab"]

    def test_two_forges_need_a_choice(self, env):
        env.write_config({"gitlab": {"url": env.forge.as_uri()},
                          "github": {"url": "https://github.com"}})
        env.upstream()
        assert env.run("clone", "acme/widget")[0] == istota_dev.EXIT_USAGE
        assert env.run("clone", "acme/widget", "--forge", "gitlab")[0] == 0

    def test_a_nested_group_lands_at_its_own_path(self, env):
        env.upstream("group/sub/project")
        out = env.clone("group/sub/project")
        assert out["bare_dir"] == str(env.repos / "group" / "sub" / "project.git")

    @pytest.mark.parametrize("repo", [
        "widget", "acme/../widget", "acme/./widget", "-acme/widget", "acme/-x",
        "acme//widget", "acme/wid get", "acme/widget.git", "/acme/widget",
        "acme.git/widget", "acme/widget\n",
    ])
    def test_a_bad_repository_path_is_a_usage_error(self, env, repo):
        code, out = env.run("clone", repo)
        assert code == istota_dev.EXIT_USAGE, out
        assert not any(env.repos.iterdir())


# --------------------------------------------------------------------------
# The credential check
# --------------------------------------------------------------------------

# (config key, value, flagged). Shared by both detectors below: the helper's
# tripwire and the daemon's `git_remote_scrub` must agree on every row. One
# divergence is deliberate and left out: a non-auth `extraheader`, which the
# helper flags (it only stops) and the scrub leaves alone (it rewrites).
CREDENTIAL_CASES = [
    ("remote.leaky.url", f"https://oauth2:{SECRET}@gitlab.example.com/ns/p.git", True),
    ("remote.origin.pushurl", f"https://oauth2:{SECRET}@gitlab.example.com/ns/p.git", True),
    ("remote.leaky.url", f"https://:{SECRET}@gitlab.example.com/ns/p.git", True),
    ("remote.leaky.url", f"https://{SECRET}@gitlab.example.com/ns/p.git", True),
    (f"url.https://oauth2:{SECRET}@example.com/.insteadOf", "https://example.com/", True),
    ("http.https://gitlab.example.com/.extraheader", f"AUTHORIZATION: basic {HEADER_SECRET}", True),
    ("remote.ssh.url", "git@github.com:ns/p.git", False),
    ("remote.user.url", "https://oauth2@gitlab.example.com/ns/p.git", False),
    ("remote.port.url", "https://gitlab.example.com:8443/ns/p.git", False),
    ("remote.empty.url", "https://user:@gitlab.example.com/ns/p.git", False),
    ("remote.query.url", "https://gitlab.example.com/ns/p.git?x=a:b@c", False),
]
_IDS = [f"{'flag' if f else 'clean'}-{i}" for i, (_, _, f) in enumerate(CREDENTIAL_CASES)]


def _expected_key(key: str) -> str:
    """What git lists for `key` (the variable name lowercased) and what the
    helper prints for it (userinfo inside a key redacted)."""
    section, _, name = key.rpartition(".")
    return istota_dev.redact(f"{section}.{name.lower()}")


class TestTheCredentialCheck:
    @pytest.mark.parametrize("key,value,flagged", CREDENTIAL_CASES, ids=_IDS)
    def test_clone_stops_on_a_credential_and_names_only_the_key(self, env, key, value, flagged):
        env.upstream()
        env.clone()
        _git(env.bare(), "config", key, value)

        code, out, err = env.run("clone", "acme/widget", raw=True)

        if not flagged:
            assert code == 0, out
            return
        assert code == istota_dev.EXIT_CREDENTIAL
        payload = json.loads(out)
        assert payload["keys"] == [_expected_key(key)]
        for secret in (SECRET, HEADER_SECRET):
            assert secret not in out + err, "the check printed the secret"

    @pytest.mark.parametrize("key,value,flagged", CREDENTIAL_CASES, ids=_IDS)
    def test_the_daemon_scrub_flags_the_same_inputs(self, tmp_path, monkeypatch, key, value, flagged):
        """Two statements of one rule: the scrub strips at setup, the helper is
        the tripwire for one that appeared afterwards."""
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        helper_repo = tmp_path / "helper.git"
        scrub_repo = tmp_path / "scrub.git"
        for repo in (helper_repo, scrub_repo):
            _git(tmp_path, "init", "-q", "--bare", str(repo))
            _git(repo, "config", key, value)

        helper_flags = bool(istota_dev.credential_findings(helper_repo))
        scrub_flags = bool(git_remote_scrub.scrub_config(
            scrub_repo / "config", scrub_repo, tmp_path,
        ))

        assert helper_flags is flagged
        assert scrub_flags is flagged

    def test_a_non_auth_extraheader_is_flagged_by_the_helper_alone(self):
        """The one stated divergence, pinned so it stays a decision."""
        entries = [("http.https://gitlab.example.com/.extraheader", "X-Trace: 1")]
        assert istota_dev.config_findings(entries) == [entries[0][0]]

    def test_a_value_with_a_newline_cannot_forge_a_record(self):
        data = (
            "remote.origin.url\nhttps://gitlab.example.com/a.git\nremote.x.url\0"
            "core.bare\ntrue\0"
        )
        entries = istota_dev.parse_config_list(data)
        assert [k for k, _ in entries] == ["remote.origin.url", "core.bare"]

    def test_the_token_prefixes_match_the_scrub(self):
        assert istota_dev.TOKEN_PREFIXES == git_remote_scrub._TOKEN_PREFIXES

    def test_git_errors_are_redacted(self):
        text = f"fatal: unable to access 'https://oauth2:{SECRET}@gitlab.example.com/x.git/'"
        assert SECRET not in istota_dev.redact(text)
        assert "https://***@gitlab.example.com" in istota_dev.redact(text)


# --------------------------------------------------------------------------
# worktree
# --------------------------------------------------------------------------

class TestWorktree:
    def test_a_new_worktree(self, env):
        env.upstream()
        env.clone()

        code, out = env.run("worktree", "acme/widget", "fix-null-deref")

        work = env.repos / "acme" / "widget--istota-412-fix-null-deref"
        assert code == 0, out
        assert out == {
            "work_dir": str(work),
            "branch": "istota/412-fix-null-deref",
            "base": "origin/main",
            "existing": False,
            "agents_file": None,
        }
        assert (work / "src" / "app.py").read_text() == "print('v1')\n"
        assert _git(work, "rev-parse", "--abbrev-ref", "HEAD").strip() == (
            "istota/412-fix-null-deref"
        )
        assert _git(work, "config", "--get", "core.hooksPath").strip() == ".githooks"

    def test_a_second_call_returns_the_existing_worktree(self, env):
        env.upstream()
        env.clone()
        env.run("worktree", "acme/widget", "fix")
        code, out = env.run("worktree", "acme/widget", "fix")
        assert code == 0
        assert out["existing"] is True

    def test_an_existing_worktree_reports_no_base(self, env):
        """The second call's base is not the one the worktree was cut from, and
        `glab mr create --target-branch` is where a wrong one would land."""
        upstream = env.upstream()
        _git(upstream, "branch", "release")
        env.clone()
        env.run("worktree", "acme/widget", "fix", "--base", "origin/release")
        assert env.run("worktree", "acme/widget", "fix")[1]["base"] is None

    @pytest.mark.parametrize("name", ["AGENTS.md", "CLAUDE.md"])
    def test_the_instruction_file_is_named(self, env, name):
        env.upstream(files={name: "conventions\n"})
        env.clone()
        assert env.run("worktree", "acme/widget", "fix")[1]["agents_file"] == name

    def test_agents_md_wins_over_claude_md(self, env):
        env.upstream(files={"AGENTS.md": "a\n", "CLAUDE.md": "c\n"})
        env.clone()
        assert env.run("worktree", "acme/widget", "fix")[1]["agents_file"] == "AGENTS.md"

    def test_a_path_taken_by_something_else_is_refused(self, env):
        env.upstream()
        env.clone()
        taken = env.repos / "acme" / "widget--istota-412-fix"
        taken.mkdir()
        (taken / "keep.txt").write_text("not ours\n")

        code, out = env.run("worktree", "acme/widget", "fix")

        assert code == istota_dev.EXIT_MISSING
        assert (taken / "keep.txt").read_text() == "not ours\n"

    def test_another_branchs_worktree_at_the_path_is_refused(self, env):
        env.upstream()
        env.clone()
        taken = env.repos / "acme" / "widget--istota-412-fix"
        _git(env.bare(), "worktree", "add", "-q", "-b", "other", str(taken), "origin/main")

        assert env.run("worktree", "acme/widget", "fix")[0] == istota_dev.EXIT_MISSING

    def test_no_clone_yet(self, env):
        code, out = env.run("worktree", "acme/widget", "fix")
        assert code == istota_dev.EXIT_MISSING
        assert out["hint"] == "run istota-dev clone first"

    @pytest.mark.parametrize("slug", ["Fix", "-fix", "fix-", "fix_it", "", "a" * 49, "fix/it", "fix\n"])
    def test_a_bad_slug_is_a_usage_error(self, env, slug):
        env.upstream()
        env.clone()
        assert env.run("worktree", "acme/widget", slug)[0] == istota_dev.EXIT_USAGE

    def test_a_48_character_slug_is_accepted(self, env):
        env.upstream()
        env.clone()
        assert env.run("worktree", "acme/widget", "a" * 48)[0] == 0

    @pytest.mark.parametrize("base", ["main", "refs/heads/main", "origin/../main", "-x"])
    def test_a_base_outside_origin_is_a_usage_error(self, env, base):
        env.upstream()
        env.clone()
        code, _ = env.run("worktree", "acme/widget", "fix", f"--base={base}")
        assert code == istota_dev.EXIT_USAGE

    def test_an_origin_base_is_used(self, env):
        upstream = env.upstream()
        _git(upstream, "checkout", "-q", "-b", "release")
        (upstream / "REL").write_text("r\n")
        _git(upstream, "add", "REL")
        _git(upstream, "commit", "-q", "-m", "rel")
        env.clone()

        code, out = env.run("worktree", "acme/widget", "fix", "--base", "origin/release")

        assert code == 0, out
        assert out["base"] == "origin/release"
        assert (Path(out["work_dir"]) / "REL").exists()

    @pytest.mark.parametrize("value", [None, "", "abc", "0", "-3"])
    def test_a_missing_task_id_is_a_usage_error(self, env, value):
        env.upstream()
        env.clone()
        if value is None:
            env.monkeypatch.delenv("ISTOTA_TASK_ID")
        else:
            env.monkeypatch.setenv("ISTOTA_TASK_ID", value)
        assert env.run("worktree", "acme/widget", "fix")[0] == istota_dev.EXIT_USAGE

    def test_a_credential_that_breaks_the_fetch_is_still_a_stop(self, env):
        """A planted credential is a likely reason a fetch fails; exit 4 there
        would send the model to `git config --list` to see why."""
        env.upstream()
        env.clone()
        _git(env.bare(), "remote", "set-url", "origin",
             f"https://oauth2:{SECRET}@gitlab.invalid/acme/widget.git")

        for argv in (("clone", "acme/widget"), ("worktree", "acme/widget", "fix")):
            code, out, err = env.run(*argv, raw=True)
            assert code == istota_dev.EXIT_CREDENTIAL, (argv, out)
            assert SECRET not in out + err

    def test_a_credential_stops_it_before_anything_is_created(self, env):
        env.upstream()
        env.clone()
        _git(env.bare(), "config", "remote.leaky.url",
             f"https://oauth2:{SECRET}@gitlab.example.com/ns/p.git")

        code, out, err = env.run("worktree", "acme/widget", "fix", raw=True)

        assert code == istota_dev.EXIT_CREDENTIAL
        assert SECRET not in out + err
        assert not (env.repos / "acme" / "widget--istota-412-fix").exists()

    def test_master_is_the_base_on_a_master_repository(self, env):
        env.upstream(branch="master")
        env.clone()
        assert env.run("worktree", "acme/widget", "fix")[1]["base"] == "origin/master"

    def test_it_works_with_no_forge_configured(self, env):
        env.upstream()
        env.clone()
        env.write_config({})
        assert env.run("worktree", "acme/widget", "fix")[0] == 0


# --------------------------------------------------------------------------
# show
# --------------------------------------------------------------------------

class TestShow:
    def test_it_prints_current_source_after_upstream_moves(self, env):
        upstream = env.upstream()
        env.clone()
        (upstream / "src" / "app.py").write_text("print('v2')\n")
        _git(upstream, "commit", "-q", "-am", "v2")

        code, out, _ = env.run("show", "acme/widget", "src/app.py", raw=True)

        assert code == 0
        assert out == "print('v2')\n"
        assert out == _git(env.bare(), "show", "origin/HEAD:src/app.py")

    def test_an_object_id_ref_is_accepted(self, env):
        upstream = env.upstream()
        sha = _git(upstream, "rev-parse", "HEAD").strip()
        env.clone()
        code, out, _ = env.run("show", "acme/widget", "src/app.py", "--ref", sha[:12], raw=True)
        assert code == 0
        assert out == "print('v1')\n"

    @pytest.mark.parametrize("ref", ["main", "HEAD", "refs/heads/main", "origin/../x", "abc", "origin/main\n"])
    def test_a_local_ref_is_refused(self, env, ref):
        env.upstream()
        env.clone()
        code, out = env.run("show", "acme/widget", "src/app.py", f"--ref={ref}")
        assert code == istota_dev.EXIT_USAGE

    def test_a_missing_file_is_a_git_failure(self, env):
        env.upstream()
        env.clone()
        assert env.run("show", "acme/widget", "nope.py")[0] == istota_dev.EXIT_GIT

    def test_no_clone_yet(self, env):
        assert env.run("show", "acme/widget", "src/app.py")[0] == istota_dev.EXIT_MISSING


# --------------------------------------------------------------------------
# verify-remote
# --------------------------------------------------------------------------

class TestVerifyRemote:
    @pytest.fixture
    def checkout(self, env):
        env.write_config({"gitlab": {"url": "https://gitlab.example.com"},
                          "github": {"url": "https://github.com"}})
        work = env.tmp / "work"
        work.mkdir()
        _git(work, "init", "-q")
        env.monkeypatch.chdir(work)

        def set_origin(url: str) -> None:
            if _git_rc(work, "remote", "get-url", "origin") == 0:
                _git(work, "remote", "set-url", "origin", url)
            else:
                _git(work, "remote", "add", "origin", url)
        return set_origin

    def test_a_match(self, env, checkout):
        checkout("https://gitlab.example.com/acme/widget.git")
        assert env.run("verify-remote", "acme/widget") == (
            0, {"remote": "gitlab.example.com/acme/widget", "forge": "gitlab"},
        )

    def test_a_mismatch(self, env, checkout):
        checkout("https://gitlab.example.com/acme/widget.git")
        code, out = env.run("verify-remote", "acme/other")
        assert code == istota_dev.EXIT_MISMATCH
        assert out["remote"] == "gitlab.example.com/acme/widget"
        assert out["expected"] == "acme/other"

    def test_the_comparison_is_case_sensitive(self, env, checkout):
        checkout("https://gitlab.example.com/acme/widget.git")
        assert env.run("verify-remote", "Acme/widget")[0] == istota_dev.EXIT_MISMATCH

    def test_a_nested_group_is_compared_whole(self, env, checkout):
        checkout("https://gitlab.example.com/group/sub/project.git")
        assert env.run("verify-remote", "group/sub/project")[0] == 0
        assert env.run("verify-remote", "sub/project")[0] == istota_dev.EXIT_MISMATCH

    def test_a_path_prefixed_forge(self, env, checkout):
        env.write_config({"gitlab": {"url": "https://git.example.com/gitlab"}})
        checkout("https://git.example.com/gitlab/acme/widget.git")
        assert env.run("verify-remote", "acme/widget") == (
            0, {"remote": "git.example.com/acme/widget", "forge": "gitlab"},
        )

    def test_an_scp_style_remote(self, env, checkout):
        checkout("git@github.com:acme/widget.git")
        assert env.run("verify-remote", "acme/widget")[1]["forge"] == "github"

    def test_an_unconfigured_host_does_not_match(self, env, checkout):
        checkout("https://elsewhere.example.com/acme/widget.git")
        code, out = env.run("verify-remote", "acme/widget")
        assert code == istota_dev.EXIT_MISMATCH
        assert out["forge"] is None

    @pytest.mark.parametrize("url", [
        f"https://oauth2:{SECRET}@gitlab.example.com/acme/widget.git",
        f"https://{SECRET}@gitlab.example.com/acme/widget.git",
    ])
    def test_a_credentialed_origin_stops_without_printing_it(self, env, checkout, url):
        checkout(url)
        code, out, err = env.run("verify-remote", "acme/widget", raw=True)
        assert code == istota_dev.EXIT_CREDENTIAL
        assert SECRET not in out + err
        assert "gitlab.example.com" not in out

    def test_an_scp_shaped_credential_is_never_echoed(self, env, checkout):
        checkout(f"user:{SECRET}@gitlab.example.com:acme/widget.git")
        code, out, err = env.run("verify-remote", "acme/other", raw=True)
        assert code == istota_dev.EXIT_CREDENTIAL
        assert SECRET not in out + err

    def test_a_bare_username_is_not_a_credential(self, env, checkout):
        checkout("https://oauth2@gitlab.example.com/acme/widget.git")
        assert env.run("verify-remote", "acme/widget")[0] == 0

    # ISSUE-622: `git push` goes to the push URL when one is set.
    def test_a_push_url_to_another_project_is_a_mismatch(self, env, checkout):
        checkout("https://gitlab.example.com/acme/widget.git")
        _git(Path.cwd(), "config", "remote.origin.pushurl",
             "https://gitlab.example.com/someone/else.git")
        code, out = env.run("verify-remote", "acme/widget")
        assert code == istota_dev.EXIT_MISMATCH
        assert out["remote"] == "gitlab.example.com/acme/widget"
        assert out["push_remote"] == "gitlab.example.com/someone/else"
        assert out["expected"] == "acme/widget"

    def test_a_credentialed_push_url_stops(self, env, checkout):
        checkout("https://gitlab.example.com/acme/widget.git")
        _git(Path.cwd(), "config", "remote.origin.pushurl",
             f"https://oauth2:{SECRET}@gitlab.example.com/acme/widget.git")
        code, out, err = env.run("verify-remote", "acme/widget", raw=True)
        assert code == istota_dev.EXIT_CREDENTIAL
        assert SECRET not in out + err

    def test_a_credentialed_push_url_outranks_a_mismatch(self, env, checkout):
        checkout("https://gitlab.example.com/acme/widget.git")
        _git(Path.cwd(), "config", "remote.origin.pushurl",
             f"https://oauth2:{SECRET}@gitlab.example.com/acme/widget.git")
        code, out, err = env.run("verify-remote", "acme/other", raw=True)
        assert code == istota_dev.EXIT_CREDENTIAL
        assert SECRET not in out + err

    def test_a_credentialed_push_url_outranks_an_unparseable_one(self, env, checkout):
        checkout("https://gitlab.example.com/acme/widget.git")
        work = Path.cwd()
        _git(work, "config", "--add", "remote.origin.pushurl", "/srv/elsewhere.git")
        _git(work, "config", "--add", "remote.origin.pushurl",
             f"https://oauth2:{SECRET}@gitlab.example.com/acme/widget.git")
        code, out, err = env.run("verify-remote", "acme/widget", raw=True)
        assert code == istota_dev.EXIT_CREDENTIAL
        assert SECRET not in out + err

    def test_an_unparseable_push_url_is_named(self, env, checkout):
        checkout("https://gitlab.example.com/acme/widget.git")
        _git(Path.cwd(), "config", "remote.origin.pushurl", "/srv/elsewhere.git")
        code, out = env.run("verify-remote", "acme/widget")
        assert code == istota_dev.EXIT_GIT
        assert out["error"] == "could not parse the origin push URL"

    def test_one_bad_push_url_among_several_is_a_mismatch(self, env, checkout):
        checkout("https://gitlab.example.com/acme/widget.git")
        work = Path.cwd()
        _git(work, "config", "--add", "remote.origin.pushurl",
             "https://gitlab.example.com/acme/widget.git")
        _git(work, "config", "--add", "remote.origin.pushurl",
             "git@github.com:someone/else.git")
        code, out = env.run("verify-remote", "acme/widget")
        assert code == istota_dev.EXIT_MISMATCH
        assert out["push_remote"] == "github.com/someone/else"

    def test_a_matching_push_url_still_matches(self, env, checkout):
        checkout("https://gitlab.example.com/acme/widget.git")
        _git(Path.cwd(), "config", "remote.origin.pushurl",
             "git@gitlab.example.com:acme/widget.git")
        assert env.run("verify-remote", "acme/widget") == (
            0, {"remote": "gitlab.example.com/acme/widget", "forge": "gitlab"},
        )

    def test_a_push_insteadof_rewrite_is_followed(self, env, checkout):
        checkout("https://gitlab.example.com/acme/widget.git")
        _git(Path.cwd(), "config",
             "url.https://gitlab.example.com/someone/.pushInsteadOf",
             "https://gitlab.example.com/acme/")
        code, out = env.run("verify-remote", "acme/widget")
        assert code == istota_dev.EXIT_MISMATCH
        assert out["push_remote"] == "gitlab.example.com/someone/widget"

    def test_outside_a_worktree_is_a_git_failure(self, env):
        elsewhere = env.tmp / "nowhere"
        elsewhere.mkdir()
        env.monkeypatch.chdir(elsewhere)
        assert env.run("verify-remote", "acme/widget")[0] == istota_dev.EXIT_GIT


# --------------------------------------------------------------------------
# Inputs and the contract
# --------------------------------------------------------------------------

class TestInputs:
    def test_a_repos_dir_mismatch_is_a_usage_error(self, env):
        env.monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(env.tmp / "other"))
        code, out = env.run("clone", "acme/widget")
        assert code == istota_dev.EXIT_USAGE
        assert out["config_repos_dir"] == str(env.repos)

    def test_a_matching_repos_dir_is_fine(self, env):
        env.upstream()
        env.monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(env.repos) + "/")
        assert env.run("clone", "acme/widget")[0] == 0

    def test_a_missing_config_is_a_usage_error(self, env):
        env.config.unlink()
        assert env.run("clone", "acme/widget")[0] == istota_dev.EXIT_USAGE

    @pytest.mark.parametrize("override", [
        {"version": 2}, {"repos_dir": "relative/dir"}, {"bot_dir": "../x"},
        {"forges": {"bitbucket": {"url": "https://bitbucket.org"}}},
    ])
    def test_an_invalid_config_is_a_usage_error(self, env, override):
        forges = override.pop("forges", {"gitlab": {"url": env.forge.as_uri()}})
        env.write_config(forges, **override)
        assert env.run("clone", "acme/widget")[0] == istota_dev.EXIT_USAGE

    def test_bad_arguments_are_json_and_exit_2(self, env):
        assert env.run("clone")[0] == istota_dev.EXIT_USAGE
        assert env.run("frobnicate")[0] == istota_dev.EXIT_USAGE
        assert env.run()[0] == istota_dev.EXIT_USAGE

    def test_an_internal_error_is_json_without_a_traceback_on_stdout(self, env, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("x")
        monkeypatch.setattr(istota_dev, "load_config", boom)
        code, out, err = env.run("clone", "acme/widget", raw=True)
        assert code == istota_dev.EXIT_GIT
        assert json.loads(out) == {"error": "internal: RuntimeError"}
        assert "Traceback" in err

    def test_the_default_config_sits_beside_the_program(self):
        assert istota_dev.default_config_path() == HELPER.parent / "istota-dev.json"


class TestItRunsWithoutThePackage:
    """It is copied out of the package and run by a bare `python3`, so an
    import of anything outside the standard library breaks it in every sandbox
    while every in-process test here stays green."""

    def test_it_imports_under_a_bare_interpreter(self, tmp_path):
        code = f"import runpy; runpy.run_path({str(HELPER)!r}, run_name='not_main')"
        proc = subprocess.run(
            [sys.executable, "-I", "-S", "-c", code],
            cwd=str(tmp_path), capture_output=True, text=True,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": ""},
        )
        assert proc.returncode == 0, proc.stderr

    def test_every_import_is_standard_library(self):
        tree = ast.parse(HELPER.read_text())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, "a relative import ties it to the package"
                names.add((node.module or "").split(".")[0])
        assert "istota" not in names
        assert names <= set(sys.stdlib_module_names), names - set(sys.stdlib_module_names)

    def test_it_runs_as_a_script(self, tmp_path):
        """The installed copy is exec'd by its shebang, with no config beside it."""
        assert HELPER.read_text().startswith("#!/usr/bin/env python3\n")
        copy = tmp_path / "istota-dev"
        copy.write_text(HELPER.read_text())
        proc = subprocess.run([sys.executable, "-I", "-S", str(copy), "clone", "acme/widget"],
                              capture_output=True, text=True)
        assert proc.returncode == istota_dev.EXIT_USAGE
        assert "no config file" in json.loads(proc.stdout)["error"]
