"""Run a skill CLI's ``main`` in-process and read back its envelope.

Every skill CLI answers with one JSON object on stdout and an exit status, and
since ISSUE-447 a host-path refusal is that same envelope — printed by
``_cli.fail`` and exiting 1, before any handler runs. A test that wants to
assert on a refusal therefore has to drive the *parse*, not the handler: the
resolution moved out of the handlers into ``parse_and_resolve``, so calling a
``cmd_*`` directly with a hostile path now exercises nothing at all.

``run_skill_main`` is that driver. It captures stdout itself rather than taking
``capsys``, so it composes with a test that is already using ``capsys`` for
something else, and it swallows the ``SystemExit`` the facade raises so a
caller reads a status code rather than writing ``pytest.raises`` around every
refusal.

**``sys.argv`` is set as well as passed.** ``health.main()`` takes no argv at
all and parses ``sys.argv`` — the only skill in the tree that does — so a
runner that only passed the list would silently drive that CLI with pytest's
own command line.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import sys
from dataclasses import dataclass
from typing import Callable, Sequence


@dataclass(frozen=True)
class CliRun:
    """What one in-process CLI invocation produced."""

    exit_code: int
    stdout: str

    @property
    def envelope(self) -> dict:
        """The last JSON object printed, or ``{}``.

        The *last*, because a handler may print progress lines of its own
        before the envelope; the facade's own error paths print exactly one.
        """
        for line in reversed(_json_candidates(self.stdout)):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
        return {}


def _json_candidates(text: str) -> list[str]:
    """Whole-document and per-line readings of stdout, cheapest last.

    ``emit`` pretty-prints with ``indent=2`` by default and ``fail`` prints
    compact, so neither a line-wise nor a whole-buffer parse covers both.
    """
    stripped = text.strip()
    return ([stripped] if stripped else []) + [
        line for line in text.splitlines() if line.strip()
    ]


def run_skill_main(
    main: Callable, argv: Sequence[str], *, prog: str = "istota-skill",
) -> CliRun:
    """Run ``main`` with ``argv``, returning its exit code and stdout."""
    buffer = io.StringIO()
    code = 0
    saved = sys.argv
    sys.argv = [prog, *argv]
    try:
        with contextlib.redirect_stdout(buffer):
            try:
                takes_argv = bool(inspect.signature(main).parameters)
                result = main(list(argv)) if takes_argv else main()
            except SystemExit as exit_:
                code = exit_.code if isinstance(exit_.code, int) else 1
            else:
                # `memory` and `ntfy` return an exit code rather than raising.
                if isinstance(result, int):
                    code = result
    finally:
        sys.argv = saved
    return CliRun(exit_code=code, stdout=buffer.getvalue())
