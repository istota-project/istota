"""The name and value rules for a skill plugin's entry point, and where a skill runs.

A plugin registers ``<skill> = "<package>"`` in the ``istota.skills`` entry-point
group. The entry point name is the skill name and the value is one top-level
package (spec: external-skill-packages). The loader validates both with the
functions here, and so does :func:`resolve_unindexed_skill_module`, the
resolver ``skill_client`` uses with no index. That client must not import
``istota.skills``, which is why this is a stdlib-only leaf.

This module and ``skills._loader.resolve_skill_module`` are the only places
that spell a bundled skill's package; ``tests/test_skill_cli_facade.py`` pins
that, so a new dispatch site cannot run a plugin as a module that is not there.

``RESERVED_SKILL_NAMES`` is a literal copy of the scope tokens the room model
withholds by string, held to the constants it copies by
``tests/test_skill_plugins.py``. A plugin taking one of those names would be
withheld on every restricted task along with the scope, and ``room_notes``
would take the ``Channels/<token>`` bind with it.

:func:`check_tree` and ``python -m istota.lib.skill_module check-tree DIR`` are
what ``istota-stack plugins sync`` runs, in its one-shot, on the tree it has
just installed and before that tree becomes the one the venv reads.
"""

import importlib.machinery
import importlib.metadata
import os
import re
import sys
from collections.abc import Iterable
from pathlib import Path

#: The entry-point group a plugin registers in.
PLUGIN_GROUP = "istota.skills"

#: Where the image's venv finds the synced plugins: the one line of the
#: `istota-plugins.pth` the Dockerfile writes, held equal by tests/test_docker_plugins.py.
PLUGIN_SITE_DIR = "/app/plugins/installed/site"

#: A skill name: what `cron_loader` accepts in `command: istota-skill <name>`.
SKILL_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")

#: A single top-level package name. No dot: `find_spec("pkg.sub")` executes
#: `pkg/__init__.py`, and discovery must run no plugin code.
MODULE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: A plugin's secret service: what the loader accepts in a plugin manifest and
#: what `credentials.schema.plugin_service_keys` admits to the writable
#: registry. A prefix rather than a denylist, because the registry does not
#: list every service name the code stores under.
PLUGIN_SERVICE_RE = re.compile(r"plugin_[a-z0-9_]+")

#: `rooms.scopes.SYNTHETIC_SCOPES`, `sandbox.user_scope.ROOM_NOTES_WITHHELD`,
#: `user_profiles.CORRESPONDENT_REFUSED_SCOPES`, and `web`.
RESERVED_SKILL_NAMES = frozenset({
    "files", "memory", "recall", "memory_search", "room_notes", "web",
})


def plugin_name_problem(name: object) -> str | None:
    """``"bad_name"``, ``"reserved_name"`` or ``None`` for an entry point name."""
    if not isinstance(name, str) or SKILL_NAME_RE.fullmatch(name) is None:
        return "bad_name"
    if name in RESERVED_SKILL_NAMES:
        return "reserved_name"
    return None


def valid_plugin_value(value: object) -> bool:
    """Whether an entry point value names one top-level package, no attribute."""
    return isinstance(value, str) and MODULE_NAME_RE.fullmatch(value) is not None


def normalize_dist_name(name: str) -> str:
    """A distribution name as PEP 503 compares it: lower case, runs of ``-_.`` as ``-``."""
    return re.sub(r"[-_.]+", "-", name).lower()


def wheel_dist_name(filename: str) -> str | None:
    """The normalized distribution name a wheel's filename carries, or None.

    The name is everything before the first ``-`` (PEP 427 escapes a ``-``
    inside it to ``_``), so a filename with no ``-`` or ending other than
    ``.whl`` names nothing.
    """
    if not filename.endswith(".whl"):
        return None
    head, sep, _ = filename[: -len(".whl")].partition("-")
    if not head or not sep:
        return None
    return normalize_dist_name(head)


def bundled_skill_module(name: str) -> str:
    """The package a bundled skill lives in."""
    return f"istota.skills.{name}"


def resolve_unindexed_skill_module(name: str, bundled_dir: Path) -> str:
    """The package that runs skill ``name``, for a caller with no skill index.

    A bundled skill wins, as in the loader. Otherwise exactly one entry point
    of that name in the group, under the loader's name and value rules, names
    the package. Anything else is the bundled spelling, and the subprocess
    reports the missing module itself. Never raises.
    """
    default = bundled_skill_module(name)
    if not isinstance(name, str) or SKILL_NAME_RE.fullmatch(name) is None:
        return default
    try:
        if (bundled_dir / name / "skill.md").is_file():
            return default
    except OSError:
        return default
    if name in RESERVED_SKILL_NAMES:
        return default
    try:
        matches = [ep for ep in importlib.metadata.entry_points(group=PLUGIN_GROUP) if ep.name == name]
    except Exception:
        return default
    # Two distributions claiming one name: picking one would depend on sys.path order.
    if len(matches) != 1 or not valid_plugin_value(matches[0].value):
        return default
    return matches[0].value


def _venv_path(site_dir: Path, ignore: Iterable[str]) -> list[str]:
    """``sys.path`` without the tree being checked and the plugin trees in ``ignore``.

    The synced tree is on the path through the `.pth` while the next one is
    checked, and a re-sync installs what it already holds; it is not the venv.
    """
    skip = [str(path) for path in (site_dir, *ignore)]
    return [
        entry for entry in sys.path
        if not any(same_dir(entry or os.curdir, path) for path in skip)
    ]


def same_dir(left: str | os.PathLike, right: str | os.PathLike) -> bool:
    """Whether two names are one directory, asked of the file system.

    False when either is missing. Not a comparison of resolved spellings,
    which `tests/test_confined_file_access.py` refuses in new code.
    """
    try:
        return os.path.samefile(left, right)
    except (OSError, ValueError):
        return False


def _package_resolves(name: str, search_path: list[str]) -> bool:
    """Whether ``import name`` would find something other than a namespace portion.

    A namespace portion does not count: a regular package later on the path
    wins over one.
    """
    for finder in sys.meta_path:
        try:
            if finder is importlib.machinery.PathFinder:
                spec = importlib.machinery.PathFinder.find_spec(name, search_path)
            else:
                find_spec = getattr(finder, "find_spec", None)
                spec = find_spec(name, None) if find_spec is not None else None
        except Exception:
            continue
        if spec is not None and spec.origin is not None:
            return True
    return False


def check_tree(site_dir: Path, *, ignore: Iterable[str] = (PLUGIN_SITE_DIR,)) -> list[str]:
    """Why a freshly installed plugin tree must not replace the synced one.

    One line per reason, empty when the tree may go in. Refused: a
    distribution with no ``istota.skills`` entry point (the directory is for
    plugins, not for adding libraries to the daemon), one whose normalized name
    the venv already has, and one whose entry point names a package the venv
    already imports. The plugins directory is appended last on ``sys.path``,
    so in the last two the venv's copy wins and discovery cannot see the
    plugin at all. Also refused: any top-level entry that is neither a
    ``.dist-info`` directory nor a package an entry point names, since the tree
    is on every interpreter's path and a ``sitecustomize.py`` there would run
    in the web process, which otherwise imports no plugin code. Never raises.
    """
    site_dir = Path(site_dir)
    if not site_dir.is_dir():
        return [f"{site_dir}: not a directory"]
    search_path = _venv_path(site_dir, ignore)
    try:
        installed = {
            normalize_dist_name(name)
            for dist in importlib.metadata.distributions(path=search_path)
            if (name := dist.metadata.get("Name"))
        }
    except Exception as exc:
        return [f"{site_dir}: the venv's distributions could not be read ({type(exc).__name__})"]
    reasons = []
    packages = set()
    for dist_info in sorted(site_dir.glob("*.dist-info")):
        dist = importlib.metadata.PathDistribution(dist_info)
        try:
            name = dist.metadata.get("Name")
            entry_points = list(dist.entry_points.select(group=PLUGIN_GROUP))
        except Exception as exc:
            reasons.append(f"{dist_info.name}: unreadable metadata ({type(exc).__name__})")
            continue
        if not name:
            reasons.append(f"{dist_info.name}: no distribution name in its metadata")
            continue
        if not entry_points:
            reasons.append(f"{name}: declares no {PLUGIN_GROUP} entry point; plugins/src/ is for skill plugins alone")
            continue
        if normalize_dist_name(name) in installed:
            reasons.append(f"{name}: the image's venv already has a distribution of this name, which would hide this one")
        for ep in entry_points:
            if not valid_plugin_value(ep.value):
                continue
            packages.add(ep.value)
            if _package_resolves(ep.value, search_path):
                reasons.append(
                    f"{name}: entry point {ep.name} names {ep.value}, which the image's venv already imports"
                    " and which would hide this one"
                )
    try:
        entries = sorted(site_dir.iterdir())
    except OSError as exc:
        return [*reasons, f"{site_dir}: could not be listed ({type(exc).__name__})"]
    for entry in entries:
        is_dir = entry.is_dir() and not entry.is_symlink()
        if is_dir and (entry.name.endswith(".dist-info") or entry.name in packages):
            continue
        reasons.append(
            f"{entry.name}: not a plugin's package or its metadata; a plugin wheel puts one package"
            " at the top level and nothing else (no second module, sitecustomize.py, .pth file or script)"
        )
    return reasons


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "check-tree":
        print("usage: python -m istota.lib.skill_module check-tree DIR", file=sys.stderr)
        return 64
    reasons = check_tree(Path(argv[1]))
    for reason in reasons:
        print(reason, file=sys.stderr)
    return 1 if reasons else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
