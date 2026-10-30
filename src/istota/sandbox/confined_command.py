"""A shell command from a model-writable file, run inside a task's sandbox.

CRON.md and HEARTBEAT.md live in the user's workspace and the model writes
them, so a `command:` job or a `shell-command` check has the authority of
whatever last edited the file, an injected task included (ISSUE-886). It gets
what the task's own Bash call gets: the namespace, the credential split, both
proxies and the cgroup. A job that needs the host belongs in a system timer,
outside anything the model can write.

Built on `task_env.build_task_runtime` rather than beside it. No skill is
selected, so a job has the reach of a turn that selected none: the proxy runs any
CLI, and a skill is authorized for its hosts and binds only where the user has
its credentials. Selecting every skill would buy an edit to CRON.md the devbox
socket and hosts that a turn of the same user does not have.
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from .network_proxy import BRIDGE_PORT
from .peer_process import reporting_pid
from .process_group import run_capture
from .shell_exec import shell_argv

if TYPE_CHECKING:
    from istota import db
    from istota.config import Config


# The wrapper starts the bridge and execs without waiting, which suits the tool
# server, whose first connection comes far later. Here the user's command is the
# first thing to run, and `curl` or `git` do not retry a refused connection.
_WAIT_FOR_BRIDGE = (
    f"for _ in {{1..40}}; do (exec 3<>/dev/tcp/127.0.0.1/{BRIDGE_PORT}) 2>/dev/null"
    " && break; sleep 0.05; done\n"
)


def run_confined_command(
    config: Config,
    task: db.Task,
    command: str,
    *,
    timeout: float,
    control_dir: Path,
    discovered_calendars: list | None = None,
    defer_ops: bool = True,
) -> subprocess.CompletedProcess:
    """Run ``command`` for ``task`` under the sandbox and return what it did.

    ``control_dir`` is the caller's: a task row has a stable one, a heartbeat
    check has none and removes what it made. Raises ``TimeoutExpired`` as
    ``run_capture`` does, and whatever building the runtime raises. A caller
    treats either as a failed run: nothing here falls back to running the
    command without the namespace.

    ``defer_ops`` false is for a caller nothing drains: a skill CLI writes
    directly from the proxy, rather than answering ok for a file nobody applies.
    """
    from istota import db as _db
    from istota.executor import (
        SandboxProfile,
        _record_worker_pid,
        _release_task_cgroup,
        _resolve_task_groups,
        _task_withheld_scopes,
        build_bwrap_cmd,
        task_temp_dir,
    )
    from istota.sandbox import cgroup as task_cgroup
    from istota.sandbox import task_env
    from istota.skills._loader import load_skill_index
    from istota.workspace.places import user_resources_for

    skill_index = load_skill_index(
        config.skills_dir, bundled_dir=config.bundled_skills_dir,
    )
    withheld = _task_withheld_scopes(config, None, task, skill_index)
    temp_dir = task_temp_dir(config, task, restricted=bool(withheld))
    # Bound read-write under this name, so a planted link would decide what the
    # namespace sees. Checked before the mkdir, as `execute_task` does.
    if temp_dir.is_symlink():
        raise OSError(f"task temp directory {temp_dir} is a symlink")
    temp_dir.mkdir(parents=True, exist_ok=True)

    with _db.get_db_if_present(config.db_path) as conn:
        user_resources = user_resources_for(config, conn, task.user_id)
    group_ids = _resolve_task_groups(config, task, None)
    is_admin = config.is_admin(task.user_id)

    runtime = task_env.build_task_runtime(
        config,
        task,
        user_temp_dir=temp_dir,
        control_dir=control_dir,
        task_attempt=task.attempt_count + 1,
        selected_skills=[],
        skill_index=skill_index,
        is_admin=is_admin,
        user_resources=user_resources,
        user_config=config.get_user(task.user_id),
        discovered_calendars=discovered_calendars,
        withheld_scopes=withheld,
        group_ids=group_ids,
    )

    # An unattended shell, so a `git commit` from it refuses when a scan cannot run.
    runtime.env["PRECOMMIT_SCANS_REQUIRED"] = "1"
    if not defer_ops:
        runtime.env.pop("ISTOTA_DEFERRED_DIR", None)
        if runtime.proxy_ctx is not None:
            runtime.proxy_ctx.base_env.pop("ISTOTA_DEFERRED_DIR", None)

    bridged = config.security.sandbox_enabled and runtime.net_proxy_sock is not None
    argv = shell_argv(_WAIT_FOR_BRIDGE + command if bridged else command)

    cgroup_path = None

    def on_pid(pid: int) -> None:
        _record_worker_pid(
            config, task.id, pid,
            proxies=(runtime.proxy_ctx, runtime.net_proxy_ctx),
            task_cg=cgroup_path,
        )

    with contextlib.ExitStack() as stack:
        if config.scheduler.task_cgroup_enabled:
            cgroup_path = task_cgroup.create(
                task.id,
                task_cgroup.CgroupLimits(
                    memory_max_mb=config.scheduler.task_memory_max_mb,
                    pids_max=config.scheduler.task_pids_max,
                    cpu_max_percent=config.scheduler.task_cpu_max_percent,
                ),
                attempt=task.attempt_count,
            )
            if cgroup_path is not None:
                stack.callback(_release_task_cgroup, task.id, cgroup_path)
        if runtime.proxy_ctx is not None:
            stack.enter_context(runtime.proxy_ctx)
        if runtime.net_proxy_ctx is not None:
            stack.enter_context(runtime.net_proxy_ctx)
        # After both proxies are up: the plan binds a socket only if it exists.
        if config.security.sandbox_enabled:
            argv = build_bwrap_cmd(
                argv, config, task, is_admin, user_resources, temp_dir,
                proxy_sock=runtime.proxy_sock,
                net_proxy_sock=runtime.net_proxy_sock,
                extra_ro_binds=runtime.extra_ro_binds,
                authorized_skills=runtime.authorized_skills,
                profile=SandboxProfile.NATIVE,
                withheld_scopes=withheld,
                group_ids=group_ids,
                sandbox_env=runtime.sandbox_env,
            )
        with task_cgroup.placement(cgroup_path) as place, \
                reporting_pid(on_pid, place, report_reap=True) as preexec:
            return run_capture(
                argv, timeout=timeout, cwd=str(temp_dir), env=runtime.env,
                preexec_fn=preexec,
            )
