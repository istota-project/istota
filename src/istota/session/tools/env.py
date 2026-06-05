"""Shared execution environment for native-brain tools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ToolEnv:
    """Per-task context every tool closes over.

    - ``cwd`` — working directory; relative paths resolve against it and Bash
      runs in it.
    - ``sandbox_wrap`` — wraps a raw argv (``["bash", "-c", …]``) with bwrap.
      ``None`` on macOS / when the sandbox is disabled (the wrap is a no-op).
    - ``subprocess_env`` — environment for Bash subprocesses (already
      credential-stripped by the caller). ``None`` inherits the parent env.
    - ``bash_timeout_seconds`` — default per-command wall-clock cap.
    - ``max_output_bytes`` — per-tool output cap before truncation.
    - ``max_read_lines`` — default line cap for Read.
    """

    cwd: Path
    sandbox_wrap: Callable[[list[str]], list[str]] | None = None
    subprocess_env: dict[str, str] | None = None
    bash_timeout_seconds: int = 120
    max_output_bytes: int = 30_000
    max_read_lines: int = 2000

    def resolve(self, path_str: str) -> Path:
        """Resolve a possibly-relative path against ``cwd``."""
        p = Path(path_str)
        return p if p.is_absolute() else (self.cwd / p)
