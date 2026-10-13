"""Structural validation for settings plans, without store reads.

Mappings retain supplied keys, including empty values. Domain defaults and
required creation fields are resolved after merging with existing state by
the section adapters; filling them here would turn omissions into writes.
"""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass, field

from istota.apply import Meta, PlanError, _check_meta
from istota.briefings.models import ALLOWED_SHARED_SOURCE_KINDS, RENDER_MODES, SOURCE_KINDS
from istota.lib.user_ids import user_id_index
from istota.sandbox.user_scope import is_scopable_user_id


PROFILE_FIELDS = {
    "display_name": str, "timezone": str, "log_channel": str, "alerts_channel": str,
    "email_addresses": list, "trusted_email_senders": list, "quiet_email_senders": list,
    "disabled_skills": list, "disabled_modules": list, "routing": dict,
    "default_destination": str, "default_room": str, "outbound_approval": str,
    "external_turn_display": str, "relay_delivery": str, "sms_phone_number": str,
    "max_foreground_workers": int, "max_background_workers": int,
    "default_briefings": bool, "briefing_email_html": bool, "timezone_follow_location": bool,
}
JOB_FIELDS = {
    "name": str, "cron": str, "prompt": str, "command": str, "prompt_file": str,
    "target": str, "room": str, "enabled": bool, "silent_unless_action": bool,
    "skip_log_channel": bool, "once": bool, "model": str, "effort": str, "brain": str,
    "publish_shared_kv": str, "publish_shared_kv_trusted": bool,
}
RESOURCE_FIELDS = {"type": str, "path": str, "name": str, "permissions": str, "extra": dict}
BRIEFING_FIELDS = {
    "name": str, "cron": str, "title": str, "conversation_token": str,
    "output": str, "enabled": bool, "blocks": list,
}
BLOCK_FIELDS = {"title": str, "directive": str, "render_mode": str, "options": dict, "sources": list}
SOURCE_FIELDS = {"kind": str, "enabled": bool, "config": dict}
SHARED_FIELDS = {
    "name": str, "cron": str, "title": str, "directive": str, "render_mode": str,
    "enabled": bool, "trusted": bool, "sources": list,
}
HEARTBEAT_SETTINGS_FIELDS = {
    "conversation_token": str, "quiet_hours": list, "default_cooldown_minutes": int,
}
CHECK_FIELDS = {
    "name": str, "type": str, "channel": str, "cooldown_minutes": int,
    "interval_minutes": int, "reset": list,
}
CHECK_TYPE_FIELDS = {
    "file-watch": {"path": str, "max_age_hours": (int, float)},
    "shell-command": {"command": str, "condition": str, "message": str, "timeout": (int, float)},
    "url-health": {"url": str, "expected_status": int, "timeout": (int, float)},
    "calendar-conflicts": {"lookahead_hours": (int, float)},
    "task-deadline": {"warn_hours_before": (int, float)},
    "self-check": {"execution_test": bool},
}
USER_FIELDS = {
    "profile_mode": str, "profile": dict, "enrollment": dict, "resources": list,
    "briefings": list, "scheduled_jobs": list, "heartbeat": dict, "secrets": list,
}


class SchemaError(PlanError):
    """A safe reason and segment path, never a submitted value."""

    def __init__(self, path: list[str | int], message: str):
        self.problem = {"kind": "discarded", "path": list(path), "message": message}
        super().__init__(message)


@dataclass
class UserPlan:
    user_id: str
    sections: dict = field(repr=False)

    @property
    def profile_mode(self) -> str:
        return self.sections.get("profile_mode", "update")


@dataclass
class Plan:
    meta: Meta
    metadata: dict = field(repr=False)
    config: dict | None = field(default=None, repr=False)
    users: dict[str, UserPlan] | None = field(default=None, repr=False)
    shared_briefing_blocks: list[dict] | None = field(default=None, repr=False)


@dataclass
class Sidecar:
    users: dict = field(default_factory=dict, repr=False)
    config: list[dict] = field(default_factory=list, repr=False)


def _fail(path, message):
    raise SchemaError(path, message)


def _type(value, expected, path):
    types = expected if isinstance(expected, tuple) else (expected,)
    if type(value) not in types:
        _fail(path, "must be " + " or ".join(item.__name__ for item in types))


def _fields(value, allowed, path):
    _type(value, dict, path)
    for name, item in value.items():
        if name not in allowed:
            message = "unknown field"
            if name == "components":
                message = "legacy components are not supported; use blocks and sources"
            _fail([*path, name], message)
        _type(item, allowed[name], [*path, name])


def _values(value, path, *, json_only=True):
    if value is None:
        _fail(path, "null is not supported; omit the field to leave it unmanaged")
    if isinstance(value, float) and not math.isfinite(value):
        _fail(path, "non-finite numbers are not supported")
    if json_only and type(value) not in (dict, list, str, int, float, bool):
        _fail(path, "must be a JSON value; quote dates and times as strings")
    if isinstance(value, dict):
        for name, item in value.items():
            if not isinstance(name, str):
                _fail(path, "object keys must be strings")
            _values(item, [*path, name], json_only=json_only)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _values(item, [*path, index], json_only=json_only)


def _strings(value, path, *, unique=False):
    _type(value, list, path)
    seen = set()
    for index, item in enumerate(value):
        _type(item, str, [*path, index])
        if unique and item in seen:
            _fail([*path, index], "duplicate entry")
        seen.add(item)


def _choice(value, choices, path):
    if value not in choices:
        _fail(path, "must be one of: " + ", ".join(sorted(choices)))


def _identity(item, name, path):
    value = item.get(name)
    if not isinstance(value, str) or not value.strip():
        _fail([*path, name], "a nonempty identity is required")
    return value


def _entries(value, fields, path, identity=("name",)):
    _type(value, list, path)
    seen = set()
    for index, item in enumerate(value):
        item_path = [*path, index]
        _fields(item, fields, item_path)
        if identity:
            key = tuple(_identity(item, name, item_path) for name in identity)
            if key in seen:
                _fail(item_path, "duplicate identity")
            seen.add(key)
        yield item, item_path


def _user_ids(users, path):
    _type(users, dict, path)
    for user_id in users:
        if not is_scopable_user_id(user_id):
            _fail(path, "user ID must be a nonempty, contained path component")
    try:
        user_id_index(users)
    except ValueError:
        _fail(path, "user IDs must not differ only in case")


def _sources(sources, path, *, shared=False):
    allowed = {"kind": str, "config": dict} if shared else SOURCE_FIELDS
    for source, source_path in _entries(sources, allowed, path, identity=()):
        if "kind" in source:
            kinds = ALLOWED_SHARED_SOURCE_KINDS if shared else SOURCE_KINDS
            _choice(source["kind"], kinds, [*source_path, "kind"])


def _blocks(blocks, path):
    for block, block_path in _entries(blocks, BLOCK_FIELDS, path, identity=()):
        if "render_mode" in block:
            _choice(block["render_mode"], RENDER_MODES, [*block_path, "render_mode"])
        if "sources" in block:
            _sources(block["sources"], [*block_path, "sources"])


def _enrollment(value, path):
    from istota.user_profiles import normalize_whatsapp_phone_number

    _fields(value, {"number": str, "bsuid": str, "clear": bool, "reset_identity": bool}, path)
    if value.get("reset_identity"):
        _fail([*path, "reset_identity"], "one-shot reset is not desired state; use user ensure --reset-whatsapp-identity")
    if value.get("clear") and ("number" in value or "bsuid" in value):
        _fail(path, "clear cannot be combined with number or bsuid")
    if "number" in value:
        try:
            normalize_whatsapp_phone_number(value["number"], allow_empty=True)
        except ValueError:
            _fail([*path, "number"], "number must be exact E.164")


def _heartbeat(value, path):
    _fields(value, {"settings": dict, "checks": list}, path)
    if "settings" in value:
        settings = value["settings"]
        _fields(settings, HEARTBEAT_SETTINGS_FIELDS, [*path, "settings"])
        if "quiet_hours" in settings:
            _strings(settings["quiet_hours"], [*path, "settings", "quiet_hours"])
    all_fields = dict(CHECK_FIELDS)
    for type_fields in CHECK_TYPE_FIELDS.values():
        all_fields.update(type_fields)
    for check, check_path in _entries(value.get("checks", []), all_fields, [*path, "checks"]):
        if "type" in check:
            _choice(check["type"], CHECK_TYPE_FIELDS, [*check_path, "type"])
            _fields(check, CHECK_FIELDS | CHECK_TYPE_FIELDS[check["type"]], check_path)
        if "reset" in check:
            _strings(check["reset"], [*check_path, "reset"], unique=True)
            for index, name in enumerate(check["reset"]):
                _choice(name, ("cooldown_minutes", "interval_minutes", "max_age_hours"), [*check_path, "reset", index])
                if name in check:
                    _fail([*check_path, "reset"], "cannot reset and supply the same override")
                if name == "max_age_hours" and "type" in check and check["type"] != "file-watch":
                    _fail([*check_path, "reset"], "age limits belong to file-watch checks")


def validate_user_plan(user_id: str, document: dict) -> UserPlan:
    """Check one user's partial definitions without resolving stored values."""
    _user_ids({user_id: document}, ["users"])
    path = ["users", user_id]
    _values(document, path)
    _fields(document, USER_FIELDS, path)
    if "profile_mode" in document:
        _choice(document["profile_mode"], ("update", "managed", "seed"), [*path, "profile_mode"])
    if "profile" in document:
        profile = document["profile"]
        profile_path = [*path, "profile"]
        _fields(profile, PROFILE_FIELDS, profile_path)
        for name, value in profile.items():
            if PROFILE_FIELDS[name] is list:
                _strings(value, [*profile_path, name])
            elif name == "routing":
                for purpose, destination in value.items():
                    _type(destination, str, [*profile_path, name, purpose])
    if "enrollment" in document:
        _enrollment(document["enrollment"], [*path, "enrollment"])
    for resource, resource_path in _entries(document.get("resources", []), RESOURCE_FIELDS, [*path, "resources"], ("type", "path")):
        _choice(resource["type"], ("folder",), [*resource_path, "type"])
        if "permissions" in resource:
            _choice(resource["permissions"], ("read", "readwrite"), [*resource_path, "permissions"])
    for briefing, briefing_path in _entries(document.get("briefings", []), BRIEFING_FIELDS, [*path, "briefings"]):
        if "blocks" in briefing:
            _blocks(briefing["blocks"], [*briefing_path, "blocks"])
    for job, job_path in _entries(document.get("scheduled_jobs", []), JOB_FIELDS, [*path, "scheduled_jobs"]):
        if job["name"].startswith("_module."):
            _fail([*job_path, "name"], "reserved module jobs are outside the plan")
    if "heartbeat" in document:
        _heartbeat(document["heartbeat"], [*path, "heartbeat"])
    for secret, secret_path in _entries(document.get("secrets", []), {"service": str, "keys": list}, [*path, "secrets"], ("service",)):
        if "keys" not in secret:
            _fail([*secret_path, "keys"], "an explicit key list is required")
        _strings(secret["keys"], [*secret_path, "keys"], unique=True)
        for index, key in enumerate(secret["keys"]):
            if not key:
                _fail([*secret_path, "keys", index], "key must be nonempty")
    return UserPlan(user_id, deepcopy(document))


def parse_plan(document: dict) -> Plan:
    """Validate the whole input, before selecting the users to reconcile."""
    _values(document, [], json_only=False)
    _fields(document, {"meta": dict, "config": dict, "users": dict, "shared_briefing_blocks": list}, [])
    metadata = document.get("meta", {})
    meta = _check_meta(metadata)
    users = None
    if "users" in document:
        _user_ids(document["users"], ["users"])
        users = {user_id: validate_user_plan(user_id, data) for user_id, data in document["users"].items()}
    if "shared_briefing_blocks" in document:
        _values(document["shared_briefing_blocks"], ["shared_briefing_blocks"])
        for block, path in _entries(document["shared_briefing_blocks"], SHARED_FIELDS, ["shared_briefing_blocks"]):
            if "render_mode" in block:
                _choice(block["render_mode"], RENDER_MODES, [*path, "render_mode"])
            if "sources" in block:
                _sources(block["sources"], [*path, "sources"], shared=True)
    return Plan(meta, deepcopy(metadata), deepcopy(document.get("config")), users,
                deepcopy(document.get("shared_briefing_blocks")))


def select_users(plan: Plan, user_ids: list[str] | None) -> Plan:
    """A selected-user invocation excludes every instance-wide section."""
    if not user_ids:
        return plan
    users = plan.users or {}
    for user_id in user_ids:
        if user_id not in users:
            _fail(["users"], "selected user is not declared in the plan")
    return Plan(plan.meta, deepcopy(plan.metadata), users={user_id: users[user_id] for user_id in user_ids})


def parse_sidecar(document: dict) -> Sidecar:
    """Check private value structure; selected targets are checked by adapters."""
    _values(document, [])
    _fields(document, {"version": int, "users": dict, "config": list}, [])
    if document.get("version", 1) != 1:
        _fail(["version"], "unsupported sidecar version; expected 1")
    users = document.get("users", {})
    _user_ids(users, ["users"])
    for user_id, services in users.items():
        path = ["users", user_id]
        _type(services, dict, path)
        for service, keys in services.items():
            if not service:
                _fail(path, "service must be nonempty")
            _type(keys, dict, [*path, service])
            for key, value in keys.items():
                if not key:
                    _fail([*path, service], "key must be nonempty")
                _type(value, str, [*path, service, key])
                if not value:
                    _fail([*path, service, key], "credential value must be nonempty")
    seen = set()
    for entry, path in _entries(document.get("config", []), {"path": list, "value": str}, ["config"], identity=()):
        target = entry.get("path")
        if not target:
            _fail([*path, "path"], "a nonempty segment path is required")
        for index, segment in enumerate(target):
            if type(segment) not in (str, int) or (isinstance(segment, int) and segment < 0):
                _fail([*path, "path", index], "path segments must be strings or nonnegative integer indices")
        if tuple(target) in seen:
            _fail([*path, "path"], "duplicate config credential target")
        seen.add(tuple(target))
        if not entry.get("value"):
            _fail([*path, "value"], "credential value must be nonempty")
    return Sidecar(deepcopy(users), deepcopy(document.get("config", [])))
