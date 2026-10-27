"""Run one eval case through `ClaudeCodeBrain.execute` and measure it from its stream-json.

Two arms over the same cases, model, prompt and timeout, both on the brain's
real code path:

- ``builtin``: no harness. The brain runs the CLI unwrapped with its own
  tools, cwd the case's working directory, as the stack runs it today minus
  the bwrap wrap.
- ``harness``: ``BrainRequest.harness`` set. The brain runs the CLI unwrapped
  with every machine-acting built-in off and the six core tools served through
  the real adapter, bridge and tool server; the tool server runs unwrapped
  with the working directory as its cwd and no file roots, as the native eval
  runs native.

Both arms get a scratch ``HOME``, ``TMPDIR`` and ``CLAUDE_CONFIG_DIR`` (the
harness makes its own per attempt) and the CLI switches the harness sets, and
nothing of the test process's environment but ``PATH`` and the one Anthropic
credential. Auth is first-party only: a CLI run of a third-party endpoint
measures something production never runs.

The brain consumes the CLI's stdout, so a per-run ``claude`` shim, first on
the CLI's PATH, tees it to a file for ``metrics_from_stream``; the shim takes
its own directory back off PATH before it starts the real CLI.

The case machinery (`prepare_workdir`, `run_check`, `task_env`, the report and
the spend cap) is the native eval's, imported rather than copied.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shlex
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from istota.brain._types import BrainRequest, BrainResult, HarnessSpec
from istota.brain.claude_code import ClaudeCodeBrain, cli_resolve_alias
from istota.executor import HARNESS_CLI_SWITCHES, HARNESS_MCP_TIMEOUT_MARGIN_SECONDS
from istota.llm.providers import CLI_AUTH_VARIABLE
from istota.session.tools.bash import bash_result_failed

from ..native_eval import runner as native_runner
from ..native_eval.runner import EvalCase, prepare_workdir, run_check, task_env

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO_ROOT / "docker" / "istota" / "Dockerfile"

ARMS = ("builtin", "harness")
MODEL_TIER = "general"
EVAL_USER = "eval"

ANTHROPIC_HOSTS = frozenset({"api.anthropic.com"})

# What a member's task is granted on a CLI brain: the six core tools and
# WebSearch, which the harness keeps as the CLI's one built-in.
CORE_TOOLS = ("Read", "Write", "Edit", "Grep", "Glob", "Bash")
GRANTED_TOOLS = (*CORE_TOOLS, "WebSearch")
HARNESS_SERVER_KEY = "istota"

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
# The request and the CLI's environment
# --------------------------------------------------------------------------


def scratch_dirs(root: Path) -> dict[str, Path]:
    dirs = {name: root / name for name in ("home", "tmp", "config", "harness")}
    for path in dirs.values():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return dirs


def cli_switches(timeout: int) -> dict[str, str]:
    """The harness's own CLI switches, from the executor, for both arms."""
    timeout_ms = (int(timeout) + HARNESS_MCP_TIMEOUT_MARGIN_SECONDS) * 1000
    return {**HARNESS_CLI_SWITCHES, "MCP_TOOL_TIMEOUT": str(timeout_ms)}


def cli_env(path_value: str, dirs: dict[str, Path], auth: dict[str, str], timeout: int) -> dict[str, str]:
    """The built-in arm's CLI environment: built from nothing, never inherited.

    ``SHELLOPTS=pipefail`` is what `build_clean_env` gives every task env, so
    the CLI's own Bash fails a pipeline as the tool server's Bash does.
    """
    env = {
        "PATH": path_value,
        "LANG": "C.UTF-8",
        "HOME": str(dirs["home"]),
        "TMPDIR": str(dirs["tmp"]),
        "CLAUDE_CONFIG_DIR": str(dirs["config"]),
        "SHELLOPTS": "pipefail",
        **cli_switches(timeout),
    }
    env.update(auth)
    return env


def harness_cli_env(
    path_value: str, home: Path, auth_mode: str, auth: dict[str, str], timeout: int,
) -> dict[str, str]:
    """``HarnessSpec.cli_env``: what `build_harness_cli_env` gives the CLI,
    less the proxy names and the allowlist a deployment adds. The brain sets
    ``CLAUDE_CONFIG_DIR``, ``HOME`` and ``TMPDIR`` per harness; ``HOME`` here
    only decides where the config dir looks for a credentials file, and the
    scratch one has none."""
    env = {
        "PATH": path_value,
        "LANG": "C.UTF-8",
        "HOME": str(home),
        CLI_AUTH_VARIABLE: auth_mode,
        **cli_switches(timeout),
    }
    env.update(auth)
    return env


def build_request(
    arm: str, *, prompt: str, workdir: Path, system_file: Path, model: str,
    effort: str | None, timeout: int, run: int, path_value: str,
    dirs: dict[str, Path], auth_mode: str, auth: dict[str, str],
) -> BrainRequest:
    """One case's `BrainRequest` in ``arm``. Neither arm is sandbox-wrapped."""
    base = dict(
        prompt=prompt,
        allowed_tools=list(GRANTED_TOOLS),
        cwd=workdir.resolve(),
        model=model,
        effort=effort or "",
        timeout_seconds=timeout,
        composed_system_prompt_path=system_file.resolve(),
        task_id=1,
        attempt=run,
        user_id=EVAL_USER,
        source_type="eval",
        streaming=True,
    )
    if arm == "builtin":
        return BrainRequest(env=cli_env(path_value, dirs, auth, timeout), **base)
    if arm == "harness":
        # `req.env` is the tool server's: the model's Bash gets the native
        # eval's task env, and the CLI gets `HarnessSpec.cli_env`.
        return BrainRequest(
            env=task_env(workdir),
            harness=HarnessSpec(
                cli_env=harness_cli_env(path_value, dirs["home"], auth_mode, auth, timeout),
                harness_root=dirs["harness"],
            ),
            **base,
        )
    raise ValueError(f"unknown arm {arm!r}")


def expected_harness_tools() -> set[str]:
    return {"WebSearch"} | {f"mcp__{HARNESS_SERVER_KEY}__{name}" for name in CORE_TOOLS}


def write_tee_shim(bin_dir: Path, claude: str, stream_path: Path) -> Path:
    """``{bin_dir}/claude``: runs ``claude`` with ``bin_dir`` off PATH and
    appends its stdout to ``stream_path`` on the way through.

    Appends, so a CLI the brain respawns (the transient-API retry) adds to the
    same file. A signal death of the real CLI is passed on as one, so the
    brain's signal classification sees what it would have seen without the
    shim.
    """
    bin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    shim = bin_dir / "claude"
    shim.write_text(
        "#!/bin/bash\n"
        f"shim_dir={shlex.quote(str(bin_dir))}\n"
        'p=":$PATH:"; p="${p//:$shim_dir:/:}"; p="${p#:}"; export PATH="${p%:}"\n'
        f'{shlex.quote(claude)} "$@" | /usr/bin/tee -a {shlex.quote(str(stream_path))}\n'
        "rc=${PIPESTATUS[0]}\n"
        'if [ "$rc" -gt 128 ]; then kill -s "$(kill -l $((rc - 128)))" $$; fi\n'
        'exit "$rc"\n'
    )
    shim.chmod(0o700)
    return shim


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


# --------------------------------------------------------------------------
# One run
# --------------------------------------------------------------------------


@dataclass
class Settings:
    claude: str
    model: str
    effort: str | None
    auth: dict[str, str]
    auth_mode: str = ""


@dataclass
class CliEvalResult:
    case: str
    arm: str
    run: int
    passed: bool = False
    check_passed: bool = False
    check_detail: str = ""
    brain_success: bool | None = None
    brain_stop_reason: str = ""
    brain_text: str = ""
    wall_seconds: float = 0.0
    stream: str = ""
    error: str = ""
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        row = dataclasses.asdict(self)
        metrics = row.pop("metrics")
        metrics["result_text"] = (metrics.get("result_text") or "")[:2000]
        row["brain_text"] = (row.get("brain_text") or "")[:2000]
        row.update(metrics)
        return row


def run_case(
    case: EvalCase, arm: str, run: int, root: Path, settings: Settings, *, keep_dir: Path
) -> CliEvalResult:
    """Run ``case`` once in ``arm`` under ``root`` (created here) and measure it.

    ``keep_dir`` receives the tee'd stream-json, so the report can name files
    that outlive pytest's basetemp.
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
    stream_path.unlink(missing_ok=True)
    result = CliEvalResult(case=case.name, arm=arm, run=run, stream=str(stream_path))

    shim_dir = root / "shim"
    write_tee_shim(shim_dir, settings.claude, stream_path)
    req = build_request(
        arm, prompt=native_runner._prompt_text(case.prompt), workdir=workdir,
        system_file=system_file, model=settings.model, effort=settings.effort,
        timeout=case.timeout, run=run,
        path_value=os.pathsep.join([str(shim_dir), tools_env["PATH"]]),
        dirs=dirs, auth_mode=settings.auth_mode, auth=settings.auth,
    )

    started = time.monotonic()
    try:
        brain_result: BrainResult = ClaudeCodeBrain().execute(req)
    except Exception as exc:  # noqa: BLE001 — the brain is not meant to raise
        result.wall_seconds = round(time.monotonic() - started, 2)
        result.error = f"{type(exc).__name__}: {exc}"
        result.metrics = metrics_from_stream(stream_path, case.success_marker) if stream_path.exists() else {}
        return result
    result.wall_seconds = round(time.monotonic() - started, 2)
    result.brain_success = bool(brain_result.success)
    result.brain_stop_reason = brain_result.stop_reason or ""
    result.brain_text = brain_result.result_text or ""

    if stream_path.exists():
        result.metrics = metrics_from_stream(stream_path, case.success_marker)
    else:
        result.metrics = metrics_from_stream(_empty(control / "empty.jsonl"), case.success_marker)
    timed_out = result.brain_stop_reason == "timeout"
    passed, detail = run_check(case, workdir, result.metrics["result_text"], env=tools_env)
    result.check_passed = passed
    # Passed means the user got an answer: the check holds, and the brain
    # neither timed out nor reported a failure.
    result.passed = passed and not timed_out and bool(result.brain_success)
    result.check_detail = detail if not timed_out else f"timed out; {detail}"
    if not result.brain_success:
        result.error = result.brain_text[:500]
    return result


def _empty(path: Path) -> Path:
    path.write_text("")
    return path


def init_smoke(arm: str, root: Path, settings: Settings) -> dict:
    """One no-model run: a bogus key, the arm's request, and the ``system/init`` it gets.

    The CLI emits ``system/init`` before its first API request (Stage 1), so
    this checks the tool set and, on the harness, the server's connection and
    the brain's own init check, for free.
    """
    case = EvalCase(name="init_smoke", fixture=Path(__file__).parent / "fixtures" / "read_image",
                    prompt="Say hello.", timeout=120)
    smoke = dataclasses.replace(
        settings, auth={"ANTHROPIC_API_KEY": "sk-ant-bogus-init-smoke"}, auth_mode="api_key",
    )
    result = run_case(case, arm, 1, root, smoke, keep_dir=root / "kept")
    return {
        "init_tools": result.metrics["init_tools"],
        "init_mcp_servers": result.metrics["init_mcp_servers"],
        "is_error": result.metrics["is_error"],
        "result_text": result.metrics["result_text"],
        "tool_calls": result.metrics["tool_calls"],
        "brain_stop_reason": result.brain_stop_reason,
        "brain_text": result.brain_text,
    }
