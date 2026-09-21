"""Browser instances and their Chrome, Xvfb and VNC process lifetimes.

Acquisition and release run on the Flask thread. Plain registry snapshots may
be read by the monitor; Chrome's lifecycle lock serializes watchdog recovery.
"""

from dataclasses import dataclass, field
import os
import logging
from pathlib import Path
import socket
import subprocess
import threading
import time

import chrome

log = logging.getLogger(__name__)

PROFILE_ROOT = chrome.PROFILE_ROOT
MAX_INSTANCES = int(os.environ.get("BROWSER_MAX_INSTANCES", "2"))
CDP_PORT_BASE = 9300
VNC_PORT_BASE = 5900
DISPLAY_BASE = 100


@dataclass
class BrowserInstance:
    user_id: str
    profile_dir: str
    display: str
    cdp_port: int
    vnc_port: int
    slot: int
    proc: subprocess.Popen | None = None
    xvfb_proc: subprocess.Popen | None = None
    x11vnc_proc: subprocess.Popen | None = None
    pw_browser: object = None
    pw_context: object = None
    pw_thread_id: int | None = None
    launch_generation: int = 0
    launching: bool = False
    last_used: float = 0.0
    wedge_recoveries: list[float] = field(default_factory=list)
    cdp_health: dict = field(default_factory=lambda: {
        "last_success": 0.0, "last_failure": 0.0,
        "consecutive_failures": 0, "last_error": "",
    })


class PoolFull(RuntimeError):
    """No free browser slot."""


class LaunchFailed(RuntimeError):
    """A browser instance could not start."""


_instances: dict[str, BrowserInstance] = {}
_registry_lock = threading.Lock()


def live():
    """Return a stable snapshot without exposing the mutable registry."""
    with _registry_lock:
        return list(_instances.values())


def instance_for(user_id):
    with _registry_lock:
        return _instances.get(user_id)


def _wait_for_display(inst):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if inst.xvfb_proc.poll() is not None:
            raise RuntimeError("Xvfb exited before its display was ready")
        result = subprocess.run(
            ["xdpyinfo", "-display", inst.display], timeout=2,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return
        time.sleep(0.2)
    raise RuntimeError("Xvfb display did not become ready")


def _wait_for_vnc(inst):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if inst.x11vnc_proc.poll() is not None:
            raise RuntimeError("x11vnc exited before its listener was ready")
        try:
            with socket.create_connection(("127.0.0.1", inst.vnc_port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("x11vnc listener did not become ready")


def _start_display(inst):
    width = int(os.environ.get("SCREEN_WIDTH", "1440"))
    height = int(os.environ.get("SCREEN_HEIGHT", "900"))
    inst.xvfb_proc = subprocess.Popen(
        ["Xvfb", inst.display, "-screen", "0", f"{width}x{height}x24", "-ac"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    _wait_for_display(inst)
    args = [
        "x11vnc", "-display", inst.display, "-forever", "-shared",
        "-rfbport", str(inst.vnc_port),
    ]
    password = os.environ.get("VNC_PASSWORD", "")
    if password:
        args.extend(["-passwd", password])
    inst.x11vnc_proc = subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _wait_for_vnc(inst)


def acquire(user_id):
    """Start or reuse one profile. Capacity policy is added separately."""
    # This stage admits only a hardcoded user through Flask. The common scoping
    # rule is vendored in the next stage, before user ids arrive over the wire.
    if not user_id or user_id in (".", "..") or any(c in user_id for c in ("/", "\\", "\x00")):
        raise ValueError("Invalid browser user id")
    inst = instance_for(user_id)
    if inst is not None:
        inst.last_used = time.monotonic()
        return inst
    used = {item.slot for item in live()}
    slot = next((slot for slot in range(MAX_INSTANCES) if slot not in used), None)
    if slot is None:
        raise PoolFull("All browser slots are occupied")
    users = Path(PROFILE_ROOT) / "users"
    users.mkdir(parents=True, exist_ok=True)
    profile = users / user_id
    if users.is_symlink() or profile.is_symlink():
        raise ValueError("Browser profile must not be a symlink")
    profile.mkdir(mode=0o700, exist_ok=True)
    profile.chmod(0o700)
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        (profile / name).unlink(missing_ok=True)
    inst = BrowserInstance(
        user_id, str(profile), f":{DISPLAY_BASE + slot}",
        CDP_PORT_BASE + slot, VNC_PORT_BASE + slot, slot,
        last_used=time.monotonic(),
    )
    chrome._assert_pw_thread(inst, "acquire", record=False)
    try:
        _start_display(inst)
        chrome.launch_chrome(inst)
        chrome.connect_cdp(inst)
    except BaseException as exc:
        release_slot(inst)
        if isinstance(exc, Exception):
            log.warning("Browser instance launch failed for slot %d: %s", slot, exc)
            raise LaunchFailed(f"Browser instance failed to start: {exc}") from exc
        raise
    with _registry_lock:
        _instances[user_id] = inst
    return inst


def release_slot(inst):
    """Stop every instance child, retaining its profile directory."""
    # Hold the lifecycle lock through teardown so recovery cannot resurrect a
    # Chrome between its stop and its display's stop.
    with chrome._chrome_lock:
        chrome.cleanup(inst)
        chrome._kill_chrome_proc(inst.x11vnc_proc)
        chrome._kill_chrome_proc(inst.xvfb_proc)
        inst.x11vnc_proc = inst.xvfb_proc = None
        with _registry_lock:
            if _instances.get(inst.user_id) is inst:
                del _instances[inst.user_id]


def cleanup():
    """Release instances and then the one shared Patchright driver at exit."""
    for inst in live():
        release_slot(inst)
    if chrome._pw is not None:
        chrome._pw.stop()
        chrome._pw = None
        chrome._driver_thread_id = None
