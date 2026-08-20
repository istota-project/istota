"""Signal a subprocess and every descendant that shares its process group.

Killing a task's subprocess by pid alone leaves its children running. The
incident behind ISSUE-257 was a `pytest -n auto` run started by a task: the
`claude` CLI was killed on timeout, the four pytest workers it had spawned
were not, and they ran to completion on a 4-core production host. The same
holds for `!stop` and the web cancel endpoint, which are the paths a user
actually reaches for when a task is eating the machine.

The guard is the point of this module. ``os.killpg`` takes a *group*, and a
pid that does not lead its own group shares one with whoever spawned it — for
a child the daemon started without ``start_new_session``, that group is the
daemon's. Signalling it would kill the scheduler along with the task. So the
group is only ever signalled when the pid leads it, and anything else falls
back to the single process.

The fallback is what makes a *non-leader* pid safe, and both of today's writers
of ``tasks.worker_pid`` record leaders: ClaudeCodeBrain's own child is a session
leader after this change, and a tmux pane pid is one already (tmux ``setsid``s
the pane child so it can take the pty as its controlling terminal — measured,
pane pid and pgid are the same number). So `!stop` on a tmux task now signals
the pane's whole command tree rather than the pane leader alone, which is what
a cancel wants. The guard is what keeps the helper safe if a future caller
records a pid it did not spawn.

stdlib-only leaf so any module can import it without a cycle. Never raises.
"""

from __future__ import annotations

import logging
import os
import signal

__all__ = ["kill_process_group"]

logger = logging.getLogger(__name__)


def kill_process_group(pid: int, sig: int = signal.SIGKILL) -> str:
    """Send `sig` to `pid`'s process group, or to `pid` alone if it leads none.

    Returns what was signalled: ``"group"``, ``"process"``, or ``"gone"`` when
    the pid named nothing — a task that finished between the cancel and the
    signal is the ordinary race, not an error.

    Never raises, and that is load-bearing rather than tidy: the streaming
    brain's timeout calls this from a `threading.Timer` callback that has no
    `try`, so an exception here would kill the timer thread *after* the timeout
    flag is set — the task would report a timeout while the process it named ran
    on, which is the symptom this module exists to remove.
    """
    try:
        outcome = _signal(pid, sig)
    except Exception:
        logger.warning("kill_process_group(%r, %r) raised", pid, sig, exc_info=True)
        return "gone"
    # The only record of a kill that did not land. A `!stop` that visibly fails
    # to stop anything is otherwise indistinguishable from one that worked.
    logger.debug("kill_process_group pid=%s sig=%s outcome=%s", pid, sig, outcome)
    return outcome


def _signal(pid: int, sig: int) -> str:
    # os.kill(0, sig) signals our own process group and os.kill(-1, sig)
    # signals every process we may signal. A cleared or absent worker_pid must
    # never reach either.
    if pid <= 0:
        return "gone"

    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        pgid = None

    if pgid == pid:
        try:
            os.killpg(pgid, sig)
            return "group"
        except (ProcessLookupError, PermissionError, OSError):
            pass  # raced, or not ours — try the process on its own below

    try:
        os.kill(pid, sig)
        return "process"
    except (ProcessLookupError, PermissionError, OSError):
        return "gone"
