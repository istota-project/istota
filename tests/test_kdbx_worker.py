"""`credentials/kdbx_worker`: admission, the child's lifetime, and what it returns.

The admission and lifetime tests use a lightweight controlled child in place
of the real worker (`command=`), so nothing waits on a key derivation; the
parent still passes it the locked descriptor exactly as it passes the worker.
The opening tests run the real child on test-cost KeePass files.

A lock is probed from this process with a fresh open of the anchor: `flock`
conflicts between open file descriptions, in one process or several.
"""

from __future__ import annotations

import asyncio
import io
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from istota.credentials import kdbx_import, kdbx_worker
from istota.credentials.kdbx_worker import WorkerLimits
from istota.lib.file_lock import exclusive_lock

PASSWORD = "SENTINEL-worker-password-5e2b"
VALUE = "worker-fixture-value-42"
LIMITS = WorkerLimits(entries=16, depth=1, text_bytes=64 * 1024, attachment_bytes=4096)

#: A child that reports its pid, then sleeps, holding whatever it inherited.
SLEEPER = textwrap.dedent("""
    import os, sys, time
    with open(sys.argv[1], "w") as marker:
        marker.write(str(os.getpid()))
    time.sleep(float(sys.argv[2]))
""")


def _sleeper(marker: Path, seconds: float) -> list[str]:
    return [sys.executable, "-c", SLEEPER, str(marker), str(seconds)]


def _held(lock_path: Path) -> bool:
    try:
        with exclusive_lock(lock_path, timeout_seconds=0):
            return False
    except TimeoutError:
        return True


def _wait_for(predicate, seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _pid(marker: Path) -> int:
    assert _wait_for(lambda: marker.exists() and marker.read_text().strip())
    return int(marker.read_text())


@pytest.fixture
def lock_path(tmp_path):
    return tmp_path / ".config-backup-decrypt.lock"


@pytest.fixture
def cheap(monkeypatch):
    from tests.support.kdbx import _template_bytes, create_database

    _template_bytes()
    monkeypatch.setattr("pykeepass.create_database", create_database)


def _vault(*, group="Reserved", entries=1, subgroup_depth=0, attachment=b"", password=PASSWORD):
    from pykeepass import create_database

    kp = create_database(io.BytesIO(), password=password)
    root = kp.add_group(kp.root_group, group)
    for index in range(entries):
        entry = kp.add_entry(root, f"entry {index}", "", VALUE)
        entry.set_custom_property("istota.value", '"typed"', protect=True)
    if attachment:
        kp.root_group.subgroups[0].entries[0].add_attachment(kp.add_binary(attachment), "config.toml")
    parent = root
    for level in range(subgroup_depth):
        parent = kp.add_group(parent, f"level {level}")
    out = io.BytesIO()
    kp.save(out)
    return out.getvalue()


def _open(data, lock_path, **kwargs):
    return kdbx_worker.open_group(
        data, kwargs.pop("password", PASSWORD), kwargs.pop("keyfile", None),
        group=kwargs.pop("group", "Reserved"), limits=kwargs.pop("limits", LIMITS),
        lock_path=lock_path, **kwargs,
    )


class TestTheRealChild:
    def test_it_returns_the_named_group_as_plain_data(self, cheap, lock_path):
        answer = _open(_vault(attachment=b"a = 1\n"), lock_path)
        assert answer["matches"] == 1
        [entry] = answer["group"]["entries"]
        assert entry["password"] == VALUE
        assert entry["properties"] == {"istota.value": '"typed"'}
        assert entry["protected"] == ["istota.value"]
        assert entry["attachments"][0]["filename"] == "config.toml"
        assert not _held(lock_path)

    def test_no_such_group_is_no_match(self, cheap, lock_path):
        answer = _open(_vault(group="istota"), lock_path)
        assert answer == {"ok": True, "matches": 0, "group": None}

    def test_a_wrong_password_is_locked(self, cheap, lock_path):
        with pytest.raises(kdbx_import.VaultLocked):
            _open(_vault(), lock_path, password="wrong")
        assert not _held(lock_path)

    def test_bytes_that_are_not_keepass_are_corrupt(self, lock_path):
        with pytest.raises(kdbx_import.VaultCorrupt):
            _open(b"not a keepass file", lock_path)

    @pytest.mark.parametrize("limit, vault", [
        ("entries", lambda: _vault(entries=LIMITS.entries + 1)),
        ("depth", lambda: _vault(subgroup_depth=2)),
        ("attachments", lambda: _vault(attachment=b"x" * (LIMITS.attachment_bytes + 1))),
        ("text", lambda: _vault(entries=12, attachment=b"")),
    ])
    def test_each_limit_is_enforced_in_the_child(self, cheap, lock_path, limit, vault):
        limits = LIMITS
        if limit == "text":
            limits = WorkerLimits(entries=16, depth=1, text_bytes=12 * len(VALUE), attachment_bytes=4096)
        with pytest.raises(kdbx_worker.WorkerTooLarge) as caught:
            _open(vault(), lock_path, limits=limits)
        assert caught.value.limit == limit

    def test_the_limits_at_their_maximum_pass(self, cheap, lock_path):
        answer = _open(_vault(entries=LIMITS.entries - 1, subgroup_depth=1,
                              attachment=b"x" * LIMITS.attachment_bytes), lock_path)
        assert len(answer["group"]["entries"]) == LIMITS.entries - 1
        assert len(answer["group"]["groups"]) == 1

    def test_the_child_ends_itself_at_its_deadline(self):
        # Never fed: only its own alarm ends it, as for an orphan whose parent died.
        child = subprocess.Popen(
            [*kdbx_worker.default_command(1.0)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        )
        try:
            assert child.wait(timeout=20) == -signal.SIGALRM
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdin.close()

    def test_the_child_environment_carries_nothing_of_the_parent(self, monkeypatch):
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "parent-secret-43")
        assert "ISTOTA_SECRET_KEY" not in kdbx_worker._child_env()


class TestTheParentsBounds:
    def test_output_past_the_bound_is_refused(self, lock_path):
        command = [sys.executable, "-c", "import sys; sys.stdin.buffer.read(); sys.stdout.write('x' * 10**6)"]
        small = WorkerLimits(entries=0, depth=0, text_bytes=0, attachment_bytes=0)
        with pytest.raises(kdbx_worker.WorkerTooLarge) as caught:
            _open(b"x", lock_path, limits=small, command=command)
        assert caught.value.limit == "output"
        assert not _held(lock_path)

    def test_a_malformed_answer_fails_and_releases(self, lock_path):
        command = [sys.executable, "-c", "import sys; sys.stdin.buffer.read(); sys.stdout.write('{nope')"]
        with pytest.raises(kdbx_worker.WorkerFailed):
            _open(b"x", lock_path, command=command)
        assert not _held(lock_path)

    def test_a_failed_spawn_fails_and_releases(self, lock_path, tmp_path):
        with pytest.raises(kdbx_worker.WorkerFailed):
            _open(b"x", lock_path, command=[str(tmp_path / "no-such-program")])
        assert not _held(lock_path)

    def test_a_child_past_the_deadline_is_killed_reaped_and_released(self, lock_path, tmp_path):
        marker = tmp_path / "pid"
        started = time.monotonic()
        with pytest.raises(kdbx_worker.WorkerTimeout):
            _open(b"x", lock_path, timeout=1.0, command=_sleeper(marker, 60))
        assert time.monotonic() - started < 15
        assert not _alive(_pid(marker))
        assert not _held(lock_path)

    def test_the_bound_covers_the_worst_escaping(self):
        assert LIMITS.output_bytes() >= 6 * LIMITS.text_bytes + 4 * LIMITS.attachment_bytes // 3


class TestAdmission:
    def test_a_held_slot_is_busy_at_once_and_spawns_nothing(self, lock_path, tmp_path):
        marker = tmp_path / "spawned"
        with exclusive_lock(lock_path, timeout_seconds=0, nofollow=True):
            started = time.monotonic()
            with pytest.raises(kdbx_worker.WorkerBusy):
                _open(b"x", lock_path, command=_sleeper(marker, 0))
            assert time.monotonic() - started < 1
        assert not marker.exists()

    @pytest.mark.parametrize("shape", ["symlink", "directory", "missing_parent"])
    def test_an_unusable_anchor_is_unavailable_never_unadmitted(self, tmp_path, shape):
        marker = tmp_path / "spawned"
        anchor = tmp_path / "anchor"
        if shape == "symlink":
            anchor.symlink_to(tmp_path / "elsewhere")
        elif shape == "directory":
            anchor.mkdir()
        else:
            anchor = tmp_path / "absent" / "anchor"
        with pytest.raises(kdbx_worker.WorkerUnavailable):
            _open(b"x", anchor, command=_sleeper(marker, 0))
        assert not marker.exists()
        if shape == "symlink":
            assert not (tmp_path / "elsewhere").exists()

    def test_a_second_process_is_refused_while_one_decrypts(self, lock_path, tmp_path):
        first, second = tmp_path / "first", tmp_path / "second"
        outcome = {}

        def run():
            try:
                _open(b"x", lock_path, command=_sleeper(first, 3))
            except kdbx_worker.WorkerError as exc:
                outcome["first"] = exc.code

        thread = threading.Thread(target=run)
        thread.start()
        try:
            _pid(first)
            script = textwrap.dedent(f"""
                import sys
                from pathlib import Path
                from istota.credentials import kdbx_worker
                from istota.credentials.kdbx_worker import WorkerLimits
                try:
                    kdbx_worker.open_group(
                        b"x", "p", None, group="g", limits=WorkerLimits(1, 1, 1, 1),
                        lock_path=Path({str(lock_path)!r}),
                        command=[sys.executable, "-c", "open({str(second)!r}, 'w')"],
                    )
                except kdbx_worker.WorkerBusy:
                    print("busy")
            """)
            other = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
            assert other.stdout.strip() == "busy", other.stderr
            assert not second.exists()
        finally:
            thread.join(30)
        # The controlled child answers nothing, which is a failure, after it exits.
        assert outcome["first"] == "failed"
        assert not _held(lock_path)

    async def test_cancelling_the_caller_holds_the_slot_until_the_child_is_reaped(self, lock_path, tmp_path):
        marker = tmp_path / "pid"
        task = asyncio.create_task(kdbx_worker.open_group_async(
            b"x", PASSWORD, None, group="g", limits=LIMITS, lock_path=lock_path,
            command=_sleeper(marker, 2),
        ))
        pid = await asyncio.to_thread(_pid, marker)
        task.cancel()
        results = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert _alive(pid)
        assert _held(lock_path)
        assert await asyncio.to_thread(_wait_for, lambda: not _held(lock_path))
        # Released only once the thread had reaped the child.
        assert not _alive(pid)

    def test_the_slot_outlives_a_parent_killed_mid_decrypt(self, lock_path, tmp_path):
        marker = tmp_path / "pid"
        script = textwrap.dedent(f"""
            import sys
            from pathlib import Path
            from istota.credentials import kdbx_worker
            from istota.credentials.kdbx_worker import WorkerLimits
            kdbx_worker.open_group(
                b"x", "p", None, group="g", limits=WorkerLimits(1, 1, 1, 1),
                lock_path=Path({str(lock_path)!r}),
                command=[sys.executable, "-c", {SLEEPER!r}, {str(marker)!r}, "3"],
            )
        """)
        parent = subprocess.Popen([sys.executable, "-c", script])
        try:
            child = _pid(marker)
            parent.kill()
            parent.wait(10)
            # The parent's descriptor closed with it; the child's copy holds the slot.
            assert _alive(child)
            assert _held(lock_path)
            assert _wait_for(lambda: not _held(lock_path), 30)
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait()

    def test_the_slot_is_free_after_a_successful_open(self, cheap, lock_path):
        _open(_vault(), lock_path)
        assert not _held(lock_path)
        _open(_vault(), lock_path)
