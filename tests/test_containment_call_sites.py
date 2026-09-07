"""Every site that now calls ``user_scope`` still refuses what it refused (F18).

Nine hand-rolled containment checks were replaced by a call to
:mod:`istota.user_scope`. The claim each conversion makes is that the site's
rule did not change, and the only way to hold that claim is to ask each site
the hostile questions ``tests/test_user_dir_containment.py`` asks the rule
itself: the four ids that are not a child, the absolute one, and a planted
symlink — which is the case the *lexical* half misses and the one the model
can actually create, since ``developer.repos_dir`` and the cache root were both
bound read-write for as long as the shared layout stood.

**These assertions are written to be able to fail**, which for a containment
test is not automatic. Round 1 of this spec measured that every stage contained
at least one test that could not, and a consolidation is the worst case for it:
delete one of two implementations and the survivor can be left covered by a
test that only ever exercised the deleted one. So each class here asserts the
refusal *and* the acceptance — an ordinary id still gets its directory — because
a rule that refuses everything satisfies half of this file and is an outage.

The one site deliberately **not** converted has its own class at the bottom,
and that class is the evidence for the decision rather than a note about it.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from istota import repos_relocate, sandbox_cache_sweeper as sweeper
from istota import git_remote_scrub, skill_host_paths, worktree_reaper
from istota.executor import _daemon_dirs, get_task_control_dir
from istota.skills.developer import _user_repos_dir
from istota.user_scope import is_within, paths_overlap, scoped_user_dir

#: The ids that never name a child of a root. `""` and `"."` are dropped by
#: `PurePath`, `".."` is a child by name and the parent on disk, `"a/b"` goes
#: deeper than the layout describes, and an absolute component replaces the
#: root outright.
BAD_IDS = ["", ".", "..", "a/b", "/etc"]

#: Refused since the rule moved into the leaf, and refused in the safe
#: direction at every site: `skill_host_paths` reads `ISTOTA_USER_ID` through
#: `.strip()`, so a padded id names one directory to a daemon-side join and
#: another to the host-side allowlist.
PADDED_IDS = [" alice", "alice ", "\talice"]


def _elsewhere(tmp_path: Path) -> Path:
    other = tmp_path / "elsewhere"
    other.mkdir()
    return other


class TestTheTwoNewIdioms:
    """`is_within` and `paths_overlap` before any call site."""

    def test_a_child_is_within_its_root(self, tmp_path):
        assert is_within(tmp_path / "a" / "b", tmp_path)

    def test_the_root_itself_is_within_it(self, tmp_path):
        """`Path.is_relative_to` already answers True for equal paths, which is
        why four converted sites carried a redundant `a == b` term."""
        assert is_within(tmp_path, tmp_path)

    def test_a_sibling_is_not(self, tmp_path):
        assert not is_within(tmp_path.parent / "other", tmp_path)

    def test_it_is_lexical_and_says_so(self, tmp_path):
        """`{root}/..` is within `root` by spelling and is its parent on disk.

        Asserted rather than left implicit: every caller resolves before
        asking, and a future one that forgets has to be able to find out here
        that this function will not catch it.
        """
        assert is_within(tmp_path / "..", tmp_path)
        assert not is_within((tmp_path / "..").resolve(), tmp_path)

    @pytest.mark.parametrize("junk", [None, 3, object()])
    def test_it_never_raises(self, tmp_path, junk):
        assert is_within(junk, tmp_path) is False
        assert is_within(tmp_path, junk) is False

    def test_overlap_answers_in_both_directions(self, tmp_path):
        inner = tmp_path / "a" / "b"
        assert paths_overlap(inner, tmp_path)
        assert paths_overlap(tmp_path, inner)
        assert paths_overlap(tmp_path, tmp_path)

    def test_overlap_is_false_for_two_unrelated_paths(self, tmp_path):
        assert not paths_overlap(tmp_path / "a", tmp_path / "b")


class TestTheCacheSweeperEnumeratedLayout:
    """`_candidates_in_root`: the user id is read back out of the tree."""

    def test_a_symlinked_entry_is_reported_and_not_swept(self, tmp_path):
        root = tmp_path / "caches"
        root.mkdir()
        (root / "alice").mkdir()
        (root / "zzz").symlink_to(root / "alice", target_is_directory=True)

        found = {name: ok for name, _path, ok in sweeper._candidates_in_root(root)}

        assert found["zzz"] is False, (
            "a link named for one user and resolving to another's cache: the "
            "busy check would be asked about zzz while the reclaim ran on alice"
        )
        assert found["alice"] is True

    def test_a_link_out_of_the_root_is_reported(self, tmp_path):
        root = tmp_path / "caches"
        root.mkdir()
        (root / "bob").symlink_to(_elsewhere(tmp_path), target_is_directory=True)

        found = {name: ok for name, _path, ok in sweeper._candidates_in_root(root)}

        assert found == {"bob": False}

    def test_a_padded_directory_name_is_reported_rather_than_swept(self, tmp_path):
        """The one tightening the conversion brought to this function.

        A directory literally called `" alice"` used to resolve to its own name
        and be swept as that user's cache. It is now reported instead, which is
        the fail-safe answer: nothing in the framework creates such a name, and
        the busy check would be asked about a user id no task carries.
        """
        root = tmp_path / "caches"
        root.mkdir()
        (root / " alice").mkdir()

        found = {name: ok for name, _path, ok in sweeper._candidates_in_root(root)}

        assert found == {" alice": False}


class TestTheCacheSweeperDerivedLayout:
    """`_candidates_for_users`: two levels, both of them model-plantable."""

    def _repos(self, tmp_path):
        root = tmp_path / "repos"
        (root / "alice" / sweeper.CACHE_ROOT_NAME).mkdir(parents=True)
        return root

    def test_an_ordinary_user_still_gets_their_cache(self, tmp_path):
        root = self._repos(tmp_path)
        found = list(sweeper._candidates_for_users(root, ["alice"]))
        assert [(u, ok) for u, _p, ok in found] == [("alice", True)]

    @pytest.mark.parametrize("user_id", BAD_IDS + PADDED_IDS)
    def test_an_id_that_is_not_a_child_is_refused(self, tmp_path, user_id):
        root = self._repos(tmp_path)
        found = [(u, ok) for u, _p, ok in sweeper._candidates_for_users(root, [user_id])]
        assert found in ([], [(user_id, False)]), user_id
        assert not any(ok for _u, ok in found)

    def test_a_symlink_at_the_user_subtree_is_refused(self, tmp_path):
        """The first of the two levels. A link at `{repos}/bob` aimed into
        alice's subtree resolves to a real cache directory, so the *lexical*
        half says yes and only the equality refuses it."""
        root = self._repos(tmp_path)
        (root / "bob").symlink_to(root / "alice", target_is_directory=True)

        found = [(u, ok) for u, _p, ok in sweeper._candidates_for_users(root, ["bob"])]

        assert found == [("bob", False)]

    def test_a_symlink_at_the_cache_name_is_refused(self, tmp_path):
        """The second level, which a single check on the user id would miss."""
        root = self._repos(tmp_path)
        (root / "carol").mkdir()
        (root / "carol" / sweeper.CACHE_ROOT_NAME).symlink_to(
            _elsewhere(tmp_path), target_is_directory=True,
        )

        found = [(u, ok) for u, _p, ok in sweeper._candidates_for_users(root, ["carol"])]

        assert found == [("carol", False)]

    def test_a_user_with_no_cache_yet_is_still_skipped_in_silence(self, tmp_path):
        """The ordering the conversion had to preserve.

        Both `scoped_user_dir` calls run *after* the `is_dir` check, in the
        position the single equality held. Moving them earlier would turn every
        user who has not run a task into an outcome row.
        """
        root = self._repos(tmp_path)
        (root / "dave").mkdir()

        assert list(sweeper._candidates_for_users(root, ["dave"])) == []


class TestTheDeveloperReposSubtree:
    """The `setup_env` hook that creates and chmods `{repos_dir}/{user_id}`."""

    def _ctx(self, user_id):
        return types.SimpleNamespace(
            is_admin=True, task=types.SimpleNamespace(user_id=user_id),
        )

    def _dev(self, root):
        return types.SimpleNamespace(repos_dir=str(root))

    def test_an_ordinary_id_gets_its_subtree(self, tmp_path):
        root = tmp_path / "repos"
        root.mkdir()
        assert _user_repos_dir(self._dev(root), self._ctx("alice")) == root / "alice"

    @pytest.mark.parametrize("user_id", BAD_IDS + PADDED_IDS)
    def test_an_id_that_is_not_a_child_creates_nothing(self, tmp_path, user_id):
        root = tmp_path / "repos"
        root.mkdir()

        assert _user_repos_dir(self._dev(root), self._ctx(user_id)) is None
        assert list(root.iterdir()) == [], (
            "the hook refused and still created a directory — the refusal has "
            "to happen before the mkdir, not after it"
        )

    def test_a_planted_symlink_is_not_chmodded_or_scrubbed(self, tmp_path):
        """The case the lexical half cannot see, and the one that was real:
        every deployment running the old shared bind gave a task read-write
        access to this root, so a link left at `{repos_dir}/{user_id}` predates
        the per-user layout."""
        root = tmp_path / "repos"
        root.mkdir()
        other = _elsewhere(tmp_path)
        (root / "alice").symlink_to(other, target_is_directory=True)

        assert _user_repos_dir(self._dev(root), self._ctx("alice")) is None


class TestTheDaemonWorkDir:
    """`_daemon_dirs`, whose fallback value is also its refusal signal."""

    def _config(self, tmp_path):
        return types.SimpleNamespace(temp_dir=tmp_path / "temp")

    def test_an_ordinary_id_gets_its_own_directory(self, tmp_path):
        config = self._config(tmp_path)
        root, work = _daemon_dirs(config, "alice")
        assert work != root
        assert work == root / "alice"

    @pytest.mark.parametrize("user_id", BAD_IDS + PADDED_IDS)
    def test_an_id_that_is_not_a_child_falls_back_to_the_shared_root(
        self, tmp_path, user_id,
    ):
        """Equal to the root is what `build_daemon_sandbox` reads as `refused`,
        so a tool-granting caller declines to run rather than binding every
        user's scratch space into one namespace."""
        config = self._config(tmp_path)
        root, work = _daemon_dirs(config, user_id)
        assert work == root

    def test_a_symlinked_user_directory_falls_back(self, tmp_path):
        config = self._config(tmp_path)
        temp = tmp_path / "temp"
        temp.mkdir()
        (temp / "alice").symlink_to(_elsewhere(tmp_path), target_is_directory=True)

        root, work = _daemon_dirs(config, "alice")

        assert work == root

    def test_a_non_string_id_does_not_raise(self, tmp_path):
        """It used to reach `root / user_id` and raise `TypeError` out of a
        function two callers treat as never-raising."""
        root, work = _daemon_dirs(self._config(tmp_path), 7)
        assert work == root


class TestTheRelocateCloneName:
    """`repos_relocate._contained`, asked about a directory name off the tree."""

    def test_an_ordinary_name_is_contained(self, tmp_path):
        (tmp_path / "istota.git").mkdir()
        assert repos_relocate._contained(tmp_path, "istota.git")

    @pytest.mark.parametrize("name", BAD_IDS + PADDED_IDS)
    def test_a_name_that_is_not_a_child_is_refused(self, tmp_path, name):
        assert not repos_relocate._contained(tmp_path, name)

    def test_a_symlinked_entry_is_refused(self, tmp_path):
        (tmp_path / "linked").symlink_to(_elsewhere(tmp_path), target_is_directory=True)
        assert not repos_relocate._contained(tmp_path, "linked")

    def test_the_at_or_under_test_resolves_both_sides(self, tmp_path):
        """`_under` compares realpaths, so a record naming a path through a
        symlinked ancestor is recognised as the same tree."""
        real = tmp_path / "real"
        (real / "clone").mkdir(parents=True)
        (tmp_path / "link").symlink_to(real, target_is_directory=True)

        assert repos_relocate._under(tmp_path / "link" / "clone", real)
        assert not repos_relocate._under(_elsewhere(tmp_path), real)


class TestTheWorktreeRecordRoot:
    """`worktree_reaper._is_within`, which gates a delete."""

    def test_a_record_inside_the_root_is_within_it(self, tmp_path):
        (tmp_path / "wt").mkdir()
        assert worktree_reaper._is_within(tmp_path / "wt", tmp_path)

    def test_a_record_naming_the_parent_is_not(self, tmp_path):
        """The lexical trap, closed here by resolving before asking."""
        assert not worktree_reaper._is_within(tmp_path / "..", tmp_path)

    def test_a_symlinked_record_pointing_out_is_not(self, tmp_path):
        root = tmp_path / "repos"
        root.mkdir()
        (root / "wt").symlink_to(_elsewhere(tmp_path), target_is_directory=True)

        assert not worktree_reaper._is_within(root / "wt", root)


class TestTheScrubRewriteTarget:
    """`git_remote_scrub._writable`: detection may range outside the root,
    correction may not."""

    def test_a_config_inside_the_root_is_writable(self, tmp_path):
        (tmp_path / "repo").mkdir()
        (tmp_path / "repo" / "config").touch()
        assert git_remote_scrub._writable(tmp_path / "repo" / "config", tmp_path)

    def test_an_included_path_outside_the_root_is_not(self, tmp_path):
        root = tmp_path / "repos"
        root.mkdir()
        outside = tmp_path / "gitconfig"
        outside.touch()
        assert not git_remote_scrub._writable(outside, root)

    def test_a_traversal_out_of_the_root_is_not(self, tmp_path):
        root = tmp_path / "repos"
        root.mkdir()
        (tmp_path / "gitconfig").touch()
        assert not git_remote_scrub._writable(root / ".." / "gitconfig", root)


class TestTheHostPathAllowlistPredicate:
    """`skill_host_paths.path_under_roots` — the root *set* stays the
    caller's, which is the whole shape of ISSUE-447."""

    def test_a_path_under_one_of_the_roots_passes(self, tmp_path):
        assert skill_host_paths.path_under_roots(tmp_path / "a" / "f", [tmp_path])

    def test_an_empty_root_set_is_false_never_true(self, tmp_path):
        assert not skill_host_paths.path_under_roots(tmp_path, [])

    def test_a_path_under_none_of_them_is_refused(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        assert not skill_host_paths.path_under_roots(tmp_path / "c" / "f", [a, b])

    def test_two_callers_with_different_root_sets_still_get_different_answers(
        self, tmp_path,
    ):
        """The property a generic sweep would have destroyed: one predicate
        over two root sets is not the same thing as one root set."""
        workspace, talk = tmp_path / "ws", tmp_path / "talk"
        assert skill_host_paths.path_under_roots(talk / "f", [workspace, talk])
        assert not skill_host_paths.path_under_roots(talk / "f", [workspace])


class TestTheOverlapCallers:
    """`paths_overlap` where a bind above the tree and one inside it are both
    unacceptable."""

    def test_the_doctor_predicate_answers_both_directions(self, tmp_path):
        from istota.doctor import _overlaps

        control = tmp_path / "temp" / ".control"
        assert _overlaps(control, tmp_path / "temp")
        assert _overlaps(tmp_path / "temp", control)
        assert _overlaps(control, control)
        assert not _overlaps(control, tmp_path / "other")


class TestTheControlDirIsNotConverted:
    """Why `get_task_control_dir` keeps its own copy of the equality.

    This is the stage's one "reported rather than silently tightened" finding,
    inverted: the site is not looser than the shared rule, the shared rule is
    looser than the site. `scoped_user_dir` compares against
    ``root.resolve() / user_id``, so a symlink planted at ``.control`` moves
    both sides of its equality and the check passes. The site compares against
    ``root / user_id`` as spelled, and refuses.

    Asserted on both functions in one test on purpose. A comment saying "do not
    convert this" is what the audit found ten of, two of them already stale;
    an executable statement of the difference is what makes a future conversion
    go red instead of green.
    """

    def _config(self, tmp_path):
        return types.SimpleNamespace(temp_dir=tmp_path / "temp")

    def test_the_site_refuses_a_symlinked_control_root(self, tmp_path):
        temp = tmp_path / "temp"
        temp.mkdir()
        (temp / ".control").symlink_to(_elsewhere(tmp_path), target_is_directory=True)

        assert get_task_control_dir(self._config(tmp_path), "alice", 1) is None

    def test_the_shared_rule_would_have_accepted_it(self, tmp_path):
        temp = tmp_path / "temp"
        temp.mkdir()
        elsewhere = _elsewhere(tmp_path)
        (temp / ".control").symlink_to(elsewhere, target_is_directory=True)

        # Exactly the call the conversion would have made, at the same root.
        assert scoped_user_dir(temp / ".control", "alice") == temp / ".control" / "alice"

    def test_the_site_still_accepts_an_ordinary_id(self, tmp_path):
        got = get_task_control_dir(self._config(tmp_path), "alice", 1)
        assert got == (tmp_path / "temp").resolve() / ".control" / "alice" / "task_1"

    @pytest.mark.parametrize("user_id", BAD_IDS)
    def test_the_site_still_refuses_the_hostile_ids(self, tmp_path, user_id):
        assert get_task_control_dir(self._config(tmp_path), user_id, 1) is None
