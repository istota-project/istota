"""Whether a `claude_code` task's tools can read the CLI's credential.

GitLab #860. On the old path bwrap wrapped the `claude` CLI and the model's
Bash was its child in the same namespace, so the credential the CLI
authenticates with was one `env` or one `cat ~/.claude/.credentials.json` away.
On the tool harness, the only path since the cli-brain-tools-only-path spec's
Stage 6, the CLI runs outside the sandbox and the tools run in the tool server,
which holds no credential. This file asks the question from inside a live task
in the shipped image, in both auth modes.

No model runs. The task runs through `istota task -x` in the daemon container
against a copy of the rendered config switched to the `claude_code` brain,
with `claude` on `PATH` being `tests/support/fake_claude_cli.py`. The fake
spawns the real adapter and sends the probe as an `mcp__istota__Bash` call
through the bridge into the tool server. Its result frame is the probe's
output, and the task's result is that frame.

The probe, in one Bash call: the CLI's credentials file, `env`, every readable
`/proc/*/environ` grepped for the credential, and a CONNECT to
`api.anthropic.com:443` through the task's proxy. The credential is a dummy
generated per run, so no other text in the stack can carry it. A host-side
line from the fake's own view (the CLI's process, unwrapped on the harness)
shows the CLI did hold it, so an absence in the tools' view is not a
credential that was never configured.

Everything is green, the connect included: the Anthropic hosts are on no
allowlist and the task env carries no credential.

**The direct callers** (GitLab #867) are the second class. Health OCR, the code
reviewer and doctor's model probe build their own `BrainRequest` with no task
behind it: a harness from `build_daemon_harness`, a wrap from
`build_daemon_sandbox` (OCR, the reviewer) or `build_bwrap_cmd` (doctor). Each
runs here through its own entry point, with the fake's results written to a
file, because none of them prints what a tool returned.

Doctor grants Bash, so it gets the shell probe. OCR and the reviewer grant no
Bash, so their probe is a `Read` of the daemon's credentials file, of the
harness's link to it and of `/proc/<cli pid>/environ`, each of which must come
back as the roots' refusal, and a Bash call, which must not run. On those two
the roots answer first, so these reads say nothing about the namespace behind
them, and doctor's Bash does not reach it, since doctor builds its own wrap.

**The third class is that namespace.** `build_daemon_sandbox`, as OCR and the
reviewer each call it, wraps the same shell probes in a driver that holds the
dummy credential in its own environment and has planted it in the default
harness root, the directory those two callers' harnesses live under.

**The network posture differs on this path, on purpose.** There is no CONNECT
proxy and no `--unshare-net`: a direct caller's tool server shares the daemon's
network namespace, so the task path's refused connect does not apply and
doctor's Bash can reach what the daemon can. OCR and the reviewer offer no tool
that opens a socket. What keeps the credential safe here is that the namespace
holds none, not where it could be sent. The doctor case asserts the shared
namespace, and the task class asserts the opposite with the same comparison.

**The negative control** is `scripts/test-image-negative-control.sh
cli-witness`, which sets `ISTOTA_CLI_WITNESS_CONTROL=cli-runs-tools`: the fake
runs the probe in its own process, unwrapped and holding the credential, as a
CLI whose own tools were left on would. On the task path it must go red on
`env`, `/proc`, the result, the network namespace and the connect in both
modes, and on the direct callers on every probe that can see the credential.
It replaced the old path's switch-off arm, which the old path's removal ended.

The fake and its plan go in `$HOME/.local/bin`, on the task's `PATH` and
holding no system binary. The fixture removes them again.
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

#: `cli-runs-tools` selects the negative control: the fake runs the probe in
#: the CLI's own process, where the credential is.
CONTROL = os.environ.get("ISTOTA_CLI_WITNESS_CONTROL", "")

#: The dummy credential's prefix. The probes grep for it, so the whole value
#: is in no command text; only a leak can put it in a result.
PREFIX = "cliwitness"

#: The fake's own view: the CLI's process. Counts, never the value.
HOST_PROBE = (
    'echo "host_harness=${CLAUDE_CONFIG_DIR:+yes}"; '
    f'echo "host_env_cred=$(env | grep -c {PREFIX})"; '
    f'echo "host_file_cred=$(grep -c {PREFIX} "${{CLAUDE_CONFIG_DIR:-$HOME/.claude}}/.credentials.json" 2>/dev/null || echo 0)"; '
    'echo "host_netns=$(readlink /proc/self/ns/net)"; '
    f'echo "host_harness_link=$(grep -c {PREFIX} {{harness_dir}}/config/.credentials.json 2>/dev/null || echo 0)"'
)

#: The env and process half of the model's Bash, on both paths. Sections, so
#: each assertion reads only its own probe.
_ENV_PROBES = r"""
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
echo "--- netns"
readlink /proc/self/ns/net
""".replace("__PREFIX__", PREFIX)

#: The model's Bash on a task.
PROBE = r"""
echo WITNESS_BEGIN
echo "--- credfile"
cat ~/.claude/.credentials.json 2>&1
echo
""" + _ENV_PROBES + r"""
echo "--- connect"
code=$(curl -s -o /dev/null --max-time 20 --proxytunnel -w '%{http_connect}' https://api.anthropic.com/ 2>/dev/null)
echo "connect=$code rc=$?"
echo "--- end"
echo WITNESS_END
"""

PLAN = {
    "host_probe": HOST_PROBE,
    "calls": [{"name": "Bash", "args": {"command": PROBE}}],
    "run_in_cli": CONTROL == "cli-runs-tools",
}

#: The witness config: the rendered one with the brain switched. Through
#: tomllib and the tree's TOML writer rather than `sed`, so a key the base
#: does not carry yet is added rather than missed.
RENDER = """
import sys, tomllib
from istota.lib import toml_write
mode, base_url, out = sys.argv[1:4]
with open(%r, "rb") as fh:
    doc = tomllib.load(fh)
brain = doc.setdefault("brain", {})
brain["kind"] = "claude_code"
brain["fallback"] = ""
brain.setdefault("claude_code", {})["auth"] = mode
if mode == "api_key":
    doc.setdefault("providers", {}).setdefault("anthropic", {})["base_url"] = base_url
with open(out, "w") as fh:
    fh.write(toml_write.dumps(doc))
""" % CONFIG

#: One run per stack and auth mode, shared by the tests below.
_RUNS: dict[tuple[int, str], dict] = {}


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
        'bin="$HOME/.local/bin"; rm -f "$bin/claude" "$bin/fake_claude_cli.py" "$bin/plan.json" "$bin/results.json"; '
        'rmdir "$bin" 2>/dev/null; '
        'c="$HOME/.claude/.credentials.json"; rm -f "$c"; '
        'if [ -e "$c.witness-bak" ]; then mv "$c.witness-bak" "$c"; fi; '
        f'rm -f {WITNESS_CONFIG} {DOCUMENT}; rm -rf {REVIEW_REPO}; true'
    )])


def _prepare(stack, mode: str) -> tuple[str, str]:
    """The witness config and, under "subscription", the credentials file.
    Returns the dummy value and the `NAME=value` that hands it to a process."""
    value = PREFIX + secrets.token_hex(12)
    render = stack.exec(
        ["uv", "run", "python", "-c", RENDER, mode,
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
    return value, cred_env


def _run(stack, mode: str) -> dict:
    key = (id(stack), mode)
    if key in _RUNS:
        return _RUNS[key]
    value, cred_env = _prepare(stack, mode)

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
        # The harness gives the CLI a per-run CLAUDE_CONFIG_DIR.
        assert witness["host"].get("host_harness") == "yes", (
            f"the task did not run on the tool harness\n{_show(witness)}"
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

    def test_the_tools_network_namespace_is_not_the_clis(self, witness):
        """The discriminating half of the direct callers' shared namespace."""
        host = witness["host"].get("host_netns", "")
        assert host.startswith("net:["), _show(witness)
        assert "net:[" in witness["sections"]["netns"], _show(witness)
        assert host not in witness["sections"]["netns"], _show(witness)

    def test_the_anthropic_connect_is_refused(self, witness):
        # `%{http_connect}` is the proxy's answer to CONNECT: 403 is the
        # allowlist's refusal, 200 a tunnel, 502 an allowed host it could not
        # reach.
        assert re.search(r"connect=403 ", witness["sections"]["connect"]), _show(witness)


# -- The direct callers (GitLab #867) ----------------------------------------

#: The reviewer needs `developer.enabled` and a `repos_dir`. The other two only
#: need a stack, so all three share this profile.
DIRECT_PROFILE = "forge"

#: `temp_dir` on the lean shape, as `test_code_review_in_stack.py` restates it.
USER_TEMP = f"/data/tmp/{USER}"
DOCUMENT = f"{USER_TEMP}/cli-witness-document.txt"
DOCUMENT_TEXT = "cli-witness-document-7c2e"

REPOS = f"/data/repos/{USER}"
REVIEW_REPO = f"{REPOS}/cli-witness-review"
REVIEWED_FILE = "reviewed.txt"
TREE_TEXT = "cli-witness-tree-40ab"
REVIEWER_ANSWER = json.dumps({"findings": [], "ruled_out": []})

#: Roots' refusal, the same text the reviewer's own smoke file reads.
REFUSAL = "outside the allowed workspace"

#: Computed by the shell, so the command text cannot stand in for it having run.
BASH_RAN = "bash-ran-42"
BASH_CHECK = {"name": "Bash", "args": {"command": "echo bash-ran-$((6*7))"}}

DOCTOR_MARKER = "healthcheck-ok"

#: Doctor's Bash. `__HOME__` is the daemon's, which the fake's own `~` is not.
DIRECT_PROBE = r"""
echo WITNESS_BEGIN
echo "--- credfile"
cat __HOME__/.claude/.credentials.json 2>&1
echo
echo "--- harnessfile"
cat {harness_dir}/config/.credentials.json 2>&1
echo
""" + _ENV_PROBES + r"""
echo "--- end"
echo WITNESS_END
"""

#: Run with the image's interpreter; each prints one marked line and nothing a
#: tool returned.
DOCTOR_DRIVER = """
import json, sys
from pathlib import Path
from istota import doctor
from istota.config import load_config
from istota.llm.providers import resolve_process_auth
config = load_config(Path(sys.argv[1]))
resolve_process_auth(config)
check = doctor.check_model_execution(config, probe=True)
print("DRIVER", json.dumps({"status": check.status, "detail": check.detail}))
"""

OCR_DRIVER = """
import json, sys
from pathlib import Path
from istota.config import load_config
from istota.health._brain_call import call_health_brain
from istota.llm.providers import resolve_process_auth
config = load_config(Path(sys.argv[1]))
resolve_process_auth(config)
answer = call_health_brain(
    "witness", config, origin="health_ocr", user_id=sys.argv[2], read_path=Path(sys.argv[3]),
)
print("DRIVER", json.dumps({"answer": answer}))
"""

#: The reads a Read-only caller makes after its own document, in order.
READS = ("daemon_file", "harness_file", "cli_environ")


def _reads(home: str) -> list[dict]:
    return [
        {"name": "Read", "args": {"file_path": f"{home}/.claude/.credentials.json"}},
        {"name": "Read", "args": {"file_path": "{harness_dir}/config/.credentials.json"}},
        {"name": "Read", "args": {"file_path": "/proc/{cli_pid}/environ"}},
    ]


def _direct_plan(caller: str, home: str) -> dict:
    base = {
        "host_probe": HOST_PROBE,
        "run_in_cli": CONTROL == "cli-runs-tools",
        "results_file": f"{home}/.local/bin/results.json",
    }
    if caller == "doctor":
        return base | {"calls": [
            {"name": "Bash", "args": {"command": DIRECT_PROBE.replace("__HOME__", home)}},
            {"name": "Bash", "args": {"command": f"echo {DOCTOR_MARKER}"}},
        ]}
    if caller == "ocr":
        return base | {
            "result": "ocr-answered",
            "calls": [
                {"name": "Read", "args": {"file_path": DOCUMENT}},
                *_reads(home),
                BASH_CHECK,
            ],
        }
    # By a search relative to the snapshot's `tree/`: the run dir's name is random.
    return base | {
        "result": REVIEWER_ANSWER,
        "calls": [
            {"name": "Grep", "args": {"pattern": TREE_TEXT, "path": ".", "output_mode": "content"}},
            *_reads(home),
            BASH_CHECK,
        ],
    }


def _caller_argv(cred_env: str, *argv: str, extra: tuple[str, ...] = ()) -> list[str]:
    """`argv` in the daemon's container with the fake first on `PATH` and the
    dummy credential the one model credential in its environment."""
    return [
        "sh", "-c",
        'PATH="$HOME/.local/bin:$PATH" exec env -u CLAUDE_CODE_OAUTH_TOKEN -u ANTHROPIC_API_KEY "$@"',
        "sh", cred_env, *extra, *argv,
    ]


def _build_review_repo(stack) -> None:
    git = "git -c user.email=smoke@example.com -c user.name=Smoke"
    build = stack.exec(["sh", "-c", f"""
set -eu
mkdir -p {REPOS}
rm -rf {REVIEW_REPO}
git init -q -b main {REVIEW_REPO}
cd {REVIEW_REPO}
echo "first line" > {REVIEWED_FILE}
{git} add {REVIEWED_FILE}
{git} commit -q -m "Add {REVIEWED_FILE}"
echo {TREE_TEXT} >> {REVIEWED_FILE}
{git} commit -q -am "Extend {REVIEWED_FILE}"
"""], timeout=60)
    assert build.returncode == 0, f"building the reviewed repo failed\n{build.stderr}"


def _caller_output(stack, caller: str, mode: str, cred_env: str):
    if caller == "doctor":
        argv = _caller_argv(cred_env, "uv", "run", "python", "-c", DOCTOR_DRIVER, WITNESS_CONFIG)
    elif caller == "ocr":
        argv = _caller_argv(
            cred_env, "uv", "run", "python", "-c", OCR_DRIVER, WITNESS_CONFIG, USER, DOCUMENT,
        )
    else:
        _build_review_repo(stack)
        # What the skill proxy's `model_caller_env` copy carries in production.
        argv = _caller_argv(
            cred_env, "uv", "run", "python", "-m", "istota.skills.code_review", "run",
            "--worktree", REVIEW_REPO, "--base", "HEAD~1", "--intent", "credential witness",
            "--timeout", "120",
            extra=(
                f"ISTOTA_CLI_AUTH={mode}",
                f"ISTOTA_CONFIG_PATH={WITNESS_CONFIG}",
                f"ISTOTA_USER_ID={USER}",
                f"DEVELOPER_REPOS_DIR={REPOS}",
            ),
        )
    return stack.exec(argv, timeout=300)


def _run_direct(stack, caller: str, mode: str) -> dict:
    key = (id(stack), caller, mode)
    if key in _DIRECT_RUNS:
        return _DIRECT_RUNS[key]
    value, cred_env = _prepare(stack, mode)
    home = stack.exec(["sh", "-c", 'printf %s "$HOME"']).stdout
    plan = _direct_plan(caller, home)
    setup = stack.exec(["sh", "-c", " && ".join([
        f"mkdir -p {USER_TEMP}",
        f"echo {DOCUMENT_TEXT} > {DOCUMENT}",
        _b64_write(f"{home}/.local/bin/plan.json", json.dumps(plan), "0644"),
        f'rm -f "{plan["results_file"]}"',
    ])])
    assert setup.returncode == 0, f"writing the plan failed\n{setup.stderr}"

    ran = _caller_output(stack, caller, mode, cred_env)
    output = ran.stdout + "\n" + ran.stderr
    results = stack.exec(["cat", plan["results_file"]])
    if results.returncode != 0:
        raise AssertionError(
            f"{caller} never reached the fake's tool calls (exit {ran.returncode}), so "
            f"this says nothing about the credential\n--- output ---\n{output[-6000:]}"
        )
    host_text, *calls = json.loads(results.stdout)
    run = {
        "caller": caller,
        "mode": mode,
        "value": value,
        "output": output,
        "calls": calls,
        "host": dict(re.findall(r"^(host_\w+)=(\S*)$", host_text, re.M)),
    }
    if caller == "doctor":
        marked = re.search(r"^WITNESS_BEGIN$(.*?)^WITNESS_END$", calls[0], re.S | re.M)
        assert marked is not None, f"the probe's output never came back\n{calls[0][-6000:]}"
        run["sections"] = dict(re.findall(r"^--- (\w+)\n(.*?)(?=^--- )", marked.group(1), re.S | re.M))
    else:
        run["reads"] = dict(zip(READS, calls[1:4]))
        run["bash"] = calls[4]
    _DIRECT_RUNS[key] = run
    return run


_DIRECT_RUNS: dict[tuple[int, str, str], dict] = {}

MODES = ("subscription", "api_key")


def _params(*callers: str) -> list:
    return [pytest.param(c, m, id=f"{c}-{m}") for c in callers for m in MODES]


def _show_direct(run) -> str:
    return f"--- calls ---\n{json.dumps(run['calls'])[-6000:]}\n--- output ---\n{run['output'][-3000:]}"


def _driver_line(run) -> dict:
    for line in run["output"].splitlines():
        if line.startswith("DRIVER "):
            return json.loads(line[len("DRIVER "):])
    raise AssertionError(f"{run['caller']} printed no result line\n{_show_direct(run)}")


def _envelope(run) -> dict:
    for line in run["output"].splitlines():
        line = line.strip()
        if line.startswith("{") and '"status"' in line:
            return json.loads(line)
    raise AssertionError(f"the reviewer printed no envelope\n{_show_direct(run)}")


@pytest.mark.profile(DIRECT_PROFILE)
class TestTheDirectCallersToolsCannotReachIt:
    @pytest.mark.parametrize(("caller", "mode"), _params("doctor", "ocr", "reviewer"))
    def test_the_caller_ran_and_the_cli_held_the_credential(self, fake_cli, caller, mode):
        """The control: an absence below is not a caller that stopped early, a
        tool server that serves nothing or a credential the CLI never had."""
        run = _run_direct(fake_cli, caller, mode)
        assert run["host"].get("host_harness") == "yes", _show_direct(run)
        assert run["host"].get("host_env_cred", "0") != "0", _show_direct(run)
        if mode == "subscription":
            assert run["host"].get("host_file_cred", "0") != "0", _show_direct(run)
            # The spelling the file probes below use, so they are not a wrong path.
            assert run["host"].get("host_harness_link", "0") != "0", _show_direct(run)
        if caller == "doctor":
            assert _driver_line(run)["status"] == "ok", _show_direct(run)
            assert set(run["sections"]) >= {"credfile", "harnessfile", "env", "proc", "netns"}
            assert re.search(r"proc_readable=[1-9]", run["sections"]["proc"]), _show_direct(run)
        elif caller == "ocr":
            assert _driver_line(run)["answer"] == "ocr-answered", _show_direct(run)
            assert DOCUMENT_TEXT in run["calls"][0], _show_direct(run)
        else:
            answer = _envelope(run)
            assert answer["status"] == "ok", _show_direct(run)
            assert answer["reviewer"]["tools"] is True, _show_direct(run)
            # The tree line, not just the text: `meta/diff.patch` carries it too.
            assert f"{REVIEWED_FILE}:2:{TREE_TEXT}" in run["calls"][0], _show_direct(run)

    @pytest.mark.parametrize(("caller", "mode"), _params("doctor", "ocr", "reviewer"))
    def test_no_tool_result_or_output_carries_it(self, fake_cli, caller, mode):
        run = _run_direct(fake_cli, caller, mode)
        for text in [*run["calls"], run["output"]]:
            assert run["value"] not in text, _show_direct(run)

    @pytest.mark.parametrize(("caller", "mode"), _params("doctor", "ocr", "reviewer"))
    def test_the_credentials_files_are_out_of_reach(self, fake_cli, caller, mode):
        run = _run_direct(fake_cli, caller, mode)
        if caller == "doctor":
            for section in ("credfile", "harnessfile"):
                assert run["value"] not in run["sections"][section], _show_direct(run)
            return
        for read in ("daemon_file", "harness_file"):
            assert REFUSAL in run["reads"][read], _show_direct(run)

    @pytest.mark.parametrize(("caller", "mode"), _params("doctor", "ocr", "reviewer"))
    def test_the_clis_process_environment_is_out_of_reach(self, fake_cli, caller, mode):
        run = _run_direct(fake_cli, caller, mode)
        if caller == "doctor":
            assert run["value"] not in run["sections"]["env"], _show_direct(run)
            assert run["value"] not in run["sections"]["proc"], _show_direct(run)
            return
        assert REFUSAL in run["reads"]["cli_environ"], _show_direct(run)

    @pytest.mark.parametrize(("caller", "mode"), _params("ocr", "reviewer"))
    def test_a_read_only_caller_has_no_bash(self, fake_cli, caller, mode):
        run = _run_direct(fake_cli, caller, mode)
        assert BASH_RAN not in run["bash"], _show_direct(run)
        assert run["bash"].startswith("ERROR"), _show_direct(run)

    @pytest.mark.parametrize(("caller", "mode"), _params("doctor"))
    def test_doctors_tools_share_the_daemons_network_namespace(self, fake_cli, caller, mode):
        """No `--unshare-net` and no CONNECT proxy on this path. The task class
        asserts the opposite with the same comparison."""
        run = _run_direct(fake_cli, caller, mode)
        host = run["host"].get("host_netns", "")
        assert host.startswith("net:["), _show_direct(run)
        assert host in run["sections"]["netns"], _show_direct(run)


# -- The namespace OCR and the reviewer run in (GitLab #867) ------------------

WRAP_PROBE = r"""
echo WITNESS_BEGIN
echo "--- credfile"
cat __HOME__/.claude/.credentials.json 2>&1
echo
echo "--- harnessroot"
cat __HARNESS__/witness-credential 2>&1
echo
""" + _ENV_PROBES + r"""
echo "--- dbmask"
stat -f -c %T /data/db
echo "--- end"
echo WITNESS_END
"""

#: The tool server's own env is the launcher's file and holds no credential, so
#: the wrapped probe gets a bare one and the driver above it holds the value.
#: The control runs the probe unwrapped with the driver's environment.
WRAP_DRIVER = """
import json, os, subprocess, sys
from pathlib import Path
from istota.config import load_config
from istota.executor import (
    SandboxProfile, build_daemon_sandbox, ensure_task_harness_dir, release_daemon_sandbox,
)
from istota.skills.code_review import _reviewer_withheld_scopes
config_path, user, caller, probe, control = sys.argv[1:6]
config = load_config(Path(config_path))
value = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ["ISTOTA_PROVIDER_ANTHROPIC_API_KEY"]
home = os.environ["HOME"]
planted = ensure_task_harness_dir(config, user, 0) / "witness-credential"
planted.write_text(value)
withheld = _reviewer_withheld_scopes(config) if caller == "reviewer" else frozenset()
box = build_daemon_sandbox(config, user, profile=SandboxProfile.NATIVE, withheld_scopes=withheld)
try:
    probe = probe.replace("__HOME__", home).replace("__HARNESS__", str(planted.parent))
    argv = ["bash", "-c", probe]
    env = {"PATH": os.environ["PATH"], "HOME": home}
    if control:
        env = dict(os.environ)
    else:
        argv = box.tool_server_wrap(argv)
    ran = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=box.work_dir)
    credfile = Path(home, ".claude", ".credentials.json")
    print("DRIVER", json.dumps({
        "refused": box.refused,
        "file_holds_value": credfile.exists() and value in credfile.read_text(),
        "stdout": ran.stdout,
        "stderr": ran.stderr[-1000:],
    }))
finally:
    planted.unlink(missing_ok=True)
    release_daemon_sandbox(box)
"""

_WRAP_RUNS: dict[tuple[int, str, str], dict] = {}


def _run_wrap(stack, caller: str, mode: str) -> dict:
    key = (id(stack), caller, mode)
    if key in _WRAP_RUNS:
        return _WRAP_RUNS[key]
    value, cred_env = _prepare(stack, mode)
    made = stack.exec(["mkdir", "-p", USER_TEMP])
    assert made.returncode == 0, made.stderr
    ran = stack.exec(_caller_argv(
        cred_env, "uv", "run", "python", "-c", WRAP_DRIVER,
        WITNESS_CONFIG, USER, caller, WRAP_PROBE, CONTROL,
    ), timeout=120)
    output = ran.stdout + "\n" + ran.stderr
    run = {"caller": caller, "mode": mode, "value": value, "output": output, "calls": []}
    driver = _driver_line(run)
    marked = re.search(r"^WITNESS_BEGIN$(.*?)^WITNESS_END$", driver["stdout"], re.S | re.M)
    assert marked is not None, f"the probe's output never came back\n{output[-6000:]}"
    run["driver"] = driver
    run["sections"] = dict(re.findall(r"^--- (\w+)\n(.*?)(?=^--- )", marked.group(1), re.S | re.M))
    _WRAP_RUNS[key] = run
    return run


@pytest.mark.profile(DIRECT_PROFILE)
@pytest.mark.parametrize(("caller", "mode"), _params("ocr", "reviewer"))
class TestTheDaemonSandboxHoldsNoCredential:
    def test_the_probes_ran_and_the_daemon_held_the_credential(self, fake_cli, caller, mode):
        run = _run_wrap(fake_cli, caller, mode)
        assert run["driver"]["refused"] is False, run["output"][-3000:]
        assert re.search(r"proc_readable=[1-9]", run["sections"]["proc"]), run["output"][-3000:]
        if mode == "subscription":
            assert run["driver"]["file_holds_value"] is True, run["output"][-3000:]

    def test_it_is_a_real_namespace(self, fake_cli, caller, mode):
        """An unconfined run sees the volume at the database directory."""
        run = _run_wrap(fake_cli, caller, mode)
        assert run["sections"]["dbmask"].strip() == "tmpfs", run["output"][-3000:]

    def test_the_credentials_file_and_harness_root_are_out_of_reach(self, fake_cli, caller, mode):
        run = _run_wrap(fake_cli, caller, mode)
        assert run["value"] not in run["sections"]["credfile"], run["output"][-3000:]
        assert run["value"] not in run["sections"]["harnessroot"], run["output"][-3000:]

    def test_no_environment_it_can_read_holds_it(self, fake_cli, caller, mode):
        run = _run_wrap(fake_cli, caller, mode)
        assert run["value"] not in run["sections"]["env"], run["output"][-3000:]
        assert run["value"] not in run["sections"]["proc"], run["output"][-3000:]
