"""Explicit portable fields for plans and snapshots, before schema validation."""

import re

from istota.lib.path_rebase import rebase_json
from istota.settings_plan.schema import SchemaError


# Path-typed configuration inputs. config_path is runtime-only.
CONFIG_PATH_FIELDS = frozenset({
    ("db_path",), ("workspace_path",), ("workspace_mount_path",),
    ("nextcloud_mount_path",), ("skills_dir",), ("bundled_skills_dir",),
    ("temp_dir",), ("module_data_dir",),
    # String-valued paths used by the migration's layout/rebase tables.
    ("logging", "file"), ("scheduler", "host_pressure_docker_socket"),
    ("scheduler", "db_backup_dir"), ("security", "sandbox_cache_dir"),
    ("brain", "native", "session_log", "dir"),
    ("whatsapp", "baileys", "session_dir"), ("whatsapp", "baileys", "pairing_relay_path"),
    ("devbox", "docker_cli"), ("developer", "repos_dir"),
    ("developer", "gh_bin_path"), ("developer", "glab_bin_path"),
    ("developer", "devbox_proxy_socket_dir"), ("developer", "container", "exec_socket_dir"),
})
_PLACEHOLDER = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}(?:/[^\x00]*)?\Z")


def _prefix(value, index):
    if not value or "\x00" in value:
        raise SchemaError(["patch", index], "path prefixes must be nonempty")
    if value.startswith("${"):
        valid = _PLACEHOLDER.fullmatch(value) and "${" not in value[2:]
    else:
        valid = value.startswith("/") and "${" not in value
    if not valid:
        raise SchemaError(["patch", index], "use an absolute prefix or a literal ${NAME} with an optional /suffix")
    return value.rstrip("/") or "/"


def parse_patches(patches: list[str]) -> list[tuple[str, str]]:
    """Validate SOURCE=DEST arguments without expanding or disclosing values."""
    result = []
    seen = set()
    for index, patch in enumerate(patches):
        source, separator, destination = patch.partition("=")
        if not separator:
            raise SchemaError(["patch", index], "expected SOURCE=DEST")
        source, destination = _prefix(source, index), _prefix(destination, index)
        if source in seen:
            raise SchemaError(["patch", index], "duplicate source prefix")
        seen.add(source)
        result.append((source, destination))
    return result


def _at(document, path):
    node = document
    for part in path:
        if isinstance(node, dict) and isinstance(part, str):
            node = node.get(part)
        elif isinstance(node, list) and type(part) is int and part < len(node):
            node = node[part]
        else:
            return None
    return node


def _selected(document, path, value):
    domain_path = path
    if path and path[0] == "config":
        domain_path = path[1:]
        if domain_path in CONFIG_PATH_FIELDS:
            return True
        if len(domain_path) == 3 and domain_path[:2] == ("security", "sandbox_ro_paths") and type(domain_path[2]) is int:
            return True
    match domain_path:
        case ("users", str(), "resources", int(), "path"):
            return _at(document, (*path[:-1], "type")) == "folder"
        case ("users", str(), "scheduled_jobs", int(), "prompt_file"):
            return True
        case ("users", str(), "heartbeat", "checks", int(), "path"):
            return _at(document, (*path[:-1], "type")) == "file-watch"
        case ("users", str(), "briefings", int(), "blocks", int(), "sources", int(), "config", "path"):
            pass
        case ("default_briefings", int(), "blocks", int(), "sources", int(), "config", "path") if path[0] == "config":
            pass
        case ("briefing_shared_blocks", int(), "sources", int(), "config", "path") if path[0] == "config":
            pass
        case ("shared_briefing_blocks", int(), "sources", int(), "config", "path"):
            pass
        case _:
            return False
    return _at(document, (*path[:-2], "kind")) in ("todos", "reminders", "notes")


def patch_document(document: dict, patches: list[str] | None = None) -> tuple[dict, list[str]]:
    """Return a patched copy and safe warnings; validate/deduplicate afterwards."""
    prefixes = parse_patches(patches or [])
    if not prefixes:
        return document, []
    matched = set()
    rewritten, _ = rebase_json(
        document, sorted(prefixes, key=lambda pair: len(pair[0]), reverse=True),
        select=lambda path, value: _selected(document, path, value), matched=matched,
    )
    warnings = [f"Path patch {index + 1} matched no selected paths."
                for index, (source, _) in enumerate(prefixes) if source not in matched]
    return rewritten, warnings
