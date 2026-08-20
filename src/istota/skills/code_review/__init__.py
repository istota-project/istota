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

from istota.skill_host_paths import resolve_under_repos

from . import engine

logger = logging.getLogger(__name__)

# Context assembly, prompt building and merging happen outside any agent's
# timeout, so the command's own wall time is `timeout_seconds` plus this. The
# proxy kills the command at `security.skill_proxy_timeout`, and an operator who
# raises `timeout_seconds` past that ceiling should learn about it from a
# startup warning rather than from a review that dies half-finished.
ASSEMBLY_ALLOWANCE_SECONDS = 60


def _emit(envelope: dict, code: int):
    """The facade contract: one line of JSON on stdout, then an exit code."""
    print(json.dumps(envelope))
    sys.exit(code)


def _fail(reason: str, message: str):
    logger.warning("code_review refused (%s): %s", reason, message)
    _emit({"status": "error", "reason": reason, "error": message}, 1)


def _skip(reason: str, message: str, **extra):
    """A state of the environment rather than of the diff.

    Exit 0 and `skipped`, never `error`. The workflow does not block a push on
    these, because none of them resolves by refusing to push — and a review that
    errors *does* block, so misfiling one here would strand finished work on a
    branch nobody is watching. A skipped review still counts as unreviewed.
    """
    logger.info("code_review skipped (%s): %s", reason, message)
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
        _fail(
            "review_disabled",
            "[developer.review] enabled = false, so code review is switched off",
        )

    user_id = os.environ.get("ISTOTA_USER_ID", "")
    if not config.is_admin(user_id):
        _fail(
            "not_admin",
            "code review is admin-only; repos_dir is bound into the sandbox for "
            "admins only, so a non-admin has no worktree to review",
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

    task_id = _task_id()
    db_path = _db_path()
    cap = review_cfg.max_calls_per_task
    calls_used = None
    if task_id is not None and db_path:
        with db.get_db(db_path) as conn:
            calls_used = db.code_review_calls_get(conn, task_id)
        if cap > 0 and calls_used >= cap:
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
        )

    budget = review_cfg.timeout_seconds + ASSEMBLY_ALLOWANCE_SECONDS
    proxy_ceiling = config.security.skill_proxy_timeout
    if proxy_ceiling and budget > proxy_ceiling:
        logger.warning(
            "code_review budget of %ss (timeout_seconds %s + assembly %ss) exceeds "
            "security.skill_proxy_timeout of %ss — the proxy will kill the review "
            "before it finishes. Lower timeout_seconds or raise skill_proxy_timeout.",
            budget, review_cfg.timeout_seconds, ASSEMBLY_ALLOWANCE_SECONDS, proxy_ceiling,
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
            timeout_seconds=review_cfg.timeout_seconds,
        )
    except engine.ReviewError as exc:
        _fail(exc.reason, str(exc))

    rounds = envelope.pop("rounds", 0)
    if rounds and task_id is not None and db_path:
        with db.get_db(db_path) as conn:
            calls_used = db.code_review_calls_increment(conn, task_id)
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
    commands[args.command](args)


if __name__ == "__main__":
    main()
