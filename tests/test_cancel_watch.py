"""A cancel flagged where no signal can be sent still stops the worker (ISSUE-812).

On the Docker stack the web app is another container with its own PID
namespace, so a group removal (`groups.rooms.stop_room_tasks`) or a web cancel
can only set ``cancel_requested``: `signal_task_worker` refuses there. The
`claude` CLI reads that flag on its next event, or at exit for the
non-streaming child, so a long tool call ran on to completion. The worker's own
process now watches the flag while the attempt runs and signals the recorded
``worker_pid`` itself.

These run a real child through ClaudeCodeBrain's two spawn paths, with the pid
recorded the way the executor's `on_pid` records it. Nothing is mocked: the
child sleeps for a minute, and the test holds the stop to a bound well short
of that.
"""

import os
import signal
import sys
import threading
import time

import pytest

from istota import db, scheduler
from istota.brain import BrainRequest
from istota.brain.claude_code import ClaudeCodeBrain
from istota.config import (
    Config, EmailConfig, NextcloudConfig, SchedulerConfig, TalkConfig, UserConfig,
)
from istota.executor import _make_cancel_check
from istota.groups import rooms as group_rooms
from istota.scheduler import _task_heartbeat

# The child outlives every bound below by a wide margin, so a pass is the
# watcher's doing and never the child finishing on its own.
_CHILD = [sys.executable, "-c", "import time; time.sleep(60)"]
_STOP_BOUND_SECONDS = 15.0


@pytest.fixture(autouse=True)
def _outside_the_scheduler_process(monkeypatch):
    """The web container: this process never ran `run_daemon`."""
    from istota.sandbox import process_group
    monkeypatch.setattr(process_group, "_task_pids_are_local", False)


def _config(tmp_path, *, heartbeat=60):
    db_path = tmp_path / "cancel.db"
    db.init_db(db_path)
    config = Config(
        db_path=db_path,
        nextcloud=NextcloudConfig(),
        talk=TalkConfig(),
        email=EmailConfig(),
        scheduler=SchedulerConfig(worker_heartbeat_seconds=heartbeat),
        temp_dir=tmp_path / "temp",
    )
    config.users = {"alice": UserConfig()}
    return config


def _running_task(config, token="room-g"):
    with db.get_db(config.db_path) as conn:
        task_id = db.create_task(
            conn, prompt="hi", user_id="alice", source_type="web",
            conversation_token=token,
        )
        db.update_task_status(conn, task_id, "running")
    return task_id


def _req(config, task_id, tmp_path):
    def _on_pid(pid):
        with db.get_db(config.db_path) as conn:
            db.update_task_pid(conn, task_id, pid)

    return BrainRequest(
        prompt="hi", allowed_tools=["Bash"], cwd=tmp_path, env={},
        timeout_seconds=120, model="", effort="",
        on_pid=_on_pid, cancel_check=_make_cancel_check(config, task_id),
    )


def _worker_pid(config, task_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with db.get_db(config.db_path) as conn:
            row = conn.execute(
                "SELECT worker_pid FROM tasks WHERE id = ?", (task_id,),
            ).fetchone()
        if row["worker_pid"]:
            return int(row["worker_pid"])
        time.sleep(0.05)
    raise AssertionError("the brain never recorded a worker pid")


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _run_brain(config, task_id, tmp_path, spawn):
    """Run one attempt the way `process_one_task` does, on a thread."""
    out = {}

    def _body():
        with _task_heartbeat(config, task_id):
            out["result"] = spawn(_CHILD, _req(config, task_id, tmp_path))
        out["done_at"] = time.monotonic()

    thread = threading.Thread(target=_body, daemon=True)
    thread.start()
    return thread, out


def _remove_from_group(config):
    """What the Groups routes do in the web container on a member removal."""
    with db.get_db(config.db_path) as conn:
        pids = group_rooms.stop_room_tasks(conn, "room-g", "alice")
        conn.commit()
    group_rooms.signal_stopped_tasks(pids)
    return pids


def _web_cancel(config, task_id):
    import istota.webui.app as mod
    original = mod._config
    mod._config = config
    try:
        mod._chat_cancel_task(task_id, "alice")
    finally:
        mod._config = original


_SPAWNS = {
    "streaming": ClaudeCodeBrain._execute_streaming_once,
    "non-streaming": ClaudeCodeBrain._execute_simple_once,
}


@pytest.mark.parametrize("path", sorted(_SPAWNS))
def test_a_removal_from_the_web_process_stops_the_running_worker(tmp_path, path):
    config = _config(tmp_path)
    task_id = _running_task(config)
    thread, out = _run_brain(config, task_id, tmp_path, _SPAWNS[path])
    pid = _worker_pid(config, task_id)
    try:
        assert _remove_from_group(config) == [pid]
        flagged_at = time.monotonic()

        thread.join(_STOP_BOUND_SECONDS)

        assert not thread.is_alive(), (
            f"the {path} worker ran on after its cancel was flagged"
        )
        assert out["result"].stop_reason == "cancelled"
        assert out["done_at"] - flagged_at < _STOP_BOUND_SECONDS
        assert not _alive(pid)
    finally:
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)
        thread.join(5)


def test_a_web_cancel_stops_the_running_worker(tmp_path):
    config = _config(tmp_path)
    task_id = _running_task(config)
    thread, out = _run_brain(
        config, task_id, tmp_path, ClaudeCodeBrain._execute_streaming_once,
    )
    pid = _worker_pid(config, task_id)
    try:
        _web_cancel(config, task_id)

        thread.join(_STOP_BOUND_SECONDS)

        assert not thread.is_alive(), "the worker ran on after a web cancel"
        assert out["result"].stop_reason == "cancelled"
    finally:
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)
        thread.join(5)


def test_the_watch_runs_with_the_heartbeat_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "_CANCEL_POLL_SECONDS", 0.05)
    config = _config(tmp_path, heartbeat=0)
    task_id = _running_task(config)
    thread, out = _run_brain(
        config, task_id, tmp_path, ClaudeCodeBrain._execute_streaming_once,
    )
    pid = _worker_pid(config, task_id)
    try:
        _remove_from_group(config)
        thread.join(_STOP_BOUND_SECONDS)
        assert not thread.is_alive()
        assert out["result"].stop_reason == "cancelled"
    finally:
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)
        thread.join(5)


def test_an_uncancelled_worker_is_left_running(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "_CANCEL_POLL_SECONDS", 0.05)
    config = _config(tmp_path)
    task_id = _running_task(config)
    thread, _out = _run_brain(
        config, task_id, tmp_path, ClaudeCodeBrain._execute_streaming_once,
    )
    pid = _worker_pid(config, task_id)
    try:
        time.sleep(1.0)  # twenty polls
        assert thread.is_alive()
        assert _alive(pid)
    finally:
        os.kill(pid, signal.SIGKILL)
        thread.join(5)


def test_a_pid_whose_holder_changed_is_not_signalled(tmp_path, monkeypatch):
    # The row keeps a child's pid after the brain reaps it (an API retry
    # backoff, an in-process `!stop`). If the number is reused, the start
    # time pinned at first sight no longer matches and nothing is signalled.
    import subprocess

    from istota.sandbox import peer_process

    monkeypatch.setattr(scheduler, "_CANCEL_POLL_SECONDS", 0.05)
    config = _config(tmp_path)
    task_id = _running_task(config)
    holder = subprocess.Popen(_CHILD, start_new_session=True)
    try:
        with _task_heartbeat(config, task_id):
            with db.get_db(config.db_path) as conn:
                db.update_task_pid(conn, task_id, holder.pid)
            time.sleep(0.5)  # the watch has seen and pinned the pid
            real = peer_process.start_time
            monkeypatch.setattr(
                peer_process, "start_time", lambda pid, **kw: (real(pid, **kw) or 0) + 1,
            )
            with db.get_db(config.db_path) as conn:
                conn.execute(
                    "UPDATE tasks SET cancel_requested = 1 WHERE id = ?", (task_id,),
                )
                conn.commit()
            time.sleep(0.5)
        assert holder.poll() is None, "signalled a pid now held by another process"
    finally:
        holder.kill()
        holder.wait()
