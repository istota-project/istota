"""The scope daemon-side git runs in (#906), on any host.

`tests/linux/test_git_dir_routes.py` runs what the namespace does to a planted
repository, with a control per route. This file holds the argv and the scope
each caller hands it, which is what a host with no bwrap can check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from istota import executor
from istota.maintenance import repos_relocate, worktree_reaper
from istota.maintenance.repos_relocate import apply, plan, repair_after_copy
from istota.maintenance.worktree_reaper import reap_worktrees
from istota.sandbox.git_hardening import run_git
from istota.sandbox.git_namespace import namespaced
from tests.test_repos_relocate import _clone, tree  # noqa: F401 - a fixture
from tests.test_repos_relocate import _upstream as _named_upstream
from tests.test_repos_relocate import _worktree as _named_worktree
from tests.test_worktree_reaper import _age, _git, _upstream, _worktree


@pytest.fixture
def bwrap_on(monkeypatch):
    monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
    monkeypatch.setattr(executor, "_bwrap_requires_unshare_user", lambda: False)
    monkeypatch.setattr(executor, "_bwrap_supports_disable_userns", lambda: False)
    monkeypatch.setattr("istota.sandbox.shell_exec.on_daemon_path", lambda name: f"/usr/bin/{name}")


class TestTheArgv:
    def test_writable_binds_the_root_read_write(self, tmp_path, bwrap_on):
        argv = namespaced(["git", "status"], tmp_path, tmp_path, writable=True)

        at = argv.index("--bind")
        assert argv[at + 1] == str(tmp_path)
        assert str(tmp_path) not in argv[:at]

    def test_network_leaves_the_network_namespace_alone(self, tmp_path, bwrap_on):
        argv = namespaced(["git", "fetch"], tmp_path, tmp_path, network=True)

        assert "--unshare-net" not in argv
        assert "--unshare-pid" in argv

    def test_nothing_above_the_root_is_bound(self, tmp_path, bwrap_on):
        root = tmp_path / "repos" / "alice"
        root.mkdir(parents=True)

        argv = namespaced(["git", "status"], root, root, writable=True)

        assert str(root.parent) not in argv
        assert str(tmp_path) not in argv

    def test_none_where_bwrap_cannot_make_one(self, tmp_path, monkeypatch):
        monkeypatch.setattr(executor, "_bwrap_available", lambda: False)

        assert namespaced(["git", "status"], tmp_path, tmp_path) is None


class TestRunGit:
    def test_a_scope_wraps_the_call_read_write(self, tmp_path, monkeypatch, bwrap_on):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            raise OSError("not run")

        monkeypatch.setattr("istota.sandbox.git_hardening.subprocess.run", fake_run)

        assert run_git(tmp_path, "status", scope=tmp_path) == (1, "")
        assert seen["argv"][0] == "/usr/bin/bwrap"
        assert "--bind" in seen["argv"]
        assert "--unshare-net" in seen["argv"]

    def test_no_scope_stays_plain_git(self, tmp_path, monkeypatch, bwrap_on):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            raise OSError("not run")

        monkeypatch.setattr("istota.sandbox.git_hardening.subprocess.run", fake_run)

        run_git(tmp_path, "status")

        assert seen["argv"][0] == "git"


@pytest.fixture
def two_users(tmp_path):
    upstream = _upstream(tmp_path)
    root = (tmp_path / "repos").resolve()
    clones = {}
    for user in ("alice", "bob"):
        bare = root / user / "ns" / "project.git"
        bare.parent.mkdir(parents=True)
        _git(tmp_path, "clone", "-q", "--bare", str(upstream), str(bare))
        _git(bare, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        _git(bare, "fetch", "-q", "origin")
        _git(bare, "remote", "set-head", "origin", "-a")
        clones[user] = bare
    return root, clones


def _record_calls(module, monkeypatch) -> list:
    real = module.run_git
    calls = []

    def recording(cwd, *args, **kwargs):
        calls.append((Path(cwd), args, kwargs))
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(module, "run_git", recording)
    return calls


class TestTheReapersScope:
    def test_every_git_call_runs_in_its_users_subtree(self, two_users, monkeypatch):
        root, clones = two_users
        for bare in clones.values():
            _age(_worktree(bare, "wt", "origin/main"), 48)
        calls = _record_calls(worktree_reaper, monkeypatch)

        outcomes = reap_worktrees(root, retention_hours=24)

        assert sum(o.removed for o in outcomes) == 2
        assert {"worktree", "fetch", "status", "merge-base", "rev-parse"} <= {
            args[0] for _, args, _ in calls
        }
        for cwd, args, kwargs in calls:
            assert kwargs.get("scope") == root / cwd.relative_to(root).parts[0], args

    def test_only_the_fetch_has_a_network(self, two_users, monkeypatch):
        root, clones = two_users
        calls = _record_calls(worktree_reaper, monkeypatch)

        reap_worktrees(root, retention_hours=24)

        networked = [args[0] for _, args, kwargs in calls if kwargs.get("network")]
        assert networked == ["fetch"] * len(clones)

    def test_a_record_naming_another_users_worktree_is_out_of_scope(self, two_users):
        root, clones = two_users
        theirs = _worktree(clones["bob"], "wt", "origin/main")
        _age(theirs, 48)
        admin = clones["alice"] / "worktrees" / "planted"
        admin.mkdir(parents=True)
        (admin / "gitdir").write_text(f"{theirs / '.git'}\n")
        (admin / "HEAD").write_text(_git(theirs, "rev-parse", "HEAD"))
        (admin / "commondir").write_text("../..\n")

        outcomes = reap_worktrees(root, retention_hours=24)

        reasons = {o.reason for o in outcomes if o.path == theirs}
        assert worktree_reaper.REASON_OUTSIDE in reasons

    def test_a_repository_linked_in_from_outside_the_root_is_not_swept(
        self, two_users, tmp_path, caplog, monkeypatch
    ):
        root, clones = two_users
        outside = (tmp_path / "outside.git").resolve()
        _git(tmp_path, "clone", "-q", "--bare", str(tmp_path / "upstream"), str(outside))
        (clones["alice"].parent / "link").symlink_to(outside, target_is_directory=True)
        calls = _record_calls(worktree_reaper, monkeypatch)

        with caplog.at_level("WARNING", logger="istota.maintenance.worktree_reaper"):
            reap_worktrees(root, retention_hours=24)

        assert outside not in {cwd for cwd, _, _ in calls}
        assert "linked in from outside" in caplog.text

    def test_a_repository_that_will_not_list_says_so(self, two_users, caplog):
        root, clones = two_users
        (clones["alice"] / "commondir").write_text("/nonexistent\n")

        with caplog.at_level("WARNING", logger="istota.maintenance.worktree_reaper"):
            reap_worktrees(root, retention_hours=24)

        assert "could not list" in caplog.text


class TestTheRepairsScope:
    def test_git_gets_the_moved_namespace(self, tree, monkeypatch):  # noqa: F811
        repos_dir = tree["repos_dir"]
        calls = _record_calls(repos_relocate, monkeypatch)

        report = apply(plan(repos_dir, {"alice"}))

        assert report.repaired
        assert {kwargs.get("scope") for _, _, kwargs in calls} == {
            (repos_dir / "alice" / name).resolve() for name in ("acme", "other-org")
        }

    def test_a_copied_tree_is_repaired_one_top_level_directory_at_a_time(
        self, tmp_path, monkeypatch
    ):
        new = tmp_path / "new"
        new.mkdir()
        bare = _clone(new, "alice/ns", "widget", _named_upstream(tmp_path, "widget"))
        _named_worktree(bare, "widget--wt")
        calls = _record_calls(repos_relocate, monkeypatch)

        repair_after_copy(new, new)

        assert [kwargs.get("scope") for _, _, kwargs in calls] == [(new / "alice").resolve()]
