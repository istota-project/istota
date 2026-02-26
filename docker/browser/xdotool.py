"""X11 input helpers via xdotool for CDP-free browser interaction."""

import logging
import os
import subprocess
import time

log = logging.getLogger(__name__)

_XDO_ENV = {**os.environ, "DISPLAY": ":99"}


def chrome_wid():
    """Get the main Chrome browser window ID.

    Without a window manager on Xvfb, xdotool can't infer the active window.
    Chrome spawns multiple X11 windows (helper windows, popups); we pick
    the largest one which is the actual browser window.
    """
    result = subprocess.run(
        ["xdotool", "search", "--class", "chrome"],
        env=_XDO_ENV, capture_output=True, text=True, timeout=5,
    )
    wids = [w.strip() for w in result.stdout.strip().split("\n") if w.strip()]
    if not wids:
        return None
    best_wid, best_area = None, 0
    for wid in wids:
        geo = subprocess.run(
            ["xdotool", "getwindowgeometry", "--shell", wid],
            env=_XDO_ENV, capture_output=True, text=True, timeout=5,
        )
        w = h = 0
        for line in geo.stdout.splitlines():
            if line.startswith("WIDTH="):
                w = int(line.split("=")[1])
            elif line.startswith("HEIGHT="):
                h = int(line.split("=")[1])
        area = w * h
        if area > best_area:
            best_area = area
            best_wid = wid
    return best_wid


def xdo(*args):
    """Run an xdotool command on the Xvfb display."""
    subprocess.run(
        ["xdotool"] + list(args),
        env=_XDO_ENV, timeout=5, capture_output=True,
    )


def xdo_key(*keys):
    """Send keyboard input to the Chrome window."""
    wid = chrome_wid()
    if not wid:
        log.warning("Chrome window not found for xdotool key input")
        return
    subprocess.run(
        ["xdotool", "windowfocus", "--sync", wid],
        env=_XDO_ENV, timeout=5, capture_output=True,
    )
    for key in keys:
        subprocess.run(
            ["xdotool", "key", "--window", wid, key],
            env=_XDO_ENV, timeout=5, capture_output=True,
        )


def xdo_type(text, delay_ms=8):
    """Type text into the Chrome window."""
    wid = chrome_wid()
    if not wid:
        log.warning("Chrome window not found for xdotool type")
        return
    subprocess.run(
        ["xdotool", "windowfocus", "--sync", wid],
        env=_XDO_ENV, timeout=5, capture_output=True,
    )
    subprocess.run(
        ["xdotool", "type", "--window", wid, "--delay", str(delay_ms),
         "--clearmodifiers", text],
        env=_XDO_ENV, timeout=10, capture_output=True,
    )


def window_title():
    """Get Chrome window title via X11 (zero CDP)."""
    wid = chrome_wid()
    if not wid:
        return ""
    result = subprocess.run(
        ["xdotool", "getwindowname", wid],
        env=_XDO_ENV, capture_output=True, text=True, timeout=5,
    )
    return result.stdout.strip()


def navigate(url, timeout_s=30):
    """Navigate by typing URL in Chrome's address bar via pure X11 input."""
    wid = chrome_wid()
    if not wid:
        raise RuntimeError("Chrome window not found")
    subprocess.run(
        ["xdotool", "windowfocus", "--sync", wid],
        env=_XDO_ENV, timeout=5, capture_output=True,
    )
    time.sleep(0.2)
    xdo_key("ctrl+l")
    time.sleep(0.2)
    xdo_type(url)
    time.sleep(0.15)
    xdo_key("Return")
    time.sleep(1.0)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        title = window_title()
        if title and "about:blank" not in title and "New Tab" not in title:
            break
        time.sleep(0.5)


def wait_for_challenges(timeout_s=15):
    """Wait for Cloudflare/security challenges to resolve via X11 title polling."""
    title = window_title()
    challenge_patterns = [
        "just a moment", "checking your browser", "verify you are human",
    ]
    if not any(p in title.lower() for p in challenge_patterns):
        return
    log.info("Challenge detected (title=%r) -- waiting for resolution", title)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(1.5)
        title = window_title()
        if not any(p in title.lower() for p in challenge_patterns):
            log.info("Challenge resolved (title=%r)", title)
            return
    log.warning(
        "Challenge did not resolve within %ds (title=%r)", timeout_s, title,
    )
