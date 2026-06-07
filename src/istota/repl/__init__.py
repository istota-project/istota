"""Terminal REPL surface — full-stack assistant over the configured brain.

``run_session`` drives the interactive loop; ``TerminalSubscriber`` renders the
task event stream. See ``cli.cmd_repl`` for the ``istota repl`` entry point.
"""

from .session import run_session
from .terminal import TerminalSubscriber

__all__ = ["run_session", "TerminalSubscriber"]
