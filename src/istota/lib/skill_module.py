"""The name and value rules for a skill plugin's entry point.

A plugin registers ``<skill> = "<package>"`` in the ``istota.skills`` entry-point
group. The entry point name is the skill name and the value is one top-level
package (spec: external-skill-packages). The loader validates both with the
functions here; the no-index resolver in ``skill_client`` will too, and it must
not import ``istota.skills``, which is why this is a stdlib-only leaf.

``RESERVED_SKILL_NAMES`` is a literal copy of the scope tokens the room model
withholds by string, held to the constants it copies by
``tests/test_skill_plugins.py``. A plugin taking one of those names would be
withheld on every restricted task along with the scope, and ``room_notes``
would take the ``Channels/<token>`` bind with it.
"""

import re

#: A skill name: what `cron_loader` accepts in `command: istota-skill <name>`.
SKILL_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")

#: A single top-level package name. No dot: `find_spec("pkg.sub")` executes
#: `pkg/__init__.py`, and discovery must run no plugin code.
MODULE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

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
