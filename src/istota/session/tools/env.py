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
    - ``write_denied_roots`` — read-only carve-outs nested *inside* a write
      root. A path under one of these is readable but never writable, which is
      what ``build_bwrap_cmd`` gets for free by re-binding a subdirectory
      ``--ro-bind`` after its parent's read-write bind. Containment alone can't
      express that: ``.developer`` sits inside ``user_temp_dir``, so without
      this the model could rewrite ``credential-fetch``. Unlike the two above,
      this one is enforced whether or not confinement is active, and its empty
      value is ``()`` rather than ``None`` — a deny set has no unconfined
      meaning to signal.

    The resolved forms of the three root lists are computed once, in
    ``__post_init__``. The dataclass is not frozen (matching its neighbours),
    so reassigning any of them after construction leaves the resolved copies
    stale. Build a new ``ToolEnv`` instead; nothing mutates one today.
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
    write_denied_roots: tuple[Path, ...] = ()

    # Where Bash spills full over-cap output (task-scoped ISTOTA_DEFERRED_DIR).
    # ``None`` falls back to the system temp dir. Kept in the write-root set on a
    # confined env so the model can Read the spill back.
    deferred_dir: Path | None = None
    # Whether Bash spills over-cap output to a file (vs. cap-only truncation).
    bash_spill_full_output: bool = True
    # Per-task cgroup v2 directory (A6), or ``None`` where the deployment has
    # no delegated subtree. Each child Bash spawns places *itself* into it from
    # ``preexec_fn``, before it execs — membership is inherited at ``fork``, so
    # moving it afterwards would leave everything the child had already forked
    # outside the group for good (ISSUE-285). This is the only way this brain's
    # subprocesses get contained: it has no single long-lived child for the
    # executor's ``on_pid`` path to place.
    task_cgroup: Path | None = None

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
    _write_denied_real: list[Path] = field(default_factory=list, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Resolved unconditionally, and consulted unconditionally: the deny
        # check in ``resolve``/``contains`` runs ahead of the unconfined early
        # return. No caller sets a deny root without confinement today, so this
        # costs an empty-list scan; what it buys is that a future
        # ``ToolEnv(cwd=…, write_denied_roots=…)`` with no ``read_roots``
        # refuses the write it looks like it refuses.
        self._write_denied_real = [_realpath(p) for p in self.write_denied_roots]
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

        Returns the **symlink-resolved** path, which is what the checks below
        ran against. Callers must operate on the returned value rather than on
        what they passed in: checking one path and opening another lets an
        intermediate component be swapped between the two.
        ``skill_host_paths.resolve_host_path`` states the same rule for the
        host-side skill CLIs.

        When confinement is active, the target must lie inside an allowed root
        — ``write_roots`` for writes, the union of read+write roots for reads.
        A write into ``write_denied_roots`` is refused whether or not
        confinement is active. Raises ``ToolPathError`` otherwise.
        """
        p = Path(path_str)
        candidate = p if p.is_absolute() else (self.cwd / p)
        real = _realpath(candidate)

        # Ahead of the unconfined return: a deny root is a statement about a
        # path, not about whether a root allowlist happens to be configured.
        if write and self._in_denied(real):
            # Distinct from "outside": the path is inside the workspace and is
            # readable. Reporting it as outside sends the caller looking for a
            # missing root that is in fact present.
            raise ToolPathError(
                f"Cannot write to {candidate}: path is read-only in this workspace."
            )

        if self._read_real is None:
            return real  # unconfined

        if self._contains(real, write=write):
            return real
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
        if write and self._in_denied(path):
            return False
        if self._read_real is None:
            return True
        return self._contains(path, write=write)

    def _in_denied(self, path: Path) -> bool:
        real = _realpath(path)
        return any(
            real == denied or real.is_relative_to(denied)
            for denied in self._write_denied_real
        )

    def _contains(self, path: Path, *, write: bool) -> bool:
        roots = self._write_real if write else self._read_real
        real = _realpath(path)
        # Denied before allowed: a carve-out is always nested inside a root
        # that would otherwise admit it, so order is the whole mechanism.
        if write and self._in_denied(real):
            return False
        for root in roots or ():
            if real == root or real.is_relative_to(root):
                return True
        return False
