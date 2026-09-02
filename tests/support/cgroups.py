"""A live-cgroup name no concurrently running test can also be using.

Every test that makes a *real* cgroup makes it under one shared root — the
subtree `scripts/test-linux.sh` builds and names in `ISTOTA_TEST_CGROUP_ROOT`,
or the daemon's own delegated unit on a deployed host. The name is
`task-<id>-<attempt>`, so a hardcoded id is a global resource: the suite runs
`-n auto`, and three tests across two files spelled the same literal `999997`.
The two cases of `TestTheWholeTreeIsInTheTaskCgroup` are enough on their own,
since both take the same fixture — one worker's `finally: destroy(path)` removes
the directory the other worker is still reading, and the failure surfaces as a
`FileNotFoundError` on `cgroup.procs` that reads as a product defect in
placement.

The pid is the obvious axis and the sufficient one: xdist workers are separate
processes, and within one worker the tests are sequential, so a name is free
again by the time the next test asks for it. Callers still vary `attempt` where
one test needs two cgroups at once.
"""

from __future__ import annotations

import os


def live_cgroup_task_id() -> int:
    """A fake task id unique to this test process."""
    return os.getpid()
