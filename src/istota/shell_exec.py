"""How a command string becomes a shell argv, in one place.

Three callers hand a command string to a shell and then treat the exit status
as the answer: the native brain's Bash tool (which appends ``[exit code: N]``
to what the model reads), the scheduler's CRON ``command:`` rows (whose status
drives auto-disable after five consecutive failures) and the heartbeat's
``shell-command`` checks (whose status is the health verdict). All three ran
under a shell with ``pipefail`` off, so a pipeline reported its *last* stage —
``<runner> … | tail`` came back 0 on a run that failed, and each of those three
consequences followed from a number that was wrong in the reassuring direction.

That is ISSUE-307, which fixed the same defect in ``devbox exec``. This module
is where the rule lives so the next caller inherits it rather than repeating it.

**The fallback is the old behaviour, not a degraded one.** Every caller was
``shell=True`` before, which is ``/bin/sh -c``; Debian ships dash there, which
has no ``pipefail`` at all. So a host without bash gets exactly what it had.

stdlib-only leaf. Imports nothing from the package, raises nothing.
"""

from __future__ import annotations

import shutil

# What `shell=True` resolves to on POSIX, and what we fall back to.
POSIX_SH = "/bin/sh"


def shell_argv(command: str, *, bash: str | None = None) -> list[str]:
    """Return the argv that runs ``command`` as a shell command, pipefail on.

    ``command`` is passed through as a single argv element and is never split,
    quoted or rewritten — the shell parses it, which is the whole point of
    handing it to one.

    ``bash`` names the interpreter. ``None`` probes ``PATH`` and is right for a
    caller running in the daemon's own filesystem view. A caller whose argv is
    executed somewhere else passes the name it wants: ``session/tools/bash.py``
    wraps its argv in bubblewrap, which binds ``/usr`` but need not reproduce
    the host's ``/bin`` symlink, so an absolute path probed out here is not
    necessarily a path in the namespace where the command runs. It passes
    ``"bash"`` and lets PATH resolve inside, which is what it always did.
    Passing an empty string forces the fallback.
    """
    interpreter = bash if bash is not None else shutil.which("bash")
    if interpreter:
        return [interpreter, "-o", "pipefail", "-c", command]
    return [POSIX_SH, "-c", command]


# 128 + SIGPIPE(13). `pipefail` newly colours a pipeline in two cases and this
# is the one with a fixed code, so it is the one that can be recognised: a
# downstream `head` or `grep -q` closes the pipe and kills a producer that was
# doing nothing wrong.
#
# The other case has no marker and is documented rather than detected — a
# non-final stage that exits non-zero to *report* something rather than to
# fail, so `grep -c x f | wc -l` returns 1 where it returned 0. Nothing can
# distinguish that from a real failure. It is the price of the option.
SIGPIPE_EXIT = 141

SIGPIPE_NOTE = (
    "exit 141 usually means SIGPIPE: a command in the pipeline was killed "
    "because the next one closed the pipe (`| head`, `| grep -q`), and with "
    "pipefail on that becomes the pipeline's status. If the consumer's output "
    "is what you wanted, this is not a failure — re-run without the early-exit "
    "consumer to get a status you can act on."
)
