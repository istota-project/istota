"""The one-shot migrator that splits `developer.repos_dir` into per-user subtrees.

Every repository here is a real one — `git init --bare`, a real `git worktree
add`, a real commit. A fixture that fakes the administrative directory would
assert against paths this module writes and prove nothing about the one call
the whole migration rests on, which is `git worktree repair`.

That call is load-bearing in a way that is easy to get wrong, and the shape was
measured before the module was written rather than assumed: when a bare clone
*and* its worktrees move together, `git worktree repair` **with no arguments**
succeeds, prints nothing, and fixes nothing — the repository's record of where
the worktree lives is stale, so git has nowhere to look. Only `git worktree
repair <new path>` reestablishes both halves of the link. `TestTheRepair`
carries that as an explicit control: the same wholesale rename done by hand,
without the migrator, leaves the worktree broken.

The other half of the file is refusals. Ownership cannot be read off the disk —
a forge namespace is not a user id — so the migrator infers it from exactly one
configured admin and refuses everything else. Guessing wrong hands one admin's
clones to another, which is the exact exposure the per-user layout exists to
close, so the refusal cases get as much attention as the happy path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from istota.repos_relocate import (
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_REFUSED,
    LAYOUT_VERSION,
    MARKER_NAME,
    RelocateRefusal,
    apply,
    main,
    plan,
)

GIT_ISOLATION = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
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


def _git_status(cwd: Path, *args: str) -> int:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd), capture_output=True, text=True,
        env={**os.environ, **GIT_ISOLATION},
    )
    return proc.returncode


def _upstream(tmp_path: Path, name: str) -> Path:
    """A real repository with one commit on `main`, to clone from."""
    up = tmp_path / "upstreams" / name
    up.mkdir(parents=True)
    _git(up, "init", "-q", "-b", "main", ".")
    (up / "README").write_text("base\n")
    _git(up, "add", "README")
    _git(up, "commit", "-q", "-m", "init")
    return up


def _clone(repos_dir: Path, namespace: str, project: str, upstream: Path) -> Path:
    """A bare clone in the old shared layout: `{repos_dir}/{namespace}/{project}.git`."""
    bare = repos_dir / namespace / f"{project}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(repos_dir, "clone", "-q", "--bare", str(upstream), str(bare))
    return bare


def _worktree(bare: Path, name: str) -> Path:
    """A worktree beside its bare clone, the way the developer skill cuts one."""
    path = bare.parent / name
    _git(bare, "worktree", "add", "-q", "-b", name, str(path), "main")
    return path


def _toplevel(worktree: Path) -> str:
    return _git(worktree, "rev-parse", "--show-toplevel").strip()


def _resolves(worktree: Path) -> bool:
    return _git_status(worktree, "rev-parse", "--show-toplevel") == 0


@pytest.fixture
def tree(tmp_path):
    """The old layout: two namespaces, three clones, two live worktrees.

    `acme/widget.git` has a worktree, `acme/gadget.git` has none, and
    `other-org/thing.git` is in a second namespace — so the migration has to
    move more than one directory and repair only where there is something to
    repair.
    """
    repos_dir = tmp_path / "repos"
    repos_dir.mkdir()
    widget = _clone(repos_dir, "acme", "widget", _upstream(tmp_path, "widget"))
    gadget = _clone(repos_dir, "acme", "gadget", _upstream(tmp_path, "gadget"))
    thing = _clone(repos_dir, "other-org", "thing", _upstream(tmp_path, "thing"))
    wt_widget = _worktree(widget, "widget--istota-42-add-auth")
    wt_thing = _worktree(thing, "thing--istota-7-fix")
    return {
        "repos_dir": repos_dir,
        "widget": widget,
        "gadget": gadget,
        "thing": thing,
        "worktrees": [wt_widget, wt_thing],
    }


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    """A real `config.toml` and a real admins file, wired as Ansible wires them.

    `main` reads both through the ordinary loaders — `ISTOTA_CONFIG_PATH` and
    `ISTOTA_ADMINS_FILE` — rather than through anything this test injects, so
    the CLI cases exercise the same path the deploy does.
    """

    class Deployment:
        def __init__(self):
            self.repos_dir = tmp_path / "repos"
            self.db_path = tmp_path / "data" / "istota.db"
            self.config_path = tmp_path / "config.toml"
            self.admins_path = tmp_path / "admins"

        def write(self, admins=("alice",), repos_dir=None):
            self.admins_path.write_text("".join(f"{a}\n" for a in admins))
            root = self.repos_dir if repos_dir is None else repos_dir
            self.config_path.write_text(
                f'db_path = "{self.db_path}"\n'
                f"\n[developer]\n"
                f"enabled = true\n"
                f'repos_dir = "{root}"\n'
            )

        def with_live_task(self, user_id: str, status: str = "running"):
            from istota import db

            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            db.init_db(self.db_path)
            with db.get_db(self.db_path) as conn:
                task_id = db.create_task(conn, prompt="x", user_id=user_id)
                conn.execute(
                    "UPDATE tasks SET status = ? WHERE id = ?", (status, task_id)
                )
                conn.commit()

    dep = Deployment()
    monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(dep.config_path))
    monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(dep.admins_path))
    return dep


def _snapshot(root: Path) -> set[str]:
    """Every path under `root`, relative and sorted — for "nothing changed"."""
    if not root.exists():
        return set()
    out = {"."}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in list(dirnames) + filenames:
            out.add(str(Path(dirpath, name).relative_to(root)))
    return out


# ---------------------------------------------------------------------------
# The move
# ---------------------------------------------------------------------------


class TestTheMove:
    """One admin, two namespaces: everything lands under that user's subtree."""

    def test_every_namespace_moves_under_the_admin(self, tree):
        repos_dir = tree["repos_dir"]

        report = apply(plan(repos_dir, {"alice"}))

        assert sorted(report.moved) == ["acme", "other-org"]
        assert (repos_dir / "alice" / "acme" / "widget.git").is_dir()
        assert (repos_dir / "alice" / "acme" / "gadget.git").is_dir()
        assert (repos_dir / "alice" / "other-org" / "thing.git").is_dir()
        assert not (repos_dir / "acme").exists()
        assert not (repos_dir / "other-org").exists()

    def test_the_user_subtree_is_created_0700(self, tree):
        """`setup_env` creates it at 0700 on every task; a migrator that left it
        world-readable would hand the next `chmod` a directory other users had
        already been able to read."""
        repos_dir = tree["repos_dir"]

        apply(plan(repos_dir, {"alice"}))

        assert (repos_dir / "alice").stat().st_mode & 0o777 == 0o700

    def test_the_marker_records_the_layout_version(self, tree):
        repos_dir = tree["repos_dir"]

        report = apply(plan(repos_dir, {"alice"}))

        assert report.marker_written
        assert (repos_dir / MARKER_NAME).read_text().strip() == LAYOUT_VERSION

    def test_the_bare_clone_still_works_after_the_move(self, tree):
        repos_dir = tree["repos_dir"]

        apply(plan(repos_dir, {"alice"}))

        moved = repos_dir / "alice" / "acme" / "widget.git"
        assert _git(moved, "rev-parse", "--is-bare-repository").strip() == "true"
        assert _git(moved, "log", "--oneline", "-1", "main").strip()


# ---------------------------------------------------------------------------
# The repair
# ---------------------------------------------------------------------------


class TestTheRepair:
    """`git worktree repair`, with the new paths, is what keeps a live worktree
    working across the rename. Both halves of the link are absolute paths git
    stores on disk, and a wholesale namespace rename breaks both at once."""

    def test_the_control_a_hand_rename_breaks_the_worktree(self, tree):
        """The negative control. Without this the repair assertions below could
        pass on a migration that never repaired anything."""
        repos_dir = tree["repos_dir"]
        (repos_dir / "alice").mkdir()

        os.rename(repos_dir / "acme", repos_dir / "alice" / "acme")

        moved = repos_dir / "alice" / "acme" / "widget--istota-42-add-auth"
        assert moved.is_dir()
        assert not _resolves(moved), (
            "fixture is wrong: the rename left the worktree working, so the "
            "repair cases below cannot fail"
        )

    def test_the_worktree_still_resolves_after_the_migration(self, tree):
        repos_dir = tree["repos_dir"]

        report = apply(plan(repos_dir, {"alice"}))

        moved = repos_dir / "alice" / "acme" / "widget--istota-42-add-auth"
        assert _resolves(moved)
        assert Path(_toplevel(moved)).resolve() == moved.resolve()
        assert sorted(Path(p).name for p in report.repaired) == [
            "thing--istota-7-fix",
            "widget--istota-42-add-auth",
        ]

    def test_every_worktree_in_every_namespace_is_repaired(self, tree):
        repos_dir = tree["repos_dir"]

        apply(plan(repos_dir, {"alice"}))

        for old in tree["worktrees"]:
            moved = repos_dir / "alice" / old.relative_to(repos_dir)
            assert _resolves(moved), f"{moved} did not survive the move"

    def test_the_clone_lists_the_worktree_at_its_new_path(self, tree):
        """The other direction of the link: the repository's own record. A
        `.git` file repaired without it leaves `git worktree list` naming a
        path that is gone, and the reaper reads that listing."""
        repos_dir = tree["repos_dir"]

        apply(plan(repos_dir, {"alice"}))

        moved_clone = repos_dir / "alice" / "acme" / "widget.git"
        listing = _git(moved_clone, "worktree", "list")
        assert str(repos_dir / "alice" / "acme" / "widget--istota-42-add-auth") in listing
        assert str(repos_dir / "acme") not in listing

    def test_the_worktree_can_still_commit(self, tree):
        """Resolving is not the same as working: the index and HEAD live in the
        administrative directory the repair re-pointed at."""
        repos_dir = tree["repos_dir"]

        apply(plan(repos_dir, {"alice"}))

        moved = repos_dir / "alice" / "acme" / "widget--istota-42-add-auth"
        (moved / "NEW").write_text("after\n")
        _git(moved, "add", "NEW")
        _git(moved, "commit", "-q", "-m", "after the move")
        assert "after the move" in _git(moved, "log", "--oneline", "-1")

    def test_a_worktree_git_cannot_repair_is_reported_by_path(self, tree):
        """The spec's edge case: report it and carry on with the rest. A
        partial repair leaves a recoverable tree and stopping halfway leaves a
        worse one, so the other namespace still gets its repair."""
        repos_dir = tree["repos_dir"]
        (repos_dir / "acme" / "widget--istota-42-add-auth" / ".git").unlink()

        report = apply(plan(repos_dir, {"alice"}))

        broken = repos_dir / "alice" / "acme" / "widget--istota-42-add-auth"
        assert report.unrepaired == (str(broken),)
        assert report.repaired == (
            str(repos_dir / "alice" / "other-org" / "thing--istota-7-fix"),
        )
        assert _resolves(repos_dir / "alice" / "other-org" / "thing--istota-7-fix")
        assert report.moved == ("acme", "other-org")

    def test_an_unrepairable_worktree_exits_partial(self, tree, deployment):
        deployment.write()
        (tree["repos_dir"] / "acme" / "widget--istota-42-add-auth" / ".git").unlink()

        assert main([]) == EXIT_PARTIAL

    def test_an_unrepairable_worktree_still_writes_the_marker(self, tree):
        """The layout did move, so a re-run has nothing left to do. Making the
        marker conditional on the repairs would leave every later run trying to
        move namespaces that are no longer there."""
        repos_dir = tree["repos_dir"]
        (repos_dir / "acme" / "widget--istota-42-add-auth" / ".git").unlink()

        report = apply(plan(repos_dir, {"alice"}))

        assert report.marker_written

    def test_a_worktree_record_pointing_nowhere_is_a_note_not_a_failure(self, tree):
        """A checkout deleted by hand leaves its record behind until someone
        runs `git worktree prune`. That is ordinary git litter, not a migration
        failure, so it must not fail the deploy."""
        repos_dir = tree["repos_dir"]
        shutil.rmtree(repos_dir / "acme" / "widget--istota-42-add-auth")

        report = apply(plan(repos_dir, {"alice"}))

        assert report.unrepaired == ()
        assert any("widget--istota-42-add-auth" in n for n in report.notes)
        assert report.marker_written


class TestHardening:
    """`repos_dir` is bound read-write into the sandbox, so every `.git/config`
    under it is model-written. Repository config is covered by neither
    `GIT_CONFIG_NOSYSTEM` nor `GIT_CONFIG_GLOBAL`, so every call carries the
    `-c` overrides."""

    @pytest.fixture
    def recorder(self, tmp_path, monkeypatch):
        """A `git` first on PATH that logs its argv and then runs the real one."""
        real = shutil.which("git")
        assert real, "git is not installed"
        bindir = tmp_path / "bin"
        bindir.mkdir()
        log = tmp_path / "git-argv.log"
        shim = bindir / "git"
        shim.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> {log}\n'
            f'exec {real} "$@"\n'
        )
        shim.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

        def read():
            if not log.exists():
                return []
            return [ln for ln in log.read_text().splitlines() if ln.strip()]

        return read

    def test_every_git_call_carries_the_hardening_overrides(self, tree, recorder):
        repos_dir = tree["repos_dir"]

        apply(plan(repos_dir, {"alice"}))

        calls = recorder()
        assert calls, "the migrator ran no git commands at all"
        for call in calls:
            assert "-c core.fsmonitor=" in call, call
            assert "-c core.hooksPath=/dev/null" in call, call

    def test_one_repair_call_per_bare_clone_with_worktrees(self, tree, recorder):
        """Two clones have worktrees and one does not, so the repair runs twice
        and passes the new paths — a no-argument `repair` fixes nothing when the
        repository moved too."""
        repos_dir = tree["repos_dir"]

        apply(plan(repos_dir, {"alice"}))

        repairs = [c for c in recorder() if "worktree repair" in c]
        assert len(repairs) == 2, repairs
        for call in repairs:
            assert str(repos_dir / "alice") in call


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


class TestOwnership:
    """Nothing on disk says who owns a clone. Exactly one admin is the only
    configuration the destination can be inferred from."""

    def test_zero_admins_refuses(self, tree):
        outcome = plan(tree["repos_dir"], set())

        assert isinstance(outcome, RelocateRefusal)
        assert outcome.reason == "no_admins"

    def test_two_admins_refuses(self, tree):
        outcome = plan(tree["repos_dir"], {"alice", "bob"})

        assert isinstance(outcome, RelocateRefusal)
        assert outcome.reason == "many_admins"

    def test_the_refusal_names_the_namespaces_it_could_not_place(self, tree):
        outcome = plan(tree["repos_dir"], {"alice", "bob"})

        details = "\n".join(outcome.details)
        assert "acme" in details
        assert "other-org" in details

    def test_the_refusal_names_the_admins_it_found(self, tree):
        outcome = plan(tree["repos_dir"], {"alice", "bob"})

        details = "\n".join(outcome.details)
        assert "alice" in details and "bob" in details

    @pytest.mark.parametrize("admins", [set(), {"alice", "bob"}])
    def test_a_refused_run_touches_nothing(self, tree, deployment, admins):
        deployment.write(admins=sorted(admins))
        before = _snapshot(tree["repos_dir"])

        assert main([]) == EXIT_REFUSED

        assert _snapshot(tree["repos_dir"]) == before
        assert not (tree["repos_dir"] / MARKER_NAME).exists()


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


class TestLiveTaskGuard:
    """Moving a clone out from under a task that is using it destroys the task."""

    @pytest.mark.parametrize("status", ["running", "locked"])
    def test_a_live_task_refuses(self, tree, deployment, status):
        deployment.write()
        deployment.with_live_task("alice", status=status)
        before = _snapshot(tree["repos_dir"])

        assert main([]) == EXIT_REFUSED

        assert _snapshot(tree["repos_dir"]) == before

    def test_the_refusal_names_the_user(self, tree, deployment, capsys):
        deployment.write()
        deployment.with_live_task("alice")

        main([])

        assert "alice" in capsys.readouterr().err

    def test_a_finished_task_does_not_refuse(self, tree, deployment):
        deployment.write()
        deployment.with_live_task("alice", status="completed")

        assert main([]) == EXIT_OK
        assert (tree["repos_dir"] / "alice" / "acme" / "widget.git").is_dir()

    def test_no_database_yet_is_not_a_live_task(self, tree, deployment):
        """A first install has no framework DB. Nothing can be running against a
        database that does not exist, and refusing there would make the very
        first deploy fail."""
        deployment.write()

        assert main([]) == EXIT_OK

    def test_a_live_task_does_not_block_an_already_migrated_tree(
        self, tree, deployment
    ):
        """The second run is a no-op, and a no-op cannot race anything."""
        deployment.write()
        assert main([]) == EXIT_OK
        deployment.with_live_task("alice")

        assert main([]) == EXIT_OK


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Ansible calls this unconditionally, so the second run has to be a no-op —
    and by the marker, never by inferring the layout from directory names."""

    def test_the_second_run_moves_nothing(self, tree, deployment):
        deployment.write()
        assert main([]) == EXIT_OK
        after_first = _snapshot(tree["repos_dir"])

        assert main([]) == EXIT_OK

        assert _snapshot(tree["repos_dir"]) == after_first

    def test_the_second_run_says_so(self, tree, deployment, capsys):
        deployment.write()
        main([])
        capsys.readouterr()

        main([])

        assert "already" in capsys.readouterr().out.lower()

    def test_the_marker_wins_over_an_ambiguous_admin_set(self, tree, deployment):
        """A migrated deployment that later gains a second admin is still a
        no-op, not a refusal: there is nothing left to place."""
        deployment.write()
        assert main([]) == EXIT_OK
        deployment.write(admins=["alice", "bob"])

        assert main([]) == EXIT_OK

    def test_the_plan_reports_the_marker_rather_than_re_reading_the_tree(self, tree):
        (tree["repos_dir"] / MARKER_NAME).write_text(f"{LAYOUT_VERSION}\n")

        outcome = plan(tree["repos_dir"], {"alice"})

        assert outcome.already_migrated
        assert outcome.moves == ()

    def test_applying_an_already_migrated_plan_does_nothing(self, tree):
        (tree["repos_dir"] / MARKER_NAME).write_text(f"{LAYOUT_VERSION}\n")
        before = _snapshot(tree["repos_dir"])

        report = apply(plan(tree["repos_dir"], {"alice"}))

        assert report.moved == ()
        assert _snapshot(tree["repos_dir"]) == before


# ---------------------------------------------------------------------------
# Nothing to migrate
# ---------------------------------------------------------------------------


class TestNothingToMigrate:
    """A fresh install is already in the new layout, so the marker goes down
    without any ownership question being asked at all."""

    def test_an_empty_root_writes_the_marker(self, tmp_path, deployment):
        deployment.repos_dir.mkdir()
        deployment.write(admins=[])

        assert main([]) == EXIT_OK
        assert (deployment.repos_dir / MARKER_NAME).read_text().strip() == LAYOUT_VERSION

    def test_an_absent_root_writes_the_marker(self, deployment):
        deployment.write(admins=[])

        assert main([]) == EXIT_OK
        assert (deployment.repos_dir / MARKER_NAME).read_text().strip() == LAYOUT_VERSION

    def test_an_unconfigured_repos_dir_is_not_an_error(self, deployment):
        deployment.write(admins=[], repos_dir="")

        assert main([]) == EXIT_OK

    def test_a_root_that_is_a_file_refuses(self, tmp_path, deployment):
        deployment.repos_dir.write_text("not a directory\n")
        deployment.write()

        assert main([]) == EXIT_REFUSED


# ---------------------------------------------------------------------------
# What is not a namespace
# ---------------------------------------------------------------------------


class TestEntriesThatAreNotNamespaces:
    """The root has held more than namespaces since the package caches moved
    into it, and it was bound read-write into every admin task before this
    layout, so an entry there may be model-written."""

    def test_the_old_package_cache_root_stays_where_it_is(self, tree, deployment):
        """`{repos_dir}/.package-caches` was the cc691d6f layout. It is a cache
        rather than a repository and its per-user directories are named from
        disk, which is the one axis that must not be trusted here, so it is
        reported and left rather than moved into somebody's subtree."""
        cache = tree["repos_dir"] / ".package-caches" / "alice" / "uv"
        cache.mkdir(parents=True)
        deployment.write()

        assert main([]) == EXIT_OK

        assert cache.is_dir()
        assert not (tree["repos_dir"] / "alice" / ".package-caches").exists()

    def test_a_file_at_the_root_is_left_alone(self, tree):
        stray = tree["repos_dir"] / "notes.txt"
        stray.write_text("hello\n")

        apply(plan(tree["repos_dir"], {"alice"}))

        assert stray.is_file()

    def test_a_symlink_at_the_root_is_not_followed(self, tree, tmp_path):
        """A symlink here would be model-planted, and moving it hands whatever
        it points at to the user whose subtree it lands in."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret").write_text("x\n")
        link = tree["repos_dir"] / "sneaky"
        link.symlink_to(outside)

        report = apply(plan(tree["repos_dir"], {"alice"}))

        assert "sneaky" not in report.moved
        assert link.is_symlink()
        assert not (tree["repos_dir"] / "alice" / "sneaky").exists()
        assert (outside / "secret").exists()

    def test_a_namespace_named_like_the_admin_is_left_in_place(self, tree):
        """`{repos_dir}/alice` is the destination. Renaming it into itself is
        `EINVAL`, and on a re-run after a crash it is the half-migrated tree —
        so the destination name is never a move candidate. The clones inside it
        end up one level shallower than the rest, inside the right user's root."""
        repos_dir = tree["repos_dir"]
        personal = _clone(repos_dir, "alice", "sideproject", _upstream(repos_dir.parent, "side"))

        report = apply(plan(repos_dir, {"alice"}))

        assert "alice" not in report.moved
        assert personal.is_dir()
        assert (repos_dir / "alice" / "acme" / "widget.git").is_dir()

    def test_a_destination_collision_refuses(self, tree):
        """A namespace whose name already exists under the user's subtree. The
        two trees are not merged, because a name collision here means the
        migration's assumption about the tree is wrong."""
        repos_dir = tree["repos_dir"]
        (repos_dir / "alice" / "acme").mkdir(parents=True)

        outcome = plan(repos_dir, {"alice"})

        assert isinstance(outcome, RelocateRefusal)
        assert outcome.reason == "destination_collision"
        assert "acme" in "\n".join(outcome.details)


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_it_touches_nothing(self, tree, deployment):
        deployment.write()
        before = _snapshot(tree["repos_dir"])

        assert main(["--dry-run"]) == EXIT_OK

        assert _snapshot(tree["repos_dir"]) == before
        assert not (tree["repos_dir"] / MARKER_NAME).exists()

    def test_it_prints_each_move(self, tree, deployment, capsys):
        deployment.write()

        main(["--dry-run"])

        out = capsys.readouterr().out
        assert str(tree["repos_dir"] / "alice" / "acme") in out
        assert str(tree["repos_dir"] / "alice" / "other-org") in out

    def test_it_still_refuses_an_ambiguous_owner(self, tree, deployment):
        """A dry run that reported success and a real run that refused would
        make the dry run worthless."""
        deployment.write(admins=["alice", "bob"])

        assert main(["--dry-run"]) == EXIT_REFUSED

    def test_it_still_refuses_a_live_task(self, tree, deployment):
        deployment.write()
        deployment.with_live_task("alice")

        assert main(["--dry-run"]) == EXIT_REFUSED


class TestList:
    """Inspection, never a verdict: `--list` reports the tree as it stands and
    exits 0 even where a migration would refuse."""

    def test_it_names_the_namespaces(self, tree, deployment, capsys):
        deployment.write()

        assert main(["--list"]) == EXIT_OK

        out = capsys.readouterr().out
        assert "acme" in out and "other-org" in out

    def test_it_touches_nothing(self, tree, deployment):
        deployment.write()
        before = _snapshot(tree["repos_dir"])

        main(["--list"])

        assert _snapshot(tree["repos_dir"]) == before

    def test_it_exits_zero_with_an_ambiguous_owner(self, tree, deployment):
        deployment.write(admins=["alice", "bob"])

        assert main(["--list"]) == EXIT_OK


class TestTheCliContract:
    """The error posture the reaper and the cache sweeper already use: never
    raises out of `main`, and a refusal is distinguishable from a success by the
    exit code as well as by the text."""

    def test_a_successful_migration_exits_zero(self, tree, deployment):
        deployment.write()

        assert main([]) == EXIT_OK

    def test_the_exit_codes_are_distinct(self):
        assert EXIT_OK == 0
        assert EXIT_REFUSED != EXIT_OK
        assert EXIT_PARTIAL not in (EXIT_OK, EXIT_REFUSED)

    @pytest.mark.requires_dac
    def test_an_unreadable_root_refuses_rather_than_reporting_success(
        self, tree, deployment
    ):
        """The cheerful reading of an unreadable root — no marker, no
        namespaces, nothing to do — writes a marker over a tree nobody looked
        at, and every later run is then a no-op."""
        deployment.write()
        os.chmod(tree["repos_dir"], 0o000)
        try:
            code = main([])
        finally:
            os.chmod(tree["repos_dir"], 0o755)

        assert code == EXIT_REFUSED
        assert not (tree["repos_dir"] / MARKER_NAME).exists()

    def test_a_namespace_that_cannot_be_moved_is_reported_and_leaves_no_marker(
        self, tree, monkeypatch
    ):
        """A half-migrated tree with a marker on it would never be retried, so
        the marker goes down only when every move landed."""
        repos_dir = tree["repos_dir"]
        real_rename = os.rename

        def failing_rename(src, dst, *a, **kw):
            if Path(src).name == "other-org":
                raise OSError(13, "Permission denied")
            return real_rename(src, dst, *a, **kw)

        monkeypatch.setattr(os, "rename", failing_rename)

        report = apply(plan(repos_dir, {"alice"}))

        assert report.moved == ("acme",)
        assert any("other-org" in f for f in report.failed)
        assert not report.marker_written
        assert not (repos_dir / MARKER_NAME).exists()

    def test_a_failed_move_exits_partial(self, tree, deployment, monkeypatch):
        deployment.write()
        real_rename = os.rename

        def failing_rename(src, dst, *a, **kw):
            if Path(src).name == "other-org":
                raise OSError(13, "Permission denied")
            return real_rename(src, dst, *a, **kw)

        monkeypatch.setattr(os, "rename", failing_rename)

        assert main([]) == EXIT_PARTIAL
