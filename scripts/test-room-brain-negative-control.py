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
    "tests/test_web_chat_commands.py",
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
    # ---- Stage 4, the web surface ----
    (
        "PATCH admin gate removed",
        "src/istota/web_app.py",
        '        if not _config.is_admin(user["username"]):\n'
        "            return JSONResponse(",
        "        if False:\n"
        "            return JSONResponse(",
    ),
    (
        "PATCH allowlist check removed",
        "src/istota/web_app.py",
        "            if brain not in room_selectable_kinds(_config.brain):",
        "            if False:",
    ),
    (
        "PATCH clears the pin before applying the model, not after",
        "src/istota/web_app.py",
        "            if model is not _UNSET or effort is not _UNSET:",
        "            if brain is not _UNSET:\n"
        "                _reg = db.get_room(conn, updated.token)\n"
        "                if _reg is not None:\n"
        "                    from .commands import _clear_pin_across_namespaces\n"
        "                    model_cleared = bool(_clear_pin_across_namespaces(\n"
        "                        _config, conn, updated.token, _reg,\n"
        '                        source_type="web",\n'
        '                        outgoing=(_reg.brain or "").strip(),\n'
        "                        incoming=brain,\n"
        "                    ))\n"
        "                db.set_room_brain(conn, updated.token, brain)\n"
        "                brain = _UNSET\n"
        "            if model is not _UNSET or effort is not _UNSET:",
    ),
    (
        "PATCH response drops the brain",
        "src/istota/web_app.py",
        '        d["brain"] = reg.brain if reg else None',
        "        pass",
    ),
    (
        "the room listing drops the brain",
        "src/istota/web_app.py",
        '            d["brain"] = r.brain\n',
        "",
    ),
    (
        "the stream snapshot drops the brain",
        "src/istota/web_app.py",
        '                "brain": r.brain,\n',
        "",
    ),
    (
        "the promote response drops the brain",
        "src/istota/web_app.py",
        '        d["brain"] = reg.brain\n',
        "",
    ),
    (
        "selectable_brains ignores the admin gate",
        "src/istota/web_app.py",
        "        if not _config.is_admin(username):\n            return empty",
        "        if False:\n            return empty",
    ),
    (
        "the inherited brain ignores the lane rule",
        "src/istota/web_app.py",
        '        inherited_kind = resolve_brain_kind("web", _config.brain).kind',
        "        inherited_kind = _config.brain.kind",
    ),
    (
        "brain_namespaces narrowed to the offered kinds",
        "src/istota/web_app.py",
        "            for kind in sorted(KNOWN_BRAIN_KINDS)",
        "            for kind in sorted(room_selectable_kinds(_config.brain))",
    ),
    (
        "the crossing clear reports nothing about the effort",
        "src/istota/web_app.py",
        '                        cleared = ["model"] + (["effort"] if reg.effort else [])',
        '                        cleared = ["model"]',
    ),
    (
        # Both filters at once, deliberately. Dropping only the allowlist one
        # turns nothing red — the per-kind `except` below catches an unbuildable
        # name anyway — so a control naming one site alone reports a test that
        # cannot fail when what it has actually found is two mechanisms
        # covering one case.
        "selectable_brains offers a kind that cannot be built",
        "src/istota/web_app.py",
        "            for kind in sorted(room_selectable_kinds(_config.brain))",
        "            for kind in sorted(_config.brain.room_selectable)",
    ),
    (
        "selectable_brains has no per-kind guard",
        "src/istota/web_app.py",
        "            except Exception:  # noqa: BLE001 — one bad kind is not the whole list",
        "            except ZeroDivisionError:",
    ),
    (
        "/chat/commands ignores room_id",
        "src/istota/web_app.py",
        "    if room_id is not None:\n"
        '        room = await asyncio.to_thread(_chat_owned_room, user["username"], room_id)',
        "    if False:\n"
        '        room = await asyncio.to_thread(_chat_owned_room, user["username"], room_id)',
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
