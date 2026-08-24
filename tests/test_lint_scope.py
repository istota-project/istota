"""Every Python program the devbox image ships is inside the lint scope.

Three of them carry no `.py` suffix, because they sit on the container's `PATH`
and are invoked by name. Ruff discovers a file by extension, so before
`[tool.ruff] extend-include` named them one by one, `ruff check docker/devbox`
walked past `istota-exec-serve` — the process that makes every containment
decision inside the container — and reported the directory clean.

That list is hand-maintained and nothing held it, which is the failure mode the
comment beside it describes: a fourth program added later is silently unlinted
and the command still says "All checks passed". So the discriminator here is the
shebang rather than the list — the tree is walked, and anything with a Python
interpreter on its first line and no `.py` suffix has to be named.

Deliberately scoped to `docker/devbox/`. `scripts/qtest` is a Python program in
the same shape and is outside every documented lint invocation; widening the
scope to cover it is a separate decision with its own argument, not something to
smuggle in through a guard.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEVBOX = REPO / "docker" / "devbox"


def _extend_include() -> list[str]:
    with (REPO / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)
    return data.get("tool", {}).get("ruff", {}).get("extend-include", [])


def _has_python_shebang(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            first = fh.readline(256)
    except OSError:
        return False
    if not first.startswith(b"#!"):
        return False
    return b"python" in first


def _extensionless_python_programs() -> list[Path]:
    found = []
    for path in sorted(DEVBOX.rglob("*")):
        if not path.is_file() or path.suffix:
            continue
        if "__pycache__" in path.parts:
            continue
        if _has_python_shebang(path):
            found.append(path)
    return found


def test_every_extensionless_python_program_is_named_in_extend_include():
    named = set(_extend_include())
    missing = [
        str(p.relative_to(REPO))
        for p in _extensionless_python_programs()
        if str(p.relative_to(REPO)) not in named
    ]
    assert not missing, (
        f"{missing} are Python programs with no .py suffix, so ruff will not "
        f"discover them and `ruff check … docker/devbox` reports the directory "
        f"clean without reading them. Add each to [tool.ruff] extend-include."
    )


def test_the_walk_finds_the_programs_it_is_meant_to_guard():
    """A guard over an empty set passes on any tree at all.

    `rglob` plus a shebang read is enough machinery to go quietly wrong — a
    renamed directory, a `suffix` check that starts matching, an unreadable
    file — and every one of those failures leaves the test above green.
    """
    found = {p.name for p in _extensionless_python_programs()}
    assert {"istota-exec-serve", "git-credential-istota"} <= found, found


def test_nothing_named_there_is_a_shell_script():
    """`istota-exec-run` sits beside them and is `/bin/sh`.

    Which is why the list is written out one path at a time rather than as a
    `scripts/*` glob: a glob would hand ruff a shell script to parse as Python,
    and the lint command would fail on a file that is not Python at all.
    """
    for entry in _extend_include():
        path = REPO / entry
        if not path.exists() or path.suffix:
            continue
        assert _has_python_shebang(path), (
            f"{entry} is in extend-include but does not start with a Python "
            f"shebang; ruff would try to parse it as Python"
        )
