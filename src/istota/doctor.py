"""Runtime self-check: every environmental fact istota depends on, named once.

A whole class of istota bug is invisible to the test suite by construction. The
suite asserts against Python objects on a developer's macOS host; production is
a built image, a rendered ``config.toml``, a ``PATH`` and a bubblewrap
namespace. The forge-CLI failures (ISSUE-263 and its neighbours) were all the
same shape — a disagreement between what the code assumed about its runtime and
what the runtime was — and none of them was a Python defect.

This module writes those assumptions down. Each check answers one question
about the host, returns a :class:`CheckResult`, and never raises. It runs at
daemon start-up, on a scheduler interval, from ``istota doctor``, and from the
admin dashboard. It is also the oracle the image and smoke test tiers reuse
instead of hand-writing assertions that drift from the code.

Two constraints shape the design and are easy to violate by accident:

**No check on the config-load path may spawn a process.** ``_validate_forge_clis``
is called unconditionally from ``load_config``, and ``load_config`` runs in the
daemon, the web app, the webhook receiver, every CLI invocation, and every
host-side skill CLI the skill proxy spawns *per call*. ``probe=False`` is what
keeps a free ``os.path.exists`` from becoming five ``--version`` spawns.

**A check can only be an oracle for a test if the test names the environment
that makes it run.** The ``developer.*`` checks ``SKIP`` when no token is
configured — correct for an operator, and fatal for a test, because a suite
asserting "no FAIL" is green on exactly the broken image. Callers asserting over
doctor must assert the checks they care about did not ``SKIP``.

Plain functions over plain data: no classes beyond the frozen result record, and
no decorator-driven registration — a decorator makes the set of checks depend on
what happened to be imported.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only; a runtime import is a cycle
    from .config import Config

logger = logging.getLogger(__name__)

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"

# What a check is a property *of*. An image-scoped check can be answered by a
# bare `docker run` with no volumes; a deployment-scoped one needs a real
# install (a mount, a database, a network). The image test tier asserts only
# over the former — without the split it would fail on a perfectly good image
# (no /mnt/shared, no DB), and the tempting repair is to soften the runtime
# check, which weakens the product to make a test green.
IMAGE, DEPLOYMENT = "image", "deployment"

# How long a probed subprocess gets. Doctor runs on the start-up path and from
# an HTTP handler; an unbounded wait on a wedged binary is an outage.
PROBE_TIMEOUT = 10

# The deep sandbox probe spawns bubblewrap around a shell. Bounded separately
# and more generously, and a timeout is reported as FAIL rather than hanging.
DEEP_TIMEOUT = 30

# Below this length a configured "credential" is a placeholder or a mode string,
# not something worth scanning rendered output for.
_MIN_SECRET_LEN = 8

_REDACTED = "[redacted]"


@dataclass(frozen=True)
class CheckResult:
    """One answer about the host.

    ``name`` is a stable dotted id and the only thing machine consumers key on.
    ``detail`` is one line saying what was *observed* — never what was expected,
    and never a credential. ``remedy`` says what to do about it and is required
    for every ``WARN`` and ``FAIL``; a finding an operator cannot act on is a
    log line, not a check.
    """

    name: str
    status: str
    detail: str
    remedy: str = ""
    scope: str = DEPLOYMENT


# A check takes the loaded config and whether it may spawn anything, and
# returns one result or several.
Check = Callable[["Config", bool], "CheckResult | list[CheckResult]"]


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def parse_version(text: str) -> tuple[int, int, int] | None:
    """First ``N.N.N`` (or ``N.N``) triple in `text`, or None if there is none.

    Written against the real output shapes rather than a guessed grammar::

        gh version 2.98.0 (2026-01-01)
        glab 1.114.0
        glab version 1.114.0 (2026-01-01)

    Returning None rather than raising is deliberate: an unparseable banner is a
    ``WARN`` about a CLI we cannot judge, not a crash in a diagnostic.
    """
    if not text:
        return None
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if match is None:
        return None
    major, minor, patch = match.group(1), match.group(2), match.group(3)
    return (int(major), int(minor), int(patch or 0))


def _run(argv: list[str], *, timeout: int = PROBE_TIMEOUT) -> subprocess.CompletedProcess | None:
    """Run `argv`, returning None on anything that stops it producing output.

    Every caller is a check, and a check that let an OSError out would take the
    daemon's start-up path with it.
    """
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _executable(path: str | Path) -> bool:
    p = Path(path)
    return p.is_file() and os.access(p, os.X_OK)


def _binary_status(path: str, *, probe: bool) -> tuple[str, str]:
    """``(status, detail)`` for "is this binary usable", honouring `probe`.

    Under ``probe=False`` the answer comes from the filesystem alone and says
    so, because an operator reading the result otherwise cannot tell whether
    anything was actually executed.
    """
    p = Path(path)
    if not p.exists():
        return FAIL, f"{path} does not exist"
    if not _executable(p):
        return FAIL, f"{path} is present but not executable"
    if not probe:
        return OK, f"{path} exists and is executable (not executed: probe disabled)"
    result = _run([str(p), "--version"])
    if result is None:
        return FAIL, f"{path} could not be executed"
    if result.returncode != 0:
        return FAIL, f"{path} exited {result.returncode} on --version"
    banner = (result.stdout or result.stderr or "").strip().splitlines()
    return OK, f"{path}: {banner[0] if banner else 'ran, no version output'}"


def _dev_gate(config: "Config") -> tuple[object | None, str]:
    """The developer skill's gating, lifted verbatim from ``_validate_forge_clis``.

    Returns ``(developer_config, "")`` when the checks should run, or
    ``(None, reason)`` when they must ``SKIP``. Without this a tokenless
    developer-skill deployment goes from silent today to alerting after this
    lands, and the boot alert makes that loud.
    """
    dev = getattr(config, "developer", None)
    if dev is None or not dev.enabled:
        return None, "developer skill is disabled"
    if not dev.repos_dir:
        return None, "developer skill has no repos_dir configured"
    return dev, ""


def _forge_token_gate(dev) -> str:
    """"" when a forge token is configured, else the reason to SKIP."""
    if dev.gitlab_token or dev.github_token:
        return ""
    return "no forge token configured"


def _bwrap_usable() -> bool:
    """Whether bubblewrap can actually create a namespace here.

    Delegates to the executor's own cached probe so doctor and the sandbox agree
    on one answer. Imported lazily: `executor` pulls in most of the package.
    """
    try:
        from .executor import _bwrap_available

        return _bwrap_available()
    except Exception:  # pragma: no cover - defensive; never fail a check
        return False


# ---------------------------------------------------------------------------
# runtime.*
# ---------------------------------------------------------------------------


def check_platform(config: "Config", probe: bool) -> CheckResult:
    """OS and architecture, reported always.

    Everything istota knows about its own runtime has historically been asserted
    on darwin — the one platform that cannot run the sandbox. Saying so out loud
    is the cheapest check here and the one that explains the others.
    """
    system, machine = platform.system(), platform.machine()
    detail = f"{system} {machine}"
    if system == "Linux":
        return CheckResult("runtime.platform", OK, detail, scope=IMAGE)
    if getattr(config.security, "sandbox_enabled", False):
        return CheckResult(
            "runtime.platform",
            FAIL,
            f"{detail}; bubblewrap sandboxing is enabled but only runs on Linux",
            remedy=(
                "Run on Linux, or set [security] sandbox_enabled = false to "
                "accept an unsandboxed deployment."
            ),
            scope=IMAGE,
        )
    return CheckResult(
        "runtime.platform",
        WARN,
        f"{detail}; not a supported deployment platform",
        remedy="Linux + bubblewrap is the only supported deployment shape.",
        scope=IMAGE,
    )


def check_bwrap(config: "Config", probe: bool) -> CheckResult:
    """``bwrap`` is installed and runnable. The sandbox is the per-user boundary."""
    if not getattr(config.security, "sandbox_enabled", False):
        return CheckResult(
            "runtime.bwrap", SKIP, "sandbox is disabled ([security] sandbox_enabled)", scope=IMAGE
        )
    path = shutil.which("bwrap")
    if path is None:
        return CheckResult(
            "runtime.bwrap",
            FAIL,
            "bwrap is not on PATH",
            remedy="Install bubblewrap (apt-get install bubblewrap).",
            scope=IMAGE,
        )
    status, detail = _binary_status(path, probe=probe)
    return CheckResult(
        "runtime.bwrap",
        status,
        detail,
        remedy="" if status == OK else "Install a working bubblewrap; the sandbox needs it.",
        scope=IMAGE,
    )


def check_model_cli(config: "Config", probe: bool) -> CheckResult:
    """The ``claude`` CLI the subprocess brains exec.

    Resolved from the daemon's PATH, matching ``ClaudeCodeBrain``'s own spawn
    (``["claude", "-p", "-"]``) — a check against a path the brain does not use
    would be asserting about the wrong thing.
    """
    kind = getattr(config.brain, "kind", "claude_code")
    if kind not in ("claude_code", "tmux_claude"):
        return CheckResult(
            "runtime.model_cli",
            SKIP,
            f"brain.kind = {kind!r} runs the agent loop in-process (native), no CLI needed",
            scope=IMAGE,
        )
    path = shutil.which("claude")
    if path is None:
        return CheckResult(
            "runtime.model_cli",
            FAIL,
            f"brain.kind = {kind!r} but no `claude` on PATH",
            remedy="Install the Claude Code CLI, or switch to brain.kind = \"native\".",
            scope=IMAGE,
        )
    status, detail = _binary_status(path, probe=probe)
    return CheckResult(
        "runtime.model_cli",
        status,
        detail,
        remedy="" if status == OK else "Reinstall the Claude Code CLI.",
        scope=IMAGE,
    )


def check_tmux(config: "Config", probe: bool) -> CheckResult:
    """``tmux``, needed only by the brain that drives the interactive TUI."""
    kind = getattr(config.brain, "kind", "claude_code")
    if kind != "tmux_claude":
        return CheckResult(
            "runtime.tmux", SKIP, f"brain.kind = {kind!r} does not use tmux", scope=IMAGE
        )
    path = shutil.which("tmux")
    if path is None:
        return CheckResult(
            "runtime.tmux",
            FAIL,
            "brain.kind = 'tmux_claude' but no `tmux` on PATH",
            remedy="Install tmux, or switch brain.kind.",
            scope=IMAGE,
        )
    # tmux answers `-V`, not `--version`.
    if not probe:
        return CheckResult(
            "runtime.tmux",
            OK if _executable(path) else FAIL,
            f"{path} exists and is executable (not executed: probe disabled)",
            remedy="" if _executable(path) else "Install a working tmux.",
            scope=IMAGE,
        )
    result = _run([path, "-V"])
    if result is None or result.returncode != 0:
        return CheckResult(
            "runtime.tmux",
            FAIL,
            f"{path} could not be executed",
            remedy="Install a working tmux.",
            scope=IMAGE,
        )
    return CheckResult(
        "runtime.tmux", OK, f"{path}: {(result.stdout or '').strip()}", scope=IMAGE
    )


def check_framework_db(config: "Config", probe: bool) -> CheckResult:
    """The framework DB opens and ``PRAGMA quick_check`` is clean.

    Read-only on purpose. ``scheduler.check_db_health`` owns the ``REINDEX``
    self-repair; a diagnostic that silently mutated the thing it was diagnosing
    would make its own next answer meaningless.
    """
    import sqlite3

    from .db_health import quick_check

    db_path = Path(config.db_path)
    if not db_path.exists():
        return CheckResult(
            "runtime.framework_db",
            WARN,
            f"{db_path} does not exist",
            remedy="Run `istota init` to create the framework database.",
        )
    try:
        # Read-only, via the URI form. A read-write open of a WAL database
        # materializes the `-wal` / `-shm` sidecars, so `sudo istota doctor`
        # against a stopped daemon would leave root-owned files the daemon's own
        # user then cannot open. A diagnostic must not be able to do that.
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.DatabaseError as exc:
        return CheckResult(
            "runtime.framework_db",
            FAIL,
            f"{db_path} could not be opened: {exc}",
            remedy="Restore the database from a snapshot (`python -m istota.db_restore`).",
        )
    try:
        issues = quick_check(conn)
    except sqlite3.DatabaseError as exc:
        return CheckResult(
            "runtime.framework_db",
            FAIL,
            f"{db_path} failed quick_check: {exc}",
            remedy="Restore the database from a snapshot (`python -m istota.db_restore`).",
        )
    finally:
        conn.close()
    if issues:
        return CheckResult(
            "runtime.framework_db",
            FAIL,
            f"{db_path}: quick_check reported {len(issues)} issue(s)",
            remedy=(
                "The scheduler's db-health sweep attempts a REINDEX; if it does not "
                "clear, restore from a snapshot (`python -m istota.db_restore`)."
            ),
        )
    return CheckResult("runtime.framework_db", OK, f"{db_path}: quick_check clean")


def check_writable_dirs(config: "Config", probe: bool) -> list[CheckResult]:
    """The directories the daemon writes to exist and are writable.

    One result per directory, so a failure names which one rather than making an
    operator guess from a combined line.
    """
    candidates: list[tuple[str, Path | None]] = [
        ("temp_dir", Path(config.temp_dir)),
    ]
    try:
        candidates.append(("module_db_root", config.module_db_root()))
    except Exception as exc:  # noqa: BLE001 - a misconfigured module_data_dir raises
        candidates.append(("module_db_root", None))
        module_root_error = str(exc)
    else:
        module_root_error = ""
    if config.nextcloud_mount_path is not None:
        candidates.append(("mount", Path(config.nextcloud_mount_path)))

    results: list[CheckResult] = []
    for label, path in candidates:
        name = f"runtime.writable_dirs.{label}"
        if path is None:
            results.append(
                CheckResult(
                    name,
                    FAIL,
                    f"{label} could not be resolved: {module_root_error}",
                    remedy="Fix `module_data_dir`; it must not resolve under the Nextcloud mount.",
                )
            )
            continue
        if not path.exists():
            # Not a failure by itself: several of these are created on first
            # use. What matters is whether the *parent* would allow that.
            parent = path.parent
            if parent.is_dir() and os.access(parent, os.W_OK):
                results.append(
                    CheckResult(name, OK, f"{path} does not exist yet; {parent} is writable")
                )
            else:
                results.append(
                    CheckResult(
                        name,
                        FAIL,
                        f"{path} does not exist and {parent} is not writable",
                        remedy=f"Create {path} and make it writable by the daemon's user.",
                    )
                )
            continue
        if not os.access(path, os.W_OK):
            results.append(
                CheckResult(
                    name,
                    FAIL,
                    f"{path} is not writable",
                    remedy=f"chown/chmod {path} so the daemon's user can write to it.",
                )
            )
            continue
        results.append(CheckResult(name, OK, f"{path} is writable"))
    return results


def check_mount_liveness(config: "Config", probe: bool) -> CheckResult:
    """A configured Nextcloud mount is actually mounted.

    An rclone mount that dropped leaves a plain empty directory behind, which
    every path check above happily reports as fine while every read returns
    nothing. ``ismount`` is the only cheap way to tell the two apart.

    Gated on the workspace actually being Nextcloud-backed, not merely on a path
    being configured. The local single-user install sets
    ``nextcloud_mount_path`` to a plain directory under ``~`` and nothing ever
    mounts it — asserting ``ismount`` there reports a healthy install as broken.
    ``storage_is_nextcloud`` is the existing distinction between the two shapes.
    """
    mount = config.nextcloud_mount_path
    if mount is None:
        return CheckResult(
            "runtime.mount_liveness", SKIP, "no nextcloud_mount_path configured"
        )
    if not config.storage_is_nextcloud:
        return CheckResult(
            "runtime.mount_liveness",
            SKIP,
            f"{mount} is a local workspace folder, not a mount (no Nextcloud URL configured)",
        )
    path = Path(mount)
    if os.path.ismount(path):
        return CheckResult("runtime.mount_liveness", OK, f"{path} is a mount point")
    return CheckResult(
        "runtime.mount_liveness",
        FAIL,
        f"{path} is configured as the workspace mount but is not a mount point",
        remedy=(
            "Check the rclone mount unit; a dropped mount leaves an empty directory "
            "that reads as an empty workspace."
        ),
    )


# ---------------------------------------------------------------------------
# security.*
# ---------------------------------------------------------------------------


def check_skill_proxy(config: "Config", probe: bool) -> list[CheckResult]:
    """The skill proxy's two independent facts.

    ``security.skill_proxy`` — the ``istota-skill`` entry point resolves on the
    daemon's own PATH. The proxy spawns it per call; an unresolvable one turns
    every skill CLI into a command-not-found inside somebody's task.

    ``security.skill_proxy.forge_posture`` — the wording is preserved from
    ``_validate_forge_clis`` because it is a security posture statement, not a
    bug report: with the proxy off the forge token sits in the environment the
    model's own shell inherits rather than being injected per call.
    """
    results: list[CheckResult] = []
    enabled = getattr(config.security, "skill_proxy_enabled", True)

    if not enabled:
        results.append(
            CheckResult(
                "security.skill_proxy",
                SKIP,
                "[security] skill_proxy_enabled = false",
            )
        )
    else:
        path = shutil.which("istota-skill")
        if path is None:
            results.append(
                CheckResult(
                    "security.skill_proxy",
                    FAIL,
                    "the skill proxy is enabled but `istota-skill` is not on the daemon's PATH",
                    remedy=(
                        "Install the package so its console scripts are on PATH "
                        "(the docker image sets ENV PATH for exactly this)."
                    ),
                )
            )
        else:
            results.append(
                CheckResult("security.skill_proxy", OK, f"istota-skill resolves to {path}")
            )

    dev, reason = _dev_gate(config)
    if dev is None:
        posture_reason = reason
    elif _forge_token_gate(dev):
        posture_reason = _forge_token_gate(dev)
    elif enabled:
        posture_reason = "the skill proxy is enabled; tokens are injected per call"
    else:
        posture_reason = ""

    if posture_reason:
        results.append(
            CheckResult("security.skill_proxy.forge_posture", SKIP, posture_reason)
        )
    else:
        results.append(
            CheckResult(
                "security.skill_proxy.forge_posture",
                WARN,
                (
                    "forge tokens are configured but [security] skill_proxy_enabled = false; "
                    "gh and glab will work — the policy grants them the ambient token — but "
                    "that token is readable by anything else the task runs, instead of being "
                    "injected per call"
                ),
                remedy="Enable the skill proxy to keep the token out of the task environment.",
            )
        )
    return results


# ---------------------------------------------------------------------------
# developer.*
# ---------------------------------------------------------------------------

_FORGE_BINARIES = ("gh", "glab")


def _resolved_forge_bin(dev, name: str) -> str:
    """What the wrapper would actually exec for `name`."""
    # The leaf, not `skills.developer` — reaching the same function through the
    # skill package costs ~190ms of import on every `load_config`, which is the
    # exact expense `probe=False` exists to avoid.
    from .forge_bin import resolve_real_bin

    configured = dev.gh_bin_path if name == "gh" else dev.glab_bin_path
    return resolve_real_bin(configured, name)


def _configured_forge_bin(dev, name: str) -> str:
    return dev.gh_bin_path if name == "gh" else dev.glab_bin_path


def check_forge_binaries(config: "Config", probe: bool) -> list[CheckResult]:
    """The binary the wrapper will exec exists and is executable.

    This is the ISSUE-263 shape exactly: ``setup_env`` wrote the wrappers, ``gh``
    resolved on PATH to one, and the wrapper's ``os.execve`` hit a path that did
    not exist and exited 6 — after clone, branch and push had all worked, so the
    skill looked configured and died only where it would publish.

    ``FAIL`` is reserved for this, because it is unambiguous and needs no
    version knowledge.
    """
    dev, reason = _dev_gate(config)
    if dev is None:
        return [
            CheckResult(f"developer.forge_binaries.{n}", SKIP, reason, scope=IMAGE)
            for n in _FORGE_BINARIES
        ]
    token_reason = _forge_token_gate(dev)
    if token_reason:
        return [
            CheckResult(f"developer.forge_binaries.{n}", SKIP, token_reason, scope=IMAGE)
            for n in _FORGE_BINARIES
        ]

    results: list[CheckResult] = []
    for name in _FORGE_BINARIES:
        resolved = _resolved_forge_bin(dev, name)
        status, detail = _binary_status(resolved, probe=probe)
        results.append(
            CheckResult(
                f"developer.forge_binaries.{name}",
                status,
                detail,
                remedy=(
                    ""
                    if status == OK
                    else (
                        f"Install {name} and point [developer] {name}_bin_path at it; "
                        f"every forge command will otherwise fail at exec time."
                    )
                ),
                scope=IMAGE,
            )
        )
    return results


def check_forge_config_drift(config: "Config", probe: bool) -> list[CheckResult]:
    """The configured path is the path resolution actually returns.

    ``_resolve_real_bin``'s fallback chain is correct and load-bearing — it is
    what makes a code-only auto-update keep working — but it *hides* the stale
    ``config.toml`` that ``config.py`` used to warn about. Routing the only
    check through resolution therefore reports ``ok`` on exactly the drifted
    deployment this exists to catch.

    ``WARN``, never ``FAIL``: the deployment works. What it has lost is the
    property that its config file describes it.
    """
    dev, reason = _dev_gate(config)
    if dev is None:
        return [
            CheckResult(f"developer.forge_config_drift.{n}", SKIP, reason)
            for n in _FORGE_BINARIES
        ]
    token_reason = _forge_token_gate(dev)
    if token_reason:
        return [
            CheckResult(f"developer.forge_config_drift.{n}", SKIP, token_reason)
            for n in _FORGE_BINARIES
        ]

    results: list[CheckResult] = []
    for name in _FORGE_BINARIES:
        configured = _configured_forge_bin(dev, name)
        resolved = _resolved_forge_bin(dev, name)
        exists = Path(configured).exists() if configured else False
        if exists and configured == resolved:
            results.append(
                CheckResult(
                    f"developer.forge_config_drift.{name}",
                    OK,
                    f"[developer] {name}_bin_path = {configured} is what resolution returns",
                )
            )
            continue
        # Two conditions, two messages. An operator who set an explicit path that
        # does not exist gets `configured == resolved` from `_resolve_real_bin`
        # (it returns a chosen path as given rather than exec'ing something
        # else), so a single combined message reads as the self-contradicting
        # "x but the wrapper will exec x".
        if configured and configured == resolved:
            results.append(
                CheckResult(
                    f"developer.forge_config_drift.{name}",
                    WARN,
                    (
                        f"[developer] {name}_bin_path = {configured} is what the wrapper "
                        f"will exec, but nothing exists there"
                    ),
                    remedy=(
                        f"Install {name} at that path, or point {name}_bin_path at the "
                        f"real one. An explicitly chosen path is never silently replaced."
                    ),
                )
            )
            continue
        results.append(
            CheckResult(
                f"developer.forge_config_drift.{name}",
                WARN,
                (
                    f"[developer] {name}_bin_path = {configured or '(unset)'} but the wrapper "
                    f"will exec {resolved}"
                ),
                remedy=(
                    f"Rewrite config.toml so {name}_bin_path names the installed binary "
                    f"(a full Ansible play does; the auto-update cron does not, and the "
                    f"docker entrypoint writes config.toml only onto a fresh volume)."
                ),
            )
        )
    return results


def check_forge_versions(config: "Config", probe: bool) -> list[CheckResult]:
    """The observed version against the version this deployment was exercised at.

    Deliberately not a floor. No floor has ever been derived from the verbs the
    developer skill uses, and inventing one would fail a working host on a CLI
    that does everything asked of it. ``WARN`` naming both numbers is more
    actionable than a failure against a threshold nobody established — and a
    genuinely too-old CLI announces itself as a command error within one task.
    """
    from .forge_cli import GH_KNOWN_GOOD, GLAB_KNOWN_GOOD

    known_good = {"gh": GH_KNOWN_GOOD, "glab": GLAB_KNOWN_GOOD}

    dev, reason = _dev_gate(config)
    if dev is None:
        return [
            CheckResult(f"developer.forge_versions.{n}", SKIP, reason, scope=IMAGE)
            for n in _FORGE_BINARIES
        ]
    token_reason = _forge_token_gate(dev)
    if token_reason:
        return [
            CheckResult(f"developer.forge_versions.{n}", SKIP, token_reason, scope=IMAGE)
            for n in _FORGE_BINARIES
        ]
    if not probe:
        return [
            CheckResult(
                f"developer.forge_versions.{n}",
                SKIP,
                "a version cannot be observed without running the binary (probe disabled)",
                scope=IMAGE,
            )
            for n in _FORGE_BINARIES
        ]

    results: list[CheckResult] = []
    for name in _FORGE_BINARIES:
        resolved = _resolved_forge_bin(dev, name)
        expected = known_good[name]
        expected_text = ".".join(str(part) for part in expected)
        if not _executable(resolved):
            results.append(
                CheckResult(
                    f"developer.forge_versions.{name}",
                    SKIP,
                    f"{resolved} is not runnable; developer.forge_binaries carries that",
                    scope=IMAGE,
                )
            )
            continue
        result = _run([resolved, "--version"])
        if result is not None and result.returncode != 0:
            # Don't parse usage text. A binary that exits nonzero while printing
            # a help screen containing any dotted pair would otherwise be
            # reported as a healthy version.
            results.append(
                CheckResult(
                    f"developer.forge_versions.{name}",
                    WARN,
                    f"{resolved} exited {result.returncode} on --version",
                    remedy=(
                        f"Confirm by hand that `{resolved} --version` works; this "
                        f"deployment is exercised against {name} {expected_text}."
                    ),
                    scope=IMAGE,
                )
            )
            continue
        banner = "" if result is None else (result.stdout or result.stderr or "").strip()
        observed = parse_version(banner)
        if observed is None:
            results.append(
                CheckResult(
                    f"developer.forge_versions.{name}",
                    WARN,
                    f"{resolved} reported a version we could not parse: {banner.splitlines()[0] if banner else '(no output)'}",
                    remedy=(
                        f"Confirm by hand that `{resolved} --version` looks sane; "
                        f"this deployment is exercised against {name} {expected_text}."
                    ),
                    scope=IMAGE,
                )
            )
            continue
        observed_text = ".".join(str(part) for part in observed)
        if observed[: len(expected)] < expected:
            results.append(
                CheckResult(
                    f"developer.forge_versions.{name}",
                    WARN,
                    f"{name} {observed_text}, exercised against {expected_text}",
                    remedy=(
                        f"Install a newer {name} if a verb misbehaves; this is not a known "
                        f"floor, only the version the deployment has been run against."
                    ),
                    scope=IMAGE,
                )
            )
            continue
        results.append(
            CheckResult(
                f"developer.forge_versions.{name}",
                OK,
                f"{name} {observed_text} (exercised against {expected_text})",
                scope=IMAGE,
            )
        )
    return results


# The sentinel `forge_cli.py` carries for exactly this purpose. Matching on a
# deliberate marker rather than on docstring text: the wrapper is a verbatim
# copy of that file, whose prose happens to contain "istota" today, and an
# identity test that depends on wording flips a correct install to a failure the
# next time someone rewrites a comment.
_WRAPPER_SENTINEL = b"ISTOTA_FORGE_WRAPPER"


def _looks_like_the_wrapper(path: str) -> bool | None:
    """Whether `path` is istota's forge wrapper rather than a real forge binary.

    ``None`` means "could not tell" — an unreadable file is not evidence of a
    shadowing real binary, and reporting it as one would fail a deployment over
    a permission bit.

    Read as bytes and bounded: a real ``gh`` is a ~40MB Go binary, and reading
    it whole to answer a yes/no would be its own defect.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(8192)
    except OSError:
        return None
    if head[:4] == b"\x7fELF":
        return False
    return _WRAPPER_SENTINEL in head


def check_forge_wrapper_shadowing(config: "Config", probe: bool) -> list[CheckResult]:
    """Nothing resolves ``gh`` / ``glab`` by name to an *unexpected* binary.

    The question is not "is a real forge binary on PATH" — that is true by
    design on the Ansible shape, which is what production runs: the role
    installs from the Debian archive into ``/usr/bin`` and renders those paths
    into ``config.toml``. Asserting the image's off-PATH layout everywhere
    reports a correct bare-metal host as broken, and since a ``FAIL`` alerts the
    admin allowlist, it would do so on every boot and every sweep.

    What is worth catching is a *disagreement*: something reachable as ``gh``
    that is not the binary this deployment resolved. That is the regression the
    off-PATH design exists to prevent — someone ``apt install``s gh into
    ``/usr/bin`` on the image shape, and the model's shell finds it before the
    per-task wrapper, skipping the deny policy and the per-call token injection
    that both live in the wrapper.

    So the four cases, and why each lands where it does:

    * nothing on PATH — the image shape, working as designed. ``OK``.
    * the wrapper — also fine; that is the thing meant to be found. ``OK``.
    * the same real binary resolution returned — the Ansible shape, working as
      designed. ``OK``, and the detail says which shape it is.
    * a *different* real binary — nobody intended this. ``FAIL``.
    """
    dev, reason = _dev_gate(config)
    if dev is None:
        return [
            CheckResult(f"developer.forge_wrapper_shadowing.{n}", SKIP, reason, scope=IMAGE)
            for n in _FORGE_BINARIES
        ]
    # The token gate applies here too, which the spec's gating sentence assigns
    # only to the binary / drift / version checks. Both of the things a shadowing
    # binary bypasses — the deny policy and the per-call token injection — exist
    # to govern a credential, so with no credential configured there is nothing
    # being bypassed. Without this gate a tokenless deployment on any host with a
    # real `gh` on PATH (every developer laptop) goes from silent today to
    # alerting, which is the exact regression the gating exists to prevent.
    token_reason = _forge_token_gate(dev)
    if token_reason:
        return [
            CheckResult(f"developer.forge_wrapper_shadowing.{n}", SKIP, token_reason, scope=IMAGE)
            for n in _FORGE_BINARIES
        ]

    results: list[CheckResult] = []
    for name in _FORGE_BINARIES:
        found = shutil.which(name)
        if found is None:
            results.append(
                CheckResult(
                    f"developer.forge_wrapper_shadowing.{name}",
                    OK,
                    f"nothing on the daemon's PATH resolves `{name}`",
                    scope=IMAGE,
                )
            )
            continue
        identified = _looks_like_the_wrapper(found)
        if identified:
            results.append(
                CheckResult(
                    f"developer.forge_wrapper_shadowing.{name}",
                    OK,
                    f"`{name}` resolves to the istota wrapper at {found}",
                    scope=IMAGE,
                )
            )
            continue
        if identified is None:
            results.append(
                CheckResult(
                    f"developer.forge_wrapper_shadowing.{name}",
                    WARN,
                    f"`{name}` resolves to {found}, which could not be read to identify it",
                    remedy=(
                        f"Check the permissions on {found}. Until it can be read, whether "
                        f"it shadows the wrapper is unknown rather than fine."
                    ),
                    scope=IMAGE,
                )
            )
            continue
        resolved = _resolved_forge_bin(dev, name)
        if os.path.realpath(found) == os.path.realpath(resolved):
            results.append(
                CheckResult(
                    f"developer.forge_wrapper_shadowing.{name}",
                    OK,
                    (
                        f"`{name}` resolves to {found}, which is the binary this "
                        f"deployment resolved (the Ansible shape installs on PATH)"
                    ),
                    scope=IMAGE,
                )
            )
            continue
        results.append(
            CheckResult(
                f"developer.forge_wrapper_shadowing.{name}",
                FAIL,
                (
                    f"`{name}` resolves on PATH to {found}, but this deployment resolved "
                    f"{resolved} — neither the wrapper nor the intended binary"
                ),
                remedy=(
                    f"Remove the unexpected {name} from PATH. Whatever is found first "
                    f"bypasses the deny policy and the per-call token injection, which "
                    f"both live in the wrapper."
                ),
                scope=IMAGE,
            )
        )
    return results


def check_forge_policy(config: "Config", probe: bool) -> CheckResult:
    """A ``forge_cli_permit`` entry that matches no rule.

    Lifted from ``_validate_forge_clis``. A hatch that silently stopped matching
    after a baseline rewording looks exactly like one that is still open, and
    otherwise surfaces as nothing at all — which is the problem.
    """
    dev, reason = _dev_gate(config)
    if dev is None:
        return CheckResult("developer.forge_policy", SKIP, reason)
    try:
        from .forge_cli import FORGE_GITHUB, FORGE_GITLAB, unmatched_permits

        dead = unmatched_permits(
            [FORGE_GITHUB, FORGE_GITLAB],
            list(dev.forge_cli_permit),
            list(dev.forge_cli_extra_denied),
        )
    except Exception as exc:  # noqa: BLE001 - never fail over a warning
        return CheckResult(
            "developer.forge_policy",
            WARN,
            f"forge_cli_permit validation could not run: {exc}",
            remedy="Check the [developer] forge_cli_permit / forge_cli_extra_denied syntax.",
        )
    if not dead:
        return CheckResult(
            "developer.forge_policy",
            OK,
            f"{len(dev.forge_cli_permit)} forge_cli_permit entrie(s), all matching a rule",
        )
    return CheckResult(
        "developer.forge_policy",
        WARN,
        f"forge_cli_permit entries matching no rule: {', '.join(repr(e) for e in dead)}",
        remedy=(
            "Check the spelling against the baseline policy before assuming the verb is "
            "permitted — the entry is turning nothing off."
        ),
    )


# ---------------------------------------------------------------------------
# web.*
# ---------------------------------------------------------------------------


def check_web_static(config: "Config", probe: bool) -> CheckResult:
    """The SvelteKit build the web surface serves actually exists.

    A web-builder stage that silently produced nothing gives a running server
    that 404s its own app shell — which reads as a routing bug for as long as it
    takes someone to look in the image.
    """
    if not getattr(config.web, "enabled", False) and not getattr(config, "site", None):
        return CheckResult("web.static", SKIP, "no web surface configured", scope=IMAGE)
    if not getattr(config.web, "enabled", False):
        return CheckResult("web.static", SKIP, "[web] enabled = false", scope=IMAGE)
    # The leaf, not `web_app`: importing that module pulls in FastAPI, authlib,
    # starlette and httpx (+56 MB RSS, permanently, in the scheduler process)
    # and runs a second full `load_config()` at import time. A diagnostic does
    # not get to cost that.
    from .static_dir import resolve_static_dir

    index = Path(resolve_static_dir()) / "index.html"
    if not index.is_file():
        return CheckResult(
            "web.static",
            FAIL,
            f"{index} does not exist",
            remedy="Build the frontend (`npm --prefix web run build`); the image's web-builder stage does this.",
            scope=IMAGE,
        )
    if index.stat().st_size == 0:
        return CheckResult(
            "web.static",
            FAIL,
            f"{index} is empty",
            remedy="Rebuild the frontend; the build produced a zero-byte app shell.",
            scope=IMAGE,
        )
    return CheckResult("web.static", OK, f"{index} is present ({index.stat().st_size} bytes)", scope=IMAGE)


# ---------------------------------------------------------------------------
# sandbox.* (deep)
# ---------------------------------------------------------------------------


def check_sandbox_masks(config: "Config", probe: bool) -> CheckResult:
    """Spawn a real bubblewrap namespace and confirm the DB masks hold.

    The one check that costs a subprocess with a namespace in it, so it runs
    only under ``deep=True``. What it asserts is what argv assertions
    structurally cannot: that the database directories are empty and unwritable
    *inside* the namespace, rather than that the right flags were passed.
    """
    if not probe:
        # The contract is unconditional: probe=False forbids spawning. Checked
        # before `_bwrap_usable()`, which spawns a probe of its own.
        return CheckResult(
            "sandbox.masks",
            SKIP,
            "a namespace cannot be entered without spawning one (probe disabled)",
        )
    if not getattr(config.security, "sandbox_enabled", False):
        # `_bwrap_usable()` answers "could a namespace be created", which is not
        # the same question. On a Linux host with bwrap installed and the
        # sandbox switched off, probing anyway would report a boundary healthy
        # that the executor never applies — the most misleading answer available.
        return CheckResult(
            "sandbox.masks",
            SKIP,
            "sandbox is disabled ([security] sandbox_enabled); the executor applies no masks",
        )
    if not _bwrap_usable():
        return CheckResult(
            "sandbox.masks", SKIP, "bubblewrap cannot create a namespace here"
        )
    try:
        import tempfile

        from . import db
        from .executor import build_bwrap_cmd

        # Both directories `build_bwrap_cmd` masks, not just the framework one —
        # the message says "directories" and `module_db_root()` is the one that
        # went unmasked in the first place.
        db_dirs = [Path(config.db_path).parent]
        try:
            db_dirs.append(config.module_db_root())
        except Exception:  # noqa: BLE001 - a misconfigured module_data_dir raises
            pass

        task = db.Task(
            id=0,
            status="running",
            source_type="doctor",
            user_id="doctor",
            prompt="",
        )
        # Two questions per directory: is it empty, and does a write into it
        # fail? A mask that is present but writable is the failure mode that
        # reads as corruption rather than as a boundary.
        probe_script = "; ".join(
            f'ls -A "{d}" 2>/dev/null | head -n 1; '
            f'touch "{d}/.doctor-probe" 2>/dev/null && echo WRITABLE'
            for d in db_dirs
        )
    except Exception as exc:  # noqa: BLE001 - a deep probe must not take the caller down
        return CheckResult(
            "sandbox.masks",
            FAIL,
            f"the sandbox probe could not be built: {exc}",
            remedy="Check [security] sandbox settings; build_bwrap_cmd refused to build a command.",
        )

    # A TemporaryDirectory, not a fixed path under temp_dir: the previous shape
    # created `{temp_dir}/doctor` on every deep run and never removed it.
    try:
        with tempfile.TemporaryDirectory(prefix="istota-doctor-") as user_temp:
            cmd = build_bwrap_cmd(
                ["/bin/sh", "-c", probe_script],
                config,
                task,
                is_admin=False,
                user_resources=[],
                user_temp_dir=Path(user_temp),
            )
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=DEEP_TIMEOUT, check=False
            )
    except subprocess.TimeoutExpired:
        return CheckResult(
            "sandbox.masks",
            FAIL,
            f"the sandbox probe timed out after {DEEP_TIMEOUT}s",
            remedy="Investigate by hand; a bubblewrap spawn that never returns is not a mask problem.",
        )
    except Exception as exc:  # noqa: BLE001 - a deep probe must not take the caller down
        return CheckResult(
            "sandbox.masks",
            FAIL,
            f"the sandbox probe could not be run: {exc}",
            remedy="Confirm bubblewrap works (`bwrap --version`).",
        )

    output = (result.stdout or "").strip()
    if "WRITABLE" in output:
        return CheckResult(
            "sandbox.masks",
            FAIL,
            "a database directory is writable inside the sandbox",
            remedy=(
                "build_bwrap_cmd must mask the DB directories last, read-only. A writable "
                "mask lets a sqlite3 probe create a zero-byte file that reads as corruption."
            ),
        )
    visible = [line for line in output.splitlines() if line and line != "WRITABLE"]
    if visible:
        return CheckResult(
            "sandbox.masks",
            FAIL,
            f"a database directory is not empty inside the sandbox ({len(visible)} entr(ies) visible)",
            remedy=(
                "Narrow [security] sandbox_ro_paths; an earlier bind is showing through "
                "the mask, or the mask is no longer the last mount operation."
            ),
        )
    return CheckResult(
        "sandbox.masks", OK, "the database directories are empty and unwritable inside the sandbox"
    )


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

# The name is part of the registry rather than only of the result, so `only=`
# can select *before* invoking. Filtering afterwards would mean running every
# check to discard most of them — which is exactly what the config-load path
# cannot afford.
CHECKS: tuple[tuple[str, Check], ...] = (
    ("runtime.platform", check_platform),
    ("runtime.bwrap", check_bwrap),
    ("runtime.model_cli", check_model_cli),
    ("runtime.tmux", check_tmux),
    ("runtime.framework_db", check_framework_db),
    ("runtime.writable_dirs", check_writable_dirs),
    ("runtime.mount_liveness", check_mount_liveness),
    ("security.skill_proxy", check_skill_proxy),
    ("developer.forge_binaries", check_forge_binaries),
    ("developer.forge_config_drift", check_forge_config_drift),
    ("developer.forge_versions", check_forge_versions),
    ("developer.forge_wrapper_shadowing", check_forge_wrapper_shadowing),
    ("developer.forge_policy", check_forge_policy),
    ("web.static", check_web_static),
    ("sandbox.masks", check_sandbox_masks),
)

# Checks that spawn a namespace and are therefore opt-in. Kept as a set beside
# the registry rather than as a flag on the tuple: the registry is a mapping of
# name to function and stays readable as one.
DEEP_CHECKS = frozenset({"sandbox.masks"})

# Each check's scope, so `scope=` can select *before* invoking. Filtering the
# results afterwards would mean `--scope image` in a volume-less `docker run`
# still opened the framework DB and stat'd a mount that isn't there — paying for
# the deployment-scoped checks in order to throw them away, in the one tier that
# exists because it is cheap. A check's results all carry its registry scope,
# and a unit test enforces that.
CHECK_SCOPES: dict[str, str] = {
    "runtime.platform": IMAGE,
    "runtime.bwrap": IMAGE,
    "runtime.model_cli": IMAGE,
    "runtime.tmux": IMAGE,
    "runtime.framework_db": DEPLOYMENT,
    "runtime.writable_dirs": DEPLOYMENT,
    "runtime.mount_liveness": DEPLOYMENT,
    "security.skill_proxy": DEPLOYMENT,
    "developer.forge_binaries": IMAGE,
    "developer.forge_config_drift": DEPLOYMENT,
    "developer.forge_versions": IMAGE,
    "developer.forge_wrapper_shadowing": IMAGE,
    "developer.forge_policy": DEPLOYMENT,
    "web.static": IMAGE,
    "sandbox.masks": DEPLOYMENT,
}


def run_checks(
    config: "Config",
    *,
    only: tuple[str, ...] = (),
    skip: tuple[str, ...] = (),
    scope: str = "",
    deep: bool = False,
    probe: bool = True,
) -> list[CheckResult]:
    """Run the registry, in order, and return every result.

    ``only`` selects by registry-name prefix (all checks when empty); ``skip``
    excludes by the same kind of prefix and wins over ``only``, for a caller
    that wants nearly everything. ``scope`` narrows to ``IMAGE`` or
    ``DEPLOYMENT``. ``deep`` opts into the checks that spawn a namespace.
    ``probe=False`` forbids spawning anything: a check that would exec something
    answers from the filesystem alone and says so in its ``detail``.

    Every selector filters *before* invoking, so a narrowed run does not pay for
    the checks it discards.

    A check that raises is reported as ``FAIL`` with the exception text. Doctor
    never raises, because it runs on the daemon's start-up path — an exception
    there would turn a diagnostic into an outage.
    """
    results: list[CheckResult] = []
    for name, func in CHECKS:
        if name in DEEP_CHECKS and not deep:
            continue
        if only and not any(name.startswith(prefix) for prefix in only):
            continue
        if skip and any(name.startswith(prefix) for prefix in skip):
            continue
        if scope and CHECK_SCOPES.get(name) != scope:
            continue
        try:
            produced = func(config, probe)
        except Exception as exc:  # noqa: BLE001 - deliberate: see the docstring
            logger.debug("doctor check %s raised", name, exc_info=True)
            results.append(
                CheckResult(
                    name,
                    FAIL,
                    f"the check itself raised {type(exc).__name__}: {exc}",
                    remedy="This is a defect in the check, not necessarily in the deployment.",
                    scope=CHECK_SCOPES.get(name, DEPLOYMENT),
                )
            )
            continue
        produced_list = [produced] if isinstance(produced, CheckResult) else list(produced)
        results.extend(produced_list)
    return results


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def config_secrets(config: "Config") -> list[str]:
    """Every configured credential value, for the renderers' redaction pass.

    Reuses ``admin_config_view``'s field-level classification so there is one
    answer to "is this field a credential", rather than a second list here that
    drifts from the one the config page uses.
    """
    from .admin_config_view import is_secret_field

    found: list[str] = []

    def _consider(value, key: str, field_name: str, depth: int) -> None:
        """Classify one value, recursing through the containers config uses.

        Dicts and lists are traversed, not skipped: ``config.users`` is a
        ``dict[str, UserConfig]`` and every per-user credential lives under it,
        so a walk that only followed dataclass attributes would leave the
        largest group of secrets out of the redaction pass while claiming to
        reuse ``admin_config_view``'s classification.
        """
        if depth > 6:
            return
        if hasattr(value, "__dataclass_fields__"):
            _walk(value, f"{key}.", depth + 1)
            return
        if isinstance(value, dict):
            # A dict the classifier flags wholesale — `brain.native.extra_headers`
            # is the live case — has credential *contents* whatever its keys are
            # called. Harvest every string in it rather than re-asking about each
            # header name, or a spelling nobody anticipated is the one that
            # escapes redaction.
            if is_secret_field(key, field_name):
                for sub_value in value.values():
                    if isinstance(sub_value, str) and len(sub_value) >= _MIN_SECRET_LEN:
                        found.append(sub_value)
                return
            for sub_key, sub_value in value.items():
                # Otherwise a dict key is a name (a user id, a header name), so
                # classification travels with it as a field name would.
                _consider(sub_value, f"{key}.{sub_key}", str(sub_key), depth + 1)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                _consider(item, key, field_name, depth + 1)
            return
        if isinstance(value, str) and len(value) >= _MIN_SECRET_LEN:
            if is_secret_field(key, field_name):
                found.append(value)

    def _walk(obj, prefix: str, depth: int) -> None:
        if depth > 6:
            return
        for field_name in getattr(obj, "__dataclass_fields__", {}):
            value = getattr(obj, field_name, None)
            _consider(value, f"{prefix}{field_name}", field_name, depth)

    _walk(config, "", 0)
    return found


def _redact(text: str, secrets: Iterable[str] | None) -> str:
    """Replace every configured credential value in `text`.

    Check authors are forbidden from putting a credential in ``detail`` or
    ``remedy``. This does not take their word for it: ``detail`` carries
    observed paths and raw exception text, and both cross an HTTP boundary to
    the admin dashboard.
    """
    if not secrets:
        return text
    for secret in secrets:
        if secret and len(secret) >= _MIN_SECRET_LEN and secret in text:
            text = text.replace(secret, _REDACTED)
    return text


def _redacted_results(
    results: list[CheckResult], secrets: Iterable[str] | None
) -> list[CheckResult]:
    if not secrets:
        return results
    secret_list = [s for s in secrets if s and len(s) >= _MIN_SECRET_LEN]
    if not secret_list:
        return results
    return [
        replace(
            r,
            detail=_redact(r.detail, secret_list),
            remedy=_redact(r.remedy, secret_list),
        )
        for r in results
    ]


def redact(results: list[CheckResult], config: "Config") -> list[CheckResult]:
    """Results with every configured credential value replaced.

    For the consumers that are not the renderers — the start-up log lines and
    the operator alert. Those cross boundaries too (a log file, a Talk room),
    and several checks interpolate raw exception text into ``detail``.
    """
    return _redacted_results(results, config_secrets(config))


def render_json(results: list[CheckResult], *, secrets: Iterable[str]) -> str:
    """A stable array of objects, for the image tests and the admin endpoint.

    Always valid JSON, including when checks failed — a machine consumer that
    has to distinguish "the run found problems" from "the run produced garbage"
    has already lost.

    ``secrets`` is required rather than defaulting to none, because this output
    crosses an HTTP boundary to the admin dashboard and a caller that simply
    forgot the argument would be fail-open. Pass ``config_secrets(config)``, or
    ``()`` to say deliberately that there is nothing to redact.
    """
    return json.dumps(check_payload(_redacted_results(results, secrets)), indent=2)


def check_payload(results: list[CheckResult]) -> list[dict]:
    """The wire shape of a result list — the one definition of it.

    Both the CLI's ``--json`` and the admin endpoint go through here, so a key
    added for the image test tier cannot reach one and miss the other. That
    divergence is invisible to tests that assert each surface against its own
    hardcoded dict, which is what they were doing.

    Does **not** redact: callers pass results that already have been. Redaction
    is not optional and so does not belong in a shape function, where an
    argument could be forgotten.
    """
    return [
        {
            "name": r.name,
            "status": r.status,
            "detail": r.detail,
            "remedy": r.remedy,
            "scope": r.scope,
        }
        for r in results
    ]


_STATUS_ORDER = {FAIL: 0, WARN: 1, OK: 2, SKIP: 3}


def render_text(results: list[CheckResult], *, secrets: Iterable[str]) -> str:
    """One line per check, grouped by prefix, with remedies indented beneath.

    Grouping is by the first dotted segment, in the order the registry produced
    them, so the output reads the same way twice running.

    ``secrets`` is required for the same reason as in :func:`render_json`:
    terminal output is where a pasted credential ends up in a bug report.
    """
    lines: list[str] = []
    current_group = ""
    for r in _redacted_results(results, secrets):
        group = r.name.split(".", 1)[0]
        if group != current_group:
            if lines:
                lines.append("")
            lines.append(f"{group}:")
            current_group = group
        lines.append(f"  {r.status.upper():<5} {r.name}  {r.detail}")
        if r.status in (WARN, FAIL) and r.remedy:
            lines.append(f"        -> {r.remedy}")
    return "\n".join(lines)


def exit_code(results: list[CheckResult]) -> int:
    """1 if any check failed, else 0. Warnings are not failures."""
    return 1 if any(r.status == FAIL for r in results) else 0


def summarize(results: list[CheckResult]) -> dict[str, int]:
    """Count by status — for a log line, and for the interval check's transition."""
    counts = {OK: 0, WARN: 0, FAIL: 0, SKIP: 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return counts


def failing(results: list[CheckResult]) -> list[CheckResult]:
    """Just the failures, sorted by name — what an alert names.

    By name rather than by severity: everything here is already ``FAIL``, so
    there is no severity left to order by, and a stable alphabetical order makes
    two alerts about the same set of problems read identically.
    """
    return sorted((r for r in results if r.status == FAIL), key=lambda r: r.name)
