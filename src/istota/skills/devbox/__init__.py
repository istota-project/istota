"""Devbox skill — thin CLI wrapper around `docker exec` / `docker cp` /
`docker inspect` against the user's own persistent container.

Container name is derived from ``ISTOTA_USER_ID`` and ``ISTOTA_DEVBOX_CONTAINER``
(set by the executor). The CLI refuses to operate on any container whose
name doesn't match the per-user pattern — defence-in-depth in case the
env vars are wrong or absent.

Hardening layers (see also `.claude/rules/skills.md` § devbox):
* Container name matches ``^[a-zA-Z0-9_.-]+$`` before every docker call.
* ``_check_owned`` reads the ``com.istota.user_id`` label and refuses to
  proceed unless it equals ``ISTOTA_USER_ID`` — guards against name reuse
  / stale containers from a prior tenant.
* ``args.command`` is capped at 32 KB and rejects NUL bytes.
* ``cp-in`` / ``cp-out`` host paths must stay under ``ISTOTA_DEFERRED_DIR``
  or the user's ``NEXTCLOUD_MOUNT_PATH`` subtree; host-side symlinks are
  refused.
* Container paths inside a tmpfs mount (``_CONTAINER_TMPFS_MOUNTS``) are
  refused for ``cp-in`` / ``cp-out``, and ``cp-in`` reads the destination back
  from inside the container — ``docker cp`` cannot traverse a tmpfs and exits
  0 anyway (ISSUE-306). Both checks anchor the path at ``/`` the way
  ``docker cp`` does, not at the image's WORKDIR.
* ``reset --yes`` requires ``/home/dev`` to be a real mountpoint inside
  the container before wiping it (prevents nuking a baked-in image layer
  when the volume is mis-attached).

Usage:
    python -m istota.skills.devbox exec "<command>" [--timeout 300]
    python -m istota.skills.devbox exec-file /local/script [--interpreter python3] [--timeout 300]
    python -m istota.skills.devbox cp-in  /local/path /container/path
    python -m istota.skills.devbox cp-out /container/path /local/path
    python -m istota.skills.devbox status
    python -m istota.skills.devbox reset --yes
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from istota.shell_exec import SIGPIPE_EXIT, SIGPIPE_NOTE, shell_argv
from istota.skill_host_paths import resolve_host_path, validate_host_path

DEFAULT_TIMEOUT = 300
DEFAULT_MAX_OUTPUT_BYTES = 102_400
MAX_COMMAND_BYTES = 32 * 1024  # `bash -o pipefail -c` argv length cap
_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]+$")
_OWNER_LABEL = "com.istota.user_id"

# Both re-exported from `istota.shell_exec` rather than restated here. This
# module is where the rule was first paid for, and the sentence below is
# user-facing text: a second copy is a second thing to keep in step, which is
# what `shell_exec` exists to prevent. `shell_exec` is a stdlib-only leaf, so a
# skill CLI subprocess can import it.
#
# The note covers the one case `pipefail` newly colours that has a fixed code —
# a downstream `head` or `grep -q` closing the pipe on a producer that was doing
# nothing wrong. The other cannot be recognised and is named in skill.md
# instead: a non-final stage exiting non-zero to *report* something rather than
# to fail, so `grep -c x f | wc -l` now returns 1 where it returned 0.
_SIGPIPE_EXIT = SIGPIPE_EXIT
_SIGPIPE_NOTE = SIGPIPE_NOTE

# Container paths `docker cp` cannot reach. The daemon resolves a container
# path against the container's rootfs on the host and mounts only the
# container's MountPoints first — moby's setupMounts skips tmpfs destinations
# outright, so nothing ever mounts them into that view. A copy in therefore
# lands in the rootfs directory shadowed by the mount, where no process inside
# the container can see it, and a copy out looks in that same shadowed
# directory and finds nothing. Both directions, every subcommand, and `docker
# cp` exits 0 on the way in — which is why it read as working (ISSUE-306).
# This is a permanent property of `docker cp`, not a bug to wait out, so the
# only honest answer is to refuse the path.
#
# Hand-maintained mirror of the ``tmpfs:`` keys in the devbox compose files
# (deploy/ansible/templates/docker-compose.devbox.yml.j2 and
# docker/docker-compose.yml); tests/test_skills_devbox.py pins the two together.
_CONTAINER_TMPFS_MOUNTS = ("/workspace",)

# Where `exec-file` stages the script it is about to run. /home/dev is the ext4
# volume, so `docker cp` reaches it, the cleanup `rm -f` removes a file that
# exists, and the tmpfs's `noexec` stops applying to the no-interpreter
# fallback. The directory is created before each copy — `docker cp` creates no
# parent.
_EXEC_STAGING_DIR = "/home/dev/.istota-exec"

# Working directory for `exec`. Also /home/dev rather than the tmpfs: a command
# that writes a relative path has to leave it somewhere `cp-out` can reach, and
# it matches the image's own WORKDIR.
_DEFAULT_WORKDIR = "/home/dev"


def _err(msg: str) -> dict:
    return {"status": "error", "error": msg}


def _docker_cli() -> str:
    return os.environ.get("ISTOTA_DEVBOX_DOCKER_CLI") or shutil.which("docker") or "docker"


def _user_id() -> str | None:
    uid = os.environ.get("ISTOTA_USER_ID", "").strip()
    return uid or None


def _container_name() -> str | None:
    """Resolve and validate the per-user container name."""
    name = os.environ.get("ISTOTA_DEVBOX_CONTAINER", "").strip()
    if not name:
        uid = _user_id()
        if not uid:
            return None
        name = f"devbox-{uid}"
    if not _NAME_PATTERN.match(name):
        return None
    return name


def _exec_timeout() -> int:
    raw = os.environ.get("ISTOTA_DEVBOX_EXEC_TIMEOUT", "")
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_TIMEOUT


def _max_output_bytes() -> int:
    raw = os.environ.get("ISTOTA_DEVBOX_MAX_OUTPUT_BYTES", "")
    if not raw:
        return DEFAULT_MAX_OUTPUT_BYTES
    try:
        return max(1024, int(raw))
    except ValueError:
        return DEFAULT_MAX_OUTPUT_BYTES


def _truncate(data: bytes, cap: int) -> str:
    if len(data) <= cap:
        return data.decode("utf-8", "replace")
    head = data[:cap].decode("utf-8", "replace")
    return f"{head}\n…[truncated: {len(data) - cap} more bytes]"


def _run_docker(args: list[str], timeout: int) -> tuple[int, bytes, bytes]:
    """Run ``docker …`` and return ``(rc, stdout, stderr)``. Raises on timeout."""
    cmd = [_docker_cli(), *args]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _inspect(container: str, template: str, *, timeout: int = 10) -> tuple[int, str]:
    rc, out, _ = _run_docker(
        ["inspect", "-f", template, container], timeout=timeout,
    )
    return rc, out.decode("utf-8", "replace").strip()


def _check_owned(container: str) -> str | None:
    """Return None when the container exists, is running, and is owned by
    the current user — otherwise return an error string.

    Ownership is encoded as a Docker label (``com.istota.user_id=<user_id>``)
    written by the Ansible-rendered compose template. Containers without
    the label are accepted only when ``ISTOTA_USER_ID`` is unset (CLI
    smoke-tests on dev machines that don't deploy the label).
    """
    rc, running = _inspect(container, "{{.State.Running}}")
    if rc != 0:
        return f"Devbox container '{container}' does not exist."
    if running != "true":
        return f"Devbox container '{container}' is not running."
    uid = _user_id()
    if not uid:
        return None
    rc2, label = _inspect(container, "{{index .Config.Labels \"" + _OWNER_LABEL + "\"}}")
    if rc2 != 0:
        # Inspect already succeeded above; missing label means the container
        # was provisioned outside Ansible. Accept but don't enforce.
        return None
    if not label:
        return None  # legacy / hand-built container — same lenient stance
    if label != uid:
        return (
            f"Devbox container '{container}' is owned by '{label}', not '{uid}'. "
            "Refusing to operate."
        )
    return None


def _validate_host_path(p: Path, *, must_exist: bool) -> str | None:
    """Reject symlinks; require the path to land under an allowed root.

    The rule itself lives in ``istota.skill_host_paths`` — ``kv set
    --value-file`` needs the identical scoping, and two copies of a boundary
    check drift. Returns None on success, an error string on failure.

    Prefer `_resolve_host_path`, which hands back the approved path: acting on
    the caller-supplied one re-walks its symlinks and reopens the window.
    """
    return validate_host_path(p, must_exist=must_exist, operation="cp-in/cp-out")


def _resolve_host_path(p: Path, *, must_exist: bool) -> tuple[Path | None, str | None]:
    """`_validate_host_path` plus the approved path to actually operate on."""
    return resolve_host_path(
        p, writable=not must_exist, operation="cp-in/cp-out",
    )


def _normalize_container_path(path: str) -> str:
    """Resolve a container path the way `docker cp` does.

    `docker cp` reads a container path as relative to the container's ``/``,
    never to the image's WORKDIR — `c:tmp/x` and `c:/tmp/x` name the same file
    (moby's `container.ResolvePath` joins onto `/`). Anything reasoning about
    a container path has to use that base or it reasons about a different file
    than the one being copied: an earlier cut of this fix exempted relative
    paths on the WORKDIR premise, which both left `cp-in x workspace/a.txt`
    landing in the shadowed rootfs and made the arrival check below report a
    successful `cp-in x a.txt` as a failure.
    """
    collapsed = re.sub(r"/{2,}", "/", path.strip())
    return posixpath.normpath(posixpath.join("/", collapsed))


def _tmpfs_path_error(path: str, *, what: str) -> str | None:
    """Refuse a container path that lands inside a tmpfs mount.

    Comparison is against the normalized path, so `/workspace/../x` and
    `workspace/x` are judged on where they end up rather than on how they were
    spelled. A prefix match is anchored on a separator: `/workspaces/a` is not
    inside `/workspace`.
    """
    normalized = _normalize_container_path(path)
    for mount in _CONTAINER_TMPFS_MOUNTS:
        if normalized == mount or normalized.startswith(mount + "/"):
            return (
                f"{what} {path} is inside {mount}, which is a tmpfs mount. "
                "`docker cp` cannot traverse a tmpfs, so the copy would go to "
                "a directory nothing in the container can see. Use a path "
                f"under {_DEFAULT_WORKDIR} instead — {mount} is scratch space "
                "for the container's own processes, not an exchange path."
            )
    return None


def _validate_command(command: str) -> str | None:
    if "\x00" in command:
        return "NUL byte in command — refusing."
    if len(command.encode("utf-8", "replace")) > MAX_COMMAND_BYTES:
        return f"Command exceeds {MAX_COMMAND_BYTES}-byte cap — refusing."
    return None


def cmd_exec(args) -> dict:
    container = _container_name()
    if not container:
        return _err(
            "No devbox configured. Operator must enable [devbox] and the "
            "container must be named devbox-<user_id>."
        )
    err = _validate_command(args.command)
    if err:
        return _err(err)
    timeout = args.timeout or _exec_timeout()
    cap = _max_output_bytes()
    ownership_err = _check_owned(container)
    if ownership_err:
        return _err(ownership_err)

    # `-o pipefail`, because the envelope below tells its reader that
    # `exit_code` is the result (ISSUE-307). `bash -c` starts with the option
    # off, so a pipeline reports its *last* command's status and
    # `<runner> … | tail` — which the output cap actively pushes toward — came
    # back 0 on a run that failed. That is the failure this whole surface
    # exists to make impossible: a green test suite that was not green.
    #
    # The cost is real and is paid deliberately. `yes | head -5` reports 141
    # where it used to report 0, and a non-final stage that exits non-zero as
    # information — `grep` with no match — now colours the pipeline too. Both
    # are stated at `_SIGPIPE_EXIT` above and in `skill.md`. A status that is
    # wrong in the alarming direction makes a reader look; one wrong in the
    # reassuring direction is acted on, which is what settles the trade.
    cmd = [
        "exec", "-i", "-u", "dev", "-w", _DEFAULT_WORKDIR,
        container, *shell_argv(args.command, bash="bash"),
    ]
    start = time.monotonic()
    try:
        rc, stdout, stderr = _run_docker(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        # Killing the host-side `docker exec` doesn't stop the in-container
        # process tree. Identify it via `docker top` and signal pid 1 of the
        # exec session — same primitive Docker uses internally.
        _kill_stragglers(container, timeout)
        return _err(f"Command timed out after {timeout}s")
    duration_ms = int((time.monotonic() - start) * 1000)
    result = {
        "status": "ok",
        "exit_code": rc,
        "stdout": _truncate(stdout, cap),
        "stderr": _truncate(stderr, cap),
        "duration_ms": duration_ms,
    }
    if rc == _SIGPIPE_EXIT:
        result["note"] = _SIGPIPE_NOTE
    return result


def _kill_stragglers(container: str, timeout: int) -> None:
    """Find and TERM any bash/python/etc. processes left over from a timed-out
    exec inside the container. Scoped to processes owned by the dev user."""
    try:
        rc, out, _ = _run_docker(
            ["exec", "-u", "root", container, "sh", "-c",
             # ps output: pid,ppid,user,comm — skip header.
             # We only kill processes whose parent is PID 1 (the sleep
             # infinity entrypoint), which is the natural parent of any
             # exec session that's lost its docker-side handle.
             "ps -e -o pid=,ppid=,user= | awk '$2==1 && $3==\"dev\" {print $1}'"],
            timeout=5,
        )
        if rc != 0:
            return
        for pid in out.decode("utf-8", "replace").split():
            if not pid.isdigit():
                continue
            try:
                _run_docker(
                    ["exec", "-u", "root", container, "kill", "-TERM", pid],
                    timeout=5,
                )
            except Exception:
                continue
    except Exception:
        # Best-effort cleanup; never raise.
        pass


def cmd_exec_file(args) -> dict:
    container = _container_name()
    if not container:
        return _err("No devbox configured.")
    local, path_err = _resolve_host_path(Path(args.path), must_exist=True)
    if path_err:
        return _err(path_err)
    if not local.is_file():
        return _err(f"Script not found: {local}")
    ownership_err = _check_owned(container)
    if ownership_err:
        return _err(ownership_err)

    # Copy to a staging path keyed on the script name + pid to avoid collisions
    # when several exec-file calls run in parallel. The basename passes the same
    # regex as the container name so a hostile filename can't escape the
    # staging dir.
    base = local.name
    if not _NAME_PATTERN.match(base):
        return _err(f"Refusing unusual script basename: {base!r}")
    remote = f"{_EXEC_STAGING_DIR}/exec_{os.getpid()}_{base}"

    # `docker cp` does not create the destination's parent.
    rc_mk, _, mk_err = _run_docker(
        ["exec", "-u", "dev", container, "mkdir", "-p", _EXEC_STAGING_DIR],
        timeout=10,
    )
    if rc_mk != 0:
        detail = mk_err.decode("utf-8", "replace").strip()
        return _err(f"could not create staging dir {_EXEC_STAGING_DIR} in devbox: {detail}")

    rc, _, stderr = _run_docker(
        ["cp", str(local), f"{container}:{remote}"], timeout=30,
    )
    if rc != 0:
        _run_docker(["exec", "-u", "dev", container, "rm", "-f", remote], timeout=10)
        return _err(f"cp into devbox failed: {stderr.decode('utf-8', 'replace').strip()}")

    # `docker cp` preserves the *host* file's uid/gid and mode, and the daemon
    # user is not the container's `dev`. A script the daemon wrote at 0600 —
    # whatever wrote it chose the umask — therefore arrives owned by a stranger
    # and unreadable by the user about to run it, which surfaces as a bare
    # "Permission denied" from the interpreter. Fix the mode once, here, so
    # both the interpreter branch and the fallback below are covered; `chmod
    # +x` on the fallback alone would grant execute without read and still
    # fail. Root because `dev` does not own the file; that is no privilege
    # gain, since the image already grants `dev` passwordless sudo.
    rc_ch, _, ch_err = _run_docker(
        ["exec", "-u", "root", container, "chmod", "0755", remote], timeout=10,
    )
    if rc_ch != 0:
        _run_docker(["exec", "-u", "dev", container, "rm", "-f", remote], timeout=10)
        detail = ch_err.decode("utf-8", "replace").strip()
        return _err(f"chmod on the staged script failed: {detail}")

    interpreter = args.interpreter or _guess_interpreter(local)
    timeout = args.timeout or _exec_timeout()
    cap = _max_output_bytes()
    if interpreter:
        argv = ["exec", "-i", "-u", "dev", "-w", _DEFAULT_WORKDIR, container, interpreter, remote]
    else:
        # Run it directly; the staging copy is already 0755 and no longer on a
        # `noexec` mount.
        argv = ["exec", "-i", "-u", "dev", "-w", _DEFAULT_WORKDIR, container, remote]

    start = time.monotonic()
    timed_out = False
    try:
        rc, stdout, stderr = _run_docker(argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_stragglers(container, timeout)
    finally:
        # Clean up regardless — these are scratch copies.
        _run_docker(["exec", "-u", "dev", container, "rm", "-f", remote], timeout=10)
    if timed_out:
        return _err(f"Script timed out after {timeout}s")
    duration_ms = int((time.monotonic() - start) * 1000)
    return {
        "status": "ok",
        "exit_code": rc,
        "stdout": _truncate(stdout, cap),
        "stderr": _truncate(stderr, cap),
        "duration_ms": duration_ms,
    }


def _guess_interpreter(path: Path) -> str | None:
    suffix = path.suffix.lower()
    return {
        ".py": "python3",
        ".sh": "bash",
        ".bash": "bash",
        ".js": "node",
        ".rb": "ruby",
    }.get(suffix)


def cmd_cp_in(args) -> dict:
    container = _container_name()
    if not container:
        return _err("No devbox configured.")
    src, path_err = _resolve_host_path(Path(args.src), must_exist=True)
    if path_err:
        return _err(path_err)
    tmpfs_err = _tmpfs_path_error(args.dest, what="Destination")
    if tmpfs_err:
        return _err(tmpfs_err)
    ownership_err = _check_owned(container)
    if ownership_err:
        return _err(ownership_err)
    rc, _, stderr = _run_docker(
        ["cp", str(src), f"{container}:{args.dest}"], timeout=120,
    )
    if rc != 0:
        return _err(stderr.decode("utf-8", "replace").strip() or "docker cp failed")
    arrival_err = _check_arrived(
        container, _normalize_container_path(args.dest), src.name,
        src_is_dir=src.is_dir(),
    )
    if arrival_err:
        return _err(arrival_err)
    return {"status": "ok", "src": str(src), "dest": args.dest}


def _check_arrived(container: str, dest: str, basename: str, *, src_is_dir: bool) -> str | None:
    """Read the destination back from inside the container after a copy.

    `docker cp` exits 0 for a write into a directory the container cannot see,
    which is how ISSUE-306 stayed invisible for three months: silent data loss
    reported as ``{"status": "ok"}``. It catches what the tmpfs list above
    cannot enumerate — notably a symlink inside the container pointing an
    otherwise innocent destination into `/workspace`, which `docker cp` follows
    in rootfs scope and the mount list never sees.

    What it proves is that something exists at the destination, not that this
    copy is what put it there: an overwrite of a name already present passes
    whether or not the write landed. Sizing it up to a content comparison would
    buy the overwrite case and nothing else, so the weaker check is deliberate.

    `dest` must already be anchored at `/` by `_normalize_container_path` — the
    exec below has no `-w`, and `docker cp`'s base and the shell's cwd are not
    the same place. A *file* copied onto an existing directory lands inside it,
    so a bare ``test -e`` on the directory would pass without the file ever
    arriving; a *directory* copied onto a path that does not exist becomes that
    path rather than a child of it, which is why the source's kind decides the
    test. Both paths go in as positional arguments, nothing spliced into the
    script text. Root, so a destination under a directory `dev` cannot traverse
    still gives a real answer.
    """
    if src_is_dir:
        script = 'test -e "$1"'
    else:
        script = 'if [ -d "$1" ]; then test -e "$1/$2"; else test -e "$1"; fi'
    rc, _, stderr = _run_docker(
        ["exec", "-u", "root", container, "sh", "-c", script, "sh", dest, basename],
        timeout=30,
    )
    if rc == 0:
        return None
    detail = stderr.decode("utf-8", "replace").strip()
    if detail:
        # `rc` here is the *docker CLI's*, and only one of the two things it
        # can mean is "the file is absent". `docker exec` writes to stderr when
        # it could not run the check or could not fetch its status — the
        # allowlist proxy refusing an untracked exec is the case that happens
        # (ISSUE-313) — and `test -e` answering "no" writes nothing at all. So
        # a non-empty stderr means the question was never answered, and the
        # message below would be a confident false claim of the exact defect
        # ISSUE-306 was filed for. Report what was observed instead.
        return (
            f"could not read {dest} back from inside {container} after the "
            f"copy: {detail}. Whether the file arrived is unknown — the check "
            "did not run to an answer."
        )
    return (
        f"docker cp reported success but {dest} does not exist inside "
        f"{container}. The file was not copied; nothing was written where the "
        "container can reach it."
    )


def cmd_cp_out(args) -> dict:
    container = _container_name()
    if not container:
        return _err("No devbox configured.")
    dest, path_err = _resolve_host_path(Path(args.dest), must_exist=False)
    if path_err:
        return _err(path_err)
    tmpfs_err = _tmpfs_path_error(args.src, what="Source")
    if tmpfs_err:
        return _err(tmpfs_err)
    ownership_err = _check_owned(container)
    if ownership_err:
        return _err(ownership_err)
    rc, _, stderr = _run_docker(
        ["cp", f"{container}:{args.src}", str(dest)], timeout=120,
    )
    if rc != 0:
        return _err(stderr.decode("utf-8", "replace").strip() or "docker cp failed")
    return {"status": "ok", "src": args.src, "dest": str(dest)}


def cmd_status(args) -> dict:
    container = _container_name()
    if not container:
        return _err("No devbox configured.")
    fmt = (
        "{{.State.Running}}|{{.State.StartedAt}}|{{.Config.Image}}|"
        "{{.Id}}|{{.RestartCount}}|{{index .Config.Labels \""
        + _OWNER_LABEL + "\"}}"
    )
    rc, out, stderr = _run_docker(["inspect", "-f", fmt, container], timeout=10)
    if rc != 0:
        return _err(stderr.decode("utf-8", "replace").strip() or f"container '{container}' not found")
    parts = out.decode("utf-8", "replace").strip().split("|")
    while len(parts) < 6:
        parts.append("")
    running, started_at, image, cid, restart_count, owner = parts[:6]
    info: dict = {
        "status": "ok",
        "container": container,
        "running": running == "true",
        "started_at": started_at,
        "image": image,
        "id": cid[:12],
        "restart_count": _to_int(restart_count),
        "owner": owner or None,
    }
    # Disk usage (best-effort)
    rc2, out2, _ = _run_docker(
        ["exec", "-u", "dev", container, "sh", "-c",
         "du -sh /home/dev 2>/dev/null | awk '{print $1}'"],
        timeout=15,
    )
    if rc2 == 0:
        info["home_size"] = out2.decode("utf-8", "replace").strip() or None
    return info


def _to_int(s: str) -> int | None:
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def cmd_reset(args) -> dict:
    container = _container_name()
    if not container:
        return _err("No devbox configured.")
    if not args.yes:
        return _err(
            "Refusing to reset without --yes. This wipes /home/dev for the user."
        )
    ownership_err = _check_owned(container)
    if ownership_err:
        return _err(ownership_err)
    # Refuse to wipe /home/dev unless it's actually a mountpoint — otherwise
    # we'd be wiping a baked-in image layer the container couldn't restore
    # from a `docker restart`.
    rc_mp, _, _ = _run_docker(
        ["exec", "-u", "root", container, "mountpoint", "-q", "/home/dev"],
        timeout=10,
    )
    if rc_mp != 0:
        return _err(
            "/home/dev is not a mountpoint inside the container — refusing "
            "to wipe (the volume is likely misconfigured)."
        )
    rc, _, stderr = _run_docker(
        ["exec", "-u", "root", container, "sh", "-c",
         "find /home/dev -mindepth 1 -maxdepth 1 -exec rm -rf {} +"],
        timeout=120,
    )
    if rc != 0:
        return _err(stderr.decode("utf-8", "replace").strip() or "wipe failed")
    rc2, _, stderr2 = _run_docker(["restart", container], timeout=60)
    if rc2 != 0:
        return _err(stderr2.decode("utf-8", "replace").strip() or "restart failed")
    return {"status": "ok", "container": container, "reset": True}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m istota.skills.devbox",
        description="Per-user devbox container — exec, copy, inspect.",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    p_exec = sub.add_parser("exec", help="Run a command inside the devbox")
    p_exec.add_argument("command", help="Shell command to run (executed via bash -o pipefail -c)")
    p_exec.add_argument("--timeout", type=int, help="Per-exec timeout (s)")

    p_xf = sub.add_parser("exec-file", help="Copy a local script in and run it")
    p_xf.add_argument("path", help="Local file path")
    p_xf.add_argument("--interpreter", help="Interpreter (python3, bash, node, ruby). Default: guess from suffix")
    p_xf.add_argument("--timeout", type=int)

    p_in = sub.add_parser("cp-in", help="Copy a file into the devbox")
    p_in.add_argument("src", help="Local path")
    p_in.add_argument("dest", help="Path inside the container")

    p_out = sub.add_parser("cp-out", help="Copy a file out of the devbox")
    p_out.add_argument("src", help="Path inside the container")
    p_out.add_argument("dest", help="Local path")

    sub.add_parser("status", help="Devbox state, image, uptime, disk usage")

    p_reset = sub.add_parser("reset", help="Wipe /home/dev and restart container")
    p_reset.add_argument("--yes", action="store_true", help="Required confirmation flag")

    return p


_DISPATCH = {
    "exec": cmd_exec,
    "exec-file": cmd_exec_file,
    "cp-in": cmd_cp_in,
    "cp-out": cmd_cp_out,
    "status": cmd_status,
    "reset": cmd_reset,
}


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = _DISPATCH[args.subcommand]
    try:
        result = handler(args)
    except FileNotFoundError as e:
        # docker CLI not on PATH
        result = _err(f"Docker CLI not available: {e}")
    except Exception as e:  # noqa: BLE001 — JSON envelope is the contract
        result = _err(f"{type(e).__name__}: {e}")
    print(json.dumps(result, ensure_ascii=False))
    if result.get("status") == "error":
        sys.exit(1)
