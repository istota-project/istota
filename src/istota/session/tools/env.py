"""Shared execution environment for native-brain tools."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


class ToolPathError(Exception):
    """A tool path escaped the confinement roots.

    Tools catch this and return an error ToolResult (never propagate it into
    the loop), so a model asking to read/write outside the workspace gets a
    clean tool error instead of crashing the run.
    """


@dataclass(frozen=True)
class WebFetchPolicy:
    """Resolved fetch policy the native WebFetch tool closes over.

    Threaded onto ``ToolEnv`` (like ``read_roots``) rather than passed to the
    tool factory, matching the existing pattern. ``None`` on ``ToolEnv`` means
    the tool is omitted from ``build_default_tools`` entirely.

    Safe defaults: HTTPS-only, no credentials, size/time capped, private/
    reserved IP destinations refused (SSRF), redirects re-validated per hop.
    """

    enabled: bool = True
    timeout_seconds: float = 20.0
    max_bytes: int = 5_000_000  # response body cap (streamed)
    max_content_chars: int = 100_000  # extracted-text cap returned to the model
    max_redirects: int = 5
    allow_http: bool = False  # http:// (cleartext) — off by default (CONNECT-only posture)
    allowed_ports: tuple[int, ...] = (80, 443)
    user_agent: str = "IstotaBot/1.0"
    # If non-empty, an allowlist: only these hosts (suffix match) may be fetched.
    allow_hosts: tuple[str, ...] = ()
    # Always-denied hosts (suffix match), applied after allow_hosts.
    block_hosts: tuple[str, ...] = ()
    # Operator additions to the built-in private/reserved IP blocklist (CIDRs).
    extra_blocked_cidrs: tuple[str, ...] = ()
    # If true, only fetch URLs seen in the task or prior tool output (blocks
    # model-fabricated URLs). Requires the in-context URL corpus threaded onto
    # ToolEnv (``web_fetch_url_corpus``); default-off threads nothing new.
    require_url_provenance: bool = False


def _realpath(p: Path) -> Path:
    """Resolve symlinks and normalize. Works for non-existent paths too — the
    existing prefix's symlinks are resolved, the rest is normalized — so a file
    the model is about to *create* is confined by its (existing) parent dir."""
    return Path(os.path.realpath(str(p)))


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
    - ``read_roots`` — when set, file tools (Read/Grep/Glob) may only touch
      paths inside these roots (symlink-resolved). ``None`` = unconfined (dev /
      unsandboxed). This is the native brain's stand-in for the bwrap
      filesystem isolation the claude_code path gets: the file tools run
      in-process (no bwrap), so the boundary must be enforced here. See NB-1.
    - ``write_roots`` — the writable subset (Write/Edit). Reads are allowed in
      ``read_roots`` (which the constructor unions with ``write_roots``); writes
      only in ``write_roots``. Ignored when ``read_roots`` is ``None``.
    """

    cwd: Path
    sandbox_wrap: Callable[[list[str]], list[str]] | None = None
    subprocess_env: dict[str, str] | None = None
    bash_timeout_seconds: int = 120
    max_output_bytes: int = 30_000
    max_read_lines: int = 2000
    # Hard byte cap on a single file read (Read / Grep per-file) so a multi-GB
    # file can't stall or OOM the worker before the line caps apply (NB-19).
    max_read_bytes: int = 25_000_000
    read_roots: tuple[Path, ...] | None = None
    write_roots: tuple[Path, ...] | None = None

    # Where Bash spills full over-cap output (task-scoped ISTOTA_DEFERRED_DIR).
    # ``None`` falls back to the system temp dir. Kept in the write-root set on a
    # confined env so the model can Read the spill back.
    deferred_dir: Path | None = None
    # Whether Bash spills over-cap output to a file (vs. cap-only truncation).
    bash_spill_full_output: bool = True

    # Native WebFetch policy. ``None`` → the tool is omitted from
    # ``build_default_tools`` (the model never sees it). See WebFetchPolicy.
    web_fetch: WebFetchPolicy | None = None
    # In-context URL corpus for ``require_url_provenance`` enforcement — URLs
    # present in the task prompt + prior tool output. ``None``/empty when the
    # provenance knob is off (the default path threads nothing new).
    web_fetch_url_corpus: frozenset[str] | None = None

    # Resolved (symlink-free) roots, populated in __post_init__. Not init args.
    _read_real: list[Path] | None = field(default=None, init=False, repr=False, compare=False)
    _write_real: list[Path] | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.read_roots is None:
            self._read_real = None
            self._write_real = None
            return
        writes = [_realpath(p) for p in (self.write_roots or ())]
        reads = [_realpath(p) for p in self.read_roots]
        # You can always read what you can write: fold the writable set into the
        # readable set (dedup, order-preserving).
        merged_reads = list(dict.fromkeys(reads + writes))
        self._read_real = merged_reads
        self._write_real = writes

    @property
    def confined(self) -> bool:
        """True when path confinement is active."""
        return self._read_real is not None

    def resolve(self, path_str: str, *, write: bool = False) -> Path:
        """Resolve a possibly-relative path against ``cwd``.

        When confinement is active, the resolved (symlink-free) target must lie
        inside an allowed root — ``write_roots`` for writes, the union of
        read+write roots for reads. Raises ``ToolPathError`` otherwise.
        """
        p = Path(path_str)
        candidate = p if p.is_absolute() else (self.cwd / p)

        if self._read_real is None:
            return candidate  # unconfined

        if self._contains(candidate, write=write):
            return candidate
        verb = "write to" if write else "read"
        raise ToolPathError(
            f"Cannot {verb} {candidate}: path is outside the allowed workspace."
        )

    def contains(self, path: Path, *, write: bool = False) -> bool:
        """True if ``path`` is allowed (or confinement is off).

        Used by Grep/Glob to drop individual result files that escape the roots
        via a symlink planted inside a root — ``resolve`` only guards the search
        root, not every file walked under it.
        """
        if self._read_real is None:
            return True
        return self._contains(path, write=write)

    def _contains(self, path: Path, *, write: bool) -> bool:
        roots = self._write_real if write else self._read_real
        real = _realpath(path)
        for root in roots or ():
            if real == root or real.is_relative_to(root):
                return True
        return False
