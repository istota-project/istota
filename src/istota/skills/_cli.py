"""The skill CLI facade: one JSON envelope on stdout, and the exit code with it.

Every skill CLI answers the same way — a JSON object on stdout and an exit
status — and the status is not decoration. The scheduler detects the
module-skill facade convention and treats a non-zero exit as a failed step, so
a handler that *returns* ``{"status": "error", ...}`` has to fail the task just
as a raised exception does. That rule was stated six ways across eight files
before this module existed: five copies checked the status inside the ``try``,
``skills/email`` moved it outside and wrote down why, ``skills/browse`` and
``skills/code_review`` each grew their own, and ``skills/location`` had three
sites that printed an error envelope and exited 0. (``skills/kv`` checked no
status in ``main`` either, but every one of its error paths already exited 1.)

Nothing from the package beyond ``._hostpath``, itself a leaf over
``istota.skill_host_paths`` — so a skill subprocess pays nothing for it beyond
what ``istota.skills.__init__`` already costs.

``parse_and_resolve`` is the other half of the facade's contract and is here
rather than in ``_hostpath`` for that reason: the refusal has to come back as
one JSON envelope on stdout with the status in the exit code, which is this
module's rule, not the allowlist's.

Two things a reader will want to know before converting the next call site.

**A handler that returns ``None`` has already printed.** Several skills
(``kv``, ``location``, ``health``, ``feeds``) are written the other way round —
each ``cmd_*`` prints its own envelope through ``emit`` and returns nothing.
``run_skill_cli`` prints only what a handler hands back, so both shapes work
and neither prints twice.

**``sys.exit`` raises ``SystemExit``, which is not an ``Exception``.** That is
what lets a handler emit-and-exit from inside ``run_skill_cli``'s ``try``
without the epilogue catching it and rewriting the envelope, and it is why the
status check outside the ``try`` needs no guard against re-entry.

The one deliberate non-consumer is ``istota/ocr_leaf.py``, which carries the
same epilogue and may not import it: that module's whole contract is that it
imports the standard library, Pillow and pytesseract and nothing from
``istota``, pinned by
``tests/test_transcribe_out_of_process.py::TestTheChildImportSurface``.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable, NoReturn

from ._hostpath import resolve_parsed


def error_envelope(message: str, **extra: Any) -> dict:
    """The error envelope every skill returns, built in one place.

    An ``extra`` naming ``status`` or ``error`` is dropped rather than splatted
    over the discriminator: `emit` exits on `is_error`, so an envelope this
    built whose status was overwritten would print and *not* exit — turning
    `skills/skills._output_error`, whose callers all fall through into code
    assuming it did not return, from an unconditional refusal into a
    conditional one. No caller does it today; the invariant is what the two
    exiting helpers below rest on.
    """
    extra.pop("status", None)
    extra.pop("error", None)
    return {"status": "error", "error": message, **extra}


def is_error(payload: Any) -> bool:
    """Whether a handler's return value reports a failure.

    ``isinstance`` rather than ``payload.get`` because a handler may return a
    list — ``skills/nextcloud`` has several — and a list has no ``get``.
    """
    return isinstance(payload, dict) and payload.get("status") == "error"


def status_exit_code(payload: Any) -> int:
    """0 when the envelope reports success, 1 otherwise.

    For the two skills — ``memory`` and ``ntfy`` — whose ``main`` *returns* an
    exit code rather than calling ``sys.exit``, so their ``__main__`` can pass
    it on. Note the asymmetry with `is_error`, which is deliberate and is the
    behaviour both already had: this asks whether the status is ``ok``, so a
    third status such as ``skipped`` is non-zero here and zero there.
    """
    return 0 if isinstance(payload, dict) and payload.get("status") == "ok" else 1


def emit(
    payload: Any,
    *,
    indent: int | None = 2,
    ensure_ascii: bool = False,
    default: Callable[[Any], Any] | None = None,
    exit_on_error: bool = True,
) -> None:
    """Print one JSON envelope, and exit 1 if it reports an error.

    The four keyword arguments are the serialization each converted call site
    already used; the defaults are the majority shape. They are knobs rather
    than a single format because the envelope is what the model reads, and
    re-serializing nine skills' output under a refactor is a user-visible
    change nobody asked for.
    """
    print(json.dumps(payload, indent=indent, ensure_ascii=ensure_ascii, default=default))
    if exit_on_error and is_error(payload):
        sys.exit(1)


def fail(message: str, **extra: Any) -> NoReturn:
    """Print an error envelope and exit 1.

    Compact and ASCII-escaped, which is what every ``_fail`` variant and every
    ``except`` branch this replaced already emitted.
    """
    print(json.dumps(error_envelope(message, **extra)))
    sys.exit(1)


def parse_and_resolve(
    parser: argparse.ArgumentParser, argv: Any = None,
) -> argparse.Namespace:
    """`parser.parse_args(argv)`, with every declared host path resolved.

    The one call every skill ``main`` makes, so that a host-path stamp on an
    argument is enforced by the fact of the argument being parsed rather than
    by each handler remembering to ask. **Enforcement is at parse, not at
    dispatch**: ``run_skill_cli`` looks like the chokepoint and is not —
    ``money`` does not use it, and a handler-local existence probe would
    already have run by the time dispatch saw the value.

    A refusal is the facade's envelope on stdout and exit 1, with
    ``reason="host_path_refused"`` so the model reads a boundary rather than a
    missing file. That is also why this is not an argparse ``type=`` callable,
    which is otherwise the tempting shape: argparse reports a ``type`` failure
    through ``parser.error()`` — usage text on stderr and exit 2 — and getting
    the envelope out of that means every skill building its parser through a
    shared subclass, which is this conversion with more machinery and less
    visible control flow.

    A parser with no stamped argument behaves exactly as ``parse_args`` does,
    which is what let all twenty skill ``main`` functions be converted in one
    step with nothing declared.
    """
    args = parser.parse_args(argv)
    refusal = resolve_parsed(parser, args)
    if refusal is not None:
        fail(refusal, reason="host_path_refused")
        # `fail` exits. The raise is what makes that independent of it, and it
        # is the property `health.cmd_export_csv` used to keep for itself with
        # a bare `return` after its own `_fail`: a `fail` that ever stopped
        # exiting would return a namespace `resolve_parsed` left *partially*
        # rewritten — the dests before the refusing one absolute, the rest as
        # parsed, and nothing on it saying which is which — and every handler
        # would then run on the mixture. There is one refusal site now, so
        # there is one place to keep this true.
        raise SystemExit(1)  # pragma: no cover — `fail` is NoReturn
    return args


def run_skill_cli(
    commands: dict,
    args: Any,
    *,
    command: str | None = None,
    indent: int | None = 2,
    ensure_ascii: bool = False,
    default: Callable[[Any], Any] | None = None,
    on_exception: Callable[[BaseException], Any] | None = None,
    error_indent: int | None = None,
    error_ensure_ascii: bool = True,
    handlers_print: bool = False,
) -> None:
    """Dispatch one skill subcommand and apply the facade's exit-code rule.

    ``on_exception`` maps a raised exception onto the envelope to print; the
    default names the exception's message. It is where a skill that needs to
    say more than ``str(exc)`` — ``browse`` naming the endpoint it could not
    reach, ``devbox`` classifying a protocol refusal — puts that, so the
    epilogue itself stays one shape.

    A raised exception is serialized compact rather than through the ``indent``
    the result path uses, because that is what **nine of the ten** converted
    ``except`` branches already printed; ``skills/nextcloud`` routed its three
    through the same ``indent=2`` helper as its results and passes
    ``error_indent=2`` to keep them that way. The error path keeps the result
    path's ``default``, since that same helper carried ``str``.
    ``error_ensure_ascii`` is the other place they disagreed and ``devbox`` is
    the one caller that passes it.

    ``handlers_print`` is for the skills written the other way round — every
    ``cmd_*`` prints its own envelope and exits — where the return value means
    nothing and printing it would be a second envelope. Stated by the caller
    rather than inferred from a ``None`` return, because inferring it makes the
    epilogue's behaviour depend on a property no signature declares.

    **The serialization is inside the ``try`` and the status check is outside**,
    which is the split the convention names and is not cosmetic. A payload the
    encoder cannot take — a `datetime`, a `Decimal`, and none of the seven
    skills whose epilogue had this shape passes a ``default`` — has to come
    back as a well-formed error envelope, not as a traceback on stderr with
    empty stdout, which is the exact failure `skills/code_review` documents the
    facade as existing to prevent. The status check stays outside because a
    returned error envelope must fail the task just as a raised exception does,
    and running it inside would make an emitting handler's own ``SystemExit``
    look like a dispatch failure to any future ``except BaseException``.
    """
    name = args.command if command is None else command
    handler = commands.get(name)
    if handler is None:
        fail(f"unknown command: {name!r}")

    result = None
    try:
        result = handler(args)
        if not handlers_print and result is not None:
            emit(result, indent=indent, ensure_ascii=ensure_ascii, default=default,
                 exit_on_error=False)
    except Exception as exc:
        envelope = (
            error_envelope(str(exc)) if on_exception is None else on_exception(exc)
        )
        print(json.dumps(envelope, indent=error_indent,
                         ensure_ascii=error_ensure_ascii, default=default))
        sys.exit(1)

    if not handlers_print and is_error(result):
        sys.exit(1)
