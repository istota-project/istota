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


class TestTheLimaTemplate:
    def test_the_probe_does_not_rely_on_roots_path(self):
        """Lima runs the probe as the user, whose PATH has no /usr/local/sbin, so
        a `command -v istota-stack` probe waited out its timeout on every boot."""
        template = (REPO / "host" / "lima" / "istota.yaml").read_text()
        probe = template.split("probes:", 1)[1].split("portForwards:", 1)[0]
        assert "/usr/local/sbin/istota-stack" in probe
        assert "command -v istota-stack" not in probe
