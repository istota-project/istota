"""`host/mac/istota-stack` and `host/mac/istota`: the local VM's commands on the Mac.

The Mac CLI creates and controls a Lima VM and forwards every other verb to
the VM's own `istota-stack`, so what is held here is what it hands to
`limactl`, `security`, `lsof` and `open`, each replaced by a recording stub on
PATH. PATH is otherwise only the system directories, so a test can never reach
a real `limactl` or the real login Keychain.

What a stub answers comes from a JSON state file the test writes: the
instance and its status, what the VM's probe reports (deployed tag, whether a
config exists, the first admin), the master key `istota-stack exec cat`
returns, and the fake Keychain's items. Every stub appends its argv to one
calls file; `security -i` reads its command from stdin, which is not recorded,
and that is the route the master key takes.

Row L4 (the shims run as uid 10001 with no capabilities) is witnessed in the
vm tier (`tests/vm/test_host_folder.py::TestTheMacCliExecsAsTheDaemon`); the
default suite holds the argv the shim and the forward produce.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MAC_CLI = REPO / "host" / "mac" / "istota-stack"
MAC_SHIM = REPO / "host" / "mac" / "istota"
TEMPLATE = REPO / "host" / "lima" / "istota.yaml"
# The Mac's own bash is 3.2; run the scripts under it wherever there is one.
BASH = "/bin/bash" if Path("/bin/bash").exists() else "bash"

KEY = "7f" * 32
SIGNING_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIStageSixTestKeyOnly releases"

STUB = r'''#!{python}
import json, os, sys, time

name = os.path.basename(sys.argv[0])
argv = sys.argv[1:]
state_path = os.environ["STUB_STATE"]
with open(state_path) as fh:
    state = json.load(fh)
with open(os.environ["STUB_CALLS"], "a") as fh:
    fh.write(json.dumps({"cmd": name, "argv": argv}) + "\n")


def save():
    with open(state_path, "w") as fh:
        json.dump(state, fh)


def out(text):
    sys.stdout.write(text)
    sys.stdout.flush()


if name == "uname":
    out((state.get("arch", "arm64") if "-m" in argv else state.get("system", "Darwin")) + "\n")
    sys.exit(0)

if name == "lsof":
    if any(a.endswith("istota-scheduler-daemon.lock") for a in argv):
        if state.get("lock_holder"):
            out(f"{state['lock_holder']}\n")
            sys.exit(0)
        sys.exit(1)
    holder = state.get("port_holder")
    if holder:
        out(f"COMMAND   PID USER   FD   TYPE\n{holder} 4242 me 5u IPv4 TCP 127.0.0.1:8080 (LISTEN)\n")
        sys.exit(0)
    sys.exit(1)

if name == "open":
    sys.exit(0)

if name == "security":
    keychain = state.setdefault("keychain", {})
    if argv[:1] == ["-i"]:
        words = sys.stdin.read().split()
        if words and words[0] == "add-generic-password":
            opts = {}
            i = 1
            while i < len(words):
                if words[i] in ("-s", "-a", "-w") and i + 1 < len(words):
                    opts[words[i]] = words[i + 1]
                    i += 2
                else:
                    i += 1
            item = f"{opts.get('-s')}/{opts.get('-a')}"
            if item in keychain and "-U" not in words:
                sys.exit(45)
            keychain[item] = "" if state.get("short_read") else opts.get("-w", "")
            save()
        sys.exit(0)
    if argv[:1] == ["find-generic-password"]:
        item = f"{argv[argv.index('-s') + 1]}/{argv[argv.index('-a') + 1]}"
        if item not in keychain:
            print("security: The specified item could not be found in the keychain.", file=sys.stderr)
            sys.exit(44)
        if "-w" in argv:
            out(keychain[item] + "\n")
        sys.exit(0)
    sys.exit(0)

# limactl
instance = state.get("instance")
if argv[:1] == ["--version"]:
    out(f"limactl version {state.get('version', '2.2.1')}\n")
    sys.exit(0)
verb = argv[0]
if verb == "list":
    if instance and instance["name"] == argv[-1]:
        out(instance["status"] + "\n")
    sys.exit(0)
if verb == "create":
    name_arg = next(a for a in argv if a.startswith("--name="))
    state["instance"] = {"name": name_arg.split("=", 1)[1], "status": "Stopped"}
    save()
    sys.exit(state.get("create_rc", 0))
if verb in ("start", "stop"):
    instance["status"] = "Running" if verb == "start" else "Stopped"
    save()
    sys.exit(0)
if verb == "delete":
    state["instance"] = None
    state["vm"] = {}
    save()
    sys.exit(0)
if verb == "autostart":
    sys.exit(0)
if verb == "copy":
    source, target = argv[-2], argv[-1]
    if ":" in source and not source.startswith("/"):
        guest = source.split(":", 1)[1]
        with open(target, "w") as fh:
            fh.write(state.get("guest_files", {}).get(guest, f"#!/bin/bash\n# copied from the VM: {guest}\n"))
    else:
        state.setdefault("copied", []).append(source)
        save()
        if state.get("vm_root") and os.path.isdir(source):
            import shutil
            shutil.copytree(source, os.path.join(state["vm_root"], os.path.basename(source.rstrip("/"))))
    sys.exit(0)
if verb == "shell":
    rest = argv[1:]
    if rest[:1] == ["--workdir"]:
        rest = rest[2:]
    command = rest[1:]
    if not instance or instance["status"] != "Running":
        print(f"instance {rest[0]} is not running", file=sys.stderr)
        sys.exit(1)
    if not command or command[0] != "sudo":
        if command[:1] == ["mktemp"]:
            out("/tmp/tmp.stub\n")
        sys.exit(0)
    command = command[1:]
    vm = state.setdefault("vm", {})
    if command[:2] == ["sh", "-c"]:
        out(f"tag={vm.get('tag', '')}\nunsigned={vm.get('unsigned', '')}\n"
            f"config={1 if vm.get('config') else 0}\nadmin={vm.get('admin', '')}\n")
        sys.exit(0)
    if command[:1] != ["istota-stack"]:
        sys.exit(0)
    stack = command[1:]
    if stack == ["exec", "cat", "/data/.secret_key"]:
        out(state.get("key", ""))
        sys.exit(0)
    if stack[:3] == ["exec", "istota", "doctor"]:
        out("doctor ran\n")
        sys.exit(state.get("doctor_rc", 0))
    if stack[:4] == ["exec", "istota", "auth", "list"]:
        out(state.get("auth_list", "") + "state=unchanged\n")
        sys.exit(0)
    if stack[:3] == ["exec", "istota", "auth"]:
        out("http://localhost:8080/istota/auth/set-password?token=t0k3n\nstate=updated\n")
        sys.exit(0)
    if stack[:1] == ["update"]:
        sys.stdout.write("building\n" * 30)
        rc = state.get("update_rc", 0)
        if rc == 0:
            vm["tag"] = stack[-1]
            vm["unsigned"] = "1" if "--unsigned" in stack else ""
        save()
        sys.exit(rc)
    if stack[:1] == ["setup"]:
        vm["config"] = True
        vm["admin"] = state.get("admin", "alice")
        save()
        sys.exit(0)
    if stack[:1] == ["check-image"]:
        rcs = state.get("check_image_rcs", [0])
        rc = rcs.pop(0) if rcs else 0
        save()
        sys.exit(rc)
    if stack[:1] == ["host-env"]:
        env = vm.setdefault("host_env", {})
        if len(stack) == 2 and "=" not in stack[1]:
            out(env.get(stack[1], "") + "\n")
        else:
            for pair in stack[1:]:
                key, _, value = pair.partition("=")
                env[key] = value
            save()
        sys.exit(0)
    if stack[:1] == ["restore"]:
        state["restore_stdin"] = sys.stdin.read()
        rc = state.get("restore_rc", 0)
        if rc == 0:
            vm["config"] = True
        save()
        sys.exit(rc)
    if stack[:1] == ["import-standalone"]:
        state["import_stdin"] = sys.stdin.read()
        rc = state.get("import_rc", 0)
        if rc == 0 and "--dry-run" not in stack:
            vm["config"] = True
            vm["admin"] = state.get("admin", "alice")
        save()
        sys.exit(rc)
    if stack[:1] == ["stage"]:
        out(f"/srv/app/istota/staging/AbCd1234/{os.path.basename(stack[-1])}\n")
        sys.exit(0)
    if stack[:1] == ["down"] and state.get("down_sleep"):
        time.sleep(state["down_sleep"])
    print(f"forwarded {stack}", file=sys.stderr)
    sys.exit(state.get("forward_rc", 0))
sys.exit(0)
'''


@dataclass
class Mac:
    root: Path
    home: Path
    env: dict
    state_path: Path
    calls_path: Path

    @property
    def bin_dir(self) -> Path:
        return self.home / ".local" / "bin"

    @property
    def data_dir(self) -> Path:
        return self.root / "xdg-data" / "istota"

    def state(self) -> dict:
        return json.loads(self.state_path.read_text())

    def set(self, **values) -> None:
        state = self.state()
        state.update(values)
        self.state_path.write_text(json.dumps(state))

    def run(self, *args: str, script: Path = MAC_CLI, stdin: str | None = None,
            **extra_env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [BASH, str(script), *args], capture_output=True, text=True,
            input=stdin if stdin is not None else "", env={**self.env, **extra_env}, timeout=60,
        )

    def calls(self, cmd: str | None = None) -> list[list[str]]:
        if not self.calls_path.exists():
            return []
        found = [json.loads(line) for line in self.calls_path.read_text().splitlines()]
        return [c["argv"] for c in found if cmd is None or c["cmd"] == cmd]

    def all_calls(self) -> list[dict]:
        if not self.calls_path.exists():
            return []
        return [json.loads(line) for line in self.calls_path.read_text().splitlines()]

    def vm_calls(self) -> list[list[str]]:
        """What each `limactl shell ... sudo` ran in the VM."""
        found = []
        for argv in self.calls("limactl"):
            if argv[:1] != ["shell"]:
                continue
            rest = argv[1:]
            if rest[:1] == ["--workdir"]:
                rest = rest[2:]
            if rest[1:2] == ["sudo"]:
                found.append(rest[2:])
        return found

    def stack_calls(self) -> list[list[str]]:
        return [c[1:] for c in self.vm_calls() if c[:1] == ["istota-stack"]]


@pytest.fixture
def mac(tmp_path) -> Mac:
    home = tmp_path / "home"
    home.mkdir()
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    stub = stubs / "stub"
    stub.write_text(STUB.replace("{python}", sys.executable))
    stub.chmod(0o755)
    for name in ("limactl", "security", "lsof", "open", "uname"):
        (stubs / name).symlink_to(stub)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"key": KEY}))
    calls = tmp_path / "calls"
    env = {
        # The system directories only: nothing here may find a real limactl.
        "PATH": f"{stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(home),
        # No XDG_CONFIG_HOME, as on a Mac: ~/.config is shared with the
        # standalone install's ~/.config/istota, and a fixture that moved it
        # away hid a state file written over that install's istota.env.
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "TMPDIR": str(tmp_path),
        "LANG": "C",
        "STUB_STATE": str(state),
        "STUB_CALLS": str(calls),
    }
    return Mac(tmp_path, home, env, state, calls)


def _create(mac: Mac, *args: str, **extra_env: str) -> subprocess.CompletedProcess:
    return mac.run("create", "--tag", "v1.2.0", "--signing-key", SIGNING_KEY, *args, **extra_env)


def _resolved(path: Path) -> str:
    return str(path.resolve())


class TestForwarding:
    def test_a_server_verb_reaches_the_vm_verbatim(self, mac):
        mac.set(instance={"name": "istota", "status": "Running"})
        result = mac.run("exec", "sh", "-c", "grep -E '^Uid:' /proc/self/status")
        assert result.returncode == 0, result.stderr
        assert mac.calls("limactl")[-1] == [
            "shell", "--workdir", "/", "istota", "sudo", "istota-stack",
            "exec", "sh", "-c", "grep -E '^Uid:' /proc/self/status",
        ]

    def test_the_vms_exit_status_comes_back_unchanged(self, mac):
        mac.set(instance={"name": "istota", "status": "Running"}, forward_rc=3)
        result = mac.run("rollback")
        assert result.returncode == 3, result.stderr
        assert mac.stack_calls()[-1] == ["rollback"]

    def test_the_instance_name_is_overridable(self, mac):
        mac.set(instance={"name": "istota-vm-1", "status": "Running"})
        result = mac.run("compose", "ps", ISTOTA_LIMA_INSTANCE="istota-vm-1")
        assert result.returncode == 0, result.stderr
        assert mac.calls("limactl")[-1][:4] == ["shell", "--workdir", "/", "istota-vm-1"]

    def test_the_shim_is_the_vms_istota_wrapper(self, mac):
        mac.set(instance={"name": "istota", "status": "Running"}, forward_rc=4)
        result = subprocess.run(
            ["sh", str(MAC_SHIM), "user", "list"], capture_output=True, text=True,
            env=mac.env, input="", timeout=60,
        )
        assert result.returncode == 4, result.stderr
        assert mac.stack_calls()[-1] == ["exec", "istota", "user", "list"]


class TestFileArguments:
    """`apply <plan>` and `import-bare-metal <dir>` name paths on the Mac; the
    VM is handed a copy under its staging directory, removed afterwards."""

    def test_a_plan_is_copied_in_and_the_vm_path_passed(self, mac, tmp_path):
        mac.set(instance={"name": "istota", "status": "Running"}, forward_rc=2)
        plan = tmp_path / "plan.toml"
        plan.write_text("[config]\n")
        result = mac.run("apply", str(plan), "--dry-run")
        assert result.returncode == 2, result.stderr
        copies = [c for c in mac.calls("limactl") if c[:1] == ["copy"]]
        assert copies == [["copy", str(plan), "istota:/tmp/tmp.stub/"]]
        staged = "/srv/app/istota/staging/AbCd1234/plan.toml"
        assert mac.stack_calls() == [
            ["stage", "/tmp/tmp.stub/plan.toml"],
            ["apply", staged, "--dry-run"],
            ["unstage", staged],
        ]

    def test_an_export_is_copied_recursively_for_the_daemon(self, mac, tmp_path):
        mac.set(instance={"name": "istota", "status": "Running"})
        export = tmp_path / "export"
        export.mkdir()
        (export / "manifest.json").write_text("{}")
        result = mac.run("import-bare-metal", str(export), "--ingress", "local")
        assert result.returncode == 0, result.stderr
        assert ["copy", "-r", str(export), "istota:/tmp/tmp.stub/"] in mac.calls("limactl")
        staged = "/srv/app/istota/staging/AbCd1234/export"
        assert mac.stack_calls() == [
            ["stage", "--daemon", "/tmp/tmp.stub/export"],
            ["import-bare-metal", staged, "--ingress", "local"],
            ["unstage", staged],
        ]

    def test_a_missing_path_reaches_nothing(self, mac, tmp_path):
        mac.set(instance={"name": "istota", "status": "Running"})
        result = mac.run("apply", str(tmp_path / "nope.toml"))
        assert result.returncode == 64, result.stderr
        assert mac.calls("limactl") == []


REFUSED = {
    "icloud": "Library/Mobile Documents/com~apple~CloudDocs/Istota",
    "cloud storage": "Library/CloudStorage/Dropbox/Istota",
    "documents": "Documents/Istota",
    "desktop": "Desktop/Istota",
    "downloads": "Downloads/Istota",
    "the standalone root": ".istota",
    "inside the standalone root": ".istota/workspace",
    "the home directory": ".",
    "inside ~/.ssh": ".ssh/istota",
}


class TestTheFolder:
    @pytest.mark.parametrize("relative", REFUSED.values(), ids=REFUSED.keys())
    def test_a_refused_location_is_refused_before_limactl(self, mac, relative):
        target = mac.home / relative
        result = _create(mac, "--workspace", str(target))
        assert result.returncode == 78, result.stdout + result.stderr
        assert mac.calls("limactl") == []
        assert not (target / ".istota-workspace").exists()

    @pytest.mark.parametrize("path", ["/", "PARENT"], ids=["root", "parent of ~/.lima"])
    def test_a_parent_of_the_home_is_refused(self, mac, path):
        target = str(mac.home.parent) if path == "PARENT" else path
        result = _create(mac, "--workspace", target)
        assert result.returncode == 78, result.stdout + result.stderr
        assert mac.calls("limactl") == []

    def test_a_link_into_a_refused_place_is_refused(self, mac):
        real = mac.home / "Documents" / "real"
        real.mkdir(parents=True)
        link = mac.home / "Istota"
        link.symlink_to(real)
        result = _create(mac, "--workspace", str(link))
        assert result.returncode == 78, result.stdout + result.stderr
        assert mac.calls("limactl") == []

    def test_a_folder_with_files_and_no_marker_needs_adopt(self, mac):
        folder = mac.home / "Istota"
        folder.mkdir()
        (folder / "notes.txt").write_text("mine")
        result = _create(mac)
        assert result.returncode == 78, result.stdout + result.stderr
        assert "--adopt" in result.stderr
        assert not (folder / ".istota-workspace").exists()
        assert not any(c[:1] == ["create"] for c in mac.calls("limactl"))

        result = _create(mac, "--adopt")
        assert result.returncode == 0, result.stdout + result.stderr
        assert (folder / ".istota-workspace").is_file()
        assert (folder / "notes.txt").read_text() == "mine"

    def test_a_new_folder_is_made_with_its_marker(self, mac):
        result = _create(mac)
        assert result.returncode == 0, result.stdout + result.stderr
        assert (mac.home / "Istota" / ".istota-workspace").is_file()


def _limactl_create(mac: Mac) -> list[str]:
    return next(c for c in mac.calls("limactl") if c[:1] == ["create"])


class TestCreate:
    def test_a_release_create_runs_the_server_sequence(self, mac):
        result = _create(mac, "--port", "8181", "--cpus", "6", "--memory", "8")
        assert result.returncode == 0, result.stdout + result.stderr

        folder = _resolved(mac.home / "Istota")
        create = _limactl_create(mac)
        assert "--name=istota" in create
        assert create[create.index("--cpus") + 1] == "6"
        assert create[create.index("--memory") + 1] == "8"
        assert f"--param=WORKSPACE={folder}" in create
        assert "--param=ISTOTA_TAG=v1.2.0" in create
        assert f"--param=RELEASE_SIGNING_KEY={SIGNING_KEY}" in create
        assert not any(a.startswith("--param=ISTOTA_REPO=") for a in create)
        expression = create[create.index("--set") + 1]
        assert ".portForwards[0].guestPort = 8181" in expression
        assert ".portForwards[0].hostPort = 8181" in expression
        assert 'del(.mounts[] | select(.mountPoint == "/mnt/istota-repo"))' in expression
        assert create[-1] == str(TEMPLATE)
        assert ["start", "--tty=false", "istota"] in mac.calls("limactl")

        assert mac.stack_calls()[:6] == [
            ["host-env", "STORAGE=host-folder", "INGRESS=local",
             "HOST_LOOPBACK_BLOCK=1", "HOST_GATEWAY_ADDR=192.168.5.2"],
            ["update", "v1.2.0"],
            ["setup", "--storage", "host-folder", "--ingress", "local", "--hostname", "localhost:8181"],
            ["check-image"],
            ["exec", "cat", "/data/.secret_key"],
            ["exec", "istota", "auth", "list"],
        ]
        vm = mac.vm_calls()
        provision = vm.index(["/srv/app/istota/src/host/provision.sh"])
        enable = vm.index(["systemctl", "enable", "--now", "istota-stack"])
        assert vm.index(["istota-stack", "check-image"]) < provision < enable
        assert enable < vm.index(["istota-stack", "exec", "cat", "/data/.secret_key"])

    def test_the_master_key_goes_to_the_keychain_and_never_on_an_argv(self, mac):
        result = _create(mac)
        assert result.returncode == 0, result.stdout + result.stderr
        assert mac.state()["keychain"] == {"istota.local/secret-key-istota": KEY}
        for call in mac.all_calls():
            assert not any(KEY in arg for arg in call["argv"]), call
        assert KEY not in result.stdout + result.stderr
        assert ["-i"] in mac.calls("security")

    def test_nothing_writes_a_host_folder_uid(self, mac):
        assert _create(mac).returncode == 0
        assert "HOST_FOLDER_UID" not in json.dumps(mac.all_calls())

    def test_a_short_keychain_write_is_caught_on_the_read_back(self, mac):
        mac.set(short_read=True)
        result = _create(mac)
        assert result.returncode != 0
        assert "Keychain" in result.stderr

    def test_a_different_key_already_in_the_keychain_is_kept(self, mac):
        mac.set(keychain={"istota.local/secret-key-istota": "ab" * 32})
        result = _create(mac)
        assert result.returncode == 78, result.stdout + result.stderr
        assert mac.state()["keychain"] == {"istota.local/secret-key-istota": "ab" * 32}

    def test_an_image_without_what_the_config_needs_is_rebuilt(self, mac):
        mac.set(check_image_rcs=[2])
        result = _create(mac)
        assert result.returncode == 0, result.stdout + result.stderr
        calls = mac.stack_calls()
        check = calls.index(["check-image"])
        assert calls[check + 1] == ["update", "v1.2.0"]

    def test_a_rerun_continues_from_the_first_missing_step(self, mac):
        assert _create(mac).returncode == 0
        mac.calls_path.unlink()
        result = mac.run("create")
        assert result.returncode == 0, result.stdout + result.stderr
        limactl = [c[0] for c in mac.calls("limactl") if c[0] != "shell"]
        assert "create" not in limactl and "start" not in limactl
        verbs = [c[0] for c in mac.stack_calls()]
        assert "update" not in verbs and "setup" not in verbs
        assert "check-image" in verbs
        assert ["/srv/app/istota/src/host/provision.sh"] in mac.vm_calls()

    def test_a_stopped_instance_is_started_and_not_created_again(self, mac):
        assert _create(mac).returncode == 0
        mac.set(instance={"name": "istota", "status": "Stopped"})
        mac.calls_path.unlink()
        assert mac.run("create").returncode == 0
        limactl = [c[0] for c in mac.calls("limactl") if c[0] != "shell"]
        assert "create" not in limactl and "start" in limactl

    def test_a_taken_port_is_refused_with_its_holder(self, mac):
        mac.set(port_holder="ollama")
        result = _create(mac)
        assert result.returncode == 78, result.stdout + result.stderr
        assert "ollama" in result.stderr
        assert not any(c[:1] == ["create"] for c in mac.calls("limactl"))

    def test_another_architecture_is_refused(self, mac):
        mac.set(arch="x86_64")
        result = _create(mac)
        assert result.returncode == 78, result.stdout + result.stderr
        assert mac.calls("limactl") == []

    def test_without_limactl_it_exits_69(self, mac):
        (Path(mac.env["PATH"].split(":")[0]) / "limactl").unlink()
        result = _create(mac)
        assert result.returncode == 69, result.stdout + result.stderr
        assert "brew install lima" in result.stderr

    def test_an_old_limactl_is_refused(self, mac):
        mac.set(version="1.2.1")
        result = _create(mac)
        assert result.returncode == 69, result.stdout + result.stderr

    def test_a_release_needs_its_signing_key(self, mac):
        result = mac.run("create", "--tag", "v1.2.0")
        assert result.returncode == 64, result.stdout + result.stderr
        assert not any(c[:1] == ["create"] for c in mac.calls("limactl"))

    def test_a_checkout_create_mounts_it_and_follows_main(self, mac):
        result = mac.run("create", "--repo", str(REPO))
        assert result.returncode == 0, result.stdout + result.stderr
        create = _limactl_create(mac)
        assert f"--param=ISTOTA_REPO={REPO.resolve()}" in create
        assert not any(a.startswith("--param=ISTOTA_TAG=") for a in create)
        assert "istota-repo" not in create[create.index("--set") + 1]
        assert ["update", "--unsigned", "latest"] in mac.stack_calls()

    def test_a_failed_step_names_itself_and_passes_its_status(self, mac):
        mac.set(update_rc=5)
        result = _create(mac)
        assert result.returncode == 5, result.stdout + result.stderr
        assert "istota-stack update v1.2.0" in result.stderr
        assert "exit 5" in result.stderr
        assert "building" in result.stderr
        assert ["setup", "--storage", "host-folder", "--ingress", "local",
                "--hostname", "localhost:8080"] not in mac.stack_calls()

    def test_the_enrolment_link_is_printed(self, mac):
        mac.set(auth_list="alice email=alice@example.com password_set=False disabled=False last_login=- live\n")
        result = _create(mac)
        assert result.returncode == 0, result.stdout + result.stderr
        assert ["exec", "istota", "auth", "invite", "alice", "--print-link"] in mac.stack_calls()
        assert "http://localhost:8080/istota/auth/set-password?token=t0k3n" in result.stdout

    def test_without_a_login_it_says_how_to_make_one(self, mac):
        result = _create(mac)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "istota auth add alice --email" in result.stdout


class TestTheScriptsRefresh:
    """After a verified update the Mac scripts are the ones from the VM's
    checkout, written beside the old ones and renamed over them."""

    @pytest.fixture
    def installed(self, mac):
        mac.bin_dir.mkdir(parents=True)
        for name in ("istota-stack", "istota"):
            (mac.bin_dir / name).write_text("#!/bin/bash\n# installed\n")
        return mac

    def test_update_refreshes_the_scripts_and_the_template(self, installed):
        installed.set(instance={"name": "istota", "status": "Running"})
        result = installed.run("update", "v1.3.0")
        assert result.returncode == 0, result.stdout + result.stderr
        assert installed.stack_calls() == [["update", "v1.3.0"]]
        for name in ("istota-stack", "istota"):
            path = installed.bin_dir / name
            assert path.read_text() == f"#!/bin/bash\n# copied from the VM: /srv/app/istota/src/host/mac/{name}\n"
            assert os.access(path, os.X_OK)
        template = installed.data_dir / "istota.yaml"
        assert "/srv/app/istota/src/host/lima/istota.yaml" in template.read_text()

    def test_a_failed_update_refreshes_nothing(self, installed):
        installed.set(instance={"name": "istota", "status": "Running"}, update_rc=65)
        result = installed.run("update", "v1.3.0")
        assert result.returncode == 65
        assert (installed.bin_dir / "istota-stack").read_text() == "#!/bin/bash\n# installed\n"

    def test_create_refreshes_them_too(self, installed):
        assert _create(installed).returncode == 0
        assert "copied from the VM" in (installed.bin_dir / "istota").read_text()

    def test_a_copy_that_is_not_a_script_is_not_installed(self, installed):
        installed.set(instance={"name": "istota", "status": "Running"},
                      guest_files={"/srv/app/istota/src/host/mac/istota-stack": ""})
        result = installed.run("update", "v1.3.0")
        assert result.returncode != 0
        assert (installed.bin_dir / "istota-stack").read_text() == "#!/bin/bash\n# installed\n"


class TestTheVmsLife:
    @pytest.fixture
    def running(self, mac):
        assert _create(mac, "--port", "8181").returncode == 0
        mac.calls_path.unlink()
        return mac

    def test_start(self, running):
        running.set(instance={"name": "istota", "status": "Stopped"})
        assert running.run("start").returncode == 0
        assert running.calls("limactl") == [["list", "--format", "{{.Status}}", "istota"],
                                            ["start", "--tty=false", "istota"]]

    def test_stop_takes_the_stack_down_first(self, running):
        assert running.run("stop").returncode == 0
        calls = running.calls("limactl")
        down = calls.index(["shell", "--workdir", "/", "istota", "sudo", "istota-stack", "down"])
        assert down < calls.index(["stop", "istota"])

    def test_stop_does_not_wait_forever_on_the_stack(self, running):
        running.set(down_sleep=30)
        result = running.run("stop", ISTOTA_STOP_TIMEOUT="1")
        assert result.returncode == 0, result.stderr
        assert ["stop", "istota"] in running.calls("limactl")

    @pytest.mark.parametrize("word,verb", [("on", "enable"), ("off", "disable")])
    def test_autostart(self, running, word, verb):
        assert running.run("autostart", word).returncode == 0
        assert running.calls("limactl")[-1] == ["autostart", verb, "istota"]

    def test_allow_and_deny_a_host_port(self, running):
        running.set(vm={**running.state()["vm"], "host_env": {"HOST_LOOPBACK_ALLOW": "1234"}})
        assert running.run("allow-host-port", "11434").returncode == 0
        assert running.state()["vm"]["host_env"]["HOST_LOOPBACK_ALLOW"] == "1234 11434"
        assert running.vm_calls()[-1] == ["systemctl", "restart", "istota-devbox-egress.service"]
        assert running.run("deny-host-port", "1234").returncode == 0
        assert running.state()["vm"]["host_env"]["HOST_LOOPBACK_ALLOW"] == "11434"

    @pytest.mark.parametrize("port", ["0", "65536", "80a", ""])
    def test_a_bad_port_is_a_usage_error(self, running, port):
        assert running.run("allow-host-port", port).returncode == 64
        assert running.calls("limactl") == []

    def test_status(self, running):
        result = running.run("status")
        assert result.returncode == 0, result.stderr
        assert "Running" in result.stdout
        assert "v1.2.0" in result.stdout
        assert _resolved(running.home / "Istota") in result.stdout
        assert ["compose", "ps"] in running.stack_calls()

    def test_status_says_when_the_marker_is_gone(self, running):
        (running.home / "Istota" / ".istota-workspace").unlink()
        result = running.run("status")
        assert "marker" in result.stdout

    def test_open(self, running):
        assert running.run("open").returncode == 0
        assert running.calls("open") == [["http://localhost:8181/istota"]]

    def test_open_files_reveals_the_users_folder(self, running):
        (running.home / "Istota" / "Users" / "alice").mkdir(parents=True)
        assert running.run("open", "--files").returncode == 0
        assert running.calls("open") == [[_resolved(running.home / "Istota" / "Users" / "alice")]]

    def test_shell(self, running):
        assert running.run("shell").returncode == 0
        assert running.calls("limactl")[-1] == ["shell", "istota"]


class TestDestroy:
    @pytest.fixture
    def made(self, mac):
        assert _create(mac).returncode == 0
        mac.calls_path.unlink()
        return mac

    def test_without_a_key_copy_it_refuses(self, made):
        made.set(keychain={})
        result = made.run("destroy", stdin="istota\n")
        assert result.returncode == 78, result.stderr
        assert "cannot be decrypted without this key" in result.stderr
        assert not any(c[:1] == ["delete"] for c in made.calls("limactl"))

    def test_force_deletes_without_a_key_copy(self, made):
        made.set(keychain={})
        result = made.run("destroy", "--force", stdin="istota\n")
        assert result.returncode == 0, result.stderr
        assert ["delete", "--force", "istota"] in made.calls("limactl")

    def test_the_name_has_to_be_typed(self, made):
        result = made.run("destroy", stdin="yes\n")
        assert result.returncode != 0
        assert not any(c[:1] == ["delete"] for c in made.calls("limactl"))

    def test_it_deletes_the_vm_and_leaves_the_folder_and_the_key(self, made):
        result = made.run("destroy", stdin="istota\n")
        assert result.returncode == 0, result.stderr
        assert ["delete", "--force", "istota"] in made.calls("limactl")
        assert (made.home / "Istota" / ".istota-workspace").is_file()
        assert made.state()["keychain"] == {"istota.local/secret-key-istota": KEY}
        assert "Keychain" in result.stdout


BACKUP_DATE = "2026-10-10"


def _back_up(mac: Mac, date: str = BACKUP_DATE) -> Path:
    """What db_backup leaves in the folder: a config copy and a framework snapshot."""
    folder = mac.home / "Istota"
    copy = folder / "Backups" / "config" / date
    copy.mkdir(parents=True)
    (copy / "config.toml").write_text("bot_name = 'Istota'\n")
    (copy / "admins").write_text("alice\n")
    snapshot = folder / "Backups" / "db" / "snapshots" / date / "framework"
    snapshot.mkdir(parents=True)
    (snapshot / "istota.db").write_bytes(b"SQLite format 3\x00")
    return folder


class TestCreateWithoutSetup:
    def test_no_setup_leaves_the_stack_unconfigured_and_stopped(self, mac):
        result = _create(mac, "--no-setup")
        assert result.returncode == 0, result.stdout + result.stderr
        verbs = [c[0] for c in mac.stack_calls()]
        assert verbs == ["host-env", "update"]
        vm = mac.vm_calls()
        assert ["/srv/app/istota/src/host/provision.sh"] in vm
        assert ["systemctl", "enable", "--now", "istota-stack"] not in vm
        assert "keychain" not in mac.state()

    def test_the_signing_key_is_kept_for_a_rerun(self, mac):
        assert _create(mac).returncode == 0
        mac.set(instance=None)
        mac.calls_path.unlink()
        result = mac.run("create")
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"--param=RELEASE_SIGNING_KEY={SIGNING_KEY}" in _limactl_create(mac)


class TestRestore:
    """A new VM from the folder's backups and the Keychain's copy of the key.
    Everything that can refuse is asked before the old VM is deleted."""

    @pytest.fixture
    def made(self, mac):
        assert _create(mac).returncode == 0
        _back_up(mac)
        mac.calls_path.unlink()
        return mac

    def test_it_replaces_the_vm_from_the_folder_and_the_keychain(self, made):
        result = made.run("restore", stdin="istota\n")
        assert result.returncode == 0, result.stdout + result.stderr
        limactl = [c[0] for c in made.calls("limactl") if c[0] not in ("shell", "list", "--version", "copy")]
        assert limactl[:3] == ["delete", "create", "start"], limactl
        calls = made.stack_calls()
        verbs = [c[0] for c in calls]
        assert "setup" not in verbs
        assert verbs.index("update") < verbs.index("restore") < verbs.index("check-image")
        assert ["restore"] in calls
        assert made.state()["restore_stdin"] == KEY + "\n"
        vm = made.vm_calls()
        restore = vm.index(["istota-stack", "restore"])
        assert restore < vm.index(["systemctl", "enable", "--now", "istota-stack"])
        assert made.state()["keychain"] == {"istota.local/secret-key-istota": KEY}

    def test_the_key_is_on_no_argv(self, made):
        assert made.run("restore", stdin="istota\n").returncode == 0
        for call in made.all_calls():
            assert not any(KEY in arg for arg in call["argv"]), call

    def test_a_date_and_force_reach_the_vm(self, made):
        result = made.run("restore", "--date", BACKUP_DATE, "--force", stdin="istota\n")
        assert result.returncode == 0, result.stdout + result.stderr
        assert ["restore", "--date", BACKUP_DATE, "--force"] in made.stack_calls()

    def test_with_no_vm_it_makes_one_without_asking(self, made):
        made.set(instance=None)
        result = made.run("restore")
        assert result.returncode == 0, result.stdout + result.stderr
        assert not any(c[:1] == ["delete"] for c in made.calls("limactl"))
        assert ["restore"] in made.stack_calls()

    def test_without_the_keychain_item_nothing_is_touched(self, made):
        made.set(keychain={})
        result = made.run("restore", stdin="istota\n")
        assert result.returncode == 78, result.stdout + result.stderr
        assert "Keychain" in result.stderr
        assert [c[0] for c in made.calls("limactl")
                if c[0] in ("delete", "create", "start", "shell")] == []

    def test_without_backups_nothing_is_touched(self, made):
        import shutil

        shutil.rmtree(made.home / "Istota" / "Backups" / "config")
        result = made.run("restore", stdin="istota\n")
        assert result.returncode == 78, result.stdout + result.stderr
        assert "Backups/config" in result.stderr
        assert not any(c[:1] in (["delete"], ["create"]) for c in made.calls("limactl"))

    def test_a_date_with_no_snapshot_is_refused_first(self, made):
        result = made.run("restore", "--date", "2026-01-01", stdin="istota\n")
        assert result.returncode == 78, result.stdout + result.stderr
        assert not any(c[:1] in (["delete"], ["create"]) for c in made.calls("limactl"))

    def test_a_folder_without_its_marker_is_refused_first(self, made):
        (made.home / "Istota" / ".istota-workspace").unlink()
        result = made.run("restore", stdin="istota\n")
        assert result.returncode == 78, result.stdout + result.stderr
        assert not any(c[:1] in (["delete"], ["create"]) for c in made.calls("limactl"))

    @pytest.mark.parametrize("date", ["yesterday", "2026-1-1", "2026-10-10x"])
    def test_a_date_that_is_not_one_is_a_usage_error(self, made, date):
        assert made.run("restore", "--date", date).returncode == 64
        assert made.calls("limactl") == []

    def test_the_name_has_to_be_typed_before_the_old_vm_goes(self, made):
        result = made.run("restore", stdin="yes\n")
        assert result.returncode != 0
        assert not any(c[:1] in (["delete"], ["create"]) for c in made.calls("limactl"))

    def test_a_failed_restore_does_not_start_the_stack(self, made):
        made.set(restore_rc=2)
        result = made.run("restore", stdin="istota\n")
        assert result.returncode == 2, result.stdout + result.stderr
        assert "istota-stack restore" in result.stderr
        assert ["systemctl", "enable", "--now", "istota-stack"] not in made.vm_calls()

    def test_a_restored_config_that_needs_claude_code_gets_its_rebuild(self, made):
        made.set(check_image_rcs=[2])
        result = made.run("restore", stdin="istota\n")
        assert result.returncode == 0, result.stdout + result.stderr
        calls = made.stack_calls()
        check = calls.index(["check-image"])
        assert calls[check + 1] == ["update", "v1.2.0"]

    def test_it_says_what_the_backups_do_not_hold(self, made):
        result = made.run("restore", stdin="istota\n")
        assert result.returncode == 0
        assert "secrets/" in result.stdout


STAGED = "/srv/app/istota/staging/AbCd1234/standalone"


def _import(mac: Mac, *args: str) -> subprocess.CompletedProcess:
    return mac.run("import-standalone", "--tag", "v1.2.0", "--signing-key", SIGNING_KEY, *args)


@dataclass
class Standalone:
    root: Path
    config: Path
    env_text: str
    conn: object

    def tree(self) -> dict[str, bytes]:
        """Every path under the root, with the bytes of each file that is not a
        database (an open WAL connection may touch those)."""
        out = {}
        for path in sorted(self.root.rglob("*")):
            rel = str(path.relative_to(self.root))
            out[rel] = b"" if path.is_dir() or ".db" in path.name else path.read_bytes()
        return out


@pytest.fixture
def standalone(mac):
    """A standalone install as `istota setup` leaves one: ~/.istota with the
    framework database in WAL mode holding a row only its -wal file has, a
    module database, scratch, transcripts and snapshots, and the config,
    istota.env and admins in ~/.config/istota."""
    import shutil
    import sqlite3

    from istota import db

    if shutil.which("sqlite3", path="/usr/bin:/bin") is None:
        pytest.skip("needs the sqlite3 command")
    root = (mac.home / ".istota")
    (root / "Users" / "alice" / "inbox").mkdir(parents=True)
    root = root.resolve()
    (root / "Users" / "alice" / "inbox" / "a.txt").write_text("from the standalone install")
    (root / "PERSONA.md").write_text("persona")
    (root / "tmp").mkdir()
    (root / "tmp" / "scratch").write_text("scratch")
    (root / "logs" / "alice").mkdir(parents=True)
    (root / "logs" / "alice" / "20261001_task-1-1.jsonl").write_text('{"type": "session"}\n')
    (root / "Backups" / "db" / "snapshots" / "2026-10-01" / "framework").mkdir(parents=True)
    (root / "modules" / "alice").mkdir(parents=True)
    module = sqlite3.connect(root / "modules" / "alice" / "feeds.db")
    module.execute("CREATE TABLE feeds (url TEXT)")
    module.execute("INSERT INTO feeds VALUES ('https://example.test/feed.xml')")
    module.commit()
    module.close()
    db.init_db(root / "istota.db")
    conn = sqlite3.connect(root / "istota.db")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("INSERT INTO user_profiles (user_id, display_name) VALUES ('alice', 'Alice')")
    conn.execute(
        "INSERT INTO tasks (id, status, source_type, user_id, prompt, attachments) VALUES "
        "(1, 'completed', 'web', 'alice', 'only in the wal', ?)",
        (json.dumps([f"{root}/Users/alice/inbox/a.txt"]),),
    )
    conn.commit()
    config_dir = mac.home / ".config" / "istota"
    config_dir.mkdir(parents=True)
    config = config_dir / "config.toml"
    config.write_text(
        f'db_path = "{root}/istota.db"\nworkspace_path = "{root}"\ntemp_dir = "{root}/tmp"\n\n'
        '[web]\nauth = "none"\n\n[brain]\nkind = "claude_code"\n\n'
        '[users.alice]\ndisplay_name = "Alice"\ntimezone = "UTC"\n'
    )
    env_text = f"ISTOTA_SECRET_KEY={KEY}\nISTOTA_WEB_INSECURE_COOKIES=1\nANTHROPIC_API_KEY=sk-test-only\n"
    (config_dir / "istota.env").write_text(env_text)
    (config_dir / "admins").write_text("alice\n")
    made = Standalone(root, config, env_text, conn)
    yield made
    conn.close()


class TestImportStandalone:
    """A standalone install into a new VM: everything that can refuse is asked
    before the VM is made, istota.env goes on stdin alone, the files are copied
    into the folder, and ~/.istota is never changed."""

    def test_it_moves_into_a_new_vm(self, mac, standalone):
        before = standalone.tree()
        result = _import(mac)
        assert result.returncode == 0, result.stdout + result.stderr
        assert any(c[:1] == ["create"] for c in mac.calls("limactl"))
        calls = mac.stack_calls()
        verbs = [c[0] for c in calls]
        assert "setup" not in verbs
        assert ["import-standalone", STAGED, "--hostname", "localhost:8080"] in calls
        assert ["unstage", STAGED] in calls
        assert ["stage", "--daemon", "/tmp/tmp.stub/standalone"] in calls
        assert mac.state()["import_stdin"] == standalone.env_text
        vm = mac.vm_calls()
        assert vm.index(["istota-stack", "import-standalone", STAGED, "--hostname", "localhost:8080"]) \
            < vm.index(["systemctl", "enable", "--now", "istota-stack"]) \
            < vm.index(["istota-stack", "exec", "istota", "doctor"])
        assert mac.state()["keychain"] == {"istota.local/secret-key-istota": KEY}
        assert "Trash" in result.stdout
        assert standalone.tree() == before

    def test_the_files_are_copied_and_the_state_stays_behind(self, mac, standalone):
        assert _import(mac).returncode == 0
        folder = mac.home / "Istota"
        assert (folder / "Users" / "alice" / "inbox" / "a.txt").read_text() == "from the standalone install"
        assert (folder / "PERSONA.md").read_text() == "persona"
        assert (folder / ".istota-workspace").is_file()
        for left in ("istota.db", "tmp", "logs", "modules", "Backups/db"):
            assert not (folder / left).exists(), left
        assert (standalone.root / "Users" / "alice" / "inbox" / "a.txt").exists()

    def test_no_secret_is_on_an_argv(self, mac, standalone):
        assert _import(mac).returncode == 0
        for call in mac.all_calls():
            for secret in (KEY, "sk-test-only"):
                assert not any(secret in arg for arg in call["argv"]), call

    def test_what_it_stages_is_what_the_vms_import_reads(self, mac, standalone, tmp_path):
        """The seam: the Mac's staging read by `istota migrate import-standalone`,
        with a row only the source's WAL file held."""
        from istota.maintenance import standalone_import as si
        from istota.setup_wizard import SECRET_NAMES

        vm_root = tmp_path / "vm-root"
        vm_root.mkdir()
        mac.set(vm_root=str(vm_root))
        result = _import(mac)
        assert result.returncode == 0, result.stdout + result.stderr
        staged = vm_root / "standalone"
        manifest = (staged / "MANIFEST").read_text()
        assert f"source_root={standalone.root}\n" in manifest
        assert (staged / "workspace-users").read_text() == "alice\n"
        assert (staged / "admins").read_text() == "alice\n"
        assert not (staged / "istota.env").exists()
        assert KEY not in "".join(p.read_text(errors="replace") for p in staged.rglob("*") if p.is_file())
        (snapshot,) = (staged / "db").iterdir()
        assert (snapshot / "alice" / "feeds.db").is_file()

        data, vm = tmp_path / "data", tmp_path / "vm"
        (data / "config").mkdir(parents=True)
        (vm / "secrets").mkdir(parents=True)
        for name in SECRET_NAMES:
            (vm / "secrets" / name).write_text("")
        out, err = io.StringIO(), io.StringIO()
        code = si.main(
            ["--source", str(staged), "--hostname", "localhost:8080", "--config",
             str(data / "config" / "config.toml"), "--vm-dir", str(vm), "--data-dir", str(data)],
            stdin=io.StringIO(mac.state()["import_stdin"]), out=out, err=err,
        )
        assert code == 0, out.getvalue() + err.getvalue()
        import sqlite3

        conn = sqlite3.connect(data / "db" / "istota.db")
        try:
            rows = conn.execute("SELECT prompt, attachments FROM tasks").fetchall()
        finally:
            conn.close()
        assert rows == [("only in the wal", json.dumps(["/mnt/shared/Users/alice/inbox/a.txt"]))]
        assert (data / ".secret_key").read_text() == KEY
        assert (vm / "secrets" / "anthropic_api_key").read_text() == "sk-test-only"

    def test_a_running_standalone_install_is_refused_first(self, mac, standalone):
        mac.set(lock_holder="4242")
        result = _import(mac)
        assert result.returncode == 78, result.stdout + result.stderr
        assert "istota serve" in result.stderr
        assert mac.calls("limactl") == []
        assert not (mac.home / "Istota").exists()

    def test_no_standalone_install_is_refused_first(self, mac, standalone):
        result = _import(mac, "--from", str(mac.home / "elsewhere"))
        assert result.returncode == 78, result.stdout + result.stderr
        assert mac.calls("limactl") == []

    def test_an_env_file_without_a_key_goes_to_the_vm_to_decide(self, mac, standalone):
        """An older install has no master key; the VM's import makes one when
        nothing in the database needs the old one, and refuses otherwise."""
        env_text = "ANTHROPIC_API_KEY=sk-test-only\n"
        (standalone.config.parent / "istota.env").write_text(env_text)
        result = _import(mac)
        assert result.returncode == 0, result.stdout + result.stderr
        assert mac.state()["import_stdin"] == env_text
        assert mac.state()["keychain"] == {"istota.local/secret-key-istota": KEY}

    def test_without_a_key_a_keychain_item_is_refused_first(self, mac, standalone):
        """The VM would make a new key, which the Keychain's item for this
        instance would then not match."""
        (standalone.config.parent / "istota.env").write_text("ANTHROPIC_API_KEY=sk-test-only\n")
        mac.set(keychain={"istota.local/secret-key-istota": "ab" * 32})
        result = _import(mac)
        assert result.returncode == 78, result.stdout + result.stderr
        assert "Keychain" in result.stderr
        assert mac.calls("limactl") == []

    def test_a_key_line_that_is_not_usable_is_refused_first(self, mac, standalone):
        (standalone.config.parent / "istota.env").write_text("ISTOTA_SECRET_KEY=short\nANTHROPIC_API_KEY=sk-test-only\n")
        result = _import(mac)
        assert result.returncode == 78, result.stdout + result.stderr
        assert "ISTOTA_SECRET_KEY" in result.stderr
        assert mac.calls("limactl") == []

    def test_a_different_key_in_the_keychain_is_refused_first(self, mac, standalone):
        mac.set(keychain={"istota.local/secret-key-istota": "ab" * 32})
        result = _import(mac)
        assert result.returncode == 78, result.stdout + result.stderr
        assert mac.calls("limactl") == []

    def test_a_refused_import_copies_nothing_and_starts_nothing(self, mac, standalone):
        mac.set(import_rc=1)
        result = _import(mac)
        assert result.returncode == 1, result.stdout + result.stderr
        assert ["unstage", STAGED] in mac.stack_calls()
        assert not (mac.home / "Istota" / "Users").exists()
        assert ["systemctl", "enable", "--now", "istota-stack"] not in mac.vm_calls()

    def test_a_file_already_in_the_folder_is_named_and_kept(self, mac, standalone):
        folder = mac.home / "Istota"
        (folder / "Users" / "alice" / "inbox").mkdir(parents=True)
        (folder / ".istota-workspace").write_text("")
        (folder / "Users" / "alice" / "inbox" / "a.txt").write_text("already here")
        result = _import(mac)
        assert result.returncode == 0, result.stdout + result.stderr
        assert (folder / "Users" / "alice" / "inbox" / "a.txt").read_text() == "already here"
        assert "Users/alice/inbox/a.txt" in result.stdout

    def test_a_failing_doctor_leaves_the_old_install_named(self, mac, standalone):
        mac.set(doctor_rc=1)
        result = _import(mac)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "Trash" not in result.stdout + result.stderr
        assert "run again" in result.stdout + result.stderr

    def test_a_dry_run_changes_nothing(self, mac, standalone):
        assert _create(mac, "--no-setup").returncode == 0
        mac.calls_path.unlink()
        result = _import(mac, "--dry-run")
        assert result.returncode == 0, result.stdout + result.stderr
        assert ["import-standalone", STAGED, "--hostname", "localhost:8080", "--dry-run"] in mac.stack_calls()
        assert not (mac.home / "Istota" / "Users").exists()
        assert ["systemctl", "enable", "--now", "istota-stack"] not in mac.vm_calls()
        assert not any(c[:1] == ["create"] for c in mac.calls("limactl"))


class TestTheStandaloneConfigIsNeverWritten:
    """`~/.config/istota/` is the standalone install's: `config.toml`,
    `istota.env` (the master key and every credential) and `install.json`. The
    Mac CLI keeps what `create` was given somewhere else, and nothing it does
    changes a byte there."""

    @staticmethod
    def _config_dir(mac: Mac) -> dict[str, bytes]:
        root = mac.home / ".config" / "istota"
        return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}

    def test_create_import_and_destroy_leave_it_byte_identical(self, mac, standalone):
        (mac.home / ".config" / "istota" / "install.json").write_text('{"source": "checkout"}\n')
        before = self._config_dir(mac)
        assert "istota.env" in before

        assert _create(mac, "--no-setup").returncode == 0
        assert self._config_dir(mac) == before
        result = _import(mac, "--dry-run")
        assert result.returncode == 0, result.stdout + result.stderr
        assert mac.state()["import_stdin"] == standalone.env_text
        assert self._config_dir(mac) == before
        result = _import(mac)
        assert result.returncode == 0, result.stdout + result.stderr
        assert mac.state()["import_stdin"] == standalone.env_text
        assert self._config_dir(mac) == before
        result = mac.run("destroy", stdin="istota\n")
        assert result.returncode == 0, result.stdout + result.stderr
        assert self._config_dir(mac) == before

    def test_an_import_with_no_vm_yet_carries_the_key(self, mac, standalone):
        """The import makes the VM itself; what goes on stdin is still the
        standalone istota.env, not create's state."""
        before = self._config_dir(mac)
        result = _import(mac)
        assert result.returncode == 0, result.stdout + result.stderr
        assert mac.state()["import_stdin"] == standalone.env_text
        assert self._config_dir(mac) == before

    def test_the_state_is_kept_in_a_directory_of_its_own(self, mac):
        assert _create(mac, "--port", "9090", "--no-setup").returncode == 0
        state = mac.home / ".config" / "istota-stack" / "istota.env"
        assert "PORT=9090\n" in state.read_text()
        assert not (mac.home / ".config" / "istota").exists()

    def test_a_state_file_at_the_old_path_is_read_and_left(self, mac):
        old = mac.home / ".config" / "istota" / "istota.env"
        old.parent.mkdir(parents=True)
        text = f"WORKSPACE={mac.home / 'Elsewhere'}\nPORT=9191\nTAG=v1.2.0\nREPO=\nSIGNING_KEY={SIGNING_KEY}\n"
        old.write_text(text)
        result = mac.run("status")
        assert result.returncode == 0, result.stderr
        assert "localhost:9191" in result.stdout
        assert str(mac.home / "Elsewhere") in result.stdout
        assert _create(mac, "--no-setup").returncode == 0
        assert old.read_text() == text
        assert "PORT=9191\n" in (mac.home / ".config" / "istota-stack" / "istota.env").read_text()

    def test_a_standalone_env_at_the_old_path_is_not_read_as_state(self, mac, standalone):
        """A key=value file with WORKSPACE in it is still not state when it
        carries the standalone install's names."""
        env = standalone.config.parent / "istota.env"
        env.write_text(f"ISTOTA_SECRET_KEY={KEY}\nWORKSPACE=/tmp/nowhere\nPORT=9393\n")
        result = mac.run("status")
        assert result.returncode == 0, result.stderr
        assert "localhost:8080" in result.stdout
        assert "/tmp/nowhere" not in result.stdout


class TestInstallLocal:
    def test_it_installs_the_mac_scripts_and_runs_create(self, mac):
        result = subprocess.run(
            [BASH, str(REPO / "install.sh"), "--local", "--tag", "v1.2.0",
             "--signing-key", SIGNING_KEY, "--port", "8282"],
            capture_output=True, text=True, env=mac.env, input="", timeout=120,
            # No controlling terminal, so install.sh cannot reattach one.
            start_new_session=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        for name in ("istota-stack", "istota"):
            installed = mac.bin_dir / name
            assert os.access(installed, os.X_OK), name
        template = mac.data_dir / "istota.yaml"
        create = _limactl_create(mac)
        assert create[-1] == str(template)
        assert "--param=ISTOTA_TAG=v1.2.0" in create
        assert ".portForwards[0].hostPort = 8282" in create[create.index("--set") + 1]
        # create refreshed the scripts from the VM's checkout after the update.
        assert "copied from the VM" in (mac.bin_dir / "istota-stack").read_text()

    def test_it_refuses_a_machine_that_is_not_an_apple_silicon_mac(self, mac):
        mac.set(system="Linux")
        result = subprocess.run(
            [BASH, str(REPO / "install.sh"), "--local", "--tag", "v1.2.0", "--signing-key", SIGNING_KEY],
            capture_output=True, text=True, env=mac.env, input="", timeout=60,
            start_new_session=True,
        )
        assert result.returncode != 0
        assert mac.calls("limactl") == []
        assert not (mac.bin_dir / "istota-stack").exists()
