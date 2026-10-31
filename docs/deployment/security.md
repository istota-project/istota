# Security

Istota isolates the model's tools through layered security: clean environment, filesystem sandbox, credential proxy, network isolation, and the credential broker.

## Supported deployment

There is one deployment: the istota stack on a Debian 13 machine or VM dedicated to it, installed by `install.sh` and managed with `istota-stack`, with every task in a [bubblewrap](https://github.com/containers/bubblewrap) sandbox. The filesystem sandbox is the boundary between users and between Claude and the host — without it, env-var scoping in the prompt is the only thing keeping one user's tasks from reading another user's data, and that boundary depends on the model following instructions.

Inside the machine the stack's services are containers, and a container cannot build a bubblewrap namespace under Docker's defaults, and `CAP_SYS_ADMIN` is not what it is missing: the default seccomp profile blocks `unshare`, and `docker-default`'s AppArmor profile denies `mount`. The stack's `daemon` service carries a narrow grant instead: a seccomp profile that is Docker's default plus the six calls bwrap needs, an AppArmor profile that is `docker-default` plus `mount` and `pivot_root`, `systempaths=unconfined`, and no capabilities once a short root phase is over. That grant is acceptable only on a host that runs nothing else, which is why the machine is dedicated. [Running tasks sandboxed](running-the-stack.md#running-tasks-sandboxed) has the details. On Linux, a daemon whose sandbox cannot be built refuses to start rather than run tasks unconfined.

macOS has no bubblewrap. On a Mac the stack runs in the [local VM](../getting-started/local-install.md), a Lima VM with the same sandbox as a server; [The local VM](#the-local-vm) below covers what its boundary does and does not protect. `istota serve` on a Mac checkout runs tasks unsandboxed; it is a development configuration, not a deployment. The scheduler logs a `SECURITY UNSUPPORTED CONFIGURATION` warning at startup when it runs that way with more than one user configured.

If you run without the sandbox, you accept that:

- A prompt injection in one user's task may exfiltrate any other user's data on the same host.
- Claude has access to the full filesystem visible to the istota service account, not just the per-user subtree.
- The credential proxy and network proxy still run, but their effectiveness drops without the sandbox boundary (Claude can read arbitrary files, including ones holding the credentials the proxy exists to hide).

## Release signing

Every release is a git tag signed with the project's SSH release key. The public key is in the repository at `host/release-signing-key.pub`:

```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIM8wnX7YSjgMETMV8EOf/hQ6J5HpZsw9WfY/r0Glfq4z istota-releases
```

Its fingerprint is `SHA256:msQ02X7UFfotEHfGWtElonM/WEQxCXCMBdLhBAjsX88`. The installer, `host/cloud-init.yaml` and the Mac's `istota-stack create` use this key unless you give another with `--signing-key`. The first install writes it to `/srv/app/istota/host.env`, and `provision.sh` writes it to `/srv/app/istota/allowed_signers`. From then on that file decides which tags the machine accepts. `install.sh --server` run later with a different key is refused rather than swapped in; to change the key, edit `RELEASE_SIGNING_KEY` in `host.env` and run `provision.sh`.

`install.sh` and `istota-stack update` check the tag with `git verify-tag` against `allowed_signers` alone, with git's global and system configuration not read and OpenPGP and X.509 verification switched off. They refuse a branch, a lightweight tag, an unsigned tag, a tag signed by another key and a tag ref pointing at another release's tag object, before anything from the checkout runs. With no tag named, both pick the newest `vX.Y.Z` tag and check it the same way; when it fails, they stop and never fall back to an older tag. `istota-stack update --unsigned` is the only way past the check, and only for the run that asks for it.

The installer itself comes from `main` and is not covered by a signature. It carries the key, so a copy fetched from somewhere other than this repository can name a different one. To check the key a machine trusts out of band, compare its fingerprint with the one above:

```bash
awk '{print $3, $4}' /srv/app/istota/allowed_signers | ssh-keygen -lf -
```

## Clean environment

Every task starts from a minimal environment built by `build_clean_env()`: only PATH, HOME, PYTHONUNBUFFERED, and configured passthrough vars (`LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`). Task-specific variables (Nextcloud credentials, CalDAV, email, etc.) are added per-task. No task env carries the `claude` CLI's credential; see [The model's tools and the CLI's credential](#the-models-tools-and-the-clis-credential).

Heartbeat and cron shell commands run in the task sandbox with the same environment split as a task, since the bot can edit both files.

## Filesystem sandbox (bubblewrap)

When `sandbox_enabled = true` (default), each task's tools run inside a `bwrap` mount namespace with PID isolation: one tool server per task attempt, which runs every Read, Write, Edit, Grep, Glob, Bash, BashOutput and KillShell call on either brain. A background job started with Bash is in the same namespace and cgroup, and it ends with the attempt.

**Every task sees**:

- System libraries (read-only)
- Python venv + source (read-only)
- Their own workspace subtree, `Users/<id>/` (read-write)
- Active channel directory (read-write)
- Their temp directory (read-write)
- Extra resource paths

**Hidden from everyone, admin included**: every SQLite database the daemon owns. The framework DB directory and the per-user module-DB root (`module_data_dir`, holding each user's `health.db` / `money.db` / `location.db` / `feeds.db`) are covered by an empty tmpfs, applied as the last mount operations so no earlier bind shows through. Local DB backups and the browser profile live under the same directory and go with them.

Each mask is then remounted read-only (`--remount-ro`, where bwrap supports it — 0.2+). A writable mask makes the dead end lie: `sqlite3 {db_dir}/istota.db "select …"` creates the file on the tmpfs and answers `no such table`, which reads as a missing schema rather than as "this file is not in the namespace", and leaves a zero-byte `istota.db` behind for the rest of the task. Read-only, the same command fails at open. Nothing a task writes under a database directory can survive to be mistaken for a database.

A mask hides rather than revokes: `kernel.unprivileged_userns_clone` is on (bwrap needs it), so a sandboxed process can enter a nested user namespace and unmount a tmpfs to reveal whatever was bound underneath. `--disable-userns` is passed where bwrap supports it (0.8+), together with the `--unshare-user` it requires — without that companion flag bwrap refuses the argv, which is why the support check answered "unsupported" on every host and the flag reached no sandbox at all until this was found. In the shipped default nothing is bound underneath, so there was nothing to reveal either way. Both of those are reasons to keep `sandbox_ro_paths` narrow rather than to rely on the mask to make a broad entry safe.

**Also hidden from everyone**: other users' directories and the config directory.

The config directory is not bound — it holds `config.toml`. `emissaries.md`, `persona.md`, `guidelines/*.md` and the skill bodies reach the model as content the daemon read and put in the prompt, so they never needed to be there. `system-prompt.md` is no exception: under `custom_system_prompt = true` the native brain reads it in the daemon, and the `claude` CLI, which opens the path itself, runs outside the sandbox.

**Admin users additionally see**: their own developer repositories, when the developer skill is enabled. The workspace bind is the same for an admin as for anyone else: only their own `Users/<id>/` subtree, never the whole workspace or another admin's repositories.

The masks are unconditional rather than a matter of not binding the files, because not binding them was the previous design and it did not hold. `module_data_dir` defaults under the framework DB's directory, the reference deployment puts that under `istota_home`, and `sandbox_ro_paths` defaulted to the `/srv/app` containing it — so one RO bind that named no database exposed all of them, to every task. `sandbox_ro_paths` now defaults to `[]` and is honoured from config (it was previously never parsed), but the masks are what makes the property hold regardless.

Reads and writes reach the databases only through skill CLIs, which the credential proxy runs **outside** the sandbox and which scope their queries by `ISTOTA_USER_ID`. That scoping, not the filesystem, is the per-user boundary; the sandbox is defence in depth behind it.

Linux-only and merged-usr compatible for Debian 13+. See [Supported deployment](#supported-deployment) above for the policy on non-Linux / no-bwrap configurations.

### Credential files and the live store

Credentials live in the encrypted framework database, which no task sandbox binds. Import reads an uploaded KeePass file in memory and stores only selected entries. Export builds a new file in memory; the user's own KeePass file is never rewritten. A one-time retirement reader handles old configured files through the existing no-symlink path resolver. The stored legacy passphrase is deleted when retirement completes.

A web export, recovery reveal, backup recipient change, history restore or purge needs a fresh emailed code bound to the session and action. The code goes to the native sign-in identity, never a profile address a session holder can edit. On an install with no mail set up, the confirmation is the account password instead, bound the same way and counted by the sign-in throttle. Every export is announced by email (where mail is set up) and in the bell. A stolen session and mailbox together remain outside that protection, as does a stolen session and password where there is no mail.

### Back up the secret key separately

`ISTOTA_SECRET_KEY` opens the encrypted database. Losing it loses every stored credential unless you have an independent export. Keep a copy outside the storage and backups that hold the database.

The stack keeps it at `/data/.secret_key`, which is `/srv/app/istota/data/.secret_key` on the machine, unless a secret file supplies the key. The local VM copies it into the Mac's login Keychain, apart from the snapshots in the workspace folder (see [The local VM](#the-local-vm)). Database snapshots under `Backups/db/snapshots` do not include the key. A backup of the whole state directory (`/srv/app/istota/data`) contains both the database and its key, so it does not provide that separation.

An age credential backup is an independent recovery path: decrypt it with the private age key you keep elsewhere, then open its KeePass file with the enclosed password. It needs no server secret key. `security.key_separation` checks file locations, and reports when scheduled credential backups are off.

### The `.developer` carve-out

Each task's scratch space holds a `.developer` directory, written by the `developer` skill's `setup_env` hook. It holds two kinds of thing, and both need the same protection.

The **credential plumbing**: the git credential helper and the `gh` / `glab` wrappers in `.developer`, and the `istota-credential` shim in `.istota` beside it. A task that could replace one of them could intercept a forge token on its next use, so both directories are re-bound read-only.

The **policy the wrappers enforce**, which is the half that is easy to overlook: `forge-policy.json` — which carries the deny rules, the real binary path and the forge URL, precisely so the wrapper reads none of them from an environment the model's own shell can set — plus the seeded per-forge config dirs and the pinned-empty data dirs. Those last two are not incidental. `gh` expands aliases from `config.yml` *before* command dispatch, so a writable config dir is a complete bypass of the deny list; and it dispatches an unknown first argument to `gh-<name>` under the data dir, which no argv rule can see. A writable `.developer` would leave the deny list decorative rather than merely leaking a token.

`build_bwrap_cmd` therefore re-binds `.developer` read-only *after* the read-write bind of its parent, so the sandboxed path has always been covered. The in-process agent loop (the native brain) runs without bwrap, and its confinement is a list of writable roots — pure containment, which cannot express a hole inside a root. Every path under the task's temp directory, `.developer` included, was writable there.

`ToolEnv` now takes `write_denied_roots`, checked before the allow loop and on the write path only. Reads still pass, matching what the read-only bind gives the other brain. `native_fs_roots()` returns the carve-outs as a third element rather than leaving them to a second call a future caller could forget, and the executor passes them through as `BrainRequest.fs_write_denied_roots`.

The deny root is appended unconditionally rather than only when the directory exists. bwrap re-checks on every Bash call and self-heals; this list is built once, so an existence gate would hand a task that started before `.developer` existed an empty deny set for its whole life — and `Write` creates parent directories, so the model could then make the directory itself. A refused write reports read-only rather than "outside the allowed workspace", which is the one thing it is not.

### The task control directory

The same mechanism as `.developer`, applied to a directory of the daemon's own. Istota's standing instructions — identity, persona, tool descriptions, rules, response guidelines, skill bodies — reach the model as a *system* prompt rather than as its first message, so a native compaction cannot summarize them away. The handoff is a file: the executor writes `system_prompt.txt` into the task's control directory and names it on the brain request, which each backend then reads or passes to the CLI.

That directory is `{temp_dir}/.control/{user_id}/task_<id>/`, and it holds every per-task file the daemon authors: both prompt halves, the briefing block metadata and the prepared image renditions. It is a *sibling* of the task's scratch directory rather than a child of it, created `0700` at all three levels and owned by the daemon, so no task can write into it and nothing model-writable is an ancestor of it. The two guards are the ones above, applied to the directory: `build_bwrap_cmd` binds it read-only after every other bind, and `ToolEnv` carries it as a second `write_denied_roots` entry — plus a read root under confinement, since the directory is inside no write root and the task still has to open its own prepared attachment. Both are needed and neither substitutes for the other: the bind covers the sandboxed `Bash` tool, and the file tools reach `ToolEnv` without entering a namespace at all. Unlike `.developer`, the deny entry is seeded whether or not filesystem confinement is active, because the shapes that skip bwrap (`istota serve` on macOS) are exactly the ones with no bind behind it.

Only the task's own directory is bound, never the per-user level above it, so one task reaches no other task's instructions and no other task's assembled request. The flat layout this replaced allowed both: the write for as long as two of a user's tasks overlapped, the read for the length of the temp-file retention window.

Two limits are worth stating plainly. `security.sandbox_ro_paths` is bound verbatim, so an entry at or above `temp_dir` would expose the whole tree; the config loader warns on one, and `istota doctor`'s `runtime.task_control_dir` reports the same overlap for the other binds a config can produce, including a `user_resources` row — which is bounded by the workspace root and nothing else. And the deferred-op files still sit in the writable scratch directory with `O_NOFOLLOW` as their only guard against a symlink planted at a not-yet-started task's filename. Those are model-authored by design, and the scheduler's own validation of what they contain is the boundary there.

### Shared rooms

A member's turn in a room more than one person reads runs with that member's full reach, as it does in their private room, apart from their personal memory, which is not loaded into the prompt (see [shared rooms](../features/shared-rooms.md#what-a-task-can-reach)). Two kinds of task there run with nothing of the user's: a guest's turn, which runs as the room's host, and a task nobody asked in the room, such as a scheduled job whose conversation is a shared room. Mail somebody else wrote is close to that: it runs as the user with the workspace withheld and a short allowlist, and `USER.md` and skill overlays load (see [what someone else's mail can reach](../features/email.md#what-someone-elses-mail-can-reach)). On a sandboxed deployment that is a property of the namespace rather than of the prompt: the user's workspace is not bound at all, and neither are per-resource mounts or the developer repos. Such a task gets a temp directory of its own (`emissary-task-<id>` for a guest's turn, `room-task-<id>` otherwise) instead of the per-user one that persists across the user's tasks in every room, and like every task, no bind of the shared Talk attachments directory: its own attachments are copied into the read-only control directory. Host-side skill CLIs apply the same rule to every path argument, and the Nextcloud skill applies it to its WebDAV verbs and refuses to post into a Talk conversation other people read.

Other participants' messages in a shared room's history reach the model inside an untrusted-content fence, since a co-member's message could otherwise try to steer a turn that runs at full reach. Confirmations still gate side effects.

**Without the bubblewrap sandbox none of the filesystem half applies.** Under `istota serve` on macOS, what a guest's turn loses is removed from the prompt, the skill list, the proxy's allowlist and the environment, but the task's own tools can still read and write the host's files on disk, including `CRON.md`, whose jobs run as the host. `istota doctor` reports a warning (`security.room_scope_confinement`) on that shape. If guests should not be able to reach a host's files, run [the stack](../getting-started/quickstart-server.md), where the sandbox applies.

## Credential proxy

When `skill_proxy_enabled = true` (default), secret env vars are stripped from Claude's environment and routed through a Unix socket proxy instead. See [credentials](../configuration/credentials.md) for the full inventory of which credentials are global vs per-user and how they're provisioned.

The set of stripped variables is **manifest-derived**: `derive_credential_set(skill_index)` collects every env var declared with `sensitive: true` across all loaded skill manifests. Today's set:

- `CALDAV_PASSWORD`, `NC_PASS`, `ISTOTA_NEXTCLOUD_APP_PASSWORD`, `SMTP_PASSWORD`, `IMAP_PASSWORD`
- `KARAKEEP_API_KEY`
- `GITLAB_TOKEN`, `GITHUB_TOKEN`, `MONARCH_SESSION_ID`, `MONARCH_CSRFTOKEN`, `GOOGLE_WORKSPACE_CLI_TOKEN`
- `NTFY_TOKEN`, `NTFY_PASSWORD`, `TUMBLR_API_KEY`

`ISTOTA_SECRET_KEY` (the master Fernet key) is **not** in the manifest-derived set. It is the proxy's hard-reject lookup var (`_PROXY_LOOKUP_BLOCKED`) and never enters any subprocess env.

Adding a sensitive credential to a skill's `env:` block is the only step needed to route it through the proxy; there is no longer a hand-maintained `_PROXY_CREDENTIAL_VARS` list to keep in sync.

Skill CLI commands run through the proxy (`sandbox/skill_proxy.py`) in the executor thread. The proxy injects credentials server-side, scoped per skill: `derive_skill_credential_map(authorized, skill_index)` returns the per-skill credential map, so a CLI invocation only ever sees credentials its own manifest declared. The `istota-skill` client connects to the socket or falls back to direct execution when the proxy is disabled.

Proxied skills run in the task's cgroup, so `task_memory_max_mb`, `task_pids_max` and `task_cpu_max_percent` now cover them along with the task's own tools; a task that relies on a heavy skill may need a higher memory limit. Each proxy also limits its own work in the daemon: request size and arrival time, open connections, concurrent skills and skill output. When a task ends, its proxies close their connections and kill any skill still running. The settings are in the [configuration reference](../configuration/reference.md).

The proxy's Unix socket path includes the host process PID — `istota-proxy-{pid}-{task_id}.sock` (and the same shape for the network proxy). This prevents collisions when multiple processes (xdist test workers, parallel `istota run` instances, the daemon plus a manual scheduler) pick the same `task.id` from independent SQLite databases.

### Who may connect

The socket is mode 0600, which keeps out other OS users. It keeps out no other task, because every task runs as the daemon's uid. Under bubblewrap only a task's own socket is bound into its namespace, but without the sandbox (macOS) all tasks share one uid and one `/tmp`, and the path above is predictable.

So the proxy asks the kernel which process connected (`SO_PEERCRED` on Linux, `LOCAL_PEERPID` on macOS) and serves it only if it descends from a process its own task registered: the brain's child, reported when it is spawned, or a skill CLI the proxy itself is running. Each registered process is pinned to its start time, so a pid the kernel later hands to another process carries nothing over. Anything else gets `reason: peer_not_in_task` and a `proxy_rejected ... reason=peer` warning in the log, before the request is parsed. A process whose parent exited and left it reparented to init (a backgrounded command that outlived its shell) is refused too.

What this does not close: on an unsandboxed multi-user shape one task can still rewrite a file another task executes (its credential shim, a git hook, the CLI's shell snapshot) and have its own code run inside that task's tree, and with `kernel.yama.ptrace_scope = 0` it can attach and read memory as well. `istota doctor --only security.proxy_peer_check` reports that shape as a `WARN`, and reports a `FAIL` on a platform where no peer can be read, since the proxy then refuses every connection.

### Authorization model

Credential authorization is **decoupled from skill selection**. A skill is authorized for credential access if any of its sensitive `EnvSpec`s actually resolves under the task's context — that is, if the user has the corresponding resource configured (Karakeep, etc.) or the relevant instance config is set (SMTP, GitLab/GitHub tokens). Skill selection controls only which skill *docs* go into the prompt, not which credentials can be requested at runtime.

This avoids the failure mode where a keyword miss locks a skill out: e.g. a user has a Karakeep resource configured, the prompt didn't say "bookmark", `bookmarks` wasn't selected — under the old model the proxy would refuse to inject `KARAKEEP_API_KEY` and the CLI invocation would fail mysteriously. Under the new model the credential is injectable as soon as Claude decides it needs the bookmarks skill, regardless of selection.

Doc-only skills (no CLI module) are eligible too: the `developer` skill consumes `GITLAB_TOKEN`/`GITHUB_TOKEN` via `istota-credential env` from the git credential helper and the `gh` / `glab` wrappers its `setup_env` hook writes into the task's `.developer` directory. Gating authorization on `cli=true` (the prior heuristic) would lock it out.

Auto-authorization uses `_resolve_env_spec(spec, ctx, fallbacks_disabled=True)` so an instance-wide `EnvironmentFile` fallback for an operator-set value cannot fan out and auto-authorize every user — preserving the per-user privacy posture.

`derive_lookup_allowlist(authorized, skill_index)` is the union the proxy will respond to over `istota-credential env`, with `_PROXY_LOOKUP_BLOCKED = {"ISTOTA_SECRET_KEY"}` subtracted as a defense-in-depth hard reject. The master Fernet key flows into specific module-skill subprocess envs (so they can decrypt per-user secrets in-process) but is never returned over the lookup channel — `istota-credential env ISTOTA_SECRET_KEY` from inside Claude is rejected.

Threat model for manifest credentials: a compromised Claude can only request values that already exist for this user (resources are user-scoped, instance config is operator-controlled). The vault's separate create path is described above.

### Rejection observability

Every proxy rejection emits a structured WARNING log:

```
proxy_rejected task_id=42 type=skill skill=evil_skill reason=unknown_skill
proxy_rejected task_id=42 type=credential name=NC_PASS reason=not_authorized
```

Reason codes: `unknown_skill` (skill name not in the CLI whitelist), `not_authorized_credential` (credential not in this task's allowed set), `credential_not_present` (credential genuinely missing from env).

Rejection responses include the structured `reason` field and, for unknown skills, an `authorized_skills` list — surfaced to the model via the client's stderr so it can adapt rather than retry blindly.

Use these logs together with the selection logs (`pass1_selection`, `disclosure:`; see [skills](../features/skills.md#selection-observability)) to count selection misses and decide whether a skill's keywords or disclosure mode need tuning.

## Admin-gated job types

Two scheduled-job types can run arbitrary shell, so they're gated to admin users:

- **`command:` rows in CRON.md** — `cron_loader.sync_cron_jobs_to_db` drops command-type rows for non-admin authors at sync time and orphan-deletes any DB row left over from a prior admin sync. `_execute_command_task` refuses non-admin tasks at runtime as defense in depth. Auto-seeded `_module.*` rows are scheduler-inserted, not user-authored, so they're unaffected.
- **`type: shell-command` heartbeat checks** — `heartbeat.run_check` refuses these for non-admin users.

CRON.md `command:` rows of the shape `istota-skill <name> [args]` (no shell metacharacters) auto-promote to skill-tasks at sync time and dispatch through `_execute_skill_task` instead, which is not admin-gated — operators can give non-admin users access to specific skills without granting full shell.

## The development container

A deployment may run project code — `npm`, `uv`, `cargo`, a test suite — inside the user's devbox rather than on the host. That is derived rather than switched on directly: it happens where `[developer] enabled`, `[developer] repos_dir` and `[devbox] enabled` are all set. Off by default, and where it is off none of this exists.

**Nothing here creates a container.** The devbox is per user and standing, started by its compose service (rendered by `istota devbox compose-file`). The daemon starts none, and a task cannot ask for one. `istota-skill devbox reset` wipes `/home/dev` and restarts the user's own container by a request on the exec transport; nothing in istota speaks Docker.

**The Docker API has left the sandbox, and its proxy is deleted.** Until this change `build_bwrap_cmd` bound a per-user Docker-API allowlist proxy at `/var/run/docker.sock` in **every** task's sandbox — including tasks built from an email, a feed or a fetched page. The allowlist was narrow and held: it refused create, run, build, privileged and host-mount, so a task reaching the socket directly with `curl --unix-socket` could not escalate. It still permitted exec, cp, inspect and restart on that user's container from any task at all. Nothing in a build needs it now, so the bind is gone, `src/istota/docker_proxy.py` and its systemd units are deleted, and a deploy stops and removes any that are still running. The `docker` CLI is no longer bound in either — though `/usr` is, so a `docker` binary may still resolve on a host that installs one. The guarantee is the socket: none is bound at any path and no `DOCKER_HOST` is exported, so any `docker` a task finds fails at connect.

**What replaces it is gated, and that is the difference that matters.** Commands reach the container over a Unix socket into a server running inside it. That socket's directory is bound into a task's sandbox only when `"developer" in authorized_skills` — byte for byte the predicate that already decides whether the task's CONNECT allowlist gets the package registries and the forge. An allowlist is safe to hand every task; an unauthenticated arbitrary-command channel into a container with permissive egress is not, because it would be a route around the per-task, skill-scoped network allowlist described below.

**What does not improve, stated plainly.** What the model may do *inside* the devbox is not narrowed by any of this. `dev` has passwordless sudo, so a task with access was already root in that container, and the transport widens the degree: no output cap, no timeout of its own, stdin, and a real exit status. One axis narrows — a working directory the caller names must resolve under that user's repos root, where `docker exec -w` took any path in the container.

**Forge credentials stay out of a devbox by default.** The credential socket in a devbox returns the raw forge token, and any process in the box can ask it. So it has no token to give unless `[devbox] forge_users` names the user. The tokens are the operator's and shared by every user, so for a listed user the token's own scope is the only limit on what code in that box can do with it. A task's own `git`, `gh` and `glab` run in the sandbox and do not use this socket.

**Every containment decision is made inside the container**, by the server, at the moment of use. A file path is resolved with `realpath` and refused unless the result is under an explicit root list: that user's repos root, `/home/dev`, and the staging directory. A caller-named working directory is narrower still, and must resolve under the repos root. The credential and transport socket directories are refused by their literal default paths as well, `/run/istota-cred` and `/run/istota-exec` — a deployment that repoints either keeps the root-list refusal and loses the named one. This replaces four daemon-side mechanisms that each encoded a guess about what the container's mount table looked like, which is what the two file-copy defects before it were. A process inside the container sees what the container sees.

**Everything is per user.** `[developer] repos_dir` is a per-user root whether builds run in the container or on the host: the daemon derives `{repos_dir}/{user_id}`, the container binds only that subdirectory, and the socket directory is per user too — only the per-user subdirectory is ever mounted, since the parent holds every user's socket and mounting it would be arbitrary command execution against another user's repositories. The daemon and the container also run as the same numeric uid, which is what makes a file either side writes readable, editable and removable by the other.

**The socket directory is one a container root can write to, and that is bounded rather than denied.** `dev` has sudo, so it can create files there, change their modes, and unlink the server's own inode to bind something else in its place. Three things bound what that buys. The daemon connects to exactly one path it composed itself and never opens, lists or executes anything else in that directory. The directory is per user, so the substituting party is already root for that user and reaches only that user's own tasks. And the client refuses to hang on whatever answers: it gives up on an acknowledgement that does not arrive within its own ceiling rather than holding a shim for the length of a task. The directory holds one inode, is mode 0770 owned by the daemon's uid and gid, and on the default path lives on a tmpfs.

**Egress from the container is permissive**, as it always was, bounded by the `DOCKER-USER` drops `host/provision.sh` installs (`istota-devbox-egress.service`) for link-local, cloud-metadata, RFC1918 and carrier-grade NAT ranges. The hosts a postinstall script, a `build.rs` or a git dependency wants are not nameable in advance, which is the whole point of the container; the sandbox's own CONNECT allowlist below is unchanged and still governs everything running on the host. `CAP_NET_RAW` is dropped, so `ping`, `traceroute`, `mtr` and `tcpdump` no longer work inside the container: a build needs none of them, and a container holding that capability picks its own source address and walks past every address-scoped drop rule.

**A task's cgroup no longer bounds a build.** Container processes are in the container's cgroup, not the task's, so `memory.max`, `pids.max` and `cpu.max` stop applying to anything that moved. The container's own `mem_limit`, `pids_limit` and `cpus` replace them at a coarser granularity — per user rather than per task — so one user's runaway build can take another of their own tasks down with it. Cross-user isolation is intact. An OOM-killed build comes back as exit 137 and is reported by nothing else.

### The model's tools and the CLI's credential

On the `claude_code` brain the `claude` CLI runs outside the sandbox, and none of its own tools that act on the machine are available to it. Its tools come from the same tool server the native brain uses, inside the task's namespace, through one MCP server. The CLI's credential is in the CLI's own environment and, under `auth = "subscription"`, a per-run config directory linking to the daemon's credentials file. Nothing inside the namespace holds it: not the task env, not a file the namespace binds, not a process environment a tool can read under `/proc`. The smoke tier's `tests/smoke/test_cli_credential_unreachable.py` runs those probes, and `scripts/test-image-negative-control.sh cli-witness` turns them red by running the tool calls in the CLI's process instead.

The model calls with no task behind them that grant tools (health OCR, the code reviewer and doctor's model probe) run the same way, with one difference: they have no CONNECT proxy and no `--unshare-net`, so their tool server shares the daemon's network namespace. OCR offers only `Read` and the reviewer `Read`, `Grep` and `Glob`, none of which opens a socket. Doctor's fixed prompt grants `Bash`, which can reach what the daemon can. What protects the credential on this path is that the namespace does not hold it. The same test file covers them.

Each run of the CLI gets a fresh `CLAUDE_CONFIG_DIR` with an empty `settings.json`, so the operator's own `~/.claude/settings.json` no longer applies to tasks, and the CLI's own transcripts land in that directory, removed with the rest of the harness directory by the temp-file sweep. `istota-skill tasks transcript` reads the native brain's session logs only.

This holds on the sandboxed shape. Where nothing is sandboxed, the tool server runs as the daemon user without a namespace, and a Bash call can read any process of that user, the CLI included.

## Network isolation

When `[security.network] enabled = true` (default, requires sandbox), each task's sandbox gets `--unshare-net` (own network namespace, no external connectivity). Outbound traffic goes through a CONNECT proxy on a Unix socket.

Default allowlist:

- `pypi.org:443`, `files.pythonhosted.org:443` -- package installs (when `allow_pypi = true`)

Nothing in the sandbox talks to Anthropic: the `claude` CLI runs outside it, and the code reviewer calls its model host-side. `api.anthropic.com:443` and `mcp-proxy.anthropic.com:443` are no longer on the list, and under `auth = "api_key"` the `anthropic` provider's base-URL host is not added either. A task that calls Anthropic's API from Bash needs the host in `extra_hosts`.

Additional hosts added automatically:

- Git remote hosts from `[developer]` config when the developer skill is selected
- `results-receiver.actions.githubusercontent.com:443` on github.com, where `gh run view --log-failed` fetches job logs. Measured through a logging CONNECT proxy rather than assumed: it is one stable hostname across independent uncached runs, so an exact entry covers it and the proxy needs no wildcard support
- `registry.npmjs.org:443`, `index.crates.io:443` and `static.crates.io:443` when the developer skill is *authorized*, so an install inside a worktree can resolve its dependencies. Authorized is wider than selected: `derive_authorized_skills` also admits a skill whose credentials resolve, and `developer` qualifies as soon as either forge token is configured — so on such a deployment these hosts are on the allowlist for every one of that user's tasks, not only the coding ones. That is the gate the git remote hosts above already ride, deliberately; the registries were added to it rather than to a new one. Measured the same way: a full `npm ci` of this repo's frontend made 15 CONNECTs, all to the one npm host, and `cargo fetch` used the sparse index plus the download host. `crates.io:443` is the publish and search API, was never contacted by a build, and is not allowlisted. There is no `allow_npm` flag — the registries arrive with the developer skill, which is already opt-in through `developer.enabled` and skill authorization
- Operator extras via `extra_hosts`

**The allowlist is checked against the address, not only the name.** The proxy resolves an allowed host once and connects to the address it checked, so a DNS answer that changes between the check and the connect cannot redirect the connection. A built-in host (PyPI, the package registries, GitHub's API and log host, Google's APIs) is refused if any address is private, loopback, link-local or otherwise non-public, since its DNS is somebody else's. A host you configured may be on your own network: an `extra_hosts` entry, or the host of `developer.gitlab_url`, `developer.github_url` or `nextcloud.url`. Listing it is the permission, and nothing else needs setting. The same rule covers the credential broker's upstream connections.

**A raw tunnel carries TLS for the host it was opened to.** Before connecting the client through, the proxy reads the TLS ClientHello and requires its server name to be the CONNECT host; plain bytes, another name or no name end the tunnel (a tunnel to an IP literal may send no name, as RFC 6066 has it). What the proxy cannot see is the HTTP `Host` header inside that TLS, so a shared front end that routes on it can still be asked for another tenant through an allowed name. Only the broker, which terminates TLS for credential-bound hosts, checks that header. A client using Encrypted Client Hello sends an outer name that is not the host and is refused.

**A private registry or a corporate mirror is an `extra_hosts` job.** The entries above are the public defaults, measured against the tools' own defaults; a deployment pointing npm or cargo somewhere else needs its host added, and the proxy matches `host:port` exactly with no wildcards, so a missing name fails at the boundary and reads as a broken install rather than as a refused connection. `extra_hosts` is global rather than skill-gated: it applies to every task, not only developer ones, and there is no per-skill operator list. Set it as `extra_hosts` under `[security.network]` in `config.toml`.

**Ecosystems that are not covered**, listed so the boundary is legible rather than looking like a bug. None of these is exercised by this repository, so unlike the entries above they are reasoned rather than measured. `node-gyp` compiling a native addon wants headers from `nodejs.org`; Playwright wants its own CDN for browser binaries; anything resolving a GitHub release asset wants `objects.githubusercontent.com`, which the `github.com` entry does not cover — and that last one includes `uv python install`, which fetches interpreters from release assets rather than from PyPI. Composer, Maven, NuGet, RubyGems and the Go module proxy are absent entirely. Add what a deployment actually needs through `extra_hosts`, and prefer measuring it through a logging proxy over taking a vendor's documented hostname on trust.

`gh run download` is deliberately **not** covered. Artifacts come from `productionresultssa<N>.blob.core.windows.net` with the shard varying per repository, and the only entry that would cover that is `*.blob.core.windows.net` — all of Azure Blob Storage, a general-purpose exfiltration channel reachable from the sandbox. The CI feedback loop needs logs, not artifacts.

The forge wrapper sets `GH_TELEMETRY=0` and `DO_NOT_TRACK=1`, so no telemetry host needs allowlisting and no command spends a rejected CONNECT on one. GitHub Enterprise Server needs no extra entry: its API is a path on the same host (`<host>/api/v3`), already added as the git remote.

TLS is end-to-end except for hosts bound to a credential in the task's grant snapshot, which the credential broker intercepts (below). Every other connection is a raw tunnel.

**The browser container has a firewall outside it.** Chrome's proxy flags hold only while Chrome honours them, and it runs with `--no-sandbox`, so a compromised renderer could open its own sockets. On a VM, `DOCKER-USER` rules drop the browser's traffic to link-local and cloud metadata addresses, RFC 1918 and carrier-grade NAT ranges, and leave public addresses open. The exceptions are the addresses that `[browser] private_hosts` resolves to on the VM, refreshed every five minutes, so a site whose address changes is unreachable until the next refresh. The rules match the browser's own address, so the daemon on the same network is not filtered. They cover forwarded traffic only: the VM's own addresses are not filtered, as for a devbox. A stack started without `istota-stack` (Docker Desktop, the testbed) has no such rules.

## Credential broker

On by default on the stack, together with reveal enforcement. A task never holds the value of a stored credential: it writes a placeholder such as `{{cred:portal_token}}` in an authentication header, and the network proxy substitutes the value on a connection to a host the credential is bound to, when a grant covers the task. The proxy terminates TLS for those hosts only, with certificates from a CA kept in daemon state outside every sandbox bind; each task trusts it through a bundle in its read-only control directory. Reveal enforcement refuses `istota-credential get`, `run` and `env` for any entry not marked readable, so the only way a value leaves the daemon is to its own site, or through a host-side skill. The forge token reaches `git`, `gh` and `glab` the same way.

Off by default where there is no sandbox and network proxy for it to rely on (`sandbox_enabled = false`, `[security.network] enabled = false`, or any platform other than Linux). Setup, use, the request path and the limits are in [Credential broker](../configuration/credentials.md#credential-broker).

## The local VM

The [local install](../getting-started/local-install.md) runs the stack in a Lima VM on a Mac, with a folder in the user's home (`~/Istota` by default) as the workspace. Inside the VM everything on this page applies unchanged. What the VM adds is a boundary between the stack and the Mac, and that boundary has a direction.

**It protects the Mac from the VM, not the VM from the Mac.** Any process running as the Mac user can run `limactl shell istota sudo ...`, which is root in the VM. Such a process can read the database, the config and the secrets files, or write a `CRON.md` into the workspace folder, and the folder gives it nothing it did not already have. Treat a compromise of the Mac account as a compromise of the install.

**From the VM, the folder is the only Mac path.** The Lima template mounts the workspace folder, writable, and in developer mode a checkout, read-only; it is built on Lima's bare Debian image, which does not pull in Lima's default mount of the whole home folder. A symbolic link in the folder is resolved by the VM's kernel in the VM's own filesystem, so a link to `/Users/<you>/.ssh` or to `../outside` leads to nothing, and a link to `/etc/hostname` reads the VM's own file. A compromised daemon or container can write into the folder and nowhere else on the Mac.

**Inside the VM, every uid owns the folder.** Apple's virtiofs server reports every entry as owned by whichever process in the VM asks, and the VM's kernel enforces mode bits against that answer. So any process in the VM can read and write any file in the folder, whatever mode the Mac gave it, and a `chown` from the VM changes nothing on the Mac: every file the VM writes is owned by the Mac user there. Separation inside the folder comes from the sandbox alone. A task's namespace binds only its own `Users/<id>/` subtree, as on a server; outside a task there is none, which on a server is also true of the daemon's view of its workspace.

**Files the VM writes carry no quarantine flag.** macOS marks a file downloaded by a browser with `com.apple.quarantine`, and Gatekeeper checks a marked program or app bundle before it first runs. Files written into the folder from the VM arrive without the flag, so a program the bot downloaded is not checked when you open it, as with a file a sync client writes. This is accepted and not mitigated: setting the flag would need a process watching the folder on the Mac, which the CLI install does not have. Open a program from the folder only if you would run it from any other download.

**Containers cannot reach the Mac's loopback.** Lima's user-mode network forwards the Mac's address as the VM sees it, `192.168.5.2`, to the Mac's `127.0.0.1`, so without a rule every container could reach whatever listens on the Mac's loopback: development databases, admin consoles, local model servers. `create` writes `HOST_LOOPBACK_BLOCK=1` and `HOST_GATEWAY_ADDR=192.168.5.2` into `host.env`, and the VM's egress unit (`host/istota-devbox-egress.sh`, which already owns the stack's `DOCKER-USER` rules) drops forwarded traffic from every container to that address. Two exceptions are returned ahead of the drop:

- DNS, UDP and TCP port 53. Lima serves DNS at the gateway address itself, and a default-bridge container (a `docker build` step, for one) resolves through it. Compose services use Docker's own resolver, which asks from the VM's namespace, outside the rule.
- The TCP ports in `HOST_LOOPBACK_ALLOW`, which `istota-stack allow-host-port` and `deny-host-port` edit, for a native brain pointed at a model server on the Mac.

A devbox gets neither exception. The rules fail closed: an allow-list entry that is not a port allows nothing, and a switch value other than `0` or `1` blocks. The VM itself is not filtered, nor is a container on the host network, and no service in the compose file uses it. No `doctor` check reports whether the block is in force, since the daemon cannot see `DOCKER-USER`.

**The master key leaves the VM once, for the Keychain.** `create` reads `/data/.secret_key` and stores it in the login Keychain (service `istota.local`, account `secret-key-<instance>`) through `security -i`, which reads its command on stdin, so the key is on no command line another local user could see with `ps`. It reads the item back and compares. The snapshots and config copies in the workspace folder hold no key and no credential (`secrets/` is never copied there), so the folder, and a Time Machine backup of it, cannot decrypt the stored credentials alone. The Keychain item is in the file-based login keychain, which iCloud Keychain does not sync.

## The Mac app

The Mac app, a separate project (istota-mac), is the local VM with a window, so everything in the section above holds for it. This section covers what istota provides for it; the app's own confinement (its web windows, per-install cookie stores, the command-line tools it writes) is described in that project's documentation.

**Signing in without a password is for the Mac user who owns the app.** The app asks the VM for a one-use code (`istota web login-code`, through the Mac CLI's `exec`), valid for 60 seconds, and loads it in the URL fragment, which no server log sees. The `local_code` method is refused under any ingress but `local` or unset (a stack started with no `host.env`) and for any `[site] hostname` but `localhost:<port>`, the address the app opens, and the web app answers only at `localhost` and `127.0.0.1` on that port. Whoever can run `limactl` against the app's Lima directory can mint a code; that is the macOS user who owns it, who could reach the VM as root anyway.

**Keys stay off the Mac's command lines and files.** The model's key is entered in the web setup page, which runs in the VM. The master key's copy goes to the login Keychain through `security -i`'s stdin, as the command-line install does.

## Deferred DB operations

With no database reachable from inside the sandbox at all, skills write JSON request files to the always-writable temp dir. The scheduler (unsandboxed) processes them after successful completion:

- `task_{id}_subtasks.json` -- subtask creation (admin-only)
- `task_{id}_sent_emails.json` -- outbound email tracking
- `task_{id}_kv_ops.json` -- KV store set/delete operations
- `task_{id}_kg_ops.json` -- knowledge-graph fact add/invalidate/delete (per-op commit)
- `task_{id}_user_alerts.json` -- notices the model raised, graded `security` / `action_needed` / `note`; the first two are pushed to the user's alerts channel, a `note` is recorded in the panel only
- `task_{id}_sender_flags.json` -- the model's flag that the mail it answered looks like an injection attempt; only a reason from a fixed set is read, the sender comes from the task's mail record, and the flag counts only on a correspondent's turn on mail that passed strict DMARC ([flagged senders](../features/email.md#flagged-senders))
- `task_{id}_email_output.json` -- deferred email sends (SMTP delivery after task completion)
- `task_{id}_health_ops.json` -- health module writes (stats, bloodwork, encounters)
- `task_{id}_garmin_import.json` -- Garmin Connect sync requests

One recovery artifact is written *by* the scheduler rather than read by it: `task_{id}_health_op_failures.json`, left behind when a health op fails mid-batch so an operator can recover the lost rows. It is recognized but never purged on retry.

Handlers and the shared envelope helper (`_load_deferred_json`) live in `scheduler_deferred.py`. Identity fields (`user_id`, `conversation_token`) come from the task, not the JSON, preventing spoofing via prompt injection. See [scheduler](../architecture/scheduler.md#deferred-db-operations) for retry-replay safety and the unconsumed-file warning.

## Configuration

```toml
[security]
sandbox_enabled = true
skill_proxy_enabled = true   # needed wherever sandbox_enabled is true; turning it
                             # off with the sandbox on warns at startup, leaves every
                             # configured credential in the task environment where the
                             # model can read it, and leaves skill commands with
                             # nothing to read
skill_proxy_timeout = 300
skill_client_wait_seconds = 600  # what the sandboxed istota-skill client waits on the
                             # proxy socket; every skill budget is capped at this
                             # minus a 30s margin. The proxy derives its ceiling from
                             # this field, never from the exported copy the model
                             # could rewrite
passthrough_env_vars = ["LANG", "LC_ALL", "LC_CTYPE", "TZ"]
sandbox_ro_paths = []        # extra RO binds for co-located services; keep narrow

[security.network]
enabled = true
allow_pypi = true
extra_hosts = []

[security.credential_broker]
enabled = true               # unset, follows the sandbox and the network proxy
enforce_reveal = true        # unset, follows enabled
```

The startup warning is read once at install. `istota doctor --only security.sandbox_credentials` reports the same pairing on demand, and the admin Health pane shows it as a standing `WARN` — with the bwrap probe's answer attached, so it says whether the sandbox you asked for is actually in force. Both switches off together is the single-user install's deliberate trust decision and is reported as a `SKIP`, not a warning.
