"""Convert the role's application variables through the ensure CLI contract."""

from __future__ import annotations

import argparse
from copy import deepcopy

from istota.settings_plan.schema import SchemaError, parse_plan, parse_sidecar


LEGACY_VARIABLES = (
    "istota_config", "istota_users", "istota_user_resources",
    "istota_user_briefings", "istota_user_secrets",
)
ROLE_VARIABLES = (*LEGACY_VARIABLES, "istota_plan", "istota_plan_secrets")


class _EnsureParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse errors can quote a credential supplied as a flag value.
        raise ValueError("invalid ensure arguments; check the flag types and combinations")


def _ensure_args(kind, entry, path):
    from istota import cli

    if not isinstance(entry, dict):
        raise SchemaError(path, "ensure flags must be a mapping")
    parser = _EnsureParser(add_help=False, allow_abbrev=False)
    add_arguments = {
        "user": cli.add_user_ensure_arguments,
        "resource": cli.add_resource_arguments,
        "briefing": cli.add_briefing_arguments,
    }
    add_arguments[kind](parser)
    identity = "name" if kind == "user" else "user"
    argv = [f"--{identity}", "role-user"]
    for key, value in entry.items():
        if key == identity:
            raise SchemaError([*path, key], "use the user mapping key for the identity")
        if not isinstance(key, str) or f"--{key}" not in parser._option_string_actions:
            raise SchemaError(path, f"unsupported ensure flag; use `istota {kind} ensure --help`")
        if value is False or value is None:
            continue
        if key == "reset-whatsapp-identity" and value:
            raise SchemaError([*path, key], "one-shot action: run `user ensure --reset-whatsapp-identity` separately")
        if value is True:
            argv.append(f"--{key}")
        elif isinstance(value, list):
            for item in value:
                argv.extend([f"--{key}", str(item)])
        else:
            argv.extend([f"--{key}", str(value)])
    try:
        return parser.parse_args(argv)
    except ValueError:
        raise SchemaError(path, f"invalid ensure arguments; check `istota {kind} ensure --help`") from None


def _user(entry, path):
    from istota.cli import user_ensure_updates

    args = _ensure_args("user", entry, path)
    try:
        profile = user_ensure_updates(args)
    except ValueError:
        raise SchemaError(path, "invalid profile flag value; check user ensure arguments") from None
    result = {"profile": profile, "profile_mode": "managed" if args.managed else "seed" if args.seed else "update"}
    enrollment = {}
    for flag, field in (("whatsapp_number", "number"), ("whatsapp_bsuid", "bsuid")):
        value = getattr(args, flag)
        if value is not None:
            enrollment[field] = value
    if args.clear_whatsapp:
        enrollment["clear"] = True
    if enrollment:
        result["enrollment"] = enrollment
    return result


def _resource(entry, path):
    from istota.cli import resource_ensure_extras

    args = _ensure_args("resource", entry, path)
    if not args.type or not args.path:
        raise SchemaError(path, "resource ensure requires type and path")
    result = {"type": args.type, "path": args.path, "name": args.name or "", "permissions": args.permissions or "read"}
    try:
        extras = resource_ensure_extras(args)
    except ValueError:
        raise SchemaError(path, "invalid extras; use extras-json with a JSON object or extras key=value pairs") from None
    if extras is not None:
        result["extra"] = extras
    return result


def _briefing(entry, path):
    args = _ensure_args("briefing", entry, path)
    if not args.name or not args.cron:
        raise SchemaError(path, "briefing ensure requires name and cron")
    return {"name": args.name, "cron": args.cron, "title": "", "conversation_token": args.conversation_token or "",
            "output": args.output or "talk", "enabled": not args.disabled}


def convert_role_input(document: dict) -> tuple[dict, dict]:
    """Return a canonical plan and private sidecar, without state reads or writes.

    The envelope contains application variables only. Deployment variables
    stay with their role writers and are not submitted to apply.
    """
    if not isinstance(document, dict) or set(document) - set(ROLE_VARIABLES):
        raise SchemaError([], "role input must contain only application-settings variables")
    for name, value in document.items():
        if not isinstance(value, dict):
            raise SchemaError([name], "role variable must be a mapping")
    canonical = document.get("istota_plan", {})
    legacy = any(document.get(name) for name in LEGACY_VARIABLES)
    if canonical and legacy:
        raise SchemaError(["istota_plan"], "istota_plan cannot be combined with nonempty legacy application variables")
    if document.get("istota_plan_secrets") and not canonical:
        raise SchemaError(["istota_plan_secrets"], "istota_plan_secrets requires canonical istota_plan input")
    if canonical:
        plan = deepcopy(canonical)
        sidecar = deepcopy(document.get("istota_plan_secrets", {}))
    else:
        plan, sidecar = {}, {}
        if document.get("istota_config"):
            plan["config"] = deepcopy(document["istota_config"])
        users = {}
        for user, entry in document.get("istota_users", {}).items():
            users[user] = _user(entry, ["istota_users", user])
        for variable, section, convert in (
            ("istota_user_resources", "resources", _resource),
            ("istota_user_briefings", "briefings", _briefing),
        ):
            for user, entries in document.get(variable, {}).items():
                if not isinstance(entries, list):
                    raise SchemaError([variable, user], "must be a list of ensure mappings")
                if entries:
                    users.setdefault(user, {})[section] = [
                        convert(entry, [variable, user, index]) for index, entry in enumerate(entries)
                    ]
        for user, entries in document.get("istota_user_secrets", {}).items():
            if not isinstance(entries, list):
                raise SchemaError(["istota_user_secrets", user], "must be a list of secret mappings")
            services = {}
            for index, entry in enumerate(entries):
                path = ["istota_user_secrets", user, index]
                if not isinstance(entry, dict) or set(entry) - {"service", "key", "value"}:
                    raise SchemaError(path, "secret entry accepts service, key and value only")
                # The old role skips an absent/empty value. No slot is declared:
                # even explicit secret prune must not turn that skip into deletion.
                value = str(entry.get("value", ""))
                if not value:
                    continue
                service, key = entry.get("service"), entry.get("key")
                if not isinstance(service, str) or not isinstance(key, str):
                    raise SchemaError(path, "secret entry requires service and key")
                values = services.setdefault(service, {})
                if key in values:
                    raise SchemaError(path, "duplicate secret service/key")
                values[key] = value
            if services:
                users.setdefault(user, {})["secrets"] = [
                    {"service": service, "keys": sorted(values)} for service, values in sorted(services.items())
                ]
                sidecar.setdefault("users", {})[user] = services
        if users:
            plan["users"] = users
    # A role owns the config it submits, independently of per-user modes.
    if "config" in plan:
        meta = plan.setdefault("meta", {})
        if not isinstance(meta, dict):
            raise SchemaError(["meta"], "must be a mapping")
        meta.update(managed=True, source="ansible")
    parse_plan(plan)
    parse_sidecar(sidecar)
    return plan, sidecar
