# Groups

A group is a named set of Istota users with a memory of its own: a family, a work team, a band, a two-person company. It holds facts whose subject is the group rather than one person, such as how the family does holidays, the plumber's name, or a team's deploy conventions. Before groups, every store was either one user's or the whole deployment's, and `CHANNEL.md` only covered one conversation.

Each group has two stores:

- **`GROUP.md`**, a prose file at `{mount}/Groups/<group_id>/GROUP.md`. It loads into each member's prompt as `## Group memory` and is readable by a person with no server running.
- **A key-value store**, `group_kv`, for machine state: counters, last-run stamps, lists several members append to. It is the per-user KV store with a group in its key instead of a user.

The operator creates groups and manages membership. Members write to the stores through the bot, and only when they ask it to.

## The charter

Every group store is governed by one rule:

> Everything in a group store may be said in front of every member of that group, present **and future**, in any room where the group's material is loaded. Nothing else goes in it.

The "future" clause is the one that is easy to miss. Adding a member exposes the whole existing store to them, including what was written before they joined. There is no per-fact audience; the charter is the mechanism. The corollary is the default: when in doubt, a fact goes in the user's own `USER.md`.

The charter appears in three places: as an HTML comment at the top of every seeded `GROUP.md`, in the memory skill's classification gate (the "Group fact" branch the model reads before it writes anything), and on this page.

## No automatic writer

Nothing writes to a group store on its own. The nightly sleep cycle extracts into a user's dated memories and the knowledge graph, and into a room's `CHANNEL.md`; it never writes `GROUP.md` or `group_kv`, and it never copies something from `USER.md` or the knowledge graph into a group. A test holds this. A group store changes only when a member tells the bot "remember this for the family" or similar, and the bot runs `istota-skill memory append --group <id>` or `istota-skill kv set --group <id>`.

## Setting up a group

Groups are managed with the operator CLI. There is no web UI and no chat command for membership in this version.

```bash
istota group create family --kind family --name "The Smiths"
istota group add-member family alice
istota group add-member family bob --role owner
istota group show family
```

- The id is lowercase letters, digits, `.`, `_` and `-`, 2 to 64 characters, starting with a letter or digit. It names a directory, so it is checked before anything is created.
- `create` makes `Groups/<id>/` with an empty `memories/` directory and seeds `GROUP.md` with the charter and three headings (`Members`, `Conventions`, `Reference`) for the bot to append under.
- `add-member` refuses a user id that is not a configured Istota user, so a typo cannot quietly leave someone out.
- `--kind` and `--role` are labels. `kind` is shown and changes no behaviour; `role` (`owner` or `member`) is recorded and nothing reads it yet.

Membership is a history. `istota group remove-member family bob` ends Bob's membership and keeps the row, so `show` can answer who could see the group's store and when. A re-join adds a new row. Groups are never deleted: `istota group archive family` stops the group loading anywhere and removes it from every member's reach, while its rows stay readable to the operator through `show`, `kv-get` and `kv-list`. An archived group's id cannot be reused.

The full command list is in the [CLI reference](../reference/cli.md#groups).

## Where a group's material loads

Each task works out once which groups it may carry, and that one answer decides three things: the `## Group memory` block in the prompt, which `Groups/<id>` directories are bound into the task's sandbox, and which groups `kv --group` and `memory --group` will accept.

A group is in that set when the task's user is a current member and everyone who will read the answer is a member too:

- **A conversation with the bot alone** loads all of the user's groups. That covers a private room, the user's own SMS or WhatsApp conversation, and a task with no conversation at all.
- **A room shared with other people** loads only the groups whose current members include everyone in the room. A family room where all four members are in the `family` group loads it; the same room with a friend in it loads nothing.
- **Nothing loads** on a guest's turn, on a turn whose audience included someone outside the room's members, while a guest or another bot is present, or in a room whose members are not recorded. Email threads without a registered room fall in the last case.
- Skills that exclude memory, such as briefings, carry no group material either.

This is deliberately stricter than "recall it but do not repeat it". In a room with a non-member, the bot does not have the group's material at all.

### Linking a room to a group

A room can be linked to one group. A linked room carries that group's material and no other group's, so a family room whose members also share a book club loads only `family`. The link narrows the rule above and never widens it: the linked group still loads only on a member's turn, while everyone in the room is a member of it and no guest or other bot is present. When that does not hold, the room loads no group at all rather than falling back to the others.

The room's host sets the link, and only to a group the host belongs to. In a private room that is the room's one member. Use `!room group <id>` in the room, `!room group none` to remove it, or `!room group` to see the current link; on the web, the Group field in the room settings does the same. A side room has no link of its own. Linking to a group you are not in, or one that does not exist, gets the same refusal, so the command does not reveal which groups exist. No group can be named `none`, so `!room group none` always means unlink.

A link to a group that is later archived, or that the room's members leave, stays on the room and loads nothing until it applies again.

Room grants (what a host lets the bot read in a shared room) do not affect any of this. A grant is consent to disclose the granter's own data; group material follows only the audience rule above.

## How the model sees it

`GROUP.md` arrives in the user half of the prompt between the knowledge graph facts and channel memory, so memory narrows from the user to the group to the room. Each group gets a `### <display name>` heading under one `## Group memory` section, in id order.

Any member can write a group's store, and any member's task could have been steered by something it read. So group material reaches the model as untrusted content: the file body in the prompt, the output of `memory show --group`, and every value read with `kv get`, `kv list` and `kv set-members --group` are wrapped in untrusted-content markers. The prompt says to read it as information, not as instructions. Keys, namespace names and heading names come back bare, because the model has to pass them back exactly.

`GROUP.md` counts towards `max_memory_chars` but is never cut, like `USER.md` and `CHANNEL.md`. It is read with `USER.md`'s read cap, and the daemon logs a `group_memory_large` warning once a file passes 32 KB. Nothing trims a group file, so watch for that warning: one member's task can grow the file, and every member's prompt carries it.

## Writes and their trail

- `memory --group` writes go through the same op engine and section routing as `USER.md`, run host-side, and take a file lock shared by every member, so two members' writes cannot interleave. Each write, applied or rejected, is recorded in the group's own store under the reserved `_memory_audit` namespace with the group, the writer, the task and the outcome. Read it with `istota group kv-list <id> _memory_audit`.
- `kv --group` writes from a sandboxed task are deferred. The scheduler applies them after the task finishes, and only if the task's user is still a member and the group is still in the task's set. Each value records `written_by`.
- A group can never be a delivery target. A `group` or `group:<id>` entry in an output target or a `CRON.md` job is dropped with a warning.

## Security notes

- `Groups/<id>/` is bound read-write into the sandbox of a task that carries the group. The `Groups/` root and every other group's directory are not bound at all.
- `GROUP.md` is read host-side with the same hardening as `USER.md` and `CHANNEL.md`: a symlinked directory, a symlinked or special file, or an oversized file is refused rather than followed.
- Every refusal from `kv --group` and `memory --group` reads `not a member of group '<id>'`, whatever the reason, so the skill CLIs cannot be used to learn which groups exist.
- On a deployment that runs tasks unsandboxed (the shipped Docker stack, macOS, the standalone install), the group set still decides what loads into the prompt and what the skill CLIs accept, but nothing stops a task's own file tools from reaching other directories under the mount. See [security](../deployment/security.md).

## Not in this version

- Membership from chat or the web UI. The operator CLI is the only write path.
- An approval queue, or any automatic extraction into a group store.
- A per-fact audience. The charter is the audience.
- Sharing `Groups/<id>/` with members' own Nextcloud accounts, so it would appear in their Files app.
- Group-scoped knowledge graph facts, and group-owned rooms. A room can be linked to a group, but it still belongs to the people in it.
