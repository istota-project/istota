"""Browser instances and their Chrome, Xvfb and VNC process lifetimes.

Acquisition and release run on the Flask thread. Plain registry snapshots may
be read by the monitor; Chrome's lifecycle lock serializes watchdog recovery.
"""

from dataclasses import dataclass, field
import os
import json
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit, urlunsplit, parse_qsl
import logging
import socket
import subprocess
import threading
import time

import chrome
from lib.istota_user_scope import scoped_user_dir

log = logging.getLogger(__name__)

RUNTIME_DIR = Path("/run/istota-browser")
PROFILE_ROOT = chrome.PROFILE_ROOT
MAX_INSTANCES = int(os.environ.get("BROWSER_MAX_INSTANCES", "2"))
INSTANCE_IDLE_S = int(os.environ.get("BROWSER_INSTANCE_IDLE_S", "900"))
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
    retired: bool = False
    term_sent: bool = False
    cdp_wedge_reported: bool = False
    wedge_loop_reported: bool = False
    last_used: float = 0.0
    wedge_recoveries: list[float] = field(default_factory=list)
    cdp_health: dict = field(default_factory=lambda: {
        "last_success": 0.0, "last_failure": 0.0,
        "consecutive_failures": 0, "last_error": "",
    })


class PoolFull(RuntimeError):
    """No free browser slot."""


class MemoryRejected(RuntimeError):
    """Memory remains too high to start another browser."""


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


def console_url(inst, base="vnc.html"):
    """Encode the address, websocket query, and noVNC path independently."""
    if not base:
        return ""
    token = quote(inst.user_id, safe="")
    websocket_path = "websockify/?" + urlencode({"token": token})
    parts = urlsplit(base)
    query = [(name, value) for name, value in parse_qsl(parts.query, keep_blank_values=True)
             if name != "path"]
    query.append(("path", websocket_path))
    return urlunsplit(parts._replace(query=urlencode(query)))


def instance_metadata(base=""):
    """Read the live pool without acquiring instances or extending idle time."""
    now = time.monotonic()
    return [{
        "user": inst.user_id, "slot": inst.slot,
        "idle_seconds": max(0, now - inst.last_used),
        "url": console_url(inst, base),
    } for inst in live()]


def _write_console_file(path, text):
    # Temporary files stay outside TokenFile's directory: it reads every file.
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = RUNTIME_DIR / "console.tmp"
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_instances():
    now, monotonic_now = time.time(), time.monotonic()
    entries = [{
        "user": inst.user_id, "slot": inst.slot,
        "last_used": now - max(0, monotonic_now - inst.last_used),
        "url": console_url(inst),
    } for inst in live()]
    _write_console_file(RUNTIME_DIR / "web/instances.json", json.dumps(entries))


def _publish_route(inst):
    # Canonical identities may contain newlines or TokenFile's ': ' delimiter.
    # Slot filenames also avoid expanding long UTF-8 identities past NAME_MAX.
    token = quote(inst.user_id, safe="")
    _write_console_file(
        RUNTIME_DIR / "vnc-tokens" / str(inst.slot),
        f"{token}: localhost:{inst.vnc_port}\n",
    )


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


def evictable(exclude=()):
    """Return the oldest instance outside the caller's live-session set."""
    candidates = [inst for inst in live() if inst.user_id not in exclude]
    return min(candidates, key=lambda inst: inst.last_used, default=None)


def reap_idle(now, *, exclude=(), on_release=None):
    """Stop idle instances on the Flask thread, preserving their profiles."""
    reaped = []
    for inst in live():
        if inst.user_id in exclude or now - inst.last_used <= INSTANCE_IDLE_S:
            continue
        if on_release is not None:
            on_release(inst)
        release_slot(inst)
        reaped.append(inst.user_id)
    return reaped


def acquire(user_id, *, on_acquire=None, exclude=(), memory_pct=None,
            memory_reject_pct=80):
    """Start or reuse a profile; notify before browser work for watchdog arming."""
    users = scoped_user_dir(PROFILE_ROOT, "users")
    profile = scoped_user_dir(users, user_id)
    if profile is None:
        raise ValueError("Invalid browser user id")
    inst = instance_for(user_id)
    if inst is not None:
        chrome._assert_pw_thread(inst, "acquire", record=False)
        if on_acquire is not None:
            on_acquire(inst)
        inst.last_used = time.monotonic()
        _publish_instances()
        return inst
    # The API supplies its existing cgroup reader, so admission and deferred
    # pressure eviction use the same measurement and threshold.
    if memory_pct is not None and memory_pct() > memory_reject_pct:
        victim = evictable(exclude)
        if victim is not None:
            if on_acquire is not None:
                on_acquire(victim)
            release_slot(victim)
        if memory_pct() > memory_reject_pct:
            raise MemoryRejected("Memory pressure too high, refusing browser instance")
    if len(live()) >= MAX_INSTANCES:
        victim = evictable(exclude)
        if victim is None:
            raise PoolFull("All browser instances hold live sessions")
        if on_acquire is not None:
            on_acquire(victim)
        release_slot(victim)
    used = {item.slot for item in live()}
    slot = next((slot for slot in range(MAX_INSTANCES) if slot not in used), None)
    if slot is None:
        raise PoolFull("All browser slots are occupied")
    users.mkdir(parents=True, exist_ok=True)
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
        if on_acquire is not None:
            on_acquire(inst)
        # A failed removal during an earlier release must not route its old
        # address to this slot while the replacement browser is starting.
        (RUNTIME_DIR / "vnc-tokens" / str(inst.slot)).unlink(missing_ok=True)
        _start_display(inst)
        chrome.launch_chrome(inst)
        chrome.connect_cdp(inst)
        with _registry_lock:
            _instances[user_id] = inst
        _publish_route(inst)
        _publish_instances()
    except BaseException as exc:
        release_slot(inst)
        if isinstance(exc, Exception):
            log.warning("Browser instance launch failed for slot %d: %s", slot, exc)
            raise LaunchFailed(f"Browser instance failed to start: {exc}") from exc
        raise
    return inst


def release_slot(inst, *, require_stopped=False):
    """Stop every instance child, retaining its profile directory."""
    chrome._assert_pw_thread(inst, "release_slot", record=False)
    # Retire under the lifecycle lock so a late watchdog cannot resurrect this
    # slot. CDP teardown must run outside that lock, after Chrome is stopped.
    with chrome._chrome_lock:
        inst.retired = True
        if require_stopped and inst.proc is not None:
            # A destructive caller must retain the registry entry on failure,
            # or its next request could mistake a still-live profile for cold.
            chrome._kill_chrome_proc(inst.proc)
            if inst.proc.poll() is None:
                inst.retired = False
                raise RuntimeError("Browser did not stop; profile retained")
    try:
        (RUNTIME_DIR / "vnc-tokens" / str(inst.slot)).unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Could not remove browser console route: %s", exc)
    try:
        chrome.cleanup(inst)
    except Exception as exc:
        log.warning("Could not clean up browser connection in slot %d: %s", inst.slot, exc)
    for name in ("x11vnc_proc", "xvfb_proc"):
        try:
            chrome._kill_chrome_proc(getattr(inst, name))
        except Exception as exc:
            log.warning("Could not stop %s in slot %d: %s", name, inst.slot, exc)
        setattr(inst, name, None)
    with _registry_lock:
        if _instances.get(inst.user_id) is inst:
            del _instances[inst.user_id]
    try:
        _publish_instances()
    except OSError as exc:
        log.warning("Could not update browser console index: %s", exc)


def cleanup():
    """Release instances and then the one shared Patchright driver at exit."""
    instances = live()
    # Give every profile a chance to flush before waiting for any one browser
    # or its CDP connection. The init forwards SIGTERM to this API alone.
    with chrome._chrome_lock:
        for inst in instances:
            inst.retired = True
            if inst.proc is not None:
                try:
                    inst.proc.terminate()
                    inst.term_sent = True
                except OSError:
                    pass
    for inst in instances:
        release_slot(inst)
    if chrome._pw is not None:
        chrome._pw.stop()
        chrome._pw = None
        chrome._driver_thread_id = None
