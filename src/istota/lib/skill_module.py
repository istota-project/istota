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
"""

import importlib.metadata
import re
from pathlib import Path

#: The entry-point group a plugin registers in.
PLUGIN_GROUP = "istota.skills"

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
