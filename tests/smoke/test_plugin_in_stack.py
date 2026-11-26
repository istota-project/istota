"""A skill plugin, installed beside the image, run by a task through the sandbox.

Spec: external-skill-packages, Stage 8. The `plugins` profile boots the lean
stack with the shipped `docker-compose.plugins.yml` and `ISTOTA_PLUGINS_DIR`
pointing at a tree built before boot by `host/istota-stack`'s sync one-shot
(the `plugins_tree` fixture), which is the state `istota-stack plugins sync`
leaves a VM in. The image tier showed the tree loads in a bare container and
the vm tier that the verb installs it; this is the one place a *task* reaches
the plugin, so the only place the proxy's plugin branch runs across a real
namespace.

Three witnesses:

- The daemon's own view has the tree and loaded the plugin (doctor's
  `skills.plugins`). This is the control for the third.
- A scripted Bash call runs `istota-skill example ping` from inside the task's
  sandbox. The answer comes back from the plugin's own CLI, spawned daemon-side
  by the skill proxy, and `cred_fd: false` says the proxy opened no private
  credential channel for it.
- From the same sandbox `/app/plugins` does not exist and the plugin's package
  cannot be imported, while `/app/.venv`, the sibling the sandbox does bind and
  whose `.pth` names the tree, is present. An absence that the probe could also
  report for a path it cannot see at all would prove nothing: the sibling and
  the daemon-side control are what let it fail.
"""

from __future__ import annotations

import json
import uuid

import pytest

from testbed import stack as stack_support

from tests.support.plugin_fixture import WHEEL_DIST, WHEEL_PACKAGE, WHEEL_SKILL, WHEEL_VERSION

pytestmark = pytest.mark.smoke

PROFILE = pytest.mark.profile("plugins")

PLUGINS = "/app/plugins"
VENV = "/app/.venv"

PROBE = f"""
echo PING_BEGIN
istota-skill {WHEEL_SKILL} ping 2>&1
echo PING_END
for path in {VENV} {PLUGINS}; do
  if test -e "$path"; then echo "$path=present"; else echo "$path=absent"; fi
done
echo "import=$({VENV}/bin/python -c 'import {WHEEL_PACKAGE}' 2>&1 | tail -n 1)"
"""


def _run_probe(stack) -> str:
    """Run `PROBE` as a task's Bash call and return what it printed."""
    call = f"plugin-probe-{uuid.uuid4().hex[:8]}"
    stack.script([
        {"tool_calls": [{"id": call, "name": "Bash", "arguments": {"command": PROBE}}]},
        {"text": "I ran the plugin"},
    ])
    task_id = stack.submit("run the example plugin")
    task = stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=180)
    assert task["status"] == "completed", (
        f"task {task_id} ended {task['status']!r}: {task.get('error')!r}\n{stack.logs(120)}"
    )
    output = stack.endpoint.tool_results_by_id().get(call)
    assert output is not None, (
        "the probe's Bash call never reported back to the model\n"
        f"--- daemon logs ---\n{stack.logs(120)}"
    )
    return output


def _between(output: str, begin: str, end: str) -> str:
    start = output.find(begin)
    stop = output.find(end, start + 1)
    assert start >= 0 and stop >= 0, f"no {begin}..{end} block in the probe output:\n{output}"
    return output[start + len(begin):stop].strip()


@PROFILE
class TestThePluginInTheStack:
    def test_the_daemon_has_the_tree_and_loaded_the_plugin(self, stack):
        """The control: the tree is there to be absent from the sandbox."""
        present = stack.exec(["test", "-f", f"{PLUGINS}/installed/site/{WHEEL_PACKAGE}/__init__.py"])
        assert present.returncode == 0, "the daemon container has no plugins tree at /app/plugins"

        result = stack.exec(
            ["istota", "-c", stack_support.CONTAINER_CONFIG, "doctor", "--only", "skills.plugins", "--json"],
            timeout=180,
        )
        checks = {check["name"]: check for check in json.loads(result.stdout or "[]")}
        assert checks.get("skills.plugins", {}).get("status") == "ok", f"{checks}\n{result.stderr}"
        assert checks["skills.plugins"]["detail"] == (
            f"{WHEEL_SKILL}: {WHEEL_DIST} {WHEEL_VERSION} ({WHEEL_PACKAGE})"
        )

    def test_a_task_runs_the_plugin_through_the_proxy_with_no_credential_channel(self, stack):
        output = _run_probe(stack)
        ping = _between(output, "PING_BEGIN", "PING_END")
        assert json.loads(ping) == {"status": "ok", "cred_fd": False}, output

    def test_the_plugins_tree_is_not_in_the_sandbox(self, stack):
        output = _run_probe(stack)
        lines = output.splitlines()
        # The sibling first: a probe that could not see /app at all would
        # report every path absent.
        assert f"{VENV}=present" in lines, output
        assert f"{PLUGINS}=absent" in lines, output
        assert f"import=ModuleNotFoundError: No module named '{WHEEL_PACKAGE}'" in lines, output
