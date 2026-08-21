"""ISSUE-270 — a credential must never sit in a git config under `repos_dir`.

The developer skill hands the model read-write access to `DEVELOPER_REPOS_DIR`
(`executor.py`, the "Developer repos (RW)" bind). Every worktree cut from a bare
clone inherits that clone's remotes, and `git remote -v`, `git config --list`,
`git remote show origin` and a fair number of failure messages print configured
URLs in full — into the sandbox, into the model's context, and from there into a
task result and a transcript. A token in one therefore routes around the whole
credential architecture: proxy injection, `_split_credential_env`, the per-host
credential helper the skill already registers.

Nothing in this repository *writes* such a config. What was missing is any
guard that notices one made by hand, which is what these cover.

Most of this file is the *survival paths* — the ways a credential stays live
while looking absent. Each was demonstrated against real git before the code
handled it: a value pulled in by `include.path`, a secret riding in the key of
`url.<base>.insteadOf`, a per-worktree override in `config.worktree`, a token
used as the username with no password, a value containing a literal newline, a
bare clone whose directory is not named `*.git`. A suite that only proved the
ordinary `remote.origin.url` case passes against an implementation that leaks
through all six, which is what the first one did.

Detection matches a userinfo *password* or a known token prefix, deliberately,
not a bare `@` — the same rule the deploy side settled on in ISSUE-258
(`deploy/ansible/tasks/main.yml`, `urlsplit('password')`). `git@github.com:x/y`
and `https://oauth2@host/x` both contain an `@` and neither carries a secret.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from istota.git_remote_scrub import (
    config_files_for,
    find_git_dirs,
    scrub_remotes,
    strip_url_credential,
    url_credential,
)

GIT_ISOLATION = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
}

# A shape, not a value. `xxxx` is what `scripts/check-private-data.sh` treats as
# a documentation placeholder, so the repo's own scanners read this as an
# example rather than as a leak.
FAKE_TOKEN = "glpat-xxxxxxxxxxxxxxxxxxxx"
FAKE_URL = f"https://oauth2:{FAKE_TOKEN}@gitlab.com/cynium/istota.git"
CLEAN_URL = "https://gitlab.com/cynium/istota.git"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd), capture_output=True, text=True,
        env={**os.environ, **GIT_ISOLATION},
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{proc.stderr}")
    return proc.stdout


def _bare(tmp_path: Path, name: str = "project.git") -> Path:
    """A bare repository at `<namespace>/<name>`, the documented layout."""
    bare = tmp_path / "namespace" / name
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    return bare


def _with_upstream(tmp_path: Path, name: str = "project.git") -> tuple[Path, Path]:
    """A bare clone of a real upstream, so fetch/worktree operations work."""
    upstream = tmp_path / f"upstream-{name}"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main", ".")
    _git(upstream, "commit", "-q", "--allow-empty", "-m", "init")
    bare = tmp_path / "namespace" / name
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "-q", "--bare", str(upstream), str(bare))
    return bare, upstream


def _text(path: Path) -> str:
    return path.read_text()


class TestUrlCredential:
    """Detection."""

    @pytest.mark.parametrize("url", [
        FAKE_URL,
        f"https://x-access-token:{FAKE_TOKEN}@github.com/owner/repo.git",
        f"http://user:{FAKE_TOKEN}@internal.example/repo.git",
        f"ssh://user:{FAKE_TOKEN}@host:2222/repo.git",
    ])
    def test_detects_a_userinfo_password(self, url):
        assert url_credential(url) == FAKE_TOKEN

    @pytest.mark.parametrize("url,token", [
        ("https://ghp_xxxxxxxxxxxxxxxxxxxx@github.com/o/r.git", "ghp_xxxxxxxxxxxxxxxxxxxx"),
        ("https://github_pat_xxxxxxxx@github.com/o/r.git", "github_pat_xxxxxxxx"),
        (f"https://{FAKE_TOKEN}@gitlab.com/ns/p.git", FAKE_TOKEN),
    ])
    def test_detects_a_token_used_as_the_username(self, url, token):
        """`https://<token>@github.com/o/r` is a documented, working auth form
        with no password at all. A password-only rule reports it clean while
        `git remote -v` prints the token."""
        assert url_credential(url) == token

    @pytest.mark.parametrize("url", [
        "https://gitlab.com/cynium/nebula.git",          # no userinfo at all
        "git@github.com:cynium/istota.git",              # scp-like, no scheme
        "ssh://git@host/path.git",                       # username, no password
        "https://oauth2@gitlab.com/x.git",               # boilerplate username
        "https://x-access-token@github.com/o/r.git",     # ditto
        "https://gitlab-ci-token@gitlab.com/ns/p.git",   # ditto
        "https://user:@host/x.git",                      # empty password
        "https://myname@github.com/o/r.git",             # a person, not a token
        "/srv/app/repos/x.git",                          # a plain path
        "",
    ])
    def test_leaves_everything_else_alone(self, url):
        assert url_credential(url) is None

    def test_a_bare_at_sign_is_not_a_credential(self):
        """The reason this matches a password rather than `@`: an ssh remote
        and a bare-username https remote both contain one and neither carries a
        secret. Matching `@` would rewrite them on every run, forever."""
        for url in ("git@github.com:o/r.git", "https://oauth2@gitlab.com/x.git"):
            assert "@" in url
            assert url_credential(url) is None

    @pytest.mark.parametrize("url", ["://", "https://", "h ttp://a:b@c", "%%%", "https://[::1"])
    def test_never_raises_on_junk(self, url):
        """This runs on the task setup path. Any string at all has to come back
        as an answer, not a traceback."""
        result = url_credential(url)
        assert result is None or isinstance(result, str)


class TestStripUrlCredential:
    def test_removes_the_whole_userinfo(self):
        """Not just the password. Leaving `oauth2@` behind would pin git to a
        username the credential helper does not necessarily answer with, which
        turns a fixed leak into an auth failure."""
        stripped = strip_url_credential(FAKE_URL)
        assert stripped == CLEAN_URL
        assert FAKE_TOKEN not in stripped

    def test_keeps_a_non_default_port(self):
        assert strip_url_credential(f"ssh://user:{FAKE_TOKEN}@host:2222/repo.git") == (
            "ssh://host:2222/repo.git"
        )

    @pytest.mark.parametrize("url,expected", [
        (f"https://u:{FAKE_TOKEN}@[::1]:8443/x.git", "https://[::1]:8443/x.git"),
        (f"https://u:{FAKE_TOKEN}@[2001:db8::1]/x.git", "https://[2001:db8::1]/x.git"),
    ])
    def test_keeps_the_brackets_on_an_ipv6_literal(self, url, expected):
        """Reassembling from `urlsplit().hostname` drops them, producing
        `https://::1:8443/x.git` — a URL git cannot parse. The sweep would then
        report success having broken the remote."""
        assert strip_url_credential(url) == expected

    def test_preserves_host_case(self):
        """`urlsplit().hostname` lowercases, and the helper is registered as
        `credential.<url>.helper` — a case change there stops it matching."""
        assert strip_url_credential(f"https://u:{FAKE_TOKEN}@GitLab.Example.COM/x.git") == (
            "https://GitLab.Example.COM/x.git"
        )

    @pytest.mark.parametrize("url", [
        "https://gitlab.com/cynium/nebula.git",
        "git@github.com:cynium/istota.git",
        "ssh://git@host/path.git",
        "https://oauth2@gitlab.com/x.git",
    ])
    def test_credential_free_urls_are_returned_unchanged(self, url):
        """Byte-identical, so a caller can use inequality as "something changed"
        and never rewrites a config it had no reason to touch."""
        assert strip_url_credential(url) == url


class TestFindGitDirs:
    def test_finds_a_bare_clone_in_the_documented_layout(self, tmp_path):
        bare = _bare(tmp_path)
        assert find_git_dirs(tmp_path) == [bare]

    def test_finds_a_bare_clone_whose_directory_is_not_named_dot_git(self, tmp_path):
        """`git clone --bare <url> myrepo` produces exactly this, and the
        threat model is a repository made by hand. A name test misses it and
        then walks its object store instead of pruning."""
        plain = tmp_path / "namespace" / "plainname"
        plain.parent.mkdir(parents=True)
        _git(tmp_path, "init", "-q", "--bare", str(plain))
        assert find_git_dirs(tmp_path) == [plain]

    def test_finds_an_ordinary_clone(self, tmp_path):
        work = tmp_path / "ns" / "project"
        work.parent.mkdir(parents=True)
        _git(tmp_path, "init", "-q", "-b", "main", str(work))
        assert find_git_dirs(tmp_path) == [work / ".git"]

    def test_finds_a_repository_at_the_root_itself(self, tmp_path):
        """`repos_dir` pointed straight at a repository. The first
        implementation guarded this branch with `here != root` and skipped it,
        while the ordinary-clone branch had no such guard — the asymmetry was
        the tell that the guard was accidental."""
        root = tmp_path / "rootbare"
        _git(tmp_path, "init", "-q", "--bare", str(root))
        assert find_git_dirs(root) == [root.resolve()]

    def test_finds_a_repository_reached_through_a_symlink(self, tmp_path):
        """`os.walk` never yields a symlinked directory as a dirpath while
        followlinks is off, so a repo symlinked into repos_dir was invisible.
        Symlinking one in is exactly the "arrived some other way" shape."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        real = elsewhere / "proj.git"
        _git(tmp_path, "init", "-q", "--bare", str(real))
        repos = tmp_path / "repos"
        repos.mkdir()
        (repos / "proj.git").symlink_to(real)

        assert find_git_dirs(repos) == [real.resolve()]

    def test_does_not_descend_into_a_repository(self, tmp_path):
        """A bare clone holds further `config` files below it
        (`modules/*/config` for submodules). Only the top one is its own."""
        bare = _bare(tmp_path)
        nested = bare / "modules" / "sub"
        nested.mkdir(parents=True)
        (nested / "config").write_text('[remote "origin"]\n\turl = https://x/y.git\n')
        assert find_git_dirs(tmp_path) == [bare]

    def test_missing_root_is_not_an_error(self, tmp_path):
        assert find_git_dirs(tmp_path / "does-not-exist") == []

    def test_logs_rather_than_silently_stopping_at_max_depth(self, tmp_path, caplog):
        """A sweep that declines to look must not be indistinguishable from one
        that looked and found nothing."""
        deep = tmp_path / "a" / "b" / "c" / "d" / "e"
        deep.mkdir(parents=True)
        with caplog.at_level("INFO"):
            find_git_dirs(tmp_path, max_depth=2)
        assert "not descending past depth" in caplog.text


class TestSweepDoesNotWalkCheckouts:
    """`setup_env` runs on every task where developer config is enabled, not
    only tasks that selected the skill (`_env.dispatch_setup_env_hooks`
    iterates the whole index). `repos_dir` holds a full source checkout per
    active worktree, so a sweep that descends into them pays thousands of stat
    calls per task for directories that hold no config of their own."""

    def _spy_walk(self, monkeypatch):
        walked: list[str] = []
        real_walk = os.walk

        def _spy(top, **kwargs):
            for entry in real_walk(top, **kwargs):
                walked.append(entry[0])
                yield entry

        monkeypatch.setattr(os, "walk", _spy)
        return walked

    def test_a_worktree_checkout_is_not_descended_into(self, tmp_path, monkeypatch):
        bare, _ = _with_upstream(tmp_path)
        _git(bare, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        _git(bare, "fetch", "-q", "origin")
        work = tmp_path / "namespace" / "project--task-1"
        _git(bare, "worktree", "add", "-q", "-b", "task-1", str(work), "origin/main")
        deep = work / "src" / "pkg"
        deep.mkdir(parents=True)

        walked = self._spy_walk(monkeypatch)
        find_git_dirs(tmp_path)

        assert str(deep) not in walked, f"descended into a worktree checkout: {walked}"

    def test_an_ordinary_clone_is_not_descended_into(self, tmp_path, monkeypatch):
        work = tmp_path / "ns" / "project"
        work.parent.mkdir(parents=True)
        _git(tmp_path, "init", "-q", "-b", "main", str(work))
        deep = work / "src" / "pkg"
        deep.mkdir(parents=True)

        walked = self._spy_walk(monkeypatch)
        found = find_git_dirs(tmp_path)

        assert work / ".git" in found
        assert str(deep) not in walked, f"descended into a checkout: {walked}"


class TestScrubRemoteUrls:
    """The ordinary case: a credential in `remote.<name>.url`."""

    def test_strips_the_credential(self, tmp_path):
        bare = _bare(tmp_path)
        _git(bare, "config", "remote.origin.url", FAKE_URL)

        findings = scrub_remotes(tmp_path)

        assert [(f.repo, f.key, f.removed) for f in findings] == [
            (bare, "remote.origin.url", True)
        ]
        assert _git(bare, "config", "--get", "remote.origin.url").strip() == CLEAN_URL
        assert FAKE_TOKEN not in _text(bare / "config")

    def test_the_report_never_carries_the_secret(self, tmp_path):
        """What comes back is what gets logged. The host tells the operator
        which token to rotate; the value is not theirs to print."""
        bare = _bare(tmp_path)
        _git(bare, "config", "remote.origin.url", FAKE_URL)

        findings = scrub_remotes(tmp_path)

        assert findings[0].host == "gitlab.com"
        assert FAKE_TOKEN not in repr(findings)

    def test_leaves_a_clean_config_untouched(self, tmp_path):
        """No rewrite means no mtime churn and no chance of corrupting a config
        that was fine — this runs on every task."""
        bare = _bare(tmp_path)
        _git(bare, "config", "remote.origin.url", CLEAN_URL)
        before = (bare / "config").read_bytes()

        assert scrub_remotes(tmp_path) == []
        assert (bare / "config").read_bytes() == before

    def test_scrubs_a_pushurl_and_a_second_remote(self, tmp_path):
        """`origin` is the one the reporter noticed. A pushurl or a second
        remote leaks through exactly the same `git remote -v`."""
        bare = _bare(tmp_path)
        _git(bare, "config", "remote.origin.url", CLEAN_URL)
        _git(bare, "config", "remote.upstream.url", FAKE_URL)
        _git(bare, "config", "remote.origin.pushurl", FAKE_URL)

        findings = scrub_remotes(tmp_path)

        assert {f.key for f in findings} == {"remote.upstream.url", "remote.origin.pushurl"}
        assert all(f.removed for f in findings)
        assert FAKE_TOKEN not in _text(bare / "config")

    def test_a_multivalued_url_keeps_its_clean_siblings(self, tmp_path):
        """`remote.<name>.url` is multi-valued — the first is the fetch URL and
        all of them are push targets. A bare `--replace-all` collapses them into
        the rewritten one, destroying a clean URL to remove a dirty one."""
        bare = _bare(tmp_path)
        other = "https://example.com/a.git"
        _git(bare, "config", "--add", "remote.origin.url", other)
        _git(bare, "config", "--add", "remote.origin.url", FAKE_URL)

        findings = scrub_remotes(tmp_path)

        assert [f.removed for f in findings] == [True]
        assert _git(bare, "config", "--get-all", "remote.origin.url").split() == [
            other, CLEAN_URL
        ]

    def test_git_still_operates_on_the_repo_afterwards(self, tmp_path):
        """A rewritten config is a config that can be broken. Prove git still
        parses it and the remaining settings survived."""
        bare, upstream = _with_upstream(tmp_path)
        _git(bare, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        _git(bare, "config", "remote.origin.url", FAKE_URL)

        scrub_remotes(tmp_path)

        assert _git(bare, "config", "--get", "remote.origin.fetch").strip() == (
            "+refs/heads/*:refs/remotes/origin/*"
        )
        _git(bare, "config", "remote.origin.url", str(upstream))
        _git(bare, "fetch", "-q", "origin")

    def test_sweeps_several_repos_in_one_pass(self, tmp_path):
        clean = _bare(tmp_path, "nebula.git")
        dirty = _bare(tmp_path, "istota.git")
        _git(clean, "config", "remote.origin.url", CLEAN_URL)
        _git(dirty, "config", "remote.origin.url", FAKE_URL)

        findings = scrub_remotes(tmp_path)

        assert [f.repo for f in findings] == [dirty]
        assert FAKE_TOKEN not in _text(dirty / "config")

    def test_missing_root_is_a_no_op(self, tmp_path):
        assert scrub_remotes(tmp_path / "nope") == []


class TestSurvivalPaths:
    """Six ways a credential stayed live while looking absent. Each was
    reproduced against real git before the implementation handled it."""

    def test_a_credential_in_an_included_file(self, tmp_path):
        """`git config --file` does not follow `include.path` unless asked, so
        the sweep reported the repo clean while `git remote -v` printed the
        token. The correction must also land in the *included* file — writing
        it to the top-level config leaves the secret where it was."""
        bare = _bare(tmp_path)
        included = bare / "secret.inc"
        included.write_text(f'[remote "up"]\n\turl = {FAKE_URL}\n')
        with open(bare / "config", "a") as handle:
            handle.write("[include]\n\tpath = secret.inc\n")
        assert FAKE_TOKEN in _git(bare, "remote", "-v")

        findings = scrub_remotes(tmp_path)

        assert [(f.key, f.removed) for f in findings] == [("remote.up.url", True)]
        assert findings[0].config == included, "the fix must land where the value lives"
        assert FAKE_TOKEN not in _text(included)
        assert FAKE_TOKEN not in _git(bare, "remote", "-v")

    def test_a_credential_in_an_insteadof_key(self, tmp_path):
        """The secret rides in the *key*, so a value scan can never see it, and
        `remote -v` shows a clean URL while every fetch is rewritten through
        the credentialed base."""
        bare = _bare(tmp_path)
        _git(bare, "config", "remote.origin.url", "https://example.com/ns/p.git")
        _git(bare, "config", f"url.https://oauth2:{FAKE_TOKEN}@example.com/.insteadOf",
             "https://example.com/")
        assert FAKE_TOKEN in _git(bare, "config", "--list")

        findings = scrub_remotes(tmp_path)

        assert [f.removed for f in findings] == [True]
        assert FAKE_TOKEN not in _text(bare / "config")
        assert FAKE_TOKEN not in _git(bare, "config", "--list")

    def test_a_credential_in_a_worktree_config(self, tmp_path):
        """`extensions.worktreeConfig` puts overrides in
        `<gitdir>/worktrees/<id>/config.worktree`, which the repository config
        does not include — so covering bare clones does *not* cover it, and
        `git remote -v` inside that worktree prints the token."""
        upstream = tmp_path / "up"
        upstream.mkdir()
        _git(upstream, "init", "-q", "-b", "main", ".")
        _git(upstream, "commit", "-q", "--allow-empty", "-m", "init")
        main = tmp_path / "repos" / "main"
        main.parent.mkdir(parents=True)
        _git(tmp_path, "clone", "-q", str(upstream), str(main))
        _git(main, "config", "extensions.worktreeConfig", "true")
        work = tmp_path / "repos" / "wt1"
        _git(main, "worktree", "add", "-q", str(work), "-b", "t1")
        _git(work, "config", "--worktree", "remote.origin.url", FAKE_URL)
        assert FAKE_TOKEN in _git(work, "remote", "-v")

        findings = scrub_remotes(tmp_path / "repos")

        assert [f.removed for f in findings] == [True]
        assert FAKE_TOKEN not in _git(work, "remote", "-v")

    def test_a_value_containing_a_newline_forges_nothing(self, tmp_path):
        """Line-based parsing let a crafted value fabricate a second config
        entry: the sweep created a `remote.evil` section that was never there,
        raised a false rotate-this alert, and left the real secret in place."""
        bare = _bare(tmp_path)
        (bare / "config").write_text(
            '[core]\n\tbare = true\n'
            f'[remote "origin"]\n\turl = "https://h/a\\nremote.evil.url {FAKE_URL}"\n'
        )

        findings = scrub_remotes(tmp_path)

        assert not any(f.key == "remote.evil.url" for f in findings), (
            "a value forged a config entry that does not exist"
        )
        text = _text(bare / "config")
        assert '[remote "evil"]' not in text, "the sweep wrote a section that was never there"
        # The value is one malformed string rather than a URL, so it is not a
        # working remote and there is nothing to preserve by rewriting it. It
        # gets dropped — reporting it and leaving the token live would be the
        # same silent leak in a politer form.
        assert [(f.key, f.removed) for f in findings] == [("remote.origin.url", True)]
        assert FAKE_TOKEN not in text
        assert FAKE_TOKEN not in _git(bare, "remote", "-v")

    def test_a_token_used_as_the_username(self, tmp_path):
        bare = _bare(tmp_path)
        _git(bare, "config", "remote.origin.url",
             f"https://{FAKE_TOKEN}@gitlab.com/ns/p.git")

        findings = scrub_remotes(tmp_path)

        assert [f.removed for f in findings] == [True]
        assert _git(bare, "config", "--get", "remote.origin.url").strip() == (
            "https://gitlab.com/ns/p.git"
        )

    def test_a_bare_clone_without_a_dot_git_suffix(self, tmp_path):
        plain = tmp_path / "namespace" / "plainname"
        plain.parent.mkdir(parents=True)
        _git(tmp_path, "init", "-q", "--bare", str(plain))
        _git(plain, "config", "remote.origin.url", FAKE_URL)

        findings = scrub_remotes(tmp_path)

        assert [f.removed for f in findings] == [True]
        assert FAKE_TOKEN not in _text(plain / "config")


class TestExtraHeader:
    """`http.<url>.extraheader` carries an Authorization header directly. It is
    how Azure DevOps and GitLab CI inject a job token, and it never appears in
    a remote URL — so a URL-only sweep reports the repository clean."""

    def test_an_extraheader_is_removed(self, tmp_path):
        bare = _bare(tmp_path)
        _git(bare, "config", "remote.origin.url", CLEAN_URL)
        _git(bare, "config", "http.https://gitlab.com/.extraheader",
             "AUTHORIZATION: basic eHh4eHh4eHh4eHh4")

        findings = scrub_remotes(tmp_path)

        assert [f.removed for f in findings] == [True]
        assert "extraheader" in findings[0].key
        assert "eHh4eHh4eHh4eHh4" not in _text(bare / "config")

    def test_a_bare_extraheader_is_removed(self, tmp_path):
        bare = _bare(tmp_path)
        _git(bare, "config", "http.extraheader", "AUTHORIZATION: bearer xxxxxxxx")

        findings = scrub_remotes(tmp_path)

        assert [(f.key, f.removed) for f in findings] == [("http.extraheader", True)]
        assert "bearer" not in _text(bare / "config")


class TestFailureIsNotSilence:
    """A caller must never read "could not look" as "nothing there"."""

    def test_an_unreadable_config_is_reported_not_skipped(self, tmp_path):
        bare = _bare(tmp_path)
        (bare / "config").write_text('[remote "origin"\n  url = oops\n')

        findings = scrub_remotes(tmp_path)

        assert [(f.key, f.removed) for f in findings] == [("<unreadable>", False)]

    def test_a_broken_config_does_not_stop_the_sweep(self, tmp_path):
        """One unparseable repo must not shield the rest."""
        good = _bare(tmp_path, "good.git")
        _git(good, "config", "remote.origin.url", FAKE_URL)
        broken = tmp_path / "namespace" / "broken.git"
        broken.mkdir(parents=True)
        for marker in ("HEAD", "config"):
            (broken / marker).write_text('[remote "origin"\n  url = oops\n')
        (broken / "objects").mkdir()

        findings = scrub_remotes(tmp_path)

        assert any(f.repo == good and f.removed for f in findings)
        assert FAKE_TOKEN not in _text(good / "config")

    def test_a_failed_rewrite_still_reports_the_credential(self, tmp_path, monkeypatch):
        """A locked config is what two overlapping sweeps produce — the hook
        runs per task and tasks run in a worker pool. Dropping the finding on a
        failed write reports a repository as clean while the token sits there."""
        bare = _bare(tmp_path)
        _git(bare, "config", "remote.origin.url", FAKE_URL)

        from istota import git_remote_scrub

        monkeypatch.setattr(git_remote_scrub, "_run_write", lambda *a: False)
        findings = git_remote_scrub.scrub_remotes(tmp_path)

        assert [(f.key, f.removed) for f in findings] == [("remote.origin.url", False)]
        assert FAKE_TOKEN in _text(bare / "config"), "precondition: the write really failed"

    def test_scrub_and_report_warns_it_could_not_remove_one(self, tmp_path, monkeypatch, caplog):
        bare = _bare(tmp_path)
        _git(bare, "config", "remote.origin.url", FAKE_URL)

        from istota import git_remote_scrub

        monkeypatch.setattr(git_remote_scrub, "_run_write", lambda *a: False)
        with caplog.at_level("WARNING"):
            git_remote_scrub.scrub_and_report(tmp_path)

        assert "could NOT be removed" in caplog.text
        assert FAKE_TOKEN not in caplog.text

    def test_scrub_and_report_never_raises(self, tmp_path, monkeypatch):
        """The never-raises contract, exercised rather than asserted. A hook
        that raises has its whole returned env discarded by the dispatcher."""
        from istota import git_remote_scrub

        def _boom(_root):
            raise RuntimeError("walk exploded")

        monkeypatch.setattr(git_remote_scrub, "scrub_remotes", _boom)
        assert git_remote_scrub.scrub_and_report(tmp_path) == []


class TestConfigFilesFor:
    def test_a_repo_with_no_worktrees_has_one_config(self, tmp_path):
        bare = _bare(tmp_path)
        assert config_files_for(bare) == [bare / "config"]

    def test_a_missing_worktrees_dir_is_not_an_error(self, tmp_path):
        bare = _bare(tmp_path)
        assert not (bare / "worktrees").exists()
        assert config_files_for(bare) == [bare / "config"]


class TestDeveloperSetupEnvSweep:
    """The integration seam: the sweep runs on the real setup path.

    `setup_env` is the moment the daemon prepares git access to `repos_dir`,
    and it runs before `build_bwrap_cmd` binds that directory into the sandbox.
    Testing through the hook rather than calling `scrub_remotes` directly is
    what pins the ordering — a unit test of the sweep passes just as well if
    nobody ever calls it.
    """

    def _run_hook(self, tmp_path, repos_dir):
        from istota import db
        from istota.config import Config, DeveloperConfig, SecurityConfig
        from istota.skills.developer import setup_env

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / "_index.toml").write_text(
            '[files]\ndescription = "File ops"\nalways_include = true\n'
        )
        (skills_dir / "files.md").write_text("File operations guide.")

        config = Config(
            db_path=db_path,
            skills_dir=skills_dir,
            bundled_skills_dir=None,
            temp_dir=tmp_path / "temp",
            developer=DeveloperConfig(
                enabled=True,
                repos_dir=str(repos_dir),
                gitlab_url="https://gitlab.example.com",
                gitlab_token="glpat-test",
                gitlab_username="istotabot",
            ),
            security=SecurityConfig(skill_proxy_enabled=False),
        )
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True, exist_ok=True)

        class _Ctx:
            pass

        ctx = _Ctx()
        ctx.config = config
        ctx.user_temp_dir = str(user_temp)
        return setup_env(ctx)

    def test_the_hook_strips_a_credentialed_remote(self, tmp_path):
        repos = tmp_path / "repos"
        repos.mkdir()
        bare = _bare(repos)
        _git(bare, "config", "remote.origin.url", FAKE_URL)

        env = self._run_hook(tmp_path, repos)

        assert FAKE_TOKEN not in _text(bare / "config")
        assert _git(bare, "config", "--get", "remote.origin.url").strip() == CLEAN_URL
        # The helper is still what authenticates, so stripping the URL costs
        # the repository nothing on the configured forge.
        assert env["GIT_CONFIG_KEY_0"] == "credential.https://gitlab.example.com.helper"

    def test_the_hook_warns_without_printing_the_value(self, tmp_path, caplog):
        repos = tmp_path / "repos"
        repos.mkdir()
        bare = _bare(repos)
        _git(bare, "config", "remote.origin.url", FAKE_URL)

        with caplog.at_level("WARNING"):
            self._run_hook(tmp_path, repos)

        assert "rotate" in caplog.text.lower()
        assert FAKE_TOKEN not in caplog.text

    def test_a_missing_repos_dir_does_not_break_setup(self, tmp_path):
        """`repos_dir` is configured but not yet created on a fresh install.
        The sweep must not turn that into a task with no credential helper."""
        env = self._run_hook(tmp_path, tmp_path / "repos-not-created")
        assert env["GIT_CONFIG_COUNT"] == "1"

    def test_a_failing_sweep_does_not_cost_the_credential_helper(self, tmp_path, monkeypatch):
        """`dispatch_setup_env_hooks` keeps only what the hook returned, so an
        exception here would silently discard GIT_CONFIG_COUNT and the forge
        wiring — a task that looks fine and cannot authenticate."""
        from istota import git_remote_scrub

        def _boom(_root):
            raise RuntimeError("sweep exploded")

        monkeypatch.setattr(git_remote_scrub, "scrub_remotes", _boom)
        repos = tmp_path / "repos"
        repos.mkdir()

        env = self._run_hook(tmp_path, repos)

        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "credential.https://gitlab.example.com.helper"


class TestWritesStayInsideTheRoot:
    """`repos_dir` is bound read-write into the sandbox, so its contents are
    model-controlled. `git config --includes` will happily report a value whose
    origin is any file on the host that git can parse, and rewriting that would
    hand the model a daemon-privileged write at a path of its choosing —
    a worse hole than the one this module closes. Detection may range that far;
    correction may not."""

    def test_an_include_outside_the_root_is_reported_not_rewritten(self, tmp_path):
        repos = tmp_path / "repos"
        repos.mkdir()
        bare = _bare(repos)
        outside = tmp_path / "outside" / "victim-gitconfig"
        outside.parent.mkdir()
        original = (
            "[user]\n\tname = someone\n"
            f'[url "https://oauth2:{FAKE_TOKEN}@example.com/"]\n'
            "\tinsteadOf = https://example.com/\n"
        )
        outside.write_text(original)
        with open(bare / "config", "a") as handle:
            handle.write(f"[include]\n\tpath = {outside}\n")

        findings = scrub_remotes(repos)

        assert [f.removed for f in findings] == [False], (
            "a file outside repos_dir must never be rewritten by the sweep"
        )
        assert outside.read_text() == original, "the victim file was modified"

    def test_a_symlinked_repo_outside_the_root_is_not_rewritten(self, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        real = elsewhere / "proj.git"
        _git(tmp_path, "init", "-q", "--bare", str(real))
        _git(real, "config", "remote.origin.url", FAKE_URL)
        repos = tmp_path / "repos"
        repos.mkdir()
        (repos / "proj.git").symlink_to(real)

        findings = scrub_remotes(repos)

        assert [f.removed for f in findings] == [False]
        assert FAKE_TOKEN in _text(real / "config"), "wrote outside the sweep root"

    def test_a_repo_inside_the_root_is_still_rewritten(self, tmp_path):
        """The confinement must not stop the ordinary case working."""
        repos = tmp_path / "repos"
        repos.mkdir()
        bare = _bare(repos)
        _git(bare, "config", "remote.origin.url", FAKE_URL)

        assert [f.removed for f in scrub_remotes(repos)] == [True]
        assert FAKE_TOKEN not in _text(bare / "config")


class TestOneBadRepoDoesNotEndTheSweep:
    def test_a_non_utf8_config_does_not_abort_everything(self, tmp_path):
        """`text=True` raises UnicodeDecodeError on one stray byte — a
        ValueError, so neither the OSError nor the SubprocessError guard caught
        it. It propagated to the blanket handler, which returns the empty list
        that means "looked, found nothing", leaving every other repo unswept."""
        good = _bare(tmp_path, "good.git")
        _git(good, "config", "remote.origin.url", FAKE_URL)
        bad = _bare(tmp_path, "bad.git")
        (bad / "config").write_bytes(
            b'[core]\n\tbare = true\n[remote "origin"]\n\turl = https://h/\xff\xfe.git\n'
        )

        findings = scrub_remotes(tmp_path)

        assert any(f.repo == good and f.removed for f in findings), (
            "a bad config elsewhere stopped the sweep"
        )
        assert FAKE_TOKEN not in _text(good / "config")

    def test_scrub_and_report_survives_a_non_utf8_config(self, tmp_path):
        bad = _bare(tmp_path, "bad.git")
        (bad / "config").write_bytes(b'[core]\n\tbare = true\n\turl = https://h/\xff.git\n')
        from istota import git_remote_scrub

        assert isinstance(git_remote_scrub.scrub_and_report(tmp_path), list)


class TestPlantedDirectoriesDoNotHideRepos:
    """Finding a git directory prunes the walk, and `repos_dir` is writable by
    the model — so a loose structural test lets a planted directory conceal
    every repository beneath it."""

    def test_three_empty_markers_do_not_prune(self, tmp_path):
        blind = tmp_path / "blind"
        blind.mkdir()
        (blind / "HEAD").write_text("")
        (blind / "config").write_text("")
        (blind / "objects").mkdir()
        real = blind / "ns" / "real.git"
        real.parent.mkdir(parents=True)
        _git(tmp_path, "init", "-q", "--bare", str(real))
        _git(real, "config", "remote.origin.url", FAKE_URL)

        findings = scrub_remotes(tmp_path)

        assert any(f.repo == real and f.removed for f in findings), (
            "a planted directory hid a real repository"
        )
        assert FAKE_TOKEN not in _text(real / "config")

    def test_a_stray_dot_git_file_does_not_prune(self, tmp_path):
        hide = tmp_path / "hide"
        hide.mkdir()
        (hide / ".git").write_text("not a worktree pointer\n")
        real = hide / "ns" / "real.git"
        real.parent.mkdir(parents=True)
        _git(tmp_path, "init", "-q", "--bare", str(real))
        _git(real, "config", "remote.origin.url", FAKE_URL)

        findings = scrub_remotes(tmp_path)

        assert any(f.repo == real and f.removed for f in findings)
        assert FAKE_TOKEN not in _text(real / "config")

    def test_a_real_worktree_pointer_still_prunes(self, tmp_path, monkeypatch):
        """The stricter test must not cost the pruning that keeps this cheap."""
        bare, _ = _with_upstream(tmp_path)
        _git(bare, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        _git(bare, "fetch", "-q", "origin")
        work = tmp_path / "namespace" / "project--task-1"
        _git(bare, "worktree", "add", "-q", "-b", "task-1", str(work), "origin/main")
        deep = work / "src" / "pkg"
        deep.mkdir(parents=True)

        walked: list[str] = []
        real_walk = os.walk

        def _spy(top, **kwargs):
            for entry in real_walk(top, **kwargs):
                walked.append(entry[0])
                yield entry

        monkeypatch.setattr(os, "walk", _spy)
        find_git_dirs(tmp_path)

        assert str(deep) not in walked


class TestSectionDedupeIsPerFile:
    def test_the_same_base_in_two_files_is_removed_from_both(self, tmp_path):
        """Deduping on the section name alone removed it from the first file
        and dropped the second with no finding at all — so the caller read a
        still-leaking repository as clean."""
        bare = _bare(tmp_path)
        extra = bare / "extra.inc"
        extra.write_text(
            f'[url "https://oauth2:{FAKE_TOKEN}@example.com/"]\n'
            "\tpushInsteadOf = https://example.com/\n"
        )
        with open(bare / "config", "a") as handle:
            handle.write(
                f'[url "https://oauth2:{FAKE_TOKEN}@example.com/"]\n'
                "\tinsteadOf = https://example.com/\n"
                f"[include]\n\tpath = {extra.name}\n"
            )

        findings = scrub_remotes(tmp_path)

        assert len(findings) == 2, f"one file was dropped silently: {findings}"
        assert all(f.removed for f in findings)
        assert FAKE_TOKEN not in _text(bare / "config")
        assert FAKE_TOKEN not in _text(extra)
        assert FAKE_TOKEN not in _git(bare, "config", "--list")


class TestSubmoduleUrls:
    def test_a_credentialed_submodule_url_is_stripped(self, tmp_path):
        """`git config --list` prints it to the model exactly like a remote."""
        bare = _bare(tmp_path)
        _git(bare, "config", "submodule.sub.url", FAKE_URL)

        findings = scrub_remotes(tmp_path)

        assert [(f.key, f.removed) for f in findings] == [("submodule.sub.url", True)]
        assert _git(bare, "config", "--get", "submodule.sub.url").strip() == CLEAN_URL


class TestExtraHeaderIsGatedOnTheHeaderName:
    def test_a_non_auth_header_is_left_alone(self, tmp_path):
        """Removing it would break a repository that needs it, and tell the
        operator to rotate a credential that never existed."""
        bare = _bare(tmp_path)
        _git(bare, "config", "http.https://internal/.extraheader", "X-Trace-Id: build-42")

        assert scrub_remotes(tmp_path) == []
        assert "X-Trace-Id" in _text(bare / "config")

    def test_the_host_is_reported_for_rotation(self, tmp_path):
        """The key is the only record of which host the header authenticated
        to, so "host unknown" is useless in exactly the case it is needed."""
        bare = _bare(tmp_path)
        _git(bare, "config", "http.https://gitlab.com/.extraheader",
             "AUTHORIZATION: basic eHh4eHh4eHh4")

        findings = scrub_remotes(tmp_path)

        assert findings[0].host == "gitlab.com"


class TestTokenPrefixMatching:
    @pytest.mark.parametrize("username", [
        "GHP_deploybot",     # a person's username, not a token
        "atlassian_bot",
        "bbp_someuser",
        "myname",
        "build-runner",
    ])
    def test_does_not_strip_an_ordinary_username(self, username):
        """A false positive here strips a working remote's username and raises
        a rotate-this warning about a credential that does not exist. Prefixes
        are matched case-sensitively because no forge issues mixed case."""
        assert url_credential(f"https://{username}@example.com/x.git") is None

    @pytest.mark.parametrize("username", [
        "ghp_xxxxxxxxxxxxxxxx", "glpat-xxxxxxxxxxxx", "glcbt-xxxxxxxx",
        "ATATT3xFfGF0xxxx", "github_pat_xxxxxxxx",
    ])
    def test_strips_a_real_token_prefix(self, username):
        assert url_credential(f"https://{username}@example.com/x.git") == username
