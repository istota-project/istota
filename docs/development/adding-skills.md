# Adding skills

Skills are self-contained directories under `src/istota/skills/`. Each skill needs at minimum a `skill.toml` manifest and a `skill.md` documentation file.

## 1. Create the skill directory

```
src/istota/skills/my_skill/
├── skill.toml     # Manifest (required)
├── skill.md       # Documentation for Claude (required)
├── __init__.py    # CLI module (optional)
└── __main__.py    # python -m support (optional)
```

## 2. Define the manifest

```toml
[skill]
description = "What this skill does"
keywords = ["trigger", "words"]        # Optional: prompt keywords
resource_types = ["my_resource"]       # Optional: require user resource
source_types = ["briefing"]            # Optional: auto-include for source type
always_include = false                 # Default
admin_only = false                     # Default
dependencies = ["some-package"]        # Optional: skip if missing

[[env]]                                # Optional: declarative env vars
name = "MY_VAR"
source = "resource"
resource_type = "my_resource"
field = "path"
```

### Manifest fields

| Field | Purpose |
|---|---|
| `description` | Shown in `!skills` and LLM classification manifest |
| `keywords` | Prompt words that trigger this skill (Pass 1) |
| `resource_types` | Required user resources (combined with keywords) |
| `source_types` | Auto-include for these task source types |
| `file_types` | Auto-include for these attachment extensions |
| `always_include` | Load for every task |
| `admin_only` | Hidden from non-admin users |
| `companion_skills` | Pull in these skills when this one is selected |
| `exclude_skills` | Remove these skills when this one is selected |
| `dependencies` | Python packages required (skip skill if missing) |
| `cli` | Whether this skill has a CLI module |
| `exclude_memory` | Skip memory loading for tasks using this skill |
| `exclude_persona` | Skip persona loading |
| `exclude_resources` | Resource types to hide from prompt |

## 3. Write the documentation

`skill.md` is what Claude sees. It should explain how to use the skill's tools and CLIs. Optional YAML frontmatter overrides routing metadata:

```yaml
---
name: My Skill
triggers:
  - my_keyword
  - another_keyword
description: One-line description for the LLM classification manifest
---

# My Skill

Instructions for Claude on how to use this skill...
```

Frontmatter `triggers` overrides `skill.toml` `keywords`. Frontmatter `description` overrides `skill.toml` `description`.

Use `{BOT_NAME}`, `{BOT_DIR}`, and `{user_id}` placeholders -- they're substituted at load time.

## 4. (Optional) Create a CLI module

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

## 5. (Optional) Add env var mapping

For skills with resource types, add env var mapping in `executor.py`:

```python
# In execute_task(), after existing resource mappings
my_resources = [r for r in user_resources if r.resource_type == "my_resource"]
if my_resources:
    env["MY_RESOURCE_PATH"] = str(
        config.nextcloud_mount_path / my_resources[0].resource_path.lstrip("/")
    )
```

Or use declarative env vars in `skill.toml`:

```toml
[[env]]
name = "MY_RESOURCE_PATH"
source = "resource"
resource_type = "my_resource"
field = "path"
```

Declarative env vars don't override hardcoded ones in executor.py.

## 6. (Optional) Add a new resource type

If your skill needs a new resource type:

1. Users add via: `istota resource add -u USER -t my_resource -p /path`
2. Add env var mapping (step 5)
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
