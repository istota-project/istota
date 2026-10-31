"""What a process running as Chrome's user can get at, in a live browser service (#902).

Run through `docker exec -i -u browser <container> python -` by the image and vm
tiers, after a browse has started an instance. Prints one JSON line. Every
outcome is the exception's name or what was read, so the caller asserts on it.
"""

import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

TOKEN_NAME = b"ISTOTA_BROWSER_API_TOKEN="
SECRET = "/run/secrets/istota_browser_api_token"
HOLDS_TOKEN = re.compile(re.escape(TOKEN_NAME) + rb"[^\0]")
HOLDER = """
import ctypes, sys, time
if sys.argv[1] == "non-dumpable":
    ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)
print("ready", flush=True)
time.sleep(60)
"""


def cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return handle.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def pids():
    return [name for name in os.listdir("/proc") if name.isdigit() and int(name) != os.getpid()]


def read_environ(pid):
    try:
        with open(f"/proc/{pid}/environ", "rb") as handle:
            return handle.read()
    except OSError as exc:
        return type(exc).__name__


def read_stack(pid):
    """The initial environment block lives at the top of the stack."""
    try:
        with open(f"/proc/{pid}/maps") as maps:
            stack = next(line for line in maps if line.rstrip().endswith("[stack]"))
        start, end = (int(part, 16) for part in stack.split()[0].split("-"))
        with open(f"/proc/{pid}/mem", "rb") as mem:
            mem.seek(start)
            return mem.read(end - start)
    except OSError as exc:
        return type(exc).__name__


def outcome(read):
    if isinstance(read, str):
        return read
    return "holds the token" if HOLDS_TOKEN.search(read) else "read, no token"


def the_control(state):
    """The same two reads on a stand-in holding a token, without and with the change."""
    env = {**os.environ, TOKEN_NAME[:-1].decode(): "stand-in"}
    child = subprocess.Popen(
        [sys.executable, "-c", HOLDER, state], env=env, stdout=subprocess.PIPE)
    try:
        child.stdout.readline()
        return {"environ": outcome(read_environ(child.pid)), "mem": outcome(read_stack(child.pid))}
    finally:
        child.kill()
        child.wait()


def through_websockify():
    """The console's web server, asked for a link this user may plant in its own web root."""
    link = "/run/istota-browser/web/probe-secret"
    os.symlink(SECRET, link)
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    def status(path):
        try:
            with urllib.request.urlopen(f"https://127.0.0.1:6080/{path}", timeout=20, context=context) as answer:
                return answer.status
        except urllib.error.HTTPError as exc:
            return exc.code

    try:
        # A plain file first, or a server that answers 404 to everything would pass.
        return {"plain_file": status("instances.json"), "link": status("probe-secret")}
    finally:
        os.unlink(link)


def renderers():
    out = []
    for pid in pids():
        try:
            if "--type=renderer" not in cmdline(pid):
                continue
            with open(f"/proc/{pid}/status") as status:
                nspid = next(line for line in status if line.startswith("NSpid:"))
            out.append(len(nspid.split()) - 1)
        except (OSError, StopIteration):
            pass
    return out


def chromes_own_report():
    from patchright.sync_api import sync_playwright

    with sync_playwright() as driver:
        browser = driver.chromium.connect_over_cdp("http://127.0.0.1:9300")
        page = browser.contexts[0].new_page()
        try:
            page.goto("chrome://sandbox")
            time.sleep(1)
            return page.inner_text("body")
        finally:
            page.close()


def open_for_append(path):
    try:
        with open(path, "ab"):
            return "written"
    except OSError as exc:
        return type(exc).__name__


def read_secret():
    try:
        with open(SECRET, "rb") as handle:
            handle.read()
        return "read"
    except OSError as exc:
        return type(exc).__name__


def main():
    # By its start: tini's argv, root's and unreadable, names browse_api.py too.
    api = next(pid for pid in pids() if cmdline(pid).startswith("python ") and "/app/browse_api.py" in cmdline(pid))
    chrome = next(pid for pid in pids() if "--user-data-dir=" in cmdline(pid) and "--type=" not in cmdline(pid))
    holders = [pid for pid in pids() if outcome(read_environ(pid)) == "holds the token"]
    chrome_environ = read_environ(chrome)
    print(json.dumps({
        "uid": os.getuid(),
        "api_environ": outcome(read_environ(api)),
        "api_mem": outcome(read_stack(api)),
        "holders": [cmdline(pid)[:120] for pid in holders],
        "chrome_environ_readable": not isinstance(chrome_environ, str) and b"DISPLAY=" in chrome_environ,
        "control_as_shipped_before": the_control("dumpable"),
        "control_non_dumpable": the_control("non-dumpable"),
        "secret_file": read_secret(),
        "websockify": through_websockify(),
        "api_source": open_for_append("/app/browse_api.py"),
        "renderer_pid_namespaces": renderers(),
        "chrome_sandbox_page": chromes_own_report(),
    }))


if __name__ == "__main__":
    main()
