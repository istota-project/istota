"""The old bare-metal role's variables, as the management role's.

    istota migrate role-vars --old-vars FILE [--old-vars FILE ...]
                             [--config config.toml] [--host-env host.env]
                             [--stack-env .env] [--rclone-conf rclone.conf]
                             --output FILE

The old role (`deploy/ansible/`) took every setting as a flat `istota_*`
variable and rendered config.toml from them. The management role (`ansible/`)
takes its deployment settings as variables (`host.env`'s, one file per stack
secret as `istota_secrets`, `rclone.conf`, the admins) and the application
settings as one `istota apply` plan, `istota_plan`. This writes the deployment
variables from the old role's, and the old per-user credentials as the plan's
sidecar, `istota_plan_secrets`. The plan itself is not converted: the import
already put the users, their folders and briefings and the config on the VM,
so `istota snapshot --format json` there is the plan.

The inputs are the old variables, decrypted (`ansible-vault view` to a file or
stdin; later files win, so pass group vars before host vars), and, from the
migrated VM, its host.env, the stack's .env and rclone.conf, and its live
config.toml, which is read only for the Nextcloud account when no rclone.conf
is given. The live files are what the VM runs, so a first `--check` with the
result and the VM's snapshot should report no drift.

The output holds credentials. It is written 0600 and only names are printed;
encrypt it with `ansible-vault encrypt` before it goes anywhere. A credential
with no stack secrets file is a refusal with nothing written, the import's
rule: a credential is never dropped silently. The master key and the web token
key have no role variable (they live in the state volume) and are kept as
`vault_istota_secret_key` and `vault_istota_web_token_key`, because the vault
is their only copy off the VM.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from istota import apply as apply_module
from istota.lib.rclone_conf import obscure as rclone_obscure
from istota.lib.rclone_conf import render_webdav_remote
from istota.maintenance.bare_metal_import import Refusal, parse_env_file

EXIT_OK = 0
EXIT_REFUSED = 1

#: The old role's variable for each stack secrets file. Held to
#: `SECRET_NAMES` by tests/test_role_vars.py.
SECRET_VARS = {
    "istota_nextcloud_app_password": "istota_nextcloud_app_password",
    "istota_caldav_password": "istota_caldav_password",
    "istota_email_imap_password": "istota_email_imap_password",
    "istota_email_smtp_password": "istota_email_smtp_password",
    "istota_sms_telnyx_api_key": "istota_sms_telnyx_api_key",
    "istota_sms_telnyx_public_key": "istota_sms_telnyx_public_key",
    "istota_sms_telnyx_messaging_profile_id": "istota_sms_telnyx_messaging_profile_id",
    "istota_developer_gitlab_token": "istota_developer_gitlab_token",
    "istota_developer_github_token": "istota_developer_github_token",
    "istota_web_oauth2_client_secret": "istota_web_oauth2_client_secret",
    "istota_web_secret_key": "istota_web_session_secret_key",
    "istota_google_workspace_client_secret": "istota_google_workspace_client_secret",
    "istota_claude_code_oauth_token": "claude_code_oauth_token",
    "istota_brain_native_api_key": "istota_brain_native_api_key",
    "istota_sms_twilio_auth_token": "istota_sms_twilio_auth_token",
    "istota_sms_twilio_api_key_sid": "istota_sms_twilio_api_key_sid",
    "istota_sms_twilio_api_key_secret": "istota_sms_twilio_api_key_secret",
    "istota_whatsapp_access_token": "istota_whatsapp_access_token",
    "istota_whatsapp_app_secret": "istota_whatsapp_app_secret",
    "istota_whatsapp_verify_token": "istota_whatsapp_verify_token",
    "istota_provider_anthropic_api_key": "istota_provider_anthropic_api_key",
    "istota_provider_anthropic_extra_headers": "istota_provider_anthropic_extra_headers",
    "istota_provider_openrouter_api_key": "istota_provider_openrouter_api_key",
    "istota_provider_openrouter_extra_headers": "istota_provider_openrouter_extra_headers",
    "istota_anthropic_listing_key": "istota_anthropic_listing_key",
}

#: Values the old role rendered beside its secrets that have no stack secrets
#: file: the two Twilio identifiers, which are config values, not credentials.
NO_STACK_FILE = (
    "istota_sms_twilio_account_sid",
    "istota_sms_twilio_messaging_service_sid",
)

#: Keys the state volume holds, kept in the vault as the only copy off the VM.
KEY_VARS = {
    "istota_secret_key": "vault_istota_secret_key",
    "istota_web_token_key": "vault_istota_web_token_key",
}

#: host.env keys and the role variables that write them.
HOST_ENV_VARS = {
    "STORAGE": "istota_storage",
    "INGRESS": "istota_ingress",
    "DOMAIN": "istota_domain",
    "TLS_CERT_SOURCE": "istota_tls_cert_source",
    "UPSTREAM_PROXY": "istota_upstream_proxy",
    "LISTEN_ADDR": "istota_listen_addr",
    "LISTEN_PORT": "istota_listen_port",
    "RELEASE_SIGNING_KEY": "istota_release_signing_key",
    "CLAUDE_CODE_VERSION": "istota_claude_code_version",
    "ACME_EMAIL": "istota_acme_email",
    "ACME_SERVER": "istota_acme_server",
    "RCLONE_REMOTE": "istota_rclone_remote",
    "ZRAM_ENABLED": "istota_zram_enabled",
    "HOST_LOOPBACK_BLOCK": "istota_host_loopback_block",
    "HOST_GATEWAY_ADDR": "istota_host_gateway_addr",
    "HOST_LOOPBACK_ALLOW": "istota_host_loopback_allow",
}

#: Old variables the conversion reads besides the secrets and keys.
CONSUMED = {
    "istota_admin_users", "istota_user_secrets",
    "istota_nextcloud_url", "istota_nextcloud_username", "istota_rclone_password_obscured",
    "istota_rclone_remote", "istota_caldav_url",
}

_REFERENCE = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")


@dataclass
class Conversion:
    vars: dict
    notes: list[str] = field(default_factory=list)


def parse_env(text: str, name: str) -> dict[str, str]:
    return parse_env_file(text, name)


def _resolve(old: dict, value, notes: list[str], where: str, depth: int = 0):
    """A value that is exactly `{{ other_var }}` becomes that variable's value."""
    if isinstance(value, str) and "{{" in value:
        match = _REFERENCE.match(value.strip())
        if match and match.group(1) in old and depth < 10:
            return _resolve(old, old[match.group(1)], notes, where, depth + 1)
        notes.append(f"{where}: a template the converter cannot resolve; carried as written")
        return value
    if isinstance(value, dict):
        return {k: _resolve(old, v, notes, f"{where}.{k}", depth) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(old, v, notes, f"{where}[{i}]", depth) for i, v in enumerate(value)]
    return value


def _secrets(old: dict, notes: list[str]) -> dict[str, str]:
    no_file = [name for name in NO_STACK_FILE if old.get(name)]
    if no_file:
        raise Refusal(
            "no stack secrets file for " + ", ".join(no_file) + ". Nothing was written. "
            "Set these in the plan's config (the [sms.twilio] keys); "
            "a value is never dropped silently."
        )
    secrets: dict[str, str] = {}
    for variable, name in SECRET_VARS.items():
        value = old.get(variable) or ""
        if variable == "istota_email_smtp_password" and not value:
            value = old.get("istota_email_imap_password") or ""
        if variable == "istota_caldav_password" and value and not old.get("istota_caldav_url"):
            notes.append(
                "istota_caldav_password: left out, as the old role did without istota_caldav_url"
            )
            continue
        if value:
            secrets[name] = str(value)
    return secrets


def _rclone_conf(old: dict, config: dict, remote: str, obscure: Callable[[str], str], notes) -> str:
    nextcloud = config.get("nextcloud") if isinstance(config.get("nextcloud"), dict) else {}
    url = (nextcloud.get("url") or old.get("istota_nextcloud_url") or "").rstrip("/")
    username = nextcloud.get("username") or old.get("istota_nextcloud_username") or ""
    password = old.get("istota_rclone_password_obscured") or ""
    if not password and old.get("istota_nextcloud_app_password"):
        password = obscure(str(old["istota_nextcloud_app_password"]))
    if not (url and username and password):
        notes.append("istota_rclone_conf: not written; pass --rclone-conf with the VM's file")
        return ""
    return render_webdav_remote(
        remote, url, username, password, dav_prefix=str(nextcloud.get("dav_prefix") or ""),
    )


def _plan_secrets(old_secrets, notes: list[str]) -> dict:
    """The old `istota_user_secrets` lists as the sidecar's user -> service -> key -> value."""
    users: dict = {}
    for user, entries in (old_secrets or {}).items():
        for index, entry in enumerate(entries or []):
            where = f"istota_user_secrets.{user}[{index}]"
            if not isinstance(entry, dict):
                notes.append(f"{where}: not a mapping; dropped")
                continue
            service, key, value = entry.get("service"), entry.get("key"), entry.get("value")
            if not isinstance(service, str) or not isinstance(key, str):
                notes.append(f"{where}: no service and key; dropped")
                continue
            # The old role skipped an empty value, so it never cleared a credential.
            if value is None or value == "":
                continue
            users.setdefault(user, {}).setdefault(service, {})[key] = str(value)
    return {"users": users} if users else {}


def _host_vars(host_env: dict[str, str], stack_env: dict[str, str], notes: list[str]) -> dict:
    out: dict = {}
    extra: dict[str, str] = {}
    for key, value in host_env.items():
        variable = HOST_ENV_VARS.get(key)
        if variable is None:
            extra[key] = value
        elif key == "LISTEN_PORT":
            out[variable] = int(value) if value.isdigit() else value
        elif key in ("ZRAM_ENABLED", "HOST_LOOPBACK_BLOCK"):
            out[variable] = value == "1"
        elif key == "HOST_LOOPBACK_ALLOW":
            out[variable] = [int(p) if p.isdigit() else p for p in value.split()]
        else:
            out[variable] = value
    if extra:
        out["istota_host_env_extra"] = extra
    tag = stack_env.get("ISTOTA_TAG", "")
    unsigned = stack_env.get("ISTOTA_TAG_UNSIGNED") == "1"
    if unsigned and re.fullmatch(r"main-[0-9a-f]{12}", tag):
        out["istota_update_mode"] = "latest"
    elif tag:
        out["istota_update_mode"] = "release"
        out["istota_release"] = tag
        if unsigned:
            notes.append(
                f"istota_release: {tag} was installed unsigned; the role leaves it alone while "
                "istota_release matches, and verifies any tag it updates to"
            )
    profiles = [p for p in stack_env.get("COMPOSE_PROFILES", "").split(",") if p]
    if profiles:
        out["istota_compose_profiles"] = profiles
    if host_env and not host_env.get("RELEASE_SIGNING_KEY") and out.get("istota_update_mode") == "release":
        notes.append("istota_release_signing_key: empty on the VM, and release mode requires one")
    return out


def convert(
    old: dict,
    *,
    config: dict | None = None,
    host_env: dict[str, str] | None = None,
    stack_env: dict[str, str] | None = None,
    rclone_conf: str | None = None,
    obscure: Callable[[str], str] = rclone_obscure,
) -> Conversion:
    """The management role's variables from the old role's. Pure but for `obscure`."""
    notes: list[str] = []
    old = {k: _resolve(old, v, notes, k) for k, v in old.items()}
    host_env = host_env or {}
    stack_env = stack_env or {}
    out: dict = {}

    out.update(_host_vars(host_env, stack_env, notes))

    out["istota_secrets"] = _secrets(old, notes)
    storage = host_env.get("STORAGE") or ("nextcloud" if old.get("istota_nextcloud_url") else "local")
    if storage == "nextcloud":
        remote = host_env.get("RCLONE_REMOTE") or old.get("istota_rclone_remote") or "nextcloud"
        conf = rclone_conf if rclone_conf is not None else _rclone_conf(old, config or {}, remote, obscure, notes)
        if conf:
            out["istota_rclone_conf"] = conf

    if old.get("istota_admin_users"):
        out["istota_admins"] = list(old["istota_admin_users"])

    plan_secrets = _plan_secrets(old.get("istota_user_secrets"), notes)
    if plan_secrets:
        out["istota_plan_secrets"] = plan_secrets
    notes.append(
        "istota_plan: not written; take `istota-stack exec istota snapshot --format json` "
        "on the VM, which holds the users, their settings and the config the import carried"
    )

    for variable, kept_as in KEY_VARS.items():
        if old.get(variable):
            out[kept_as] = str(old[variable])

    handled = set(SECRET_VARS) | set(NO_STACK_FILE) | set(KEY_VARS) | CONSUMED
    dropped = sorted(name for name in old if name not in handled)
    if dropped:
        notes.append(
            f"{len(dropped)} old variable(s) not carried (the application settings come from "
            "the VM's snapshot, and the VM's files own the rest): " + ", ".join(dropped)
        )
    return Conversion(vars=out, notes=notes)


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper: yaml.SafeDumper, value: str):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _str)


def render(variables: dict) -> str:
    return (
        "# The istota management role's variables, written by `istota migrate role-vars`.\n"
        "# Holds credentials: encrypt with `ansible-vault encrypt` before committing.\n"
        + yaml.dump(variables, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=1000)
    )


def _read(path: str) -> str:
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="istota migrate role-vars",
        description="Write the management role's variables from the old bare-metal role's.",
    )
    parser.add_argument(
        "--old-vars", action="append", required=True, metavar="FILE",
        help="The old role's variables, decrypted YAML; '-' for stdin. Repeat: later files win",
    )
    parser.add_argument("--config", metavar="FILE", help="The VM's live config.toml, for the Nextcloud account rclone.conf is rendered with")
    parser.add_argument("--host-env", metavar="FILE", help="The VM's host.env")
    parser.add_argument("--stack-env", metavar="FILE", help="The stack's .env")
    parser.add_argument("--rclone-conf", metavar="FILE", help="The VM's rclone.conf; otherwise rendered from the old variables")
    parser.add_argument("-o", "--output", required=True, metavar="FILE", help="Where to write the variables (0600)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    err = sys.stderr
    try:
        old: dict = {}
        for path in args.old_vars:
            try:
                loaded = yaml.safe_load(_read(path)) or {}
            except yaml.YAMLError as exc:
                # PyYAML's message quotes the offending line, which is a credential here.
                mark = getattr(exc, "problem_mark", None)
                where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
                raise Refusal(f"{path}: not valid YAML{where}") from None
            if not isinstance(loaded, dict):
                raise Refusal(f"{path}: not a mapping of variables")
            old.update(loaded)
        config = tomllib.loads(_read(args.config)) if args.config else None
        host_env = parse_env(_read(args.host_env), "host.env") if args.host_env else None
        stack_env = parse_env(_read(args.stack_env), ".env") if args.stack_env else None
        rclone_conf = _read(args.rclone_conf) if args.rclone_conf else None
        result = convert(old, config=config, host_env=host_env, stack_env=stack_env, rclone_conf=rclone_conf)
    except (Refusal, OSError, yaml.YAMLError, tomllib.TOMLDecodeError) as exc:
        print(f"refused: {exc}", file=err)
        return EXIT_REFUSED

    apply_module.write_private(Path(args.output), render(result.vars))
    print(f"wrote {args.output}: " + ", ".join(result.vars), file=err)
    secrets = result.vars.get("istota_secrets", {})
    print(f"istota_secrets: {', '.join(secrets) or 'none'}", file=err)
    for note in result.notes:
        print(f"note: {note}", file=err)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
