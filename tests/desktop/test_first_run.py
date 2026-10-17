"""The first run on this Mac, to a logged-in web UI, and row M6.

The app under test carries a release (`scripts/test-desktop.sh` with
`ISTOTA_DESKTOP_RELEASE`) and the `desktop-test-hooks` feature, which starts
the first run at launch with `ISTOTA_DESKTOP_FIRST_RUN_FOLDER` through the same
path the first-run screen's Set up takes, and hands the web setup's address to
the recording opener rather than the window. Everything it makes is scratch: a
state directory (so its own `LIMA_HOME`), an instance name of its own
(`ISTOTA_DESKTOP_INSTANCE`, so the Mac CLI's Keychain item is
`secret-key-<instance>`, never the default install's), a workspace folder, and
a port. The module removes all of them, the Keychain item included.

The run: the app unpacks the release and makes the VM; the test answers the
web setup over its API with a marker brain key; the app then mints a login
code and opens the web UI in its window, whose page spends the code. Logged
in means the VM's database shows that code spent and the identity's
`last_login_at` set.

Row M6: brain keys and the master key never sit in an argv or a file under the
app's state. `ps -axww` is sampled every 200 ms from launch until the window
has signed in; no sampled argv, and no file under the state directory, may
hold the marker or the master key (read from the scratch Keychain item at the
end, in this process only). The VM's own disk is left out of the file scan:
it is where both keys belong, inside the VM.

Control: `scripts/test-desktop.sh --control m6` builds the
`m6-negative-control` feature, which after the first run writes the master key
back to the Keychain with it on `security`'s argv, the write the Mac CLI makes
through `security -i`'s stdin; `test_no_argv_held_a_key` must go red.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest

from tests.support import parity

pytestmark = pytest.mark.desktop

REPO = Path(__file__).resolve().parents[2]
INSTANCE = "istota-dt"
KEYCHAIN_SERVICE = "istota.local"
KEYCHAIN_ACCOUNT = f"secret-key-{INSTANCE}"
FIRST_RUN_TIMEOUT = 45 * 60
SAMPLE_SECONDS = 0.2
#: The VM's own disk in its Lima instance directory (`disk` from Lima 2, the
#: other two before): the keys live inside the VM, and that is where they are.
VM_DISK_NAMES = {"disk", "basedisk", "diffdisk"}


def _bundled_release(bundle: Path) -> str | None:
    root = bundle / "Contents" / "Resources" / "release"
    if not root.is_dir():
        return None
    for entry in sorted(root.iterdir()):
        if (entry / f"istota-appliance-{entry.name}.raw.zst").is_file():
            return entry.name
    return None


def _keychain_has_item() -> bool:
    found = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return found.returncode == 0


def _keychain_value() -> str:
    found = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
        capture_output=True, text=True,
    )
    return found.stdout.strip() if found.returncode == 0 else ""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Sampler:
    """Every distinct process command line seen, from `ps -axww`."""

    def __init__(self):
        self.lines: set[str] = set()
        self.samples = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            out = subprocess.run(["/bin/ps", "-axww", "-o", "command="], capture_output=True, text=True)
            self.lines.update(out.stdout.splitlines())
            self.samples += 1
            self._stop.wait(SAMPLE_SECONDS)

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=10)


def _files_holding(root: Path, needles: list[str]) -> list[str]:
    """Files under `root` holding any needle, the VM's disks left out."""
    wanted = [n.encode() for n in needles if n]
    found = []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file() or path.name in VM_DISK_NAMES:
            continue
        with open(path, "rb") as f:
            tail = b""
            while chunk := f.read(1 << 20):
                block = tail + chunk
                if any(w in block for w in wanted):
                    found.append(str(path.relative_to(root)))
                    break
                tail = block[-256:]
    return found


class _Cli:
    """The bundled Mac CLI, in the app's environment for the scratch instance."""

    def __init__(self, bundle: Path, state: Path):
        resources = bundle / "Contents" / "Resources"
        self.bin = resources / "bin"
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": os.environ["HOME"],
            "LIMA_HOME": str(state / "lima"),
            "XDG_CONFIG_HOME": str(state / "config"),
            "ISTOTA_LIMA_INSTANCE": INSTANCE,
            "ISTOTA_MAC_BIN_DIR": str(state / "bin"),
        }

    def run(self, *args: str, timeout: float = 300) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.bin / "istota-stack"), *args], env=self.env,
                              capture_output=True, text=True, timeout=timeout)

    def limactl(self, *args: str, timeout: float = 300) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.bin / "limactl"), *args], env=self.env,
                              capture_output=True, text=True, timeout=timeout)


LOGIN_PROBE = (
    "import sqlite3; c = sqlite3.connect('file:/data/db/istota.db?mode=ro', uri=True); "
    "t = c.execute(\"select count(*) from web_auth_tokens where purpose = 'local_login' and used_at is not null\").fetchone()[0]; "
    "s = c.execute('select count(*) from web_auth_identities where last_login_at is not null').fetchone()[0]; "
    "print(t, s)"
)


def _signed_in(cli: _Cli) -> bool:
    out = cli.run("exec", "python3", "-c", LOGIN_PROBE, timeout=60)
    parts = out.stdout.split()
    return out.returncode == 0 and len(parts) == 2 and int(parts[0]) >= 1 and int(parts[1]) >= 1


def _cookies(response: httpx.Response, jar: dict[str, str]) -> None:
    for header in response.headers.get_list("set-cookie"):
        name, _, rest = header.partition("=")
        jar[name.strip()] = rest.split(";", 1)[0]


def _answer_setup(url: str, marker: str) -> dict:
    match = re.match(r"(http://localhost:\d+)/istota/setup#code=([0-9A-Za-z-]+)$", url)
    assert match, url
    origin, code = match.group(1), match.group(2)
    with httpx.Client(base_url=origin, timeout=60, trust_env=False) as client:
        unlocked = client.post("/istota/api/setup/unlock", json={"code": code}, headers={"origin": origin})
        assert unlocked.status_code == 200, unlocked.text
        jar: dict[str, str] = {}
        _cookies(unlocked, jar)
        headers = {"origin": origin, "cookie": "; ".join(f"{k}={v}" for k, v in jar.items())}
        put = client.put("/istota/api/setup/draft", headers=headers, json={
            "bot_name": "Istota", "display_name": "Desktop Tier", "user_id": "desktop",
            "user_email": "desktop@example.com", "timezone": "UTC",
            "brain_kind": "native", "native_model": "claude-sonnet-4-5", "native_api_key": marker,
            "ingress": "local", "hostname": origin.removeprefix("http://"), "profiles": [],
            "password": "desktop-tier-" + secrets.token_hex(8),
        })
        assert put.status_code == 200, put.text
        finish = client.post("/istota/api/setup/finish", headers=headers)
        assert finish.status_code == 200, finish.text
        return finish.json()


@pytest.fixture(scope="module")
def first_run(app_bundle: Path, app_executable: Path):
    tag = _bundled_release(app_bundle)
    if tag is None:
        pytest.skip("the app carries no release; build it with ISTOTA_DESKTOP_RELEASE set (scripts/test-desktop.sh)")
    root = Path(os.environ.get("ISTOTA_DESKTOP_SCRATCH") or Path.home() / ".istota-dt")
    assert not root.exists(), f"{root} is left from an earlier run; remove it (and its VM) first"
    assert not _keychain_has_item(), (
        f"the login Keychain already holds {KEYCHAIN_SERVICE} / {KEYCHAIN_ACCOUNT}; this tier does not touch an item it did not make"
    )
    signing_key = (os.environ.get("ISTOTA_DESKTOP_RELEASE_KEY") or "").strip() or (
        REPO / "host" / "release-signing-key.pub").read_text().strip()

    state, workspace = root / "s", root / "ws"
    port = _free_port()
    (state / "config" / "istota-stack").mkdir(parents=True)
    # The Mac CLI's own state file: create takes the release key and the port
    # from it, as it does on a re-run.
    (state / "config" / "istota-stack" / f"{INSTANCE}.env").write_text(
        f"SIGNING_KEY={signing_key}\nPORT={port}\n")
    shim_dir = root / "bin"
    opened = root / "opened"
    opener = root / "opener"
    opener.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{opened}"\n')
    opener.chmod(0o755)

    marker = "sk-ant-api03-m6marker" + secrets.token_hex(24)
    env = {k: v for k, v in os.environ.items() if k in ("HOME", "USER", "LOGNAME", "TMPDIR", "PATH", "LANG")}
    env.update({
        "ISTOTA_DESKTOP_STATE_DIR": str(state),
        "ISTOTA_DESKTOP_INSTANCE": INSTANCE,
        "ISTOTA_DESKTOP_OPENER": str(opener),
        "ISTOTA_DESKTOP_FIRST_RUN_FOLDER": str(workspace),
        "ISTOTA_DESKTOP_SHIM_DIR": str(shim_dir),
    })
    cli = _Cli(app_bundle, state)
    sampler = _Sampler()
    log_path = state / "logs" / "app.log"
    result: dict = {"tag": tag, "port": port, "marker": marker, "state": state}
    sampler.start()
    started = time.monotonic()
    app = subprocess.Popen([str(app_executable)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        def log_text() -> str:
            return log_path.read_text() if log_path.exists() else ""

        def wait(what: str, done, timeout: float = FIRST_RUN_TIMEOUT, interval: float = 2):
            deadline = started + timeout
            while time.monotonic() < deadline:
                if app.poll() is not None:
                    pytest.fail(f"the app exited ({app.returncode}) waiting for {what}; log:\n{log_text()[-4000:]}")
                value = done()
                if value:
                    return value
                time.sleep(interval)
            pytest.fail(f"no {what} within {timeout:.0f}s; log:\n{log_text()[-4000:]}")

        def setup_url():
            if not opened.exists():
                return None
            return next((line for line in opened.read_text().splitlines() if "/istota/setup#code=" in line), None)

        url = wait("web setup address handed to the opener", setup_url)
        result["setup_page_seconds"] = round(time.monotonic() - started)
        result["finish"] = _answer_setup(url, marker)
        wait("login code in the app log", lambda: "$ istota-stack exec istota web login-code" in log_text())
        result["login_code_seconds"] = round(time.monotonic() - started)
        # A login code lives 60 s; past that the window cannot sign in with it.
        # Recorded rather than required here, so row M6 is read either way.
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not _signed_in(cli):
            time.sleep(5)
        result["signed_in"] = _signed_in(cli)
        result["signed_in_seconds"] = round(time.monotonic() - started)
        logs = cli.run("compose", "logs", "--no-color", "nginx", timeout=120).stdout
        result["login_requests"] = [
            m.group(1) for m in re.finditer(r'"((?:GET|POST) /istota/auth/local HTTP/1\.1" \d{3})', logs)]
        result["shims"] = _through_the_shims(shim_dir)
        # The control writes the key on an argv after the first run; give the
        # sampler the same few seconds in every build.
        time.sleep(8)
    finally:
        sampler.stop()
        result["master_key"] = _keychain_value()
        result["log"] = log_path.read_text() if log_path.exists() else ""
        result["files"] = _files_holding(state, [marker, result["master_key"]]) if state.exists() else []
        if app.poll() is None:
            app.send_signal(signal.SIGTERM)
            try:
                app.wait(timeout=15)
            except subprocess.TimeoutExpired:
                app.kill()
                app.wait()
        result["stderr"] = app.stderr.read().decode(errors="replace") if app.stderr else ""
        result["argv"] = sampler.lines
        result["samples"] = sampler.samples
        timings = {k: result.get(k) for k in ("setup_page_seconds", "login_code_seconds", "signed_in_seconds")}
        print(f"\n[desktop first run] {timings}, {sampler.samples} ps samples, "
              f"{len(sampler.lines)} distinct command lines, login requests {result.get('login_requests')}")
        _teardown(cli, root)
    return result


STATUS = "grep -E '^(Uid|Gid|CapInh|CapPrm|CapEff|CapBnd|CapAmb):' /proc/self/status"


def _through_the_shims(shim_dir: Path) -> dict:
    """Row L4 again, through the command-line tools the app wrote: run with
    nothing of the app's environment, as from a Terminal."""
    env = {"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin", "LANG": "C"}
    runs = {}
    for name, argv in (("exec", ["istota-stack", "exec", "sh", "-c", STATUS]),
                       ("users", ["istota", "user", "list"])):
        script = shim_dir / argv[0]
        if not script.is_file():
            runs[name] = None
            continue
        out = subprocess.run([str(script), *argv[1:]], env=env, capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=300)
        runs[name] = {"rc": out.returncode, "stdout": out.stdout, "stderr": out.stderr[-2000:]}
    return runs


def _teardown(cli: _Cli, root: Path) -> None:
    if (Path(cli.env["LIMA_HOME"]) / INSTANCE).exists():
        cli.limactl("stop", "--force", INSTANCE, timeout=180)
        cli.limactl("delete", "--force", INSTANCE, timeout=180)
    if _keychain_has_item():
        subprocess.run(["/usr/bin/security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    shutil.rmtree(root, ignore_errors=True)


class TestTheFirstRunReachesASignedInWebUI:
    def test_the_first_run_started_from_the_bundled_release(self, first_run):
        log = first_run["log"]
        assert "first run started by ISTOTA_DESKTOP_FIRST_RUN_FOLDER" in log
        assert "$ istota-stack create --appliance … --workspace … --web-setup-in-app" in log
        assert re.search(r"^\S+ exit Some\(0\)$", log, re.M)

    def test_the_app_minted_a_login_code_once_set_up(self, first_run):
        assert "$ istota-stack exec istota web login-code" in first_run["log"]

    def test_the_window_signed_in_with_it(self, first_run):
        """WKWebView keeps no Secure cookie from http://localhost, so this holds
        only while local ingress writes ISTOTA_WEB_INSECURE_COOKIES=1."""
        assert first_run["signed_in"], f"the window's requests: {first_run['login_requests']}"

    def test_the_master_key_went_to_the_scratch_keychain_item(self, first_run):
        assert len(first_run["master_key"]) >= 32


class TestTheCommandLineToolsReachTheAppsVm:
    """Row L4 through the app's `istota-stack` and `istota`: they set the
    app's LIMA_HOME, state and instance themselves, and the CLI they run
    execs as uid 10001 with every capability set empty."""

    def test_the_app_wrote_them(self, first_run):
        assert first_run["shims"]["exec"] is not None, first_run["log"][-2000:]

    def test_exec_runs_as_10001_with_no_capabilities(self, first_run):
        run = first_run["shims"]["exec"]
        assert run["rc"] == 0, run
        fields = dict(
            (key.strip(), value.split()[0]) for key, _, value in
            (line.partition(":") for line in run["stdout"].splitlines()) if value.split())
        assert fields.get("Uid") == "10001" and fields.get("Gid") == "10001", fields
        for cap in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
            assert fields.get(cap) == "0000000000000000", f"{cap} is {fields.get(cap)}"

    def test_istota_reaches_the_cli(self, first_run):
        run = first_run["shims"]["users"]
        assert run["rc"] == 0, run
        assert "desktop" in run["stdout"], run


@parity.witness("M6")
class TestKeysStayOffTheMac:
    def test_the_sampler_ran_through_the_run(self, first_run):
        assert first_run["samples"] > first_run["signed_in_seconds"]
        assert any("istota-stack" in line and "create" in line for line in first_run["argv"])

    def test_no_argv_held_a_key(self, first_run):
        keys = [first_run["marker"], first_run["master_key"]]
        # Only each program's name is reported: the line itself holds the key.
        leaked = sorted({line.split()[0] for line in first_run["argv"] for key in keys if key and key in line})
        assert leaked == [], f"a key was on the argv of: {leaked}"

    def test_no_file_under_the_state_directory_holds_a_key(self, first_run):
        assert first_run["files"] == []
