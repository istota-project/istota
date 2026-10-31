"""Exercise host skill reads while a separate process replaces a directory."""

import json
import multiprocessing
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

import pytest

from istota import executor
from istota.sandbox.host_paths import write_resolved
from istota.sandbox.skill_proxy import SkillProxy
from istota.skills.code_review import engine
from tests.test_code_review_engine import commit, run_git

pytestmark = [pytest.mark.linux, pytest.mark.skipif(sys.platform != "linux", reason="Linux witness")]


def test_git_directory_swap_after_validation_cannot_read_outside(tmp_path, monkeypatch):
    root = tmp_path / "repos" / "alice"
    root.mkdir(parents=True)
    monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(root))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    repositories = [root / "project", tmp_path / "private"]
    for repo in repositories:
        repo.mkdir()
        run_git(repo, "init", "-q", "-b", "main")
        (repo / "file.txt").write_text("base")
        commit(repo, "base")
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "file.txt").write_text("outside-private-canary" if repo == repositories[1] else "approved")
        commit(repo, "change")
    worktree, outside = repositories
    assert "approved" in engine.collect_diff(worktree, "main..HEAD", 10000).body
    original = engine.git_dir

    def validate_then_swap(path):
        result = original(path)
        (path / ".git").rename(path / ".git.saved")
        (path / ".git").symlink_to(outside / ".git", target_is_directory=True)
        return result

    monkeypatch.setattr(engine, "git_dir", validate_then_swap)
    with pytest.raises(engine.ReviewError):
        engine.collect_diff(worktree, "main..HEAD", 10000)


@pytest.fixture
def repositories(tmp_path, monkeypatch):
    """A worktree under the user's repos root, and a repository outside it."""
    root = tmp_path / "repos" / "alice"
    root.mkdir(parents=True)
    monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(root))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    worktree, outside = root / "project", tmp_path / "private"
    for repo, changed in ((worktree, "approved"), (outside, "outside-private-canary")):
        repo.mkdir()
        run_git(repo, "init", "-q", "-b", "main")
        (repo / "file.txt").write_text("base")
        commit(repo, "base")
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "file.txt").write_text(changed)
        commit(repo, "change")
    return worktree, outside


def _link_objects_and_refs(worktree, outside):
    for name in ("objects", "refs"):
        shutil.rmtree(worktree / ".git" / name)
        (worktree / ".git" / name).symlink_to(outside / ".git" / name, target_is_directory=True)


def _name_outside_objects(worktree, outside):
    info = worktree / ".git" / "objects" / "info"
    info.mkdir(exist_ok=True)
    (info / "alternates").write_text(f"{outside / '.git' / 'objects'}\n")
    for branch in ("main", "feature"):
        (worktree / ".git" / "refs" / "heads" / branch).write_text(
            run_git(outside, "rev-parse", branch).strip() + "\n"
        )


@pytest.mark.parametrize("plant", [_link_objects_and_refs, _name_outside_objects])
def test_a_planted_git_directory_cannot_read_outside(repositories, plant):
    """No race: `git_dir` passes these, since both directories it asks about
    stay inside the root and only their contents point away."""
    worktree, outside = repositories
    plant(worktree, outside)

    assert engine.git_dir(worktree) == (worktree / ".git").resolve()
    with pytest.raises(engine.ReviewError):
        engine.collect_diff(worktree, "main..HEAD", 10000)


@pytest.mark.parametrize("plant", [_link_objects_and_refs, _name_outside_objects])
def test_the_same_plant_reads_outside_without_the_namespace(repositories, plant, monkeypatch):
    """The control: the refusals above are the namespace's, not a broken repo's."""
    worktree, outside = repositories
    plant(worktree, outside)
    monkeypatch.setattr(executor, "_bwrap_available", lambda: False)

    assert "outside-private-canary" in engine.collect_diff(worktree, "main..HEAD", 10000).body


def test_a_linked_worktree_of_a_bare_clone_in_the_root_is_reviewed(repositories):
    """The layout the developer skill builds, whose `.git` file and `commondir`
    name other directories in the subtree by absolute path."""
    worktree, _ = repositories
    root = worktree.parent
    bare = root / "group" / "project.git"
    bare.parent.mkdir()
    run_git(root, "clone", "-q", "--bare", str(worktree), str(bare))
    linked = root / "group" / "project-wt"
    run_git(bare, "worktree", "add", "-q", str(linked), "feature")

    bundle = engine.collect_diff(linked, "main..HEAD", 10000)

    assert "approved" in bundle.body
    assert engine.git_dir(linked).is_relative_to(bare.resolve())


def test_a_namespace_that_cannot_start_is_not_no_match(repositories):
    """bwrap's own failure exits 1, the code `git grep` uses for no match."""
    worktree, _ = repositories

    with pytest.raises(engine.ReviewError):
        engine._git(worktree / "missing", ["grep", "-e", "x"], allow_codes=(0, 1))


def _replace_parent(parent, outside, stop, ready, cycles):
    import ctypes

    alternate = parent.with_name("alternate")
    alternate.symlink_to(outside, target_is_directory=True)
    libc = ctypes.CDLL(None, use_errno=True)

    def exchange():
        # RENAME_EXCHANGE keeps the name present while alternating its type.
        if libc.renameat2(-100, os.fsencode(parent), -100, os.fsencode(alternate), 2):
            raise OSError(ctypes.get_errno(), "renameat2 failed")

    try:
        while not stop.is_set():
            exchange()
            ready.set()
            exchange()
            with cycles.get_lock():
                cycles.value += 1
    finally:
        alternate.unlink(missing_ok=True)


def _request(socket_path, source):
    request = {"skill": "email", "args": ["output", "--body-file", str(source)]}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(30)
        client.connect(str(socket_path))
        client.sendall(json.dumps(request).encode() + b"\n")
        with client.makefile("r") as stream:
            return json.loads(stream.readline())


def test_real_proxy_and_concurrent_replacer_preserve_outside_canaries(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    parent = allowed / "inputs"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (parent / "body.txt").write_text("approved body")
    (parent / "existing.txt").write_text("old content")
    (outside / "body.txt").write_text("outside-private-canary")
    (outside / "existing.txt").write_text("outside-write-canary")
    result_path = allowed / "task_123_email_output.json"
    env = {
        "PATH": os.environ["PATH"],
        "ISTOTA_DEFERRED_DIR": str(allowed),
        "ISTOTA_TASK_ID": "123",
    }
    context = multiprocessing.get_context("spawn")
    stop, ready = context.Event(), context.Event()
    cycles = context.Value("i", 0)
    replacer = context.Process(target=_replace_parent, args=(parent, outside, stop, ready, cycles))

    with tempfile.TemporaryDirectory(prefix="hp-race-", dir="/tmp") as sockets:
        socket_path = Path(sockets) / "s.sock"
        with SkillProxy(socket_path, {}, env, allowed_skills=frozenset({"email"}),
                        trusted_roots={os.getpid()}):
            # Prove the proxy actually executes the skill before starting the race.
            assert _request(socket_path, parent / "body.txt")["returncode"] == 0
            assert json.loads(result_path.read_text())["body"] == "approved body"
            replacer.start()
            try:
                assert ready.wait(10), "replacer did not start"
                for _ in range(30):
                    result_path.unlink(missing_ok=True)
                    response = _request(socket_path, parent / "body.txt")
                    assert "outside-private-canary" not in response["stdout"]
                    if result_path.exists():
                        assert json.loads(result_path.read_text())["body"] == "approved body"
                    for name in ("new.txt", "existing.txt"):
                        try:
                            write_resolved(parent / name, b"approved replacement")
                        except OSError:
                            pass
                    assert not (outside / "new.txt").exists()
                    assert (outside / "existing.txt").read_text() == "outside-write-canary"
            finally:
                stop.set()
                replacer.join(10)
                if replacer.is_alive():
                    replacer.kill()
                    replacer.join()
            assert replacer.exitcode == 0
            assert cycles.value > 0
            assert _request(socket_path, parent / "body.txt")["returncode"] == 0
            assert json.loads(result_path.read_text())["body"] == "approved body"
