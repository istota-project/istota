"""Validating a config document in a child process.

`load_config` sets process globals (the alias and model tables) and logs
through a shared logger, so the web process never runs it on a document that
may not be applied. It runs `istota apply --check -f - --json` instead, the
same code the host's dry run uses in the same image, with the plan on stdin.

A timeout, a failed spawn or a reply that is not the expected JSON object is
a blocking `error`. Neither the plan nor the reply is ever logged: the plan
holds credentials.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tomllib

from istota.apply import CheckResult
from istota.lib import toml_write

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30.0

#: The error a check that did not finish, or answered nonsense, reports.
INCOMPLETE = "the configuration check did not complete"

#: Problem kinds that stop a change. A loader exception (`error`) does too.
BLOCKING_KINDS = frozenset({"discarded", "error"})


def problems_of(result: CheckResult) -> list[dict]:
    """`result` as one list, each problem marked `blocking`; the error, if any,
    is a blocking problem at the empty path."""
    found = [
        {**problem, "blocking": problem.get("kind") in BLOCKING_KINDS}
        for problem in result.problems
    ]
    if result.error is not None:
        found.append({"kind": "error", "path": [], "message": result.error, "blocking": True})
    return found


def default_command() -> list[str]:
    """`istota apply --check -f - --json`, run by this interpreter.

    `-m istota.cli` rather than the console script, so the check runs the
    installed code whatever `PATH` holds.
    """
    return [sys.executable, "-m", "istota.cli", "apply", "--check", "-f", "-", "--json"]


def _reply(stdout: bytes) -> CheckResult | None:
    try:
        reply = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(reply, dict):
        return None
    problems, error = reply.get("problems"), reply.get("error")
    if not isinstance(problems, list) or not (error is None or isinstance(error, str)):
        return None
    for problem in problems:
        if not (
            isinstance(problem, dict)
            and isinstance(problem.get("kind"), str)
            and isinstance(problem.get("message"), str)
            and isinstance(problem.get("path"), list)
            and all(isinstance(segment, str) for segment in problem["path"])
        ):
            return None
    return CheckResult(problems=problems, error=error)


async def run_check(
    document: dict, *, command: list[str] | None = None, timeout: float = TIMEOUT_SECONDS,
) -> CheckResult:
    """What loading `document` as `config.toml` reports, from a child process."""
    try:
        plan = toml_write.dumps({"config": document}).encode("utf-8")
    except toml_write.TomlWriteError as exc:
        return CheckResult(error=f"[config]: {exc}")
    argv = command or default_command()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.warning("config check: could not start the check: %s", exc.strerror or type(exc).__name__)
        return CheckResult(error=INCOMPLETE)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(plan), timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        logger.warning("config check: no answer after %ss", timeout)
        return CheckResult(error=INCOMPLETE)
    result = _reply(stdout)
    if result is None:
        logger.warning("config check: exit status %s, and the reply was not a check result", process.returncode)
        return CheckResult(error=INCOMPLETE)
    return result


_cached: tuple[str, CheckResult] | None = None


def forget_cached() -> None:
    global _cached
    _cached = None


async def check_current(text: str, digest: str) -> CheckResult:
    """The check of the current `config.toml`, run once per digest.

    A file that does not parse is reported without a child process. A check
    that did not complete is not kept, so the next view asks again.
    """
    global _cached
    if _cached is not None and _cached[0] == digest:
        return _cached[1]
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        result = CheckResult(error=f"config.toml does not parse: {exc}")
    else:
        result = await run_check(document)
    if result.error != INCOMPLETE:
        _cached = (digest, result)
    return result
