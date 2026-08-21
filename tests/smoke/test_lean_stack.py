"""One task, end to end, through the artifact that ships.

Layer 0 (`tests/image/`) asks whether the image contains the right things.
Nothing there starts the daemon, so nothing there can tell a container that
boots from one that exits on a malformed config, and nothing can tell a config
that parses from one the scheduler cannot actually run against. That gap is what
this file closes, and it closes it with the smallest thing that spans it: submit
a task through the shipped CLI, let the real scheduler pick it up, and read the
row back.

The model is scripted (`tests/support/model_endpoint.py`), so no credential and
no network are involved. Its wire format is pinned separately in
`tests/test_model_endpoint.py`, which runs in the default suite.

**Every assertion is filtered on the id `submit` returned.** The daemon queues
work of its own for the same user during startup, and the first version of this
file waited on `user_id=` alone — it matched a scheduled feeds poll, returned
that row, and reported a `source_type` mismatch that had nothing to do with what
the test was asking.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.smoke


class TestTheStackAnswersATask:
    def test_a_submitted_task_reaches_completed_with_the_scripted_answer(
        self, lean_stack
    ):
        task_id = lean_stack.submit("what is the scripted answer")

        task = lean_stack.probe.wait_for_task(
            status="completed", task_id=task_id, timeout=120
        )

        assert task["status"] == "completed", (
            f"task {task_id} ended {task['status']!r}: {task.get('error')!r}\n"
            f"--- daemon logs ---\n{lean_stack.logs()}"
        )
        # The answer came from the scripted endpoint, which is the proof that
        # the daemon reached it — a task can complete for other reasons, and a
        # status assertion alone would not distinguish them.
        assert "the scripted answer" in (task.get("result") or ""), task.get("result")

    def test_the_daemon_actually_called_the_endpoint(self, lean_stack):
        """The other half, asserted from the endpoint's side.

        Without this, a `completed` row proves only that something finished. The
        recorded request is what proves the container resolved
        `host.docker.internal`, read `base_url` out of the rendered config, and
        assembled a prompt — three separate things the row cannot distinguish
        between.
        """
        prompt = "a question the model must be asked"
        task_id = lean_stack.submit(prompt)

        lean_stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=120)

        assert lean_stack.endpoint.requests, (
            "the daemon never reached the scripted endpoint\n"
            f"--- daemon logs ---\n{lean_stack.logs()}"
        )
        body = lean_stack.endpoint.requests[0]
        assert body["model"] == "scripted-test-model", body["model"]
        assert any(
            prompt in str(message.get("content")) for message in body["messages"]
        ), body["messages"]


class TestTheProbeReadsTheRealSchema:
    def test_the_task_row_carries_the_columns_the_deployment_writes(self, lean_stack):
        # A thin assertion by itself, but it is the one that fails when `init`
        # stops running on a fresh boot — the DB file would still exist, and a
        # health check that only asked for the file would still report healthy.
        task_id = lean_stack.submit("populate a row")
        task = lean_stack.probe.wait_for_task(
            status="completed", task_id=task_id, timeout=120
        )

        for column in ("id", "created_at", "status", "source_type", "user_id", "prompt"):
            assert column in task, f"{column} missing from the task row: {sorted(task)}"
        assert task["source_type"] == "cli"
        assert task["user_id"] == "testuser"
        assert task["prompt"] == "populate a row"
