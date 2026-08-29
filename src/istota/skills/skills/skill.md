---
name: skills
description: Load full instructions for a skill on demand, list available skills, or read the user's own per-skill additions
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

## Per-skill overlays

The user can add their own instructions to any skill, in a file of their own at
`{BOT_DIR}/config/skills/<skill-name>.md`. Whatever is in it is appended to that
skill's instructions whenever the skill loads, under a heading saying it takes
precedence — so a skill you loaded normally arrives with its overlay already in
it. The exception is a skill that arrives as a *companion* under another one
(the extra bodies below a `---` in a `skills show`): those carry no overlay, so
fetch it with `skills overlay <name>` if you are about to rely on it.

```bash
istota-skill skills overlays          # what is customized, and does each one load
istota-skill skills overlay notes     # print one overlay
```

These two verbs read; nothing here writes. An overlay is the user's own
document — often prose and code blocks, not a bullet list — so when you are
asked to change one, edit the file directly with your file tools, the same as
any other file in their workspace.

After editing one, run `istota-skill skills overlays` and check that its
`binds` is `true`. That is the only thing that decides whether the file reaches
a prompt, and a file can look right and load into nothing: a misspelled skill
name, a skill that is switched off, a file holding nothing but frontmatter, or
one over the 32 KB cap. The `reason` field says which.

Two skills accept no overlay at all: `sensitive_actions` and `untrusted_input`.
