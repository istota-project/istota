"""ISSUE-587: the tmux brain's per-attempt workdir is task-writable.

`{user_temp_dir}` is bound read-write into every sandbox of that user and the
workdir name is predictable from the task id, so a task can plant a directory,
a symlink or a file there before a later task runs. The daemon writes and reads
in that directory unsandboxed, so each test here plants something and asserts
the daemon neither follows it nor adopts it.
"""

import json
import os
from pathlib import Path

import pytest

from istota.brain._types import BrainRequest
from istota.brain.tmux_claude import (
    TmuxClaudeBrain,
    _SessionDir,
    reset_circuit_breaker,
)

PLANTED = ".tmux-istota-41-0-planted"


@pytest.fixture(autouse=True)
def _reset_breaker(monkeypatch):
    import istota.brain.tmux_claude as mod
    monkeypatch.setattr(mod, "_VERSION_CHECKED", True)
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/tmux")
    reset_circuit_breaker()
    yield
    reset_circuit_breaker()


class _CP:
    stdout = ""
    returncode = 0


def _req(tmp_path, deferred, **kw):
    base = dict(
        prompt="the user half of the prompt",
        allowed_tools=[],
        cwd=tmp_path,
        env={"ISTOTA_DEFERRED_DIR": str(deferred)},
        timeout_seconds=60,
        session_label="istota-41-0",
    )
    base.update(kw)
    return BrainRequest(**base)


def _fixed_name(monkeypatch):
    """Pin the random tail so a test can plant at the name the brain will use;
    the real name is not predictable from inside a sandbox."""
    import istota.brain.tmux_claude as mod
    monkeypatch.setattr(mod, "_workdir_name", lambda session: PLANTED)


def _record_launch(monkeypatch, brain):
    calls = []
    for m in ("_new_session", "_launch_claude", "_inject_prompt", "_kill"):
        monkeypatch.setattr(
            brain, m, lambda *a, _m=m, **k: calls.append(_m),
        )
    monkeypatch.setattr(brain, "_pane_pid", lambda *a: 4242)
    monkeypatch.setattr(brain, "_wait_ready", lambda *a: True)
    return calls


class TestAPlantedWorkdirIsNeverAdopted:
    def test_a_symlinked_workdir_fails_the_attempt(self, monkeypatch, tmp_path):
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        victim = elsewhere / "prompt.txt"
        victim.write_text("untouched")
        (deferred / PLANTED).symlink_to(elsewhere)
        _fixed_name(monkeypatch)

        brain = TmuxClaudeBrain()
        calls = _record_launch(monkeypatch, brain)
        res = brain.execute(_req(tmp_path, deferred))

        assert res.success is False
        assert res.stop_reason == "error"
        assert victim.read_text() == "untouched"
        assert sorted(p.name for p in elsewhere.iterdir()) == ["prompt.txt"]
        assert "_new_session" not in calls
        # The planted link is left alone, not followed into by a cleanup.
        assert (deferred / PLANTED).is_symlink()

    def test_a_pre_created_workdir_fails_the_attempt(self, monkeypatch, tmp_path):
        deferred = tmp_path / "deferred"
        planted = deferred / PLANTED / "config"
        planted.mkdir(parents=True)
        (planted / "CLAUDE.md").write_text("planted instructions")
        _fixed_name(monkeypatch)

        brain = TmuxClaudeBrain()
        calls = _record_launch(monkeypatch, brain)
        res = brain.execute(_req(tmp_path, deferred))

        assert res.success is False
        assert "_new_session" not in calls
        assert (planted / "CLAUDE.md").read_text() == "planted instructions"
        assert not (planted / "settings.json").exists()

    def test_a_file_planted_in_the_fresh_config_dir_fails_the_attempt(
        self, monkeypatch, tmp_path,
    ):
        """A concurrent same-user task can still write into the directory the
        daemon just made. The daemon's own writes refuse to land on anything
        already there rather than following or truncating it."""
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        victim = tmp_path / "victim.json"
        victim.write_text("untouched")

        brain = TmuxClaudeBrain()
        calls = _record_launch(monkeypatch, brain)
        orig_write_hooks = TmuxClaudeBrain._write_hooks

        def plant_then_write(config_dir, sentinel, started):
            (config_dir / "settings.json").symlink_to(victim)
            orig_write_hooks(config_dir, sentinel, started)

        monkeypatch.setattr(brain, "_write_hooks", plant_then_write)
        res = brain.execute(_req(tmp_path, deferred))

        assert res.success is False
        assert victim.read_text() == "untouched"
        assert "_new_session" not in calls

    def test_the_workdir_is_private_and_removed_after_the_run(
        self, monkeypatch, tmp_path,
    ):
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        brain = TmuxClaudeBrain()
        _record_launch(monkeypatch, brain)
        seen = {}

        def fake_wait(name, sentinel, deadline, cancel_check):
            workdir = Path(sentinel).parent
            seen["mode"] = workdir.stat().st_mode & 0o777
            seen["config_mode"] = (workdir / "config").stat().st_mode & 0o777
            seen["settings_mode"] = (
                (workdir / "config" / "settings.json").stat().st_mode & 0o777
            )
            Path(sentinel).write_text(json.dumps({"last_assistant_message": "ok"}))
            return ("done", "")

        monkeypatch.setattr(brain, "_wait_for_completion", fake_wait)
        monkeypatch.setattr(brain, "_learn_transcript_path", lambda *a: None)
        res = brain.execute(_req(tmp_path, deferred))

        assert res.success is True
        assert seen == {"mode": 0o700, "config_mode": 0o700, "settings_mode": 0o600}
        assert not list(deferred.glob(".tmux-*"))

    def test_the_name_carries_an_unpredictable_tail(self):
        import istota.brain.tmux_claude as mod
        a = mod._workdir_name("istota-41-0")
        b = mod._workdir_name("istota-41-0")
        assert a.startswith(".tmux-istota-41-0-")
        assert a != b


class TestThePromptNeverTouchesTheWorkdir:
    def test_no_prompt_file_is_written(self, monkeypatch, tmp_path):
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        brain = TmuxClaudeBrain()
        _record_launch(monkeypatch, brain)
        listings = []

        def fake_wait(name, sentinel, deadline, cancel_check):
            workdir = Path(sentinel).parent
            listings.extend(p.name for p in workdir.iterdir())
            Path(sentinel).write_text(json.dumps({"last_assistant_message": "ok"}))
            return ("done", "")

        monkeypatch.setattr(brain, "_wait_for_completion", fake_wait)
        monkeypatch.setattr(brain, "_learn_transcript_path", lambda *a: None)
        brain.execute(_req(tmp_path, deferred))

        assert "prompt.txt" not in listings

    def test_inject_loads_the_buffer_from_stdin(self, monkeypatch, tmp_path):
        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod, "_SUBMIT_SETTLE_S", 0.0)
        monkeypatch.setattr(mod, "_READY_POLL_S", 0.0)
        brain = TmuxClaudeBrain()
        tmux_calls = []
        monkeypatch.setattr(brain, "_tmux", lambda *a: tmux_calls.append(a) or _CP())
        loaded = []
        monkeypatch.setattr(
            brain, "_load_buffer", lambda buf, text: loaded.append((buf, text)),
        )
        monkeypatch.setattr(brain, "_turn_started", lambda s: True)

        brain._inject_prompt("s", "the prompt text", tmp_path / "started.json")

        assert loaded == [("istota-s", "the prompt text")]
        assert not any(a[:1] == ("load-buffer",) for a in tmux_calls)

    def test_load_buffer_passes_the_text_on_stdin(self, monkeypatch):
        import istota.brain.tmux_claude as mod
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            seen["input"] = kw.get("input")
            return _CP()

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        TmuxClaudeBrain()._load_buffer("istota-s", "hello\nworld")
        assert seen["argv"] == ["tmux", "load-buffer", "-b", "istota-s", "-"]
        assert seen["input"] == "hello\nworld"


class TestTheDaemonReadsOnlyWhatTheSessionWrote:
    def _workdir(self, tmp_path, name=".tmux-s"):
        workdir = tmp_path / name
        (workdir / "config" / "projects" / "p").mkdir(parents=True)
        return workdir

    def _transcript(self, path, text):
        path.write_text(json.dumps({
            "type": "assistant", "uuid": "u1",
            "message": {"id": "m1", "role": "assistant", "model": "m",
                        "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": text}]},
        }) + "\n")
        return path

    def test_a_transcript_outside_the_config_dir_is_not_read(self, tmp_path):
        workdir = self._workdir(tmp_path)
        foreign = self._transcript(tmp_path / "other-user.jsonl", "FOREIGN")
        sentinel = workdir / "stop.json"
        sentinel.write_text(json.dumps({
            "transcript_path": str(foreign), "last_assistant_message": "ok",
        }))
        req = BrainRequest(
            prompt="p", allowed_tools=[], cwd=tmp_path, env={}, timeout_seconds=60,
        )
        res = TmuxClaudeBrain()._build_result(
            _SessionDir(workdir), req, forward_progress=False,
        )
        assert res.result_text == "ok"
        assert "FOREIGN" not in (res.execution_trace or "")

    def test_a_symlink_inside_the_config_dir_is_resolved_before_the_check(
        self, tmp_path,
    ):
        workdir = self._workdir(tmp_path)
        foreign = self._transcript(tmp_path / "other-user.jsonl", "FOREIGN")
        link = workdir / "config" / "projects" / "p" / "t.jsonl"
        link.symlink_to(foreign)
        sentinel = workdir / "stop.json"
        sentinel.write_text(json.dumps({
            "transcript_path": str(link), "last_assistant_message": "ok",
        }))
        req = BrainRequest(
            prompt="p", allowed_tools=[], cwd=tmp_path, env={}, timeout_seconds=60,
        )
        res = TmuxClaudeBrain()._build_result(
            _SessionDir(workdir), req, forward_progress=False,
        )
        assert "FOREIGN" not in (res.execution_trace or "")

    def test_the_sessions_own_transcript_is_read(self, tmp_path):
        workdir = self._workdir(tmp_path)
        own = self._transcript(
            workdir / "config" / "projects" / "p" / "t.jsonl", "OWN",
        )
        sentinel = workdir / "stop.json"
        sentinel.write_text(json.dumps({
            "transcript_path": str(own), "last_assistant_message": "ok",
        }))
        req = BrainRequest(
            prompt="p", allowed_tools=[], cwd=tmp_path, env={}, timeout_seconds=60,
        )
        res = TmuxClaudeBrain()._build_result(
            _SessionDir(workdir), req, forward_progress=False,
        )
        assert "OWN" in (res.execution_trace or "")

    def test_a_symlinked_sentinel_is_not_followed(self, tmp_path):
        workdir = self._workdir(tmp_path)
        elsewhere = tmp_path / "some.json"
        elsewhere.write_text(json.dumps({"last_assistant_message": "FOREIGN"}))
        sentinel = workdir / "stop.json"
        sentinel.symlink_to(elsewhere)
        req = BrainRequest(
            prompt="p", allowed_tools=[], cwd=tmp_path, env={}, timeout_seconds=60,
        )
        res = TmuxClaudeBrain()._build_result(
            _SessionDir(workdir), req, forward_progress=False,
        )
        assert res.success is False
        assert "FOREIGN" not in res.result_text

    def test_the_tailer_fallback_never_globs_the_daemon_home(
        self, monkeypatch, tmp_path,
    ):
        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod, "_STARTED_SENTINEL_WAIT_S", 0.0)
        home = tmp_path / "home"
        daemon_projects = home / ".claude" / "projects" / "x"
        daemon_projects.mkdir(parents=True)
        (daemon_projects / "someone-else.jsonl").write_text("{}\n")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

        workdir = self._workdir(tmp_path)
        own = workdir / "config" / "projects" / "p" / "own.jsonl"
        own.write_text("{}\n")
        got = TmuxClaudeBrain()._learn_transcript_path(_SessionDir(workdir))
        assert got == own.resolve()

    def test_the_started_sentinel_path_is_confined_too(self, monkeypatch, tmp_path):
        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod, "_STARTED_SENTINEL_WAIT_S", 0.05)
        monkeypatch.setattr(mod, "_SENTINEL_POLL_S", 0.0)
        workdir = self._workdir(tmp_path)
        foreign = tmp_path / "other-user.jsonl"
        foreign.write_text("{}\n")
        started = workdir / "started.json"
        started.write_text(json.dumps({"transcript_path": str(foreign)}))
        got = TmuxClaudeBrain()._learn_transcript_path(_SessionDir(workdir))
        assert got is None


class TestASwappedWorkdirIsNotFollowed:
    """The session's own model can rename its workdir and leave a link to
    another session's directory at the name before that session's Stop hook
    fires. The daemon pinned the directory at creation and reads through that."""

    def _other_session(self, tmp_path):
        bob = tmp_path / "bob" / ".tmux-istota-77-0-x"
        projects = bob / "config" / "projects" / "p"
        projects.mkdir(parents=True)
        transcript = projects / "t.jsonl"
        transcript.write_text(json.dumps({
            "type": "assistant", "uuid": "u1",
            "message": {"id": "m1", "role": "assistant", "model": "m",
                        "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "BOB-SECRET-TRACE"}]},
        }) + "\n")
        (bob / "stop.json").write_text(json.dumps({
            "transcript_path": str(transcript),
            "last_assistant_message": "BOB-SECRET-ANSWER",
        }))
        (bob / "started.json").write_text(json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "transcript_path": str(transcript),
        }))
        return bob

    def _swap(self, alice, bob):
        alice.rename(alice.with_name(alice.name + ".moved"))
        alice.symlink_to(bob)

    def _alice(self, tmp_path):
        alice = tmp_path / "alice" / ".tmux-istota-41-0-y"
        (alice / "config").mkdir(parents=True)
        return alice

    def test_the_stop_sentinel_is_read_from_the_pinned_directory(self, tmp_path):
        bob = self._other_session(tmp_path)
        alice = self._alice(tmp_path)
        session_dir = _SessionDir(alice)
        self._swap(alice, bob)

        req = BrainRequest(
            prompt="p", allowed_tools=[], cwd=tmp_path, env={}, timeout_seconds=60,
        )
        res = TmuxClaudeBrain()._build_result(
            session_dir, req, forward_progress=False,
        )
        assert "BOB" not in res.result_text
        assert "BOB" not in (res.execution_trace or "")

    def test_a_transcript_reached_through_the_swapped_name_is_refused(
        self, tmp_path,
    ):
        bob = self._other_session(tmp_path)
        alice = self._alice(tmp_path)
        session_dir = _SessionDir(alice)
        self._swap(alice, bob)
        # Alice's own (moved) directory gets a sentinel naming the transcript
        # through her original path, which now resolves into bob's.
        moved = alice.with_name(alice.name + ".moved")
        (moved / "stop.json").write_text(json.dumps({
            "transcript_path": str(alice / "config" / "projects" / "p" / "t.jsonl"),
            "last_assistant_message": "ok",
        }))

        req = BrainRequest(
            prompt="p", allowed_tools=[], cwd=tmp_path, env={}, timeout_seconds=60,
        )
        res = TmuxClaudeBrain()._build_result(
            session_dir, req, forward_progress=False,
        )
        assert res.result_text == "ok"
        assert "BOB" not in (res.execution_trace or "")

    def test_turn_started_and_the_tailer_do_not_follow_the_swap(
        self, monkeypatch, tmp_path,
    ):
        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod, "_STARTED_SENTINEL_WAIT_S", 0.05)
        monkeypatch.setattr(mod, "_SENTINEL_POLL_S", 0.0)
        bob = self._other_session(tmp_path)
        alice = self._alice(tmp_path)
        session_dir = _SessionDir(alice)
        self._swap(alice, bob)

        assert TmuxClaudeBrain._turn_started(session_dir) is False
        assert TmuxClaudeBrain()._learn_transcript_path(session_dir) is None


def test_the_session_writes_no_world_readable_file(monkeypatch, tmp_path):
    """Belt and braces on the mode: the daemon's umask is not the guard."""
    old = os.umask(0)
    try:
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        brain = TmuxClaudeBrain()
        _record_launch(monkeypatch, brain)
        modes = {}

        def fake_wait(name, sentinel, deadline, cancel_check):
            cfg = Path(sentinel).parent / "config"
            for name_ in ("settings.json", ".claude.json"):
                modes[name_] = (cfg / name_).stat().st_mode & 0o777
            Path(sentinel).write_text(json.dumps({"last_assistant_message": "ok"}))
            return ("done", "")

        monkeypatch.setattr(brain, "_wait_for_completion", fake_wait)
        monkeypatch.setattr(brain, "_learn_transcript_path", lambda *a: None)
        brain.execute(_req(tmp_path, deferred))
    finally:
        os.umask(old)
    assert modes == {"settings.json": 0o600, ".claude.json": 0o600}
