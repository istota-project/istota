#!/usr/bin/env python3
"""Convert an istota settings.toml file to an Ansible vars YAML file.

Settings keys mirror Ansible variable names without the istota_ prefix.
This script adds the prefix and outputs valid YAML for --extra-vars.

Uses only stdlib (Python 3.11+).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            print("Python 3.11+ required, or install tomli: pip install tomli", file=sys.stderr)
            sys.exit(1)


def _yaml_scalar(value: object) -> str:
    """Format a Python value as a YAML scalar."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # Quote strings that could be misinterpreted by YAML
        if not value:
            return '""'
        needs_quote = any([
            value.lower() in ("true", "false", "yes", "no", "null", "~"),
            value[0] in ('"', "'", "{", "[", "|", ">", "!", "&", "*", "?", "#", "%", "@", "`"),
            ": " in value,
            value.startswith("- "),
            "\n" in value,
        ])
        if needs_quote or not value.isprintable():
            # Use double quotes with minimal escaping
            escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            return f'"{escaped}"'
        return f'"{value}"'
    return str(value)


def _yaml_list(items: list, indent: int = 0) -> str:
    """Format a list as YAML."""
    prefix = " " * indent
    if not items:
        return "[]"
    # Check if all items are scalars
    if all(isinstance(item, (str, int, float, bool)) for item in items):
        lines = []
        for item in items:
            lines.append(f"{prefix}- {_yaml_scalar(item)}")
        return "\n".join(lines)
    # Complex items (dicts in lists)
    lines = []
    for item in items:
        if isinstance(item, dict):
            first = True
            for k, v in item.items():
                formatted = _format_value(v, indent + 4)
                if isinstance(v, dict):
                    # Nested dict: put on next lines indented
                    if first:
                        lines.append(f"{prefix}- {k}:")
                        first = False
                    else:
                        lines.append(f"{prefix}  {k}:")
                    lines.append(_yaml_dict(v, indent + 4))
                else:
                    if first:
                        lines.append(f"{prefix}- {k}: {formatted}")
                        first = False
                    else:
                        lines.append(f"{prefix}  {k}: {formatted}")
        else:
            lines.append(f"{prefix}- {_yaml_scalar(item)}")
    return "\n".join(lines)


def _format_value(value: object, indent: int = 0) -> str:
    """Format any value as YAML."""
    if isinstance(value, list):
        if not value:
            return "[]"
        # For simple scalar lists, use inline if short
        if all(isinstance(item, (str, int, float, bool)) for item in value):
            inline = "[" + ", ".join(_yaml_scalar(item) for item in value) + "]"
            if len(inline) < 80:
                return inline
        return "\n" + _yaml_list(value, indent)
    if isinstance(value, dict):
        return "\n" + _yaml_dict(value, indent)
    return _yaml_scalar(value)


def _yaml_dict(d: dict, indent: int = 0) -> str:
    """Format a dict as YAML."""
    prefix = " " * indent
    lines = []
    for k, v in d.items():
        if isinstance(v, dict):
            lines.append(f"{prefix}{k}:")
            lines.append(_yaml_dict(v, indent + 2))
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            lines.append(f"{prefix}{k}:")
            lines.append(_yaml_list(v, indent + 2))
        else:
            lines.append(f"{prefix}{k}: {_format_value(v, indent + 2)}")
    return "\n".join(lines)


# Top-level settings keys that map directly with istota_ prefix.
# Keys not in this list are handled specially (sections, users, etc.)
_DIRECT_KEYS = {
    "home": "istota_home",
    "namespace": "istota_namespace",
    "package": "istota_package",
    "bot_name": "istota_bot_name",
    "emissaries_enabled": "istota_emissaries_enabled",
    "model": "istota_model",
    "repo_url": "istota_repo_url",
    "repo_branch": "istota_repo_branch",
    "repo_tag": "istota_repo_tag",
    "rclone_remote": "istota_rclone_remote",
    "rclone_password_obscured": "istota_rclone_password_obscured",
    "use_nextcloud_mount": "istota_use_nextcloud_mount",
    "nextcloud_mount_path": "istota_nextcloud_mount_path",
    "nextcloud_url": "istota_nextcloud_url",
    "nextcloud_username": "istota_nextcloud_username",
    "nextcloud_app_password": "istota_nextcloud_app_password",
    "claude_oauth_token": "istota_claude_code_oauth_token",
    "admin_users": "istota_admin_users",
    "disabled_skills": "istota_disabled_skills",
    "use_environment_file": "istota_use_environment_file",
    "configure_rclone": "istota_configure_rclone",
    "install_all_extras": "istota_install_all_extras",
}

# Section keys that flatten with istota_{section}_{key} pattern
_SECTION_FLAT_KEYS = {
    "talk": {
        "enabled": "istota_talk_enabled",
        "bot_username": "istota_talk_bot_username",
    },
    "email": {
        "enabled": "istota_email_enabled",
        "imap_host": "istota_email_imap_host",
        "imap_port": "istota_email_imap_port",
        "imap_user": "istota_email_imap_user",
        "imap_password": "istota_email_imap_password",
        "smtp_host": "istota_email_smtp_host",
        "smtp_port": "istota_email_smtp_port",
        "smtp_password": "istota_email_smtp_password",
        "poll_folder": "istota_email_poll_folder",
        "bot_email": "istota_email_bot_address",
    },
    "ntfy": {
        "enabled": "istota_ntfy_enabled",
        "server_url": "istota_ntfy_server_url",
        "topic": "istota_ntfy_topic",
        "token": "istota_ntfy_token",
        "username": "istota_ntfy_username",
        "password": "istota_ntfy_password",
        "priority": "istota_ntfy_priority",
    },
    "browser": {
        "enabled": "istota_browser_enabled",
        "api_port": "istota_browser_api_port",
        "vnc_port": "istota_browser_vnc_port",
        "vnc_password": "istota_browser_vnc_password",
        "vnc_external_url": "istota_browser_vnc_external_url",
        "max_sessions": "istota_browser_max_sessions",
        "shm_size": "istota_browser_shm_size",
    },
    "location": {
        "enabled": "istota_location_enabled",
        "webhooks_port": "istota_webhooks_port",
    },
    "whisper": {
        "enabled": "istota_whisper_enabled",
        "model": "istota_whisper_model",
        "max_model": "istota_whisper_max_model",
    },
    "backup": {
        "enabled": "istota_backup_enabled",
    },
    "site": {
        "enabled": "istota_site_enabled",
        "hostname": "istota_hostname",
        "base_path": "istota_site_base_path",
    },
    "web": {
        "enabled": "istota_web_enabled",
        "port": "istota_web_port",
        "oidc_issuer": "istota_web_oidc_issuer",
        "oidc_client_id": "istota_web_oidc_client_id",
        "oidc_client_secret": "istota_web_oidc_client_secret",
        "secret_key": "istota_web_secret_key",
    },
}

# Sections that map their keys with a common prefix
_SECTION_PREFIX_MAP = {
    "conversation": "istota_conversation_",
    "logging": "istota_logging_",
    "scheduler": "istota_scheduler_",
}

# Security section has nested structure
_SECURITY_KEYS = {
    "sandbox_enabled": "istota_security_sandbox_enabled",
    "sandbox_admin_db_write": "istota_security_sandbox_admin_db_write",
    "skill_proxy_enabled": "istota_security_skill_proxy_enabled",
    "skill_proxy_timeout": "istota_security_skill_proxy_timeout",
    "network_enabled": "istota_security_network_enabled",
    "network_allow_pypi": "istota_security_network_allow_pypi",
    "network_extra_hosts": "istota_security_network_extra_hosts",
}

# Nested sections that map as structured dicts
_NESTED_SECTIONS = {
    "sleep_cycle": {
        "enabled": "istota_sleep_cycle_enabled",
        "cron": "istota_sleep_cycle_cron",
        "lookback_hours": "istota_sleep_cycle_lookback_hours",
        "memory_retention_days": "istota_sleep_cycle_memory_retention_days",
        "auto_load_dated_days": "istota_sleep_cycle_auto_load_dated_days",
        "curate_user_memory": "istota_sleep_cycle_curate_user_memory",
    },
    "channel_sleep_cycle": {
        "enabled": "istota_channel_sleep_cycle_enabled",
        "cron": "istota_channel_sleep_cycle_cron",
        "lookback_hours": "istota_channel_sleep_cycle_lookback_hours",
        "memory_retention_days": "istota_channel_sleep_cycle_memory_retention_days",
    },
    "memory_search": {
        "enabled": "istota_memory_search_enabled",
        "auto_index_conversations": "istota_memory_search_auto_index_conversations",
        "auto_index_memory_files": "istota_memory_search_auto_index_memory_files",
        "auto_recall": "istota_memory_search_auto_recall",
        "auto_recall_limit": "istota_memory_search_auto_recall_limit",
    },
}

# Developer section
_DEVELOPER_KEYS = {
    "enabled": "istota_developer_enabled",
    "repos_dir": "istota_developer_repos_dir",
    "gitlab_url": "istota_developer_gitlab_url",
    "gitlab_token": "istota_developer_gitlab_token",
    "gitlab_username": "istota_developer_gitlab_username",
    "gitlab_default_namespace": "istota_developer_gitlab_default_namespace",
    "gitlab_reviewer_id": "istota_developer_gitlab_reviewer_id",
    "gitlab_api_allowlist": "istota_developer_gitlab_api_allowlist",
    "github_url": "istota_developer_github_url",
    "github_token": "istota_developer_github_token",
    "github_username": "istota_developer_github_username",
    "github_default_owner": "istota_developer_github_default_owner",
    "github_reviewer": "istota_developer_github_reviewer",
    "github_api_allowlist": "istota_developer_github_api_allowlist",
}


def convert(settings: dict) -> dict:
    """Convert a settings dict to Ansible vars dict."""
    result: dict = {}

    # Direct top-level keys
    for settings_key, ansible_key in _DIRECT_KEYS.items():
        if settings_key in settings:
            result[ansible_key] = settings[settings_key]

    # Flat section keys
    for section_name, key_map in _SECTION_FLAT_KEYS.items():
        section = settings.get(section_name, {})
        if isinstance(section, dict):
            for settings_key, ansible_key in key_map.items():
                if settings_key in section:
                    result[ansible_key] = section[settings_key]

    # Prefix-mapped sections (conversation, logging, scheduler)
    for section_name, prefix in _SECTION_PREFIX_MAP.items():
        section = settings.get(section_name, {})
        if isinstance(section, dict):
            for key, value in section.items():
                result[f"{prefix}{key}"] = value

    # Security section (can have nested [security.network])
    security = settings.get("security", {})
    if isinstance(security, dict):
        for key, ansible_key in _SECURITY_KEYS.items():
            if key in security:
                result[ansible_key] = security[key]
        # Handle nested [security.network] section
        network = security.get("network", {})
        if isinstance(network, dict):
            for key, value in network.items():
                ansible_key = f"istota_security_network_{key}"
                if ansible_key.replace("istota_security_network_", "") in ("enabled", "allow_pypi", "extra_hosts"):
                    result[_SECURITY_KEYS.get(f"network_{key}", ansible_key)] = value

    # Nested sections with explicit key maps
    for section_name, key_map in _NESTED_SECTIONS.items():
        section = settings.get(section_name, {})
        if isinstance(section, dict):
            for settings_key, ansible_key in key_map.items():
                if settings_key in section:
                    result[ansible_key] = section[settings_key]

    # Developer section
    developer = settings.get("developer", {})
    if isinstance(developer, dict):
        for key, ansible_key in _DEVELOPER_KEYS.items():
            if key in developer:
                result[ansible_key] = developer[key]

    # Briefing defaults (pass through as-is, it's a nested dict)
    briefing_defaults = settings.get("briefing_defaults", {})
    if briefing_defaults:
        result["istota_briefing_defaults"] = briefing_defaults

    # Users section — pass through as-is (Ansible expects istota_users dict)
    users = settings.get("users", {})
    if users:
        result["istota_users"] = users

    return result


def to_yaml(vars_dict: dict) -> str:
    """Render vars dict as YAML string."""
    lines = ["---", "# Ansible vars generated from settings.toml by settings_to_vars.py", ""]
    for key, value in sorted(vars_dict.items()):
        if isinstance(value, dict):
            lines.append(f"{key}:")
            lines.append(_yaml_dict(value, indent=2))
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            lines.append(f"{key}:")
            lines.append(_yaml_list(value, indent=2))
        else:
            lines.append(f"{key}: {_format_value(value, indent=2)}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert istota settings.toml to Ansible vars YAML"
    )
    parser.add_argument(
        "--settings", "-s",
        default="/etc/istota/settings.toml",
        help="Path to settings TOML file (default: /etc/istota/settings.toml)",
    )
    parser.add_argument(
        "--output", "-o",
        default="/etc/istota/vars.yml",
        help="Output YAML file path (default: /etc/istota/vars.yml)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print YAML to stdout instead of writing to file",
    )
    args = parser.parse_args()

    settings_path = Path(args.settings)
    if not settings_path.exists():
        print(f"Settings file not found: {settings_path}", file=sys.stderr)
        sys.exit(1)

    with open(settings_path, "rb") as f:
        settings = tomllib.load(f)

    vars_dict = convert(settings)
    yaml_text = to_yaml(vars_dict)

    if args.dry_run:
        print(yaml_text)
    else:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(yaml_text)
        print(f"  wrote {output_path}")


if __name__ == "__main__":
    main()
