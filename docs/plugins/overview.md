# Skill plugins

A skill plugin is a skill that ships in its own Python package instead of in this repository. You build it as a wheel, the operator copies the wheel onto the server, and Istota loads it beside the bundled skills. Use one to give Istota a command-line tool for a service it does not support, or for an internal tool you do not want in the public tree.

This page explains what a plugin is and how it differs from the other ways to change a skill. The other pages in this section cover the rest:

- [Installing plugins](installing.md): putting a wheel on a server, giving it credentials, and checking that it loaded.
- [Writing a plugin](writing.md): a worked example from an empty directory to a wheel, then the rules the loader holds a plugin to.
- [Trust and credentials](security.md): what a plugin can reach once it is installed, and what keeps its secrets away from the model.

## Skills and plugins

A skill is a `skill.md` file plus, optionally, a command-line program. The `skill.md` frontmatter says when the skill is offered and what environment its program needs. The body is documentation the model reads before it uses the program. See [Skills](../features/skills.md) for how skills are selected for a task.

A plugin is a skill. The difference is where it comes from and who maintains it. Once loaded, a plugin goes through the same selection, menu, `disabled_skills`, `admin_only`, `requires_capability`, companion skills, per-skill credential injection, per-user overlays and `skills show` as a bundled skill. Where a plugin is treated differently, the rule is stricter.

There are four ways to change what a skill does or says. Only a bundled skill and a plugin add code.

| | Where it lives | Who writes it | What it can do |
|---|---|---|---|
| Bundled skill | `src/istota/skills/<name>/` in this repository | Istota's developers | anything a skill can do; ships with every release |
| Skill plugin | its own wheel, in `/srv/app/istota/plugins/src/` on the server | anyone; the operator installs it | add a new skill with its own command-line program, under a restricted manifest; never replace a bundled skill |
| Operator override | `config/skills/<name>/skill.md` | the operator | replace the documentation and metadata of a bundled skill or a plugin; no code |
| Per-user overlay | `/Users/<user>/<bot>/config/skills/<name>.md` | each user | append instructions to one skill's body; no code |

Overrides are described in [Skills](../features/skills.md#discovery-layers), overlays in [Per-user configuration](../configuration/per-user.md#per-skill-overlays).

## Plugin or bundled skill

Write a plugin when:

- the skill needs code, and
- it does not belong in the public repository: it talks to a private service, it is specific to one deployment, or you want to release it on your own schedule.

Write a bundled skill instead ([Adding skills](../development/adding-skills.md)) when the skill is generally useful, or when it needs anything a plugin is not allowed:

- **A Python dependency the image does not already have.** A plugin is installed with `--no-deps` and runs on the image's own virtual environment. A library it needs has to become an optional extra of Istota first.
- **Compiled code.** A plugin is a pure-Python wheel.
- **Being always loaded, removing other skills, or claiming to be safe for a guest in a shared room.** The manifest fields for these are refused ([What the manifest may say](writing.md#what-the-manifest-may-say)).
- **Configuration values, vault reads over the private channel, or credential-name arguments.** A plugin reads its own secrets and nothing from `config.toml` ([Trust and credentials](security.md)).

If you only want to change how the model uses an existing skill, write an override or an overlay. A plugin cannot replace a bundled skill, and one with the same name is refused.

## How a plugin reaches a task

1. The operator copies the plugin's wheel into `/srv/app/istota/plugins/src/` and runs `istota-stack plugins sync`.
2. The sync installs the wheel into a tree beside the image, mounted read-only at `/app/plugins` in the daemon and web containers, and restarts them.
3. At start, each process reads the skill index. The loader finds the plugin through the `istota.skills` entry point in the wheel's metadata, reads its `skill.md` and checks it. A plugin that breaks a rule is left out, and `istota doctor` names the reason.
4. A loaded plugin appears in the task's skill menu like a bundled skill. The model reads its body with `istota-skill skills show <name>` when it decides the skill is relevant.
5. The model runs `istota-skill <name> <verb> …`. The skill proxy runs `python -m <package>` outside the task's sandbox, with the plugin's own credentials in its environment, and hands the JSON output back to the model.

The plugin's code is never inside a task's sandbox, and the model never holds the plugin's sensitive variables.

## Limits in this version

- **The Mac app is not supported.** Its VM runs the same stack, but nothing tests a plugin there and the app has no way to add one.
- **A plugin is always `egress: external`.** A limited scheduled job needs its owner to approve `skill:<name>` before it can run one ([Scheduling](../features/scheduling.md)).
- **A plugin is always `shared_room: private`.** A guest's turn in a shared room never reaches it.
- **No dependencies and no compiled code**, as above.
