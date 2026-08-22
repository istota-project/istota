"""A Bash tool call, run through the real sandbox, inside the shipped image.

`test_lean_stack.py` proves a task reaches the model and comes back. It never
runs a tool, so it never touches bubblewrap — and every forge scenario does,
because the wrapper is something the model *execs*. That gap is what this file
closes, and it is worth its own file because when it fails, nothing in
`test_forge_e2e.py` can pass and none of those failures name the reason.

The scripted model issues one `Bash` call and then answers. What is asserted is
that the command's output came back, which is only true if bwrap built a
namespace, mounted the task's view, and ran the shell inside it.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.smoke

# One turn that runs a command, then one that answers with what it saw. The
# agent loop needs the second: a turn ending in `tool_calls` is a request for
# another round, and the scripted endpoint answers an unscripted round with an
# error frame rather than replaying.
SANDBOX_SCRIPT = [
    {
        "tool_calls": [
            {
                "id": "call-1",
                "name": "Bash",
                "arguments": {"command": "echo SANDBOX_RAN_THIS"},
            }
        ]
    },
    {"text": "the command produced SANDBOX_RAN_THIS"},
]


class TestTheSandboxRunsInsideTheContainer:
    @pytest.mark.parametrize("lean_stack", [SANDBOX_SCRIPT], indirect=True)
    def test_a_bash_tool_call_completes(self, lean_stack):
        task_id = lean_stack.submit("run a command for me")

        task = lean_stack.probe.wait_for_task(
            status="completed", task_id=task_id, timeout=180
        )

        assert task["status"] == "completed", (
            f"task {task_id} ended {task['status']!r}: {task.get('error')!r}\n"
            "If this says bwrap could not create a namespace, the compose file's "
            "`security_opt: seccomp:unconfined` is missing or was not applied.\n"
            f"--- daemon logs ---\n{lean_stack.logs(120)}"
        )

    @pytest.mark.parametrize("lean_stack", [SANDBOX_SCRIPT], indirect=True)
    def test_the_commands_output_came_back_to_the_model(self, lean_stack):
        """The assertion that distinguishes "ran" from "reported success".

        A task completes whether or not the tool produced anything — the second
        scripted turn answers regardless. The proof is that the *tool result*
        the daemon sent back to the endpoint carries the command's stdout,
        which nothing but a real execution puts there.
        """
        task_id = lean_stack.submit("run a command for me")
        lean_stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=180)

        # `transcript()` rather than iterating `requests`: it takes the
        # endpoint's lock, and the daemon's own tasks keep running after
        # `wait_for_task` returns, so handler threads may still be appending.
        # It also tolerates a body with no `messages` key, which indexing does
        # not.
        assert "SANDBOX_RAN_THIS" in lean_stack.endpoint.transcript(), (
            "no request carried the command's output back; the Bash tool did "
            "not run, or its result never reached the model\n"
            f"--- daemon logs ---\n{lean_stack.logs(120)}"
        )
