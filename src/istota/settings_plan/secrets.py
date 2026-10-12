"""Private inputs for settings apply; credential values never enter reports."""

from __future__ import annotations

import os
import stat
from copy import deepcopy
from pathlib import Path

from istota import apply
from istota.config_editor import schema as config_schema
from istota.config_editor import secrets
from istota.settings_plan.schema import Plan, SchemaError, Sidecar, parse_sidecar


def read_sidecar(path: Path | str) -> Sidecar:
    """Read once from a private regular file, checking the opened descriptor."""
    if str(path) == "-":
        raise SchemaError(["secrets_file"], "--secrets-file requires a private file, not stdin")
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise SchemaError(["secrets_file"], "--secrets-file must be a private regular file (mode 0600)")
            raw = source.read()
    except OSError:
        raise SchemaError(["secrets_file"], "cannot read --secrets-file as a private regular file without following links") from None
    try:
        document = apply.parse_document(raw, is_json=path.suffix.lower() == ".json", where="in --secrets-file")
        return parse_sidecar(document)
    except SchemaError:
        raise
    except apply.PlanError as exc:
        raise SchemaError(["secrets_file"], str(exc)) from None


def select_sidecar(plan: Plan, sidecar: Sidecar, users: list[str] | None) -> Sidecar:
    """Require declared targets, and discard values outside the selected scope."""
    selected = {}
    for user, services in sidecar.users.items():
        declared = (plan.users or {}).get(user)
        if declared is None:
            raise SchemaError(["users", user, "secrets"], "sidecar user is not declared in the plan")
        if users and user not in users:
            continue
        slots = {entry["service"]: entry["keys"] for entry in declared.sections.get("secrets", [])}
        for service, values in services.items():
            if service not in slots:
                raise SchemaError(["users", user, "secrets", service], "sidecar service is not declared in the plan")
            for key in values:
                if key not in slots[service]:
                    raise SchemaError(["users", user, "secrets", service, key], "sidecar key is not declared in the plan")
        selected[user] = services
    config = [] if users else sidecar.config
    if config and plan.config is None:
        raise SchemaError(["config"], "sidecar config targets require a submitted config section")
    return Sidecar(selected, config)


def resolve_config(document: dict, config_path: Path, sidecar: Sidecar) -> dict:
    """Resolve through the editor's sentinel contract, retaining segment identity."""
    candidate = deepcopy(document)
    replacements = {}
    for entry in sidecar.config:
        path = entry["path"]
        node = candidate
        for segment in path[:-1]:
            if isinstance(node, dict) and isinstance(segment, str) and segment in node:
                node = node[segment]
            elif isinstance(node, list) and type(segment) is int and 0 <= segment < len(node):
                node = node[segment]
            else:
                raise SchemaError(["config", *path], "sidecar target is not a submitted credential leaf")
        last = path[-1]
        present = (isinstance(node, dict) and isinstance(last, str) and last in node) or (
            isinstance(node, list) and type(last) is int and 0 <= last < len(node))
        text_path = tuple(str(segment) for segment in path)
        if not present or isinstance(node[last], (dict, list)) or not secrets.is_secret_path(text_path):
            raise SchemaError(["config", *path], "sidecar target is not a submitted credential leaf")
        if text_path in config_schema.file_credentials() or config_schema.env_override_for(text_path):
            raise SchemaError(["config", *path], "credential is deployment-routed; use its existing credential writer")
        if entry["value"] == secrets.SENTINEL:
            raise SchemaError(["config", *path], "sidecar replacement must supply a credential value")
        replacements[secrets.label(text_path)] = entry["value"]
        node[last] = secrets.SENTINEL
    try:
        current = apply._parsed_or_empty(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except UnicodeError:
        raise SchemaError(["config"], "current config is not valid UTF-8") from None
    try:
        return secrets.restore_sentinels(candidate, current, replacements)
    except secrets.SentinelError as exc:
        path = ["config"]
        node = candidate
        for segment in exc.path:
            key = int(segment) if isinstance(node, list) else segment
            path.append(key)
            node = node[key]
        raise SchemaError(path, f"{exc}; supply an inline replacement through --secrets-file") from None
