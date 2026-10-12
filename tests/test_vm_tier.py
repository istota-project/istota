"""The vm tier's wiring, held in the default suite.

The tier itself needs limactl and a VM (`scripts/test-vm.sh`). What can be
checked without one: the marker is registered and deselected, every file in
`tests/vm/` carries it, and every node the negative-control driver requires to
go red exists and sits in the module that handles that control. A control
naming a node that was renamed would otherwise fail with a missing FAILED line
for the wrong reason, and a control name its module never reads would break
nothing.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VM_DIR = REPO / "tests" / "vm"
CONTROL_SCRIPT = REPO / "scripts" / "test-vm-negative-control.sh"


def _control_calls() -> list[tuple[str, list[str]]]:
    """`(name, [node ids])` for each `control` call in the driver, variables expanded."""
    text = CONTROL_SCRIPT.read_text()
    variables = {"VM": "tests/vm"}
    for name, value in re.findall(r'^([A-Z])="([^"]+)"$', text, re.M):
        variables[name] = value
    calls = []
    for block in re.findall(r"^control ([a-z0-9-]+) \\\n((?:    .*\\\n)*    .*)$", text, re.M):
        name, rest = block
        args = re.findall(r'"([^"]+)"', rest)
        nodes = []
        for arg in args[1:]:
            while "$" in arg:
                expanded = arg
                for var, value in variables.items():
                    expanded = expanded.replace(f"${var}", value)
                assert expanded != arg, f"unknown variable in {arg}"
                arg = expanded
            nodes.append(arg)
        calls.append((name, nodes))
    return calls


def _defined(path: Path) -> set[str]:
    """`Class::function` for every test method in a module."""
    tree = ast.parse(path.read_text())
    found = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    found.add(f"{node.name}::{item.name}")
    return found


class TestTheMarker:
    def test_it_is_registered_and_deselected(self):
        options = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]
        assert any(m.startswith("vm:") for m in options["markers"])
        assert "not vm" in options["addopts"]

    def test_every_vm_test_file_carries_it(self):
        files = sorted(VM_DIR.glob("test_*.py"))
        assert files, "no vm tests found; this guard would pass vacuously"
        for path in files:
            assert re.search(r"^pytestmark = pytest\.mark\.vm$", path.read_text(), re.M), path


class TestTheControls:
    def test_the_driver_names_controls(self):
        assert len(_control_calls()) >= 11

    def test_every_required_node_exists(self):
        for name, nodes in _control_calls():
            assert nodes, f"control {name} requires no node"
            for node in nodes:
                file, _, rest = node.partition("::")
                assert rest in _defined(REPO / file), f"control {name}: no {node}"

    def test_each_control_is_handled_in_the_module_it_targets(self):
        for name, nodes in _control_calls():
            for file in {node.partition("::")[0] for node in nodes}:
                assert f'"{name}"' in (REPO / file).read_text(), f"{file} never applies the control {name}"


class TestTheUpdateControls:
    def test_each_mutation_applies_once_to_istota_stack(self):
        """The update controls edit a copy of the installed script; a fragment
        that no longer matches would fail the control in the VM, minutes in."""
        from tests.vm import lima

        script = (REPO / "host" / "istota-stack").read_text()
        assert lima.UPDATE_CONTROLS
        for name, (old, new) in lima.UPDATE_CONTROLS.items():
            assert script.count(old) == 1, name
            assert old != new, name


class TestTheLimaTemplate:
    def test_the_probe_does_not_rely_on_roots_path(self):
        """Lima runs the probe as the user, whose PATH has no /usr/local/sbin, so
        a `command -v istota-stack` probe waited out its timeout on every boot."""
        template = (REPO / "host" / "lima" / "istota.yaml").read_text()
        probe = template.split("probes:", 1)[1].split("portForwards:", 1)[0]
        assert "/usr/local/sbin/istota-stack" in probe
        assert "command -v istota-stack" not in probe

    def test_row_l3s_control_finds_the_base_it_extends(self):
        """`default-mounts-in-template` adds a base by editing this line of a
        copy; a template that no longer has it would fail the control minutes
        into a VM session."""
        template = (REPO / "host" / "lima" / "istota.yaml").read_text()
        assert template.count("base:\n  - template:_images/debian-13\n") == 1
        assert "base:\n  - template:_images/debian-13\n" in (VM_DIR / "lima.py").read_text().replace('\\n', "\n")


class TestLeavingTheHostFolderMode:
    """host.env and config.toml leave the host-folder mode together.

    `unshare_host_folder` once set STORAGE=local and restarted the VM with the
    config still naming /mnt/shared as its workspace mount. istota-stack then
    passed no workspace type, /mnt/shared was the rclone mount's empty mount
    point, the daemon wrote a workspace into it, and the nextcloud mode's
    rclone mount refused that directory for the rest of the session.
    """

    CONFIG = {
        "bot_name": "Istota",
        "workspace_path": "/mnt/shared",
        "workspace_mount_path": "/mnt/shared",
        "brain": {"kind": "native"},
    }

    def _unshare(self):
        import base64
        import json
        import subprocess

        from tests.vm import lima

        events: list[tuple[str, object]] = []
        config = self.CONFIG

        class Recording(lima.Vm):
            def run(self, script, *, timeout=600, check=True, env=None):
                stdout = ""
                if "systemctl stop istota-stack.service" in script:
                    events.append(("stop", None))
                elif "tomllib" in script:
                    stdout = json.dumps(config)
                elif "base64 -d" in script:
                    encoded = script.split("<<'EOF'\n", 1)[1].split("\nEOF", 1)[0]
                    events.append(("plan", json.loads(base64.b64decode(encoded))))
                elif "istota-stack apply" in script:
                    events.append(("apply", None))
                elif '"STORAGE": "local"' in script:
                    events.append(("storage-local", None))
                elif "host/provision.sh" in script:
                    events.append(("provision", None))
                return subprocess.CompletedProcess([], 0, stdout, "")

            def info(self):
                return {"config": {"mounts": [
                    {"location": "/repo", "mountPoint": lima.REPO_MOUNT, "writable": False},
                    {"location": "/folder", "mountPoint": lima.HOST_FOLDER_MOUNT, "writable": True},
                ]}}

            def set_mounts(self, mounts):
                events.append(("mounts", [m["mountPoint"] for m in mounts]))

        release = lima.Release(repo=Path("/repo"), tag="vmtier-x", signing_key="key")
        lima.unshare_host_folder(Recording("istota-vm-test"), release)
        return events

    def test_the_config_stops_naming_the_folder(self):
        plans = [value for kind, value in self._unshare() if kind == "plan"]
        assert len(plans) == 1, plans
        written = plans[0]["config"]
        assert "workspace_mount_path" not in written
        assert written["workspace_path"] == "/data/workspace"
        assert written["brain"] == {"kind": "native"}

    def test_the_config_changes_before_anything_can_start_the_stack(self):
        order = [kind for kind, _ in self._unshare() if kind != "plan"]
        assert order == ["stop", "apply", "storage-local", "provision", "mounts"], order


class TestTheEntrypointControls:
    """The vm tier's entrypoint controls `sed` one line of the deployed
    entrypoint; a line that no longer matches fails the build in the VM, minutes
    in, and a pattern matching twice removes more than the one refusal."""

    @staticmethod
    def _pattern(dockerfile: Path) -> str:
        match = re.search(r"sed -i 's/\^(.*?)\$/", dockerfile.read_text())
        assert match, dockerfile.name
        return match.group(1).replace("\\.", ".")

    def test_each_matches_exactly_one_line_of_the_entrypoint(self):
        entrypoint = (REPO / "docker" / "istota" / "entrypoint.sh").read_text().splitlines()
        for name in ("Dockerfile.no-storage-refusal", "Dockerfile.no-workspace-marker"):
            line = self._pattern(REPO / "docker" / "test" / name)
            assert entrypoint.count(line) == 1, (name, line)
