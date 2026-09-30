"""ISSUE-550: the skill proxy serves only its own task's process tree.

Every task runs as the daemon's uid, so the socket's 0600 mode keeps out no
other task. Where the model runs without bwrap, all tasks share one `/tmp` and
any of them could connect to another live task's socket and be served as that
task's user. The proxy now asks the kernel who connected and refuses a peer
that does not descend from a process its own task registered.

The attacker here is a real second process on the same uid, and the task root
is a real process it is not descended from. A mock peer would be a model of
the kernel's answer rather than the answer.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

from istota import peer_process
from istota.skill_proxy import SkillProxy

pytestmark = pytest.mark.skipif(
    not peer_process.supported(),
    reason="no peer-credential call on this platform",
)

CLIENT = textwrap.dedent("""
    import socket, sys
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sys.argv[1])
    s.sendall(sys.argv[2].encode() + b"\\n")
    chunks = []
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    sys.stdout.write(b"".join(chunks).decode())
""")

REQUESTS = {
    "credential": {"type": "credential", "name": "GITLAB_TOKEN"},
    "vault_credential": {"type": "vault_credential", "name": "acme"},
    "vault_list": {"type": "vault_list"},
    "skill": {"skill": "calendar", "args": ["list"]},
}

SECRETS = ("glpat-victim-secret", "vault-victim-secret")


@pytest.fixture
def sock_path():
    directory = tempfile.mkdtemp(prefix="pp_", dir="/tmp")
    path = Path(directory) / "s.sock"
    yield path
    path.unlink(missing_ok=True)
    Path(directory).rmdir()


@pytest.fixture
def client_script(tmp_path):
    path = tmp_path / "client.py"
    path.write_text(CLIENT)
    return path


@pytest.fixture
def task_root():
    """A live process standing in for another task's brain child."""
    proc = subprocess.Popen(["sleep", "60"])
    yield proc.pid
    proc.kill()
    proc.wait()


def _victim(sock_path, roots):
    return SkillProxy(
        sock_path,
        {"GITLAB_TOKEN": SECRETS[0]},
        {"PATH": "/usr/bin"},
        task_id=5,
        vault_credentials={"acme": SECRETS[1]},
        vault_fetch_limit=10,
        trusted_roots=roots,
    )


def _run_client(client_script, sock_path, request, *, prefix=()):
    out = subprocess.run(
        [*prefix, sys.executable, str(client_script), str(sock_path),
         json.dumps(request)],
        capture_output=True, text=True, timeout=30,
    )
    return out.stdout


class TestASiblingProcessIsRefused:
    @pytest.fixture(autouse=True)
    def _short_grace(self, monkeypatch):
        # Nothing registers during these, so each refusal would otherwise wait
        # out the full grace period.
        monkeypatch.setattr(
            "istota.skill_proxy.PEER_REGISTRATION_GRACE_SECONDS", 0.1,
        )

    @pytest.mark.parametrize("kind", sorted(REQUESTS))
    def test_a_same_uid_process_outside_the_tree_gets_nothing(
        self, sock_path, client_script, task_root, kind,
    ):
        # The attacker is a child of pytest, not of the task root: exactly a
        # sibling task on an unsandboxed shape, same uid, holding the path.
        with _victim(sock_path, {task_root}):
            raw = _run_client(client_script, sock_path, REQUESTS[kind])
        response = json.loads(raw)
        assert response["reason"] == "peer_not_in_task"
        for secret in SECRETS:
            assert secret not in raw
        assert "names" not in response

    def test_the_refusal_is_logged_with_the_peer(
        self, sock_path, client_script, task_root, caplog,
    ):
        with caplog.at_level("WARNING", logger="istota.skill_proxy"):
            with _victim(sock_path, {task_root}):
                _run_client(client_script, sock_path, REQUESTS["credential"])
        assert any(
            "proxy_rejected" in r.getMessage() and "reason=peer" in r.getMessage()
            for r in caplog.records
        )

    def test_a_proxy_with_no_root_yet_serves_nobody(
        self, sock_path, client_script,
    ):
        # Nothing registered: the task has no process yet, so no process can
        # be it. A default that served everyone until the first registration
        # would be the bug with a delay on it.
        with _victim(sock_path, ()):
            raw = _run_client(client_script, sock_path, REQUESTS["credential"])
        assert json.loads(raw)["reason"] == "peer_not_in_task"
        assert SECRETS[0] not in raw


class TestTheTasksOwnTreeIsServed:
    def test_an_exited_root_authorizes_nothing(self, sock_path, monkeypatch):
        # A brain root is never revoked: a reroute or a retry leaves the dead
        # child's pid registered for the rest of the task. Whatever process
        # holds that number next must not inherit it.
        monkeypatch.setattr(
            "istota.skill_proxy.PEER_REGISTRATION_GRACE_SECONDS", 0.1,
        )
        with _victim(sock_path, ()) as proxy:
            proc = subprocess.Popen(["sleep", "0.2"])
            proxy.authorize_pid(proc.pid)
            assert proxy._peer_in_task(proc.pid)
            proc.wait()
            assert not proxy._peer_in_task(proc.pid)

    def test_a_descendant_of_the_registered_root_is_served(
        self, sock_path, client_script,
    ):
        # `sh -c` is the root and the client is its child, the shape of a
        # Bash tool call under the CLI. Registered *after* the spawn, the way
        # `on_pid` arrives, which is the race the grace wait exists for.
        with _victim(sock_path, ()) as proxy:
            proc = subprocess.Popen(
                ["sh", "-c",
                 f'sleep 0.2; "{sys.executable}" "{client_script}" '
                 f'"{sock_path}" \'{json.dumps(REQUESTS["credential"])}\'; true'],
                stdout=subprocess.PIPE, text=True,
            )
            proxy.authorize_pid(proc.pid)
            out, _ = proc.communicate(timeout=30)
        assert json.loads(out) == {"value": SECRETS[0]}

    def test_a_registration_arriving_after_the_connect_is_waited_for(
        self, sock_path, client_script,
    ):
        with _victim(sock_path, ()) as proxy:
            proc = subprocess.Popen(
                [sys.executable, str(client_script), str(sock_path),
                 json.dumps(REQUESTS["credential"])],
                stdout=subprocess.PIPE, text=True,
            )
            time.sleep(0.3)
            proxy.authorize_pid(proc.pid)
            out, _ = proc.communicate(timeout=30)
        assert json.loads(out) == {"value": SECRETS[0]}


class TestTheProxysOwnSkillSubprocess:
    def test_a_skill_cli_connecting_back_is_served_and_then_revoked(
        self, sock_path, client_script, tmp_path, monkeypatch,
    ):
        # A skill CLI resolving a stamped credential connects back to the
        # proxy that spawned it. It descends from the daemon, not from the
        # task's brain, so the proxy has to register it itself. The stand-in
        # interpreter ignores `-m istota.skills.x` and runs the client.
        fake_python = tmp_path / "fake-python"
        fake_python.write_text(
            f'#!/bin/sh\nsleep 1\nexec "{sys.executable}" "{client_script}" '
            f'"{sock_path}" \'{json.dumps(REQUESTS["credential"])}\'\n'
        )
        fake_python.chmod(0o755)
        monkeypatch.setattr("istota.skill_proxy.sys.executable", str(fake_python))

        with _victim(sock_path, ()) as proxy:
            outer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            outer.connect(str(sock_path))
            # The outer request comes from pytest, which stands in for the
            # task's root just long enough to be admitted, then is revoked —
            # otherwise the skill CLI, a descendant of pytest too, would pass
            # on pytest's registration rather than its own.
            proxy.authorize_pid(os.getpid())
            outer.sendall(json.dumps({"skill": "x", "args": []}).encode() + b"\n")
            time.sleep(0.4)
            proxy.revoke_pid(os.getpid())
            response = json.loads(outer.makefile().readline())
            outer.close()
            left = proxy.trusted_roots
        assert json.loads(response["stdout"]) == {"value": SECRETS[0]}
        assert left == frozenset()


class TestTheChain:
    PARENTS = {50: 40, 40: 30, 30: 1, 60: 1, 70: 71, 71: 70}
    STARTS = {30: 300, 40: 400, 50: 500, 60: 600, 70: 700, 71: 710}

    def _descends(self, pid, roots, starts=None):
        starts = self.STARTS if starts is None else starts
        return peer_process.descends_from(
            pid, {r: self.STARTS.get(r, 0) for r in roots},
            parent_of=self.PARENTS.get, start_of=starts.get,
        )

    def test_the_root_itself_and_its_descendants(self):
        assert self._descends(30, {30})
        assert self._descends(50, {30})

    def test_a_sibling_branch_is_not(self):
        assert not self._descends(60, {30})

    def test_init_is_never_a_usable_root(self):
        assert not self._descends(60, {1})

    def test_an_unreadable_link_refuses(self):
        assert not self._descends(99, {30})

    def test_a_cycle_terminates(self):
        assert not self._descends(70, {30})

    def test_a_recycled_root_pid_authorizes_nothing(self):
        # Pid 30 now belongs to a different process: same number, new start.
        assert not self._descends(50, {30}, {**self.STARTS, 30: 999})

    def test_the_real_start_time_is_stable(self):
        pid = os.getpid()
        assert peer_process.start_time(pid) == peer_process.start_time(pid)
        assert peer_process.start_time(pid) is not None

    def test_the_real_parent_is_read(self):
        assert peer_process.parent_pid(os.getpid()) == os.getppid()

    def test_the_real_peer_is_read(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            assert peer_process.peer_pid(a) == os.getpid()
        finally:
            a.close()
            b.close()


class TestReportingPid:
    def test_the_child_reports_its_own_pid(self):
        seen = []
        with peer_process.reporting_pid(seen.append) as preexec:
            result = subprocess.run(
                [sys.executable, "-c", "import os; print(os.getpid())"],
                capture_output=True, text=True, preexec_fn=preexec,
            )
        assert seen == [int(result.stdout)]

    def test_a_composed_preexec_runs_first(self, tmp_path):
        marker = tmp_path / "ran"
        seen = []

        def first():
            marker.write_text("x")

        with peer_process.reporting_pid(seen.append, first) as preexec:
            subprocess.run(["true"], preexec_fn=preexec)
        assert marker.exists()
        assert len(seen) == 1

    def test_no_callback_passes_the_preexec_through(self):
        def mine():
            pass

        with peer_process.reporting_pid(None, mine) as preexec:
            assert preexec is mine


class TestTheRootIsRegistered:
    """The proxy is only as good as the roots it hears about.

    A check with no registration refuses every legitimate call, which would be
    noticed; a registration nobody wired is the quieter failure, since every
    other test here registers by hand.
    """

    @pytest.mark.parametrize("skill_enabled", [True, False])
    def test_the_executor_hands_the_brains_pid_to_the_proxy(self, tmp_path, skill_enabled):
        from unittest.mock import MagicMock, patch

        from istota.executor import execute_task
        from istota.network_proxy import NetworkProxy

        from .test_executor_streaming import (
            _EXECUTOR_PATCH_RETURNS,
            _EXECUTOR_PATCHES,
            _make_config,
            _make_task,
            contextmanager_chain,
        )

        config = _make_config(tmp_path)
        config.security.skill_proxy_enabled = skill_enabled
        config.security.network.enabled = True
        config.security.sandbox_enabled = True
        network_registered = []
        registered: list[int] = []

        def record(self, pid):
            registered.append(pid)

        def call_on_pid(req):
            req.on_pid(4242)
            return MagicMock(
                success=True, output="ok", stop_reason="ok", actions=[],
                trace=[], usage=None, session_id=None, cost_usd=None,
            )

        patches = [
            patch(name, return_value=ret)
            for name, ret in zip(_EXECUTOR_PATCHES, _EXECUTOR_PATCH_RETURNS)
        ] + [
            patch.object(SkillProxy, "authorize_pid", record),
            patch.object(NetworkProxy, "authorize_pid", lambda self, pid: network_registered.append(pid)),
        ]
        with contextmanager_chain(patches):
            with patch("istota.executor.make_brain") as make_brain:
                brain = MagicMock()
                brain.execute.side_effect = call_on_pid
                brain.model_namespace = "anthropic"
                brain.resolve_model_name.side_effect = lambda m: m or "model"
                brain.supports_steering = False
                make_brain.return_value = brain
                execute_task(_make_task(id=81), config, [])

        # The suite's default root (this process) is registered at
        # construction through the same method, so look for the brain's pid.
        assert (4242 in registered) == skill_enabled
        assert 4242 in network_registered

    def test_the_non_streaming_claude_path_reports_its_child(self, tmp_path):
        # `subprocess.run` never hands the pid back, and a deployment with
        # `event_log_enabled = false` runs every task through here — so without
        # the report every skill call on that shape would be refused.
        from istota.brain.claude_code import ClaudeCodeBrain

        from .test_task_cgroup_placement import _req

        seen: list[int] = []
        result = ClaudeCodeBrain()._execute_simple_once(
            ["sh", "-c", "echo $$"], _req(tmp_path, on_pid=seen.append),
        )
        assert seen == [int(result.result_text)]

    def test_a_stop_on_the_non_streaming_path_reads_as_a_cancellation(
        self, tmp_path,
    ):
        # Reporting the pid is what lets `!stop` reach this path at all, so a
        # SIGTERM from it has to classify as cancelled, not as a crash.
        import signal
        import threading

        from istota.brain.claude_code import ClaudeCodeBrain

        from .test_task_cgroup_placement import _req

        def stop_soon(pid):
            threading.Timer(0.3, os.kill, (pid, signal.SIGTERM)).start()

        result = ClaudeCodeBrain()._execute_simple_once(
            ["sleep", "30"],
            _req(tmp_path, on_pid=stop_soon, cancel_check=lambda: True),
        )
        assert result.stop_reason == "cancelled"
        assert result.result_text == "Cancelled by user"
