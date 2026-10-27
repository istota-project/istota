"""Whether a `claude_code` task's tools can read the CLI's credential.

GitLab #860. On the old path bwrap wraps the `claude` CLI and the model's Bash
is its child in the same namespace, so the credential the CLI authenticates
with is one `env` or one `cat ~/.claude/.credentials.json` away. On the tool
harness the CLI runs outside the sandbox and the tools run in the tool server,
which holds no credential. This file asks the question from inside a live task
in the shipped image, in both auth modes.

No model runs. The task runs through `istota task -x` in the daemon container
against a copy of the rendered config switched to the `claude_code` brain,
with `claude` on `PATH` being `tests/support/fake_claude_cli.py`. On the
harness path (`--mcp-config` given) the fake spawns the real adapter and sends
the probe as an `mcp__istota__Bash` call through the bridge into the tool
server; on the old path it runs the probe itself, inside the bwrap the
executor wrapped it in, where the real CLI's Bash runs. Its result frame is
the probe's output, and the task's result is that frame.

The probe, in one Bash call: the CLI's credentials file, `env`, every readable
`/proc/*/environ` grepped for the credential, and a CONNECT to
`api.anthropic.com:443` through the task's proxy. The credential is a dummy
generated per run, so no other text in the stack can carry it. A host-side
line from the fake's own view (the CLI's process, unwrapped on the harness)
shows the CLI did hold it, so an absence in the tools' view is not a
credential that was never configured.

**Arms.** By default the switch is on (`[brain] tool_harness = true`), and
everything is green, the connect included: since the spec's Stage 5 the
Anthropic hosts are on no allowlist while the harness is on, and the task env
carries no credential. `ISTOTA_CLI_WITNESS_CONTROL=switch-off` runs the same
probes with the switch off: the negative control, which must go red on the
credential file (subscription), `env`, `/proc` and the connect in both modes,
since the old path still hands its wrapped CLI the credential in `req.env`
and the hosts on the allowlist.

Stage 6 of the cli-brain-tools-only-path spec removes the old path, after
which the switch-off arm cannot run and moves to a negative-control script.

The fake and its plan go in `$HOME/.local/bin`, the one directory the CLAUDE
profile binds that is on the task's `PATH` and holds no system binary, so the
wrapped CLI finds them too. The fixture removes them again.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke

FAKE_CLI = Path(__file__).resolve().parent.parent / "support" / "fake_claude_cli.py"

CONFIG = "/data/config/config.toml"
WITNESS_CONFIG = "/tmp/cli-witness-config.toml"
USER = "testuser"

#: `switch-off` selects the negative control: the same probes on the old path.
CONTROL = os.environ.get("ISTOTA_CLI_WITNESS_CONTROL", "")
HARNESS_ON = CONTROL != "switch-off"

#: The dummy credential's prefix. The probes grep for it, so the whole value
#: is in no command text; only a leak can put it in a result.
PREFIX = "cliwitness"

#: The fake's own view: the CLI's process. Counts, never the value.
HOST_PROBE = (
    'echo "host_harness=${CLAUDE_CONFIG_DIR:+yes}"; '
    f'echo "host_env_cred=$(env | grep -c {PREFIX})"; '
    f'echo "host_file_cred=$(grep -c {PREFIX} "${{CLAUDE_CONFIG_DIR:-$HOME/.claude}}/.credentials.json" 2>/dev/null || echo 0)"'
)

#: The model's Bash. Sections, so each assertion reads only its own probe.
PROBE = r"""
echo WITNESS_BEGIN
echo "--- credfile"
cat ~/.claude/.credentials.json 2>&1
echo
echo "--- env"
env 2>&1
echo "--- proc"
n=0
for f in /proc/[0-9]*/environ; do
  if tr '\0' '\n' < "$f" > /tmp/witness-environ 2>/dev/null; then
    n=$((n+1))
    grep -a __PREFIX__ /tmp/witness-environ
  fi
done
rm -f /tmp/witness-environ
echo "proc_readable=$n"
echo "--- connect"
code=$(curl -s -o /dev/null --max-time 20 --proxytunnel -w '%{http_connect}' https://api.anthropic.com/ 2>/dev/null)
echo "connect=$code rc=$?"
echo "--- end"
echo WITNESS_END
""".replace("__PREFIX__", PREFIX)

PLAN = {
    "host_probe": HOST_PROBE,
    "calls": [{"name": "Bash", "args": {"command": PROBE}}],
}

#: The witness config: the rendered one with the brain switched. Through
#: tomllib and the tree's TOML writer rather than `sed`, so a key the base
#: does not carry yet is added rather than missed.
RENDER = """
import sys, tomllib
from istota.lib import toml_write
mode, harness, base_url, out = sys.argv[1:5]
with open(%r, "rb") as fh:
    doc = tomllib.load(fh)
brain = doc.setdefault("brain", {})
brain["kind"] = "claude_code"
brain["fallback"] = ""
brain["tool_harness"] = harness == "on"
brain.setdefault("claude_code", {})["auth"] = mode
if mode == "api_key":
    doc.setdefault("providers", {}).setdefault("anthropic", {})["base_url"] = base_url
with open(out, "w") as fh:
    fh.write(toml_write.dumps(doc))
""" % CONFIG

#: One run per stack, auth mode and arm, shared by the tests below.
_RUNS: dict[tuple[int, str, bool], dict] = {}


def _b64_write(path: str, content: str, mode: str) -> str:
    data = base64.b64encode(content.encode()).decode()
    return f'echo {data} | base64 -d > "{path}" && chmod {mode} "{path}"'


@pytest.fixture
def fake_cli(stack):
    """The fake `claude` and its plan in `$HOME/.local/bin`, and the
    credentials file backed up; all undone afterwards."""
    shim = '#!/bin/sh\nexec /usr/local/bin/python3 "$(dirname "$0")/fake_claude_cli.py" "$@"\n'
    install = " && ".join([
        'set -e; bin="$HOME/.local/bin"; mkdir -p "$bin"',
        'test ! -e "$bin/claude"',
        _b64_write("$bin/claude", shim, "0755"),
        _b64_write("$bin/fake_claude_cli.py", FAKE_CLI.read_text(), "0644"),
        _b64_write("$bin/plan.json", json.dumps(PLAN), "0644"),
        'c="$HOME/.claude/.credentials.json"; if [ -e "$c" ]; then cp -p "$c" "$c.witness-bak"; fi',
        'echo "home=$HOME"',
    ])
    result = stack.exec(["sh", "-c", install])
    assert result.returncode == 0, (
        f"installing the fake claude failed\n{result.stdout}\n{result.stderr}"
    )
    yield stack
    stack.exec(["sh", "-c", (
        'bin="$HOME/.local/bin"; rm -f "$bin/claude" "$bin/fake_claude_cli.py" "$bin/plan.json"; '
        'rmdir "$bin" 2>/dev/null; '
        'c="$HOME/.claude/.credentials.json"; rm -f "$c"; '
        'if [ -e "$c.witness-bak" ]; then mv "$c.witness-bak" "$c"; fi; '
        f'rm -f {WITNESS_CONFIG}; true'
    )])


def _run(stack, mode: str) -> dict:
    key = (id(stack), mode, HARNESS_ON)
    if key in _RUNS:
        return _RUNS[key]
    value = PREFIX + secrets.token_hex(12)
    render = stack.exec(
        ["uv", "run", "python", "-c", RENDER, mode, "on" if HARNESS_ON else "off",
         stack.endpoint.anthropic_container_url, WITNESS_CONFIG],
        timeout=120,
    )
    assert render.returncode == 0, f"rendering the witness config failed\n{render.stderr}"

    if mode == "subscription":
        cred_env = f"CLAUDE_CODE_OAUTH_TOKEN={value}"
        # The CLI's credentials file, as the entrypoint writes it for a token.
        sync = stack.exec(["sh", "-c", (
            f"env {cred_env} uv run istota -c {WITNESS_CONFIG} cli-auth sync-credentials-file"
        )], timeout=120)
        assert sync.returncode == 0, f"writing the credentials file failed\n{sync.stderr}"
    else:
        cred_env = f"ISTOTA_PROVIDER_ANTHROPIC_API_KEY={value}"

    task = stack.exec(["sh", "-c", (
        f'env -u CLAUDE_CODE_OAUTH_TOKEN -u ANTHROPIC_API_KEY PATH="$HOME/.local/bin:$PATH" {cred_env} '
        f'uv run istota -c {WITNESS_CONFIG} task "credential witness" -u {USER} '
        f'--source-type cli -x'
    )], timeout=300)
    output = task.stdout + "\n" + task.stderr
    # Whole lines only: the command text (`echo WITNESS_BEGIN`) is not one.
    marked = re.search(r"^WITNESS_BEGIN$(.*?)^WITNESS_END$", output, re.S | re.M)
    if marked is None:
        raise AssertionError(
            f"the probe's output never came back (exit {task.returncode}), so "
            f"this says nothing about the credential\n--- output ---\n{output[-6000:]}"
        )
    body = marked.group(1)
    sections = {}
    for name, text in re.findall(r"^--- (\w+)\n(.*?)(?=^--- )", body, re.S | re.M):
        sections[name] = text
    run = {
        "mode": mode,
        "value": value,
        "output": output,
        "sections": sections,
        "host": dict(re.findall(r"^(host_\w+)=(\S*)$", output, re.M)),
    }
    _RUNS[key] = run
    return run


@pytest.fixture(params=["subscription", "api_key"])
def witness(request, fake_cli):
    return _run(fake_cli, request.param)


def _show(run) -> str:
    return run["output"][-6000:]


class TestTheCliCredentialIsOutOfTheToolsReach:
    def test_the_probes_ran_and_the_cli_held_the_credential(self, witness):
        """The control: an absence below is not a probe that never ran or a
        credential that never reached the CLI."""
        assert set(witness["sections"]) >= {"credfile", "env", "proc", "connect"}, _show(witness)
        # The harness gives the CLI a per-run CLAUDE_CONFIG_DIR; the old path
        # sets none. Without this an arm could be the other one.
        assert witness["host"].get("host_harness") == ("yes" if HARNESS_ON else ""), (
            f"the task did not run the arm this session asked for\n{_show(witness)}"
        )
        assert witness["host"].get("host_env_cred", "0") != "0", (
            "the CLI's own environment does not hold the dummy credential, so "
            f"every absence below is vacuous\n{_show(witness)}"
        )
        if witness["mode"] == "subscription":
            assert witness["host"].get("host_file_cred", "0") != "0", (
                "the CLI's credentials file does not hold the dummy token, so "
                f"the file probe below is vacuous\n{_show(witness)}"
            )
        assert re.search(r"proc_readable=[1-9]", witness["sections"]["proc"]), _show(witness)

    def test_the_credentials_file_is_unreadable(self, witness):
        assert witness["value"] not in witness["sections"]["credfile"], (
            f"a tool read the CLI's credentials file\n{_show(witness)}"
        )

    def test_the_environment_holds_no_credential(self, witness):
        assert witness["value"] not in witness["sections"]["env"], (
            f"the credential is in the tools' environment\n{_show(witness)}"
        )

    def test_no_readable_process_environment_holds_it(self, witness):
        assert witness["value"] not in witness["sections"]["proc"], (
            f"a tool read the credential out of /proc/*/environ\n{_show(witness)}"
        )

    def test_no_result_carries_it(self, witness):
        assert witness["value"] not in witness["output"], _show(witness)

    def test_the_anthropic_connect_is_refused(self, witness):
        # `%{http_connect}` is the proxy's answer to CONNECT: 403 is the
        # allowlist's refusal, 200 a tunnel, 502 an allowed host it could not
        # reach.
        assert re.search(r"connect=403 ", witness["sections"]["connect"]), _show(witness)
