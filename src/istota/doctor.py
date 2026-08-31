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

import ipaddress
import json
import logging
import math
import os
import platform
import re
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable, Collection, Iterable
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:  # pragma: no cover - typing only; a runtime import is a cycle
    from .config import Config
    from .subscription_usage import UsageSnapshot, UsageWindow

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


def _looks_like_a_user_id(value: str) -> bool:
    """A GitLab username is never all ASCII digits, so this can only be an id.

    ``str.isdigit`` is Unicode-wide — Arabic-Indic digits and a superscript two
    both answer True — and none of those is a user id either, so an ASCII test
    keeps the WARN's wording ("which is a user id") true of what it matched.
    """
    return value.isascii() and value.isdigit()


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


# The two remedies this check can offer. Both are fixed literals: `detail` and
# `remedy` are built from these plus a percentage, a duration, a resolver branch
# name and the configured fallback brain kind — never from the credential, the
# raw response body or an exception string. The fallback kind is safe to
# interpolate because `_validate_brain_fallback` blanks anything outside
# `KNOWN_BRAIN_KINDS` at load, and it is a setting rather than a secret.
#
# There used to be three. The other two answered a failure to obtain a reading —
# check your egress, re-run `claude setup-token`, the response shape changed —
# and both went with the WARNs they accompanied. A remedy belongs on a row an
# operator can act on, and "the endpoint will not serve this credential class"
# is not one: those rows are SKIPs now, carrying the reason and no instruction.
_USAGE_BUSY_REMEDY = (
    "Tasks will fail over to the {fallback} brain when this window is exhausted."
)

# The same row on a deployment with no `[brain] fallback` configured. The literal
# above asserted a failover that most deployments do not have: `claude_code` has
# never had an implicit fallback, and since ISSUE-362 neither has any other kind,
# so an exhausted window fails the task outright. Naming a repair the operator
# can act on beats promising a reroute that will not happen.
_USAGE_BUSY_NO_FALLBACK_REMEDY = (
    "No [brain] fallback is configured, so tasks fail when this window is "
    "exhausted. Set one to reroute them."
)


def check_subscription_usage(config: "Config", probe: bool) -> CheckResult:
    """Plan utilization for the Claude Code subscription, with its reset times.

    On a subscription deployment the dashboard's cost column is deliberately
    blank — a plan-equivalent list price is not spend — so these windows are the
    only budget there is, and the deployment currently learns it is out of plan
    headroom at the moment a task fails over.

    **This check never returns FAIL, at any utilization.** ``exit_code`` returns
    1 on any FAIL and ``scheduler._alert_doctor_failures`` messages every admin
    on the transition into failure. A plan at 97% is a fact about the plan, not a
    defect in the host: it would exit non-zero on a busy but perfectly healthy
    deployment, turn the Health pane red for a condition no operator action
    resolves, and mail everyone about it. Proactive alerting on utilization is a
    reasonable thing to want and belongs in a poller with its own per-window
    threshold state, not smuggled through doctor's failure channel.

    ``subscription_usage`` is imported inside the function, not at module scope:
    ``_validate_forge_clis`` imports this module from inside every
    ``load_config``, which runs in every CLI invocation and every host-side skill
    CLI the proxy spawns per call.
    """
    name = "runtime.subscription_usage"
    settings = getattr(getattr(config, "brain", None), "claude_code", None)

    if not getattr(settings, "subscription_usage", True):
        return CheckResult(name, SKIP, "subscription usage polling is disabled")
    if not probe:
        # Before the import, and before anything is asked of the module: the
        # reading is a network request and there is no cheap filesystem answer
        # that would still be true.
        return CheckResult(
            name,
            SKIP,
            "utilization cannot be observed without a network request (probe disabled)",
        )

    from . import subscription_usage

    # One clock for the fetch, the countdowns and the staleness age. Reading the
    # wall clock twice would let a cached snapshot's age be measured against a
    # different moment than the one the module used to decide it was fresh.
    now = time.time()
    snapshot = subscription_usage.get_snapshot(config, now_ts=now)

    # Two of the module's errors are conditions rather than faults, and a WARN
    # about network egress would be nonsense for either. `DISABLED_ERROR` is
    # unreachable through the gate above and handled anyway: the module reads
    # the same setting defensively and its own answer is the authoritative one.
    if snapshot.error in (
        subscription_usage.NO_CREDENTIAL_ERROR,
        subscription_usage.DISABLED_ERROR,
    ):
        return CheckResult(name, SKIP, snapshot.error)

    if snapshot.error and not snapshot.has_data:
        # SKIP, not WARN. This used to warn, on the reading that a reading the
        # operator expected and did not get is a problem worth surfacing. On the
        # deployment shapes that actually run, it is not: the endpoint does not
        # serve the long-lived setup-token credential Ansible and Docker deploy,
        # answering it with a persistent 429, so the WARN was permanent, matched
        # no operator action, and coloured the Health pane for a host with
        # nothing wrong with it. A reading that cannot be obtained is a check
        # that does not apply here, which is what SKIP means. The reason is
        # still carried, so anyone asking why the card is absent can read it.
        return CheckResult(name, SKIP, _usage_error(snapshot))

    if not snapshot.windows:
        # Unreachable: every error-free return from `get_snapshot` carries
        # windows, and the one that does not sets NO_WINDOWS_ERROR. Guarded
        # anyway, because the alternative is an IndexError below, and
        # `run_checks` turns a raising check into exactly the FAIL this check
        # exists never to produce. Two lines make the promise structural rather
        # than inherited from another module's invariant.
        return CheckResult(name, SKIP, subscription_usage.NO_WINDOWS_ERROR)

    # A snapshot with both windows and an error is the stale-cache branch: real
    # numbers from an older fetch, plus the failure that made them old.
    stale_note = ""
    if snapshot.error:
        age = snapshot.age_seconds(now)
        stale_note = (
            f"last successful reading is {_duration(age)} old: {_usage_error(snapshot)}"
        )
        stale_after = _setting_float(
            settings, "subscription_usage_stale_after_seconds", 3600.0
        )
        if age > stale_after:
            # Same reasoning as the no-data branch above: a reading this old
            # means the fetches are failing, which on a server shape is the
            # steady state rather than a fault. The numbers are too old to
            # report as current, so there is nothing to check.
            return CheckResult(name, SKIP, stale_note)

    # Worst first, and all of them: "5-hour at 12%, weekly at 94%" and "5-hour at
    # 94%, weekly at 12%" call for different operator responses, and this one
    # line is the whole of what a terminal reader sees.
    windows = sorted(snapshot.windows, key=lambda w: w.percent, reverse=True)
    detail = "; ".join(_usage_window(w) for w in windows)
    if stale_note:
        # Inside `stale_after` the status is still what the numbers say — that
        # threshold is the whole point of the setting — but the line has to
        # admit the numbers are old and say why. Otherwise an hour-long outage
        # reads as `OK` with an hour-old percentage beside a countdown that has
        # been recomputed against the current clock, which is the most
        # misleading pair this check could print. The admin card and `!usage`
        # both carry the same footer for the same reason.
        detail = f"{detail}; {stale_note}"

    warn_at = _setting_float(settings, "subscription_usage_warn_percent", 80.0)
    high_at = _setting_float(settings, "subscription_usage_high_percent", 95.0)
    # The status table's two busy rows differ only in which threshold caught the
    # reading; both are WARN with the same detail and the same remedy, because
    # `high` is what turns the dashboard tile red and doctor has no third colour.
    # `min` reproduces both rows including an inverted pair, which the loader
    # corrects but which a config reaching the dataclass some other way would not.
    if windows[0].percent >= min(warn_at, high_at):
        from .brain._fallback import effective_fallback_kind

        fallback_kind = effective_fallback_kind(config.brain)
        remedy = (
            _USAGE_BUSY_REMEDY.format(fallback=fallback_kind)
            if fallback_kind is not None
            else _USAGE_BUSY_NO_FALLBACK_REMEDY
        )
        return CheckResult(name, WARN, detail, remedy=remedy)
    return CheckResult(name, OK, detail)


def _usage_error(snapshot: "UsageSnapshot") -> str:
    """The module's error, plus which credential produced it.

    Which one it was is the whole diagnostic: a setup token in the environment
    and an interactive login in the keychain are refused for different reasons
    and have different repairs. The branch *name* only — the snapshot has never
    carried the credential itself.
    """
    if not snapshot.token_source:
        return snapshot.error
    return f"{snapshot.error} (credential source: {snapshot.token_source})"


def _usage_window(window: "UsageWindow") -> str:
    text = f"{window.label} at {window.percent:g}%"
    if window.resets_in_seconds is None:
        # No reset scheduled, or an unparseable one. A terminal line is better
        # short than padded with a clause that says nothing.
        return text
    if window.resets_in_seconds <= 0:
        return f"{text} (resetting now)"
    return f"{text} (resets in {_duration(window.resets_in_seconds)})"


def _duration(seconds: float) -> str:
    """A coarse human duration: ``6d 2h``, ``1h 04m``, ``12m``, ``45s``.

    Two units at most. An operator reading "resets in 1h 04m" is deciding
    whether to wait; seconds of precision six hours out is noise.
    """
    total = int(max(0.0, seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def _setting_float(settings: object, field: str, default: float) -> float:
    """A numeric setting, or `default` for anything that is not a real number.

    The loader validates and corrects these fields; this is the second line, so
    that a value arriving past the loader cannot make the comparison below raise
    or silently never fire. ``bool`` is excluded explicitly — it is an ``int``,
    and ``True`` would read as a 1% threshold.
    """
    value = getattr(settings, field, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    as_float = float(value)
    return as_float if math.isfinite(as_float) else default


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
    installs the vendors' binaries into ``/usr/local/bin`` and renders those
    paths into ``config.toml``. Asserting the image's off-PATH layout everywhere
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
    # only to the binary and drift checks. Both of the things a shadowing
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


def check_gitlab_reviewer(config: "Config", probe: bool) -> CheckResult:
    """A GitLab MR reviewer that `glab` will not resolve.

    ISSUE-289. The setting is silent in both directions. A value `glab` cannot
    resolve fails inside the task, where only the model sees it; an unset one
    produces no message at all. Either way the MR opens with nobody assigned,
    which is the step that puts a person in the loop, and the deployment ran
    that way for weeks. WARN rather than FAIL: an MR with no reviewer is still
    an MR, and the operator may simply not want one.

    Everything is read through ``str()``. TOML types its scalars, so an
    unquoted ``gitlab_reviewer = 1234567`` — the natural hand-edit for a field
    whose example value is a number in quotes — arrives as an ``int``, and a
    check that called a string method on it would raise. ``run_checks`` turns a
    raising check into a FAIL, which is the one status that alerts, so the
    crash would page the operator in exactly the misconfiguration this exists
    to describe.
    """
    dev, reason = _dev_gate(config)
    if dev is None:
        return CheckResult("developer.gitlab_reviewer", SKIP, reason)

    reviewer = str(dev.gitlab_reviewer or "")
    named = reviewer.strip()
    if named:
        if _looks_like_a_user_id(named):
            return CheckResult(
                "developer.gitlab_reviewer",
                WARN,
                f"developer.gitlab_reviewer is {named!r}, which is a user id, not a username",
                remedy=(
                    "`glab mr create --reviewer` resolves by username, and a GitLab "
                    "username is never all digits. Set the reviewer's username; "
                    "`glab api users/<id>` reports it."
                ),
            )
        if any(char.isspace() for char in reviewer):
            # The recipe expands `--reviewer $GITLAB_REVIEWER` unquoted, so an
            # internal space hands `glab` a stray positional and a surrounding
            # one is eaten by word-splitting. Neither is a username.
            return CheckResult(
                "developer.gitlab_reviewer",
                WARN,
                f"developer.gitlab_reviewer is {reviewer!r}, which contains whitespace",
                remedy=(
                    "A GitLab username has no spaces in it. This is most likely the "
                    "reviewer's display name; set their username instead."
                ),
            )
        return CheckResult(
            "developer.gitlab_reviewer",
            OK,
            f"MR reviewer {named!r}",
        )

    recorded = str(dev.gitlab_reviewer_id or "").strip()
    if recorded:
        # Which message is right depends on what the operator was told. The
        # field was documented as a username for one day before ISSUE-289 was
        # filed (56d21548), and as a numeric id for everything before that, so
        # both shapes are deployed and the remedy differs.
        if _looks_like_a_user_id(recorded):
            remedy = (
                "The id is recorded and read by nothing. Add "
                "developer.gitlab_reviewer with the same person's username; "
                "`glab api users/<id>` reports it."
            )
        else:
            remedy = (
                f"{recorded!r} is already a username — copy it verbatim into "
                "developer.gitlab_reviewer, which is the key that is read now."
            )
        return CheckResult(
            "developer.gitlab_reviewer",
            WARN,
            "developer.gitlab_reviewer_id is set but developer.gitlab_reviewer is not, "
            "so new merge requests get no reviewer",
            remedy=remedy,
        )
    return CheckResult(
        "developer.gitlab_reviewer",
        OK,
        "no MR reviewer configured",
    )


def check_forge_transport(config: "Config", probe: bool) -> CheckResult:
    """A forge token that travels over plain HTTP.

    WARN, never FAIL: the operator wrote the `http://` themselves, the
    deployment works, and refusing to run over it is not doctor's call. What it
    is is a credential leaving the host in cleartext, which nothing else in the
    report says.

    This is newly reachable rather than newly true. A plain-HTTP `gitlab_url`
    used to die at the TLS handshake — glab forces https and discards the
    scheme in `GITLAB_HOST` — so no token ever left, and the deployment was
    broken rather than insecure. The developer skill now seeds glab's own
    `api_protocol` for that case (`_plain_http_host_entry`), which makes the
    call work and the plaintext transport real.

    Both forges are checked, and for gh the plaintext is the whole of what is
    wrong: gh refuses a scheme inside `GH_HOST` outright, so a plain-HTTP
    `github_url` cannot connect however it is spelled. The port half of that
    used to be broken too and no longer is — `forge_cli._gh_host` keeps a
    non-default port (ISSUE-279), so a forge on `:8443` is reachable and only
    its scheme is this check's business.

    The detail names the URL and never the token. A URL can carry userinfo, so
    it is redacted rather than printed raw.
    """
    dev, reason = _dev_gate(config)
    if dev is None:
        return CheckResult("developer.forge_transport", SKIP, reason, scope=DEPLOYMENT)
    token_reason = _forge_token_gate(dev)
    if token_reason:
        return CheckResult(
            "developer.forge_transport", SKIP, token_reason, scope=DEPLOYMENT
        )

    plaintext, embedded = [], []
    for label, url, token in (
        ("gitlab_url", dev.gitlab_url, dev.gitlab_token),
        ("github_url", dev.github_url, dev.github_token),
    ):
        # Only where a token would actually be sent. A configured-but-tokenless
        # forge sends no credential, so its scheme is not this check's business.
        if not token or not url:
            continue
        try:
            parts = urlsplit(url)
        except ValueError:
            # `http://[::1` raises Invalid IPv6 URL. Unguarded, `run_checks`
            # turns that into a FAIL whose remedy says "this is a defect in the
            # check" — a WARN-only check emitting a FAIL, and blaming itself
            # for the operator's typo.
            embedded.append(f"{label} is not a parseable URL")
            continue
        if "@" in (parts.netloc or ""):
            # Any scheme. A credential in the URL is a disclosure on https too,
            # and since `_plain_http_host_entry` refuses to write glab's
            # protocol entry for such a URL — the entry would have to carry the
            # password, into a file the sandbox can read — a plain-HTTP one
            # also silently fails to connect. This is the only thing that says
            # why.
            embedded.append(f"{label} = {_redact_userinfo(url)}")
        elif parts.scheme == "http":
            plaintext.append(f"{label} = {_redact_userinfo(url)}")

    if not plaintext and not embedded:
        return CheckResult(
            "developer.forge_transport",
            OK,
            "every configured forge with a token is reached over https",
            scope=DEPLOYMENT,
        )

    details, remedies = [], []
    if embedded:
        details.append(f"a forge URL carries a credential: {', '.join(embedded)}")
        remedies.append(
            "Move the credential to [developer] gitlab_token / github_token and "
            "rotate it — a URL reaches logs, remotes and process arguments. A "
            "plain-HTTP forge configured this way also cannot connect at all, "
            "because the protocol entry that would fix it is not written for a "
            "URL that would put the password in a sandbox-readable file."
        )
    if plaintext:
        details.append(f"a forge token is sent over plain HTTP: {', '.join(plaintext)}")
        remedies.append(
            "Point the URL at https, or accept that the token — and everything "
            "the CLI sends with it — crosses the network in the clear. A "
            "loopback URL is usually a tunnel, and what is on its far side is "
            "not visible from here."
        )
    return CheckResult(
        "developer.forge_transport",
        WARN,
        "; ".join(details),
        remedy=" ".join(remedies),
        scope=DEPLOYMENT,
    )


def _redact_userinfo(url: str) -> str:
    """A URL safe to print: userinfo replaced, everything else intact.

    A forge URL is operator-written config and is not supposed to carry
    credentials, but `https://user:token@host` parses fine and this string goes
    straight into a report that reaches the admin dashboard and the log.

    **Replaced, not removed.** Deleting the userinfo renders
    `https://bot:token@host` as `https://host`, and an operator reading that
    cannot tell the configured value carried a credential at all — which is the
    most useful thing the report could have told them, and the thing they need
    in order to know something wants rotating.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "(unparseable)"
    if "@" not in (parts.netloc or ""):
        return url
    _, _, hostport = parts.netloc.rpartition("@")
    return urlunsplit(
        (parts.scheme, f"{_REDACTED}@{hostport}", parts.path, parts.query, parts.fragment)
    )


# ---------------------------------------------------------------------------
# web.*
# ---------------------------------------------------------------------------

def check_basemap(config: "Config", probe: bool) -> CheckResult:
    """Whether the map surfaces have a background that will actually render.

    **This check deliberately opens no socket, and that is the finding rather
    than a shortcut.** Two facts, both established by measurement, remove the
    value a fetch would have had:

    The watermark is not observable from the response. Measured against the
    live service on 2026-08-28, CARTO returns 200, ``content-type: image/png``
    and a byte-identical body and ETag for a keyless request, a request with a
    bogus key and (by construction) a good one. There is no status, header or
    length to key on, so a probe there reports a working basemap for a defaced
    one — worse than not probing, because it manufactures confidence.

    And the daemon is the wrong host. Tiles are fetched by the *browser*, over
    a different route. A deployment whose egress is a proxy would fail a probe
    for a basemap every browser on the network renders correctly, and one with
    open egress would pass for a browser network that blocks the CDN. Running
    it anyway also put a third-party request on the daemon's boot path and on
    the hourly doctor sweep — up to two per run, at ``PROBE_TIMEOUT`` each,
    where a CDN blip becomes a FAIL and pages the operator about a deployment
    with nothing wrong with it.

    What is left is the half that *is* decidable from here, and it happens to
    be the half that matters: a keyed provider with no key is exactly the
    reported bug (ISSUE-334), and configuration says so for free and with
    certainty. ``probe`` is accepted to satisfy the ``Check`` protocol and is
    unused.
    """
    from .map_basemap import resolve_basemap

    web = getattr(config, "web", None)
    if not web or not web.enabled:
        return CheckResult(
            "web.basemap", SKIP, "web interface disabled", scope=DEPLOYMENT
        )

    m = getattr(web, "map", None)
    if m is None:
        return CheckResult(
            "web.basemap", SKIP, "no [web.map] configuration", scope=DEPLOYMENT
        )

    spec = resolve_basemap(
        provider=m.provider,
        api_key=m.api_key,
        dark_style=m.dark_style,
        light_style=m.light_style,
        attribution=m.attribution,
    )

    if spec.needs_key:
        return CheckResult(
            "web.basemap",
            WARN,
            f"provider is {m.provider!r} with no API key, so its tiles come "
            "back watermarked 'API KEY REQUIRED' with a 200 status; the maps "
            f"are rendering on {spec.provider!r} instead",
            remedy=(
                "Request a free key at https://carto.com/basemaps/apikey/ and "
                "set it in [web.map] api_key, or per user on the location "
                "settings page. Or set [web.map] provider = \"openfreemap\", "
                "which needs no key. Note that CARTO is retiring its raster "
                "service, so a key buys time rather than a fix."
            ),
            scope=DEPLOYMENT,
        )

    if spec.fell_back:
        return CheckResult(
            "web.basemap",
            WARN,
            f"basemap config did not resolve as written: {spec.warning}",
            remedy=(
                "Fix [web.map] provider, or the custom style URLs beside it. "
                f"The maps are rendering on {spec.provider!r} meanwhile."
            ),
            scope=DEPLOYMENT,
        )

    detail = f"provider {spec.provider!r}"
    if spec.provider in _UNVERIFIABLE_KEY_PROVIDERS:
        detail += (
            " with an API key configured — configured, not verified: the "
            "service answers 200 with the same watermarked tile for a good "
            "key, a bad key and no key at all"
        )
    if spec.warning:
        detail += f"; {spec.warning}"
    return CheckResult("web.basemap", OK, detail, scope=DEPLOYMENT)


# Providers whose key cannot be validated from a response. Named here rather
# than imported so the reason travels with the sentence that depends on it.
_UNVERIFIABLE_KEY_PROVIDERS = frozenset({"carto"})


def check_avatar_import(config: "Config", probe: bool) -> CheckResult:
    """Whether Nextcloud profile pictures are being imported, and what happened.

    **This check opens no socket, and that is deliberate rather than lazy.**
    Doctor runs on the daemon's start-up path, on a scheduler interval, from
    `istota doctor` and from the admin dashboard's Health pane. A live Nextcloud
    call here would put a remote timeout in front of all four, and the Health
    pane is a page a person is waiting on. Same reasoning as `web.basemap`.

    So it reports configuration and recorded state: the two switches, the counts
    in `user_avatars`, and what the last tick wrote down. That last part is the
    only way to answer the question that actually matters here — whether
    Nextcloud sends the custom-avatar header at all. Without it nothing can ever
    be imported, and no count of stored rows distinguishes that from a
    deployment where nobody has set a Nextcloud avatar. `probe` is accepted to
    satisfy the `Check` protocol and is unused.
    """
    from . import avatars, db
    from .nextcloud.avatars import CUSTOM_AVATAR_HEADER

    name = "web.avatar_import"

    web = getattr(config, "web", None)
    if not web or not web.enabled:
        return CheckResult(name, SKIP, "web interface disabled", scope=DEPLOYMENT)
    if not config.storage_is_nextcloud:
        return CheckResult(
            name, SKIP,
            "storage backend is local; there is no Nextcloud to import from",
            scope=DEPLOYMENT,
        )
    if not getattr(web, "avatar_import_from_nextcloud", False):
        return CheckResult(
            name, SKIP, "[web] avatar_import_from_nextcloud is false",
            scope=DEPLOYMENT,
        )
    if not getattr(config.scheduler, "avatar_import_interval", 0):
        return CheckResult(
            name, SKIP, "[scheduler] avatar_import_interval is 0", scope=DEPLOYMENT,
        )

    try:
        with db.get_db(config.db_path) as conn:
            counts = avatars.import_counts(conn)
            state = avatars.read_import_state(conn)
    except Exception as exc:  # noqa: BLE001 - a check never raises
        return CheckResult(
            name, WARN, f"could not read the avatar tables: {exc}",
            remedy=(
                "Check `runtime.framework_db`, which reports on the database "
                "itself; this check reads it and nothing else."
            ),
            scope=DEPLOYMENT,
        )

    stored = (
        f"{counts['imported']} imported, "
        f"{counts['probes']} with no custom Nextcloud avatar"
    )

    if state is None:
        return CheckResult(
            name, OK,
            f"enabled every {config.scheduler.avatar_import_interval}s; "
            f"no import tick has been recorded yet; {stored}",
            scope=DEPLOYMENT,
        )

    header = state.get("header")
    ran = state.get("at") or "an unrecorded time"
    detail = (
        f"last tick at {ran} over {state.get('users', 0)} users "
        f"({state.get('imported', 0)} imported, "
        f"{state.get('no_custom', 0)} with no custom avatar, "
        # `unchanged` is the steady state — every user answering 304 — so
        # leaving it out made a healthy deployment print four zeroes that do
        # not add up to the user count beside them, which reads as a tick that
        # did nothing rather than one with nothing to do.
        f"{state.get('unchanged', 0)} unchanged, "
        f"{state.get('failed', 0)} failed); {stored}"
    )

    if header == avatars.HEADER_ABSENT:
        return CheckResult(
            name, WARN,
            f"{detail}; Nextcloud sent no {CUSTOM_AVATAR_HEADER} "
            "header, so a user-set picture cannot be told from the coloured "
            "letter it generates and nothing will be imported",
            remedy=(
                "Nothing here is broken and nothing is being imported. Either "
                "upgrade Nextcloud to a version that sends the header, or set "
                "[web] avatar_import_from_nextcloud = false to stop asking. "
                "Users can still upload their own picture in Settings."
            ),
            scope=DEPLOYMENT,
        )

    # **A tick every user failed is not an OK**, and it used to be: `failed` was
    # rendered into the detail and gated nothing, so the only non-OK this check
    # could produce was the absent header. A deployment whose every fetch raised
    # — a wrong `nextcloud.username`, an expired app password, a uid mapping
    # that matches no Nextcloud account — printed `5 failed` inside a green
    # line. The whole reason this row is written down is that doctor cannot open
    # a socket to find out; reading it and then ignoring the one column that
    # says "this is not working" gives that up for nothing.
    failed = state.get("failed", 0)
    progressed = state.get("imported", 0) + state.get("no_custom", 0)
    if failed and not progressed:
        return CheckResult(
            name, WARN,
            f"{detail}; every user the last tick tried failed",
            remedy=(
                "Check `nextcloud.username` and the app password, and that the "
                "ids in [users] are the Nextcloud uids. The daemon log carries "
                "the per-user reason at WARNING, tagged avatar_import_failed."
            ),
            scope=DEPLOYMENT,
        )

    # **A tick that has not run in days is not an OK either.** Two documented
    # paths stop this job silently and leave the last good row standing as the
    # current answer: `_spawn_background_check` will not start a second run
    # while the first is alive, so one wedged fetch means no further ticks
    # ever, and `check_avatar_import` returns early on an unreadable probe
    # state without recording anything at all.
    stale = _avatar_tick_is_stale(
        state.get("at"), config.scheduler.avatar_import_interval
    )
    if stale:
        return CheckResult(
            name, WARN,
            f"{detail}; that is more than {stale} — the import may have stopped",
            remedy=(
                "Check the daemon log for avatar-import errors. A tick that "
                "never finishes blocks every later one, since a second run is "
                "not started while the first thread is alive."
            ),
            scope=DEPLOYMENT,
        )

    if header == avatars.HEADER_UNOBSERVED:
        detail += "; nothing changed, so the custom-avatar header was not read"
    return CheckResult(name, OK, detail, scope=DEPLOYMENT)


def _avatar_tick_is_stale(at: object, interval: int) -> str | None:
    """How overdue the last tick is, or None if it is not.

    Returns a human phrase rather than a bool so the caller can say how late it
    is. Parses defensively and answers None on anything it cannot read: `at` is
    a JSON value out of a KV table, and a check never raises — reporting a
    healthy import as broken because a timestamp changed shape would be worse
    than the staleness it is looking for.
    """
    if not isinstance(at, str) or not at.strip() or interval <= 0:
        return None
    text = at.strip().replace("Z", "+00:00")
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    # Three intervals, not one: a tick runs on a cadence and a single missed
    # one is a restart or a slow fetch, not a fault worth paging about.
    limit = interval * 3
    age = (datetime.now(timezone.utc) - when).total_seconds()
    if age <= limit:
        return None
    return f"{limit // 3600}h" if limit >= 3600 else f"{limit}s"


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
# Devbox network isolation
# ---------------------------------------------------------------------------

# The chain the role writes into, and the marker every rule it writes carries.
# The comment is how our rules are told apart from an operator's, and it is
# already load-bearing elsewhere: `iptables -C` matches on it, so the role's
# teardown depends on the exact string too.
DEVBOX_CHAIN = "DOCKER-USER"
_DEVBOX_RULE_MARK = "istota-devbox:"

# The boot script the Ansible role installs. Read as the *oracle* for what this
# host is supposed to be blocking, and for which subnet — a count hardcoded here
# would have to be updated in lockstep with the role's blocklist, and the first
# time it was not, the check would call a healthy host broken.
DEVBOX_BOOT_SCRIPT = Path("/usr/local/sbin/istota-devbox-iptables")

# Targets that end a packet's traversal of a user-defined chain without
# reaching what follows. DROP and REJECT are deliberately absent: a rule that
# blocks ahead of ours blocks more, not less, and reporting it would be noise.
_TERMINAL_TARGETS = frozenset({"RETURN", "ACCEPT"})

# Built-in targets, so a `-j` into anything else can be recognised as a jump
# into a user-defined chain — which this check does not follow, and which
# terminates traversal for whatever the target chain accepts.
_BUILTIN_TARGETS = frozenset(
    {
        "ACCEPT", "DROP", "RETURN", "REJECT", "LOG", "MARK", "MASQUERADE",
        "SNAT", "DNAT", "REDIRECT", "TCPMSS", "AUDIT", "CONNMARK", "NFLOG",
        "NFQUEUE", "NOTRACK", "TEE", "TPROXY", "TRACE", "ULOG",
    }
)

# Options that make a rule match less than every packet. `-m comment` is
# deliberately not one: a comment is an annotation, and treating it as a match
# condition is what let an annotated unconditional RETURN read as harmless.
_MATCH_OPTIONS = frozenset(
    {
        "-s", "--source", "-d", "--destination", "-i", "--in-interface",
        "-o", "--out-interface", "-p", "--protocol", "-f", "--fragment",
    }
)

_ENSURE_DROP = re.compile(r'^\s*ensure_drop\s+"([^"]+)"\s+"[^"]*"\s*$', re.M)
_SCRIPT_SUBNET = re.compile(r'^\s*SUBNET="([^"]*)"\s*$', re.M)


def parse_devbox_boot_script(text: str) -> set[str]:
    """The destinations the installed boot script blocks.

    Returns an empty set for anything it cannot read rather than raising —
    doctor runs on the daemon's start-up path, and a parser that threw on an
    unexpected file would turn a diagnostic into an outage.
    """
    try:
        return {match.group(1) for match in _ENSURE_DROP.finditer(text or "")}
    except Exception:  # noqa: BLE001 - a diagnostic must not raise
        return set()


def parse_devbox_boot_subnet(text: str) -> str:
    """The subnet the installed boot script scopes its rules to, or ""."""
    match = _SCRIPT_SUBNET.search(text or "")
    return match.group(1).strip() if match else ""


def parse_iptables_rule(line: str, chain: str) -> dict | None:
    """One ``-A <chain> ...`` line of ``iptables -S``, as fields.

    Tokenised with ``shlex`` rather than picked apart with a regex over the raw
    line, because a rule carries an arbitrary operator-supplied string in
    ``--comment``. A regex searching the whole line for ``-j`` finds the one
    inside ``--comment "see -j DROP note"`` and reports the wrong target, which
    on this check's FAIL path means reporting a shadowing rule as harmless.
    ``shlex`` puts the comment in a single token, so scanning tokens can only
    see real options.

    Returns None for a line this cannot read, so the caller can say it could not
    read it instead of quietly treating it as benign.
    """
    prefix = f"-A {chain}"
    try:
        tokens = shlex.split(line)
    except ValueError:
        return None
    if len(tokens) < 2 or tokens[0] != "-A" or tokens[1] != chain.split()[0]:
        return None
    if not line.strip().startswith(prefix):
        return None

    rule = {
        "raw": line.strip(),
        "target": "",
        "goto": False,
        "source": "",
        "destination": "",
        "comment": "",
        "conditional": False,
    }
    index = 2
    while index < len(tokens):
        token = tokens[index]
        value = tokens[index + 1] if index + 1 < len(tokens) else ""
        if token == "!":
            # A negated match is still a match condition.
            rule["conditional"] = True
            index += 1
            continue
        if token in ("-j", "--jump", "-g", "--goto"):
            rule["target"] = value
            rule["goto"] = token in ("-g", "--goto")
            index += 2
            continue
        if token in ("-s", "--source"):
            rule["source"] = value
            rule["conditional"] = True
            index += 2
            continue
        if token in ("-d", "--destination"):
            rule["destination"] = value
            rule["conditional"] = True
            index += 2
            continue
        if token == "--comment":
            rule["comment"] = value
            index += 2
            continue
        if token == "-m":
            # `-m comment` is an annotation, not a condition. Every other match
            # module narrows what the rule applies to.
            if value != "comment":
                rule["conditional"] = True
            index += 2
            continue
        if token in _MATCH_OPTIONS:
            rule["conditional"] = True
            index += 2
            continue
        if token.startswith("-"):
            # An option this does not model. Assume it narrows the rule rather
            # than assuming it does not: over-reporting a conditional rule is a
            # WARN, under-reporting one is a missed bypass.
            rule["conditional"] = True
            index += 2
            continue
        index += 1
    return rule


def _is_terminal(rule: dict) -> bool:
    """Does this rule stop traversal before the rules that follow it?

    A goto always does, and worse than a jump: when the target chain falls off
    its end, control returns to ``FORWARD``, not to ``DOCKER-USER``. A jump to
    a user-defined chain may, for whatever that chain accepts — this check does
    not follow it, so it counts as terminal and is reported as unfollowed.
    """
    if not rule["target"]:
        return False
    if rule["goto"]:
        return True
    if rule["target"] in _TERMINAL_TARGETS:
        return True
    return rule["target"] not in _BUILTIN_TARGETS


def _covers(rule: dict, subnet: str) -> bool | None:
    """Would `rule` catch traffic from `subnet`? True, False, or None for unknown.

    Three answers rather than two, because the two-answer versions are wrong in
    opposite directions and both were reachable here.

    An unscoped terminal rule catches everything, and that is the shape ISSUE-295
    is about — dockerd's own ``-j RETURN``. A rule scoped by ``-s`` is decidable:
    ``ufw-docker`` writes ``-s 172.16.0.0/12 -j RETURN``, and the devbox subnet
    lives inside that, so every devbox packet returns. Calling that merely
    "conditional" reports a total bypass as a warning nothing alerts on.

    But a rule scoped some other way — Docker Desktop seeds ``-i eth0 -j ACCEPT``
    — cannot be decided from the chain alone, because the devbox bridge's
    interface name is a generated hash this check has no way to learn. Answering
    True there would fire a FAIL on a common healthy shape, and a check that
    cries wolf is one nobody reads. So: unknown, reported as a WARN that names
    the rule.
    """
    if not rule["conditional"]:
        return True
    if not rule["source"] or not subnet:
        return None
    try:
        return ipaddress.ip_network(rule["source"], strict=False).overlaps(
            ipaddress.ip_network(subnet, strict=False)
        )
    except ValueError:
        return None


def check_repos_layout(config: "Config", probe: bool) -> CheckResult:
    """Are this deployment's clones where the daemon now looks for them?

    ``developer.repos_dir`` became a *per-user* root: every consumer that scopes
    a task — the bwrap bind, the native brain's write root, the
    ``DEVELOPER_REPOS_DIR`` manifest variable, the credential scrub — takes
    ``{repos_dir}/{user_id}``. That applies whatever the container backend is,
    because it is what closes cross-user worktree access rather than anything to
    do with containers.

    **Which makes the upgrade a silent failure without this check.** On a host
    whose clones still sit flat under ``repos_dir``, the per-user directory is
    empty, the bind is skipped because its source does not exist, and the
    developer skill is unusable with no error anywhere naming a path. The
    Ansible role performs the move and refuses to guess an owner where there is
    more than one user; this is what says so on a host it did not reach — a
    manual install, a half-finished play, an operator who moved one user's
    clones and not another's.

    Cheap and I/O-only: two ``iterdir`` passes and a marker test per entry, no
    subprocess. That is a statement about the work, not about the import graph,
    and it is not the same thing as being safe on the config-load path — the
    ``from .executor import`` below pulls in most of the package. This check is
    deliberately outside ``config.CONFIG_LOAD_CHECKS`` for that reason.
    """
    name = "developer.repos_layout"
    dev, reason = _dev_gate(config)
    if dev is None:
        return CheckResult(name, SKIP, reason)

    root = Path(dev.repos_dir)
    if not root.is_dir():
        return CheckResult(
            name, SKIP, f"{root} does not exist yet, so there is nothing filed in it",
        )

    from .executor import get_user_repos_dir  # noqa: PLC0415 - executor pulls in most of the package

    users = set(getattr(config, "users", {}) or {})
    stray: list[str] = []
    try:
        entries = sorted(root.iterdir())
    except OSError as exc:
        return CheckResult(
            name, SKIP, f"{root} could not be listed: {exc}",
        )
    for entry in entries:
        if not entry.is_dir() or entry.is_symlink() or entry.name in users:
            continue
        if _holds_a_repository(entry):
            stray.append(entry.name)

    if not stray:
        expected = [u for u in sorted(users) if (root / u).is_dir()]
        return CheckResult(
            name, OK,
            f"{root} holds only per-user roots"
            + (f" ({', '.join(expected)})" if expected else " and is empty"),
        )

    example = get_user_repos_dir(config, sorted(users)[0]) if users else None
    return CheckResult(
        name, FAIL,
        f"{root} still holds repositories outside any user's directory: "
        f"{', '.join(stray[:5])}"
        + (f" and {len(stray) - 5} more" if len(stray) > 5 else ""),
        remedy=(
            "The daemon now looks under {repos_dir}/{user_id}"
            + (f" — for example {example} — " if example else " ")
            + "and binds nothing when that directory does not exist, so the "
            "developer skill cannot see these. Re-run the Ansible role with "
            "`istota_developer_repos_migrate_to` set to the user they belong "
            "to, or move them by hand."
        ),
    )


def _holds_a_repository(entry: "Path", depth: int = 0) -> bool:
    """Is there a git directory at or just below `entry`?

    Two levels, matching the documented layout (`<namespace>/<project>.git`) and
    the migration script's own scan. Bounded rather than a full walk, because
    this runs on the start-up path and `repos_dir` holds working trees.
    """
    markers = ("HEAD", "config", "objects")
    if all((entry / marker).exists() for marker in markers):
        return True
    if (entry / ".git").exists():
        return True
    if depth >= 2:
        return False
    try:
        children = sorted(entry.iterdir())
    except OSError:
        return False
    return any(
        child.is_dir() and not child.is_symlink()
        and _holds_a_repository(child, depth + 1)
        for child in children
    )


# ---------------------------------------------------------------------------
# The development container
# ---------------------------------------------------------------------------

#: The registry name. Four results hang off it, one per property.
CONTAINER_GROUP = "developer.container"

#: How long a transport probe waits on the socket. Shorter than `PROBE_TIMEOUT`
#: because there is a per-user loop behind it and a dead container should be
#: reported quickly rather than made to look like a hang.
CONTAINER_PROBE_TIMEOUT = 5.0

#: How long the *server* gives the one command this check runs (`test -d`).
#: A constant rather than the connect budget: they answer different questions,
#: and a command allowed exactly as long as the client will wait for a read is a
#: race the client loses about half the time.
CONTAINER_EXEC_TIMEOUT = 10.0


def _container_results(names: Iterable[str], status: str, detail: str, remedy: str = "") -> list[CheckResult]:
    """The same answer for several of the group's checks."""
    return [
        CheckResult(f"{CONTAINER_GROUP}.{name}", status, detail, remedy=remedy)
        for name in names
    ]


def _exec_transport_request(
    socket_path: "Path", payload: bytes, timeout: float
) -> tuple[list[dict], str]:
    """Send one request over the exec socket; return its control frames.

    Speaks the wire directly rather than shelling the client: the client's job
    is to be a shim's `exec` target and exit with a command's status, and doctor
    wants the control frames the server sent. `devbox_exec_protocol` is a
    stdlib-only leaf, so importing it here costs nothing.

    Returns ``(frames, "")`` or ``([], reason)``. Never raises — this is a
    start-up path.
    """
    import socket as socket_module  # noqa: PLC0415 - a leaf import on a probe path

    from . import devbox_exec_protocol as proto  # noqa: PLC0415

    sock = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        try:
            sock.connect(str(socket_path))
        except OSError as exc:
            return [], f"could not connect to {socket_path}: {exc}"

        try:
            sock.sendall(payload)
        except OSError as exc:
            return [], f"could not send to {socket_path}: {exc}"

        buffered = bytearray()

        def _recv_exactly(count: int) -> bytes | None:
            while len(buffered) < count:
                try:
                    chunk = sock.recv(65536)
                except OSError:
                    return None
                if not chunk:
                    return None
                buffered.extend(chunk)
            out = bytes(buffered[:count])
            del buffered[:count]
            return out

        # The acknowledgement line comes first, and an error one closes the
        # connection with nothing streamed behind it.
        line = bytearray()
        while b"\n" not in line:
            if buffered:
                line.extend(buffered)
                buffered.clear()
                continue
            try:
                chunk = sock.recv(65536)
            except OSError as exc:
                return [], f"no acknowledgement from {socket_path}: {exc}"
            if not chunk:
                return [], f"{socket_path} closed before acknowledging"
            line.extend(chunk)
        cut = line.index(b"\n")
        buffered[:0] = bytes(line[cut + 1 :])
        try:
            ack = proto.decode_ack(bytes(line[:cut]))
        except Exception as exc:  # noqa: BLE001 - a malformed ack is a finding
            return [], f"{socket_path} sent an unreadable acknowledgement: {exc}"
        if ack.get("status") != "ok":
            return [], (
                f"{socket_path} refused the request: "
                f"{ack.get('code', '?')} {ack.get('message', '')}".strip()
            )
        if not proto.supported_protocol(ack.get("protocol")):
            return [], (
                f"{socket_path} speaks protocol {ack.get('protocol')!r}; this "
                f"daemon speaks {proto.PROTOCOL_VERSION}"
            )

        frames: list[dict] = []
        while True:
            header = _recv_exactly(proto.FRAME_HEADER_BYTES)
            if header is None:
                return frames, f"{socket_path} closed before the terminal frame"
            try:
                stream, length = proto.unpack_header(header)
            except Exception as exc:  # noqa: BLE001
                return frames, f"{socket_path} sent an unreadable frame: {exc}"
            body = _recv_exactly(length) if length else b""
            if body is None:
                return frames, f"{socket_path} closed mid-frame"
            if stream != proto.STREAM_CONTROL:
                continue
            try:
                obj = proto.decode_control(body)
            except Exception as exc:  # noqa: BLE001
                return frames, f"{socket_path} sent an unreadable control frame: {exc}"
            frames.append(obj)
            if proto.is_terminal(obj):
                return frames, ""
    except Exception as exc:  # noqa: BLE001 - never raise from a check
        return [], f"{socket_path}: {type(exc).__name__}: {exc}"
    finally:
        try:
            sock.close()
        except OSError:
            pass


def check_developer_container(config: "Config", probe: bool) -> list[CheckResult]:
    """The five properties of the development container that fail silently.

    Every one of these is a thing an operator learns about from a task failing
    hours later, or — worse — never learns about at all:

    * **backend** — the rendered config and the running daemon disagree, so a
      deployment that was switched on is still building on the host. A unit test
      cannot tell an operator that the host in front of them is the one with the
      problem.
    * **transport** — nothing answers on the socket. This is what replaces the
      per-task setup ping: the same question, asked once by an operator instead
      of once per task by every task, including the tasks that will never run a
      build.
    * **identity** — the container's uid is not the daemon's, or the two sides
      spell the repos root differently. Untreated, that ends in worktrees that
      can never be reaped, and there is no error message anywhere that says so.
    * **uv_cache** — the derived package cache, ``{repos_dir}/{user_id}/
      .package-caches``, is not visible inside the container, so it is not
      covered by the repos mount and every ``uv sync`` pays a full copy instead
      of a hardlink. Merely slow is what nobody investigates.
    * **command_reaper** — the server is running without the child that kills
      its commands when it is killed rather than stopped. Every command is
      still reaped on its own exit path, so nothing fails; what accumulates is
      builds that outlived a server the container's OOM killer picked off, and
      the only place that was ever visible is here.

    Returns five results whatever happens, so a caller can assert on a name
    rather than on a count.
    """
    from . import config as config_module  # noqa: PLC0415 - a cycle at module scope

    names = ("backend", "transport", "identity", "uv_cache", "command_reaper")
    backend = config_module.container_backend(config)
    results = [_container_backend_result(config, backend, config_module)]

    dev = getattr(config, "developer", None)
    if backend != config_module.CONTAINER_BACKEND_DEVBOX:
        # There used to be a fourth pair here worth warning about — the devbox
        # skill offered while `[developer.container] backend = "none"` meant
        # every verb but `reset` refused. The key is retired and the backend is
        # derived from `[devbox] enabled`, so that state can no longer be
        # configured and the detail only has to name whichever input is off.
        if not getattr(dev, "enabled", False):
            why = "the developer skill is off"
        elif not getattr(dev, "repos_dir", ""):
            why = "developer.repos_dir is empty, so there is no containment root"
        else:
            why = "[devbox] enabled is false"
        return results + _container_results(
            names[1:], SKIP,
            f"development commands run on the host: {why}",
        )

    users = sorted(getattr(config, "users", {}) or {})
    if not users:
        return results + _container_results(
            names[1:], SKIP, "no users are configured, so there is no devbox to reach",
        )
    if not probe:
        return results + _container_results(
            names[1:], SKIP,
            "reaching the container means opening its socket (probe disabled)",
        )

    return results + _container_probe_results(config, config_module, users)


def _container_backend_result(config: "Config", backend: str, config_module) -> CheckResult:
    """Does the file on disk derive what the running process believes?

    The daemon holds the config it loaded at start-up. An operator who edited
    ``config.toml`` — or an Ansible run that rendered a new one — has changed
    nothing until the daemon restarts, and the symptom is a feature that was
    switched on and did not switch on.

    Since the ``backend`` key was retired this has to re-derive rather than read
    one value, from the same three inputs :func:`config.devbox_container_backend`
    uses. That is the point of doing it here rather than comparing the key: a
    check that reads a key nobody sets any more reports ``OK`` on every
    deployment forever.

    A file still carrying the retired key is reported whatever the derivation
    says, because it is the one case where an operator's stated intent and the
    running behaviour can differ without any drift being present.
    """
    name = f"{CONTAINER_GROUP}.backend"
    path = getattr(config, "config_path", None)
    if not path:
        return CheckResult(
            name, SKIP,
            f"this process built its config in memory, so there is no rendered "
            f"file to compare backend={backend!r} against",
        )
    try:
        import tomllib  # noqa: PLC0415
    except ModuleNotFoundError:  # pragma: no cover - 3.10 and older
        import tomli as tomllib  # type: ignore[no-redef]  # noqa: PLC0415
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError) as exc:
        return CheckResult(
            name, WARN,
            f"{path} could not be read, so backend={backend!r} could not be "
            f"checked against it: {exc}",
            remedy="Fix or re-render the config file the daemon was started with.",
        )

    developer = data.get("developer") or {}
    on_disk = (
        config_module.CONTAINER_BACKEND_DEVBOX
        if (
            developer.get("enabled", False)
            and str(developer.get("repos_dir", "") or "").strip()
            and (data.get("devbox") or {}).get("enabled", False)
        )
        else config_module.CONTAINER_BACKEND_NONE
    )

    retired = (developer.get("container") or {}).get("backend")

    # **Drift is asked first, and the retired key never suppresses it.** The
    # obvious order — report the stale key and return — makes this check dead
    # on exactly the hosts most likely to have one: `config.toml.j2` stopped
    # emitting the key, so an Ansible-managed host loses it on the next deploy,
    # while a hand-maintained `/etc/istota/config.toml` keeps it for ever. On
    # those, a WARN about a key would stand in for a FAIL about a daemon
    # running the wrong thing, permanently and silently, which is the failure
    # class this check exists for.
    if on_disk != backend:
        detail = (
            f"{path} derives backend={on_disk!r} and this process is running "
            f"backend={backend!r}"
        )
        remedy = (
            "Restart the daemon so it loads the rendered config. Until it does, "
            "development commands run wherever the *running* value says."
        )
        if retired is not None:
            detail += (
                f". The file also still sets [developer.container] "
                f"backend={retired!r}, which is retired and ignored — it is not "
                f"the cause of this drift and deleting it will not clear it"
            )
        return CheckResult(name, FAIL, detail, remedy=remedy)

    if retired is not None:
        return CheckResult(
            name, WARN,
            f"{path} still sets [developer.container] backend={retired!r}, which "
            f"is retired and ignored; this deployment derives backend="
            f"{on_disk!r} from [devbox] enabled",
            remedy=(
                "Delete the key. If it was set to 'none' to keep builds on the "
                "host, turn [devbox] enabled off instead — that is now the one "
                "switch, and leaving the stale key in place hides which of the "
                "two an operator meant."
            ),
        )

    return CheckResult(
        name, OK, f"{path} and this process agree: backend={backend!r}",
    )


def _container_probe_results(config: "Config", config_module, users: list[str]) -> list[CheckResult]:
    """Transport, identity and uv_cache, from one connection per user."""
    from . import devbox_exec_protocol as proto  # noqa: PLC0415

    timeout = min(
        float(
            getattr(
                getattr(getattr(config, "developer", None), "container", None),
                "connect_timeout_seconds",
                CONTAINER_PROBE_TIMEOUT,
            )
            or CONTAINER_PROBE_TIMEOUT
        ),
        PROBE_TIMEOUT,
    )

    # Not `security.sandbox_cache_dir`. That key stopped being the cache root:
    # the cache is derived at `{repos_dir}/{user_id}/.package-caches`, and the
    # key is read only where `repos_dir` is unset. Asking after it here would
    # warn on exactly the deployments that are configured correctly, since the
    # Ansible default for it is blank.
    repos_root_cfg = getattr(getattr(config, "developer", None), "repos_dir", "")
    from .executor import SANDBOX_CACHE_ROOT_NAME  # noqa: PLC0415 - executor pulls in most of the package

    reachable: list[str] = []
    transport_bad: list[str] = []
    identity_bad: list[str] = []
    identity_ok: list[str] = []
    cache_bad: list[str] = []
    cache_ok: list[str] = []
    reaper_bad: list[str] = []
    reaper_ok: list[str] = []
    without_a_devbox: list[str] = []

    for user_id in users:
        socket_path = config_module.exec_socket_path(config, user_id)
        if socket_path is None:
            transport_bad.append(f"{user_id}: no socket path could be composed")
            continue
        # **Which users have a devbox is not in the daemon's config.** The list
        # lives in Ansible (`istota_devbox_users`) and reaches neither
        # `config.users` nor `DevboxConfig`, so iterating every configured user
        # would FAIL this check permanently on the reference shape — one admin
        # with a container, several other users without — and `check_doctor`'s
        # hourly sweep would alert every admin on the transition. A check that
        # cries wolf is one nobody reads.
        #
        # The per-user socket *directory* is the discriminator available here:
        # the role creates it only for `istota_devbox_users`, both in the play
        # and in the tmpfiles snippet that recreates it at boot. Its absence is
        # "no devbox for this user", not "the devbox is broken".
        if not socket_path.parent.is_dir():
            without_a_devbox.append(user_id)
            continue
        frames, error = _exec_transport_request(
            socket_path, proto.encode_ping_request(), timeout
        )
        if error:
            transport_bad.append(f"{user_id}: {error}")
            continue
        if not any(frame.get("pong") is True for frame in frames):
            transport_bad.append(f"{user_id}: {socket_path} answered without a pong")
            continue
        reachable.append(user_id)

        frames, error = _exec_transport_request(
            socket_path, proto.encode_stat_request(), timeout
        )
        stat = next((f for f in frames if "uid" in f), None)
        if stat is None:
            identity_bad.append(
                f"{user_id}: {error or 'the server sent no stat reply'}"
            )
        else:
            findings = _identity_findings(config, config_module, user_id, stat)
            if findings:
                identity_bad.extend(findings)
            else:
                identity_ok.append(user_id)
            # A server too old to answer says nothing, which is not the same as
            # answering `false`. Only an explicit `false` is a finding here.
            if stat.get("reaper") is False:
                reaper_bad.append(user_id)
            elif stat.get("reaper") is True:
                reaper_ok.append(user_id)

        if not repos_root_cfg:
            continue
        cache_dir = Path(repos_root_cfg) / user_id / SANDBOX_CACHE_ROOT_NAME
        frames, error = _exec_transport_request(
            socket_path,
            # A *server-side* budget, deliberately not `timeout`. That one is
            # the connect budget, which `_parse_container_block` floors at 0.1 —
            # an operator who sets it small for fast failure would otherwise get
            # a 0.1s kill budget on `test -d` and be told their cache mount is
            # missing. The two numbers answer different questions and one of
            # them is not the operator's to set.
            proto.encode_exec_request(
                argv=["test", "-d", str(cache_dir)],
                cwd=None,
                stdin=False,
                timeout=CONTAINER_EXEC_TIMEOUT,
            ),
            timeout,
        )
        terminal = next((f for f in frames if proto.is_terminal(f)), None)
        if error and terminal is None:
            cache_bad.append(f"{user_id}: {error}")
        elif terminal is None or terminal.get("exit_code") != 0:
            cache_bad.append(f"{user_id}: {cache_dir} is not a directory in the container")
        else:
            cache_ok.append(user_id)

    results = [_transport_result(reachable, transport_bad, without_a_devbox)]
    results.append(_identity_result(identity_ok, identity_bad, reachable))
    results.append(_uv_cache_result(repos_root_cfg, cache_ok, cache_bad, reachable))
    results.append(_reaper_result(reaper_ok, reaper_bad, reachable))
    return results


def _identity_findings(config: "Config", config_module, user_id: str, stat: dict) -> list[str]:
    """What the container disagrees with the daemon about, if anything."""
    findings: list[str] = []
    daemon_uid = os.getuid() if hasattr(os, "getuid") else None
    container_uid = stat.get("uid")
    if daemon_uid is not None and container_uid != daemon_uid:
        findings.append(
            f"{user_id}: the container's server runs as uid {container_uid} and "
            f"this daemon is uid {daemon_uid}"
        )
    from .executor import get_user_repos_dir  # noqa: PLC0415 - executor pulls in most of the package

    expected = get_user_repos_dir(config, user_id)
    reported = stat.get("repos_root")
    if expected is not None and reported != str(expected):
        findings.append(
            f"{user_id}: the container's repos root is {reported!r} and this "
            f"daemon's is {str(expected)!r}"
        )
    return findings


def _transport_result(
    reachable: list[str], bad: list[str], without_a_devbox: list[str]
) -> CheckResult:
    name = f"{CONTAINER_GROUP}.transport"
    if not bad and not reachable:
        return CheckResult(
            name, SKIP,
            f"{len(without_a_devbox)} configured user(s) have no devbox socket "
            f"directory, so none of them routes development work into a container",
        )
    if bad:
        return CheckResult(
            name, FAIL,
            "the exec transport did not answer for " + "; ".join(bad),
            remedy=(
                "Check the devbox container is up and its exec server is running "
                "(`docker logs devbox-<user>`), that ISTOTA_EXEC_SOCKET and "
                "ISTOTA_EXEC_REPOS_ROOT are set on the service, and that the "
                "socket directory is mounted into it."
            ),
        )
    detail = f"the exec transport answered a ping for {len(reachable)} devbox user(s)"
    if without_a_devbox:
        detail += f"; {len(without_a_devbox)} configured user(s) have no devbox"
    return CheckResult(name, OK, detail)


def _identity_result(ok: list[str], bad: list[str], reachable: list[str]) -> CheckResult:
    name = f"{CONTAINER_GROUP}.identity"
    if bad:
        return CheckResult(
            name, FAIL,
            "the daemon and the container do not agree: " + "; ".join(bad),
            remedy=(
                "Rebuild the devbox image with DEV_UID/DEV_GID set to the "
                "daemon's own uid and gid, and recreate the container. Until "
                "they match, every worktree that runs a build becomes "
                "unreapable and nothing else reports it."
            ),
        )
    if not reachable:
        return CheckResult(name, SKIP, "no container answered, so nothing was compared")
    return CheckResult(
        name, OK,
        f"uid and repos root agree for {len(ok)} devbox user(s)",
    )


def _reaper_result(
    ok: list[str], bad: list[str], reachable: list[str]
) -> CheckResult:
    """Is anything behind the exec server if it is killed rather than stopped?

    WARN rather than FAIL: the transport works, commands run, and every one of
    them is still killed on its own exit path. What is missing is the backstop
    for the one death that skips those paths — so the cost is builds that
    outlive a server the container's OOM killer picked off, which is a leak
    rather than an outage.

    A server too old to report the field is not a finding. It reports SKIP by
    landing in neither list, the same way an unreachable one does.
    """
    name = f"{CONTAINER_GROUP}.command_reaper"
    if bad:
        return CheckResult(
            name, WARN,
            "the exec server has no command reaper for " + ", ".join(bad),
            remedy=(
                "Grep the container's log for 'reaper' (`docker logs "
                "devbox-<user>`) and restart the container. It either never "
                "started ('cannot start the reaper', 'cannot create the reaper "
                "pipe') or died later ('the reaper is gone', 'the reaper "
                "exited'), and the second is the common one. Until it is back, "
                "a server killed rather than stopped leaves every command it "
                "was running alive in the container."
            ),
        )
    if not ok:
        return CheckResult(
            name, SKIP,
            "no container reported whether it has a command reaper"
            if reachable
            else "no container answered, so nothing was asked",
        )
    return CheckResult(
        name, OK, f"the command reaper is running for {len(ok)} devbox user(s)",
    )


def _uv_cache_result(
    repos_root_cfg: str, ok: list[str], bad: list[str], reachable: list[str]
) -> CheckResult:
    """Is the derived package cache visible inside the container?

    The question this asks changed, and the old one would now be actively
    misleading. It used to be "did the operator set `security.sandbox_cache_dir`
    and is its bind present" — but the cache is derived at
    `{repos_dir}/{user_id}/.package-caches` now, that key is read only where
    `repos_dir` is unset, and its Ansible default is blank. Asking after the key
    would WARN on every correctly configured deployment.

    What is worth checking is the property, not the setting: the cache lives
    inside the repos subtree the container already mounts, so one mount covers
    cache and venv and `link(2)` hardlinks rather than copying. If that
    directory is missing from the container, the mount is wrong in a way that is
    slow rather than broken — which is exactly the failure nobody investigates
    on their own.
    """
    name = f"{CONTAINER_GROUP}.uv_cache"
    if not repos_root_cfg:
        return CheckResult(
            name, SKIP,
            "developer.repos_dir is unset, so there is no per-user repos subtree "
            "and no derived package cache to look for",
        )
    if bad:
        return CheckResult(
            name, WARN,
            "the derived package cache is not visible in the container for "
            + "; ".join(bad),
            remedy=(
                "The cache is {developer.repos_dir}/{user}/.package-caches and "
                "sits inside the repos bind, so a missing directory means that "
                "bind is wrong or the container predates it. Re-run the role and "
                "recreate the container. Slow rather than broken — uv falls back "
                "to copying every wheel — which is why nothing else will tell you."
            ),
        )
    if not reachable:
        return CheckResult(name, SKIP, "no container answered, so nothing was checked")
    return CheckResult(
        name, OK,
        f"the derived package cache is visible for {len(ok)} devbox user(s)",
    )


def check_devbox_netfilter(config: "Config", probe: bool) -> CheckResult:
    """Read the live ``DOCKER-USER`` chain and report anything shadowing our rules.

    This is the only witness over the devbox network boundary that looks at a
    running host. ``tests/test_ansible_devbox_iptables.py`` proves the role asks
    for the right rules in the right position; it cannot see what an operator,
    a host-firewall integration or a different Docker version put in the chain
    afterwards. ISSUE-295 is exactly that gap: four correct rules behind a
    ``-j RETURN`` are never evaluated, and ``iptables -S`` renders them
    identically to four that work.

    **Reading the chain needs root and the daemon does not run as root**, so
    under the scheduler unit this check reports SKIP and says so in as many
    words. That is a real limitation rather than a quiet one: the detail names
    the command to run by hand, because a SKIP that reads as "not applicable
    here" when it means "never runs under this unit" would be the same shape of
    silence the check exists to break.
    """
    name = "security.devbox_netfilter"
    # This was a disjunction over two switches, guarding the pair where
    # `backend = devbox` with `devbox.enabled = false` put every build in the
    # estate inside a container whose egress filtering nothing checked. The
    # backend is derived from `[devbox] enabled` now, so it can no longer be on
    # while this is off and the second arm could never fire. One switch, and it
    # is the one the Ansible role gates the rules themselves on.
    devbox_on = getattr(getattr(config, "devbox", None), "enabled", False)
    if not devbox_on:
        return CheckResult(
            name, SKIP,
            "devbox is disabled ([devbox] enabled); the role adds no rules",
        )
    if not probe:
        return CheckResult(
            name,
            SKIP,
            f"the live {DEVBOX_CHAIN} chain cannot be read without spawning iptables "
            "(probe disabled)",
        )

    result = _run(["iptables", "-S", DEVBOX_CHAIN])
    if result is None:
        return CheckResult(
            name,
            SKIP,
            f"iptables could not be run, so the {DEVBOX_CHAIN} chain was not read",
        )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        lowered = stderr.lower()
        if "permission denied" in lowered or "must be root" in lowered:
            return CheckResult(
                name,
                SKIP,
                f"reading {DEVBOX_CHAIN} needs root, and this process is not root — "
                "under the scheduler unit this check never runs, so verify the chain "
                "by hand with `sudo istota doctor --only security.devbox_netfilter`",
            )
        if "no chain" in lowered:
            return CheckResult(
                name,
                FAIL,
                f"the {DEVBOX_CHAIN} chain does not exist, so no devbox rule is present",
                remedy=(
                    "Start dockerd, which creates DOCKER-USER, then run "
                    "`systemctl start istota-devbox-iptables`."
                ),
            )
        return CheckResult(
            name,
            SKIP,
            f"`iptables -S {DEVBOX_CHAIN}` exited {result.returncode} and was not read",
        )

    # The boot script is the oracle for what this host should block and for the
    # subnet the rules are scoped to. Read before anything is judged: without it
    # there is no way to tell "configured to block nothing" from "should be
    # blocking four things and is blocking none".
    try:
        script = DEVBOX_BOOT_SCRIPT.read_text()
    except OSError:
        script = ""
    expected = parse_devbox_boot_script(script)
    subnet = parse_devbox_boot_subnet(script)

    parsed = [parse_iptables_rule(line, DEVBOX_CHAIN) for line in result.stdout.splitlines()]
    unreadable = sum(
        1
        for line, rule in zip(result.stdout.splitlines(), parsed)
        if rule is None and line.strip().startswith(f"-A {DEVBOX_CHAIN}")
    )
    rules = [rule for rule in parsed if rule is not None]

    marked = [rule for rule in rules if _DEVBOX_RULE_MARK in rule["comment"]]
    # Carrying our comment is not enough to be one of our rules. A rule with our
    # marker and a terminal target would otherwise be counted as ours, excluded
    # from the shadowing scan, and reported as part of a healthy boundary while
    # being the thing that breaks it.
    ours = [
        index
        for index, rule in enumerate(rules)
        if _DEVBOX_RULE_MARK in rule["comment"] and rule["target"] == "DROP"
    ]
    impostors = [r for r in marked if r["target"] != "DROP"]
    if impostors:
        return CheckResult(
            name,
            FAIL,
            f"{len(impostors)} rule(s) in {DEVBOX_CHAIN} carry the devbox comment but "
            f"jump to {impostors[0]['target']}, not DROP",
            remedy=(
                f"Read `iptables -S {DEVBOX_CHAIN}`; a rule wearing the devbox comment "
                "with another target did not come from this role. Remove it and "
                "re-run `systemctl restart istota-devbox-iptables`."
            ),
        )

    if not ours:
        if not script:
            return CheckResult(
                name,
                SKIP,
                f"{DEVBOX_CHAIN} carries no devbox rules and no boot script is "
                "installed, so there is nothing to say what this host should block",
            )
        if not expected:
            return CheckResult(
                name,
                OK,
                "the devbox is configured to block nothing "
                "(istota_devbox_block_metadata and istota_devbox_block_rfc1918 are "
                f"both off) and {DEVBOX_CHAIN} carries no devbox rules, as expected",
            )
        return CheckResult(
            name,
            FAIL,
            f"{DEVBOX_CHAIN} carries none of the {len(expected)} devbox DROP rules "
            f"the installed boot script blocks ({len(rules)} other rule(s) present)",
            remedy=(
                "Re-run the Ansible role, or `systemctl start "
                "istota-devbox-iptables` to re-apply them now."
            ),
        )

    # Everything ahead of the *last* of our rules, not the first: a terminal rule
    # interleaved among them leaves the ones behind it unreachable, and scanning
    # only up to the first would report that chain as healthy.
    preceding = [r for r in rules[: ours[-1]] if _DEVBOX_RULE_MARK not in r["comment"]]
    terminal = [r for r in preceding if _is_terminal(r)]
    covering = [r for r in terminal if _covers(r, subnet) is True]
    undecidable = [r for r in terminal if _covers(r, subnet) is None]
    if covering:
        first = covering[0]
        how = "a goto to" if first["goto"] else "a jump to"
        scope = f" scoped to {first['source']}" if first["source"] else " matching every packet"
        return CheckResult(
            name,
            FAIL,
            f"{len(covering)} rule(s) ahead of the devbox DROP rules in "
            f"{DEVBOX_CHAIN} end the chain for devbox traffic — the first is "
            f"{how} {first['target']}{scope}",
            remedy=(
                f"Remove that rule from {DEVBOX_CHAIN}, or re-insert the devbox rules "
                "in front of it with `systemctl restart istota-devbox-iptables`."
            ),
        )
    if undecidable:
        first = undecidable[0]
        return CheckResult(
            name,
            WARN,
            f"{len(undecidable)} rule(s) ahead of the devbox DROP rules in "
            f"{DEVBOX_CHAIN} end the chain for traffic they match, and whether that "
            f"includes the devbox cannot be told from the chain — the first jumps "
            f"to {first['target']}: {first['raw']}",
            remedy=(
                f"Read `iptables -S {DEVBOX_CHAIN}` and confirm that rule cannot "
                "match devbox traffic; if it can, move the devbox rules in front of it."
            ),
        )
    if terminal:
        # Terminal, but decidably not about us — a rule scoped to a range the
        # devbox subnet does not overlap. Worth neither an alert nor silence.
        first = terminal[0]
        return CheckResult(
            name,
            OK,
            f"{len(ours)} devbox IPv4 DROP rule(s) are in {DEVBOX_CHAIN}; "
            f"{len(terminal)} rule(s) ahead of them end the chain only for traffic "
            f"outside the devbox subnet (the first is scoped to {first['source']})",
        )
    if unreadable:
        return CheckResult(
            name,
            WARN,
            f"{unreadable} rule(s) in {DEVBOX_CHAIN} could not be parsed, so whether "
            "they shadow the devbox rules is unknown",
            remedy=f"Read `iptables -S {DEVBOX_CHAIN}` by hand and check what precedes the devbox rules.",
        )

    if expected:
        present = {rules[i]["destination"] for i in ours}
        missing = sorted(
            dest
            for dest in expected
            if not _same_network(dest, present)
        )
        if missing:
            return CheckResult(
                name,
                WARN,
                f"{len(present)} of {len(expected)} devbox DROP rules are in "
                f"{DEVBOX_CHAIN}; missing: {', '.join(missing)}",
                remedy=(
                    "Re-apply them with `systemctl restart istota-devbox-iptables` "
                    "and check the unit's journal — `set -e` means it stops at the "
                    "first rule the kernel rejects."
                ),
            )

    return CheckResult(
        name,
        OK,
        f"{len(ours)} devbox IPv4 DROP rule(s) are in {DEVBOX_CHAIN} with nothing "
        "ahead of them that ends the chain for devbox traffic",
    )


def _same_network(dest: str, present: set[str]) -> bool:
    """Is `dest` one of `present`, comparing as networks rather than as strings?

    The kernel renders a bare address with its prefix length, so a boot script
    naming `169.254.169.254` and a chain carrying `169.254.169.254/32` are the
    same rule and must not be reported as a missing one.
    """
    if dest in present:
        return True
    try:
        wanted = ipaddress.ip_network(dest, strict=False)
    except ValueError:
        return False
    for candidate in present:
        try:
            if ipaddress.ip_network(candidate, strict=False) == wanted:
                return True
        except ValueError:
            continue
    return False


# ---------------------------------------------------------------------------
# Per-skill user overlays
# ---------------------------------------------------------------------------

#: How many offending files the detail names before it stops. A check's detail
#: is one line, and a tree with fifty broken overlays is one problem to go and
#: look at rather than fifty to read here.
_OVERLAY_REPORT_LIMIT = 5

#: How much of one filename the detail carries. A filename here is text the
#: *model* wrote — the directory is bound read-write into that user's sandbox —
#: and a name may be 255 bytes of anything but ``/`` and NUL. The detail is
#: printed to a terminal and rendered into the admin dashboard, so the count
#: limit above bounds the wrong axis on its own.
_OVERLAY_NAME_CHARS = 64

#: Why an overlay will never be loaded, as opposed to loading with something
#: worth saying about it. A denylisted name and a body past the cap are each a
#: misfiling that reaches no prompt, that nothing else would ever mention, and
#: that a person fixes by renaming, shrinking or removing the file.
#:
#: ``unknown_skill`` is deliberately **not** here, and is decided per file by
#: ``_overlay_near_miss`` instead. It is the one reason an ordinary task can
#: produce with a single ``touch``, because the directory is inside the tree
#: ``build_bwrap_cmd`` binds read-write into that user's sandbox — so a flat
#: FAIL on it is a deployment-scope alert any task can pin red and an operator
#: learns to skip past, which costs the signal the check was built to give
#: (ISSUE-340). A name one or two edits from a real skill is still FAIL, because
#: that is a typo and a typo is the case the check exists for.
_OVERLAY_FATAL_REASONS = frozenset({"denylisted", "over_cap"})

#: Reported against a user's overlay *directory* rather than against a file in
#: it: the directory resolves inside that user's own tree, so
#: `contained_overlay_dir` accepts it, but a component of the path is a symlink
#: and `open_overlay_dir` refuses to follow one (ISSUE-344). Every reader takes
#: the strict answer, so none of that user's overlays reaches a prompt. Not in
#: `_OVERLAY_FATAL_REASONS`, which is matched against an `inspect_overlay`
#: reason and never sees this one — it is appended to `dir_findings` directly,
#: which reports at WARN rather than FAIL (a task can produce one at will).
#: Names what was established rather than a cause: `open_overlay_dir` collapses
#: every `OSError` to a refusal, so an unreadable `config`, a regular file at
#: `skills` and an I/O error on the mount all arrive here too.
_OVERLAY_DIR_UNOPENABLE = "dir_not_openable"

#: The other directory-level refusal, from `contained_overlay_dir` rather than
#: from the descriptor walk: the path resolves *outside* the user's own tree.
#: Reported rather than skipped, because nothing else looks at this directory —
#: the loader degrades to no overlay, and the read verbs only ever run for one
#: user who asked. Skipping it left the most clear-cut plant of the set as the
#: only one nothing anywhere reported.
_OVERLAY_DIR_OUTSIDE_TREE = "dir_outside_user_tree"

#: Edits allowed between a filename and a real skill name before the two stop
#: being a plausible typo of each other, keyed on whether the *filename* is
#: short. Two edits out of four characters is most of the name, so at that
#: length the budget is what turns every scratch file into an alert; the same
#: two out of nine is a slip.
#: `_loader.OVERLAY_UNKNOWN_SKILL`, restated so the label helpers need no
#: import of a module whose graph is heavy; the two are pinned equal by
#: `tests/test_doctor.py`.
_UNKNOWN = "unknown_skill"

_OVERLAY_TYPO_SHORT_NAME_CHARS = 5
_OVERLAY_TYPO_BUDGET_SHORT = 1
_OVERLAY_TYPO_BUDGET_LONG = 2


#: How much of the *note* half of a label the detail carries. Larger than the
#: filename budget because the two are sized for different things: 64 is sized
#: against a 255-byte filename, while a note is a fixed sentence plus a skill
#: name, and cutting at 64 took the name off ``did you mean
#: a_very_long_operator_defined_skill...`` — marked as truncated, but no longer
#: something an operator can copy.
_OVERLAY_NOTE_CHARS = 120


def _overlay_safe_text(text: str, limit: int = _OVERLAY_NAME_CHARS) -> str:
    """One field of a reportable label, with the control characters taken out.

    A newline would forge a second line in a one-line detail, and an ANSI
    escape would repaint an operator's terminal. Neither is hypothetical: a
    filename is chosen by whatever wrote the file, and that is a sandboxed task
    as often as it is a person. The note is held to the same rule because it
    now carries a skill name read off disk rather than only literals, and so is
    the user id, which is a directory name read off the same mount.
    """
    safe = "".join(ch if ch.isprintable() else "?" for ch in text)
    if len(safe) > limit:
        safe = safe[:limit] + "..."
    return safe


def _overlay_label(user_id: str, name: str, note: str) -> str:
    """One reportable filename and what is wrong with it, every field sanitized."""
    return (
        f"{_overlay_safe_text(user_id)}/{_overlay_safe_text(name)}"
        f" ({_overlay_safe_text(note, _OVERLAY_NOTE_CHARS)})"
    )


def _edit_distance(a: str, b: str, budget: int) -> int | None:
    """Levenshtein distance between ``a`` and ``b``, or None if it exceeds ``budget``.

    Bounded rather than exact because the only question asked of it is "within
    ``budget``?", and the bound is what keeps a directory of long junk names
    from costing a full matrix each. Two rows, and the row minimum is a lower
    bound on every distance reachable from it, so a row that is already over
    budget can stop.
    """
    if abs(len(a) - len(b)) > budget:
        return None
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (0 if ca == cb else 1),
                )
            )
        if min(current) > budget:
            return None
        previous = current
    return previous[-1] if previous[-1] <= budget else None


#: A suffix that marks a file as *derived from* another rather than named for a
#: skill: an editor backup, a numbered copy, a hand-made snapshot. Matched only
#: against what is left when it is stripped, and only when that remainder is an
#: exact skill name — so this reads ``notes~``, ``notes2`` and ``notes.bak`` as
#: copies of the ``notes`` overlay and leaves every other name to the distance
#: test. Without it those are all one or two edits from a real name and so all
#: FAIL, which is the largest hole in ISSUE-340's fix: ``<skill>2``,
#: ``<skill>-1`` and ``<skill>~`` are exactly what a task leaves behind, and a
#: person who copies an overlay has not misspelled anything. The ``v`` of a
#: version marker is only recognised after a separator: without that, ``kv2``
#: strips to ``k`` and the ``kv`` skill behind it is never seen.
_OVERLAY_DERIVED_SUFFIX_RE = re.compile(
    r"(?:~|[ _.\-]?(?:copy|backup|bak|tmp|temp|orig|old|new|save)"
    r"|[ _.\-]v\d+|[ _.\-]?\d+)$",
    re.IGNORECASE,
)

#: How many stacked suffixes to strip — ``notes.bak2`` is two. Bounded because
#: the stem is a model-chosen filename and the loop is over its own output.
_OVERLAY_MAX_DERIVED_SUFFIXES = 3


def _derived_copy_of(stem: str, known_skills: Collection[str]) -> str | None:
    """The skill ``stem`` is a copy of, or None when it is not a copy of one.

    Returns the name rather than a bool so the report can say *which* file it
    was copied from: an operator reading ``unknown_skill`` against ``notes2.md``
    is otherwise told, by the WARN remedy, that the name is "not close enough
    to a skill to be a typo" — which is arithmetically false, since it is one
    edit away. It is here because it is a copy, and the label now says so.


    ``notes.bak`` is not a misspelling of ``notes``; it is a copy of it, and
    whoever made it did not believe it was live. The distance test cannot tell
    the two apart — a suffix is one or two edits either way — so this runs
    first and takes the FAIL back to a WARN. The file is still reported: what
    changes is which status it holds.

    Only an *exact* remainder counts. ``develper2`` strips to ``develper``,
    which is not a skill, so it falls through and is reported as the typo it
    is.
    """
    remaining = stem
    for _ in range(_OVERLAY_MAX_DERIVED_SUFFIXES):
        match = _OVERLAY_DERIVED_SUFFIX_RE.search(remaining)
        if match is None or match.start() == 0:
            return None
        remaining = remaining[: match.start()]
        if remaining in known_skills:
            return remaining
    return None


#: Split a filename into the words a person would read in it. A skill whose
#: every word appears is *named* by that filename even when the edit distance
#: is large, which is the direction distance is blind in: the more deliberately
#: someone decorates a name — ``developer.local``, ``01-developer``,
#: ``developer-overlay`` — the further it gets from ``developer`` and the
#: quieter a distance-only rule becomes, while the author's belief that the
#: file was live only gets more obvious.
_OVERLAY_TOKEN_SPLIT_RE = re.compile(r"[^0-9A-Za-z]+")

#: Below this a skill name is too short to carry meaning as a token — ``kv``
#: would match any filename with a ``kv`` word in it.
_OVERLAY_MIN_TOKEN_CHARS = 4


def _overlay_denylist() -> frozenset[str]:
    from .skills._loader import OVERLAY_DENYLIST  # noqa: PLC0415 - heavy import graph

    return OVERLAY_DENYLIST


def _denylist_key(name: str) -> str:
    from .skills._loader import _denylist_key as key  # noqa: PLC0415 - heavy import graph

    return key(name)


def _names_a_skill(stem: str, known_skills: Collection[str]) -> str | None:
    """The skill ``stem`` names outright, or None.

    Every word of the skill has to appear as a word of the filename, so
    ``developer.local`` and ``01-developer`` name ``developer`` while
    ``develop`` does not name anything. The longest match wins, so
    ``sensitive_actions_old`` reports the two-word skill rather than a
    one-word skill that happens to share a token.

    The false positive is real and accepted: ``release-notes.md`` names
    ``notes`` and will FAIL. It is also a file sitting in the overlay directory
    that reaches no prompt, so the report is not wrong about it — only louder
    than that particular name deserves.
    """
    tokens = {t.casefold() for t in _OVERLAY_TOKEN_SPLIT_RE.split(stem) if t}
    if not tokens:
        return None
    best: str | None = None
    for skill in sorted(known_skills):
        if len(skill) < _OVERLAY_MIN_TOKEN_CHARS:
            continue
        words = [w for w in _OVERLAY_TOKEN_SPLIT_RE.split(skill) if w]
        if words and all(w.casefold() in tokens for w in words):
            if best is None or len(skill) > len(best):
                best = skill
    return best


def _classify_unknown_overlay(
    stem: str, known_skills: Collection[str]
) -> tuple[bool, str]:
    """``(fails, note)`` for a filename that is not a known skill name.

    One place, because the severity and the words shown to the operator have to
    agree: every earlier version of this had a label that stated a reason the
    branch above it had not actually used.
    """
    copy_of = _derived_copy_of(stem, known_skills)
    if copy_of is not None:
        return False, f"{_UNKNOWN}, a copy of {copy_of}.md"
    near = _overlay_near_miss(stem, known_skills)
    if near is not None:
        if _denylist_key(near) in _overlay_denylist():
            # Suggesting a rename here would walk the operator straight into
            # the next FAIL: that name takes no overlay, and the write path
            # refuses it too.
            return True, f"{_UNKNOWN}, closest is {near}, which takes no overlay"
        return True, f"{_UNKNOWN}, did you mean {near}?"
    named = _names_a_skill(stem, known_skills)
    if named is not None:
        return True, f"{_UNKNOWN}, names the {named} skill but is not {named}.md"
    return False, _UNKNOWN


def _overlay_near_miss(stem: str, known_skills: Collection[str]) -> str | None:
    """The skill ``stem`` was probably meant to be, or None if nothing was.

    This is the whole of what separates ``develper.md`` — a customization its
    author believes is live and that nothing but ``doctor`` would ever mention
    — from ``zzz.md``, which is a scratch file. The first is worth a FAIL and
    the second is not.

    A dropped plural is on the FAIL side by design: ``note.md`` for the
    ``notes`` skill, ``task.md`` for ``tasks``. Those read as scratch names,
    and they are also the most common way there is to misspell a skill — a
    file whose rules reach no prompt either way. ``_derived_copy_of`` carves
    off the class that is genuinely not a misspelling.

    Compared case-insensitively, because ``Developer.md`` is a misfiling by the
    same argument. The whole index is compared against,
    denylisted names included: a misspelling of ``sensitive_actions`` is still
    a file somebody wrote rules into believing they would load.

    Ties are broken by name so two runs over the same directory report the
    same suggestion. ``known_skills`` is the ``load_skill_index`` mapping, whose
    order is the order three discovery layers happened to produce, so iterating
    it is deterministic within a process and not across deployments — which is
    the same problem as an unordered set for anything an operator compares.

    A candidate is skipped only on an **exact** string match, not on a
    casefolded distance of zero: ``Developer.md`` folds onto ``developer`` at
    distance zero and is precisely the misfiling worth reporting, since the
    index that rejected it is case-sensitive. An exact match means the caller
    is asking about a name the index never rejected, which is not a typo of
    anything.
    """
    if not stem:
        return None
    budget = (
        _OVERLAY_TYPO_BUDGET_SHORT
        if len(stem) < _OVERLAY_TYPO_SHORT_NAME_CHARS
        else _OVERLAY_TYPO_BUDGET_LONG
    )
    lowered = stem.casefold()
    best: tuple[int, str] | None = None
    for name in sorted(known_skills):
        if name == stem:
            continue
        distance = _edit_distance(lowered, name.casefold(), budget)
        if distance is None:
            continue
        if best is None or distance < best[0]:
            best = (distance, name)
    return None if best is None else best[1]


def _overlay_dirs(mount: Path, bot_dir: str) -> list[tuple[str, Path, Path | None]]:
    """`(user_id, user dir, overlay dir)` for every user under the mount with one.

    A `None` overlay dir means the path resolved outside that user's own tree —
    reported by the caller rather than dropped, since nothing else looks at it.

    Walked rather than taken from ``config.users`` because a user whose config
    block was removed still has a tree on disk, and a file left there is exactly
    the kind of thing nobody would otherwise be told about.

    Hardened the way every other reader of this tree is, and for the same
    reason: ``{mount}/Users/{user_id}`` is bound **read-write** into that user's
    own sandbox, so every path component under it is model-plantable. Three
    consequences here specifically, because this walk crosses *all* users where
    the loader and the CLI each stay inside one:

    - a user entry is required to be a real directory rather than a symlink to
      one, so a link planted at another user's name cannot make this walk
      descend somewhere else and report a file against the wrong user;
    - the overlay directory is resolved and required to stay under its own
      user's tree, since ``config`` and ``skills`` are both ordinary entries a
      task can replace with a link. That rule is
      ``_loader.contained_overlay_dir``, shared with the loader, the memory CLI
      and the search reindex, and the **resolved** path is what is returned;
      the caller then opens it with ``_loader.open_overlay_dir`` and walks the
      descriptor, since the resolved path alone still leaves the check and the
      reads separated by a window in which the link can be swapped
      (ISSUE-344). The user directory comes back too, because that is the root
      the descriptor walk starts from;
    - nothing here opens a file. ``scandir`` stats, and the read that follows
      is ``inspect_overlay``'s, which refuses a FIFO — ``doctor`` runs on the
      daemon's start-up path, where a blocking ``open(2)`` has no timeout over
      it at all.
    """
    from .skills._loader import contained_overlay_dir  # noqa: PLC0415 - heavy import graph

    users_root = mount / "Users"
    try:
        entries = sorted(os.scandir(users_root), key=lambda e: e.name)
    except OSError:
        return []

    found: list[tuple[str, Path, Path]] = []
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        user_dir = Path(entry.path)
        resolved = contained_overlay_dir(user_dir / bot_dir / "config" / "skills", user_dir)
        if resolved is None:
            # Resolves outside the user's own tree. Reported rather than
            # skipped: nothing else reports this directory, so a link pointing
            # clean out of the mount — the most clear-cut plant of the set —
            # was the one case nothing anywhere named. `None` in the third slot
            # is what the caller reads as "refused before it was opened".
            found.append((entry.name, user_dir, None))
            continue
        try:
            if not resolved.is_dir():
                continue
        except OSError:
            continue
        found.append((entry.name, user_dir, resolved))
    return found


def check_skill_overlays(config: "Config", probe: bool) -> CheckResult:
    """Every per-skill user overlay on the mount either binds, or is named here.

    An overlay is ``config/skills/<skill-name>.md`` appended to that skill's
    body whenever the skill loads. Nothing else in the system ever says a word
    about one: a file named for a skill that does not exist is silently never
    read, and so is one for a skill that takes no overlay, and so is one past
    the loading cap. Each looks configured from ``ls``, and the user's rule is
    simply absent from every prompt with nothing anywhere reporting it. That is
    the same failure class as the missing watermark and the devbox ``command not
    found`` — the defect is the absence of a signal rather than the presence of
    a bug.

    No process, no read past the cap, and ``probe`` is unused: this is a
    ``scandir`` per user plus one bounded read per overlay file.

    The gates are ``_loader.inspect_overlay``, shared with the ``memory
    skills`` inventory, so the two surfaces cannot disagree about which files
    are live. Two differences are deliberate:

    - **a disabled skill is not reported.** Its overlay binds again the moment
      the operator switches the skill back on, so it is a fact about the
      configuration rather than a defect in the file. The inventory does say
      so, because a user asking "is my customization live?" wants that answer;
      an operator sweeping for problems does not.
    - **no overlay content is quoted.** This runs across every user's tree, and
      the same result is rendered into the admin dashboard, so a filename is
      the most that may leave one user's directory. A filename is itself text
      the model wrote, so it goes through ``_overlay_label`` rather than
      straight into the detail.

    **FAIL is reserved for a misfiling.** A name on the denylist and a body
    past the loading cap are each a file a person can fix by renaming,
    shrinking or removing it, and each is the case the check exists for.
    Everything else ``inspect_overlay`` can report — an empty file, one that is
    not UTF-8, one this process was refused — also loads as nothing, but a
    transient ``EACCES`` on one user's file is not a broken deployment and must
    not turn the one status that alerts red.

    **A name that is not a skill splits, and the split is the whole of
    ISSUE-340.** This directory is inside the tree ``build_bwrap_cmd`` binds
    read-write into that user's sandbox, so one ``touch zzz.md`` from any
    ordinary task produces ``unknown_skill`` — and a deployment-scope FAIL that
    a task reaches by accident goes red often and is skipped past, which is
    worth less than no alert. So a name within a typo's distance of a real skill
    (``_overlay_near_miss``) keeps FAIL and carries the suggestion, since a
    misspelled overlay is a customization its author believes is live and this
    is the only surface that would ever say otherwise; anything further away
    WARNs and is still named in the detail. Neither status is silence: what
    changed is which one alerts.
    """
    name = "config.skill_overlays"
    if not config.use_mount:
        return CheckResult(
            name, SKIP, "no workspace mount configured, so overlays are not read"
        )
    mount = Path(config.nextcloud_mount_path)
    if not (mount / "Users").is_dir():
        return CheckResult(name, SKIP, f"{mount}/Users does not exist yet")

    try:
        from .skills._loader import (  # noqa: PLC0415
            OVERLAY_UNKNOWN_SKILL,
            inspect_overlay,
            load_skill_index,
            open_overlay_dir,
        )
        known = load_skill_index(config.skills_dir, bundled_dir=config.bundled_skills_dir)
    except Exception as exc:  # noqa: BLE001 - a check never raises
        return CheckResult(name, SKIP, f"the skill index could not be loaded: {exc}")

    dirs = _overlay_dirs(mount, config.bot_dir_name)
    total = 0
    dead: list[str] = []
    warned: list[str] = []
    # Directory-level refusals. Kept in their own list for two reasons. They
    # are not files, so measuring them against `total` rendered "1 of 0 overlay
    # file(s) are misfiled"; and they report at **WARN**, not FAIL, because a
    # sandboxed task can produce one at will — `ln -s /tmp config` inside its
    # own workspace — and a deployment-scope red an attacker can raise on
    # demand is the aimable alert ISSUE-340 split this check to avoid. That is
    # also the severity this module already gives a symlinked overlay *file*:
    # it loads as nothing and belongs in the report, but it is not the
    # misfiling a person fixes by renaming or shrinking, which is what
    # `_OVERLAY_FATAL_REASONS` is reserved for. Nothing about the safety half
    # turns on the severity — the link is refused either way, and no file from
    # behind it is opened or named.
    dir_findings: list[str] = []
    for user_id, user_dir, overlay_dir in dirs:
        if overlay_dir is None:
            dir_findings.append(
                _overlay_label(user_id, f"{config.bot_dir_name}/config/skills",
                               _OVERLAY_DIR_OUTSIDE_TREE)
            )
            continue
        # Opened one user at a time rather than all of them up front: this
        # walks every tree on the mount, and holding a descriptor per user
        # for the length of the sweep is a file-table cost for nothing.
        dir_fd = open_overlay_dir(user_dir, config.bot_dir_name, "config", "skills")
        if dir_fd is None:
            # `contained_overlay_dir` passed and this did not, so the two
            # disagree — deliberately: it accepts a symlink landing back inside
            # the user's own tree and the descriptor walk refuses one at any
            # component. Every reader now takes the strict answer, so this
            # user's overlays reach no prompt at all, which is exactly the
            # misfiling this check exists to name. The prompt loader degrades
            # silently and logs at `debug` (it runs once per eager skill per
            # task); this is the report that posture depends on (ISSUE-344).
            dir_findings.append(
                _overlay_label(user_id, f"{config.bot_dir_name}/config/skills",
                               _OVERLAY_DIR_UNOPENABLE)
            )
            continue
        try:
            # `scandir` on the descriptor rather than `overlay_dir.glob`, so the
            # listing comes from the directory that passed. The dotfile filter
            # `glob` applied is kept, and the asymmetry with the search reindex
            # and `skills overlays` is deliberate: those two attach no severity
            # to what they list, and this check does. `_classify_unknown_overlay`
            # reads `.developer.md` as a near-miss of `developer` and buckets it
            # `dead`, so listing dotfiles here would let any sandboxed task turn
            # a deployment-scope check red with one `touch` — the aimable alert
            # ISSUE-340 split this check to avoid.
            with os.scandir(dir_fd) as entries:
                names = sorted(
                    e.name for e in entries
                    if e.name.endswith(".md") and not e.name.startswith(".")
                )
        except OSError:
            os.close(dir_fd)
            continue
        try:
            for entry_name in names:
                total += 1
                path = overlay_dir / entry_name
                found = inspect_overlay(path, known_skills=known, dir_fd=dir_fd)
                if found.reason == OVERLAY_UNKNOWN_SKILL:
                    # The one reason a task produces with a single `touch`, so
                    # the severity turns on what the name looks like rather
                    # than on the reason alone. See ISSUE-340 and
                    # `_OVERLAY_FATAL_REASONS`.
                    fails, note = _classify_unknown_overlay(found.skill, known)
                    bucket = dead if fails else warned
                    bucket.append(_overlay_label(user_id, path.name, note))
                elif found.reason in _OVERLAY_FATAL_REASONS:
                    dead.append(_overlay_label(user_id, path.name, found.reason))
                elif found.reason is not None:
                    # Empty, not UTF-8, or a read this process was refused. Each
                    # loads as nothing and so belongs in the report, but none is
                    # a misfiling an operator acts on the way a renamed or
                    # shrunk file is, and a transient EACCES on one user's file
                    # must not turn a deployment-scope check red.
                    warned.append(_overlay_label(user_id, path.name, found.reason))
                elif found.warnings:
                    warned.append(
                        _overlay_label(user_id, path.name, ", ".join(found.warnings))
                    )
        finally:
            os.close(dir_fd)

    # `dir_findings` as well as `total`, because a refused directory contributes
    # no files: a check that returned OK here would report "no per-skill
    # overlays filed" for a user whose whole directory just stopped being
    # readable, which is the reassuring direction.
    if not total and not dead and not dir_findings:
        return CheckResult(
            name, OK,
            f"no per-skill overlays filed under {mount}/Users/*/{config.bot_dir_name}/config/skills",
        )

    #: A directory is not one of `total` overlay files, so it gets a clause of
    #: its own rather than a place in that fraction — counting it there read
    #: "1 of 0 overlay file(s) are misfiled" for a lone refused tree, and
    #: understated the ratio wherever one sat beside another user's good files.
    def _dir_clause() -> str:
        return (
            f"{len(dir_findings)} overlay director(y/ies) could not be read, so "
            f"none of those users' overlays loads: {_overlay_list(dir_findings)}"
        )

    if dead:
        detail = (
            f"{len(dead)} of {total} overlay file(s) are misfiled and will never "
            f"be loaded: {_overlay_list(dead)}"
        )
        if warned:
            # Never let a FAIL swallow the WARN list. Before ISSUE-340 split
            # `unknown_skill`, every one of these was fatal and so every one
            # was named; afterwards a single planted typo would have reported
            # "1 of 21" and hidden the other twenty — including a real overlay
            # sitting just under the loading cap — which is a count that reads
            # in the reassuring direction and an alert an attacker can aim.
            detail += (
                f"; {len(warned)} more reach no prompt or need a look: "
                f"{_overlay_list(warned)}"
            )
        if dir_findings:
            detail += f"; {_dir_clause()}"
        return CheckResult(
            name, FAIL, detail,
            remedy=(
                "A file here is only read when its name is a known skill that takes "
                "an overlay and its body is under the loading cap. A `did you mean` "
                "is a filename one or two characters off a real skill and a `names "
                "the X skill` is one built around a real name, so the rules in "
                "either reach no prompt at all — rename it to `X.md`. "
                + _OVERLAY_WARN_REMEDY
            ),
        )
    if warned or dir_findings:
        parts = []
        if warned:
            parts.append(
                f"{len(warned)} of {total} overlay file(s) need a look: "
                f"{_overlay_list(warned)}"
            )
        if dir_findings:
            parts.append(_dir_clause())
        return CheckResult(
            name, WARN, "; ".join(parts), remedy=_OVERLAY_WARN_REMEDY,
        )
    return CheckResult(
        name, OK, f"{total} overlay file(s) across {len(dirs)} user tree(s), all load"
    )


#: Shared by the WARN result and by the tail of the FAIL result, because a
#: FAIL now carries the warned files too and an operator reading it needs the
#: same glossary either way.
_OVERLAY_WARN_REMEDY = (
    "over_warn_bytes: the file is within a few KB of the loading cap, past "
    "which it stops reaching any prompt at all — shrink it, or move the rules "
    "that belong to another skill into that skill's own overlay. "
    "shallow_heading: a `# ` or `## ` heading is demoted to `#### ` at load "
    "time, because at its written level it would end the skill's own section. "
    "unknown_skill with `a copy of X.md`: an editor or a task left a backup "
    "beside the real overlay — delete it. unknown_skill on its own: the name "
    "resembles no skill, so it is most likely a scratch file — delete it, or "
    "rename it if it was meant to be an overlay. empty / overlay_not_utf8 / "
    "overlay_is_a_symlink / overlay_not_a_regular_file / overlay_unreadable: "
    "the file is there and contributes nothing to any prompt. "
    "dir_not_openable / dir_outside_user_tree: these name a directory rather "
    "than a file, and none of that user's overlays is read by anything. The "
    "path has to be a chain of plain directories inside the user's own tree: "
    "the usual cause is a symlink at `config` or `skills` (replace it with a "
    "real directory and move the files into it), but an unreadable directory "
    "or a regular file left at `skills` reads the same way, so check what is "
    "actually there before assuming a link. Run "
    "`istota-skill skills overlays` as that user for the per-file verdict."
)


def _overlay_list(items: list[str]) -> str:
    shown = ", ".join(items[:_OVERLAY_REPORT_LIMIT])
    if len(items) > _OVERLAY_REPORT_LIMIT:
        shown += f", and {len(items) - _OVERLAY_REPORT_LIMIT} more"
    return shown


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
    ("runtime.subscription_usage", check_subscription_usage),
    ("security.skill_proxy", check_skill_proxy),
    ("security.devbox_netfilter", check_devbox_netfilter),
    ("developer.forge_binaries", check_forge_binaries),
    ("developer.forge_config_drift", check_forge_config_drift),
    ("developer.forge_wrapper_shadowing", check_forge_wrapper_shadowing),
    ("developer.forge_policy", check_forge_policy),
    ("developer.gitlab_reviewer", check_gitlab_reviewer),
    ("developer.forge_transport", check_forge_transport),
    ("developer.repos_layout", check_repos_layout),
    ("developer.container", check_developer_container),
    ("web.static", check_web_static),
    ("web.basemap", check_basemap),
    ("web.avatar_import", check_avatar_import),
    ("config.skill_overlays", check_skill_overlays),
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
    # Deployment, not image: it needs a credential and network egress, neither of
    # which a bare `docker run` has. Not in DEEP_CHECKS — it spawns no namespace.
    "runtime.subscription_usage": DEPLOYMENT,
    "security.skill_proxy": DEPLOYMENT,
    "security.devbox_netfilter": DEPLOYMENT,
    "developer.forge_binaries": IMAGE,
    "developer.forge_config_drift": DEPLOYMENT,
    "developer.forge_wrapper_shadowing": IMAGE,
    "developer.forge_policy": DEPLOYMENT,
    "developer.gitlab_reviewer": DEPLOYMENT,
    "developer.forge_transport": DEPLOYMENT,
    # Deployment, not image: four of its five results need a running
    # container to reach, and the fifth reads the rendered config file.
    # Deployment: it is a fact about what is filed on this host.
    "developer.repos_layout": DEPLOYMENT,
    "developer.container": DEPLOYMENT,
    "web.static": IMAGE,
    # Deployment, not image: it reads the rendered config and reaches the
    # network. A bare `docker run` can answer neither.
    "web.basemap": DEPLOYMENT,
    # Deployment: every fact it reports is in the framework database or the
    # rendered config — the counts in `user_avatars` and what the last import
    # tick wrote down. A bare `docker run` has neither.
    "web.avatar_import": DEPLOYMENT,
    # Deployment: it walks the workspace mount, which a bare `docker run` has
    # none of.
    "config.skill_overlays": DEPLOYMENT,
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
