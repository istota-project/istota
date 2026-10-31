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
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from .network_proxy import BRIDGE_PORT
from .peer_process import reporting_pid
from .process_group import run_capture
from .shell_exec import on_daemon_path, shell_argv

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


#: `egress_hosts`' default: what the task's own reach says. None means open egress.
FROM_TASK_REACH = object()


def run_confined_command(
    config: Config,
    task: db.Task,
    command: str,
    *,
    timeout: float,
    control_dir: Path,
    discovered_calendars: list | None = None,
    defer_ops: bool = True,
    egress_hosts: "frozenset[str] | None | object" = FROM_TASK_REACH,
) -> subprocess.CompletedProcess:
    """Run ``command`` for ``task`` under the sandbox and return what it did.

    ``control_dir`` is the caller's: a task row has a stable one, a heartbeat
    check has none and removes what it made. Raises ``TimeoutExpired`` as
    ``run_capture`` does, and whatever building the runtime raises. A caller
    treats either as a failed run: nothing here falls back to running the
    command without the namespace.

    ``defer_ops`` false is for a caller nothing drains: a skill CLI writes
    directly from the proxy, rather than answering ok for a file nobody applies.

    ``egress_hosts`` is where the command may reach. A cron job leaves it and takes
    its task's reach, and what it was refused is filed when it ends, however it
    ends. A heartbeat check has no task row and no subject to grant against, so it
    states its limit itself and records nothing.
    """
    from istota import db as _db
    from istota.executor import (
        SandboxProfile,
        _record_worker_pid,
        _release_task_cgroup,
        _resolve_task_groups,
        _task_reach,
        build_bwrap_cmd,
        public_posture_refusal,
        record_egress_posture,
        record_egress_refusals,
        task_temp_dir,
    )
    from istota.lib.egress_log import REFUSED_LOG_NAME
    from istota.sandbox import cgroup as task_cgroup
    from istota.sandbox import task_env
    from istota.skills._loader import load_skill_index
    from istota.workspace.places import user_resources_for

    skill_index = load_skill_index(
        config.skills_dir, bundled_dir=config.bundled_skills_dir,
    )
    reach = _task_reach(config, None, task, skill_index)
    withheld = reach.withheld
    if reach.public:
        refusal = public_posture_refusal(config)
        if refusal is not None:
            raise RuntimeError(refusal)
        discovered_calendars = []
    refusal = record_egress_posture(config, None, task, reach)
    if refusal is not None:
        raise RuntimeError(refusal)
    from_reach = egress_hosts is FROM_TASK_REACH
    limit = reach.egress_hosts if from_reach else egress_hosts
    egress_log = control_dir / REFUSED_LOG_NAME if from_reach else None
    temp_dir = task_temp_dir(config, task, restricted=reach.restricted)
    # Bound read-write under this name, so a planted link would decide what the
    # namespace sees. Checked before the mkdir, as `execute_task` does.
    if temp_dir.is_symlink():
        raise OSError(f"task temp directory {temp_dir} is a symlink")
    temp_dir.mkdir(parents=True, exist_ok=True)

    user_resources: list = []
    group_ids: list[str] = []
    if not reach.public:
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
        restricted=reach.restricted,
        isolated_browser=reach.isolated_browser,
        group_ids=group_ids,
        egress_hosts=limit,
        egress_withheld=reach.egress_withheld,
        egress_log=egress_log,
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

    try:
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
                        runtime.proxy_ctx.task_cgroup = cgroup_path
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
            # `runtime.env`'s PATH starts in directories the model can write, and
            # `subprocess` would search it for a bare name.
            if not os.path.isabs(argv[0]):
                found = on_daemon_path(argv[0])
                if found is None:
                    raise FileNotFoundError(f"{argv[0]!r} is not on the daemon's PATH")
                argv = [found, *argv[1:]]
            with task_cgroup.placement(cgroup_path) as place, \
                    reporting_pid(on_pid, place, report_reap=True) as preexec:
                return run_capture(
                    argv, timeout=timeout, cwd=str(temp_dir), env=runtime.env,
                    preexec_fn=preexec,
                    max_output_bytes=config.security.skill_proxy_max_output_mb * 1024 * 1024,
                )
    finally:
        # Outside the stack, so the proxies have stopped writing to it.
        if egress_log is not None:
            record_egress_refusals(config, task, egress_log)
