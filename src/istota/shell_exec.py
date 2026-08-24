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

**This swaps the interpreter, and ``pipefail`` is one consequence of that rather
than the whole of it.** The callers ran under ``/bin/sh`` — dash on Debian — and
now run under bash, so a few things change besides the option. The one most
likely to be noticed is ``echo``: dash interprets backslash escapes in it and
bash does not, so ``echo "a\\tb"`` emitted a real tab before and emits a literal
``\\tb`` now. Both operator-facing callers read stdout *as data* (the heartbeat
compares it against a condition, the scheduler returns it as the task result),
so that is a silent change of answer rather than a failure. ``$0`` changes too,
and bash sources ``$BASH_ENV`` for a non-interactive shell where dash does not —
``executor.build_stripped_env`` strips that variable for exactly this reason.

**The fallback is the old behaviour, not a degraded one.** Every caller was
``shell=True`` before, which is ``/bin/sh -c``; Debian ships dash there, which
has no ``pipefail`` at all. So a host without bash gets exactly what it had —
but it also gets none of the fix, silently, which is the failure mode this whole
module exists to remove. ``shell_argv`` therefore logs once when it falls back.

stdlib-only leaf. Imports nothing from the package, raises nothing.
"""

from __future__ import annotations

import logging
import shutil

logger = logging.getLogger("istota.shell_exec")

# What `shell=True` resolves to on POSIX, and what we fall back to.
POSIX_SH = "/bin/sh"

# One line per process, not one per invocation: this runs on every cron tick and
# every heartbeat check, and a host without bash has that condition permanently.
_fallback_warned = False


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
    _warn_no_pipefail()
    return [POSIX_SH, "-c", command]


#: The variable bash reads its startup options from.
#:
#: Set in a process's environment, bash enables each option named in it before
#: reading any startup file, and the variable is readonly inside the shell so a
#: command cannot unset it. ``set +o pipefail`` still works, which is the
#: per-command escape hatch and the only one there is.
SHELLOPTS_VAR = "SHELLOPTS"

#: The value. Colon-separated option names; one, here.
PIPEFAIL_SHELLOPTS = "pipefail"


def pipefail_env() -> dict[str, str]:
    """The environment entries that turn ``pipefail`` on for every bash below.

    ``shell_argv`` can only fix a shell istota spawns itself, and the largest
    consumer of shells in the deployment spawns its own: a ``ClaudeCodeBrain``
    or ``TmuxClaudeBrain`` task runs its commands through the Claude Code CLI's
    Bash tool, which builds ``bash -c 'source <shell-snapshot> && eval <cmd>'``
    in a process istota launches but does not instrument. So that shell started
    with ``pipefail`` off and reported a pipeline's *last* stage — ``gh auth
    status 2>&1 | head -3`` came back 0 for a refusal whose whole signal is
    exit 3, and ``git commit … | tail -25`` came back 0 for a commit the
    secret-scanning hook had just aborted (ISSUE-321).

    The environment is the only lever that reaches it. This is the fourth
    member of one family — ISSUE-264 fixed it in the developer skill's prose,
    ISSUE-307 in ``devbox exec``, ``shell_argv`` above in istota's own three
    exit-status-as-answer callers — and it lives here so a caller inherits the
    rule rather than restating it.

    **Why ``SHELLOPTS`` and not ``BASH_ENV``.** Both work, and both were
    measured working through a sourced shell snapshot. ``BASH_ENV`` names a
    *file* bash sources before every non-interactive shell, which is arbitrary
    code execution before every command the model runs, and needs that file to
    exist, to be bound into the sandbox, and to stay unwritable by the model
    for the guarantee to hold — three things that can each fail silently, since
    bash ignores an unreadable ``BASH_ENV`` without a word.
    ``executor._SHELL_STARTUP_ENV_VARS`` strips it for exactly that reason,
    from ``build_stripped_env`` and — since this change was reviewed — from
    ``build_clean_env``'s passthrough loop as well, which is the path that
    actually mattered here and did not filter. ``SHELLOPTS`` carries option
    *names*: a value of ``pipefail:$(touch /tmp/x)`` is rejected as an invalid
    option name rather than evaluated, measured, so there is no file and no
    inlet. The same set now strips an *inherited* ``SHELLOPTS`` before this
    value is set, so what reaches a shell is only ever what this function says.

    **It reaches further than the flag does, and only through bash.**
    ISSUE-307 recorded that ``-o pipefail`` stops at one shell, so a pipeline
    inside a ``bash script.sh`` was unguarded again. An environment variable is
    inherited, so it is not. That is more coverage and also a larger behaviour
    change, in the direction the project has already chosen twice: a status
    wrong in the alarming direction makes a reader look, one wrong in the
    reassuring direction gets acted on. **What it does not reach is anything
    that is not bash**, and the boundary is the host's rather than a rule: a
    ``#!/bin/sh`` script gets the option on macOS, where ``/bin/sh`` is bash in
    sh-mode and imports the variable, and not on Debian, where ``/bin/sh`` is
    dash and has no ``pipefail`` at all. So a ``make`` recipe, an ``npm``
    lifecycle script or a ``subprocess(shell=True)`` is covered on the
    standalone install and not on the server. Do not write either behaviour
    into a doc as though it were the rule.

    **Two more measured edges.** Bash 3.2 in POSIX mode (``--posix``, not
    sh-mode) refuses the variable outright — ``SHELLOPTS: readonly variable``
    on stderr, option not applied — which is the macOS system bash and not
    Debian's. And an option name bash does not know makes it complain on
    stderr and carry on rather than refuse to start, so a future value that is
    wrong degrades to the old behaviour instead of breaking every command.

    **The two costs are the ones ISSUE-307 already paid for.** ``yes | head``
    and ``cmd | grep -q`` now report 141, which has a fixed code and is
    annotated by :data:`SIGPIPE_NOTE`. A non-final stage that exits non-zero to
    *report* rather than to fail now colours the whole pipeline — ``grep -c x f
    | wc -l`` returns 1 where it returned 0 — which nothing can distinguish
    from a real failure and is documented instead.

    **One caller, and the asymmetry is deliberate.** ``build_clean_env`` uses
    this, so a model subprocess has the option at every depth. The cron
    ``command:`` and heartbeat ``shell-command`` paths build their env with
    ``build_stripped_env`` and get ``pipefail`` from ``shell_argv`` alone, at
    depth one — a pipeline inside a script such a row invokes is unguarded.
    Folding this in there too would be consistent, and it is not done: those
    commands are *operator*-authored, the operator can put ``set -euo
    pipefail`` at the top of their own script, and changing the meaning of a
    deployed cron row's exit status at depth is a second behaviour change with
    a different blast radius from the one ISSUE-321 asked for.

    A fresh dict each call: callers merge it into an env they go on to mutate.
    """
    return {SHELLOPTS_VAR: PIPEFAIL_SHELLOPTS}


def _warn_no_pipefail() -> None:
    """Say once that this host cannot honour the guarantee the docs promise.

    Without this the degradation is exactly the thing being fixed: an operator
    who read the changelog believes a failing pipeline is now reported, and on a
    host with no bash on the daemon's PATH it silently is not, forever.
    """
    global _fallback_warned
    if _fallback_warned:
        return
    _fallback_warned = True
    logger.warning(
        "no bash on PATH — shell commands fall back to %s, which has no "
        "pipefail, so a failing stage of a pipeline will be reported as "
        "success. Cron `command:` rows and shell-command heartbeats are "
        "affected.", POSIX_SH,
    )


def is_sigpipe_failure(text: str) -> bool:
    """True when ``text`` is a failure this module attributed to SIGPIPE.

    The scheduler classifies on failure *text* rather than on a status — the
    executor drops `stop_reason` at its return boundary, so this is the same
    shape as `brain.claude_code.is_signal_termination`. Keying on the note
    itself rather than on a second marker string means there is one spelling to
    keep in step instead of two.
    """
    return SIGPIPE_NOTE in (text or "")


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
