"""Tests for istota.skill_host_paths — the shared host-path allowlist.

A skill CLI runs host-side (the proxy spawns it outside the sandbox), so any
verb taking a host path is an arbitrary-file read or write unless it is scoped.
The module holds two allowlists. ``resolve_host_path`` scopes a path in the
caller's own workspace against the mount roots (devbox ``cp-in``/``cp-out``, kv
``set --value-file``, email ``--attach``); ``resolve_under_repos`` scopes a
worktree against ``DEVELOPER_REPOS_DIR`` for the ``code_review`` CLI. One module
so the roots and the error convention cannot drift apart.

The mount roots mirror what ``build_bwrap_cmd`` binds *for this user*.
``NEXTCLOUD_MOUNT_PATH`` is the shared mount root for everyone, so taking it
whole would hand one user another's workspace.

Several tests here are written specifically to kill a plausible wrong
implementation rather than to describe the right one — an argument-inspecting
check that never resolves, a lexical ``startswith`` containment, a root echoed
back instead of resolved. Where a test looks redundant, that is usually why.
"""

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from istota.skill_host_paths import (
    allowed_host_roots,
    developer_repos_root,
    resolve_host_path,
    resolve_under_repos,
    user_workspace_root,
    validate_host_path,
    write_resolved,
)
from istota.user_scope import scoped_user_dir
from tests.support.skill_cli import run_skill_main


@pytest.fixture
def mount(tmp_path, monkeypatch):
    """A mount laid out like the real one, with alice as the caller."""
    root = tmp_path / "mount"
    (root / "Users" / "alice").mkdir(parents=True)
    (root / "Users" / "bob").mkdir(parents=True)
    (root / "Channels" / "tok1").mkdir(parents=True)
    (root / "Channels" / "tok2").mkdir(parents=True)
    (root / "Talk").mkdir(parents=True)
    deferred = tmp_path / "deferred"
    deferred.mkdir()
    monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(root))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(deferred))
    monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
    return root


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """The developer repos tree, and the variable as `execute_task` builds it.

    One fixture for both repos classes, because the shape of the environment is
    the thing under test in each of them and two copies would let one drift.
    `DEVELOPER_REPOS_DIR` is the *caller's own subtree*, not the configured
    root: `setup_env` derives it and `build_bwrap_cmd` binds the same path. Bob
    has a tree next door, which is what the containment tests aim at.
    """
    root = tmp_path / "repos" / "alice"
    (root / "ns" / "project--branch").mkdir(parents=True)
    (tmp_path / "repos" / "bob" / "ns" / "victim--branch").mkdir(parents=True)
    monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(root))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    return root


class TestAllowedHostRoots:
    def test_scopes_the_mount_to_the_calling_user(self, mount, monkeypatch):
        roots = allowed_host_roots()
        assert mount / "Users" / "alice" in roots
        assert mount / "Users" / "bob" not in roots
        assert mount not in roots

    def test_includes_the_tasks_own_channel_only(self, mount, monkeypatch):
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "tok1")
        roots = allowed_host_roots()
        assert mount / "Channels" / "tok1" in roots
        assert mount / "Channels" / "tok2" not in roots

    def test_talk_is_readable_but_not_writable(self, mount):
        assert mount / "Talk" in allowed_host_roots(writable=False)
        assert mount / "Talk" not in allowed_host_roots(writable=True)

    def test_traversal_token_is_not_turned_into_a_path(self, mount, monkeypatch):
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "../..")
        assert all("Channels" not in str(r) for r in allowed_host_roots())

    def test_mount_contributes_nothing_without_a_user_id(self, mount, monkeypatch):
        """Fail closed: without an identity there is no per-user subtree to
        scope to, so the mount contributes no root at all."""
        monkeypatch.delenv("ISTOTA_USER_ID")
        roots = allowed_host_roots()
        assert all(not r.is_relative_to(mount) for r in roots)

    def test_blank_and_unset_are_skipped(self, monkeypatch):
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", "   ")
        monkeypatch.delenv("NEXTCLOUD_MOUNT_PATH", raising=False)
        monkeypatch.delenv("ISTOTA_USER_ID", raising=False)
        assert allowed_host_roots() == []


class TestResolveHostPath:
    def test_accepts_the_users_own_workspace(self, mount):
        p = mount / "Users" / "alice" / "ok.json"
        p.write_text("{}")
        resolved, err = resolve_host_path(p, writable=False, operation="op")
        assert err is None
        assert resolved == p.resolve()

    def test_refuses_another_users_workspace(self, mount):
        """The core cross-tenant case: the mount is shared, the bind is not."""
        p = mount / "Users" / "bob" / "private.json"
        p.write_text('{"bobs": "notes"}')
        resolved, err = resolve_host_path(p, writable=False, operation="op")
        assert resolved is None
        assert "outside allowed roots" in err

    def test_refuses_the_mount_root_itself(self, mount):
        p = mount / "loose.json"
        p.write_text("{}")
        _, err = resolve_host_path(p, writable=False, operation="op")
        assert err is not None

    def test_accepts_the_deferred_dir(self, mount, tmp_path):
        p = tmp_path / "deferred" / "v.json"
        p.write_text("{}")
        resolved, err = resolve_host_path(p, writable=False, operation="op")
        assert err is None
        assert resolved == p.resolve()

    def test_returns_the_resolved_path_for_the_caller_to_use(self, mount):
        """Handing back the approved path is what lets a caller avoid
        re-walking symlinks on the original."""
        real = mount / "Users" / "alice" / "real.json"
        real.write_text("{}")
        sub = mount / "Users" / "alice" / "sub"
        sub.mkdir()
        resolved, err = resolve_host_path(
            mount / "Users" / "alice" / "sub" / ".." / "real.json",
            writable=False, operation="op",
        )
        assert err is None
        assert resolved == real.resolve()

    def test_refuses_leaf_symlink(self, mount):
        target = mount / "Users" / "bob" / "secret.json"
        target.write_text("{}")
        link = mount / "Users" / "alice" / "link.json"
        link.symlink_to(target)
        _, err = resolve_host_path(link, writable=False, operation="op")
        assert "symlink" in err

    def test_intermediate_symlink_out_of_bounds_is_caught_by_resolution(self, mount):
        """A symlinked *directory* is not the leaf, so the leaf check misses it;
        comparing the fully resolved path is what refuses it."""
        (mount / "Users" / "bob" / "deep").mkdir()
        secret = mount / "Users" / "bob" / "deep" / "s.json"
        secret.write_text("{}")
        hop = mount / "Users" / "alice" / "hop"
        hop.symlink_to(mount / "Users" / "bob" / "deep")
        _, err = resolve_host_path(hop / "s.json", writable=False, operation="op")
        assert err is not None
        assert "outside allowed roots" in err

    def test_missing_source_reported(self, mount):
        _, err = resolve_host_path(
            mount / "Users" / "alice" / "nope.json", writable=False, operation="op",
        )
        assert "not found" in err

    def test_no_roots_refuses_and_names_the_operation(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
        monkeypatch.delenv("NEXTCLOUD_MOUNT_PATH", raising=False)
        monkeypatch.delenv("ISTOTA_USER_ID", raising=False)
        _, err = resolve_host_path(tmp_path / "x", writable=False, operation="cp-in/cp-out")
        assert "cp-in/cp-out" in err


class TestResolveHostPathWritable:
    def test_accepts_a_new_file_under_a_root(self, mount):
        dest = mount / "Users" / "alice" / "sub" / "new.txt"
        resolved, err = resolve_host_path(dest, writable=True, operation="cp-out")
        assert err is None
        assert resolved == (mount / "Users" / "alice" / "sub").resolve() / "new.txt"
        # And the parent is *not* created — see
        # TestResolutionNeverMutatesTheFilesystem.
        assert not dest.parent.exists()

    def test_does_not_create_directories_outside_the_roots(self, mount, tmp_path):
        """Resolution creates nothing anywhere now, but the out-of-roots case
        is the one that was a write as the daemon user, so it keeps its own
        test rather than resting on the general rule."""
        dest = tmp_path / "attacker" / "deep" / "tree" / "x.txt"
        _, err = resolve_host_path(dest, writable=True, operation="cp-out")
        assert err is not None
        assert not (tmp_path / "attacker").exists()

    def test_talk_is_refused_as_a_destination(self, mount):
        _, err = resolve_host_path(
            mount / "Talk" / "x.txt", writable=True, operation="cp-out",
        )
        assert err is not None


class TestValidateHostPathWrapper:
    def test_error_only_wrapper_matches(self, mount):
        p = mount / "Users" / "alice" / "ok.json"
        p.write_text("{}")
        assert validate_host_path(p, must_exist=True, operation="op") is None
        assert validate_host_path(
            mount / "Users" / "bob" / "x", must_exist=True, operation="op",
        ) is not None


class TestDevboxKeepsNoCopyOfTheRule:
    """The devbox skill declares its host paths; it does not re-state the rule.

    Two private wrappers stood here over time. `_validate_host_path` returned
    the error alone and went when the file verbs moved onto the exec transport;
    `_resolve_host_path` went with ISSUE-447, when the three host-side
    arguments were declared through `_hostpath.host_path` and the resolution
    moved to parse time. Neither may come back: a second spelling of one
    boundary check is how the four implementations that issue consolidated came
    about in the first place.

    What each argument *does* is driven end to end in
    `tests/test_skill_host_paths_refusals.py`. What is asserted here is that
    the disposition exists and says the right thing, verb by verb — the host
    side scoped, the container side left to the server.
    """

    def test_the_private_wrappers_are_gone(self):
        from istota.skills import devbox

        for name in ("_resolve_host_path", "_validate_host_path"):
            assert not hasattr(devbox, name), (
                f"{name} is back. The rule lives in skill_host_paths and is "
                f"applied by the stamp on the declaration."
            )

    def test_the_host_and_container_sides_are_declared_apart(self):
        from istota.skills import devbox
        from istota.skills._hostpath import READ, REMOTE, WRITE, stamped

        modes = {
            (command, dest): mode
            for command, dest, mode in stamped(devbox.build_parser())
        }
        assert modes == {
            ("exec-file", "path"): READ,
            ("cp-in", "src"): READ,
            ("cp-in", "dest"): REMOTE,
            ("cp-out", "src"): REMOTE,
            ("cp-out", "dest"): WRITE,
        }


class TestDeveloperReposRoot:
    """`DEVELOPER_REPOS_DIR` is its own root, separate from the mount ones.

    The review CLI is handed a worktree path chosen by the sandboxed model, and
    it runs host-side with the daemon's filesystem view. Without scoping, "review
    this worktree" is an arbitrary directory read whose contents come back in a
    reviewer prompt.

    The root is *one user's subtree*. `setup_env` derives it and everything
    downstream reads the variable, but this module does not take that on trust:
    it requires `ISTOTA_USER_ID` and requires the root to be the directory that
    user id names. Self-scoping by `ISTOTA_USER_ID` is what the whole module
    does — see its docstring — and here it means a variable that regressed to
    the shared root is refused rather than quietly containing against it.
    """

    def test_returns_the_resolved_root(self, repos):
        assert developer_repos_root() == repos.resolve()

    def test_root_is_resolved_not_echoed(self, tmp_path, monkeypatch):
        """`repos.resolve() == repos` under pytest's tmp_path, so the test above
        cannot tell a resolving implementation from one that echoes the env var.
        A symlinked root can.

        The link is one level *up* from the user's own component, which is the
        shape a symlinked deployment root actually has (`/srv/repos` ->
        `/data/repos`). A link at the user component itself is the planted one,
        refused below.
        """
        physical = tmp_path / "physical"
        (physical / "alice").mkdir(parents=True)
        link = tmp_path / "link-to-physical"
        link.symlink_to(physical)
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(link / "alice"))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        assert developer_repos_root() == (physical / "alice").resolve()
        assert developer_repos_root() != link / "alice"

    def test_unset_is_none(self, monkeypatch):
        monkeypatch.delenv("DEVELOPER_REPOS_DIR", raising=False)
        assert developer_repos_root() is None

    def test_blank_is_none(self, monkeypatch):
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", "   ")
        assert developer_repos_root() is None

    def test_no_user_id_is_none(self, repos, monkeypatch):
        """Refusing is the module's posture for a root it cannot resolve, and
        an unscoped root is one it cannot resolve: without a user id there is
        nothing to check the subtree against."""
        monkeypatch.delenv("ISTOTA_USER_ID", raising=False)
        assert developer_repos_root() is None

    def test_blank_user_id_is_none(self, repos, monkeypatch):
        monkeypatch.setenv("ISTOTA_USER_ID", "   ")
        assert developer_repos_root() is None

    def test_the_shared_root_is_refused(self, repos, tmp_path, monkeypatch):
        """The variable's pre-split value, refused rather than accepted.

        This is the defence-in-depth half of the per-user layout: if the
        derivation upstream is ever reverted or bypassed, containment here
        stops rather than silently widening to every user's tree.
        """
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(tmp_path / "repos"))
        assert developer_repos_root() is None

    def test_another_users_subtree_is_refused(self, repos, tmp_path, monkeypatch):
        """A root named for somebody else is not this caller's root, however
        well-formed it looks."""
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(tmp_path / "repos" / "bob"))
        assert developer_repos_root() is None

    def test_a_root_symlinked_to_the_callers_own_subtree_is_refused(
        self, tmp_path, monkeypatch,
    ):
        """The case only the *as-written* half of the check catches.

        Every other refusal here is caught by the resolved name alone, so
        deleting `Path(raw).name != user_id` would leave them all green. Here
        the raw name is `bob` and the resolved one is `alice`, so a check on
        the resolved name says yes to a variable naming somebody else's
        directory. Harmless in itself — the link points at this caller's own
        tree — but it is the half of the guard that answers "is the variable
        the one the layout describes", and without it that question is not
        being asked at all.
        """
        root = tmp_path / "repos"
        (root / "alice").mkdir(parents=True)
        (root / "bob").symlink_to(root / "alice")
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(root / "bob"))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        assert developer_repos_root() is None

    @pytest.mark.parametrize("target_name", ["elsewhere", "alice"])
    def test_a_symlinked_user_component_is_refused(
        self, tmp_path, monkeypatch, target_name,
    ):
        """`{repos_dir}` was bound read-write for every admin for as long as it
        was shared, so `{repos_dir}/{user_id}` may already be a symlink a task
        left behind. `executor.get_user_repos_dir` refuses to bind one; this
        refuses to contain against one, so the two seams agree.

        **The target's own name is the parameter, and `alice` is the case that
        matters.** A planted link would be named for its victim, so its target
        can be too — and against a check that compares the *resolved* basename
        to the user id, that one passes. Only the second case distinguishes the
        structural rule from a second name comparison.
        """
        elsewhere = tmp_path / "outside" / target_name
        elsewhere.mkdir(parents=True)
        root = tmp_path / "repos"
        root.mkdir()
        (root / "alice").symlink_to(elsewhere)
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(root / "alice"))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        assert developer_repos_root() is None


class TestResolveUnderReposIsPerUser:
    """The second live exposure the per-user layout closes.

    `resolve_under_repos` runs **host-side**, in a skill CLI the proxy spawns
    outside the sandbox with the daemon's filesystem view, and the path it
    validates is one the sandboxed model chose. While `DEVELOPER_REPOS_DIR`
    named the whole of `developer.repos_dir`, `code_review --worktree <another
    admin's checkout>` passed containment and came back as reviewer-prompt
    text. The bwrap-side masks never reached this — they are a property of a
    namespace this process is not in.
    """

    def test_another_users_worktree_is_refused(self, repos, tmp_path):
        """The property, at the value `setup_env` now derives."""
        victim = tmp_path / "repos" / "bob" / "ns" / "victim--branch"
        assert victim.is_dir(), "the victim tree must exist, or this passes on not-found"
        resolved, err = resolve_under_repos(str(victim))
        assert resolved is None
        assert err and "outside" in err.lower()

    def test_the_shared_root_gives_no_containment_at_all(self, tmp_path, monkeypatch):
        """The same question asked at the variable's *pre-split* value.

        This is the regression test for the live exposure: against the module
        as it stood before this stage, containment is computed against whatever
        the variable names, so bob's worktree is admitted and returned. It is
        also the control for the test above, which cannot fail while the
        fixture hands it an already-scoped root.
        """
        root = tmp_path / "repos"
        (root / "alice" / "ns" / "wt").mkdir(parents=True)
        (root / "bob" / "ns" / "victim--branch").mkdir(parents=True)
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(root))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        resolved, err = resolve_under_repos(str(root / "bob" / "ns" / "victim--branch"))
        assert resolved is None
        assert err

    def test_the_callers_own_worktree_still_resolves(self, repos):
        """A refusal that refused everything would pass the two above and
        break every review."""
        resolved, err = resolve_under_repos(str(repos / "ns" / "project--branch"))
        assert err is None
        assert resolved == (repos / "ns" / "project--branch").resolve()


class TestResolveUnderRepos:
    def test_accepts_a_worktree_inside(self, repos):
        wt = repos / "ns" / "project--branch"
        resolved, err = resolve_under_repos(str(wt))
        assert err is None
        assert resolved == wt.resolve()

    def test_returns_the_resolved_path_not_the_argument(self, repos):
        """Callers must operate on what comes back — see the module docstring.

        The argument has to be genuinely non-canonical for this to mean
        anything. `Path("a/./b")` collapses to `a/b` at construction, so a
        `.` component tests nothing; a symlink is a difference pathlib cannot
        normalise away, so only a real `resolve()` produces the target.
        """
        target = repos / "ns" / "project--branch"
        link = repos / "ns" / "via-link"
        link.symlink_to(target)

        resolved, err = resolve_under_repos(str(link))
        assert err is None
        # The argument and the answer are different paths on disk.
        assert Path(str(link)) != target
        assert resolved == target.resolve()

    def test_accepts_a_symlink_that_stays_inside(self, repos):
        """Following links is what catches an escape, so a link that does not
        escape has to be accepted. Pinned because it differs from
        `resolve_host_path`, which refuses a symlinked argument outright."""
        target = repos / "ns" / "project--branch"
        link = repos / "ns" / "inner-link"
        link.symlink_to(target)
        resolved, err = resolve_under_repos(str(link))
        assert err is None
        assert resolved == target.resolve()

    def test_resolves_a_symlinked_root_to_the_physical_path(self, tmp_path, monkeypatch):
        """A root reached through a link must still contain its own worktrees."""
        physical = tmp_path / "physical-repos"
        (physical / "alice" / "ns" / "wt").mkdir(parents=True)
        link_root = tmp_path / "linked-repos"
        link_root.symlink_to(physical)
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(link_root / "alice"))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        assert developer_repos_root() == (physical / "alice").resolve()
        resolved, err = resolve_under_repos(str(link_root / "alice" / "ns" / "wt"))
        assert err is None
        assert resolved == (physical / "alice" / "ns" / "wt").resolve()

    def test_refuses_a_path_outside(self, repos, tmp_path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        resolved, err = resolve_under_repos(str(outside))
        assert resolved is None
        assert err and "outside" in err.lower()

    def test_refuses_traversal_out(self, repos, tmp_path):
        """The target must EXIST outside the root, or this passes on the
        not-found branch and says nothing about containment.

        Two levels of `..` land in the *shared* root, one step above this
        caller's subtree, which is the traversal that matters now.
        """
        outside = repos.parent / "elsewhere"
        outside.mkdir()
        resolved, err = resolve_under_repos(str(repos / "ns" / ".." / ".." / "elsewhere"))
        assert resolved is None
        assert err and "outside" in err.lower()

    def test_refuses_a_blank_argument(self, repos):
        """`Path("")` is `.`, so without an explicit guard the answer depends
        on the daemon's working directory."""
        for blank in ("", "   "):
            resolved, err = resolve_under_repos(blank)
            assert resolved is None
            assert err and "empty" in err.lower()

    def test_refuses_a_regular_file(self, repos):
        """A worktree is a directory; `git -C <file>` is a confusing failure."""
        f = repos / "ns" / "project--branch" / "README.md"
        f.write_text("x")
        resolved, err = resolve_under_repos(str(f))
        assert resolved is None
        assert err and "directory" in err.lower()

    def test_refuses_a_symlink_pointing_out(self, repos, tmp_path):
        """A symlink planted inside repos_dir is the interesting attack: the
        argument looks compliant and resolution is what catches it."""
        secret = tmp_path / "outside-secrets"
        secret.mkdir()
        link = repos / "ns" / "escape"
        link.symlink_to(secret)
        resolved, err = resolve_under_repos(str(link))
        assert resolved is None
        assert err

    def test_refuses_when_env_unset(self, monkeypatch, tmp_path):
        """Never widen to the whole filesystem because the var is missing."""
        monkeypatch.delenv("DEVELOPER_REPOS_DIR", raising=False)
        resolved, err = resolve_under_repos(str(tmp_path))
        assert resolved is None
        assert err and "DEVELOPER_REPOS_DIR" in err

    def test_refuses_a_missing_path(self, repos):
        resolved, err = resolve_under_repos(str(repos / "ns" / "no-such-worktree"))
        assert resolved is None
        assert err

    def test_repos_root_itself_is_allowed(self, repos):
        """Reviewing at the root is odd but not an escape."""
        resolved, err = resolve_under_repos(str(repos))
        assert err is None
        assert resolved == repos.resolve()

    def test_sibling_prefix_is_not_inside(self, tmp_path, monkeypatch):
        """`.../alice-evil` must not pass because it shares a string prefix
        with `.../alice`. Containment is by path component, not by startswith.

        The sibling is the interesting one now that the root ends in the user
        id: a `startswith` implementation would admit a directory belonging to
        a user whose id merely begins with this one's.
        """
        root = tmp_path / "repos" / "alice"
        root.mkdir(parents=True)
        evil = tmp_path / "repos" / "alice-evil"
        evil.mkdir()
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(root))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        resolved, err = resolve_under_repos(str(evil))
        assert resolved is None
        assert err

    def test_does_not_use_the_mount_roots(self, repos, mount):
        """This is a separate allowlist. A path under the user's workspace is
        legitimate for kv/devbox and is not a worktree."""
        resolved, err = resolve_under_repos(str(mount / "Users" / "alice"))
        assert resolved is None
        assert err

    def test_refuses_a_non_path_argument(self, repos):
        """The contract is an error tuple, never a raise. `--worktree` omitted
        gives None, and `Path(None)` is a TypeError that would escape as a
        traceback instead of the JSON envelope the proxy expects."""
        for bad in (None, 42, b"/bytes/path", object()):
            resolved, err = resolve_under_repos(bad)
            assert resolved is None
            assert err

    def test_refuses_an_intermediate_symlink_out(self, repos, tmp_path):
        """The escape does not have to be the leaf. An argument whose *middle*
        component is a link out would survive an implementation that only
        inspected the last element."""
        outside = tmp_path / "outside-tree"
        (outside / "sub").mkdir(parents=True)
        midlink = repos / "ns" / "midlink"
        midlink.symlink_to(outside)
        resolved, err = resolve_under_repos(str(midlink / "sub"))
        assert resolved is None
        assert err and "outside" in err.lower()


class TestDeveloperReposRootSanity:
    """`DEVELOPER_REPOS_DIR` is derived from operator config, which validates
    it nowhere. Refusing an unset variable and then accepting `/` would not be
    a boundary.

    Each case here names the guard it is aimed at, because the user-id check
    added for the per-user split would refuse most of these on its own and a
    guard tested only through another guard is a guard nobody would notice
    losing.
    """

    def test_refuses_filesystem_root(self, monkeypatch):
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", "/")
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        assert developer_repos_root() is None
        resolved, err = resolve_under_repos("/etc")
        assert resolved is None
        assert err

    def test_refuses_a_single_component_root(self, monkeypatch):
        """Aimed at the depth guard alone: `/alice` is the subtree `alice`
        names, so the user-id check passes and only the depth refuses it."""
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", "/alice")
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        assert developer_repos_root() is None

    def test_refuses_a_relative_root(self, monkeypatch):
        """A relative value anchors on wherever the CLI was started."""
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", "relative/repos/alice")
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        assert developer_repos_root() is None

    def test_accepts_an_ordinary_root(self, tmp_path, monkeypatch):
        root = tmp_path / "srv" / "repos" / "alice"
        root.mkdir(parents=True)
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(root))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        assert developer_repos_root() == root.resolve()


class TestUserWorkspaceRoot:
    """The one root a caller may *derive a destination inside*.

    `browse screenshot` needs it by name rather than by list position: the
    first entry of `allowed_host_roots` is usually the task's deferred temp
    dir, which nothing serves and the scheduler sweeps.
    """

    def test_is_the_callers_own_subtree(self, mount):
        assert user_workspace_root() == (mount / "Users" / "alice").resolve()

    def test_is_none_without_a_user_id(self, mount, monkeypatch):
        monkeypatch.delenv("ISTOTA_USER_ID")
        assert user_workspace_root() is None

    def test_is_none_without_a_mount(self, mount, monkeypatch):
        monkeypatch.delenv("NEXTCLOUD_MOUNT_PATH")
        assert user_workspace_root() is None

    @pytest.mark.parametrize("user_id", ["", ".", "..", "/etc", "../bob", "a/b"])
    def test_a_user_id_that_does_not_name_a_child_gets_no_root(
        self, mount, monkeypatch, user_id,
    ):
        """The collapsed join is `{mount}/Users` — every user at once."""
        monkeypatch.setenv("ISTOTA_USER_ID", user_id)
        assert user_workspace_root() is None

    def test_allowed_host_roots_uses_the_same_derivation(self, mount):
        """One derivation, so a derived destination cannot land somewhere the
        allowlist would then refuse — or, worse, somewhere it would not."""
        assert user_workspace_root() in allowed_host_roots(writable=True)


class TestALeafSymlinkIsRefusedAsADestination:
    """The parent is resolved and contained; the final component is not.

    `resolved` is `resolved_parent / name`, so a link standing at that name
    passes containment as a *name* and is then followed by whatever opens it —
    `cp-out`'s `write_bytes`, the OPML exporter's own open. The tree is bound
    read-write into the sandbox, so the link is model-plantable.

    That the shipped destination verbs inherit this is asserted where they are
    now driven, in `tests/test_skill_host_paths_refusals.py`: the refusal moved
    to parse time with the stamps, so there is no per-skill wrapper left here
    to reach the branch through.
    """

    def test_refused(self, mount, tmp_path):
        outside = tmp_path / "outside.txt"
        link = mount / "Users" / "alice" / "export.csv"
        link.symlink_to(outside)
        resolved, err = resolve_host_path(
            link, writable=True, operation="export",
        )
        assert resolved is None
        assert err is not None and "symlink" in err
        assert not outside.exists()

    def test_a_link_that_stays_inside_the_root_is_refused_too(self, mount):
        """No consumer writes through a link on purpose, and telling the two
        apart at check time does not survive the link being repointed."""
        target = mount / "Users" / "alice" / "real.csv"
        target.write_text("x")
        link = mount / "Users" / "alice" / "alias.csv"
        link.symlink_to(target)
        _, err = resolve_host_path(link, writable=True, operation="export")
        assert err is not None

    def test_an_ordinary_existing_file_is_still_an_allowed_destination(self, mount):
        """The overwrite case, which the refusal above must not take with it."""
        target = mount / "Users" / "alice" / "real.csv"
        target.write_text("x")
        resolved, err = resolve_host_path(target, writable=True, operation="export")
        assert err is None
        assert resolved == target.resolve()


class TestADotDotLeafIsRefusedAsADestination:
    """`path_under_roots` is lexical, and the leaf is joined on unresolved.

    `{root}/..` passes `relative_to({root})`, so containment says yes about
    the root's parent. The kernel refuses the open with EISDIR today; a
    boundary should not rest on that.
    """

    def test_refused(self, mount):
        _, err = resolve_host_path(
            mount / "Users" / "alice" / "..", writable=True, operation="export",
        )
        assert err is not None
        assert "Not a file name" in err

    def test_a_dot_leaf_never_reaches_the_guard_and_is_refused_anyway(self, mount):
        """`PurePath` drops a `.` component, so the guard never sees one.

        The path becomes the workspace itself, whose parent is `{mount}/Users`
        — outside every root — so it is refused one check earlier. Written
        down because a reader adding `.` to the guard's tuple would be adding
        a case that cannot arrive.
        """
        _, err = resolve_host_path(
            mount / "Users" / "alice" / ".", writable=True, operation="export",
        )
        assert err is not None
        assert "outside allowed roots" in err


class TestWriteResolved:
    def test_writes_the_bytes(self, mount):
        dest = mount / "Users" / "alice" / "out.bin"
        write_resolved(dest, b"hello")
        assert dest.read_bytes() == b"hello"

    def test_truncates_rather_than_appending(self, mount):
        dest = mount / "Users" / "alice" / "out.bin"
        dest.write_bytes(b"aaaaaaaaaa")
        write_resolved(dest, b"bb")
        assert dest.read_bytes() == b"bb"

    def test_exclusive_refuses_an_existing_file_instead(self, mount):
        """The half a caller deriving a unique name needs.

        Overwriting is right for a destination the caller named and wrong for
        one it derived to be unique — there the overwrite is the collision,
        silently.
        """
        dest = mount / "Users" / "alice" / "out.bin"
        dest.write_bytes(b"first")
        with pytest.raises(FileExistsError):
            write_resolved(dest, b"second", exclusive=True)
        assert dest.read_bytes() == b"first"

    def test_the_mode_is_what_a_plain_open_would_have_produced(self, mount):
        """Requested `0o666`, narrowed by the umask, same as `open(p, "wb")`.

        Run under `umask 002`, which is the shape a group-shared mount
        deployment uses and the only one where the question has an answer: a
        fixed `0o644` and a umask-narrowed `0o666` are the same file under the
        `umask 022` this suite otherwise runs with, so the assertion would be
        vacuous and a narrowing would ship unseen.
        """
        import os as _os
        import stat

        previous = _os.umask(0o002)
        try:
            plain = mount / "Users" / "alice" / "plain.bin"
            with open(plain, "wb") as fh:
                fh.write(b"x")
            theirs = mount / "Users" / "alice" / "ours.bin"
            write_resolved(theirs, b"x")
            assert stat.S_IMODE(_os.stat(plain).st_mode) == 0o664
            assert stat.S_IMODE(_os.stat(theirs).st_mode) == 0o664
        finally:
            _os.umask(previous)

    def test_refuses_to_follow_a_symlink_planted_after_the_check(
        self, mount, tmp_path,
    ):
        """The window `resolve_host_path` cannot close on its own.

        The check refused a link as of its own moment; this is what happens
        when one appears between that moment and the open.
        """
        outside = tmp_path / "victim.txt"
        outside.write_text("original")
        link = mount / "Users" / "alice" / "out.bin"
        link.symlink_to(outside)
        with pytest.raises(OSError):
            write_resolved(link, b"overwritten")
        assert outside.read_text() == "original"


class TestBrowseScreenshotIsScoped:
    """`--output` was an unguarded host write, and the default was worse.

    The old default was `/tmp/screenshot.png`. The CLI runs host-side through
    the proxy while the model's `/tmp` is the sandbox's own tmpfs, so the file
    landed on the host and the model was handed a path it could not open.

    **Driven through `main`, because that is where the scoping is.** `--output`
    is declared `WRITE` and resolved by `parse_and_resolve`, so calling
    `cmd_screenshot` with a hand-built namespace exercises no boundary at all —
    it would pass against a verb with no stamp on it. The refusals below are
    the facade's envelope and exit 1, ahead of dispatch.

    Every case here asserts on the filesystem as well as on the envelope. The
    ordering bug this module already carries a test for created the directory
    and *then* refused, which a test reading the return value alone cannot see.
    """

    @pytest.fixture
    def post(self):
        """`httpx.post` answering with a real PNG, and a record of the calls."""
        from unittest.mock import MagicMock, patch

        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "image/png"}
        resp.content = b"\x89PNG\r\n\x1a\n" + b"pretend pixels"
        with patch("istota.skills.browse.httpx.post", return_value=resp) as m:
            yield m

    def _shot(self, *argv):
        from istota.skills.browse import main

        return run_skill_main(main, ["screenshot", *argv]).envelope

    def test_an_output_outside_the_workspace_is_refused(self, mount, tmp_path, post):
        dest = tmp_path / "attacker" / "deep" / "shot.png"
        result = self._shot("https://example.com", "-o", str(dest))
        assert result["status"] == "error"
        assert result["reason"] == "host_path_refused"
        assert not dest.exists()
        # The mkdir must never have run: an out-of-bounds tree created as the
        # daemon user is a write, whatever the envelope then says.
        assert not (tmp_path / "attacker").exists()
        # And nothing was captured, so a refusal costs no browser time.
        post.assert_not_called()

    def test_another_users_workspace_is_refused(self, mount, post):
        dest = mount / "Users" / "bob" / "shot.png"
        result = self._shot("https://example.com", "-o", str(dest))
        assert result["status"] == "error"
        assert not dest.exists()
        post.assert_not_called()

    def test_an_output_inside_the_workspace_is_written(self, mount, post):
        dest = mount / "Users" / "alice" / "shots" / "page.png"
        result = self._shot("https://example.com", "-o", str(dest))
        assert result["status"] == "ok"
        assert dest.read_bytes().startswith(b"\x89PNG")
        assert result["media_type"] == "image/png"

    def test_the_default_lands_in_the_callers_own_workspace(
        self, mount, monkeypatch, post,
    ):
        monkeypatch.setenv("ISTOTA_BOT_DIR_NAME", "istota")
        result = self._shot("https://example.com")
        assert result["status"] == "ok"
        written = Path(result["path"])
        expected_dir = (mount / "Users" / "alice" / "istota" / "screenshots").resolve()
        assert written.parent == expected_dir
        assert written.read_bytes().startswith(b"\x89PNG")
        # The `?path=` spelling `/chat/files` takes, so a reply can embed the
        # picture without rebuilding the path by hand.
        assert result["workspace_path"] == (
            "/Users/alice/istota/screenshots/" + written.name
        )

    def test_the_default_follows_the_configured_bot_dir(
        self, mount, monkeypatch, post,
    ):
        monkeypatch.setenv("ISTOTA_BOT_DIR_NAME", "mister_jones")
        result = self._shot("https://example.com")
        assert Path(result["path"]).parent.name == "screenshots"
        assert Path(result["path"]).parent.parent.name == "mister_jones"

    def test_a_bot_dir_that_is_not_a_plain_component_is_refused(
        self, mount, monkeypatch, post,
    ):
        monkeypatch.setenv("ISTOTA_BOT_DIR_NAME", "../../etc")
        result = self._shot("https://example.com")
        assert result["status"] == "error"
        post.assert_not_called()

    def test_no_output_and_no_workspace_refuses_rather_than_falling_back(
        self, monkeypatch, post,
    ):
        """The old fallback was `/tmp/screenshot.png`, which is the bug."""
        monkeypatch.delenv("NEXTCLOUD_MOUNT_PATH", raising=False)
        monkeypatch.delenv("ISTOTA_USER_ID", raising=False)
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
        result = self._shot("https://example.com")
        assert result["status"] == "error"
        assert "NEXTCLOUD_MOUNT_PATH" in result["error"]
        # Nothing was captured, so there are no bytes to have written
        # anywhere. Asserting on `/tmp/screenshot.png` instead would read
        # machine-global state this test does not own.
        post.assert_not_called()

    def test_no_bot_dir_refuses_and_names_that_variable(
        self, mount, monkeypatch, post,
    ):
        """Two things stop the directory resolving and they read differently.

        Naming the mount variables when `ISTOTA_BOT_DIR_NAME` is the one
        missing sends the reader to a setting that is correct — the misreport
        `doctor` states the rule against. The variable is required rather than
        defaulted, matching the two `memory` skills: guessing `istota` on a
        deployment whose bot is called something else files the capture beside
        the real bot dir, where it serves fine and reports nothing.
        """
        monkeypatch.delenv("ISTOTA_BOT_DIR_NAME", raising=False)
        result = self._shot("https://example.com")
        assert result["status"] == "error"
        assert "ISTOTA_BOT_DIR_NAME" in result["error"]
        assert "NEXTCLOUD_MOUNT_PATH" not in result["error"]
        post.assert_not_called()

    def test_a_derived_name_never_overwrites_an_existing_capture(
        self, mount, monkeypatch, post,
    ):
        """Two tasks of one user can derive the same name in one second.

        The scheduler runs a worker pool, so this is ordinary rather than
        adversarial. Under a check-then-write the second capture replaces the
        first and both report ok, the first with a `size` describing bytes
        that are no longer on disk. The name is claimed by `O_EXCL` instead.
        """
        monkeypatch.setenv("ISTOTA_BOT_DIR_NAME", "istota")
        first = self._shot("https://example.com")
        assert first["status"] == "ok"

        second = self._shot("https://example.com")
        assert second["status"] == "ok"
        assert second["path"] != first["path"]
        assert Path(first["path"]).exists()
        assert Path(first["path"]).stat().st_size == first["size"]

    def test_a_channels_destination_gets_no_workspace_path(
        self, mount, monkeypatch, post,
    ):
        """`/chat/files` serves `/Users/{uid}` and refuses `/Channels/{token}`.

        A `WRITE` admits the task's own channel directory — a destination's
        content stays inside the task's working context — so writing there is
        legitimate, and handing back a `?path=` spelling for it would have the
        reply build a URL the endpoint refuses by design.
        """
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "tok1")
        dest = mount / "Channels" / "tok1" / "shot.png"
        result = self._shot("https://example.com", "-o", str(dest))
        assert result["status"] == "ok"
        assert dest.exists()
        assert "workspace_path" not in result

    def test_a_body_that_is_not_a_raster_is_not_written(self, mount):
        """A 200 labelled `image/png` over an HTML error page.

        Same predicate `/chat/files` sniffs the file with, so a capture that
        would come back as a download rather than an image is refused here
        instead of being embedded as a broken one.
        """
        from unittest.mock import MagicMock, patch

        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "image/png"}
        resp.content = b"<html><body>upstream error</body></html>"
        dest = mount / "Users" / "alice" / "shot.png"
        with patch("istota.skills.browse.httpx.post", return_value=resp):
            result = self._shot("https://example.com", "-o", str(dest))
        assert result["status"] == "error"
        assert not dest.exists()


class TestHealthExportCsvIsScoped:
    """`--output` was an arbitrary host write with a whole health record in it.

    Driven through `main`, for the reason the browse class above states: the
    resolution is at parse and a hand-built namespace reaches none of it.
    `health.main` takes no argv and parses `sys.argv`, which `run_skill_main`
    sets — the one skill in the tree written that way.
    """

    def _export(self, output):
        from istota.skills.health import main

        return run_skill_main(main, ["export-csv", "--output", str(output)])

    def test_a_path_outside_the_workspace_is_refused_before_the_database(
        self, mount, tmp_path,
    ):
        """No `HEALTH_DB_PATH` is set here, so reaching `_connect` would fail
        with a different message — which is what shows the refusal came first.
        The export is the caller's entire health record; a refusal should not
        read it out of the database on the way to saying no."""
        dest = tmp_path / "attacker" / "panels.csv"
        run = self._export(dest)
        assert run.exit_code == 1
        assert run.envelope["status"] == "error"
        assert run.envelope["reason"] == "host_path_refused"
        assert "outside allowed roots" in run.envelope["error"]
        assert not dest.exists()
        assert not (tmp_path / "attacker").exists()

    def test_another_users_workspace_is_refused(self, mount):
        dest = mount / "Users" / "bob" / "panels.csv"
        run = self._export(dest)
        # The payload, not the exit: with the stamp removed this verb still
        # exits 1, from `_db_path` finding no HEALTH_DB_PATH. A test reading
        # only the status passes against an unguarded verb, which is what the
        # control found.
        assert "outside allowed roots" in run.envelope["error"]
        assert not dest.exists()


class TestFeedsOpmlIsScoped:
    """One read and one write, and both go to a CLI that opens the path itself.

    So the *resolved* path is what has to travel: handing the Click CLI the
    original argument re-walks every symlink the check just settled. Since the
    resolution is `parse_and_resolve`'s, that is now a property of the value on
    the namespace rather than of anything these two handlers do — which is what
    the two "the resolved path is what reaches the CLI" cases pin.
    """

    @pytest.fixture
    def ran(self):
        from unittest.mock import patch

        with patch(
            "istota.skills.feeds._run", return_value={"status": "ok"},
        ) as m:
            yield m

    def _feeds(self, *argv):
        from istota.skills.feeds import main

        return run_skill_main(main, list(argv))

    def test_import_outside_the_workspace_is_refused(
        self, mount, tmp_path, ran,
    ):
        source = tmp_path / "elsewhere" / "subs.opml"
        source.parent.mkdir()
        source.write_text("<opml/>")
        run = self._feeds("import-opml", str(source))
        assert run.exit_code == 1
        assert run.envelope["status"] == "error"
        ran.assert_not_called()

    def test_export_outside_the_workspace_is_refused_and_creates_nothing(
        self, mount, tmp_path, ran,
    ):
        dest = tmp_path / "attacker" / "deep" / "subs.opml"
        run = self._feeds("export-opml", "--output", str(dest))
        assert run.exit_code == 1
        assert run.envelope["status"] == "error"
        assert not dest.exists()
        assert not (tmp_path / "attacker").exists()
        ran.assert_not_called()

    def test_the_resolved_path_is_what_reaches_the_cli(self, mount, ran):
        """The path handed down must be the resolved one, not the argument.

        Reached through a symlinked intermediate directory so the two strings
        genuinely differ: a test where they happen to be equal cannot tell a
        resolving implementation from a passthrough, and on a host whose temp
        directory is already a realpath they are equal for every plain path.
        """
        real = mount / "Users" / "alice" / "real" / "exports"
        real.mkdir(parents=True)
        (mount / "Users" / "alice" / "via").symlink_to(
            mount / "Users" / "alice" / "real",
        )
        dest = mount / "Users" / "alice" / "via" / "exports" / "subs.opml"
        assert str(dest) != str(dest.resolve())

        self._feeds("export-opml", "--output", str(dest))
        ran.assert_called_once()
        assert ran.call_args[0][0] == [
            "export-opml", "--output", str(real.resolve() / "subs.opml"),
        ]

    def test_export_inside_the_workspace_is_allowed(self, mount, ran):
        dest = mount / "Users" / "alice" / "exports" / "subs.opml"
        self._feeds("export-opml", "--output", str(dest))
        ran.assert_called_once()
        # The Click CLI opens the path itself, so the facade is what has to
        # have made the directory: resolution creates nothing.
        assert dest.parent.is_dir()

    def test_import_inside_the_workspace_is_allowed(self, mount, ran):
        real = mount / "Users" / "alice" / "real"
        real.mkdir()
        (real / "subs.opml").write_text("<opml/>")
        (mount / "Users" / "alice" / "via").symlink_to(real)
        source = mount / "Users" / "alice" / "via" / "subs.opml"
        assert str(source) != str(source.resolve())

        self._feeds("import-opml", str(source))
        ran.assert_called_once()
        # Resolved, again: reopening the argument re-walks `via`.
        assert ran.call_args[0][0] == [
            "import-opml", str((real / "subs.opml").resolve()),
        ]


# ---------------------------------------------------------------------------
# One rule, one implementation (spec stage 2)
# ---------------------------------------------------------------------------
#
# Four implementations of one containment rule stood in the tree:
# `skill_host_paths.allowed_host_roots`, `memory_search._indexable_roots`,
# `scheduler_deferred._source_path_allowed` and
# `outbound_drafts._confined_attachment`. They differed in their roots, in
# their symlink handling, and in whether the conversation token was guarded —
# which is how the drift happened rather than a decision anybody made.
#
# The consolidation unifies the *implementation* and deliberately not the
# *roots*: the deferred replay legitimately has no channel root and no Talk
# root, and widening one caller as a side effect of a refactor is the failure
# this stage exists not to commit. So there are two kinds of test below —
# each caller's exact root set, asserted where that caller decides it, and an
# equivalence table against verbatim copies of the four old derivations.


def _legacy_env_roots(mount, deferred, user_id, token):
    """`allowed_host_roots`, as it stood before the consolidation."""
    roots = []
    if deferred:
        roots.append(Path(deferred).resolve())
    if mount and user_id:
        own = scoped_user_dir(Path(mount).resolve() / "Users", user_id)
        if own is not None:
            roots.append(own)
        if token and "/" not in token and token not in (".", ".."):
            roots.append(Path(mount).resolve() / "Channels" / token)
        roots.append(Path(mount).resolve() / "Talk")
    return roots


def _legacy_indexable_roots(mount, deferred, user_id, token):
    """`memory_search._indexable_roots`: no Talk, and the token unguarded."""
    roots = []
    if mount:
        own = scoped_user_dir(Path(mount) / "Users", user_id)
        if own is not None:
            roots.append(own)
    if mount and token:
        roots.append(Path(mount) / "Channels" / token)
    if deferred:
        roots.append(Path(deferred))
    return [r.resolve() for r in roots]


def _legacy_deferred_roots(mount, deferred, user_id, token):
    """`scheduler_deferred._source_path_allowed`: the workspace and the temp dir.

    The user root came from `Config.workspace_root(user_id)`, an unscoped
    `{mount}/Users/{user_id}` join — which is the ISSUE-402 shape and is the
    one thing the consolidation changes here.
    """
    roots = []
    if deferred:
        roots.append(Path(deferred).resolve())
    if mount and user_id:
        roots.append((Path(mount) / "Users" / user_id).resolve())
    return roots


def _legacy_draft_roots(mount, deferred, user_id, token):
    """`outbound_drafts._confined_attachment`: the owner's workspace alone."""
    if not mount or not user_id:
        return []
    return [(Path(mount) / "Users" / user_id).resolve()]


def _legacy_allows(builder, target, mount, deferred, user_id, token):
    """The containment answer that copy would have given."""
    resolved = Path(target).resolve()
    for root in builder(mount, deferred, user_id, token):
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


class TestWorkspaceRoots:
    """The one derivation, from explicit values rather than the environment."""

    def test_scopes_the_mount_to_the_named_user(self, tmp_path):
        from istota.skill_host_paths import workspace_roots

        mount = tmp_path / "mount"
        (mount / "Users" / "alice").mkdir(parents=True)
        roots = workspace_roots(mount=mount, user_id="alice")
        assert roots == [(mount / "Users" / "alice").resolve()]

    @pytest.mark.parametrize("user_id", [".", "..", "/etc", "../bob", "a/b"])
    def test_a_user_id_that_does_not_name_a_child_costs_that_root_alone(
        self, tmp_path, user_id,
    ):
        """ISSUE-402: the collapsed join is `{mount}/Users`, every user at once.

        The channel and Talk roots are scoped by the token and by nothing, so
        they survive — the documented behaviour of the environment-reading
        version, restated here because this is now where it is decided.

        """
        from istota.skill_host_paths import workspace_roots

        mount = tmp_path / "mount"
        (mount / "Users" / "bob").mkdir(parents=True)
        roots = workspace_roots(
            mount=mount, user_id=user_id, conversation_token="tok", talk=True,
        )
        assert (mount / "Users").resolve() not in roots
        assert not any(
            (mount / "Users" / "bob").resolve().is_relative_to(r) for r in roots
        )
        assert (mount / "Channels" / "tok").resolve() in roots

    @pytest.mark.parametrize("user_id", [" alice", "alice ", "\talice"])
    def test_a_user_id_needing_a_strip_is_refused_rather_than_normalised(
        self, tmp_path, user_id,
    ):
        """The case that is *not* about a collapsed join.

        `" alice"` names a real, contained directory — a different one from
        the id as written — so the failure mode is not a widened root but two
        directories for one identity: the sandbox binds `{mount}/Users/ alice`
        while a stripping allowlist admits `{mount}/Users/alice`, which is
        somebody else's. `is_scopable_user_id` refuses the spelling for that
        reason and this derivation has to agree with it, and with
        `Config.workspace_root`, which asks the same question of the same
        value. The env readers strip their own variables before calling in;
        that is where the normalisation belongs and it is all it does there.

        A version of this function stripping the id before scoping passes
        every other case in this class.
        """
        from istota.skill_host_paths import workspace_roots

        mount = tmp_path / "mount"
        (mount / "Users" / "alice").mkdir(parents=True)
        roots = workspace_roots(mount=mount, user_id=user_id)
        assert (mount / "Users" / "alice").resolve() not in roots
        assert roots == []

    def test_an_absent_user_id_contributes_nothing_from_the_mount(self, tmp_path):
        """Fail closed: with no identity there is no subtree to scope to."""
        from istota.skill_host_paths import workspace_roots

        mount = tmp_path / "mount"
        mount.mkdir()
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        roots = workspace_roots(
            mount=mount, user_id="", deferred_dir=deferred,
            conversation_token="tok", talk=True,
        )
        assert roots == [deferred.resolve()]

    @pytest.mark.parametrize("token", ["../..", "a/b", ".", "..", "", "   "])
    def test_a_traversing_token_is_not_turned_into_a_path(self, tmp_path, token):
        from istota.skill_host_paths import workspace_roots

        mount = tmp_path / "mount"
        (mount / "Users" / "alice").mkdir(parents=True)
        roots = workspace_roots(
            mount=mount, user_id="alice", conversation_token=token,
        )
        assert all("Channels" not in str(r) for r in roots)

    def test_talk_is_opt_in_and_never_writable(self, tmp_path):
        """The shared read-only root, so a caller has to ask for it.

        `env_host_roots` asks on behalf of a skill CLI read; the daemon-side
        callers do not, which is the whole of what keeps their root sets
        different from each other.
        """
        from istota.skill_host_paths import workspace_roots

        mount = tmp_path / "mount"
        (mount / "Users" / "alice").mkdir(parents=True)
        talk = (mount / "Talk").resolve()
        assert talk not in workspace_roots(mount=mount, user_id="alice")
        assert talk in workspace_roots(mount=mount, user_id="alice", talk=True)
        assert talk not in workspace_roots(
            mount=mount, user_id="alice", talk=True, writable=True,
        )

    def test_no_mount_leaves_the_deferred_dir_standing_alone(self, tmp_path):
        from istota.skill_host_paths import workspace_roots

        deferred = tmp_path / "deferred"
        deferred.mkdir()
        assert workspace_roots(
            mount=None, user_id="alice", deferred_dir=deferred,
        ) == [deferred.resolve()]

    def test_nothing_at_all_is_an_empty_list(self):
        from istota.skill_host_paths import workspace_roots

        assert workspace_roots(mount=None, user_id="") == []


class TestEnvHostRoots:
    """The environment-reading wrapper, and the alias three call sites use."""

    def test_it_is_what_allowed_host_roots_returns(self, mount, monkeypatch):
        from istota.skill_host_paths import env_host_roots

        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "tok1")
        for writable in (False, True):
            assert env_host_roots(writable=writable) == allowed_host_roots(
                writable=writable,
            )

    def test_it_derives_the_four_roots_the_sandbox_binds(
        self, mount, monkeypatch, tmp_path,
    ):
        from istota.skill_host_paths import env_host_roots

        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "tok1")
        assert set(env_host_roots()) == {
            (tmp_path / "deferred").resolve(),
            (mount / "Users" / "alice").resolve(),
            (mount / "Channels" / "tok1").resolve(),
            (mount / "Talk").resolve(),
        }

    def test_talk_can_be_dropped_for_a_read_whose_bytes_leave_the_task(
        self, mount, monkeypatch,
    ):
        from istota.skill_host_paths import env_host_roots

        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "tok1")
        assert (mount / "Talk").resolve() not in env_host_roots(talk=False)
        assert (mount / "Users" / "alice").resolve() in env_host_roots(talk=False)


class TestPathUnderRoots:
    def test_a_path_at_a_root_is_under_it(self, tmp_path):
        from istota.skill_host_paths import path_under_roots

        assert path_under_roots(tmp_path, [tmp_path])

    def test_a_sibling_prefix_is_not_a_child(self, tmp_path):
        """Lexical containment, not `startswith`: `/a/bc` is not under `/a/b`."""
        from istota.skill_host_paths import path_under_roots

        assert not path_under_roots(tmp_path / "bc", [tmp_path / "b"])

    def test_no_roots_refuses(self, tmp_path):
        from istota.skill_host_paths import path_under_roots

        assert not path_under_roots(tmp_path, [])


class TestTheFourCallersRootSets:
    """Each caller's exact root set, asserted where that caller decides it.

    The consolidation is of the implementation. These are what say the roots
    did not move with it.
    """

    def test_the_skill_cli_read_gets_all_four(self, mount, monkeypatch, tmp_path):
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "tok1")
        assert set(allowed_host_roots(writable=False)) == {
            (tmp_path / "deferred").resolve(),
            (mount / "Users" / "alice").resolve(),
            (mount / "Channels" / "tok1").resolve(),
            (mount / "Talk").resolve(),
        }

    def test_memory_search_index_file_gets_no_talk_root(self, mount, monkeypatch):
        """`index file` content comes back out through `search`.

        Talk is bound read-only into the sandbox because a task may read a
        Talk attachment into its own reasoning; indexing one into this user's
        searchable store is the other question, and this root set is what
        answers it. Driven through the verb rather than a helper, since a
        helper a test names is one the verb can stop calling.

        No `ISTOTA_DB_PATH` is set, so reaching the database would exit with a
        different message — which is what shows the refusal came first.
        """
        from istota.skills import memory_search

        monkeypatch.delenv("ISTOTA_DB_PATH", raising=False)
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "tok1")
        talk_file = mount / "Talk" / "shared.txt"
        talk_file.write_text("someone else's attachment")

        out = memory_search.cmd_index_file(
            argparse.Namespace(path=str(talk_file), source_type=None),
        )
        assert out["status"] == "error"
        assert "outside allowed roots" in out["error"]

    def test_memory_search_index_file_keeps_its_other_three_roots(
        self, mount, monkeypatch, tmp_path,
    ):
        from istota.memory import search as memory_search_lib
        from istota.skills import memory_search

        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "tok1")
        monkeypatch.setenv("ISTOTA_DB_PATH", str(tmp_path / "framework.db"))
        seen: list[str] = []

        def _record(conn, user_id, path, content, source_type):
            seen.append(path)
            return 1

        monkeypatch.setattr(memory_search_lib, "index_file", _record)

        candidates = [
            mount / "Users" / "alice" / "own.txt",
            mount / "Channels" / "tok1" / "channel.txt",
            tmp_path / "deferred" / "deferred.txt",
        ]
        for candidate in candidates:
            candidate.write_text("x")
            out = memory_search.cmd_index_file(
                argparse.Namespace(path=str(candidate), source_type=None),
            )
            assert out["status"] == "ok", out
        # The *resolved* path is what the row records, not the argument.
        assert seen == [str(c.resolve()) for c in candidates]

    def test_memory_search_index_file_guards_the_conversation_token(
        self, mount, monkeypatch,
    ):
        """The guard `_indexable_roots` lacked and the consolidation adds:
        it joined `ISTOTA_CONVERSATION_TOKEN` raw."""
        from istota.skills import memory_search

        monkeypatch.delenv("ISTOTA_DB_PATH", raising=False)
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "../..")
        victim = mount / "Users" / "bob" / "private.txt"
        victim.write_text("bob's notes")

        out = memory_search.cmd_index_file(
            argparse.Namespace(path=str(victim), source_type=None),
        )
        assert out["status"] == "error"
        assert "outside allowed roots" in out["error"]

    def test_the_deferred_replay_gets_no_channel_root_and_no_talk_root(
        self, tmp_path, monkeypatch,
    ):
        """The daemon reads these bytes after the task is over.

        There is no conversation token on the replay path and no Talk access,
        and neither is an oversight — the sandboxed task's environment is gone
        by then and the roots come from the config instead. The token is set in
        the environment anyway: a derivation that read it from there rather
        than taking it as an argument would pass every other test in this file
        and fail this one.
        """
        from istota.config import Config
        from istota.scheduler_deferred import _source_path_allowed

        mount = tmp_path / "mount"
        for sub in ("Users/alice", "Users/bob", "Channels/tok1", "Talk"):
            (mount / sub).mkdir(parents=True)
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "tok1")
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        config = Config(nextcloud_mount_path=mount)

        def allowed(p):
            p.write_text("x")
            return _source_path_allowed(p, deferred, config, "alice")

        assert allowed(mount / "Users" / "alice" / "labs.csv")
        assert allowed(deferred / "labs.csv")
        assert not allowed(mount / "Channels" / "tok1" / "labs.csv")
        assert not allowed(mount / "Talk" / "labs.csv")
        assert not allowed(mount / "Users" / "bob" / "labs.csv")

    @pytest.mark.parametrize(
        "user_id", [".", "..", "/etc", "../bob", "a/b", "", " bob", "bob "],
    )
    def test_the_deferred_replay_scopes_the_user_id(self, tmp_path, user_id):
        """ISSUE-402 reaches this caller too, through `Config.workspace_root`."""
        from istota.config import Config
        from istota.scheduler_deferred import _source_path_allowed

        mount = tmp_path / "mount"
        (mount / "Users" / "bob").mkdir(parents=True)
        victim = mount / "Users" / "bob" / "labs.csv"
        victim.write_text("x")
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        config = Config(nextcloud_mount_path=mount)

        assert not _source_path_allowed(victim, deferred, config, user_id)

    def test_outbound_drafts_confines_to_the_users_own_workspace(self, tmp_path):
        """A draft is released hours later and unsandboxed, so the root set is
        the narrow one: the owner's workspace and nothing else."""
        from istota import outbound_drafts
        from istota.config import Config

        mount = tmp_path / "mount"
        for sub in ("Users/alice", "Users/bob", "Channels/tok1", "Talk"):
            (mount / sub).mkdir(parents=True)
        config = Config(nextcloud_mount_path=mount)
        draft = SimpleNamespace(id=1, user_id="alice")

        ok = mount / "Users" / "alice" / "report.pdf"
        ok.write_text("x")
        assert outbound_drafts._confined_attachment(
            config, draft, str(ok),
        ) == ok.resolve()

        for bad in (
            mount / "Users" / "bob" / "private.pdf",
            mount / "Channels" / "tok1" / "shared.pdf",
            mount / "Talk" / "attachment.pdf",
        ):
            bad.write_text("x")
            with pytest.raises(outbound_drafts.DraftError):
                outbound_drafts._confined_attachment(config, draft, str(bad))

    def test_outbound_drafts_still_refuses_a_symlink_outright(self, tmp_path):
        """Its own rule, kept: only containment was delegated."""
        from istota import outbound_drafts
        from istota.config import Config

        mount = tmp_path / "mount"
        (mount / "Users" / "alice").mkdir(parents=True)
        target = mount / "Users" / "alice" / "real.pdf"
        target.write_text("x")
        link = mount / "Users" / "alice" / "link.pdf"
        link.symlink_to(target)
        config = Config(nextcloud_mount_path=mount)

        with pytest.raises(outbound_drafts.DraftError, match="symlink"):
            outbound_drafts._confined_attachment(
                config, SimpleNamespace(id=1, user_id="alice"), str(link),
            )


#: `(name, mount-relative path or None, user_id, token, expected)` — the
#: containment question, asked of every derivation at once.
_EQUIVALENCE_CASES = [
    ("own workspace", "Users/alice/labs.csv", "alice", "tok1", True),
    ("own channel", "Channels/tok1/labs.csv", "alice", "tok1", True),
    ("talk", "Talk/labs.csv", "alice", "tok1", True),
    ("another user", "Users/bob/labs.csv", "alice", "tok1", False),
    ("another channel", "Channels/tok2/labs.csv", "alice", "tok1", False),
    ("the users root itself", "Users/labs.csv", "alice", "tok1", False),
    ("the mount root", "labs.csv", "alice", "tok1", False),
    ("a dot user id", "Users/bob/labs.csv", ".", "tok1", False),
    ("a dotdot user id", "Users/bob/labs.csv", "..", "tok1", False),
    ("an absolute user id", "Users/bob/labs.csv", "/etc", "tok1", False),
    ("a nested user id", "Users/bob/labs.csv", "a/b", "tok1", False),
    ("an empty user id", "Users/alice/labs.csv", "", "tok1", False),
    ("a traversing token", "Channels/tok1/labs.csv", "alice", "../..", False),
    ("a nested token", "Channels/tok1/labs.csv", "alice", "a/b", False),
    ("outside the mount", None, "alice", "tok1", False),
]


class TestTheConsolidationNeverWidens:
    """The equivalence table, against verbatim copies of the four old bodies.

    The four did not agree with each other, so blanket equivalence is not the
    claim and would be false. Two claims are made instead, and together they
    are what a refactor of a boundary has to establish: the shared rule gives
    the expected answer case by case, and it **accepts nothing the widest of
    the four accepted nothing of** — `allowed_host_roots`, which is the
    ceiling because it is the only one of the four with all four roots.
    """

    @pytest.fixture
    def bed(self, tmp_path):
        mount = tmp_path / "mount"
        for sub in ("Users/alice", "Users/bob", "Channels/tok1",
                    "Channels/tok2", "Talk"):
            (mount / sub).mkdir(parents=True)
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        return mount, deferred

    @staticmethod
    def _target(bed, tmp_path, relative):
        mount, _deferred = bed
        target = (mount / relative) if relative else (tmp_path / "outside.csv")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
        return target

    @pytest.mark.parametrize(
        "name,relative,user_id,token,expected",
        _EQUIVALENCE_CASES,
        ids=[c[0].replace(" ", "_") for c in _EQUIVALENCE_CASES],
    )
    def test_the_table(self, bed, tmp_path, name, relative, user_id, token, expected):
        from istota.skill_host_paths import path_under_roots, workspace_roots

        mount, deferred = bed
        target = self._target(bed, tmp_path, relative)
        roots = workspace_roots(
            mount=mount, user_id=user_id, deferred_dir=deferred,
            conversation_token=token, talk=True,
        )
        got = path_under_roots(target.resolve(), roots)
        assert got is expected, f"{name}: {target} against {roots}"

        if got:
            assert _legacy_allows(
                _legacy_env_roots, target, mount, deferred, user_id, token,
            ), f"{name}: wider than the widest of the four old copies"

    def test_the_deferred_replay_derivation_matches_its_old_copy(self, bed):
        """Except on the ISSUE-402 ids, where it is narrower and says so."""
        from istota.skill_host_paths import workspace_roots

        mount, deferred = bed
        assert workspace_roots(
            mount=mount, user_id="alice", deferred_dir=deferred,
        ) == _legacy_deferred_roots(mount, deferred, "alice", "tok1")
        for bad in (".", "..", "/etc", "a/b"):
            new = workspace_roots(mount=mount, user_id=bad, deferred_dir=deferred)
            old = _legacy_deferred_roots(mount, deferred, bad, "tok1")
            assert set(new) < set(old), bad

    def test_the_draft_derivation_matches_its_old_copy(self, bed):
        """Against `Config.workspace_root`, which is what the draft path uses.

        `_confined_attachment` does not go through `workspace_roots` at all —
        it takes `config.workspace_root(draft.user_id)` as its single root —
        so comparing `workspace_roots` here would be a claim about a function
        that is not on this path, and would stay green through a divergence
        between the two derivations.
        """
        from istota.config import Config

        mount, _deferred = bed
        config = Config(nextcloud_mount_path=mount)
        assert [Path(config.workspace_root("alice")).resolve()] == (
            _legacy_draft_roots(mount, None, "alice", "tok1")
        )
        # And strictly narrower on every id the old copy joined blindly.
        for bad in (" alice", "alice ", ".", "..", "/etc", "a/b"):
            assert config.workspace_root(bad) is None, bad
            assert _legacy_draft_roots(mount, None, bad, "tok1") != []

    def test_the_index_file_derivation_matches_its_old_copy(
        self, mount, monkeypatch, tmp_path,
    ):
        """Equal for an ordinary token, narrower for a traversing one — which
        is the guard `_indexable_roots` lacked."""
        from istota.skill_host_paths import env_host_roots

        deferred = tmp_path / "deferred"
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "tok1")
        assert set(env_host_roots(talk=False)) == set(
            _legacy_indexable_roots(mount, deferred, "alice", "tok1"),
        )
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "../..")
        assert set(env_host_roots(talk=False)) < set(
            _legacy_indexable_roots(mount, deferred, "alice", "../.."),
        )

    def test_a_symlink_out_of_the_workspace_is_caught_by_resolving_first(self, bed):
        """The one case containment alone does not answer — every caller
        resolves before asking, which is why `path_under_roots` need not."""
        from istota.skill_host_paths import path_under_roots, workspace_roots

        mount, deferred = bed
        secret = mount / "Users" / "bob" / "secret.csv"
        secret.write_text("x")
        link = mount / "Users" / "alice" / "link.csv"
        link.symlink_to(secret)
        roots = workspace_roots(
            mount=mount, user_id="alice", deferred_dir=deferred, talk=True,
        )
        assert path_under_roots(link, roots)
        assert not path_under_roots(link.resolve(), roots)


class TestResolutionNeverMutatesTheFilesystem:
    """`resolve_host_path` used to `mkdir` the destination's parent.

    Answering "may this path be used" is not a licence to create a directory,
    and once resolution moves to parse time that side effect would fire before
    dispatch on every write argument — so a verb refusing for an unrelated
    reason would leave a tree behind on every invocation. `write_resolved`
    ensures the parent instead, where the write happens.
    """

    def test_a_writable_resolution_creates_nothing(self, mount):
        dest = mount / "Users" / "alice" / "new" / "deep" / "out.csv"
        resolved, err = resolve_host_path(dest, writable=True, operation="op")
        assert err is None
        assert resolved == (
            (mount / "Users" / "alice").resolve() / "new" / "deep" / "out.csv"
        )
        assert not (mount / "Users" / "alice" / "new").exists()

    def test_write_resolved_makes_the_parent(self, mount):
        dest = mount / "Users" / "alice" / "new" / "deep" / "out.csv"
        resolved, err = resolve_host_path(dest, writable=True, operation="op")
        assert err is None
        write_resolved(resolved, b"bytes")
        assert resolved.read_bytes() == b"bytes"

    def test_a_refused_destination_still_creates_nothing(self, mount, tmp_path):
        dest = tmp_path / "attacker" / "deep" / "out.csv"
        _, err = resolve_host_path(dest, writable=True, operation="op")
        assert err is not None
        assert not (tmp_path / "attacker").exists()


class TestTheRefusalDoesNotEnumerateTheRoots:
    """Naming them tells a model that has just tried to read another user's
    directory what the other directories are called."""

    def test_the_message_names_the_path_and_not_the_roots(
        self, mount, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "tok1")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        victim = elsewhere / "secret.txt"
        victim.write_text("x")
        _, err = resolve_host_path(victim, writable=False, operation="op")
        assert err is not None
        assert "outside allowed roots" in err
        assert str(mount) not in err
        assert "alice" not in err
        assert "tok1" not in err
