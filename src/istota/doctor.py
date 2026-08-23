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
from collections.abc import Callable, Iterable
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


# The one remedy this check can offer. A fixed literal: `detail` and `remedy` are
# built from this plus a percentage, a duration and a resolver branch name, never
# from the credential, the raw response body or an exception string.
#
# There used to be three. The other two answered a failure to obtain a reading —
# check your egress, re-run `claude setup-token`, the response shape changed —
# and both went with the WARNs they accompanied. A remedy belongs on a row an
# operator can act on, and "the endpoint will not serve this credential class"
# is not one: those rows are SKIPs now, carrying the reason and no instruction.
_USAGE_BUSY_REMEDY = (
    "Tasks will fail over to the fallback brain when this window is exhausted."
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
        return CheckResult(name, WARN, detail, remedy=_USAGE_BUSY_REMEDY)
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
    if not getattr(getattr(config, "devbox", None), "enabled", False):
        return CheckResult(
            name, SKIP, "devbox is disabled ([devbox] enabled); the role adds no rules"
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
    ("developer.forge_versions", check_forge_versions),
    ("developer.forge_wrapper_shadowing", check_forge_wrapper_shadowing),
    ("developer.forge_policy", check_forge_policy),
    ("developer.gitlab_reviewer", check_gitlab_reviewer),
    ("developer.forge_transport", check_forge_transport),
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
    # Deployment, not image: it needs a credential and network egress, neither of
    # which a bare `docker run` has. Not in DEEP_CHECKS — it spawns no namespace.
    "runtime.subscription_usage": DEPLOYMENT,
    "security.skill_proxy": DEPLOYMENT,
    "security.devbox_netfilter": DEPLOYMENT,
    "developer.forge_binaries": IMAGE,
    "developer.forge_config_drift": DEPLOYMENT,
    "developer.forge_versions": IMAGE,
    "developer.forge_wrapper_shadowing": IMAGE,
    "developer.forge_policy": DEPLOYMENT,
    "developer.gitlab_reviewer": DEPLOYMENT,
    "developer.forge_transport": DEPLOYMENT,
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
