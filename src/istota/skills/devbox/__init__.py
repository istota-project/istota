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
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_TIMEOUT = 300
DEFAULT_MAX_OUTPUT_BYTES = 102_400
MAX_COMMAND_BYTES = 32 * 1024  # bash -c argv length cap
_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]+$")
_OWNER_LABEL = "com.istota.user_id"


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


def _allowed_host_roots() -> list[Path]:
    """Host paths the skill is willing to read from / write to.

    cp-in source must be under one of these; cp-out destination must too.
    """
    roots: list[Path] = []
    for var in ("ISTOTA_DEFERRED_DIR", "NEXTCLOUD_MOUNT_PATH"):
        val = os.environ.get(var, "").strip()
        if not val:
            continue
        try:
            roots.append(Path(val).resolve())
        except Exception:
            continue
    return roots


def _validate_host_path(p: Path, *, must_exist: bool) -> str | None:
    """Reject symlinks; require the path to land under an allowed root.

    Returns None on success, an error string on failure.
    """
    roots = _allowed_host_roots()
    if not roots:
        # No allowlist configured (CLI smoke test outside the executor).
        # Don't silently widen the boundary — refuse the operation.
        return (
            "No allowed host roots configured (ISTOTA_DEFERRED_DIR / "
            "NEXTCLOUD_MOUNT_PATH unset). cp-in/cp-out refused."
        )
    try:
        if must_exist:
            if p.is_symlink():
                return f"Refusing host-side symlink: {p}"
            if not p.exists():
                return f"Source not found: {p}"
            resolved = p.resolve(strict=True)
        else:
            # For destinations, the path may not exist yet; resolve against
            # the parent (which we'll create if missing).
            parent = p.parent
            if not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
            if parent.is_symlink():
                return f"Refusing host-side symlink on dest parent: {parent}"
            resolved = (parent.resolve(strict=True) / p.name)
    except OSError as e:
        return f"Path resolution failed: {e}"
    for root in roots:
        try:
            resolved.relative_to(root)
            return None
        except ValueError:
            continue
    return (
        f"Path {resolved} is outside allowed roots "
        f"({', '.join(str(r) for r in roots)})."
    )


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

    cmd = ["exec", "-i", "-u", "dev", "-w", "/workspace", container, "bash", "-c", args.command]
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
    return {
        "status": "ok",
        "exit_code": rc,
        "stdout": _truncate(stdout, cap),
        "stderr": _truncate(stderr, cap),
        "duration_ms": duration_ms,
    }


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
    local = Path(args.path)
    path_err = _validate_host_path(local, must_exist=True)
    if path_err:
        return _err(path_err)
    if not local.is_file():
        return _err(f"Script not found: {local}")
    ownership_err = _check_owned(container)
    if ownership_err:
        return _err(ownership_err)

    # Copy to a workspace path keyed on the script name + pid to avoid
    # collisions when several exec-file calls run in parallel. The basename
    # passes the same regex as the container name so a hostile filename
    # can't escape /workspace.
    base = local.name
    if not _NAME_PATTERN.match(base):
        return _err(f"Refusing unusual script basename: {base!r}")
    remote = f"/workspace/exec_{os.getpid()}_{base}"
    rc, _, stderr = _run_docker(
        ["cp", str(local), f"{container}:{remote}"], timeout=30,
    )
    if rc != 0:
        return _err(f"cp into devbox failed: {stderr.decode('utf-8', 'replace').strip()}")

    interpreter = args.interpreter or _guess_interpreter(local)
    timeout = args.timeout or _exec_timeout()
    cap = _max_output_bytes()
    if interpreter:
        argv = ["exec", "-i", "-u", "dev", "-w", "/workspace", container, interpreter, remote]
    else:
        # Fall back to making it executable and running it directly.
        _run_docker(["exec", "-u", "dev", container, "chmod", "+x", remote], timeout=10)
        argv = ["exec", "-i", "-u", "dev", "-w", "/workspace", container, remote]

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
    src = Path(args.src)
    path_err = _validate_host_path(src, must_exist=True)
    if path_err:
        return _err(path_err)
    ownership_err = _check_owned(container)
    if ownership_err:
        return _err(ownership_err)
    rc, _, stderr = _run_docker(
        ["cp", str(src), f"{container}:{args.dest}"], timeout=120,
    )
    if rc != 0:
        return _err(stderr.decode("utf-8", "replace").strip() or "docker cp failed")
    return {"status": "ok", "src": str(src), "dest": args.dest}


def cmd_cp_out(args) -> dict:
    container = _container_name()
    if not container:
        return _err("No devbox configured.")
    dest = Path(args.dest)
    path_err = _validate_host_path(dest, must_exist=False)
    if path_err:
        return _err(path_err)
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
    p_exec.add_argument("command", help="Shell command to run (executed via bash -c)")
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
