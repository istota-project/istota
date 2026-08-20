"""Code review skill CLI.

`istota-skill code_review run --worktree <path> [--base <ref>] [--range <r>]
[--intent <text>] [--agents both]` assembles a review of a branch diff and runs
it past one or two text-only reviewers through the configured brain.

Where this runs matters more than what it does. The skill proxy spawns the
module *outside* the sandbox with the daemon's filesystem view, so `load_config`,
`make_brain` and the worktree are all reachable here and none of them is
reachable from the model. Everything the reviewers see is assembled by
`engine.py` from the repository; the caller supplies a path, a range and a line
of intent, and nothing else. A model-authored prompt never becomes a
daemon-side read.

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
import json
import logging
import os
import sys
from pathlib import Path

from istota.skill_host_paths import developer_repos_root, resolve_under_repos

from . import engine

logger = logging.getLogger(__name__)

# Context assembly, prompt building and merging happen outside any agent's
# timeout, so the command's own wall time is `timeout_seconds` plus this. The
# proxy kills the command at `security.skill_proxy_timeout`, and an operator who
# raises `timeout_seconds` past that ceiling should learn about it from a
# startup warning rather than from a review that dies half-finished.
ASSEMBLY_ALLOWANCE_SECONDS = 60

# Floor for the clamp above. A proxy ceiling tighter than the assembly allowance
# would otherwise hand an agent zero or negative seconds, which is not a shorter
# review but no review at all.
MIN_AGENT_TIMEOUT_SECONDS = 30


def _emit(envelope: dict, code: int):
    """The facade contract: one line of JSON on stdout, then an exit code."""
    print(json.dumps(envelope))
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
    """
    logger.info(
        "code_review skipped (task=%s, reason=%s): %s",
        os.environ.get("ISTOTA_TASK_ID", "-"), reason, message,
    )
    _emit({"status": "skipped", "reason": reason, "error": message, **extra}, 0)


def _task_id() -> int | None:
    raw = os.environ.get("ISTOTA_TASK_ID", "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _db_path() -> str:
    return os.environ.get("ISTOTA_DB_PATH", "").strip()


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
    # resolves against `DEVELOPER_REPOS_DIR`. Those can disagree — the variable
    # is injected only for *authorized* skills, so a claude_code deployment with
    # `[developer]` configured but no forge token has the config key and not the
    # variable. Reported through `path_not_allowed` that reads as "your path is
    # wrong" and blocks the push; it is neither. Separate reason, and skipped,
    # because no amount of not-pushing will set the variable.
    if developer_repos_root() is None:
        _skip(
            "repos_root_unavailable",
            "DEVELOPER_REPOS_DIR is not set in this process, so no worktree path "
            "can be validated. The variable is injected for authorized skills "
            "only — check that code_review resolved its credentials.",
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
    proxy_ceiling = config.security.skill_proxy_timeout
    agent_timeout = review_cfg.timeout_seconds
    if proxy_ceiling and agent_timeout + ASSEMBLY_ALLOWANCE_SECONDS > proxy_ceiling:
        # Clamped, not just warned about. Left alone, every agent would be given
        # a budget the proxy kills the whole command before it can spend, so
        # each review would die half-finished having paid for both agents.
        # Shrinking is the only outcome that returns anything.
        agent_timeout = max(
            MIN_AGENT_TIMEOUT_SECONDS, proxy_ceiling - ASSEMBLY_ALLOWANCE_SECONDS
        )
        logger.warning(
            "code_review timeout_seconds of %ss plus %ss of assembly exceeds "
            "security.skill_proxy_timeout of %ss, so each agent is being given "
            "%ss instead. Lower timeout_seconds or raise skill_proxy_timeout.",
            review_cfg.timeout_seconds, ASSEMBLY_ALLOWANCE_SECONDS,
            proxy_ceiling, agent_timeout,
        )

    task_id = _task_id()
    db_path = _db_path()
    cap = review_cfg.max_calls_per_task
    calls_used = None
    if task_id is not None and db_path:
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
        # `<= 0` means no reviews, matching `max_need_files = 0` next door rather
        # than reading as "unlimited". Two adjacent knobs where 0 means opposite
        # things is a trap, and on a spend control the expensive reading is the
        # wrong one to guess at.
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

    def invoke(agent: str, prompt: str, timeout: int):
        raw_model = (
            review_cfg.conformance_model
            if agent == engine.CONFORMANCE
            else review_cfg.bughunt_model
        )
        # Split here, not in the brain. `resolve_model_name` strips a `:effort`
        # tail and keeps only the base, so a configured "smart:high" handed to
        # it whole runs at default effort and silently drops the operator's
        # setting.
        base_model, effort = split_effort(raw_model)
        brain = make_brain(config.brain)
        req = BrainRequest(
            prompt=prompt,
            allowed_tools=[],
            cwd=cwd,
            env=dict(os.environ),
            timeout_seconds=timeout,
            model=brain.resolve_model_name(base_model),
            effort=effort or "",
            streaming=False,
            on_progress=None,
            cancel_check=None,
            on_pid=None,
            sandbox_wrap=None,
            result_file=None,
        )
        result = brain.execute(req)
        report_brain_result(result, config.brain)
        if not result.success:
            logger.error(
                "code_review %s failed (stop_reason=%s)", agent, result.stop_reason
            )
            return engine.AgentReply(
                ok=False, error=f"{agent} failed (stop_reason={result.stop_reason})"
            )
        return engine.AgentReply(ok=True, text=result.result_text or "")

    try:
        envelope = engine.run_review(
            worktree,
            intent=args.intent or "",
            base=args.base,
            explicit_range=getattr(args, "range", None),
            forced_agents=args.agents,
            cfg=engine.ReviewConfig(
                both_agents_threshold_lines=review_cfg.both_agents_threshold_lines,
                boundary_patterns=tuple(review_cfg.boundary_patterns),
                max_diff_chars=review_cfg.max_diff_chars,
                max_context_chars=review_cfg.max_context_chars,
                max_file_chars=review_cfg.max_file_chars,
                max_callers_per_symbol=review_cfg.max_callers_per_symbol,
            ),
            invoke=invoke,
            timeout_seconds=agent_timeout,
        )
    except engine.ReviewError as exc:
        _fail(exc.reason, str(exc))

    rounds = envelope.pop("rounds", 0)
    if rounds and task_id is not None and db_path:
        # The review is already paid for by this point, so a failure to record
        # the charge must not lose it. Emitting an un-counted review is a cost
        # risk; a traceback instead of an envelope violates the facade contract
        # and hands the caller nothing at all.
        try:
            with db.get_db(db_path) as conn:
                calls_used = db.code_review_calls_increment(conn, task_id)
        except Exception as exc:
            logger.error(
                "code_review completed but could not record the call against "
                "task %s: %s", task_id, exc,
            )
    envelope["calls_used"] = calls_used
    envelope["max_calls"] = cap
    _emit(envelope, 1 if envelope["status"] == "error" else 0)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.code_review",
        description="Review a branch diff with one or two text-only reviewers",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Review the changes in a worktree")
    p_run.add_argument(
        "--worktree",
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
        choices=["both", "conformance", "bughunt"],
        help="Force the reviewer set. Default sizes it from the diff",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    commands = {"run": cmd_run}
    try:
        commands[args.command](args)
    except SystemExit:
        # `_emit` is how this module returns; it is not a failure to catch.
        raise
    except Exception as exc:
        # The facade contract is one line of JSON and an exit code, and the
        # scheduler sniffs stdout for that shape. The engine shells out to git
        # through `subprocess.Popen`, which raises `OSError` and friends outside
        # `ReviewError`, so without this the caller gets a traceback on stderr,
        # empty stdout, and nothing it can classify.
        logger.exception("code_review failed unexpectedly")
        _fail("internal_error", f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
