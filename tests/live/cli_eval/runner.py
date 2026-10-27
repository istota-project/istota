"""Run one eval case through the `claude` CLI and measure it from its stream-json.

Two arms over the same cases, model, prompt and timeout:

- ``builtin``: today's tool argv (`build_claude_cli_flags`), the CLI's own tools,
  cwd the case's working directory.
- ``spike``: every machine-acting built-in off (`--tools WebSearch`, the harness
  deny list), the six core tools served by one stdio MCP server named ``spike``,
  and the CLI's cwd an empty directory, as the harness will run it. The server
  is the throwaway from the spec's Stage 2 and is not in the tree; its path
  comes from ``ISTOTA_CLI_EVAL_SPIKE_SERVER``. Stage 7 replaces this arm with
  `ClaudeCodeBrain` on the real harness.
- ``spike_cwd``: the spike with the CLI's cwd the case's working directory, a
  diagnostic. The CLI's own prompt names its cwd as the working directory and
  the model builds absolute paths from it, so this separates that cost from
  the cost of the tools themselves.

Both arms get a scratch ``HOME``, ``TMPDIR`` and ``CLAUDE_CONFIG_DIR`` and the
CLI switches the spec lists, and nothing of the test process's environment but
``PATH`` and the one Anthropic credential. Auth is first-party only: a CLI run
of a third-party endpoint measures something production never runs.

The case machinery (`prepare_workdir`, `run_check`, `task_env`, the report and
the spend cap) is the native eval's, imported rather than copied.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from istota.brain._types import BrainRequest
from istota.brain.claude_code import build_claude_cli_flags, cli_resolve_alias
from istota.session.tools.bash import bash_result_failed

from ..native_eval import runner as native_runner
from ..native_eval.runner import EvalCase, prepare_workdir, run_check, task_env

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO_ROOT / "docker" / "istota" / "Dockerfile"

ARMS = ("builtin", "spike")
SPIKE_ARMS = ("spike", "spike_cwd")
SPIKE_SERVER_KEY = "spike"
MODEL_TIER = "general"

ANTHROPIC_HOSTS = frozenset({"api.anthropic.com"})

# The spec's HARNESS_DENIED_TOOLS plus today's Agent and Workflow. Stage 6 adds
# the constant to `brain/claude_code.py`; this copy goes then.
HARNESS_DENIED_TOOLS = (
    "Agent", "Workflow",
    "Bash", "BashOutput", "KillShell", "Monitor", "Read", "Write", "Edit",
    "MultiEdit", "NotebookEdit", "Glob", "Grep", "LS", "WebFetch", "Skill",
    "TodoWrite", "Task", "TaskStop",
)
SPIKE_BUILTINS = ("WebSearch",)
OFFERED_TOOLS = ("Read", "Write", "Edit", "Grep", "Glob", "Bash")

# What the spec's `cli_env` sets on every harness CLI, and so on both arms here.
CLI_SWITCHES = {
    "CLAUDE_CODE_DISABLE_ATTACHMENTS": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_DISABLE_ADVISOR_TOOL": "1",
    "DISABLE_AUTOUPDATER": "1",
}

_MCP_PREFIX = re.compile(r"\Amcp__[^_]+(?:_[^_]+)*?__")


class AuthError(Exception):
    """The environment offers no first-party credential, or a foreign endpoint."""


# --------------------------------------------------------------------------
# Auth, model and binary
# --------------------------------------------------------------------------


def anthropic_auth(env: dict[str, str]) -> tuple[str, dict[str, str]]:
    """``(mode, the one credential variable)`` for the CLI, first-party only.

    Refuses an ``ANTHROPIC_BASE_URL`` naming any host but Anthropic's, and never
    forwards ``ANTHROPIC_AUTH_TOKEN``, which is how a third-party endpoint is
    keyed. The subscription token wins over a key, as the brain's own default
    mode does.
    """
    base_url = (env.get("ANTHROPIC_BASE_URL") or "").strip()
    if base_url:
        host = urlparse(base_url).hostname or ""
        if host not in ANTHROPIC_HOSTS:
            raise AuthError(f"ANTHROPIC_BASE_URL names {host or base_url!r}, not Anthropic")
    token = (env.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
    if token:
        return "subscription", {"CLAUDE_CODE_OAUTH_TOKEN": token}
    key = (env.get("ANTHROPIC_API_KEY") or "").strip()
    if key:
        return "api_key", {"ANTHROPIC_API_KEY": key}
    raise AuthError("neither CLAUDE_CODE_OAUTH_TOKEN nor ANTHROPIC_API_KEY is set")


def general_model() -> tuple[str, str | None]:
    """The claude_code brain's ``general`` tier, through its own resolver."""
    resolved = cli_resolve_alias(MODEL_TIER)
    if resolved is None or not resolved[0]:
        raise RuntimeError(f"the claude_code brain does not resolve {MODEL_TIER!r}")
    return resolved[0], resolved[1]


def pinned_cli_version() -> str:
    """``CLAUDE_CODE_VERSION`` as the image pins it."""
    match = re.search(r"^ARG CLAUDE_CODE_VERSION=(\S+)", DOCKERFILE.read_text(), re.M)
    if match is None:
        raise RuntimeError(f"no CLAUDE_CODE_VERSION pin in {DOCKERFILE}")
    return match.group(1)


def cli_version(claude: str, env: dict[str, str]) -> str:
    proc = subprocess.run(
        [claude, "--version"], capture_output=True, text=True, timeout=60, env=env
    )
    return (proc.stdout.strip().split() or [""])[0]


# --------------------------------------------------------------------------
# Argv and environment
# --------------------------------------------------------------------------


def base_request(system_file: Path, model: str, effort: str | None, timeout: int) -> BrainRequest:
    return BrainRequest(
        prompt="",
        allowed_tools=list(OFFERED_TOOLS),
        cwd=system_file.parent,
        env={},
        model=model,
        effort=effort or "",
        timeout_seconds=timeout,
        composed_system_prompt_path=system_file.resolve(),
        streaming=True,
    )


def build_argv(
    arm: str, claude: str, req: BrainRequest, *, mcp_config: Path | None = None
) -> list[str]:
    """The arm's argv: today's flags, and for the spike the harness changes."""
    flags = build_claude_cli_flags(req)
    if arm in SPIKE_ARMS:
        if mcp_config is None:
            raise ValueError("the spike arm needs an MCP config")
        # Today's flags open with the deny list; the harness widens it.
        if flags[:3] != ["--disallowedTools", "Agent", "Workflow"]:
            raise RuntimeError(f"unexpected flag order from build_claude_cli_flags: {flags[:3]}")
        flags = [
            "--disallowedTools", *HARNESS_DENIED_TOOLS,
            "--tools", ",".join(SPIKE_BUILTINS),
            "--mcp-config", str(mcp_config), "--strict-mcp-config",
            *flags[3:],
        ]
    elif arm != "builtin":
        raise ValueError(f"unknown arm {arm!r}")
    return [
        claude, "-p", "-", *flags, "--dangerously-skip-permissions",
        "--output-format", "stream-json", "--verbose",
    ]


def expected_spike_tools() -> set[str]:
    return set(SPIKE_BUILTINS) | {f"mcp__{SPIKE_SERVER_KEY}__{name}" for name in OFFERED_TOOLS}


def scratch_dirs(root: Path) -> dict[str, Path]:
    dirs = {name: root / name for name in ("home", "tmp", "config", "work")}
    for path in dirs.values():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return dirs


def cli_env(path_value: str, dirs: dict[str, Path], auth: dict[str, str], timeout: int) -> dict[str, str]:
    """The CLI's whole environment: built from nothing, never inherited."""
    env = {
        "PATH": path_value,
        "LANG": "C.UTF-8",
        "HOME": str(dirs["home"]),
        "TMPDIR": str(dirs["tmp"]),
        "CLAUDE_CONFIG_DIR": str(dirs["config"]),
        "MCP_TOOL_TIMEOUT": str((timeout + 30) * 1000),
        **CLI_SWITCHES,
    }
    env.update(auth)
    return env


def write_spike_config(
    path: Path, server: Path, workdir: Path, env_file: Path, call_log: Path, spill_dir: Path
) -> Path:
    doc = {"mcpServers": {SPIKE_SERVER_KEY: {
        "type": "stdio",
        "command": sys.executable,
        "args": [
            str(server), "--root", str(workdir), "--env-file", str(env_file),
            "--log", str(call_log), "--spill-dir", str(spill_dir),
        ],
        "env": {},
    }}}
    path.write_text(json.dumps(doc))
    return path


# --------------------------------------------------------------------------
# Metrics from the stream
# --------------------------------------------------------------------------


def canonical_tool_name(name: str) -> str:
    """``mcp__spike__Bash`` -> ``Bash``; any other name unchanged."""
    return _MCP_PREFIX.sub("", name or "", count=1)


def _blocks(frame: dict) -> list[dict]:
    message = frame.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def metrics_from_stream(path: Path, success_marker: str | None = None) -> dict:
    """Counts from one run's tee'd ``--output-format stream-json``.

    A tool error is a ``tool_result`` with ``is_error``, or a Bash result whose
    text reports a failed command (the tool server's Bash leaves ``is_error``
    off on a non-zero exit, as native's does). Names are canonicalised, so the
    two arms count alike. Lines that are not JSON objects are skipped.
    """
    out: dict = {
        "init_tools": None,
        "init_mcp_servers": None,
        "tool_calls": 0,
        "tool_errors": 0,
        "edit_failures": 0,
        "tool_errors_before_success": None,
        "tools_used": {},
        "raw_tool_names": [],
        "compactions": 0,
        "persisted_outputs": 0,
        "config_dir_reads": 0,
        "image_results": 0,
        "turns": 0,
        "stop_reason": "",
        "is_error": None,
        "result_text": "",
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    names: dict[str, str] = {}
    used: Counter = Counter()
    raw: set[str] = set()
    errors_so_far = 0
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if not isinstance(frame, dict):
            continue
        kind = frame.get("type")
        if kind == "system" and frame.get("subtype") == "init" and out["init_tools"] is None:
            out["init_tools"] = sorted(str(t) for t in frame.get("tools") or [])
            out["init_mcp_servers"] = [
                {"name": s.get("name"), "status": s.get("status")}
                for s in frame.get("mcp_servers") or [] if isinstance(s, dict)
            ]
        elif kind == "system" and frame.get("subtype") == "compact_boundary":
            out["compactions"] += 1
        elif kind == "assistant":
            for block in _blocks(frame):
                if block.get("type") != "tool_use":
                    continue
                name = str(block.get("name") or "")
                raw.add(name)
                canonical = canonical_tool_name(name)
                names[str(block.get("id") or "")] = canonical
                used[canonical] += 1
                args = block.get("input") if isinstance(block.get("input"), dict) else {}
                target = str(args.get("file_path") or args.get("path") or "")
                if "/tool-results/" in target:
                    out["config_dir_reads"] += 1
        elif kind == "user":
            for block in _blocks(frame):
                if block.get("type") != "tool_result":
                    continue
                out["tool_calls"] += 1
                tool = names.get(str(block.get("tool_use_id") or ""), "")
                content = block.get("content")
                text = _result_text(content)
                if isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "image" for b in content
                ):
                    out["image_results"] += 1
                if "<persisted-output>" in text or "exceeds maximum allowed tokens" in text:
                    out["persisted_outputs"] += 1
                failed = bool(block.get("is_error")) or (tool == "Bash" and bash_result_failed(text))
                if failed:
                    out["tool_errors"] += 1
                    errors_so_far += 1
                    if tool in ("Edit", "MultiEdit"):
                        out["edit_failures"] += 1
                    continue
                if (
                    success_marker
                    and out["tool_errors_before_success"] is None
                    and success_marker in text
                ):
                    out["tool_errors_before_success"] = errors_so_far
        elif kind == "result":
            out["turns"] = int(frame.get("num_turns") or 0)
            out["stop_reason"] = str(frame.get("subtype") or "")
            out["is_error"] = bool(frame.get("is_error"))
            result = frame.get("result")
            out["result_text"] = result if isinstance(result, str) else ""
            cost = frame.get("total_cost_usd")
            out["cost_usd"] = float(cost) if isinstance(cost, (int, float)) else 0.0
            usage = frame.get("usage") if isinstance(frame.get("usage"), dict) else {}
            out["input_tokens"] = int(usage.get("input_tokens") or 0)
            out["output_tokens"] = int(usage.get("output_tokens") or 0)
            out["cache_read_tokens"] = int(usage.get("cache_read_input_tokens") or 0)
            out["cache_write_tokens"] = int(usage.get("cache_creation_input_tokens") or 0)
    out["tools_used"] = dict(sorted(used.items()))
    out["raw_tool_names"] = sorted(raw)
    return out


def max_concurrency(call_log: Path) -> int:
    """The most spike tool calls that were running at once, from its call log."""
    if not call_log.is_file():
        return 0
    peak = 0
    for line in call_log.read_text().splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("event") == "start":
            peak = max(peak, int(record.get("concurrent") or 0))
    return peak


# --------------------------------------------------------------------------
# One run
# --------------------------------------------------------------------------


@dataclass
class Settings:
    claude: str
    model: str
    effort: str | None
    auth: dict[str, str]
    spike_server: Path | None = None


@dataclass
class CliEvalResult:
    case: str
    arm: str
    run: int
    passed: bool = False
    check_detail: str = ""
    returncode: int | None = None
    timed_out: bool = False
    wall_seconds: float = 0.0
    max_concurrent_calls: int = 0
    stream: str = ""
    error: str = ""
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        row = dataclasses.asdict(self)
        metrics = row.pop("metrics")
        metrics["result_text"] = (metrics.get("result_text") or "")[:2000]
        row.update(metrics)
        return row


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_case(
    case: EvalCase, arm: str, run: int, root: Path, settings: Settings, *, keep_dir: Path
) -> CliEvalResult:
    """Run ``case`` once in ``arm`` under ``root`` (created here) and measure it.

    ``keep_dir`` receives the stream-json and, for the spike, its call log, so
    the report can name files that outlive pytest's basetemp.
    """
    root = Path(root)
    workdir = root / "repo"
    dirs = scratch_dirs(root)
    control = root / "control"
    control.mkdir(parents=True, exist_ok=True)
    system_file = control / "system.md"
    system_file.write_text(native_runner.SYSTEM_PROMPT_FILE.read_text())
    prepare_workdir(case, workdir)
    tools_env = task_env(workdir)
    (workdir.parent / "tmp").mkdir(exist_ok=True)

    keep_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{arm}-{case.name}-{run}"
    stream_path = keep_dir / f"{stem}.jsonl"
    call_log = keep_dir / f"{stem}.calls.jsonl"
    result = CliEvalResult(case=case.name, arm=arm, run=run, stream=str(stream_path))

    req = base_request(system_file, settings.model, settings.effort, case.timeout)
    if arm in SPIKE_ARMS:
        if settings.spike_server is None:
            raise ValueError("ISTOTA_CLI_EVAL_SPIKE_SERVER is not set")
        env_file = control / "tool_env.json"
        env_file.write_text(json.dumps(tools_env))
        mcp_config = write_spike_config(
            control / "mcp.json", settings.spike_server, workdir, env_file,
            call_log, workdir.parent / "tmp",
        )
        argv = build_argv(arm, settings.claude, req, mcp_config=mcp_config)
        cwd = dirs["work"] if arm == "spike" else workdir
    else:
        argv = build_argv(arm, settings.claude, req)
        cwd = workdir
    env = cli_env(tools_env["PATH"], dirs, settings.auth, case.timeout)

    prompt = native_runner._prompt_text(case.prompt)
    started = time.monotonic()
    with stream_path.open("w", encoding="utf-8") as out, open(os.devnull, "w") as devnull:
        process = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=out, stderr=devnull, text=True,
            cwd=str(cwd), env=env, start_new_session=True,
        )
        try:
            process.stdin.write(prompt)
            process.stdin.close()
            result.returncode = process.wait(timeout=case.timeout)
        except subprocess.TimeoutExpired:
            result.timed_out = True
            _kill_group(process)
            result.returncode = process.wait()
    result.wall_seconds = round(time.monotonic() - started, 2)
    # The CLI leaves the stdio server to exit on stdin EOF; nothing else to reap.

    result.metrics = metrics_from_stream(stream_path, case.success_marker)
    result.max_concurrent_calls = max_concurrency(call_log)
    passed, detail = run_check(case, workdir, result.metrics["result_text"], env=tools_env)
    result.passed = passed and not result.timed_out
    result.check_detail = detail if not result.timed_out else f"timed out; {detail}"
    if result.metrics["is_error"]:
        result.error = result.metrics["result_text"][:500]
    return result


def init_smoke(arm: str, root: Path, settings: Settings) -> dict:
    """One no-model run: a bogus key, the arm's argv, and the ``system/init`` it gets.

    The CLI emits ``system/init`` before its first API request (Stage 1), so
    this checks the tool set and the server's connection for free.
    """
    case = EvalCase(name="init_smoke", fixture=Path(__file__).parent / "fixtures" / "read_image",
                    prompt="Say hello.", timeout=120)
    smoke = dataclasses.replace(settings, auth={"ANTHROPIC_API_KEY": "sk-ant-bogus-init-smoke"})
    result = run_case(case, arm, 1, root, smoke, keep_dir=root / "kept")
    return {
        "init_tools": result.metrics["init_tools"],
        "init_mcp_servers": result.metrics["init_mcp_servers"],
        "is_error": result.metrics["is_error"],
        "result_text": result.metrics["result_text"],
        "tool_calls": result.metrics["tool_calls"],
    }
