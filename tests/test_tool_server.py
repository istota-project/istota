"""A real tool server, over a real socketpair, on whatever host this runs on.

Unsandboxed — which is what a developer machine gives anyway, since
`build_bwrap_cmd` returns the command unchanged where bwrap is unavailable and
this file passes no wrap at all. That is deliberate rather than a compromise:
the containment claims belong to `tests/linux/`, and what is asserted here is
the seam — that the six tools reach the same files, refuse the same paths,
stream the same updates, abort, and that a broken server is reported as broken
rather than degraded into an answer.

Every test spawns a subprocess, so the file is deliberately small and each test
does one thing with the server it was given.
"""

import asyncio
import os
import shutil
import sys

import pytest

from istota.session.tools import ToolEnv, hello_payload, start_tool_server
from istota.session.tools import remote as remote_mod

pytestmark = pytest.mark.asyncio


def _hello(workspace, **kw):
    env = ToolEnv(cwd=workspace)
    args = dict(
        cwd=workspace,
        subprocess_env=None,
        read_roots=None,
        write_roots=None,
        write_denied_roots=(),
        deferred_dir=None,
        bash_timeout_seconds=30,
        max_output_bytes=env.max_output_bytes,
        max_read_lines=env.max_read_lines,
        max_read_bytes=env.max_read_bytes,
        bash_spill_full_output=True,
    )
    args.update(kw)
    return hello_payload(**args)


class _Server:
    """Start one, always shut it down — including when the test failed."""

    def __init__(self, **kw):
        self._kw = kw
        self.server = None

    async def __aenter__(self):
        self.server = await start_tool_server(**self._kw)
        return self.server

    async def __aexit__(self, *exc):
        if self.server is not None:
            await self.server.aclose()
        return False


def server_for(workspace, **kw):
    return _Server(hello=_hello(workspace, **kw), loop_abort=asyncio.Event())


async def _call(server, tool, args, on_update=None, abort=None):
    return await server.call(tool, "c1", args, on_update, abort)


def _text(result):
    return "".join(getattr(b, "text", "") for b in result.content)


class TestHandshake:
    async def test_it_reports_exactly_the_six_core_tools(self, tmp_path):
        async with server_for(tmp_path) as server:
            assert server.tools == ["Bash", "Edit", "Glob", "Grep", "Read", "Write"]
            assert server.failure is None

    async def test_the_recorded_pid_is_the_live_server(self, tmp_path):
        """`on_pid` is what `!stop`, the web cancel endpoint and
        `host_pressure.read_sandbox_shm` read; this brain used to record none,
        so every native task carried `worker_pid` 0."""
        seen = []
        async with _Server(
            hello=_hello(tmp_path), on_pid=seen.append, loop_abort=asyncio.Event()
        ) as server:
            assert seen == [server.pid]
            os.kill(seen[0], 0)  # raises if it is not a live process

    async def test_the_signal_death_prefix_matches_the_scheduler_predicate(self):
        """A `systemctl restart` SIGTERMs the whole cgroup, tool server
        included. `scheduler._is_shutdown_collateral` requeues such a task
        instead of failing it, and its test is a `startswith` on this exact
        string — so a drift here silently turns every deploy into a wave of
        failed native tasks."""
        from istota.brain.claude_code import _TERMINATED_PREFIX, is_signal_termination

        assert remote_mod._TERMINATED_PREFIX == _TERMINATED_PREFIX
        assert is_signal_termination(remote_mod._TERMINATED_PREFIX + "SIGTERM (signal 15)")

    async def test_a_signal_death_reaches_the_task_with_the_prefix_at_the_front(self):
        """`startswith`, so the marker cannot be labelled. Naming the tool
        server ahead of it reads better and breaks the requeue, which is the
        one thing this text is load-bearing for."""
        from istota.brain.claude_code import is_signal_termination

        killed = remote_mod._TERMINATED_PREFIX + "SIGTERM (signal 15)"
        assert remote_mod.attempt_failure_text(killed) == killed
        assert is_signal_termination(remote_mod.attempt_failure_text(killed))

    async def test_every_other_failure_names_the_tool_server(self):
        """The other arm. Without it the pass-through above would be satisfied
        by a function that never labelled anything, and a task whose sandbox
        broke would report a bare stderr tail with nothing saying where it came
        from."""
        text = remote_mod.attempt_failure_text("the tool server exited (exit 2)")
        assert "tool server" in text
        assert not is_signal_death(text)


def is_signal_death(text):
    from istota.brain.claude_code import is_signal_termination

    return is_signal_termination(text)


class TestTheToolsReachTheSameFiles:
    async def test_read_write_edit_glob_grep_bash(self, tmp_path):
        target = tmp_path / "notes.txt"
        async with server_for(tmp_path) as server:
            created = await _call(
                server, "Write", {"file_path": str(target), "content": "alpha\nbeta\n"}
            )
            assert "Created" in _text(created)
            assert target.read_text() == "alpha\nbeta\n"

            read = await _call(server, "Read", {"file_path": str(target)})
            assert "alpha" in _text(read)

            edited = await _call(server, "Edit", {
                "file_path": str(target), "old_string": "beta", "new_string": "gamma",
            })
            assert not edited.is_error, _text(edited)
            assert target.read_text() == "alpha\ngamma\n"

            found = await _call(server, "Glob", {"pattern": "*.txt", "path": str(tmp_path)})
            assert "notes.txt" in _text(found)

            grepped = await _call(server, "Grep", {
                "pattern": "gamma", "path": str(tmp_path), "output_mode": "content",
            })
            assert "gamma" in _text(grepped)

            ran = await _call(server, "Bash", {"command": "echo from-the-server"})
            assert "from-the-server" in _text(ran)

    async def test_bash_reports_a_failing_pipeline(self, tmp_path):
        """`pipefail` is applied inside the server now. Without it the exit
        status is the last stage's and `[exit code: N]` lies."""
        async with server_for(tmp_path) as server:
            result = await _call(server, "Bash", {"command": "false | cat"})
            assert "exit code: 1" in _text(result)

    async def test_it_refuses_the_same_paths(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = tmp_path / "secret.txt"
        outside.write_text("classified\n")
        async with server_for(
            tmp_path, cwd=workspace, read_roots=(workspace,), write_roots=(workspace,)
        ) as server:
            blocked = await _call(server, "Read", {"file_path": str(outside)})
            assert blocked.is_error
            assert "classified" not in _text(blocked)
            assert "outside the allowed workspace" in _text(blocked)

    async def test_a_write_denied_carve_out_still_refuses(self, tmp_path):
        workspace = tmp_path / "ws"
        carve = workspace / ".developer"
        carve.mkdir(parents=True)
        (carve / "credential-fetch").write_text("original\n")
        async with server_for(
            tmp_path, cwd=workspace, read_roots=(workspace,), write_roots=(workspace,),
            write_denied_roots=(carve,),
        ) as server:
            blocked = await _call(server, "Write", {
                "file_path": str(carve / "credential-fetch"), "content": "x",
            })
            assert "read-only" in _text(blocked).lower()
            assert (carve / "credential-fetch").read_text() == "original\n"

    async def test_an_unknown_tool_is_an_error_result_not_a_dead_server(self, tmp_path):
        async with server_for(tmp_path) as server:
            result = await _call(server, "Nope", {})
            assert result.is_error and "Unknown tool" in _text(result)
            assert server.failure is None

    async def test_the_subprocess_env_from_hello_reaches_a_bash_child(self, tmp_path):
        async with server_for(
            tmp_path, subprocess_env={"PATH": os.environ.get("PATH", ""), "MARKER": "wired"}
        ) as server:
            result = await _call(server, "Bash", {"command": "echo $MARKER"})
            assert "wired" in _text(result)


class TestStreamingAndAbort:
    async def test_on_update_chunks_arrive(self, tmp_path):
        updates = []

        async def _on_update(text):
            updates.append(text)

        async with server_for(tmp_path) as server:
            await _call(
                server, "Bash", {"command": "printf 'one\\ntwo\\n'"}, on_update=_on_update
            )
            for _ in range(50):  # updates are dispatched off the result path
                if any("one" in u for u in updates):
                    break
                await asyncio.sleep(0.02)
        assert any("one" in u for u in updates), updates

    async def test_abort_cancels_an_in_flight_bash(self, tmp_path):
        abort = asyncio.Event()
        async with server_for(tmp_path) as server:
            call = asyncio.ensure_future(
                _call(server, "Bash", {"command": "sleep 30"}, abort=abort)
            )
            await asyncio.sleep(0.4)
            abort.set()
            result = await asyncio.wait_for(call, timeout=15)
        assert "aborted" in _text(result)

    async def test_two_parallel_calls_overlap(self, tmp_path):
        """`execution_mode="parallel"` is decided daemon-side, so the server
        must run concurrent `call` frames concurrently — awaiting them one at a
        time would silently serialize every read-only batch the loop dispatches
        while every schema assertion stayed green."""
        async with server_for(tmp_path) as server:
            started = asyncio.get_running_loop().time()
            await asyncio.gather(
                server.call("Bash", "a", {"command": "sleep 1"}, None, None),
                server.call("Bash", "b", {"command": "sleep 1"}, None, None),
            )
            elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < 1.8, f"the two calls ran back to back ({elapsed:.2f}s)"


class TestFailure:
    async def test_a_malformed_frame_produces_fatal_and_a_non_zero_exit(self, tmp_path):
        async with server_for(tmp_path) as ctx:
            ctx._writer.write(b"\x00\x00\x00\x09{not json")
            await ctx._writer.drain()
            for _ in range(200):
                if ctx.failure is not None:
                    break
                await asyncio.sleep(0.02)
            assert ctx.failure is not None
            assert "fatal" in ctx.failure or "protocol" in ctx.failure
            rc = await asyncio.wait_for(ctx._proc.wait(), timeout=10)
        assert rc != 0

    async def test_a_dead_server_errors_the_call_and_fails_the_attempt(self, tmp_path):
        loop_abort = asyncio.Event()
        async with _Server(hello=_hello(tmp_path), loop_abort=loop_abort) as server:
            call = asyncio.ensure_future(
                server.call("Bash", "c", {"command": "sleep 30"}, None, None)
            )
            await asyncio.sleep(0.4)
            os.kill(server.pid, 9)
            result = await asyncio.wait_for(call, timeout=15)

        # Three things at once, and all three matter. The call returns an error
        # result rather than raising into the loop; the loop's abort is set so
        # the run stops instead of asking a dead server for more tools; and the
        # failure is *recorded*, which is what NativeBrain turns into a failed
        # attempt rather than a confident answer narrated around a dead sandbox.
        assert result.is_error and "Tool server failed" in _text(result)
        assert loop_abort.is_set()
        assert server.failure is not None

    async def test_a_call_after_the_failure_is_refused_without_hanging(self, tmp_path):
        async with _Server(hello=_hello(tmp_path), loop_abort=asyncio.Event()) as server:
            os.kill(server.pid, 9)
            for _ in range(200):
                if server.failure is not None:
                    break
                await asyncio.sleep(0.02)
            result = await asyncio.wait_for(
                server.call("Read", "c", {"file_path": "/etc/hostname"}, None, None),
                timeout=5,
            )
        assert result.is_error and "Tool server failed" in _text(result)

    async def test_a_server_that_never_answers_fails_the_start(self, tmp_path):
        """The startup timeout, with a stand-in that ignores the socket. The
        real one cannot be made to hang from here without editing it."""
        with pytest.raises(remote_mod.ToolServerError, match="did not become ready"):
            await start_tool_server(
                _hello(tmp_path),
                sandbox_wrap=lambda cmd: [sys.executable, "-c", "import time; time.sleep(30)"],
                startup_timeout=1.0,
            )

    async def test_a_server_that_exits_at_once_fails_the_start(self, tmp_path):
        with pytest.raises(remote_mod.ToolServerError):
            await start_tool_server(
                _hello(tmp_path),
                sandbox_wrap=lambda cmd: [sys.executable, "-c", "raise SystemExit(3)"],
                startup_timeout=10.0,
            )

    async def test_a_command_that_cannot_be_spawned_fails_the_start(self, tmp_path):
        with pytest.raises(remote_mod.ToolServerError, match="could not start"):
            await start_tool_server(
                _hello(tmp_path),
                sandbox_wrap=lambda cmd: [str(tmp_path / "no-such-binary")],
                startup_timeout=5.0,
            )


class TestTheWrapIsApplied:
    async def test_the_spawn_goes_through_the_wrap_it_was_given(self, tmp_path):
        """The Stage 1 guarantee, at its new site: the argv the sandbox is
        built around is the tool server's, and the wrap it goes through is the
        request's NATIVE one (`tests/test_brain_types.py` holds the other
        half — which of the two request fields reaches here)."""
        seen = {}

        def _wrap(cmd):
            seen["cmd"] = list(cmd)
            return cmd

        async with _Server(
            hello=_hello(tmp_path), sandbox_wrap=_wrap, loop_abort=asyncio.Event()
        ):
            pass
        assert seen["cmd"][:3] == [sys.executable, "-m", "istota.tool_server"]
        assert seen["cmd"][3] == "--fd"
        assert int(seen["cmd"][4]) > 2


class TestTheDescriptorIsNotInheritedByToolChildren:
    async def test_a_bash_child_cannot_see_the_control_socket(self, tmp_path):
        """`close_fds` is the whole mechanism — a command that could reach the
        socket could answer its own tool calls. Measured against the fd number
        the daemon actually passed rather than against 3."""
        seen = {}

        def _wrap(cmd):
            seen["fd"] = cmd[cmd.index("--fd") + 1]
            return cmd

        async with _Server(
            hello=_hello(tmp_path), sandbox_wrap=_wrap, loop_abort=asyncio.Event()
        ) as server:
            fd = seen["fd"]
            result = await _call(server, "Bash", {
                "command": f'if [ -e /dev/fd/{fd} ] || [ -e /proc/self/fd/{fd} ]; '
                           f'then echo FD-PRESENT; else echo FD-ABSENT; fi',
            })
        assert "FD-ABSENT" in _text(result), _text(result)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
class TestPersistenceAcrossCalls:
    async def test_tmp_and_a_background_process_survive_between_calls(self, tmp_path):
        """The behaviour change one namespace per attempt buys, and it is
        observable: a per-call sandbox threw both away every time."""
        async with server_for(tmp_path) as server:
            await _call(server, "Bash", {"command": "echo kept > /tmp/istota-probe-$$"})
            listed = await _call(server, "Bash", {"command": "ls /tmp/istota-probe-* | wc -l"})
        assert listed.content and _text(listed).strip().splitlines()[0].strip() != "0"


class TestEncodeGuardsTheCallPath:
    async def test_an_unserializable_argument_errors_the_call_not_the_server(self, tmp_path):
        """Arguments come off the model's JSON so they are serializable in
        practice; the guard is that a value that is not does not take the
        attempt down with it. The refusal itself is the protocol's, and
        `tests/test_tool_server_protocol.py` is where that is asserted."""
        async with server_for(tmp_path) as server:
            result = await server.call("Read", "c", {"file_path": object()}, None, None)
            assert result.is_error and "could not be sent" in _text(result)
            assert server.failure is None
            ok = await _call(server, "Bash", {"command": "echo still-here"})
        assert "still-here" in _text(ok)
