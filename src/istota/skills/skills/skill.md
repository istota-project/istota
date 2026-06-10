---
name: skills
description: Load full instructions for a skill on demand, or list available skills
always_include: true
cli: true
---
# Skills loader

Some skills are listed in "Available skills (load on demand)" near the tools
section with only a one-line description — their full instructions are not in
this prompt. Load a skill's documentation before you use it:

```bash
istota-skill skills show <name>    # Print the full instructions for <name>
istota-skill skills list           # List every skill you can load (name + description)
```

`show` prints the same markdown documentation that would otherwise be inlined
in this prompt. Run it once for a deferred skill, read the instructions, then
use that skill's own CLI. Do not guess a deferred skill's subcommands — load
its docs first.

If `show` reports the skill is unknown or unavailable, proceed without it (it
may be disabled or restricted for your account).
