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
description: One-line description for the LLM classification manifest
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
| `triggers` | list | Prompt keywords that trigger this skill (Pass 1) |
| `description` | string | Shown in `!skills` and LLM classification manifest |
| `always_include` | bool | Load for every task |
| `admin_only` | bool | Hidden from non-admin users |
| `cli` | bool | Whether this skill has a CLI module |
| `resource_types` | list | Required user resources (combined with keywords) |
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

Pattern: `build_parser()` + `main()`, JSON output, credentials via env vars.

## 4. (Optional) Add env var mapping

For skills with resource types, use declarative env vars in the frontmatter `env` field:

```yaml
env: [{"var":"MY_RESOURCE_PATH","from":"resource","resource_type":"my_resource","field":"path"}]
```

Or add env var mapping directly in `executor.py`:

```python
# In execute_task(), after existing resource mappings
my_resources = [r for r in user_resources if r.resource_type == "my_resource"]
if my_resources:
    env["MY_RESOURCE_PATH"] = str(
        config.nextcloud_mount_path / my_resources[0].resource_path.lstrip("/")
    )
```

Declarative env vars don't override hardcoded ones in executor.py.

## 5. (Optional) Add a new resource type

If your skill needs a new resource type:

1. Users add via: `istota resource add -u USER -t my_resource -p /path`
2. Add env var mapping (step 4)
3. Add resource display in `build_prompt()` if users should see it
4. Document in the skill's `.md` file

## Env var sources

| Source | Purpose |
|---|---|
| `config` | Dotted config path with optional guard |
| `resource` | DB resource mount path |
| `resource_json` | All resources of a type as JSON |
| `user_resource_config` | From per-user TOML `[[resources]]` extra fields |
| `template_file` | Auto-create file from template |

Skills with complex env setup can export `setup_env(ctx) -> dict[str, str]` in their `__init__.py`, called after declarative resolution.
