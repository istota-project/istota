# Adding skills

Skills are self-contained directories under `src/istota/skills/`. Each skill needs a `skill.md` file with YAML frontmatter for metadata and a markdown body for documentation.

## 1. Create the skill directory

```
src/istota/skills/my_skill/
├── skill.md       # Frontmatter metadata + documentation (required)
├── __init__.py    # CLI module (optional)
└── __main__.py    # python -m support (optional)
```

## 2. Write skill.md with frontmatter

All metadata lives in the YAML frontmatter block. The markdown body is the documentation loaded into Claude's prompt.

```yaml
---
name: my_skill
triggers: [my_keyword, another_keyword]
description: One-line description shown in the menu catalogue and `!skills`
resource_types: [my_resource]
cli: true
dependencies: [some-package]
env: [{"var":"MY_VAR","from":"user_resource_config","resource_type":"my_resource","field":"path"}]
---

# My Skill

Instructions for Claude on how to use this skill...
```

Use `{BOT_NAME}`, `{BOT_DIR}`, and `{user_id}` placeholders -- they're substituted at load time.

### Frontmatter fields

| Field | Type | Purpose |
|---|---|---|
| `name` | string | Skill identifier, matches directory name |
| `triggers` | list | Documentation-only keywords surfaced by `!skills`; not a selector |
| `description` | string | Shown in the menu catalogue and `!skills` |
| `always_include` | bool | Load for every task |
| `admin_only` | bool | Hidden from non-admin users |
| `cli` | bool | Whether this skill has a CLI module |
| `resource_types` | list | Menu-membership gate (a menu entry only when the user has a matching resource) |
| `source_types` | list | Auto-include for these task source types |
| `file_types` | list | Auto-include for these attachment extensions |
| `companion_skills` | list | Pull in these skills when this one is selected |
| `exclude_skills` | list | Remove these skills when this one is selected |
| `dependencies` | list | Python packages required (skip skill if missing) |
| `exclude_memory` | bool | Skip memory loading for tasks using this skill |
| `exclude_persona` | bool | Skip persona loading |
| `exclude_resources` | list | Resource types to hide from prompt |
| `env` | JSON array | Declarative env var specs (see env var sources below) |

Boolean fields default to `false`. List fields default to `[]`. Only include fields that differ from defaults.

## 3. (Optional) Create a CLI module

Skills can expose Python CLIs invoked by Claude via `python -m istota.skills.my_skill`:

```python
# __init__.py
import argparse, json, sys

def build_parser():
    parser = argparse.ArgumentParser(description="My skill")
    sub = parser.add_subparsers(dest="command")
    cmd = sub.add_parser("my-command")
    cmd.add_argument("--flag")
    return parser

def cmd_my_command(args):
    result = {"status": "ok"}
    print(json.dumps(result))

def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "my-command":
        cmd_my_command(args)
    else:
        parser.print_help()
        sys.exit(1)

if __name__ == "__main__":
    main()
```

```python
# __main__.py
from istota.skills.my_skill import main
main()
```

Pattern: `build_parser()` + `main()`, JSON output, credentials via env vars. See [credentials](../configuration/credentials.md) for the two-tier model and how to wire new credentials into the proxy.

## 4. Declare env vars in the manifest

The skill's `env:` block is the **only** place env vars should be wired. The hardcoded credential-injection block in `executor.py` is gone; `build_skill_env()` walks every loaded skill's manifest and resolves each `EnvSpec` against the task's `EnvContext`. The `derive_*` helpers (see [credentials](../configuration/credentials.md#how-credentials-flow-at-runtime)) compute the proxy strip-set, auth map, and lookup allowlist directly from these manifests — no executor edits, no separate `_PROXY_CREDENTIAL_VARS` / `_CREDENTIAL_SKILL_MAP` to keep in sync.

```yaml
env:
  - {"var":"MY_RESOURCE_PATH","from":"resource","resource_type":"my_resource","field":"path"}
  - {"var":"MY_API_KEY","from":"secret","service":"my_service","key":"api_key","sensitive":true}
  - {"var":"MY_API_HOST","from":"config","config_path":"my_section.api_host","when":"my_section.enabled"}
```

For complex setups that need to compute values, write helper scripts, or bind-mount files into the sandbox (see `developer` for a worked example), export `setup_env(ctx) -> dict[str, str]` in the skill's `__init__.py` and use `from: "setup_env"` for the corresponding `var`. The hook fires for the full index regardless of selection, so the skill's helper scripts work even when the skill itself isn't keyword-matched.

## 5. (Optional) Add a new resource type

If your skill needs a new resource type:

1. Users add via: `istota resource ensure -u USER -t my_resource -p /path`
2. Declare the env vars in the manifest (step 4)
3. Add resource display in `build_prompt()` if users should see it
4. Document in the skill's `.md` file

## Env var sources

| `from:` | Purpose |
|---|---|
| `config` | Dotted config path. Use `when:` (string or list) to gate on truthy paths |
| `resource` | Resource mount path |
| `resource_json` | All resources of a type as JSON |
| `user_resource_config` | TOML `[[resources]]` `extras` field |
| `secret` | Per-user encrypted secret (`service` + `key` from the `secrets` table) |
| `setup_env` | Value computed by the skill's `setup_env(ctx)` hook in `__init__.py` |
| `template_file` | Auto-create file from a template |
| `user_id` | Literal task `user_id` |

`EnvSpec` flags:

| Flag | Meaning |
|---|---|
| `sensitive: true` | Treat as a credential — strip from Claude's env, route through the proxy. Auto-enrolls the skill in `derive_credential_set` / `derive_skill_credential_map` |
| `fallback_var: "FOO"` | Read `os.environ["FOO"]` if primary resolution fails. Honored on the value path only — auto-authorization disables fallbacks so an instance-wide `EnvironmentFile` value can't fan out to per-user auth |
| `gate_user_has_resource: "<type>"` | Only resolve when the user owns at least one resource of that type |
| `gate_has_discovered_calendars: true` | Only resolve when CalDAV discovery returned at least one calendar |
