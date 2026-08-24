"""`deploy/ansible/files/migrate_repos_layout.py` — the one-time repos move.

`developer.repos_dir` became a per-user root, so existing clones move down a
level. This is the only script in the role that *moves somebody's working
directory*, and it runs unattended on a host whose repositories may have a task
running in one of them right now — so the interesting assertions are all about
what it refuses to touch.

Driven against real git repositories rather than mocked output: the script's
whole job is reading `git worktree list` and `git status` correctly, and a test
that fed it canned strings would hold the parser to the shape the author
imagined rather than the one git emits. Every case here builds a real tree.

The script lives outside the package (`deploy/ansible/files/`), so it is loaded
by path the same way `tests/test_ansible_config_template.py` loads the role's
filter plugin.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "deploy" / "ansible" / "files" / "migrate_repos_layout.py"


@pytest.fixture(scope="module")
def migrate():
    spec = importlib.util.spec_from_file_location("migrate_repos_layout", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(cwd: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        check=True, env=env, timeout=60,
    )
    return proc.stdout


def _bare_with_worktree(
    root: Path, namespace: str = "namespace", seed_root: Path | None = None
) -> tuple[Path, Path]:
    """The documented layout: `<namespace>/<project>.git` with a worktree beside it.

    Returns `(bare, worktree)`.
    """
    root.mkdir(parents=True, exist_ok=True)
    # Outside the migration root: a seed clone left inside it is itself a
    # candidate, and the scan would move it.
    seed = (seed_root or root.parent) / f"seed-{root.name}-{namespace}"
    seed.mkdir(parents=True)
    _git(seed, "init", "-q", "-b", "main")
    (seed / "README.md").write_text("hello\n")
    (seed / ".gitignore").write_text(".env\nnode_modules/\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "seed")

    bare = root / namespace / "project.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "clone", "-q", "--bare", str(seed), str(bare))

    worktree = root / namespace / "project--task"
    _git(bare, "worktree", "add", "-q", str(worktree), "main")
    return bare, worktree


def _run(
    migrate,
    root: Path,
    user: str | None = "alice",
    known: str = "",
    idle_minutes: float = 0.0,
) -> list[str]:
    # `--idle-minutes 0` by default: every tree here is built seconds before it
    # is migrated, so the live-work window would hold all of them. The window
    # itself gets its own test.
    argv = ["--root", str(root), "--known-user", known,
            "--idle-minutes", str(idle_minutes)]
    if user:
        argv += ["--user", user]
    lines: list[str] = []
    import io
    import contextlib

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        migrate.main(argv)
    lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
    return lines


class TestTheHappyPath:
    def test_a_bare_clone_and_its_worktree_move_together(self, migrate, tmp_path):
        root = tmp_path / "repos"
        root.mkdir()
        bare, worktree = _bare_with_worktree(root)

        lines = _run(migrate, root)

        assert any(line.startswith("MOVED ") for line in lines), lines
        assert not (root / "namespace").exists()
        assert (root / "alice" / "namespace" / "project.git").is_dir()
        assert (root / "alice" / "namespace" / "project--task" / "README.md").is_file()

    def test_the_moved_worktree_is_still_a_worktree(self, migrate, tmp_path):
        """A move changes every worktree's absolute path and both halves of the
        link record one, so without `git worktree repair` each worktree is
        registered against a path that no longer exists."""
        root = tmp_path / "repos"
        root.mkdir()
        _bare_with_worktree(root)

        _run(migrate, root)

        moved = root / "alice" / "namespace" / "project--task"
        status = _git(moved, "status", "--porcelain")
        assert status == ""
        listing = _git(root / "alice" / "namespace" / "project.git",
                       "worktree", "list", "--porcelain")
        assert str(moved) in listing

    def test_it_is_idempotent(self, migrate, tmp_path):
        root = tmp_path / "repos"
        root.mkdir()
        _bare_with_worktree(root)

        _run(migrate, root)
        second = _run(migrate, root, known="alice")

        assert not any(line.startswith("MOVED ") for line in second), second

    def test_an_empty_root_reports_nothing_to_do(self, migrate, tmp_path):
        root = tmp_path / "repos"
        root.mkdir()

        lines = _run(migrate, root)

        assert lines == [f"OK {root} is already in the per-user layout"]

    def test_a_missing_root_is_not_an_error(self, migrate, tmp_path):
        lines = _run(migrate, tmp_path / "never-created")

        assert lines and lines[0].startswith("SKIP ")

    def test_a_directory_named_for_a_known_user_is_left_alone(self, migrate, tmp_path):
        """A root already in the per-user shape. Without this the script would
        file `{repos_dir}/bob` under `{repos_dir}/alice/bob`."""
        root = tmp_path / "repos"
        root.mkdir()
        _bare_with_worktree(root / "bob", seed_root=tmp_path / "seeds")

        lines = _run(migrate, root, known="alice,bob")

        assert not any(line.startswith("MOVED ") for line in lines), lines
        assert (root / "bob" / "namespace" / "project.git").is_dir()

    def test_a_forge_namespace_named_after_a_user_still_moves(self, migrate, tmp_path):
        """The name alone is not the discriminator.

        A top-level entry in the old layout is a *forge namespace*, and one
        named after some other istota user is entirely ordinary. Skipping it on
        the name would leave it behind while the script printed `OK … already in
        the per-user layout` — the one line an operator would grep for, saying
        the opposite of what happened. The depth of its repositories is the real
        discriminator: a per-user root holds them at depth 2, a namespace at 1.
        """
        root = tmp_path / "repos"
        root.mkdir()
        namespace = root / "bob"
        namespace.mkdir()
        seed = tmp_path / "seed"
        seed.mkdir()
        _git(seed, "init", "-q", "-b", "main")
        (seed / "README.md").write_text("hello\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-qm", "seed")
        _git(root, "clone", "-q", "--bare", str(seed), str(namespace / "project.git"))

        lines = _run(migrate, root, known="alice,bob")

        assert any(line.startswith("NOTE ") for line in lines), lines
        assert any(line.startswith("MOVED ") for line in lines), lines
        assert (root / "alice" / "bob" / "project.git").is_dir()


class TestWhatItRefusesToMove:
    def test_a_modified_tracked_file_holds_the_repository(self, migrate, tmp_path):
        root = tmp_path / "repos"
        root.mkdir()
        _, worktree = _bare_with_worktree(root)
        (worktree / "README.md").write_text("edited\n")

        lines = _run(migrate, root)

        assert any(line.startswith("HELD ") for line in lines), lines
        assert (root / "namespace" / "project.git").is_dir()

    def test_a_gitignored_env_file_holds_the_repository(self, migrate, tmp_path):
        """The whole reason the ignored half of the check exists. A rule that
        discounted ignored entries as a class would delete it."""
        root = tmp_path / "repos"
        root.mkdir()
        _, worktree = _bare_with_worktree(root)
        (worktree / ".env").write_text("SECRET_NAME=placeholder\n")

        lines = _run(migrate, root)

        assert any(line.startswith("HELD ") for line in lines), lines

    def test_reconstructible_build_output_does_not_hold_it(self, migrate, tmp_path):
        """A worktree that ran an install is still movable: nothing under
        `node_modules` exists anywhere but in files that are already
        committed."""
        root = tmp_path / "repos"
        root.mkdir()
        _, worktree = _bare_with_worktree(root)
        (worktree / "node_modules" / "pkg").mkdir(parents=True)
        (worktree / "node_modules" / "pkg" / "index.js").write_text("//\n")

        lines = _run(migrate, root)

        assert any(line.startswith("MOVED ") for line in lines), lines

    def test_an_untracked_source_file_holds_it(self, migrate, tmp_path):
        """The asymmetry the reconstructible list is built on: a name missing
        from that list keeps a repository where it is, a name wrongly on it
        moves work that exists nowhere else."""
        root = tmp_path / "repos"
        root.mkdir()
        _, worktree = _bare_with_worktree(root)
        (worktree / "scratch.py").write_text("print('wip')\n")

        lines = _run(migrate, root)

        assert any(line.startswith("HELD ") for line in lines), lines

    def test_a_worktree_outside_the_repository_holds_it(self, migrate, tmp_path):
        """Moving the bare clone out from under a checkout that lives somewhere
        else entirely leaves that checkout pointing at a path that is gone, and
        `worktree repair` run from the new location cannot reach it."""
        root = tmp_path / "repos"
        root.mkdir()
        bare, _ = _bare_with_worktree(root)
        outside = tmp_path / "elsewhere"
        _git(bare, "worktree", "add", "-q", str(outside), "-b", "other")

        lines = _run(migrate, root)

        assert any(line.startswith("HELD ") for line in lines), lines
        assert (root / "namespace" / "project.git").is_dir()

    def test_it_never_merges_into_an_existing_destination(self, migrate, tmp_path):
        """The one operation here with no way back."""
        root = tmp_path / "repos"
        root.mkdir()
        _bare_with_worktree(root)
        (root / "alice" / "namespace").mkdir(parents=True)

        lines = _run(migrate, root)

        assert any("already exists" in line for line in lines), lines
        assert (root / "namespace" / "project.git").is_dir()

    def test_a_bare_clone_with_no_worktrees_still_moves(self, migrate, tmp_path):
        """The negative control for the worktree parser: `git status` in a bare
        repository exits non-zero, so counting the bare entry as a checkout
        would hold every repository in the documented layout and the migration
        would silently never move anything."""
        root = tmp_path / "repos"
        root.mkdir()
        bare, worktree = _bare_with_worktree(root)
        _git(bare, "worktree", "remove", str(worktree))

        lines = _run(migrate, root)

        assert any(line.startswith("MOVED ") for line in lines), lines


class TestTheIdleWindow:
    """The clean check cannot see a build in flight.

    A task running `npm ci` or `uv sync` right now has produced exactly the
    untracked `node_modules/` and `.venv/` paths `_is_reconstructible`
    discounts, so the directory reads as clean — and the daemon is up and
    dispatching while this runs, because the play does not stop it. Without the
    window the migration moves a checkout out from under the process writing to
    it.
    """

    def test_a_freshly_written_tree_is_held(self, migrate, tmp_path):
        root = tmp_path / "repos"
        root.mkdir()
        _bare_with_worktree(root)

        lines = _run(migrate, root, idle_minutes=15)

        assert any("may be using it" in line for line in lines), lines
        assert (root / "namespace" / "project.git").is_dir()

    def test_an_old_tree_moves(self, migrate, tmp_path):
        """The negative control for the window: it must not hold everything for
        ever, or the migration is a no-op with a plausible message."""
        root = tmp_path / "repos"
        root.mkdir()
        _bare_with_worktree(root)
        old = time.time() - 3600
        for path in (root / "namespace").rglob("*"):
            os.utime(path, (old, old), follow_symlinks=False)
        os.utime(root / "namespace", (old, old))

        lines = _run(migrate, root, idle_minutes=15)

        assert any(line.startswith("MOVED ") for line in lines), lines

    def test_a_build_in_flight_is_held_even_though_git_calls_it_clean(
        self, migrate, tmp_path
    ):
        """The exact case: `git status` discounts the install output, so only
        the mtime distinguishes a finished worktree from one being written."""
        root = tmp_path / "repos"
        root.mkdir()
        _, worktree = _bare_with_worktree(root)
        old = time.time() - 3600
        for path in sorted((root / "namespace").rglob("*"), reverse=True):
            os.utime(path, (old, old), follow_symlinks=False)
        os.utime(root / "namespace", (old, old))
        # …and now an install starts.
        (worktree / "node_modules").mkdir()
        (worktree / "node_modules" / "half-written.js").write_text("//\n")

        # The clean check alone, asked directly rather than by running the
        # migration — a first run with the window off would *move* the tree, and
        # the second run would then be asserting about the destination.
        assert migrate._blocking_reason(
            root / "namespace" / "project.git", root / "namespace"
        ) == "", "git should call an install-in-flight worktree clean"

        lines = _run(migrate, root, idle_minutes=15)

        assert any("may be using it" in line for line in lines), lines
        assert (root / "namespace" / "project.git").is_dir()


class TestItNeedsToKnowWhoseTheClonesAre:
    def _exit(self, migrate, root, **kwargs):
        import contextlib
        import io

        argv = ["--root", str(root), "--idle-minutes", "0"]
        for flag, value in kwargs.items():
            argv += [f"--{flag.replace('_', '-')}", value]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = migrate.main(argv)
        return code, [line for line in buffer.getvalue().splitlines() if line.strip()]

    def test_several_users_means_it_reports_and_exits_non_zero(self, migrate, tmp_path):
        """A green play here is followed by a daemon restart into a state where
        the repos bind names an empty directory and the developer skill is
        silently unusable. Nothing can work out the owner, so the operator has
        to be stopped rather than warned."""
        root = tmp_path / "repos"
        root.mkdir()
        _bare_with_worktree(root)

        code, lines = self._exit(migrate, root, known_user="alice,bob")

        assert code == 2
        assert any(line.startswith("WOULD-MOVE ") for line in lines), lines
        assert (root / "namespace" / "project.git").is_dir()
        assert not (root / "alice").exists()

    def test_no_users_at_all_also_stops(self, migrate, tmp_path):
        root = tmp_path / "repos"
        root.mkdir()
        _bare_with_worktree(root)

        code, _ = self._exit(migrate, root, known_user="")

        assert code == 2

    def test_one_configured_user_is_derived(self, migrate, tmp_path):
        """The reference deployment. With one user there is nothing to derive
        between, and leaving it to a hand-set variable is what turns "the role
        performs the move" into "the role reports and does nothing" on the host
        that most needs it."""
        root = tmp_path / "repos"
        root.mkdir()
        _bare_with_worktree(root)

        code, lines = self._exit(migrate, root, known_user="alice")

        assert code == 0
        assert any(line.startswith("MOVED ") for line in lines), lines
        assert (root / "alice" / "namespace" / "project.git").is_dir()

    def test_an_explicit_user_still_wins(self, migrate, tmp_path):
        root = tmp_path / "repos"
        root.mkdir()
        _bare_with_worktree(root)

        code, _ = self._exit(migrate, root, known_user="alice,bob", user="bob")

        assert code == 0
        assert (root / "bob" / "namespace" / "project.git").is_dir()

    def test_a_root_already_migrated_exits_zero_with_no_user(self, migrate, tmp_path):
        """The steady state on every later run. Exiting 2 there would fail the
        play for ever on a host that is already correct."""
        root = tmp_path / "repos"
        root.mkdir()
        _bare_with_worktree(root / "alice", seed_root=tmp_path / "seeds")

        code, lines = self._exit(migrate, root, known_user="alice,bob")

        assert code == 0
        assert any("already in the per-user layout" in line for line in lines), lines


class TestTheRoleShipsIt:
    def test_the_task_installs_and_runs_the_script(self):
        """The premise of everything above: a script the role does not run is a
        script that migrates nothing."""
        tasks = (REPO / "deploy" / "ansible" / "tasks" / "main.yml").read_text()

        assert "Install the repos layout migration script" in tasks
        assert "migrate-repos-layout" in tasks

    def test_it_runs_as_the_daemon_user(self):
        """Not as root. git refuses a repository owned by another user
        ("dubious ownership"), so a root-run migration would report every
        repository held for a reason unrelated to its contents — and a
        cross-device move as root would leave the tree root-owned, which is the
        uid mismatch this whole change exists to prevent."""
        tasks = (REPO / "deploy" / "ansible" / "tasks" / "main.yml").read_text()
        block = tasks.split("Migrate a flat repos layout to the per-user one")[1][:800]

        assert "sudo -u {{ istota_user }}" in block
