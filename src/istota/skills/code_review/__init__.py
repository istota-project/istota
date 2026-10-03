"""Code review skill CLI.

`istota-skill code_review run --worktree <path> [--base <ref>] [--range <r>]
[--intent <text>]` reviews a branch diff with one reviewer through the
configured brain. The reviewer reads a snapshot of the reviewed commit with
`Read`, `Grep` and `Glob`, inside a namespace that binds the snapshot and
withholds every scope; where the snapshot or the namespace cannot be built it
reviews text-only. `--agents` is accepted for one release and ignored.

Where this runs matters more than what it does. The skill proxy spawns the
module *outside* the sandbox with the daemon's filesystem view, so `load_config`,
`make_brain` and the worktree are all reachable here and none of them is
reachable from the model. Everything the reviewer sees is assembled by
`engine.py` and `snapshot.py` from the repository's object store; the caller
supplies a path, a range and a line of intent, and nothing else. A
model-authored prompt never becomes a daemon-side read.

Four things gate a run before a single token is spent, and all four are in
`cmd_run` rather than spread across the engine:

* `developer.enabled`, a non-empty `repos_dir`, and `developer.review.enabled`.
* `config.is_admin(ISTOTA_USER_ID)`. This **fails open** — `is_admin` returns
  True when no admins file exists — and that is correct here, because it matches
  the sandbox bind exactly: on such a deployment every user already gets
  `repos_dir` bound. The shared-KV gate next door deliberately fails closed; do
  not collapse the two.
* `resolve_under_repos`, which is also what `devbox cp-in` and `kv
  set --value-file` use. Containment is necessary and nowhere near sufficient —
  `repos_dir` is bound read-write into the admin sandbox, so the engine's
  hardened git runner is what stands between a contained path and a repository
  whose configuration the model wrote.
* The per-task call budget in `code_review_calls`, in the framework database
  rather than a file under `ISTOTA_DEFERRED_DIR` — that directory is writable
  from the sandbox, so a loop that reached a file-backed cap could delete the
  counter and carry on spending.

Heavy imports (`config`, `brain`, `db`) are function-local so the module stays
cheap to import and so tests can patch them at their real home.
"""

import argparse
import time
import logging
import os
import sys
from pathlib import Path

from istota.sandbox import skill_proxy
from istota.sandbox.host_paths import developer_repos_root, resolve_under_repos
from istota.skills._cli import emit, parse_and_resolve, run_skill_cli
from istota.skills._hostpath import REPO, host_path

from . import engine
from . import snapshot as review_snapshot

logger = logging.getLogger(__name__)

# The diff, the snapshot and the prompt are built outside the reviewer's
# timeout, so the command's own wall time is `timeout_seconds` plus this. The
# proxy kills the command at the ceiling `_proxy_ceiling` resolves, and an
# operator who raises `timeout_seconds` past it should learn about it from a
# startup warning rather than from a review that dies half-finished.
#
# 20, from measurements rather than an estimate. ISSUE-448 measured about one
# second outside the model calls on a 6-file, 318-line diff. The snapshot is
# now the part that can take time, and it was measured on a development
# laptop (2026-10-03): this repository's own tree, 2,264 files and 46 MB,
# took 0.6 to 0.8s; a synthetic 30,000-file, 92 MB tree took 5.0 to 5.2s,
# diff and commit log included. A smaller host's disk is slower by some
# factor this does not know, which is what the remaining margin is for —
# `overhead_seconds` in the envelope is the measurement of every real run, so
# the next reader can check this against evidence instead of picking another
# number blind.
ASSEMBLY_ALLOWANCE_SECONDS = 20

# Headroom above the reviewer's own timeout, which is enforced inside the brain;
# this is the margin for a brain that overruns it. It used to be the slack on
# the two-agent thread join, and the clamp kept reserving it when the join went
# (ISSUE-448 found a budget clamped to "just fit" overrunning by exactly this).
JOIN_SLACK_SECONDS = 10

# What the clamp must keep clear of the proxy ceiling: the command's wall bound
# is `agent_timeout + JOIN_SLACK_SECONDS + assembly` (ISSUE-448; ISSUE-265 named
# the gap and deferred it).
RESERVED_SECONDS = ASSEMBLY_ALLOWANCE_SECONDS + JOIN_SLACK_SECONDS

# Floor for the clamp above. A proxy ceiling tighter than the assembly allowance
# would otherwise hand an agent zero or negative seconds, which is not a shorter
# review but no review at all.
MIN_AGENT_TIMEOUT_SECONDS = 30


#: Retired flags this invocation passed, named on every envelope it emits —
#: guard refusals included — so a caller learns to drop them whatever the
#: outcome. Set once per process by `main`.
_deprecated_flags: list[str] = []


def _emit(envelope: dict, code: int):
    """The facade contract: one line of JSON on stdout, then an exit code.

    The shared `emit`'s status rule is not enough on its own here: `_skip`
    exits 0 on an envelope that is deliberately not an error, so the code stays
    explicit and the status check is switched off.
    """
    envelope.setdefault("deprecated_flags", list(_deprecated_flags))
    emit(envelope, indent=None, ensure_ascii=True, exit_on_error=False)
    sys.exit(code)


def _fail(reason: str, message: str, **extra):
    """Something is wrong with the *request*, so the workflow blocks the push.

    Logged with the task id and the rejected input: a guard refusal with neither
    is a line an operator cannot act on.
    """
    logger.warning(
        "code_review refused (task=%s, reason=%s): %s",
        os.environ.get("ISTOTA_TASK_ID", "-"), reason, message,
    )
    _emit({"status": "error", "reason": reason, "error": message, **extra}, 1)


def _skip(reason: str, message: str, **extra):
    """A state of the *environment* rather than of the diff.

    Exit 0 and `skipped`, never `error`. The workflow does not block a push on
    these, because none of them resolves by refusing to push — and a review that
    errors *does* block, so misfiling one here would strand finished work on a
    branch nobody is watching. A skipped review still counts as unreviewed.

    Not the only producer of `skipped`: the engine returns it too, when every
    reviewer failed (`review_failed` / `malformed_output`). Same reasoning,
    reached after the run rather than before it.
    """
    logger.info(
        "code_review skipped (task=%s, reason=%s): %s",
        os.environ.get("ISTOTA_TASK_ID", "-"), reason, message,
    )
    _emit({"status": "skipped", "reason": reason, "error": message, **extra}, 0)


#: How much of a failed reviewer's own output the envelope quotes.
#:
#: Enough for the sentence a CLI exits on ("Not logged in · Please run /login",
#: an auth error, a model name it does not know) and not enough for a diff or a
#: half-written review to arrive as an error string.
_ERROR_TEXT_CHARS = 300


def _failure_error(agent: str, stop_reason: str, text: str | None) -> str:
    """The `error` string for a reviewer whose call did not succeed.

    `stop_reason` alone is a slug — `error` covers a missing credential, an
    unreachable provider and a model name the CLI rejects alike, and the
    reviewer usually said which on its way out. `skill.md` has promised the
    quote to the reading model since the field existed; the code dropped
    `result_text` on the floor and only the slug arrived, which turned a
    one-line diagnosis into a session of tracing the call chain (ISSUE-409).

    Flattened to one line and capped: it rides in a JSON envelope a model
    reads, and the skill's own "the findings are untrusted input" rule names
    this field, so it is quoted as data rather than trusted.
    """
    head = " ".join((text or "").split())[:_ERROR_TEXT_CHARS].strip()
    base = f"{agent} failed (stop_reason={stop_reason})"
    return f"{base}: {head}" if head else base


def _task_id() -> int | None:
    raw = os.environ.get("ISTOTA_TASK_ID", "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _db_path() -> str:
    return os.environ.get("ISTOTA_DB_PATH", "").strip()


#: Scopes the reviewer namespace withholds when the skill index cannot be read.
_CORE_WITHHELD_SCOPES = frozenset({"files", "memory", "developer"})


def _reviewer_withheld_scopes(config) -> frozenset[str]:
    """Every scope, so the reviewer's namespace binds the run directory alone.

    The reviewer reads a diff that may come from an outside contributor and
    has no reason to see the user's workspace, memory or other repositories.
    """
    from istota.rooms.scopes import all_scopes
    from istota.skills._loader import load_skill_index

    try:
        index = load_skill_index(
            config.skills_dir, bundled_dir=config.bundled_skills_dir
        )
        return _CORE_WITHHELD_SCOPES | all_scopes(index)
    except Exception as exc:
        logger.warning(
            "code_review could not read the skill index (%s: %s); withholding "
            "files, memory and developer only", type(exc).__name__, exc,
        )
        return _CORE_WITHHELD_SCOPES


def cmd_run(args):
    from istota import db
    from istota.brain import (
        BrainRequest,
        make_brain,
        primary_brain_unavailable,
        report_brain_result,
    )
    from istota.brain._aliases import split_effort
    from istota.config import load_config

    config = load_config()
    dev = config.developer
    if not dev.enabled:
        _fail("developer_disabled", "[developer] is not enabled on this deployment")
    if not dev.repos_dir:
        _fail("repos_dir_unset", "[developer] repos_dir is not configured")

    review_cfg = dev.review
    if not review_cfg.enabled:
        # An operator switch, so `skipped` and exit 0. It is a state of the
        # deployment rather than of the diff and will not resolve by refusing to
        # push; blocking here would mean a deployment that turned review off
        # could never land anything. The workflow reports the work as unreviewed
        # and says why.
        _skip(
            "review_disabled",
            "[developer.review] enabled = false, so code review is switched off "
            "on this deployment",
        )

    user_id = os.environ.get("ISTOTA_USER_ID", "")
    if not config.is_admin(user_id):
        _fail(
            "not_admin",
            "code review is admin-only; repos_dir is bound into the sandbox for "
            "admins only, so a non-admin has no worktree to review",
        )

    # The guard above reads `repos_dir` off the loaded config; containment below
    # resolves against `DEVELOPER_REPOS_DIR`, which is the *task's own subtree*
    # of it. Those can disagree: the variable comes from the developer skill's
    # `setup_env`, which does not run for a non-admin and declines a subtree it
    # cannot name safely, and `developer_repos_root` refuses a value that is not
    # this task's own. Reporting any of that through `path_not_allowed` would
    # read as "your path is wrong" and block the push; it is neither. Separate
    # reason, and skipped, because no amount of not-pushing will set it.
    if developer_repos_root() is None:
        _skip(
            "repos_root_unavailable",
            "No developer repos root resolved in this process, so no worktree "
            "path can be validated. DEVELOPER_REPOS_DIR is derived per task by "
            "the developer skill's setup_env and must name this task's own "
            "subtree (ISTOTA_USER_ID); check that both are set.",
        )

    worktree, error = resolve_under_repos(args.worktree)
    if error:
        _fail("path_not_allowed", error)

    # No text-only path on tmux at all, so there is nothing to construct. This
    # is a property of the deployment and will not change by retrying.
    if config.brain.kind == "tmux_claude":
        _skip(
            "brain_unsupported",
            "the tmux_claude brain has no text-only call path, so no reviewer "
            "can be driven on this deployment",
        )

    # Before the cap and the breaker, so an operator whose budget cannot fit
    # learns about it even on a run those short-circuit — a warning that only
    # fires on the runs that were going to work is not much of a warning.
    # The ceiling this *skill* runs under, which since ISSUE-448 is not
    # necessarily the global. Resolved through the proxy's own function rather
    # than by reading the map here, because the two answers deciding one wall
    # bound have to be the same answer: a copy of the lookup would let the
    # envelope report a budget the proxy does not honour.
    proxy_ceiling = skill_proxy.resolve_skill_timeout(
        config.security.skill_proxy_timeout,
        config.security.skill_proxy_timeouts,
        "code_review",
        config.security.skill_client_wait_seconds,
    )
    # Coerced rather than trusted: nothing in the loader validates either value,
    # and a float from the TOML would land in the envelope as a float where the
    # doc promises whole seconds.
    #
    # `--timeout` wins over the config, bounded below by the same clamp. Config
    # comes from a deploy, so without a flag there was no way to ask what a
    # reviewer needs on a real diff — and with the answer discarded on every
    # timeout, nothing on the host could measure it at all (ISSUE-448).
    requested = args.timeout if args.timeout is not None else review_cfg.timeout_seconds
    configured = int(requested)
    # Only the non-positive case is floored. A `timeout_seconds` of 0 or less
    # otherwise reaches the brains, which disagree about what it means — the
    # native one runs unbounded until the proxy kills the command, `claude_code`
    # hands it to a `threading.Timer` and kills the reviewer at once — and neither
    # is a review. A small *positive* budget is left alone: it is a choice an
    # operator can legitimately make, and raising it would mean overriding the
    # number the envelope reports in the same breath as reporting it.
    agent_timeout = configured if configured > 0 else MIN_AGENT_TIMEOUT_SECONDS
    # `> 0` rather than a truthiness test: a non-positive ceiling is a
    # misconfiguration the proxy surfaces on its own by killing the command
    # immediately, and reading it as "no ceiling" at least leaves the budget
    # saying what was configured instead of blaming a clamp that never applied.
    if proxy_ceiling > 0:
        # Clamped, not just warned about. Left alone, every agent would be given
        # a budget the proxy kills the whole command before it can spend, so
        # each review would die half-finished having paid for its calls.
        # Shrinking is the only outcome that returns anything.
        #
        # Downward only, and the floor bounds how far down rather than being
        # applied to the result. Written the other way round — as
        # `max(floor, ceiling - allowance)` over the configured value — it could
        # *raise* a small budget: 25s under an 85s ceiling became 30s, which is
        # not a clamp, and it made the fit strictly worse rather than better.
        ceiling_budget = max(
            MIN_AGENT_TIMEOUT_SECONDS, proxy_ceiling - RESERVED_SECONDS
        )
        agent_timeout = min(agent_timeout, ceiling_budget)
        if proxy_ceiling - RESERVED_SECONDS < MIN_AGENT_TIMEOUT_SECONDS:
            # The floor won, so the clamp could not deliver the fit it exists to
            # produce and the command will overrun the ceiling anyway. Worth its
            # own line: the caller gets no envelope at all in this case — the
            # proxy kills the command with empty stdout — so the log is the only
            # place the deployment can say what went wrong.
            logger.warning(
                "the %ss proxy ceiling for code_review cannot fit a review at "
                "all: %ss reserved for assembly and overrun slack, plus the "
                "%ss agent floor, needs %ss. The proxy will kill this command "
                "before it answers. Raise security.skill_proxy_timeouts."
                "code_review.",
                proxy_ceiling, RESERVED_SECONDS,
                MIN_AGENT_TIMEOUT_SECONDS,
                RESERVED_SECONDS + MIN_AGENT_TIMEOUT_SECONDS,
            )
    if agent_timeout < configured:
        logger.warning(
            "code_review timeout_seconds of %ss plus %ss reserved for assembly "
            "and overrun slack exceeds the %ss proxy ceiling for this skill, "
            "so the reviewer is being given %ss instead. Lower timeout_seconds or "
            "raise security.skill_proxy_timeouts.code_review.",
            configured, RESERVED_SECONDS,
            proxy_ceiling, agent_timeout,
        )

    task_id = _task_id()
    db_path = _db_path()
    cap = review_cfg.max_calls_per_task
    calls_used = None
    # Distinct from `calls_used is not None`: this says a budget *applies*, not
    # that reading it worked. The two come apart on a database error.
    has_task_budget = task_id is not None and bool(db_path)
    if has_task_budget:
        # A read that fails must not sink a review. Losing the budget check is a
        # cost risk bounded by whatever else is wrong with the database; refusing
        # the review outright turns a transient lock into a blocked push.
        try:
            with db.get_db(db_path) as conn:
                calls_used = db.code_review_calls_get(conn, task_id)
        except Exception as exc:
            logger.error(
                "code_review could not read the call budget for task %s, "
                "proceeding uncapped: %s", task_id, exc,
            )
        # `<= 0` means no reviews rather than "unlimited": on a spend control
        # the expensive reading is the wrong one to guess at.
        if cap <= 0:
            _skip(
                "call_cap",
                f"max_calls_per_task is {cap}, so no review rounds are permitted "
                "for this task",
                calls_used=calls_used or 0,
                max_calls=cap,
            )
        if calls_used is not None and calls_used >= cap:
            _skip(
                "call_cap",
                f"this task has already spent {calls_used} review rounds, at the "
                f"max_calls_per_task cap of {cap}",
                calls_used=calls_used,
                max_calls=cap,
            )
    else:
        # An operator-driven run rather than a task's. Both variables come from
        # the proxy, not from the model, so their absence means there is no task
        # to budget against — not that a budget was evaded.
        logger.warning(
            "code_review running without a task budget (ISTOTA_TASK_ID=%r, "
            "ISTOTA_DB_PATH set=%s)",
            os.environ.get("ISTOTA_TASK_ID", ""),
            bool(db_path),
        )

    available, breaker_reason = primary_brain_unavailable(config.brain)
    if not available:
        _skip(
            "brain_unavailable",
            f"the primary brain is degraded ({breaker_reason or 'cooling down'}), "
            "so the review was not attempted",
            calls_used=calls_used,
            max_calls=cap,
        )

    cwd = Path(config.temp_dir) if config.temp_dir else Path("/tmp")
    # Imported here rather than at module scope: `executor` imports
    # `briefings.generate`, and a top-level import from any of these callers
    # risks closing a cycle back through it.
    from istota.executor import (
        build_daemon_sandbox,
        build_model_cli_env,
        persist_brain_usage,
        release_daemon_sandbox,
    )

    # What the two builders made, for the `finally` below: the engine hands
    # `invoke` the snapshot only when the namespace was built too, so a run
    # whose namespace was refused still has a run directory to remove.
    built: dict = {"snapshot": None, "sandbox": None}

    def build_snapshot(worktree_path, bundle):
        if not config.temp_dir:
            raise engine.ReviewError(
                "no temp_dir is configured to hold the review snapshot",
                reason="snapshot_failed",
            )
        snap = review_snapshot.build_snapshot(
            worktree_path,
            bundle,
            # Resolved: the namespace binds the run directory at its resolved
            # path, and the prompt, `cwd` and `fs_read_roots` must name the
            # same one or every path the reviewer is given is missing inside.
            root=Path(config.temp_dir).resolve(),
            user_id=user_id,
            max_bytes=review_cfg.snapshot_max_bytes,
            max_file_bytes=review_cfg.snapshot_max_file_bytes,
        )
        built["snapshot"] = snap
        return snap

    def build_sandbox(snap):
        box = build_daemon_sandbox(
            config,
            user_id,
            extra_ro_binds=[snap.run_dir],
            withheld_scopes=_reviewer_withheld_scopes(config),
        )
        built["sandbox"] = box
        return box

    agent = "reviewer"

    def invoke(prompt: str, timeout: int, *, tools: bool, snapshot=None, sandbox=None):
        raw_model = review_cfg.model
        # Split here, not in the brain. `resolve_model_name` strips a `:effort`
        # tail and keeps only the base, so a configured "smart:high" handed to
        # it whole runs at default effort and silently drops the operator's
        # setting.
        base_model, effort = split_effort(raw_model)
        brain = make_brain(config.brain)
        model = brain.resolve_model_name(base_model)

        # Two requests built in two places rather than one with conditional
        # keywords: the grant and its confinement travel together in each, and
        # `tests/test_brain_request_confinement.py` reads them off the AST.
        #
        # `env` is not `dict(os.environ)` (ISSUE-395). What that carried
        # depends on how this CLI was started: spawned by the skill proxy it
        # holds the manifest-injected provider key rather than the daemon's own
        # credentials; run host-side directly, `os.environ` *is* the daemon
        # environment, master Fernet key and all.
        #
        # Streamed, though nothing here consumes the stream, because the two
        # paths differ in what survives a *timeout* (ISSUE-448): non-streaming,
        # a timed-out call comes back with `usage=None` and is never billed,
        # while the streaming path stamps usage from the frames it has parsed
        # and returns `partial_text`. Token totals still come only from the
        # terminal frame, which a timed-out run never emits.
        if tools:
            # The engine passes both only when the namespace was built. A
            # `None` wrap here means the deployment confines no task
            # (`sandbox_enabled = false`); `fs_read_roots` still bounds the
            # native brain, and the read-only grant keeps the Claude CLI to
            # Read, Grep and Glob.
            sandbox_wrap = sandbox.wrap
            req = BrainRequest(
                prompt=prompt,
                allowed_tools=["Read", "Grep", "Glob"],
                cwd=snapshot.tree_dir,
                env=build_model_cli_env(config),
                fs_read_roots=[snapshot.run_dir],
                sandbox_wrap=sandbox_wrap,
                timeout_seconds=timeout,
                model=model,
                effort=effort or "",
                streaming=True,
                on_progress=None,
                cancel_check=None,
                on_pid=None,
                result_file=None,
            )
        else:
            req = BrainRequest(
                prompt=prompt,
                allowed_tools=[],
                cwd=cwd,
                env=build_model_cli_env(config),
                sandbox_wrap=None,
                timeout_seconds=timeout,
                model=model,
                effort=effort or "",
                streaming=True,
                on_progress=None,
                cancel_check=None,
                on_pid=None,
                result_file=None,
            )
        primary_started_at = time.time()
        primary_started_monotonic = time.monotonic()
        result = brain.execute(req)

        # One row per model call, with no task row behind it. A run is up to
        # two invocations (the review and its reformat), so this is real spend.
        persist_brain_usage(
            config, None, usage=result.usage, origin="code_review",
            user_id=user_id, brain_kind=result.brain_kind,
            model=result.model_used or req.model,
            stop_reason=result.stop_reason, success=result.success,
        )

        report_brain_result(
            result, config.brain, config=config, started_at=primary_started_at,
            started_monotonic=primary_started_monotonic,
        )
        if not result.success:
            logger.error(
                "code_review %s failed (stop_reason=%s)", agent, result.stop_reason
            )
            if result.partial_text:
                # Into the log rather than the envelope. A reviewer answers in
                # one JSON blob at the end, so what a timed-out one has written
                # is prose about the diff — worth having when working out what
                # the budget should be, and not something to paste into a field
                # the reading model treats as a finding.
                #
                # The **tail**, where `_failure_error` above takes the head, and
                # the two differ because the text does: an error message says
                # what went wrong at its start, while a truncated answer is
                # nearest to being finished at its end. Marked as the reviewer's
                # own words rather than run on into the line, since the diff can
                # be an outside contributor's and this is the daemon journal.
                logger.info(
                    "code_review %s wrote %d characters before it stopped; "
                    "last %d, reviewer's own text: %s",
                    agent, len(result.partial_text), _ERROR_TEXT_CHARS,
                    " ".join(result.partial_text.split())[-_ERROR_TEXT_CHARS:],
                )
            return engine.ReviewerReply(
                ok=False,
                error=_failure_error(agent, result.stop_reason, result.result_text),
                model=result.model_used or req.model,
            )
        return engine.ReviewerReply(
            ok=True, text=result.result_text or "", model=result.model_used or req.model
        )

    try:
        envelope = engine.run_review(
            worktree,
            intent=args.intent or "",
            base=args.base,
            explicit_range=getattr(args, "range", None),
            cfg=engine.ReviewConfig(
                max_diff_chars=review_cfg.max_diff_chars,
                file_budget=review_cfg.file_budget,
                snapshot_max_bytes=review_cfg.snapshot_max_bytes,
                snapshot_max_file_bytes=review_cfg.snapshot_max_file_bytes,
            ),
            invoke=invoke,
            timeout_seconds=agent_timeout,
            build_snapshot=build_snapshot,
            build_sandbox=build_sandbox,
        )
    except engine.ReviewError as exc:
        _fail(exc.reason, str(exc))
    finally:
        # Every path out of the run, `_fail`'s `SystemExit` and a raise from
        # the model call included. Neither helper raises.
        review_snapshot.remove_snapshot(built["snapshot"])
        if built["sandbox"] is not None:
            release_daemon_sandbox(built["sandbox"])

    rounds = envelope.pop("rounds", 0)
    if rounds and task_id is not None and db_path:
        # The review is already paid for by this point, so a failure to record
        # the charge must not lose it. Emitting an un-counted review is a cost
        # risk; a traceback instead of an envelope violates the facade contract
        # and hands the caller nothing at all.
        try:
            with db.get_db(db_path) as conn:
                calls_used = db.code_review_calls_increment(conn, task_id, rounds)
        except Exception as exc:
            logger.error(
                "code_review completed but could not record the call against "
                "task %s: %s", task_id, exc,
            )
    envelope["calls_used"] = calls_used
    envelope["max_calls"] = cap
    # `agent_timeout_seconds` comes back from the engine, which was handed the
    # already-clamped value. These two are what make it readable: without the
    # configured number there is nothing to compare it against, and the clamp's
    # own warning goes to the daemon journal, which the model that invoked this
    # CLI has no route to. Derived from the comparison rather than from a flag
    # set at the clamp, and `<` rather than `!=`, so that the two cases where
    # the clamp runs without costing anything — a budget already at the floor,
    # and one the floor raised — report honestly. The question a caller is
    # asking is "did this review run short", not "was the branch taken".
    #
    # `agent_timeout_configured` is always the *deployment's* number, never the
    # `--timeout` override, and the override rides beside it. The flag reaches
    # this CLI from the model's own argv, and it shortens as easily as it
    # lengthens — a reviewer given 30 seconds comes back clean quickly, and
    # reporting 30 as "configured" would leave nothing in the envelope saying
    # the deployment asked for 480. `agent_timeout_override` is `None` on a run
    # that passed no flag, so a caller can tell "not overridden" from
    # "overridden to the configured value".
    envelope["agent_timeout_configured"] = int(review_cfg.timeout_seconds)
    envelope["agent_timeout_override"] = (
        configured if args.timeout is not None else None
    )
    envelope["agent_timeout_clamped"] = agent_timeout < configured
    if envelope["status"] != "ok":
        # The guard refusals above each log through `_fail` / `_skip`; a run that
        # got as far as calling models and came back with nothing had no line at
        # any level: `invoke` logs only a call that failed, and a reviewer
        # answering unparseably is `success=True`.
        # That silence is the expensive part. A broken adapter makes *every*
        # review on the deployment come back this way (ISSUE-271), and since
        # this status no longer blocks the push, nothing else would show it: the
        # branch lands unreviewed, the breaker sees a healthy call, and a
        # scheduled review exits 0 and never trips the auto-disable counter.
        # WARNING because one of these is a bad day and a run of them is an
        # outage, and the reason slug is what tells them apart.
        logger.warning(
            "code_review returned no findings (task=%s, status=%s, reason=%s, "
            "rounds=%s): %s",
            os.environ.get("ISTOTA_TASK_ID", "-"), envelope["status"],
            envelope.get("reason", "-"), rounds,
            str(envelope.get("error", ""))[:200],
        )
    # Kept on the envelope rather than popped with the charge: it is what
    # separates a skip that spent model calls from one that refused before
    # spending any, and those two read identically otherwise.
    envelope["rounds"] = rounds
    _emit(envelope, 1 if envelope["status"] == "error" else 0)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.code_review",
        description="Review a branch diff with one reviewer over a read-only snapshot",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Review the changes in a worktree")
    host_path(
        p_run, "--worktree", mode=REPO,
        note=(
            "the other allowlist: `resolve_under_repos` against this task's "
            "own subtree of DEVELOPER_REPOS_DIR, applied by `cmd_run`. The "
            "mount roots are the wrong tool for it, and a worktree is refused "
            "for reasons `cmd_run` reports separately from a bad path"
        ),
        required=True,
        help="Path to the worktree to review. Must resolve inside $DEVELOPER_REPOS_DIR",
    )
    p_run.add_argument(
        "--base",
        help="Review <base>...HEAD. Three-dot: a two-dot range inverts every "
             "base-only commit once the base moves ahead of the branch point",
    )
    p_run.add_argument(
        "--range",
        help="An explicit range, which wins over --base. Defaults to the merge "
             "base against the tracked default branch",
    )
    p_run.add_argument(
        "--intent",
        default="",
        help="One line on what the change is meant to do, shown to the reviewers",
    )
    p_run.add_argument(
        "--agents",
        help="Deprecated and ignored: one reviewer always runs. Reported in "
             "the envelope's deprecated_flags; removed in the next release",
    )
    p_run.add_argument(
        "--timeout",
        type=int,
        help="The reviewer's wall-clock budget in seconds, overriding "
             "[developer.review] timeout_seconds. Still clamped to fit under "
             "the skill proxy's ceiling for this skill",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parse_and_resolve(parser, argv)
    global _deprecated_flags
    # Accepted for one release so workflow files that still pass it keep
    # working, and named back so the caller can drop it.
    _deprecated_flags = ["--agents"] if getattr(args, "agents", None) is not None else []
    commands = {"run": cmd_run}

    def describe(exc: BaseException) -> dict:
        # The facade contract is one line of JSON and an exit code, and the
        # scheduler sniffs stdout for that shape. The engine shells out to git
        # through `subprocess.Popen`, which raises `OSError` and friends outside
        # `ReviewError`, so without this the caller gets a traceback on stderr,
        # empty stdout, and nothing it can classify.
        #
        # `_fail` emits and exits rather than returning, which is what keeps
        # this module's `reason` discriminator and its refusal log line; the
        # epilogue's own envelope below is therefore unreachable. `_emit` is
        # also how a *successful* run returns, and its `SystemExit` is not an
        # `Exception`, so it passes the epilogue untouched with no re-raise
        # clause of its own.
        logger.exception("code_review failed unexpectedly")
        _fail("internal_error", f"{type(exc).__name__}: {exc}")
        raise AssertionError("unreachable")  # pragma: no cover

    run_skill_cli(commands, args, on_exception=describe)


if __name__ == "__main__":
    main()
