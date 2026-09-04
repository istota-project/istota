"""Negative controls for the per-room brain selection tests. Hand-run:

    uv run python scripts/test-room-brain-negative-control.py

Reading a test tells you almost nothing about whether it can fail. Each entry
below breaks one property on purpose, runs the tests that claim to guard it, and
reports which node ids went red; a control that turns nothing red is a test that
cannot fail, and the script exits non-zero for it. Same discipline as
`test-image-negative-control.sh`, applied to a default-suite change rather than
to a built artifact.

The mutation is reverted from an **in-memory copy** taken immediately before the
edit, never with `git checkout --`. An earlier version used git and silently
destroyed every uncommitted edit in the files it touched, which is a worse
outcome than the bug any control here is hunting.
"""

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
FILES = [
    "tests/test_commands.py",
    "tests/test_commands_steer.py",
    "tests/test_executor_allowed_tools.py",
    "tests/test_security.py",
    "tests/test_web_chat.py",
    "tests/test_room_model_default.py",
    "tests/test_brain_room_default.py",
    "tests/test_prompt_golden.py",
]

CONTROLS: list[tuple[str, str, str, str]] = [
    (
        "webfetch granted to everyone",
        "src/istota/executor.py",
        '    tools = ["Read", "Write", "Edit", "Grep", "Glob", "Bash", "WebSearch"]\n'
        "    if is_admin:\n"
        '        tools.append("WebFetch")',
        '    tools = ["Read", "Write", "Edit", "Grep", "Glob", "Bash", "WebSearch", "WebFetch"]\n'
        "    if False:\n"
        '        tools.append("WebFetch")',
    ),
    (
        "prompt advertises WebFetch to everyone",
        "src/istota/executor.py",
        "    elif is_admin:\n        read_line = (",
        "    elif True:\n        read_line = (",
    ),
    (
        "namespace clear is unconditional",
        "src/istota/commands.py",
        "    if before is not None and after is not None and before == after:\n        return []",
        "    if False:\n        return []",
    ),
    (
        "outgoing namespace routed through the refusal",
        "src/istota/commands.py",
        "    if kind not in KNOWN_BRAIN_KINDS:\n        return None\n"
        "    return _model_namespace(dataclasses.replace(config.brain, kind=kind))",
        "    return _model_namespace(\n"
        "        resolve_brain_kind(source_type, config.brain, override=kind or None)\n"
        "    )",
    ),
    (
        "!room model resolves via the deployment brain",
        "src/istota/commands.py",
        "        room_brain = make_brain(brain_for_room(config, conn, token, ctx.surface))",
        "        room_brain = make_brain(config.brain)",
    ),
    (
        "!models lists the deployment brain",
        "src/istota/commands.py",
        "    for alias, model, effort in make_brain(_ctx_brain(ctx)).list_aliases():",
        "    for alias, model, effort in make_brain(ctx.config.brain).list_aliases():",
    ),
    (
        "!help offers the deployment aliases",
        "src/istota/commands.py",
        "    aliases = [alias for alias, _m, _e in make_brain(_ctx_brain(ctx)).list_aliases()]",
        "    aliases = [alias for alias, _m, _e in make_brain(ctx.config.brain).list_aliases()]",
    ),
    (
        "steer gate ignores the task's brain",
        "src/istota/commands.py",
        '        source_type, config.brain, override=row["brain"],',
        "        source_type, config.brain, override=None,",
    ),
    (
        "failover line tracks the column, not the admission",
        "src/istota/commands.py",
        '        lines += ["", _describe_failover(config, ctx.surface, pinned if admitted else "")]',
        '        lines += ["", _describe_failover(config, ctx.surface, pinned)]',
    ),
    (
        "!room show drops the brain line",
        "src/istota/commands.py",
        '        return _describe_room_default(room.model, room.effort) + "\\n" + \\\n'
        "            _describe_room_brain(config, room, ctx.surface)",
        "        return _describe_room_default(room.model, room.effort)",
    ),
    (
        "default gated behind the allowlist again",
        "src/istota/commands.py",
        '    if wanted == "default":\n        if not pinned:',
        '    if wanted == "default" and room_selectable_kinds(config.brain):\n        if not pinned:',
    ),
    (
        "admin gate removed",
        "src/istota/commands.py",
        "    if not config.is_admin(ctx.user_id):\n        return \"Only an admin",
        "    if False:\n        return \"Only an admin",
    ),
    (
        "brain_for_room resolve back outside the guard",
        "src/istota/commands.py",
        "        return resolve_brain_kind(source_type, config.brain, override=override)\n"
        "    except Exception:",
        "        pass\n"
        "    except Exception:",
    ),
    (
        "PATCH validates against the deployment brain",
        "src/istota/web_app.py",
        "            if model not in _known_room_models(room_brain):",
        "            if model not in _known_room_models(_config.brain):",
    ),
]


def run() -> tuple[int, list[str]]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *FILES,
         "-q", "--no-header", "-p", "no:randomly", "-o", "addopts=", "-rf",
         "--color=no", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True,
    )
    out = proc.stdout + proc.stderr
    failed = sorted({
        m.group(1) for m in re.finditer(r"^FAILED (\S+)", out, re.MULTILINE)
    })
    return proc.returncode, failed


def main() -> int:
    code, failed = run()
    if code != 0:
        print("BASELINE IS RED — fix that first:")
        for f in failed:
            print("   ", f)
        return 1
    print("baseline: green\n")

    bad = 0
    for name, rel, old, new in CONTROLS:
        path = ROOT / rel
        original = path.read_text()
        if old not in original:
            print(f"!! {name}: anchor not found in {rel}")
            bad += 1
            continue
        try:
            path.write_text(original.replace(old, new, 1))
            _, failed = run()
        finally:
            path.write_text(original)
        if not failed:
            print(f"!! {name}: NOTHING WENT RED")
            bad += 1
        else:
            print(f"{name}: {len(failed)} red")
            for f in failed:
                print("   ", f)
        print()
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
