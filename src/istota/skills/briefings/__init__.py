"""Briefings skill — in-process facade for the briefings content CLI.

Resolves the user's :class:`BriefingsContext` via
:func:`istota.briefings.resolve_for_user` and forwards its argv to
:mod:`istota.briefings.cli` through Click's ``CliRunner`` — a thin passthrough
so new CLI subcommands need no facade change. No subprocess, no HTTP. Mirrors
:mod:`istota.skills.feeds`.

Lets the model manage briefing blocks/sources conversationally, e.g.::

    istota-skill briefings list
    istota-skill briefings blocks list --briefing morning
    istota-skill briefings blocks add --briefing morning --title "World News"
    istota-skill briefings sources add --block 3 --kind rss --config '{"feed_ref": {...}}'
    istota-skill briefings archive list
"""

import json
import os
import sys

from istota.skills._cli import emit, error_envelope
from istota.skills._hostpath import HostPathRefused


def build_cli():
    """The Click group this skill forwards to.

    `build_parser`'s counterpart for a Click CLI, and the reason this skill is
    no longer exempt from `tests/test_skill_host_paths_coverage.py`: that walk
    finds a skill's arguments by finding the function that builds its parser,
    so a Click skill exposing one is walked like any other and a path-shaped
    parameter added to `istota.briefings.cli` has to carry a disposition.
    Function-scope import, like everything else the passthrough touches — the
    briefings package pulls in the database layer.
    """
    from istota.briefings.cli import cli

    return cli


def _run(args: list[str]) -> dict:
    from click.testing import CliRunner

    from istota.briefings import UserNotFoundError, ensure_initialised, resolve_for_user
    from istota.config import load_config

    # `build_cli()` rather than a second import of the same name: the coverage
    # walk asserts against what that function returns, and a stamp enforced on
    # a group this facade does not actually invoke is a claim about nothing.
    cli = build_cli()

    user_id = os.environ.get("BRIEFINGS_USER", "") or ""
    if not user_id:
        return {"status": "error", "error": "BRIEFINGS_USER not set"}

    istota_cfg = load_config()
    try:
        ctx = resolve_for_user(user_id, istota_cfg)
    except UserNotFoundError as e:
        return {"status": "error", "error": str(e)}

    ensure_initialised(ctx, app_config=istota_cfg)

    runner = CliRunner()
    result = runner.invoke(
        cli, args, obj=ctx, standalone_mode=False, catch_exceptions=True,
    )

    if isinstance(result.exception, HostPathRefused):
        # The facade's own refusal, in the shape every other skill CLI
        # answers with. Without this it would fall through to the generic
        # branch below as `HostPathRefused: ...` in the error text, with no
        # `reason` — and `reason` is the one field telling the model it hit a
        # boundary rather than a file that is not there.
        return error_envelope(str(result.exception), reason="host_path_refused")
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        return {
            "status": "error",
            "error": f"{type(result.exception).__name__}: {result.exception}",
        }
    if result.exit_code not in (0, None):
        return {
            "status": "error",
            "error": (result.output or f"exit {result.exit_code}").strip(),
        }

    output = (result.output or "").strip()
    if not output:
        return {"status": "error", "error": "no output from briefings CLI"}
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return {"status": "ok", "raw": output}


def _output(data) -> None:
    emit(data)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(
            "Usage: istota-skill briefings <list|blocks|sources|archive> ...",
            file=sys.stderr,
        )
        sys.exit(1)
    _output(_run(argv))


if __name__ == "__main__":
    main()
