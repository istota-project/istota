"""Interactive terminal REPL session.

A full-stack assistant turn loop: each input line becomes a ``source_type="repl"``
task with ``output_target="stream"``, executed inline via
``scheduler.run_task_inline`` (no daemon required — works on the bwrap host, on
Mac/dev, and via ``docker compose exec``). Tool/agent events stream live to the
terminal; deferred memory/kv/KG/health writes persist via the inline drain.

The stable ``repl-<user>-<uuid>`` conversation token (no colons, so it is a safe
``Channels/<token>/`` path and bwrap bind) is what gives multi-turn context:
``db.get_conversation_history``, channel memory, and sticky-skill carry-forward
all key off it once the executor's interactive gates include ``repl``.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from .terminal import TerminalSubscriber

if TYPE_CHECKING:
    from ..config import Config

_HELP = """\
Commands:
  /exit, /quit   end the session
  /clear         start a fresh conversation (new context)
  /model <alias> set the model for subsequent turns (e.g. opus, sonnet, default)
  /effort <tier> set reasoning effort (low|medium|high|xhigh|max|default)
  /help          show this help
Anything else is sent to the assistant.
"""


def _mint_token(user_id: str) -> str:
    """Filesystem-safe stable conversation token (no colons)."""
    return f"repl-{user_id}-{uuid.uuid4().hex[:8]}"


def _resolve_workspace(workspace: str) -> Path | None:
    """Map the --workspace flag to a directory (or None for the temp dir).

    ``cwd`` (default) → the launch directory; ``standard`` → None (per-user temp
    dir, the daemon default); an explicit path → that path.
    """
    if workspace in ("", "cwd"):
        return Path.cwd()
    if workspace == "standard":
        return None
    return Path(workspace).expanduser()


def run_session(
    config: "Config",
    *,
    user_id: str,
    token: str | None = None,
    workspace: str = "cwd",
    model: str | None = None,
    effort: str | None = None,
    input_fn=input,
    stream=None,
) -> None:
    """Drive the interactive loop until /exit or EOF.

    ``input_fn`` / ``stream`` are injectable for testing.
    """
    from .. import db
    from ..brain import make_brain
    from ..events import EventWriter
    from ..scheduler import run_task_inline

    brain = make_brain(config.brain)
    sub = TerminalSubscriber(stream=stream)
    token = token or _mint_token(user_id)
    workspace_dir = _resolve_workspace(workspace)
    cur_model = model
    cur_effort = effort

    def _say(text: str) -> None:
        print(text, file=stream) if stream is not None else print(text)

    _say(f"istota repl — user={user_id} token={token}")
    _say(f"workspace={workspace_dir or '(standard temp dir)'}  (/help for commands)")

    while True:
        try:
            line = input_fn("» ")
        except EOFError:
            _say("")
            break
        except KeyboardInterrupt:
            # Ctrl-C at the prompt (no task running) — just reprompt.
            _say("")
            continue

        line = line.strip()
        if not line:
            continue

        if line.startswith("/"):
            cmd, _, arg = line[1:].partition(" ")
            cmd = cmd.lower().strip()
            arg = arg.strip()
            if cmd in ("exit", "quit"):
                break
            if cmd == "help":
                _say(_HELP)
                continue
            if cmd == "clear":
                token = _mint_token(user_id)
                _say(f"new conversation: {token}")
                continue
            if cmd == "model":
                cur_model = None if arg in ("", "default") else arg
                _say(f"model = {cur_model or '(default)'}")
                continue
            if cmd == "effort":
                cur_effort = None if arg in ("", "default") else arg
                _say(f"effort = {cur_effort or '(default)'}")
                continue
            _say(f"unknown command: /{cmd} (/help for the list)")
            continue

        resolved_model = brain.resolve_model_name(cur_model) if cur_model else None

        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt=line, user_id=user_id, source_type="repl",
                conversation_token=token, output_target="stream",
                model=resolved_model or None, effort=cur_effort or None,
            )
            task = db.get_task(conn, task_id)

        writer = EventWriter(task_id, str(config.db_path))
        writer.subscribe(sub)

        # Run the task in a worker thread so Ctrl-C can request a cooperative
        # cancel (db.cancel_task → the brain's cancel_check observes it) rather
        # than tearing down the interpreter mid-turn.
        def _worker() -> None:
            run_task_inline(
                config, task, event_writer=writer, workspace_dir=workspace_dir,
            )

        worker = threading.Thread(target=_worker, daemon=True)
        worker.start()
        cancelled = False
        while worker.is_alive():
            try:
                worker.join(0.2)
            except KeyboardInterrupt:
                if not cancelled:
                    cancelled = True
                    _say("cancelling… (Ctrl-C)")
                    with db.get_db(config.db_path) as conn:
                        db.cancel_task(conn, task_id)
        worker.join()
