"""`scripts/check-vendored-parity.py` — does it report the right direction?

`docker/browser/` is a vendored copy of `browser/` from the stealth-browser
repository, synced by an rsync with `--delete`. The script answers whether the
two are in step, and, when they are not, which way — because the two
directions have opposite severity:

  stealth-browser ahead   ordinary mid-flight state; the next sync brings it
                          across. Exit 0.
  istota ahead            the next `rsync --delete` reverts it, silently.
                          Exit 1.

**These tests exist because the first version of the script reported those two
backwards.** It passed a hand-check against the live repositories, which were
in step, so every run printed "in step" and nothing disconfirmed it. Driving
it against a tree that had actually drifted is what found it — and the same
run found that the only-in-istota branch was unreachable, since the file list
was enumerated from the source side alone.

So each test here builds a real pair of repositories in a temp directory and
makes them drift on purpose. A parity checker that has only ever been observed
saying "in step" is the failure it exists to catch, one level up.
"""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check-vendored-parity.py"

#: The two vendored files the fixtures put in place. Enough for the union and
#: the per-file comparison to be exercised; the script reads whatever is
#: committed, so the real set does not matter here.
VENDORED = {"visual.py": "# visual\n", "xdotool.py": "# xdotool\n"}

ISTOTA_DIR = "docker/browser"
SOURCE_DIR = "browser"


def git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
    )


def init_repo(path, subdir, files):
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.invalid")
    git(path, "config", "user.name", "t")
    target = path / subdir
    target.mkdir(parents=True)
    for name, body in files.items():
        (target / name).write_text(body)
    git(path, "add", "-A")
    git(path, "commit", "-qm", "initial")
    return path


def commit(repo, rel, body, message):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)


@pytest.fixture
def pair(tmp_path):
    """Two repositories holding an identical copy of the vendored files."""
    istota = init_repo(tmp_path / "istota", ISTOTA_DIR, VENDORED)
    source = init_repo(tmp_path / "source", SOURCE_DIR, VENDORED)
    return istota, source


def run(istota, source, *extra):
    """Run the script against a pair, comparing local `main`.

    `--ref main` rather than the default `origin/main`: these repositories
    have no remote, and the ref the script defaults to is the one that matters
    in production, which `TestTheRefItCompares` covers separately.
    """
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--target", str(istota),
         "--source", str(source), "--ref", "main", "--no-fetch", *extra],
        capture_output=True, text=True, timeout=120,
    )
    return result.returncode, result.stdout + result.stderr


class TestTheDirection:
    """Which way the drift runs, which is the whole judgement in the script."""

    def test_two_identical_trees_are_in_step(self, pair):
        code, out = run(*pair)
        assert code == 0
        assert "in step" in out
        assert "0 out of step" in out

    def test_a_change_only_istota_has_is_the_dangerous_direction(self, pair):
        """The 2026-06-10 shape from the project notes: a fix made in the
        vendored copy and never back-ported, reverted by a later sync."""
        istota, source = pair
        commit(istota, f"{ISTOTA_DIR}/visual.py", "# visual\n# vendored only\n",
               "a change that never reached the source")

        code, out = run(istota, source)
        assert code == 1
        assert "DIFFER" in out and "visual.py" in out
        assert "istota is ahead" in out
        assert "rsync --delete" in out

    def test_a_change_only_the_source_has_is_the_ordinary_one(self, pair):
        """The control for the test above, and the one that matters most: if
        this also exited 1 the script would be flagging every normal
        mid-flight state, and a reader would learn to ignore it."""
        istota, source = pair
        commit(source, f"{SOURCE_DIR}/visual.py", "# visual\n# source only\n",
               "a change not yet vendored")

        code, out = run(istota, source)
        assert code == 0
        assert "DIFFER" in out and "visual.py" in out
        assert "stealth-browser is ahead" in out
        assert "istota is ahead" not in out

    def test_both_sides_moved_apart_is_reported_as_the_dangerous_one(self, pair):
        """Neither side's content is in the other's history. The script cannot
        settle it, and the unsettled answer has to be the loud one."""
        istota, source = pair
        commit(istota, f"{ISTOTA_DIR}/visual.py", "# visual\n# istota\n", "here")
        commit(source, f"{SOURCE_DIR}/visual.py", "# visual\n# source\n", "there")

        code, out = run(istota, source)
        assert code == 1
        assert "istota is ahead" in out


class TestTheFileSet:
    """Both sides are enumerated, because a file can exist on only one."""

    def test_a_file_only_istota_has_is_found(self, pair):
        """Unreachable in the first version: the list came from the source
        alone, so a vendored-only file was never compared -- and that is
        precisely what `rsync --delete` removes."""
        istota, source = pair
        commit(istota, f"{ISTOTA_DIR}/orphan.py", "# orphan\n",
               "a vendored file with no source")

        code, out = run(istota, source)
        assert code == 1
        assert "ONLY-IN-ISTOTA" in out and "orphan.py" in out

    def test_a_file_only_the_source_has_is_found_and_is_ordinary(self, pair):
        istota, source = pair
        commit(source, f"{SOURCE_DIR}/added.py", "# added\n", "a new source file")

        code, out = run(istota, source)
        assert code == 0
        assert "ONLY-IN-SOURCE" in out and "added.py" in out
        assert "stealth-browser is ahead" in out


class TestFindingTheSourceRepository:
    def test_a_missing_checkout_skips_rather_than_fails(self, pair, tmp_path):
        """It runs on machines with no copy of a private repository, so an
        absent one must not fail a check somebody wired into a chain."""
        istota, _ = pair
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--target", str(istota),
             "--source", str(tmp_path / "nope"), "--ref", "main", "--no-fetch"],
            capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)},
            timeout=120,
        )
        assert result.returncode == 0
        assert "skipping" in result.stdout

    def test_the_environment_variable_is_honoured(self, pair):
        istota, source = pair
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--target", str(istota),
             "--ref", "main", "--no-fetch"],
            capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin",
                 "STEALTH_BROWSER_REPO": str(source)},
            timeout=120,
        )
        assert result.returncode == 0
        assert "in step" in result.stdout


class TestTheRefItCompares:
    """The reason the script exists at all: published, not local."""

    def test_it_defaults_to_the_published_ref(self):
        """`origin/main` is what a fresh clone syncs from, and it is where the
        two answers diverge -- a committed, unpushed change leaves the working
        trees identical and the published trees apart. Both parity checkers
        written on 2026-09-20 read local refs and both missed exactly that."""
        source = SCRIPT.read_text()
        assert '"--ref", default="origin/main"' in source

    def test_an_unpushed_commit_is_reported(self, pair, tmp_path):
        """A commit that has not been pushed is about to break parity even
        while every file matches, so it is named rather than left to be
        discovered by the next sync."""
        istota, source = pair
        # Both sides need a published ref: the comparison reads `origin/main`
        # on each, so a source with no remote fails before the unpushed
        # report is reached.
        for repo, name in ((istota, "istota"), (source, "source")):
            remote = tmp_path / f"{name}.git"
            subprocess.run(["git", "init", "-q", "--bare", str(remote)],
                           check=True, capture_output=True)
            git(repo, "remote", "add", "origin", str(remote))
            git(repo, "push", "-q", "origin", "main")

        commit(istota, f"{ISTOTA_DIR}/visual.py", "# visual\n# local only\n",
               "committed, not pushed")

        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--target", str(istota),
             "--source", str(source), "--ref", "origin/main", "--no-fetch"],
            capture_output=True, text=True, timeout=120,
        )
        assert "not pushed" in result.stdout
        assert "committed, not pushed" in result.stdout
