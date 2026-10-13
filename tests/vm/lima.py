"""Drive a Lima VM provisioned the way an operator provisions one.

The vm tier's harness: the VM, the release it builds from, the stack modes it
switches between, and the few things in the VM that stand in for the world
outside it (an upstream proxy, an outside client, a metadata service, an ACME
CA, a Nextcloud). It imports no pytest; `tests/vm/conftest.py` holds the
fixtures, so a failure here is a raised `VmError` with the command's output.

**The release.** `istota-stack update` deploys only a tag signed by the key in
`/srv/app/istota/allowed_signers`, so the tier makes one: a git repository under
`ISTOTA_VM_WORKDIR` (default `~/.cache/istota-vmtier`) holding a snapshot of
this checkout's working tree, committed and tagged `vmtier-<tree>` with an SSH
key made there on first use. Lima mounts that repository read-only at
`/mnt/istota-repo` (`host/lima/istota.yaml`), `provision.sh` clones it to
`/srv/app/istota/src`, and `istota-stack update` fetches the tag from it, verifies
it and builds. The tag is named for the tree, so an unchanged tree reuses the
images the last session built.

**One VM, reused.** Created from `host/lima/istota.yaml` on the first session
(`provision.sh` runs at first boot), started and stopped after that. A mode
(local, proxied, direct, nextcloud) is applied by re-running `istota-stack
setup` with that mode's answers, `istota-stack apply` for what setup does not
ask, `provision.sh` for `host.env`, and a restart of `istota-stack.service`;
the applied mode is recorded in the VM, so a session in the same mode on the
same tag applies nothing.

Everything runs as root in the VM through `limactl shell ... sudo bash -s`, the
script on stdin.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "host" / "lima" / "istota.yaml"

DEFAULT_NAME = "istota-vm-1"
STACK = "/srv/app/istota"
#: The Nextcloud rclone mount, outside the stack directory (provision.sh's default).
MOUNT = "/srv/mount/nextcloud/content"
#: What the tier keeps in the VM beside the stack: fixture configs, the Pebble
#: root, markers. Never read by the stack.
SCRATCH = "/srv/istota-vmtier"
MODE_FILE = f"{SCRATCH}/mode.json"
REPO_MOUNT = "/mnt/istota-repo"
#: The template mounts the checkout and the Mac workspace folder; a Lima mount
#: cannot be optional, so the tier's VM drops the workspace at create and
#: shares a folder in only for the host-folder mode (`share_host_folder`).
DROP_WORKSPACE_MOUNT = 'del(.mounts[] | select(.mountPoint == "/srv/mount/host-folder"))'

#: The first admin every mode's setup names, and the devbox users.
USER = "admin"
SECOND_DEVBOX_USER = "bob"
#: The placeholder the native brain sends; the scripted endpoint checks nothing.
NATIVE_KEY = "vmtier-native-key-placeholder"
DOMAIN = "istota.test"

#: Proxied ingress: a bridge in the VM with an upstream and an outsider, each a
#: network namespace. LISTEN_ADDR is the VM's own address on that bridge.
PROXIED_LISTEN = "10.99.0.1"
UPSTREAM = "10.99.0.2"
OUTSIDER = "10.99.0.3"
PROXIED_PORT = 8080

#: The devbox egress stand-ins: a namespace answering on port 80 at the cloud
#: metadata address and at the RFC 1918 address doctor's probe dials.
METADATA_ADDR = "169.254.169.254"
PRIVATE_ADDR = "10.0.0.1"

#: The Nextcloud fixture's published port in the VM.
NC_PORT = 8090
NC_BOT = "istota-bot"

CREATE_TIMEOUT = 1800
BUILD_TIMEOUT = 3600


class VmError(RuntimeError):
    """A command in the VM, or limactl itself, did not do what was asked."""


def _workdir() -> Path:
    return Path(os.environ.get("ISTOTA_VM_WORKDIR") or Path.home() / ".cache" / "istota-vmtier")


def vm_name() -> str:
    return os.environ.get("ISTOTA_VM_NAME", "").strip() or DEFAULT_NAME


def control() -> str:
    """The negative control a run asked for (`scripts/test-vm-negative-control.sh`)."""
    return os.environ.get("ISTOTA_VM_CONTROL", "").strip()


def _run_host(argv: list[str], *, timeout: int = 120, check: bool = True,
              cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, cwd=cwd,
        env={**os.environ, **(env or {})},
    )
    if check and result.returncode != 0:
        raise VmError(
            f"{' '.join(argv[:6])} exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout[-4000:]}\n--- stderr ---\n{result.stderr[-4000:]}"
        )
    return result


# --- the VM -------------------------------------------------------------------


@dataclass
class Vm:
    name: str

    def run(self, script: str, *, timeout: int = 600, check: bool = True,
            env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        """Run a bash script as root in the VM, with `set -euo pipefail`."""
        exports = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in (env or {}).items())
        body = "set -euo pipefail\n" + exports + script + "\n"
        result = subprocess.run(
            ["limactl", "shell", "--workdir", "/", self.name, "sudo", "bash", "-s"],
            input=body, capture_output=True, text=True, timeout=timeout,
        )
        if check and result.returncode != 0:
            raise VmError(
                f"in {self.name}, exit {result.returncode}:\n{script.strip()[:1500]}\n"
                f"--- stdout ---\n{result.stdout[-6000:]}\n--- stderr ---\n{result.stderr[-6000:]}"
            )
        return result

    def out(self, script: str, **kwargs) -> str:
        return self.run(script, **kwargs).stdout.strip()

    def ok(self, script: str, **kwargs) -> bool:
        return self.run(script, check=False, **kwargs).returncode == 0

    def write(self, path: str, data: str | bytes, *, mode: str = "0644", owner: str = "root") -> None:
        raw = data.encode() if isinstance(data, str) else data
        encoded = base64.b64encode(raw).decode()
        self.run(
            f"install -d {shlex.quote(os.path.dirname(path))}\n"
            f"base64 -d > {shlex.quote(path)}.tmp <<'EOF'\n{encoded}\nEOF\n"
            f"chmod {mode} {shlex.quote(path)}.tmp\n"
            f"chown {owner}:{owner} {shlex.quote(path)}.tmp\n"
            f"mv {shlex.quote(path)}.tmp {shlex.quote(path)}\n"
        )

    def read(self, path: str) -> str:
        return self.run(f"cat {shlex.quote(path)} 2>/dev/null || true").stdout

    # -- limactl

    def info(self) -> dict | None:
        result = _run_host(["limactl", "list", "--json"], check=False)
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("name") == self.name:
                return entry
        return None

    def status(self) -> str:
        entry = self.info()
        return entry.get("status", "") if entry else ""

    def start(self) -> None:
        if self.status() == "Running":
            return
        # A start whose probe reports late still leaves a running VM (seen in
        # Stage 6), so the answer is read from the VM rather than from the exit.
        _run_host(["limactl", "start", "--tty=false", self.name], timeout=CREATE_TIMEOUT, check=False)
        self._wait_running()

    def stop(self) -> None:
        if self.status() == "Running":
            _run_host(["limactl", "stop", self.name], timeout=600, check=False)

    def _wait_running(self) -> None:
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if self.status() == "Running" and self.ok("true", timeout=60):
                return
            time.sleep(5)
        raise VmError(f"{self.name} did not come up: status {self.status()!r}")

    def set_mounts(self, mounts: list[dict]) -> None:
        """Replace the VM's Lima mounts; the VM is restarted to apply them."""
        self.stop()
        expression = ".mounts = " + json.dumps(mounts)
        _run_host(["limactl", "edit", "--tty=false", "--set", expression, self.name], timeout=120)
        self.start()


# --- the release ----------------------------------------------------------------


@dataclass(frozen=True)
class Release:
    repo: Path
    tag: str
    signing_key: str  # the public key, one line


def _git(repo: Path, *args: str, work_tree: Path | None = None, timeout: int = 300,
         check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    argv = [
        "git", f"--git-dir={repo / '.git'}", f"--work-tree={work_tree or repo}",
        "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
        "-c", "user.name=istota vm tier", "-c", "user.email=vmtier@example.invalid",
        *args,
    ]
    return _run_host(argv, timeout=timeout, check=check, cwd=work_tree or repo,
                     env={"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", **(env or {})})


def snapshot_release() -> Release:
    """Commit this checkout's working tree into the tier's repository, signed."""
    workdir = _workdir()
    key = workdir / "signing" / "release"
    if not key.exists():
        key.parent.mkdir(parents=True, exist_ok=True)
        _run_host(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "istota-vmtier-release", "-f", str(key)])
    public = (key.parent / "release.pub").read_text().strip()

    repo = workdir / "repo"
    if not (repo / ".git").is_dir():
        repo.mkdir(parents=True, exist_ok=True)
        _run_host(["git", "init", "-q", "-b", "main", str(repo)],
                  env={"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
    # The index takes this checkout's files, honouring its .gitignore; the
    # commit is made only when the tree changed.
    _git(repo, "add", "-A", work_tree=REPO, timeout=900)
    tree = _git(repo, "write-tree").stdout.strip()
    head = _git(repo, "rev-parse", "-q", "--verify", "HEAD^{tree}", check=False).stdout.strip()
    if head != tree:
        _git(repo, "commit", "-q", "-m", f"vm tier snapshot of {REPO.name}", work_tree=REPO, timeout=300)
    tag = f"vmtier-{tree[:12]}"
    if _git(repo, "rev-parse", "-q", "--verify", f"refs/tags/{tag}", check=False).returncode != 0:
        _git(repo, "-c", "gpg.format=ssh", "-c", f"user.signingkey={key}",
             "tag", "-s", "-m", f"vm tier release {tag}", tag, "HEAD")
    # The repository's own files are what Lima mounts and provision.sh runs.
    _git(repo, "reset", "-q", "--hard", "HEAD")
    _git(repo, "clean", "-fdqx")
    return Release(repo=repo, tag=tag, signing_key=public)


#: The line the second ref appends to host/istota-stack, followed by its tag.
NEXT_REF_MARKER = "# vm tier: the second ref"


def next_ref(release: Release) -> str:
    """A second ref on top of the release, for `istota-stack update --unsigned`.

    The release's tree with one line appended to `host/istota-stack`, committed
    beside the branch rather than on it (the mounted work tree stays at the
    release) and tagged lightweight, which only `--unsigned` installs. No image
    copies `host/`, so its builds are cache hits; the changed script is what
    shows the update provisioned the host from the new ref.
    """
    repo = release.repo
    tag = f"{release.tag}-next"
    if _git(repo, "rev-parse", "-q", "--verify", f"refs/tags/{tag}", check=False).returncode == 0:
        return tag
    base = _git(repo, "rev-parse", f"{release.tag}^{{commit}}").stdout.strip()
    mode = _git(repo, "ls-tree", base, "host/istota-stack").stdout.split()[0]
    script = _git(repo, "show", f"{base}:host/istota-stack").stdout
    scratch = _workdir() / "next-ref"
    scratch.mkdir(parents=True, exist_ok=True)
    changed = scratch / "istota-stack"
    changed.write_text(script + f"{NEXT_REF_MARKER} {tag}\n")
    blob = _git(repo, "hash-object", "-w", str(changed)).stdout.strip()
    index = {"GIT_INDEX_FILE": str(scratch / "index")}
    _git(repo, "read-tree", f"{base}^{{tree}}", env=index)
    _git(repo, "update-index", "--cacheinfo", f"{mode},{blob},host/istota-stack", env=index)
    tree = _git(repo, "write-tree", env=index).stdout.strip()
    commit = _git(repo, "commit-tree", tree, "-p", base, "-m", f"vm tier second ref over {release.tag}").stdout.strip()
    _git(repo, "tag", tag, commit)
    return tag


# --- creating and provisioning the VM -------------------------------------------


def ensure_vm(release: Release) -> Vm:
    vm = Vm(vm_name())
    entry = vm.info()
    if entry is None:
        argv = ["limactl", "create", "--tty=false", f"--name={vm.name}",
                f"--param=ISTOTA_REPO={release.repo}", f"--set={DROP_WORKSPACE_MOUNT}"]
        memory = os.environ.get("ISTOTA_VM_MEMORY", "").strip()
        if memory:
            argv.append(f"--set=.memory = {json.dumps(memory)}")
        argv.append(str(TEMPLATE))
        _run_host(argv, timeout=CREATE_TIMEOUT)
    elif str(release.repo) not in json.dumps(entry.get("config", {})):
        raise VmError(
            f"{vm.name} exists but does not mount {release.repo}. Delete it "
            f"(`limactl delete -f {vm.name}`) or set ISTOTA_VM_NAME to another name."
        )
    vm.start()
    # The template's probe waits for provision.sh to install istota-stack; a VM
    # created by an interrupted session may not have got that far.
    if not vm.ok("command -v istota-stack >/dev/null"):
        vm.run(f"{REPO_MOUNT}/host/provision.sh", timeout=CREATE_TIMEOUT)
    return vm


def env_file_set(vm: Vm, path: str, values: dict[str, str]) -> None:
    """Set KEY=VALUE lines in a host env file, keeping every other line."""
    payload = json.dumps(values)
    vm.run(f"""
python3 - {shlex.quote(path)} <<'PY'
import json, os, sys
path = sys.argv[1]
values = json.loads({payload!r})
lines = open(path).read().splitlines() if os.path.exists(path) else []
seen = set()
out = []
for line in lines:
    key = line.split("=", 1)[0]
    if "=" in line and not line.startswith("#") and key in values:
        if key in seen:
            continue
        line = f"{{key}}={{values[key]}}"
        seen.add(key)
    out.append(line)
out += [f"{{k}}={{v}}" for k, v in values.items() if k not in seen]
open(path, "w").write("\\n".join(out) + "\\n")
PY
""")


def ensure_release(vm: Vm, release: Release) -> None:
    """The VM's checkout at the release tag, its images built, its host units current."""
    vm.run(f"install -d -m 0755 {SCRATCH} {STACK}")
    env_file_set(vm, f"{STACK}/host.env", {"RELEASE_SIGNING_KEY": release.signing_key})
    # A VM left in the nextcloud mode boots with the fixture down, and
    # provision.sh's `enable --now` of the mount then fails against it.
    if vm.ok(f"grep -qx STORAGE=nextcloud {STACK}/host.env"):
        ensure_nextcloud(vm)
    # From the mounted snapshot, which is this session's tree: installs this
    # tree's istota-stack and units, and writes allowed_signers.
    vm.run(f"{REPO_MOUNT}/host/provision.sh", timeout=CREATE_TIMEOUT)
    current = vm.out(f"awk -F= '$1 == \"ISTOTA_TAG\" {{ print $2 }}' {STACK}/.env")
    if current != release.tag:
        # `update` brings the stack up in the mode the VM was left in, and the
        # proxied listener's address lives on a namespace a VM restart drops.
        ensure_proxied_namespaces(vm)
        vm.run(f"istota-stack update {shlex.quote(release.tag)}", timeout=BUILD_TIMEOUT)


# --- the stack ------------------------------------------------------------------


STACK_COMMAND = "/usr/local/sbin/istota-stack"
#: The daemon and web image; `istota-stack` tags it with the deployed ref.
IMAGE = "istota"

#: The update controls (`tests/vm/test_update.py`): each replaces one fragment
#: of `istota-stack` in a copy the update case runs instead of the installed one.
UPDATE_CONTROLS: dict[str, tuple[str, str]] = {
    # .env names the new ref and nothing starts it.
    "update-without-up": (
        '    if [ -f "${CONFIG_DIR}/config.toml" ]; then\n        stack_up\n',
        '    if [ -f "${CONFIG_DIR}/config.toml" ]; then\n        :\n',
    ),
    # The stack moves and the host's scripts and units stay at the old ref.
    "update-without-reprovision": (
        '    log "now on ${tag} (previous: ${previous:-none})"\n    reprovision_if_changed\n',
        '    log "now on ${tag} (previous: ${previous:-none})"\n',
    ),
    # The stack returns to the release and the host's scripts stay at the newer ref.
    "rollback-without-reprovision": (
        '    log "rolled back to ${previous} (previous: ${current})"\n    reprovision_if_changed\n',
        '    log "rolled back to ${previous} (previous: ${current})"\n',
    ),
    # The workspace is copied and the mount turned off, and config.toml still
    # names the mount (`tests/vm/test_localize.py`).
    "localize-without-config": (
        '    ISTOTA_CONFIG_READ_ONLY=false oneshot "$(main_service)" istota workspace localize-config '
        '--workspace "$LOCAL_WORKSPACE" \\\n',
        '    true \\\n',
    ),
    # A content-only settings change restarts the stack as a config change
    # does (`tests/vm/test_settings_apply.py`).
    "apply-restart-on-any-change": (
        '            2:"0 1 0 0") log "the changes are read without a restart; nothing was restarted." ;;\n',
        '            2:"0 1 0 0") restart_for_config ;;\n',
    ),
    # The sidecar is staged and bound, and its path never reaches istota apply.
    "apply-sidecar-not-passed": (
        '        secrets=(--secrets-file "/secrets/$(basename "$2")")\n',
        '        secrets=()\n',
    ),
}


#: The Mac's `istota-stack` (row L4), run on this Mac against the tier's VM.
MAC_CLI = REPO / "host" / "mac" / "istota-stack"
MAC_SHIM = REPO / "host" / "mac" / "istota"

#: Row L4's control: a Mac CLI whose `exec` forward goes to `compose exec
#: daemon` itself, past the VM's `istota-stack exec` and its `istota-drop`.
MAC_SHIM_CONTROL: tuple[str, str] = (
    '    exec limactl shell --workdir / "$INSTANCE" sudo istota-stack "$@"\n',
    '    if [ "$1" = exec ]; then shift; set -- compose exec -T daemon "$@"; fi\n'
    '    exec limactl shell --workdir / "$INSTANCE" sudo istota-stack "$@"\n',
)


def mac_cli(name: str = "") -> Path:
    """`host/mac/istota-stack`, or a copy beside the shim with control `name`
    applied, so the shim's own lookup of its sibling finds it."""
    if name != "mac-shim-without-drop":
        return MAC_CLI
    old, new = MAC_SHIM_CONTROL
    text = MAC_CLI.read_text()
    if text.count(old) != 1:
        raise VmError("the mac-shim-without-drop control does not apply to host/mac/istota-stack")
    bindir = _workdir() / "mac-shim-control"
    bindir.mkdir(parents=True, exist_ok=True)
    copy = bindir / "istota-stack"
    copy.write_text(text.replace(old, new))
    copy.chmod(0o755)
    shim = bindir / "istota"
    shim.write_text(MAC_SHIM.read_text())
    shim.chmod(0o755)
    return copy


def stack_command(vm: Vm, name: str = "") -> str:
    """The installed `istota-stack`, or a copy with control `name` applied."""
    if not name:
        return STACK_COMMAND
    old, new = UPDATE_CONTROLS[name]
    copy = f"{SCRATCH}/istota-stack.{name}"
    vm.run(f"""
python3 - {STACK_COMMAND} {copy} <<'PY'
import json, sys
old, new = json.loads({json.dumps([old, new])!r})
text = open(sys.argv[1]).read()
if text.count(old) != 1:
    sys.exit("the control does not apply to " + sys.argv[1])
open(sys.argv[2], "w").write(text.replace(old, new))
PY
chmod 0755 {copy}
""")
    return copy


def env_value(vm: Vm, path: str, key: str) -> str:
    """One key of an env file in the VM, '' when absent."""
    return vm.out(f"awk -F= -v k={shlex.quote(key)} '$1 == k {{ sub(/^[^=]*=/, \"\"); v = $0 }} END {{ print v }}' "
                  f"{shlex.quote(path)} 2>/dev/null || true")


def running_image(vm: Vm, service: str) -> str:
    """The image reference a stack service's container was created from, or ''."""
    cid = container(vm, service)
    return vm.out(f"docker inspect -f '{{{{.Config.Image}}}}' {cid}") if cid else ""


def stack_exec(vm: Vm, command: str, *, timeout: int = 300, check: bool = True,
               stdin_file: str = "") -> subprocess.CompletedProcess:
    """A command in the running daemon container, as uid 10001 (`istota-stack exec`)."""
    redirect = f" < {shlex.quote(stdin_file)}" if stdin_file else " < /dev/null"
    return vm.run(f"istota-stack exec sh -c {shlex.quote(command)}{redirect}", timeout=timeout, check=check)


def doctor(vm: Vm, only: str, *, env: str = "") -> dict:
    """One doctor check's result through the VM's `istota` wrapper."""
    prefix = f"env {env} " if env else ""
    result = stack_exec(vm, f"{prefix}istota doctor --only {only} --json", check=False)
    try:
        payload = json.loads(result.stdout[result.stdout.find("{"):] if "{" in result.stdout else result.stdout)
    except json.JSONDecodeError:
        payload = None
    if payload is None:
        try:
            payload = json.loads(result.stdout[result.stdout.find("["):])
        except (json.JSONDecodeError, ValueError):
            raise VmError(f"doctor --only {only} printed no JSON:\n{result.stdout}\n{result.stderr}")
    checks = payload.get("checks", payload) if isinstance(payload, dict) else payload
    for check in checks:
        if check.get("name") == only:
            return check
    raise VmError(f"doctor --only {only} did not report it:\n{result.stdout}")


def container(vm: Vm, service: str) -> str:
    """The running container of a stack service, or ''."""
    return vm.out(
        f"docker ps -q --filter label=com.docker.compose.project=istota "
        f"--filter label=com.docker.compose.service={shlex.quote(service)} | head -1"
    )


def wait_healthy(vm: Vm, *, timeout: int = 900) -> None:
    deadline = time.monotonic() + timeout
    state = ""
    while time.monotonic() < deadline:
        cid = container(vm, "daemon")
        if cid:
            state = vm.out(f"docker inspect -f '{{{{.State.Health.Status}}}}' {cid}", check=False)
            if state == "healthy":
                return
        time.sleep(5)
    logs = vm.out("istota-stack compose logs --tail 60 daemon 2>&1 || true", check=False)
    raise VmError(f"the daemon service did not become healthy (last state {state!r}):\n{logs}")


def wait_for(predicate, *, timeout: float, interval: float = 3.0, what: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise VmError(f"timed out after {timeout:.0f}s waiting for {what or predicate}")


def vm_address(vm: Vm) -> str:
    """The VM's own address on its primary interface."""
    return vm.out("ip -4 -o route get 1.1.1.1 | sed -n 's/.* src \\([0-9.]*\\).*/\\1/p'")


def mac_address(vm: Vm) -> str:
    """The Mac as the VM reaches it: Lima's host address, which forwards to its loopback."""
    return vm.out("getent hosts host.lima.internal | awk '{ print $1 }'")


# --- modes ----------------------------------------------------------------------


@dataclass
class Mode:
    name: str
    setup_args: list[str]
    setup_env: dict[str, str] = field(default_factory=dict)
    host_env: dict[str, str] = field(default_factory=dict)
    #: Merged over the config setup wrote, through `istota-stack apply`.
    config: dict = field(default_factory=dict)
    #: Rebuild after setup (profiles and devboxes add images).
    build: bool = False
    #: Applied by the management role (`ansible/`) from the same answers,
    #: instead of `istota-stack setup`. See `converge_role`.
    by_role: bool = False
    rclone_remote: str = ""
    rclone_conf: str = ""

    def signature(self, tag: str) -> str:
        return json.dumps({
            "mode": self.name, "tag": tag, "args": self.setup_args,
            "host_env": self.host_env, "config": self.config, "by_role": self.by_role,
        }, sort_keys=True)


def common_args(model_url: str) -> list[str]:
    return [
        "--yes", "--force", "--brain", "native",
        "--native-base-url", model_url, "--native-model", "scripted-test-model",
        "--user", USER, "--user-email", f"{USER}@example.test",
        "--no-money", "--no-health", "--no-feeds", "--no-briefings",
    ]


def local_mode(model_url: str) -> Mode:
    return Mode(
        name="local",
        setup_args=common_args(model_url) + [
            # No signaling profile: the wizard refuses it without Talk.
            "--ingress", "local", "--hostname", "localhost:8080", "--developer",
            "--profile", "browser",
        ],
        config={"devbox": {"enabled": True, "users": [USER, SECOND_DEVBOX_USER]}},
        build=True,
    )


def proxied_mode(model_url: str) -> Mode:
    return Mode(
        name="proxied",
        setup_args=common_args(model_url) + [
            "--ingress", "proxied", "--hostname", DOMAIN, "--upstream-proxy", UPSTREAM,
            "--listen-addr", PROXIED_LISTEN, "--listen-port", str(PROXIED_PORT),
        ],
        by_role=True,
    )


def direct_mode(model_url: str) -> Mode:
    return Mode(
        name="direct",
        setup_args=common_args(model_url) + [
            "--ingress", "direct", "--hostname", DOMAIN, "--tls-cert-source", "acme",
        ],
        host_env={
            "ACME_SERVER": "https://localhost:14000/dir",
            "REQUESTS_CA_BUNDLE": f"{SCRATCH}/pebble.minica.pem",
        },
    )


def nextcloud_mode(model_url: str, nc_url: str, bot_password: str, rclone_conf: str) -> Mode:
    return Mode(
        name="nextcloud",
        setup_args=common_args(model_url) + [
            "--ingress", "local", "--hostname", "localhost:8080",
            "--nextcloud-url", nc_url, "--nextcloud-user", NC_BOT,
            "--nextcloud-dav-prefix", "Shared Files", "--no-nextcloud-auto-share", "--no-talk",
        ],
        setup_env={"ISTOTA_NEXTCLOUD_APP_PASSWORD": bot_password},
        by_role=True,
        rclone_remote="nextcloud",
        rclone_conf=rclone_conf,
    )


#: Where Lima shares the Mac folder in, in the host-folder mode (provision.sh's
#: directory for STORAGE=host-folder).
HOST_FOLDER_MOUNT = "/srv/mount/host-folder"
#: The marker the entrypoint, doctor and the backup ask the folder for.
HOST_FOLDER_MARKER = ".istota-workspace"


def host_folder_mode(model_url: str, *, gateway: str, allow_ports: list[int]) -> Mode:
    """The local VM's shape: local ingress, the workspace a folder on the Mac,
    and the Mac's loopback closed to every container (row L2) but for
    `allow_ports`, which holds the scripted model's port. The browser profile
    is on, since row L2 is witnessed from the browser container too."""
    return Mode(
        name="host-folder",
        setup_args=common_args(model_url) + [
            "--ingress", "local", "--hostname", "localhost:8080", "--storage", "host-folder",
            "--profile", "browser",
        ],
        host_env={
            "HOST_LOOPBACK_BLOCK": "1",
            "HOST_GATEWAY_ADDR": gateway,
            "HOST_LOOPBACK_ALLOW": " ".join(str(port) for port in allow_ports),
        },
        build=True,
    )


def host_folder() -> Path:
    """The Mac folder the host-folder mode shares in, kept between sessions so
    an unchanged mount list costs no VM restart."""
    folder = _workdir() / "host-folder"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _mounts(release: Release, *extra: dict) -> list[dict]:
    return [{"location": str(release.repo), "mountPoint": REPO_MOUNT, "writable": False}, *extra]


def template_mounts(release: Release, folder: Path, *, extra_base: str = "") -> list[dict]:
    """The mounts Lima resolves from `host/lima/istota.yaml` for the checkout
    and `folder`, read off a scratch instance that is created and never
    started. `extra_base` adds a base template, as row L3's control does."""
    name = f"{vm_name()}-template"
    template = TEMPLATE
    if extra_base:
        # A copy with the base added: `--set` applies after Lima has embedded
        # the bases, and refuses to touch `base:`.
        text = TEMPLATE.read_text()
        base = "base:\n  - template:_images/debian-13\n"
        if text.count(base) != 1:
            raise VmError(f"the template's base is not {base!r}")
        template = _workdir() / "istota-control.yaml"
        template.write_text(text.replace(base, f"{base}  - {extra_base}\n"))
    argv = ["limactl", "create", "--tty=false", f"--name={name}",
            f"--param=ISTOTA_REPO={release.repo}", f"--param=WORKSPACE={folder}", str(template)]
    _run_host(["limactl", "delete", "--force", name], check=False)
    try:
        _run_host(argv, timeout=300)
        mounts = ((Vm(name).info() or {}).get("config") or {}).get("mounts") or []
    finally:
        _run_host(["limactl", "delete", "--force", name], check=False)
    if not mounts:
        raise VmError(f"limactl resolved no mounts for {name}")
    return [{"location": m["location"], "mountPoint": m.get("mountPoint") or m["location"],
             "writable": bool(m.get("writable"))} for m in mounts]


def _current_mounts(vm: Vm) -> list[tuple]:
    mounts = ((vm.info() or {}).get("config") or {}).get("mounts") or []
    return [(m.get("location"), m.get("mountPoint"), bool(m.get("writable"))) for m in mounts]


def set_mounts_if_changed(vm: Vm, mounts: list[dict]) -> None:
    """`Vm.set_mounts`, skipped when the VM already has exactly these."""
    wanted = [(m["location"], m["mountPoint"], m["writable"]) for m in mounts]
    if _current_mounts(vm) != wanted:
        vm.set_mounts(mounts)


def share_host_folder(vm: Vm, release: Release, folder: Path) -> None:
    """The VM with `folder` shared in writable at HOST_FOLDER_MOUNT, beside the
    checkout: the template's two mounts, as Lima resolves them."""
    set_mounts_if_changed(vm, template_mounts(release, folder))


def unshare_host_folder(vm: Vm, release: Release) -> None:
    """Back to the checkout alone, which row 7 asserts, leaving the stack for
    the next mode to set up: a host folder in host.env would otherwise be kept
    by its setup, and the stack would refuse to start without the folder. The
    loopback block goes too, or the other modes' tasks would lose the model
    whenever its port moved.

    config.toml leaves the mode with host.env, and first: the restart below
    starts the enabled stack, and STORAGE=local beside a config still naming
    /mnt/shared as its workspace mount is how a daemon once wrote a workspace
    into the rclone mount's empty mount point, which rclone then refused."""
    from istota import setup_wizard

    vm.run(f"systemctl stop istota-stack.service || true; rm -f {MODE_FILE}", timeout=600)
    apply_config(vm, {"workspace_path": setup_wizard.CONTAINER_LOCAL_WORKSPACE},
                 drop=("workspace_mount_path",))
    env_file_set(vm, f"{STACK}/host.env", {"STORAGE": "local", "HOST_LOOPBACK_BLOCK": "0"})
    vm.run(f"{STACK}/src/host/provision.sh", timeout=1800)
    set_mounts_if_changed(vm, _mounts(release))


def nextcloud_wizard_mode(model_url: str, nc_url: str, bot_password: str) -> Mode:
    """Full integration as `istota-stack setup` writes it, not the role.

    `localize-workspace` refuses a config `istota apply` wrote, so the move
    needs the wizard's. The rclone.conf goes in through `prepare`.
    """
    return Mode(
        name="nextcloud-wizard",
        setup_args=common_args(model_url) + [
            "--ingress", "local", "--hostname", "localhost:8080",
            "--nextcloud-url", nc_url, "--nextcloud-user", NC_BOT, "--workspace", "mount",
            "--nextcloud-dav-prefix", "Shared Files", "--no-nextcloud-auto-share", "--no-talk",
        ],
        setup_env={"ISTOTA_NEXTCLOUD_APP_PASSWORD": bot_password},
        host_env={"RCLONE_REMOTE": "nextcloud"},
    )


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def apply_config(vm: Vm, delta: dict, *, drop: tuple[str, ...] = ()) -> int:
    """Merge `delta` over config.toml, less the top-level keys in `drop`,
    through `istota-stack apply`; its exit code."""
    current = json.loads(vm.out(
        f"python3 -c 'import json,tomllib;print(json.dumps(tomllib.load(open(\"{STACK}/config/config.toml\",\"rb\"))))'"
    ))
    merged = _deep_merge(current, delta)
    for key in drop:
        merged.pop(key, None)
    plan = {"config": merged}
    vm.write(f"{SCRATCH}/plan.json", json.dumps(plan), mode="0600")
    result = vm.run(f"istota-stack apply {SCRATCH}/plan.json", check=False, timeout=1800)
    if result.returncode not in (0, 2):
        raise VmError(f"istota-stack apply exited {result.returncode}:\n{result.stdout}\n{result.stderr}")
    return result.returncode


# --- the management role ----------------------------------------------------------


@dataclass
class RoleRun:
    """One `ansible-playbook` run of `ansible/` against the VM."""

    returncode: int
    output: str
    recap: dict[str, int]


def ansible_playbook_argv() -> list[str]:
    """`ansible-playbook` from PATH, else ansible-core through uvx, which
    installs nothing into this project."""
    found = shutil.which("ansible-playbook")
    if found:
        return [found]
    if shutil.which("uvx"):
        return ["uvx", "--from", "ansible-core", "ansible-playbook"]
    raise VmError("the proxied and nextcloud modes are applied by ansible/: install ansible-core, or uv for uvx")


def _setup_namespace(args: list[str]):
    """`istota setup`'s flags as the namespace `collect_container_answers` reads:
    each `--a-b` is the attribute `a_b`, a flag with no value is True, and
    `--profile` repeats, as the CLI's own parser declares them."""
    import argparse

    namespace = argparse.Namespace()
    index = 0
    while index < len(args):
        name = args[index].removeprefix("--").replace("-", "_")
        if index + 1 < len(args) and not args[index + 1].startswith("--"):
            value, index = args[index + 1], index + 2
        else:
            value, index = True, index + 1
        if name == "profile":
            value = [*getattr(namespace, "profile", []), value]
        setattr(namespace, name, value)
    return namespace


def role_vars(vm: Vm, release: Release, mode: Mode) -> dict:
    """The role's variables for `mode`: what `istota-stack setup` would write
    for the same answers, through the wizard's own pure renderers."""
    from unittest import mock

    from istota import setup_wizard

    credentials = {"ISTOTA_BRAIN_NATIVE_API_KEY": NATIVE_KEY, **mode.setup_env}
    # Only the mode's credentials: a key exported in the developer's shell
    # under a name the wizard reads must not reach the VM.
    scrubbed = {name.upper(): "" for name in setup_wizard.SECRET_NAMES}
    with mock.patch.dict(os.environ, {**scrubbed, **credentials}):
        answers = setup_wizard.collect_container_answers(
            _setup_namespace(mode.setup_args), input_fn=None, out=lambda *_: None, getpass_fn=None)
    # Setup generates the web session key once and carries it forward; a fixed
    # one per VM keeps a converged VM converged across sessions.
    answers.session_secret = hashlib.sha256(f"{vm.name}:web-session".encode()).hexdigest()
    secrets = {name: value for name, value in setup_wizard.container_secret_values(answers).items() if value}
    config = _deep_merge(setup_wizard.container_config_document(answers, inline_credentials=False), mode.config)
    return {
        "istota_release": release.tag,
        "istota_release_signing_key": release.signing_key,
        "istota_storage": setup_wizard.host_env_values(answers)["STORAGE"],
        "istota_ingress": answers.ingress,
        "istota_domain": answers.hostname,
        "istota_tls_cert_source": answers.tls_cert_source,
        "istota_upstream_proxy": answers.upstream_proxy,
        "istota_listen_addr": answers.listen_addr,
        "istota_listen_port": answers.listen_port or 8080,
        "istota_rclone_remote": mode.rclone_remote,
        "istota_rclone_conf": mode.rclone_conf,
        "istota_compose_profiles": list(answers.compose_profiles),
        "istota_secrets": secrets,
        "istota_admins": [answers.user_id],
        "istota_config": config,
        "istota_users": {USER: {"display-name": answers.display_name}},
        "istota_user_secrets": {USER: [
            {"service": "karakeep", "key": "base_url", "value": "https://karakeep.example.invalid"},
            # Skipped, as the old role skipped an empty value: ensure refuses one.
            {"service": "karakeep", "key": "api_key", "value": ""},
        ]},
    }


def converge_role(vm: Vm, variables: dict, *, check: bool = False, timeout: int = BUILD_TIMEOUT) -> RoleRun:
    """Run `ansible/` against the VM over Lima's ssh config, as root."""
    entry = vm.info() or {}
    ssh_config = entry.get("sshConfigFile") or str(Path.home() / ".lima" / vm.name / "ssh.config")
    host = entry.get("hostname") or f"lima-{vm.name}"
    workdir = _workdir() / "ansible"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "inventory.ini").write_text(
        "[istota]\n"
        f"{host} ansible_ssh_common_args={shlex.quote('-F ' + ssh_config)} "
        "ansible_python_interpreter=/usr/bin/python3\n"
    )
    (workdir / "site.yml").write_text(
        "- hosts: istota\n"
        "  become: true\n"
        "  roles:\n"
        f"    - role: {REPO / 'ansible'}\n"
    )
    vars_file = workdir / "vars.json"
    vars_file.write_text(json.dumps(variables))
    vars_file.chmod(0o600)
    argv = [*ansible_playbook_argv(), "-i", str(workdir / "inventory.ini"),
            "-e", f"@{vars_file}", str(workdir / "site.yml")]
    if check:
        argv.append("--check")
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        env={**os.environ, "ANSIBLE_HOST_KEY_CHECKING": "False", "ANSIBLE_NOCOLOR": "1",
             "ANSIBLE_RETRY_FILES_ENABLED": "False"},
    )
    output = result.stdout + result.stderr
    recap: dict[str, int] = {}
    match = re.search(rf"^{re.escape(host)}\s*:\s*(.+)$", output.split("PLAY RECAP", 1)[-1], re.M)
    if match:
        recap = {key: int(value) for key, value in re.findall(r"(\w+)=(\d+)", match.group(1))}
    return RoleRun(result.returncode, output, recap)


#: The converge that applied each role mode this session, with the variables
#: it ran with, for `tests/vm/test_management_role.py`.
ROLE_RUNS: dict[str, tuple[RoleRun, dict]] = {}


def ensure_mode_by_role(vm: Vm, release: Release, mode: Mode) -> RoleRun:
    """Converge the VM into `mode` with the management role, as an estate would."""
    variables = role_vars(vm, release, mode)
    run = converge_role(vm, variables)
    if run.returncode != 0 or run.recap.get("failed", 1) != 0:
        raise VmError(f"ansible-playbook exited {run.returncode} (recap {run.recap}):\n{run.output[-8000:]}")
    wait_healthy(vm)
    vm.write(MODE_FILE, mode.signature(release.tag))
    ROLE_RUNS[mode.name] = (run, variables)
    return run


def ensure_mode(vm: Vm, release: Release, mode: Mode, *, prepare=None) -> RoleRun | None:
    """Bring the stack into `mode`, unless it is already there and healthy.

    A mode the role applies is converged on every session: the role decides
    what changed, and its result is what `tests/vm/test_management_role.py`
    reads.
    """
    signature = mode.signature(release.tag)
    if prepare is not None:
        prepare(vm)
    if mode.by_role:
        return ensure_mode_by_role(vm, release, mode)
    if vm.read(MODE_FILE).strip() == signature and container(vm, "daemon"):
        try:
            wait_healthy(vm, timeout=300)
            return
        except VmError:
            pass
    vm.run(f"rm -f {MODE_FILE}; systemctl stop istota-stack.service || true", timeout=600)
    args = " ".join(shlex.quote(a) for a in mode.setup_args)
    vm.run(f"istota-stack setup {args}", env={"ISTOTA_BRAIN_NATIVE_API_KEY": NATIVE_KEY, **mode.setup_env},
           timeout=900)
    if mode.host_env:
        env_file_set(vm, f"{STACK}/host.env", mode.host_env)
    if mode.config:
        apply_config(vm, mode.config)
        vm.run("istota-stack down", timeout=600)
    if mode.build:
        vm.run(f"istota-stack update {shlex.quote(release.tag)} && istota-stack down", timeout=BUILD_TIMEOUT)
    vm.run(f"{STACK}/src/host/provision.sh", timeout=1800)
    vm.run("systemctl enable istota-stack.service >/dev/null; systemctl start istota-stack.service", timeout=1800)
    wait_healthy(vm)
    vm.write(MODE_FILE, signature)
    return None


# --- stand-ins for the world outside the VM -------------------------------------


def ensure_metadata_namespace(vm: Vm) -> None:
    """A namespace answering on :80 at the metadata address and at 10.0.0.1.

    Routed from the VM through a veth, so a devbox's connection to either is
    forwarded (and so passes DOCKER-USER), not delivered to the VM itself.
    """
    vm.run(f"""
ip netns add istota-meta 2>/dev/null || true
if ! ip link show vmeta0 >/dev/null 2>&1; then
    ip link add vmeta0 type veth peer name vmeta1
    ip link set vmeta1 netns istota-meta
fi
ip addr replace 169.254.169.253/32 dev vmeta0
ip link set vmeta0 up
ip route replace {METADATA_ADDR}/32 dev vmeta0
ip route replace {PRIVATE_ADDR}/32 dev vmeta0
ip netns exec istota-meta sh -c '
    ip link set lo up; ip link set vmeta1 up
    ip addr replace {METADATA_ADDR}/32 dev vmeta1
    ip addr replace {PRIVATE_ADDR}/32 dev vmeta1
    ip route replace 169.254.169.253/32 dev vmeta1
    ip route replace default via 169.254.169.253 dev vmeta1 onlink'
systemctl is-active --quiet istota-vmtier-meta.service || \
    systemd-run --unit istota-vmtier-meta --quiet ip netns exec istota-meta python3 -m http.server 80 --bind 0.0.0.0
for i in $(seq 1 20); do curl -s -m 2 -o /dev/null http://{METADATA_ADDR}/ && exit 0; sleep 0.5; done
echo "the metadata stand-in did not answer" >&2; exit 1
""")


def ensure_proxied_namespaces(vm: Vm) -> None:
    """A bridge holding LISTEN_ADDR, with an upstream and an outsider namespace on it."""
    vm.run(f"""
if ! ip link show istupl >/dev/null 2>&1; then ip link add istupl type bridge; fi
ip addr replace {PROXIED_LISTEN}/24 dev istupl
ip link set istupl up
for pair in upstream:{UPSTREAM} outsider:{OUTSIDER}; do
    ns="istota-${{pair%%:*}}"; addr="${{pair#*:}}"; short="${{pair%%:*}}"; short="${{short:0:4}}"
    ip netns add "$ns" 2>/dev/null || true
    if ! ip link show "v${{short}}0" >/dev/null 2>&1; then
        ip link add "v${{short}}0" type veth peer name "v${{short}}1"
        ip link set "v${{short}}1" netns "$ns"
        ip link set "v${{short}}0" master istupl
    fi
    ip link set "v${{short}}0" up
    ip netns exec "$ns" sh -c "ip link set lo up; ip link set v${{short}}1 up; \\
        ip addr replace $addr/24 dev v${{short}}1; ip route replace default via {PROXIED_LISTEN}"
done
""")


PEBBLE_IMAGE = "ghcr.io/letsencrypt/pebble:2.8.0"
CHALLTESTSRV_IMAGE = "ghcr.io/letsencrypt/pebble-challtestsrv:2.8.0"


def ensure_pebble(vm: Vm) -> None:
    """Pebble (the ACME test CA) and its DNS, resolving every name to the VM."""
    address = vm_address(vm)
    config = {
        "pebble": {
            "listenAddress": "0.0.0.0:14000",
            "managementListenAddress": "0.0.0.0:15000",
            "certificate": "test/certs/localhost/cert.pem",
            "privateKey": "test/certs/localhost/key.pem",
            "httpPort": 80,
            "tlsPort": 443,
            "ocspResponderURL": "",
            "externalAccountBindingRequired": False,
        }
    }
    vm.write(f"{SCRATCH}/pebble-config.json", json.dumps(config))
    vm.run(f"""
docker network inspect istota-vmtier-acme >/dev/null 2>&1 || docker network create istota-vmtier-acme >/dev/null
if [ -z "$(docker ps -q --filter name=^istota-vmtier-dns$)" ]; then
    docker rm -f istota-vmtier-dns >/dev/null 2>&1 || true
    docker run -d --name istota-vmtier-dns --network istota-vmtier-acme --restart unless-stopped \\
        {CHALLTESTSRV_IMAGE} -defaultIPv4 {address} -defaultIPv6 "" \\
        -http01 "" -https01 "" -tlsalpn01 "" -doh "" -dns01 :8053 -management :8055 >/dev/null
fi
if [ -z "$(docker ps -q --filter name=^istota-vmtier-pebble$)" ]; then
    docker rm -f istota-vmtier-pebble >/dev/null 2>&1 || true
    docker run -d --name istota-vmtier-pebble --network istota-vmtier-acme --restart unless-stopped \\
        -e PEBBLE_VA_NOSLEEP=1 -p 127.0.0.1:14000:14000 -p 127.0.0.1:15000:15000 \\
        -v {SCRATCH}/pebble-config.json:/test/config/vmtier.json:ro \\
        {PEBBLE_IMAGE} -config /test/config/vmtier.json -dnsserver istota-vmtier-dns:8053 >/dev/null
fi
docker cp istota-vmtier-pebble:/test/certs/pebble.minica.pem {SCRATCH}/pebble.minica.pem
for i in $(seq 1 30); do
    curl -s --cacert {SCRATCH}/pebble.minica.pem https://localhost:15000/roots/0 > {SCRATCH}/pebble-root.pem && \\
        grep -q 'BEGIN CERTIFICATE' {SCRATCH}/pebble-root.pem && exit 0
    sleep 1
done
echo "Pebble did not answer" >&2; exit 1
""", timeout=900)


def served_certificate_verifies(vm: Vm) -> bool:
    """Whether nginx serves DOMAIN a chain the running Pebble's root accepts."""
    return vm.ok(
        f"curl -s -o /dev/null -m 10 --cacert {SCRATCH}/pebble-root.pem "
        f"--resolve {DOMAIN}:443:127.0.0.1 https://{DOMAIN}/"
    )


def ensure_certificate(vm: Vm) -> None:
    """The first issuance, as `istota-stack up` starts it, waited for.

    A Pebble that restarted has a new root, so a certificate from an earlier
    one is removed and issued again.
    """
    # The issuance `istota-stack up` started may still be running; removing its
    # directory under it fails it.
    wait_for(lambda: vm.out("systemctl is-active istota-certbot.service || true") != "activating",
             timeout=300, what="a running certbot to finish")
    try:
        wait_for(lambda: served_certificate_verifies(vm), timeout=60, what="the served certificate")
        return
    except VmError:
        pass
    vm.run(f"""
rm -rf {STACK}/letsencrypt/* /var/lib/istota-certbot
istota-stack compose restart nginx >/dev/null 2>&1 || true
systemctl reset-failed istota-certbot.service 2>/dev/null || true
systemctl start istota-certbot.service
""", timeout=600)
    wait_for(lambda: served_certificate_verifies(vm), timeout=180, what="nginx to serve the issued certificate")


def nextcloud_fixture_compose(vm_ip: str) -> dict:
    """The testbed's Nextcloud fixture, minus the stack services it decorates.

    `testbed/compose/nextcloud.yml`'s postgres, redis, init-shared and
    nextcloud, run as their own project in the VM and published on NC_PORT, so
    the VM's rclone unit and the daemon container reach it by address the way
    they would reach an operator's own Nextcloud. Provisioned by the same
    `provision-nc.sh`, which creates the bot's `Shared Files` external mount.
    """
    import yaml

    fixture = yaml.safe_load((REPO / "testbed" / "compose" / "nextcloud.yml").read_text())
    services = {name: fixture["services"][name] for name in ("postgres", "redis", "init-shared", "nextcloud")}
    nextcloud = services["nextcloud"]
    nextcloud["volumes"] = [
        volume if "provision-nc.sh" not in volume
        else f"{STACK}/src/testbed/compose/provision-nc.sh:/docker-entrypoint-hooks.d/post-installation/provision.sh:ro"
        for volume in nextcloud["volumes"]
    ]
    nextcloud["ports"] = [f"0.0.0.0:{NC_PORT}:80"]
    environment = dict(nextcloud["environment"])
    # The fixture's own admin is not the istota user of the same name.
    environment["NEXTCLOUD_ADMIN_USER"] = "ncadmin"
    environment["NEXTCLOUD_TRUSTED_DOMAINS"] = f"localhost nextcloud 127.0.0.1 {vm_ip}"
    for key in ("OVERWRITEHOST", "OVERWRITEPROTOCOL"):
        environment.pop(key, None)
    environment["OVERWRITECLIURL"] = f"http://{vm_ip}:{NC_PORT}"
    nextcloud["environment"] = environment
    volumes = {name: None for name in ("postgres_data", "redis_data", "nextcloud_html", "nextcloud_data", "shared_files")}
    return {"services": services, "volumes": volumes}


def nextcloud_credentials(vm: Vm) -> dict[str, str]:
    """The fixture's passwords, made once per VM and kept there."""
    path = f"{SCRATCH}/nextcloud.env"
    existing = vm.read(path)
    values = dict(line.split("=", 1) for line in existing.splitlines() if "=" in line)
    if not values:
        values = {
            "POSTGRES_PASSWORD": uuid.uuid4().hex,
            "ADMIN_PASSWORD": uuid.uuid4().hex,
            "BOT_USER": NC_BOT,
            "BOT_PASSWORD": uuid.uuid4().hex,
            "USER_NAME": USER,
            "USER_PASSWORD": uuid.uuid4().hex,
            "NC_PORT": str(NC_PORT),
            "ISTOTA_WEB_CALLBACK_URL": "http://localhost:8080/istota/callback",
            "ISTOTA_TESTBED_COMPOSE_DIR": f"{STACK}/src/testbed/compose",
        }
        vm.write(path, "".join(f"{k}={v}\n" for k, v in values.items()), mode="0600")
    return values


def ensure_nextcloud(vm: Vm) -> dict[str, str]:
    """The Nextcloud fixture up and provisioned, and the rclone.conf for it.

    The config is returned rather than placed: the management role writes it
    to /srv/app/istota/rclone.conf, as an estate's vault would.
    """
    credentials = nextcloud_credentials(vm)
    vm_ip = vm_address(vm)
    vm.write(f"{SCRATCH}/nextcloud.json", json.dumps(nextcloud_fixture_compose(vm_ip)))
    vm.run(
        f"docker compose -p istota-vmtier-nc -f {SCRATCH}/nextcloud.json --env-file {SCRATCH}/nextcloud.env "
        f"up -d --wait --wait-timeout 1500 postgres redis nextcloud",
        timeout=2400,
    )
    # Debian's rclone, installed only so the remote's password can be obscured
    # before provision.sh first runs, which replaces it with the pinned build.
    # Only a fresh VM takes that path, and nothing here asserts on the result.
    vm.run("command -v rclone >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq rclone fuse3 >/dev/null",
           timeout=900)
    # rclone obscure is salted, so the obscured form is made once per VM and
    # kept: a new one each session would be a changed rclone.conf every time.
    obscured_path = f"{SCRATCH}/nextcloud.obscured"
    obscured = vm.read(obscured_path).strip()
    if not obscured:
        obscured = vm.out(f"rclone obscure {shlex.quote(credentials['BOT_PASSWORD'])}")
        vm.write(obscured_path, obscured + "\n", mode="0600")
    rclone_conf = (
        "[ncroot]\n"
        "type = webdav\n"
        f"url = http://127.0.0.1:{NC_PORT}/remote.php/dav/files/{NC_BOT}\n"
        "vendor = nextcloud\n"
        f"user = {NC_BOT}\n"
        f"pass = {obscured}\n"
        "\n"
        "[nextcloud]\n"
        "type = alias\n"
        "remote = ncroot:Shared Files\n"
    )
    return {**credentials, "url": f"http://{vm_ip}:{NC_PORT}", "rclone_conf": rclone_conf}


# --- probe tasks ----------------------------------------------------------------


def run_probe_task(vm: Vm, endpoint, command: str, *, timeout: int = 240) -> str:
    """Run `command` as a Bash tool call in a real task, and return what it printed.

    The task is submitted through the VM's `istota` wrapper, runs in the
    daemon's sandbox like any other, and the scripted model on the Mac answers
    it with one Bash call routed by a marker in the prompt.
    """
    marker = f"[e2e:{uuid.uuid4().hex[:12]}]"
    endpoint.rescript([{
        "when": marker,
        "turns": [
            {"tool_calls": [{"id": "call-1", "name": "Bash", "arguments": {"command": command}}]},
            {"text": "the probe ran"},
        ],
    }])
    created = vm.out(f"istota task {shlex.quote('vm tier probe ' + marker)} -u {USER} --source-type cli")
    match = re.search(r"Task created:\s*(\d+)", created)
    if not match:
        raise VmError(f"no task id in: {created}")
    task_id = int(match.group(1))
    query = (
        "import sqlite3;c=sqlite3.connect('file:/data/db/istota.db?mode=ro',uri=True);"
        f"print(c.execute('select status from tasks where id={task_id}').fetchone()[0])"
    )
    status = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = stack_exec(vm, f'python3 -c "{query}"', check=False).stdout.strip()
        if status in ("completed", "failed", "cancelled"):
            break
        time.sleep(3)
    results = endpoint.tool_results()
    if status != "completed" or not results:
        raise VmError(f"probe task {task_id} ended {status!r}; tool results: {results}")
    return results[-1]


def marked_block(text: str, begin: str = "PROBE_BEGIN", end: str = "PROBE_END") -> dict[str, str]:
    """`key=value` lines between two markers, as a dict."""
    start = text.find(begin)
    stop = text.find(end, start + 1)
    if start < 0 or stop < 0:
        raise VmError(f"no {begin}..{end} block in:\n{text[:3000]}")
    pairs = {}
    for line in text[start + len(begin):stop].splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            pairs[key.strip()] = value.strip()
    return pairs


def short_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def limactl_available() -> bool:
    return shutil.which("limactl") is not None
