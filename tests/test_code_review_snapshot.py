"""Tests for the code review snapshot: the reviewed commit, written to disk.

The reviewer reads what this module writes and nothing else, so each property
here is one a reviewer namespace depends on: content comes from the object
store (never the worktree, never through `.gitattributes`), nothing under
`tree/` is a link, the run directory is private, and a failed build leaves
nothing behind. Fixtures are real git repositories, for the reason
`tests/test_code_review_engine.py` gives.
"""

from __future__ import annotations

import io
import os
import stat
import subprocess
import tarfile
import threading
import time
from pathlib import Path

import pytest

from istota.skills.code_review import engine, snapshot
from istota.skills.code_review.engine import ReviewError, collect_diff, resolve_range
from istota.skills.code_review.snapshot import build_snapshot, sweep_stale_run_dirs
from tests.test_code_review_engine import (  # noqa: F401 - repos_root is a fixture
    GIT_ISOLATION,
    commit,
    repos_root,
    run_git,
)

BIG = 10**9


@pytest.fixture
def temp_root(tmp_path) -> Path:
    root = tmp_path / "temp"
    root.mkdir()
    return root


@pytest.fixture
def repo(repos_root) -> Path:  # noqa: F811 - the imported fixture, requested by name
    wt = repos_root / "proj"
    wt.mkdir()
    run_git(wt, "init", "-q", "-b", "main", ".")
    (wt / "pkg").mkdir()
    (wt / "pkg" / "app.py").write_text("def existing():\n    return 1\n")
    (wt / "pkg" / "neighbour.py").write_text("NEIGHBOUR = 1\n")
    (wt / "other").mkdir()
    (wt / "other" / "unrelated.py").write_text("UNRELATED = 1\n")
    (wt / "AGENTS.md").write_text("# Rules\n")
    commit(wt, "base")
    run_git(wt, "checkout", "-q", "-b", "feature")
    return wt


def change(repo: Path) -> None:
    (repo / "pkg" / "app.py").write_text(
        "def existing():\n    return 1\n\n\ndef added(value):\n    return value\n"
    )
    (repo / "pkg" / "new.py").write_text("NEW = 1\n")
    commit(repo, "pkg: add a helper")


def bundle_for(repo: Path, max_chars: int = 200_000):
    return collect_diff(repo, resolve_range(repo, base="main"), max_chars)


def snap(repo, temp_root, *, max_bytes=BIG, max_file_bytes=BIG, user_id="alice"):
    return build_snapshot(
        repo,
        bundle_for(repo),
        root=temp_root,
        user_id=user_id,
        max_bytes=max_bytes,
        max_file_bytes=max_file_bytes,
    )


def skipped_lines(result) -> list[str]:
    return (result.run_dir / "meta" / "skipped.txt").read_text().splitlines()


class TestTheTree:
    def test_regular_files_are_the_committed_blobs(self, repo, temp_root):
        change(repo)
        (repo / "data.bin").write_bytes(b"\x00\x01binary\x00")
        (repo / "run.sh").write_text("#!/bin/sh\necho hi\n")
        os.chmod(repo / "run.sh", 0o755)
        commit(repo, "more")
        # The worktree differs from the commit: the snapshot must not see it.
        (repo / "pkg" / "app.py").write_text("UNCOMMITTED\n")
        (repo / "untracked.py").write_text("UNTRACKED\n")

        result = snap(repo, temp_root)

        for path in ("pkg/app.py", "pkg/new.py", "pkg/neighbour.py", "AGENTS.md", "run.sh"):
            expected = run_git(repo, "show", f"HEAD:{path}")
            assert (result.tree_dir / path).read_text() == expected
        assert (result.tree_dir / "data.bin").read_bytes() == b"\x00\x01binary\x00"
        assert not (result.tree_dir / "untracked.py").exists()
        assert result.files == 7
        assert result.truncated is False
        assert result.skipped == {}

    def test_a_symlink_is_recorded_and_never_written(self, repo, temp_root, tmp_path):
        secret = tmp_path / "outside_secret.txt"
        secret.write_text("SECRET")
        (repo / "pkg" / "link.py").symlink_to(secret)
        commit(repo, "link")

        result = snap(repo, temp_root)

        assert not os.path.lexists(result.tree_dir / "pkg" / "link.py")
        assert "symlink\tpkg/link.py" in skipped_lines(result)
        assert result.skipped == {"symlink": 1}
        for path in result.run_dir.rglob("*"):
            assert not path.is_symlink()

    def test_control_the_same_path_as_a_regular_file_is_written(self, repo, temp_root):
        (repo / "pkg" / "link.py").write_text("REGULAR")
        commit(repo, "regular")

        result = snap(repo, temp_root)

        assert (result.tree_dir / "pkg" / "link.py").read_text() == "REGULAR"
        assert result.skipped == {}

    def test_gitattributes_change_nothing(self, repo, temp_root, tmp_path):
        """`export-ignore` and a textconv / smudge driver are all inert.

        The attack is shown live first: `git archive` honours the commit's
        `export-ignore`, and a checkout runs the smudge filter.
        """
        marker = tmp_path / "filter_ran"
        script = tmp_path / "filter.sh"
        script.write_text(f"#!/bin/sh\ntouch {marker}\ntr a-z A-Z\n")
        script.chmod(0o755)
        run_git(repo, "config", "filter.shout.smudge", str(script))
        run_git(repo, "config", "filter.shout.clean", "cat")
        run_git(repo, "config", "diff.shout.textconv", str(script))
        (repo / ".gitattributes").write_text(
            "hidden.txt export-ignore\nshout.txt filter=shout diff=shout\n"
        )
        (repo / "hidden.txt").write_text("hidden content\n")
        (repo / "shout.txt").write_text("quiet content\n")
        commit(repo, "attributes")

        archived = subprocess.run(
            ["git", "archive", "--format=tar", "HEAD"],
            cwd=repo, capture_output=True, env={**os.environ, **GIT_ISOLATION},
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archived)) as tar:
            names = tar.getnames()
        assert "hidden.txt" not in names
        assert "shout.txt" in names
        marker.unlink(missing_ok=True)

        result = snap(repo, temp_root)

        assert (result.tree_dir / "hidden.txt").read_text() == "hidden content\n"
        assert (result.tree_dir / "shout.txt").read_text() == "quiet content\n"
        assert not marker.exists()

    def test_a_submodule_is_recorded_and_not_written(self, repo, temp_root):
        head = run_git(repo, "rev-parse", "HEAD").strip()
        run_git(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},vendor/sub")
        run_git(repo, "commit", "-q", "-m", "submodule")

        result = snap(repo, temp_root)

        assert not os.path.lexists(result.tree_dir / "vendor" / "sub")
        assert "submodule\tvendor/sub" in skipped_lines(result)

    def test_paths_the_engine_rejects_are_recorded_not_written(self, repo, temp_root):
        (repo / "-rf").write_text("dash\n")
        (repo / "two\nlines.txt").write_text("newline\n")
        commit(repo, "odd names")

        result = snap(repo, temp_root)

        assert not (result.tree_dir / "-rf").exists()
        assert not (result.tree_dir / "two\nlines.txt").exists()
        lines = skipped_lines(result)
        assert "bad_path\t-rf" in lines
        # The newline is escaped, so a path cannot forge a second line.
        assert "bad_path\ttwo\\x0alines.txt" in lines
        assert result.skipped == {"bad_path": 2}


class TestCaps:
    def test_a_blob_over_the_per_file_cap_is_skipped_unread(self, repo, temp_root, monkeypatch):
        (repo / "pkg" / "big.py").write_text("x" * 5000)
        commit(repo, "big")
        read = []
        real = snapshot._read_blobs
        monkeypatch.setattr(
            snapshot, "_read_blobs",
            lambda wt, batch: read.extend(e.path for e in batch) or real(wt, batch),
        )

        result = snap(repo, temp_root, max_file_bytes=1000)

        assert not (result.tree_dir / "pkg" / "big.py").exists()
        assert "too_large\tpkg/big.py" in skipped_lines(result)
        assert "pkg/big.py" not in read
        assert result.truncated is False

    def test_the_total_cap_keeps_changed_files_and_their_directory_first(
        self, repo, temp_root
    ):
        change(repo)
        sizes = {
            path: len(run_git(repo, "show", f"HEAD:{path}").encode())
            for path in ("pkg/app.py", "pkg/new.py", "pkg/neighbour.py")
        }

        result = snap(repo, temp_root, max_bytes=sum(sizes.values()))

        for path in sizes:
            assert (result.tree_dir / path).exists(), path
        assert not (result.tree_dir / "other" / "unrelated.py").exists()
        assert not (result.tree_dir / "AGENTS.md").exists()
        assert result.truncated is True
        assert result.skipped == {"over_budget": 2}
        assert result.bytes == sum(sizes.values())

    def test_over_budget_lines_collapse_past_the_limit(self, repo, temp_root, monkeypatch):
        monkeypatch.setattr(snapshot, "MAX_OVER_BUDGET_LINES", 2)
        result = snap(repo, temp_root, max_bytes=0)
        lines = skipped_lines(result)
        assert len([line for line in lines if line.startswith("over_budget")]) == 3
        assert lines[-1] == "over_budget\t... and 2 more paths not written"
        assert result.skipped["over_budget"] == 4


class TestTheRunDirectory:
    def test_modes(self, repo, temp_root):
        change(repo)
        result = snap(repo, temp_root)

        assert result.run_dir.parent == temp_root / ".review" / "alice"
        assert result.run_dir.name.startswith("run-")
        for directory in (
            temp_root / ".review",
            temp_root / ".review" / "alice",
            result.run_dir,
            result.tree_dir,
            result.tree_dir / "pkg",
            result.run_dir / "meta",
        ):
            assert stat.S_IMODE(directory.lstat().st_mode) == 0o700, directory
        for leaf in (result.tree_dir / "pkg" / "app.py", result.run_dir / "meta" / "diff.patch"):
            assert stat.S_IMODE(leaf.lstat().st_mode) == 0o600, leaf

    def test_a_failure_mid_write_leaves_nothing(self, repo, temp_root, monkeypatch):
        change(repo)
        calls = []
        real = snapshot._write_leaf

        def flaky(path, data):
            calls.append(path)
            if len(calls) == 3:
                raise OSError(28, "No space left on device")
            real(path, data)

        monkeypatch.setattr(snapshot, "_write_leaf", flaky)

        with pytest.raises(ReviewError) as excinfo:
            snap(repo, temp_root)

        assert excinfo.value.reason == "snapshot_failed"
        assert len(calls) == 3
        assert list((temp_root / ".review" / "alice").iterdir()) == []

    def test_a_user_level_removed_mid_build_is_recreated_once(
        self, repo, temp_root, monkeypatch
    ):
        """The temp cleanup rmdirs an idle, empty level; one retry covers it."""
        real = snapshot._ensure_user_level
        calls = []

        def ensure_then_lose(root, user_id):
            level = real(root, user_id)
            calls.append(level)
            if len(calls) == 1:
                level.rmdir()
            return level

        monkeypatch.setattr(snapshot, "_ensure_user_level", ensure_then_lose)

        result = snap(repo, temp_root)

        assert len(calls) == 2
        assert result.run_dir.is_dir()

    def test_two_snapshots_get_two_directories(self, repo, temp_root):
        first = snap(repo, temp_root)
        second = snap(repo, temp_root)
        assert first.run_dir != second.run_dir

    @pytest.mark.parametrize("user_id", [".review", ".REVIEW", "../bob", "", "a/b"])
    def test_a_user_id_that_is_not_one_plain_component_is_refused(
        self, repo, temp_root, user_id
    ):
        with pytest.raises(ReviewError) as excinfo:
            snap(repo, temp_root, user_id=user_id)
        assert excinfo.value.reason == "snapshot_failed"
        assert not (temp_root / "bob").exists()

    def test_a_linked_user_level_is_refused(self, repo, temp_root, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (temp_root / ".review").mkdir(mode=0o700)
        (temp_root / ".review" / "alice").symlink_to(elsewhere)

        with pytest.raises(ReviewError):
            snap(repo, temp_root)

        assert list(elsewhere.iterdir()) == []

    def test_a_containment_refusal_keeps_its_own_reason(
        self, repo, temp_root, tmp_path
    ):
        """A `.git` pointed outside the repos root after the diff was taken is
        a request fault. Rewrapped as `snapshot_failed`, the engine would read
        it as a degraded review and carry on text-only."""
        change(repo)
        bundle = bundle_for(repo)
        outside = tmp_path / "outside"
        outside.mkdir()
        run_git(outside, "init", "-q", "-b", "main", ".")
        (outside / "f.txt").write_text("x\n")
        commit(outside, "outside")
        (repo / ".git").rename(repo.parent / "proj-git-moved")
        (repo / ".git").write_text(f"gitdir: {outside / '.git'}\n")

        with pytest.raises(ReviewError) as excinfo:
            build_snapshot(
                repo, bundle, root=temp_root, user_id="alice",
                max_bytes=BIG, max_file_bytes=BIG,
            )

        assert excinfo.value.reason == "git_dir_not_allowed"
        review_root = temp_root / ".review"
        assert not review_root.exists() or not any(review_root.rglob("run-*"))

    def test_an_unresolved_head_is_refused(self, repo, temp_root):
        bundle = bundle_for(repo)
        bundle.head = "HEAD"
        with pytest.raises(ReviewError) as excinfo:
            build_snapshot(
                repo, bundle, root=temp_root, user_id="alice",
                max_bytes=BIG, max_file_bytes=BIG,
            )
        assert excinfo.value.reason == "snapshot_failed"


class TestMeta:
    def test_the_full_patch_is_written_even_when_the_prompt_diff_is_cut(
        self, repo, temp_root
    ):
        change(repo)
        bundle = bundle_for(repo, max_chars=40)
        assert bundle.truncated

        result = build_snapshot(
            repo, bundle, root=temp_root, user_id="alice",
            max_bytes=BIG, max_file_bytes=BIG,
        )

        patch = (result.run_dir / "meta" / "diff.patch").read_text()
        expected = run_git(repo, "diff", "main...HEAD")
        assert patch == expected
        assert "def added(value)" in patch
        changed = (result.run_dir / "meta" / "changed.txt").read_text().splitlines()
        assert changed == ["pkg/app.py", "pkg/new.py"]
        assert (result.run_dir / "meta" / "stat.txt").read_text() == bundle.stat

    def test_raw_body_keeps_binary_sections_the_prompt_drops(self, repo):
        (repo / "blob.bin").write_bytes(b"\x00\x01\x02")
        commit(repo, "binary")
        bundle = bundle_for(repo)
        assert "blob.bin" in bundle.binary
        assert "blob.bin" not in bundle.body
        assert "Binary files" in bundle.raw_body

    def test_history_covers_files_that_exist_at_the_base(self, repo, temp_root):
        run_git(repo, "checkout", "-q", "main")
        (repo / "pkg" / "app.py").write_text("def existing():\n    return 2\n")
        commit(repo, "app: return two on main")
        run_git(repo, "checkout", "-q", "feature")
        run_git(repo, "merge", "-q", "--no-edit", "main")
        change(repo)

        result = snap(repo, temp_root)

        history = (result.run_dir / "meta" / "history.txt").read_text()
        assert "== pkg/app.py" in history
        lines = history.splitlines()
        assert any(line.endswith(" app: return two on main") for line in lines)
        assert any(line.endswith(" base") for line in lines)
        assert "pkg/new.py" not in history
        # Only commits before the range: the branch's own commit is in the diff.
        assert "pkg: add a helper" not in history


    def test_a_range_history_cannot_split_still_snapshots(self, repo, temp_root):
        """`HEAD^!` is a valid range with no `..` to split on; history is
        advisory, so losing it must not lose the tree."""
        change(repo)
        bundle = collect_diff(repo, resolve_range(repo, explicit="HEAD^!"), 200_000)

        result = build_snapshot(
            repo, bundle, root=temp_root, user_id="alice",
            max_bytes=BIG, max_file_bytes=BIG,
        )

        assert (result.tree_dir / "pkg" / "app.py").exists()
        history = (result.run_dir / "meta" / "history.txt").read_text()
        assert history == "[history unavailable for this range]\n"


class TestGitBatch:
    def test_a_request_larger_than_the_pipe_buffer_does_not_deadlock(self, repo):
        """Both directions past a pipe buffer at once is the deadlock case.

        A writer on the reading thread blocks on a full stdin while git blocks
        on a full stdout. Run in a thread so a regression fails here rather
        than hanging the suite.
        """
        head = run_git(repo, "rev-parse", "HEAD").strip()
        count = 40_000
        request = f"{head}\n".encode() * count  # 1.6 MB in, about 2.5 MB out
        box = {}

        def call():
            box["out"] = engine._git_batch(repo, ["cat-file", "--batch-check"], request)

        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        worker.join(timeout=60)

        assert not worker.is_alive(), "_git_batch deadlocked"
        assert box["out"].count(b"\n") == count
        assert box["out"].startswith(f"{head} commit".encode())


class TestSweep:
    @staticmethod
    def _age(path: Path, seconds: float) -> None:
        old = time.time() - seconds
        os.utime(path, (old, old), follow_symlinks=False)

    def test_stale_run_and_work_dirs_go_fresh_ones_stay(self, temp_root):
        stale = temp_root / ".review" / "alice" / "run-old"
        fresh = temp_root / ".review" / "alice" / "run-new"
        other = temp_root / ".review" / "alice" / "keep-me"
        work = temp_root / ".sandbox-work" / "alice" / "work-old"
        for directory in (stale, fresh, other, work):
            (directory / "tree").mkdir(parents=True)
            (directory / "tree" / "f").write_text("x")
        for directory in (stale, other, work):
            self._age(directory, 7200)

        assert sweep_stale_run_dirs(temp_root) == 2

        assert not stale.exists()
        assert not work.exists()
        assert fresh.exists()
        assert other.exists()

    def test_links_are_never_followed(self, temp_root, tmp_path):
        victim_root = tmp_path / "victim"
        victim = victim_root / "run-precious"
        victim.mkdir(parents=True)
        (victim / "data").write_text("keep")
        self._age(victim, 7200)
        (temp_root / ".review").mkdir()
        (temp_root / ".review" / "alice").symlink_to(victim_root)
        (temp_root / ".review" / "bob").mkdir()
        link = temp_root / ".review" / "bob" / "run-link"
        link.symlink_to(victim)
        self._age(link, 7200)

        assert sweep_stale_run_dirs(temp_root) == 0
        assert (victim / "data").read_text() == "keep"
        assert link.is_symlink()

    def test_cleanup_old_temp_files_runs_the_sweep(self, tmp_path):
        from istota.config import (
            Config,
            EmailConfig,
            NextcloudConfig,
            SchedulerConfig,
            TalkConfig,
        )
        from istota.scheduler import cleanup_old_temp_files

        config = Config(
            db_path=tmp_path / "ignore.db",
            nextcloud=NextcloudConfig(),
            talk=TalkConfig(),
            email=EmailConfig(),
            scheduler=SchedulerConfig(),
            temp_dir=tmp_path / "temp",
        )
        stale = config.temp_dir / ".review" / "alice" / "run-old"
        stale.mkdir(parents=True)
        self._age(stale, 7200)

        cleanup_old_temp_files(config, retention_days=7)

        assert not stale.exists()
        assert (config.temp_dir / ".review" / "alice").exists()

    def test_the_sweep_age_is_an_hour(self):
        assert snapshot.RUN_DIR_MAX_AGE_SECONDS == 3600


class TestAPartialClone:
    """ISSUE-615. A checkout under `developer.repos_dir` can be a partial clone,
    and in one every blob read (`git diff`, `cat-file --batch`) fetches what is
    missing from the promisor remote, through the transport its own config
    names, as the daemon user. `remote.origin.uploadpack` makes that a program
    of the repository's choosing."""

    @pytest.fixture
    def partial(self, repos_root, tmp_path):  # noqa: F811 - the imported fixture
        upstream = tmp_path / "upstream"
        upstream.mkdir()
        run_git(upstream, "init", "-q", "-b", "main", ".")
        (upstream / "app.py").write_text("def existing():\n    return 1\n")
        commit(upstream, "base")
        run_git(upstream, "checkout", "-q", "-b", "feature")
        (upstream / "app.py").write_text("def existing():\n    return 2\n")
        commit(upstream, "app: change")
        bare = tmp_path / "upstream.git"
        run_git(tmp_path, "clone", "-q", "--bare", str(upstream), str(bare))
        run_git(bare, "config", "uploadpack.allowFilter", "true")

        wt = repos_root / "proj"
        run_git(
            repos_root, "clone", "-q", "--no-checkout", "--filter=blob:none",
            f"file://{bare}", str(wt),
        )
        # The bare clone's HEAD is `feature`, so that is what was cloned to.
        run_git(wt, "branch", "-q", "main", "origin/main")

        marker = tmp_path / "upload-pack-ran"
        script = tmp_path / "upload-pack.sh"
        script.write_text(f"#!/bin/sh\ntouch '{marker}'\nexec git-upload-pack \"$@\"\n")
        script.chmod(0o755)
        run_git(wt, "config", "remote.origin.uploadpack", str(script))
        return wt, marker

    def test_reviewing_it_fetches_nothing(self, partial, temp_root):
        wt, marker = partial

        try:
            snap(wt, temp_root)
        except ReviewError:
            pass  # a missing blob may refuse the diff; the point is what did not run

        assert not marker.exists(), "the review lazily fetched through upload-pack"
