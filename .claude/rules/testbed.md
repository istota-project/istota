---
paths:
  - "testbed/**"
  - "tests/smoke/**"
  - "tests/full/**"
  - "tests/testbed/**"
  - "tests/support/**"
  - "tests/test_prompt_golden.py"
  - "docker/docker-compose*.yml"
  - "src/istota/setup_wizard.py"
---

# Testbed

`testbed/` is the staging environment the deployment tiers run against: one compose shape with an optional Nextcloud fixture, the services the daemon believes are real, a session-scoped stack pool, and a probe that reads a running stack's database. It is a package beside `src/` rather than inside `tests/`, with its own `pyproject.toml`, because it is not part of the shipped application and two rigs outside this one (istota-demo, istota-redteam) are meant to consume it rather than copy it. They do not yet; see "Still open". It imports no pytest, so a failure surfaces as a raised `StackError`.

Nothing under `src/istota/` may import it. That is convention, not a guard: no test checks it. `pythonpath = ["src", "."]` in `pyproject.toml` is what makes `testbed.stack` importable. Not `["src", "testbed"]`, which would put names as generic as `stack`, `probe` and `services` on the default suite's path. `testpaths = ["tests"]`, so the testbed's own unit tests live under `tests/` (`tests/test_testbed_services.py`, `tests/test_smoke_tier.py`, `tests/test_full_tier.py`).

The developer-facing version is `docs/development/testing.md`: which tier to run when, and how to add a service. This file is the internals and the traps.

## The one shape

| | lean | full |
|---|---|---|
| Compose | `docker/docker-compose.yml` + `testbed/compose/testbed.yml` | the same + `testbed/compose/nextcloud.yml` |
| Containers | istota, web, nginx (+ profile services) | + postgres, redis, nextcloud and two one-shot volume jobs |
| Storage | local, `/data/workspace` | full Nextcloud integration, `/mnt/shared` |
| Entrypoint | the shipped one, in full | the same |
| Boot | seconds | 50 to 84 seconds to both healthchecks on warm base images |
| Marker | `smoke` | `full` |

Both are the shipped compose file booted the way an operator boots it (the one-deployment-shape spec's Stage 4): the root phase, the entrypoint, `istota init`, the first-admin ensure, then the scheduler. `Profile.shape` picks whether the Nextcloud fixture is added, and `StackPool` boots both through one `Shape`. The full shape is the only thing in the repository that executes `provision-nc.sh` (now `testbed/compose/provision-nc.sh`) or `provision-rooms` against a real Talk.

**The config is an input on both shapes**, as it is for an operator. The testbed writes `config.toml` and the admins file from layers merged in order: a base that is what `istota setup` writes for the stack's one user (`stack.LEAN_BASE_CONFIG`), `stack.CONCESSIONS` (memory search off, `web.auth` at the dataclass default), each service's `config()` fragment, then the profile's own `config`. `tests/test_testbed_config.py` holds the base equal to `setup_wizard.render_container_config`, since this package may not import istota. Credentials go to secret files in a scratch directory the shipped `secrets:` read, on both shapes (the lean shape's is the scripted endpoint's placeholder key). The full shape adds full Nextcloud integration against the fixture (`workspace_path` and `nextcloud_mount_path` at `/mnt/shared`, `dav_prefix = "Shared Files"`, `auto_share_bot_dir = false`, the OAuth2 client the testbed generated and handed to `provision-nc.sh`). `toml_dumps` is the tree's one TOML writer, `istota.lib.toml_write`, which `istota setup` and `istota apply` render through too: a stdlib-only leaf that imports nothing from istota, so the testbed's dependency set is unchanged, though the istota package has to be importable beside it.

**The network sandbox is on**, except on the `forge` and `no-forge` profiles (`profiles.FORGE_CONFIG`): the forge stub speaks plain HTTP, and the CONNECT proxy carries only tunnels, so git's absolute-form request through it is refused where a real https forge is tunnelled. Everywhere else a task reaches a host-side stub only through the CONNECT proxy, and only where the config allowlists it. The forge stub is allowlisted by the developer skill from its `gitlab_url`, as any configured forge is. Parity row 2's witness (`tests/smoke/test_run_contract.py::TestATasksNetworkAndItsProxies`) relies on the scripted model endpoint being *off* the allowlist.

## Profiles

A profile is a named shape plus the services it runs plus any extra config. `StackPool` keys by profile *name*, so two tests declaring the same profile share one stack for the session, and a profile differing in anything a boot depends on needs a new name.

| Profile | Shape | Services | For |
|---|---|---|---|
| `base` | lean | model | anything needing only a scripted task: the sandbox masks |
| `forge` | lean | model, gitlab | the developer skill's forge chain, and secret isolation |
| `no-forge` | lean | model, gitlab | the negative control, on an image with the forge binaries removed |
| `network` | lean | model, gitlab | parity rows 2 and 3: the network sandbox on, the forge stub allowlisted and the model endpoint not |
| `notify` | lean | model, ntfy | a push leaving the container with its headers intact |
| `feeds` | lean | model, feeds | the poller against real HTTP, with `ISTOTA_FEEDS_ENABLED` in `Profile.config` |
| `mail` | lean | model, mail | the deployed email round trip, no Nextcloud |
| `signaling` | lean | model, signaling | the Talk signaling wire protocol against a real HPB |
| `browser` | lean | model, signaling | parity row 17: the `browser` and `signaling` compose profiles booted from the shipped file |
| `email` | lean | model, mail, ntfy | the email suite: `confirm_sender_match = "verify"`, the default `untrusted` outbound floor |
| `email-hold-all` | lean | model, mail, ntfy | the same with `ISTOTA_EMAIL_OUTBOUND_APPROVAL_FLOOR=all` |
| `full` | full | model, nextcloud, mail, signaling, ntfy | provisioning, Talk, storage, attachments, the signaling event stream, email seen from the web app |

Fine-grained on the lean shape, exactly one on the full shape. Many lean profiles keep unrelated pollers quiet during a test; at a cold six-container boot that argument inverts, so `full` carries mail and the watermark discipline absorbs the extra poller.

A test declares its profile as a string, `@pytest.mark.profile("forge")`, so a scenario file imports nothing from the package. `fresh=True` on the same marker buys a private stack, torn down at test end, for start-up behaviour. On the full shape that is a cold boot per test, so a file whose tests share one start-up stack takes a module-scoped fixture calling `stacks.get(profiles.FULL, fresh=True)` instead; `tests/full/test_provisioning.py` is the shipped consumer. `profiles.ALL` is what the default-suite guard iterates; a profile missing from it is invisible to that check.

`@pytest.mark.script([...])` is what the model answers, turn by turn. Omitting it installs `DEFAULT_SCRIPT`, one plain answer. A script depending on something invented at run time is installed with `stack.script(...)` inside the test, since the marker is read before the body runs.

`Probe.wait_for_task` is filtered on `task_id`, `conversation_token` or `id_above`, never on `user_id` alone: the scheduler queues its own work for the same user at startup (a `source_type='scheduled'` row nobody wrote).

There is no `backend` field and no `LOCAL` profile. See "The storage backend".

## The email suite

The email suite drives every sender class and gate of email on rooms through the deployed daemon: `tests/testbed/test_email_intake.py` and `tests/testbed/test_email_prompt_push.py` on the wire tier, six `tests/smoke/test_email_*.py` files on the `email` and `email-hold-all` profiles (the older `test_email_e2e.py` beside them runs on `mail`), and `tests/full/test_email_rooms.py` on `full`. The vocabulary they share is `tests/support/email_flow.py`; the users they assume are seeded by `tests/support/email_people.py`.

```bash
uv run pytest -m testbed -n0 tests/testbed/test_email_intake.py tests/testbed/test_email_prompt_push.py
uv run pytest -m smoke -n0 tests/smoke/test_email_*.py
uv run pytest -m full -n0 tests/full/test_email_rooms.py
```

**Turns are routed by the request, not served in order.** A script item `{"when": "<marker>", "turns": [...]}` is a route (`testbed/services/model_endpoint.py`). Each mail a scenario sends carries `[e2e:<nonce>]` in its body, and the request whose first user message holds that marker gets the route's turns; the turn index is the number of assistant messages already in the request, so a tool round trip takes the next turn and a retried attempt starts again at 0. Where several markers appear (a thread's history precedes the request), the one whose last occurrence is furthest right wins. A request matching no route takes the next positional item, or `NO_ACTION: unscripted`, and is recorded in `endpoint.unmatched`. The autouse fixture in both email conftests fails a test whose own marked request found no turn, and ignores a request whose rightmost marker the test did not send, because the nightly memory extraction and a channel's memory pass quote earlier mail, markers included. A test that rescripts mid-way keeps its earlier routes for the same reason.

**Seeding.** `seed_email_people` runs once per stack through the shipped CLI: testuser with `*@trusted.test` as a trusted-sender pattern, `*@quiet.test` as a quiet one, an ntfy secret and an alert route, then alice. Profile patterns rather than the runtime trust table, since `reset_framework_state` clears the table. A readiness mail from alice, routed `sender_match`, proves the profiles reached the running daemon before any scenario runs. The alert route is `email,ntfy` on lean, which has no Talk, and `talk:<alerts room>,ntfy` on full, so a push the product cuts to `ROOM_FREE_SURFACES` (ntfy and email) shows as no post in Talk.

**`email_flow.assert_outcome` checks eight dimensions** of one mail and reports every mismatch: the `processed_emails` row, the task, the room, its participants, the transcript rows, the reply on the wire, the email note, and the notification rows with the ntfy pushes and alert mails. `Expected` has no defaults. Three readings are not obvious:

- "No note" and "no reply" are read after the note step's own `task_logs` line, `Email note step:` and its decision (#651, `email_flow.note_step`), which is written after the note and its push for every email run that succeeds without parking, including one whose mail then failed to send. The decision is asserted too (`note written (<outcome>)`, `no note due (…)`, `not an email thread`), so "no note" is a decision read rather than an absent row. A task that is not `completed` (parked, or failed in its run or its delivery) is read after the worker's `task N completed` log line (`worker_done`) instead. "No reply" is then watched for a bounded ten seconds, since a sent mail reaches IMAP after either line. A held mail no worker ran gets the same bounded settle before "nothing was pushed".
- The email profiles leave `[speech_gate] mode` at its shipped default, `classifier`, on purpose. An email thread room is kept on `mention` by its own rule (`room_policy.effective_speech_mode`), so no classifier request is made for a thread; the recorded-only cases read the gate's rung (`email_flow.gate_rung`, `mode_mention`) to say that rule decided. Pinning `mention` in the profile would let them pass whether or not it held. A classifier-mode case on a thread is not drivable: `speech_mode_refusal` keeps a host from setting one there.
- Every push on the alert route is a title plus fixed or composed text (#638). The check is that the sender's words and the note's remark are in no push and no alert mail, not that some push happened.
- `WATERMARK_TABLES` includes `notifications`, but a row that is bumped or reopened keeps its id. A scenario finds its own row by dedup key on a fresh object (a new task, a new draft), and reads "no push" off the ntfy stub.

**The note's two homes.** On lean nobody has a private room (rooms are user-created, and lean runs no web app), so every email note is the `private-note:<task>` bell row and its push is `PRIVATE_NOTE_POINTER`. On full, `test_email_rooms.py` signs testuser in, creates its web rooms through the API, and pins one as `default_room`, which `private_replies.private_room_for` then resolves for every thread. The note is a system row there, and its bell push is room-free.

**Where the suite departs from its spec, as built.** The gate is answered by mail through #649's authenticated `!confirm`, with a forged answer that changes nothing as a case of its own. Approving a held mail on a vetoed thread asserts #650's fix: the task is cancelled, the ack says so, and the room gains no participant. Release and discard run through `/chat/drafts/{id}` on the full shape. A draft answered by an authenticated `!drafts send|discard <id>` mail (#662) is driven on lean in `test_email_outbound_gate.py`, with a forged answer and a missing id as refusals. The web confirm route's 409 for a room that is not a preview room applies to relay-held tasks, so the full file drives it with a `room post`; its #665 arm, a `room` naming a thread room, is driven beside the private park, with the thread room's history showing the mail but no answer and no active task for the parked question. A question parked in a web-only private room is pushed with `PARK_BODY`, its row being the delivery (#661). The full file signs in once per stack and keeps the client, since the email login throttles attempts per address.

**What the email seeding leaves on a session stack.** Nothing undoes it, so every later test on the same stack runs with it: testuser's trusted and quiet patterns, the alert route naming ntfy, the ntfy secret, alice as a user, and on full the three web rooms, alice in the shared one, and testuser's `default_room` pinned to the private room. A later scenario that delivers to testuser's default web room, or counts testuser's rooms, has to allow for that.

## Services

A **service** is anything the daemon talks to that is not the daemon, real or written by us. A **stub** is one we wrote, and it is a liability to minimize. `services.REGISTRY` maps a profile's names to factories:

- `HOST_STUBS`: `model`, `gitlab`, `ntfy`, `feeds`. `ThreadingHTTPServer` in the pytest process, on an ephemeral port bound to all interfaces, reachable as `host.docker.internal`. `HttpStub` is the shared base.
- `ATTACHED`: `nextcloud`, `mail`, `signaling`. Real servers a compose file already runs. `nextcloud.attach` starts nothing and returns a `Service` over the running container, so the fixture, the profile list and `diagnostics` need no special case. `signaling` is attached for rule 4's reason: `welcome`/`hello` feature negotiation is a client negotiating with a server.

The protocol's **required** members are `name`, `container_url`, `config()`, `reset()` and `close()`. Call recording is deliberately not on it: a mail server speaks IMAP and Nextcloud is asserted through its own API.

Four more are **optional**, resolved by `getattr`, each implemented by only one or two services:

- `compose_env()` (`mail`, `signaling`): variables that configure the compose *stack* rather than the daemon, such as host paths and image tags an overlay binds, and container secrets a shipped service is declared from (`signaling.compose_env()` names `ISTOTA_TALK_SIGNALING_SECRET` and neighbours, read by `docker-compose.yml` for the signaling container). Nothing istota reads as configuration goes through it: compose passes istota none, so a variable smuggled in here reaches no reader.
- `container_state_paths` (`gitlab`): container-side directories a service's use dirties. Validated against `PROTECTED_CONTAINER_PATHS`, which refuses `..`, relative paths and anything at or containing `/data/db` or `/data/config`, because the mechanism is `rm -rf` as root inside a container.
- `bind_stack(stack)` (`nextcloud`, `mail`): why an attached service can exist at all.
- `describe()`: what makes `Stack.diagnostics` generic, rather than reaching into `stub.calls`.

Two package properties behave like rules. `testbed/pyproject.toml` keeps an explicit `only-include` list, and a module missing from it fails **quietly**: the in-tree import works and only an installed consumer sees the gap. The dependency set is stdlib plus `cryptography`, deliberately, because a resolver conflict would push the external rigs back to copying.

`Stack.env` is redacted by credential *suffix shape* rather than by a list. Compose variables ride in an `--env-file` rather than `env=`, and getting that wrong fails silently both ways; `write_env_file` refuses newlines, ` #` and surrounding whitespace by name. A full boot refuses when the process environment would outrank the env-file: read that `StackError` as "your shell exports this and the scrub does not cover it".

`ServiceCall.auth` is a shape string (scheme and length, never the value). Other fields stay whole for assertions, and `__repr__` redacts, because pytest's assertion rewriting prints the repr of whatever a failing comparison touched.

## The four rules

**1. Every key a service's `config()` or a profile's `config` writes must be one `load_config` reads.** The config is an input now, so there is no generator for a fixture to side-load around; the rule is that the testbed writes nothing an operator's `istota setup` output could not hold, and an unknown key is only a warning at load, which would boot a stack silently ignoring the setting a profile asked for. `tests/test_testbed_services.py` loads every service fragment and every profile's `config` and fails on the loader's unknown-key warning. Its `services` fixture is a hand-maintained list of four (`model`, `gitlab`, `mail`, `signaling`); `ntfy`, `feeds` and `nextcloud` return nothing, so add one when it grows a key. Two services setting the same key is refused by `merge_config`; the profile may override a service.

**2. A stub bound to anything but loopback must be given a credential to expect.** `HttpStub.start` raises otherwise. Both compose tiers bind all interfaces, which on a shared network is an open listener, and for the forge stub one running `git http-backend` with `GIT_HTTP_EXPORT_ALL`. The credential also gives the secret-isolation scenario the name of every secret the session published.

**3. A negative assertion takes a watermark and a discriminating column, never an empty table.** `Probe.watermark()` captures `MAX(id)` per table at reset; `Probe.rows_above(table, mark, **filters)` refuses to run without a filter. A watermark alone still catches a background poller's row; a column filter alone still catches the previous test's. `sent_emails`, `processed_emails`, `messages` and `task_events` are framework tables nothing truncates.

**4. Do not stub a service whose client negotiates with it.** `nextcloud/capabilities.py` means the client asks before it acts, and a stub that answers wrongly steers the daemon down paths no test chose. That is why the full shape runs a real Nextcloud 30 provisioned by the shipped script. It is not "never stub": `gitlab` is spoken to by a real `glab` and `git`, and `ntfy`'s assertion is about header bytes, which a recording stub sees better.

## Session scope, and what a reset does

`Stack.reset(turns)` runs before each test, not after, so a failed test's state stays inspectable. The order is forced and the script goes **last**:

1. `reset_framework_state`: release any parked confirmation, cancel retry rows, clear `trusted_email_senders`.
2. Quiesce: poll until nothing is `pending`, `locked` or `running` whose `scheduled_for` has arrived, compared on the database's clock. That set is `IN_FLIGHT`. `pending_confirmation` is deliberately outside it: a task waiting for a human will not move, and counting it would make every reset wait out its timeout. `Probe.wait_for_task` draws the same line.
3. `service.reset()` on every service except `model`, then `clear_container_state()`.
4. Quiesce again and install the script behind the endpoint's barrier.
5. Return the watermark; the `stack` fixture stashes it as `stack.mark`.

The script goes last because `script` only holds the barrier across the swap; installed first, this test's turn 0 is exposed for as long as the slow work takes.

Four non-obvious points:

- **Nothing is truncated.** Deleting rows under a running dispatcher is a race. The three exceptions above are forced: a parked `pending_confirmation` blocks its room for two hours, a retry row can fire mid-test and take a scripted turn the barrier cannot see, and a trusted sender changes what later scenarios mean.
- **The retry ladder wedges a naive quiesce.** A failed task is rewritten `pending` with `scheduled_for` one, four, then sixteen minutes out, so a status-only quiesce waits out a backoff. The barrier's refusal is a 403 rather than 409 for the same reason: 409 is in neither the transient nor the permanent status set, so the daemon retried it and created the wedging row.
- **The daemon writes inside the container too.** A host-side stub cannot reach `/data/repos`, where the model cloned on the previous test, so each service declares its dirtied container directories beside the `config()` key that put the daemon there.
- **The workspace is never cleared.** On the full shape `/mnt/shared/.istota-provisioned` lives there, and the fixture's healthcheck (which `istota` waits on) requires it. A storage scenario writes under a generated name and asserts on that name.

`nextcloud.reset()` deletes only the rooms this object created. The boot leaves baseline rooms (the entrypoint's 1:1, `#general`, `#logs`, `#alerts`, plus Talk's own `Talk updates` and `Note to self` per account), so a scenario asserts on a room it made, never on a count.

## What the stack concedes

`testbed/compose/testbed.yml` is a harness concession file, not a deployment recipe. It is the complete list of ways a stack this tier boots differs from what an operator boots, each with its reason inline:

- `image: ${ISTOTA_TEST_IMAGE}` on istota, web and webhooks: one tag per checkout, built on the session's first boot and run as it is after; a negative control's prebuilt image goes through the same variable with no `--build`, which is what replaced `docker-compose.test.prebuilt.yml`.
- `extra_hosts: host.docker.internal:host-gateway`: built in on Docker Desktop, absent on Docker Engine.
- The config directory the testbed wrote, bound read-only at `/data/config` on `istota` and `web`. The shipped file binds a host directory there too (`ISTOTA_CONFIG_DIR`, `/srv/istota/config` on the VM); compose merges the two by target, so only the source differs.
- `/mnt/shared` replaced by an empty volume: the shipped bind is `rslave`, which Docker Desktop refuses for a macOS path.
- The shipped `tasks` healthcheck polled faster.
- The browser's fixed `container_name` reset, so two stacks on one machine can both run one.
- `ISTOTA_WEB_INSECURE_COOKIES=1` on `web`: the tiers sign in with httpx over plain http on 127.0.0.1, and its cookie jar sends a `Secure` cookie back over https only, where a browser treats `http://localhost` as secure.

`testbed/compose/nextcloud.yml` is the Nextcloud fixture: postgres, redis, Nextcloud 30 provisioned by `provision-nc.sh`, a one-shot job that applies the old root phase's shared-volume convention after provisioning, the fixture's shared volume at `/mnt/shared` on istota, web and webhooks, the Nextcloud locations on the stack's nginx (`ISTOTA_NGINX_ROOT_LOCATIONS`), and `ISTOTA_TESTBED_SHARED_VOLUME_WORKSPACE=1`, which narrows the entrypoint's mount refusal to "a mount point of its own" because the workspace is a Docker volume rather than an rclone mount. No install ships that layout; the rclone mount is the vm tier's job.

Per-session values go in the env-file `StackPool` writes (`stack.stack_env`, plus `full_env` on the full shape): the image tag, the config and secrets directories, ephemeral published ports (nginx, the browser, signaling), and on the full shape the generated passwords, the OAuth2 pair, the reserved `NC_PORT` and its matching `ISTOTA_WEB_CALLBACK_URL` (which `provision-nc.sh` bakes irreversibly into `oauth2_clients` at first install). Which subsystems a full profile runs is `FULL_MODULE_SWITCHES`, written into the config.

**The sandbox grant is the shipped file's, and no overlay touches it.** `docker/docker-compose.yml`'s `istota` service carries the run contract (Stage 2): `seccomp=./istota/seccomp-istota.json`, `apparmor=istota`, `systempaths=unconfined`, `no-new-privileges:true`, `cap_drop: ALL` with the six root-phase capabilities, `read_only`, `cgroup: private`. `tests/test_container_security_profiles.py` refuses any of those keys in `testbed.yml` or `nextcloud.yml`, and `tests/test_smoke_tier.py` refuses an unconfined grant in all three files. On a host with AppArmor the `istota` profile has to be loaded first; Docker Desktop has none and ignores the option.

**Every exec into the `istota` container goes through `istota-drop`.** `Stack.exec` and `Probe.query` prefix it for the `istota` service, because a `docker compose exec` starts as uid 0 with the `cap_add` set, not from the daemon's state, and a root-owned file under `/data` locks the daemon out of it. A caller that needs uid 0 for a read passes `user="0"`.

**The smoke negative controls break the run contract one line at a time.** `ISTOTA_TESTBED_CONTROL_OVERLAYS` (paths, `os.pathsep`-separated) adds compose overlays to every stack, last; `scripts/test-image-negative-control.sh run-contract` uses it with the overlays in `docker/test/run-contract-controls/` and the control images, and names the `tests/smoke/test_run_contract.py` node ids each must turn red.

## The storage backend

`Config.storage_is_nextcloud` is `bool(self.nextcloud.url)`, and both values are shipped modes, and both are booted now: every lean profile runs local storage (`/data/workspace`, the default an operator gets), and the full profile runs full Nextcloud integration. The pure-function differences are witnessed without a stack as well:

| What differs | Witness |
|---|---|
| The prompt's file-tool vocabulary, selected by `storage_backend` | `tests/test_prompt_golden.py`, the `base_nextcloud` / `base_local` pair |
| The skill menu, since `available_capabilities()` drops `nextcloud` on an empty URL | the same pair |
| `runtime.mount_liveness`, `ok` under `nextcloud` and `skip` under `local` | `tests/test_doctor.py::TestMountLiveness` |

Plus `tests/test_setup_container.py`: a setup with no Nextcloud writes a config that loads as `storage_backend == "local"` with the workspace at `/data/workspace`. `doctor` assertions on the lean shape name checks rather than comparing whole payloads, since `runtime.mount_liveness` reports `skip` there.

## Prompt goldens

`tests/test_prompt_golden.py` runs in the default suite with no container and no model. `execute_task(..., dry_run=True)` returns both halves of the assembled prompt as the second element of its four-tuple, behind a `[DRY RUN] Would execute with prompts:` line the test strips, labelled `===== SYSTEM =====` and `===== USER =====` by `executor.render_composed_prompt`. Each case in `CASES` snapshots the labelled pair into `tests/golden/prompts/`; `test_every_golden_file_belongs_to_a_case` checks membership, so no count is written here. The helper goes one way only: there is no parser in the product, because one would make the delimiters load-bearing and a prompt containing one would re-split wrongly. `test_prompt_golden.py::split_halves` is the test-side parser, for cases that name one half. A diff is a failure; an intentional change is a reviewed golden update:

```bash
uv run env ISTOTA_UPDATE_GOLDEN=1 pytest tests/test_prompt_golden.py -n0
```

`env` goes *inside* the `uv run`, not in front as a shell assignment. `uv` is in `DEFAULT_SHIM_COMMANDS`, so with a devbox it is a shim that hands argv to the exec server, and `devbox.exec_protocol` carries no `env` field (deliberately, and pinned), so nothing set in the calling shell arrives; the run then compares instead of rewriting. In argv the assignment survives, and without a devbox the two forms are identical. `tests/support/env_isolation.py` keeps the variable in its keep-list for the same reason.

`-n0` matters: the orphan check has no ordering with the writers under xdist, so a regeneration adding a case reports missing goldens from the run meant to create it. The variable is parsed by an `updating()` helper taking the same affirmative and negative sets as `PRECOMMIT_SCANS_REQUIRED` and raising on anything else, so `ISTOTA_UPDATE_GOLDEN=0` left exported cannot turn every golden into a rubber stamp.

**`dry_run` returns after assembly rather than instead of it**, so everything assembly calls is live. A golden path reaching a network socket is a golden that lies about running against nothing (`read_user_memory_v2` returning None once led to `ensure_user_directories_v2` and an OCS share POST). An autouse `_no_sockets` fixture records, refuses and asserts at teardown; recording matters because every caller on that path swallows exceptions. Turn a live path off through configuration, not a mock.

One product gap is held by a named test rather than fixed, so a fix arrives as a reviewed golden diff:

- `custom_system_prompt` cannot change the assembled prompt, because it is read at brain-request assembly past the `dry_run` return. It is the brain's system prompt, not the task prompt, and an identity assertion goes red if a change routes it into the task prompt.

The other is closed. `format_cli_skills` applied neither the capability gate nor the effective disabled set, so a Nextcloud-free install was told `istota-skill nextcloud` exists while the menu omitted it. Both producers now read `advertised_cli_skills`, and `test_the_cli_tool_list_does_not_apply_the_capability_gate` asserts the pair, since `local` going quiet is also what a renamed skill looks like. An operator-disabled CLI is no longer advertised though the proxy would still run it, the rule `admin_only` already followed. **It shares the disabled set with the menu, not the whole gate**: `eligible_skill_names` also drops an unenabled experimental skill and one with missing dependencies, so on a lean install `istota-skill whisper` is still named while the menu omits it; that residual is recorded on `advertised_cli_skills`. See `.claude/rules/skills.md`.

**Neither producer is pinned by the goldens alone.** The list half is held by `base_local.txt` losing its `istota-skill nextcloud` line. The `Room:` clause is gated by `build_prompt`'s `cli_skill_names`, whose `None` default is permissive and whose only production caller is `execute_task`; every golden case has `rooms` enabled, so deleting that argument left all goldens unchanged. `TestTheCliAdvertisingWiring` drives an operator-disabled `rooms` through `execute_task` for that reason, and `Case.disabled_skills` exists only to make that reachable.

**The synthetic catalogue carries a `rooms` skill for one reason.** The `Room:` line names `istota-skill rooms list` and emits that clause only where `rooms` is in the effective index, so without it the `room_*` goldens would snapshot the gated form and stop covering the sentence ISSUE-509 wrote them for (ISSUE-513). The gated form is covered by `tests/test_prompt_room_identity.py::TestTheRoomsCliClause` instead.

## A probe whose success is indistinguishable from a no-op

The recurring failure, each time found by a control rather than by reading. The first five are this tier's; the last three are default-suite cases of the same shape:

- **Readiness probe matching itself.** The full shape's probe scanned `/proc/[0-9]*/cmdline` for a string its own `sh -c` command line contained, so it returned on the first poll of any container, including a bare `alpine`.
- **Sandbox output that a skipped sandbox also produces.** Three sandbox scenarios asserted a scripted Bash call's output came back, which an unconfined run returns identically. The assertion has to name something only the mechanism produces: `tests/smoke/test_sandbox_in_stack.py::TestTheDatabaseMasks` requires `stat -f` to report `tmpfs` at `db_path.parent`, the directory empty, the framework database unopenable and a `touch` refused, with an in-session control outside the sandbox requiring the opposite. `TestTheComposedSystemPromptInTheStack` reads its probe answers together, because each passes in a state another refuses: `append=refused`, `composed=present`, `sibling=writable` (a file in the per-user temp dir, separating a scoped read-only bind from a wholesale one), `control_in_cwd=no`, and `neighbour=absent` / `neighbour_readable=no` (the bind is this task's directory, not the per-user level). It needed three negative controls: removing the bind leaves the write answers green (ENOENT), so turning the bind read-write proves the write answers can fail and widening it one level proves the isolation answers can. Its second case asserts from the endpoint transcript that the composed sentinel arrived in the system field and the request sentinel in the user message.
- **Encoded words decoded before the wire.** Three wire-level email cases meant to carry RFC 2047 encoded words sent plain text, because `EmailMessage` under `policy.SMTP` decodes an encoded word on assignment for any header its registry does not know.
- **A passthrough assertion that could not fail**, because the generator's default for `confirm_sender_match` was the value the profile asked for.
- **`published_port` takes the IPv4 line** from `docker compose port`, not the first. Docker binds v4 and v6 separately, sometimes on different host ports, while every caller pairs the answer with `127.0.0.1`.
- **A Talk double more permissive than Talk** (ISSUE-400). Delivery tests patched `get_talk_client` with a `MagicMock` that accepted any conversation token; Nextcloud 404s one naming no conversation. A room's canonical id and its `talk` binding's `surface_ref` differ only on a *promoted* room, and no test built one, so a promoted room's progress ack went to the `web-…` token and every edit no-opped. The instruments are `tests/support/talk_double.py`, which accepts a token only if it is a live `talk` `surface_ref` in `room_bindings` or named in `known_channels` (`strict=False` is the escape for what nothing can model), and `tests/support/rooms.py`'s `promoted_room`. The control that measures the conversion is mutating `TalkTransport.deliver` to the misroute, one layer below the shim the old tests mocked. The web process has its own fixture, `fake_talk_web`, which patches `istota.nextcloud.talk.TalkClient` itself, because `webui/app.py` constructs one directly in eight places (`_chat_promote_to_talk` and `_post_as_user` among them) with no factory to patch; its controls are in `tests/test_web_talk_seams.py`. It reaches all eight sites and drives seven; the rename propagation in `chat_update_room` is not driven. `_talk_conversation_verdict` branches on the bot's 404, and ISSUE-407 keyed `None` to the bot's basic auth, so `bot_removed` is driven; `gone` still is not, because the user client built inside the verdict leaves its bearer on the one shared instance. `_delete_from_talk`'s bot leg no longer reaches the singleton (ISSUE-407). `sent_id_for` puts the minted id on the call rather than walking parallel arrays, since a credential rejection records a call without an id.
- **A double that can fail only one way.** `_post_as_user` and `_mark_read_as_user` force-refresh the OAuth token once on a 401 and retry; a double answering every unhappy call with `UnknownTalkRoom` collapses that into a misroute. `bearer_rejections` is the second failure mode, recorded as `TalkCall.status` and not as `refused`, so `refusals == []` assertions keep their meaning. A server that can fail two ways needs a double that can fail two ways.
- **Real clients behind a swallowed failure.** Ten tests in `tests/test_scheduler.py` and one in `tests/test_confirmation_surfaces.py` built a real `TalkClient` against the configured host on every run: they patched `istota.scheduler.asyncio.run` or `istota.scheduler.run_coro`, but the Talk legs go through `istota.consumers.talk.run_coro`, and `TalkTransport.deliver`, `scheduler.edit_talk_message` and `inbound._post_ack` all catch and return falsy. Found with an autouse probe that *records* constructions rather than raising. The probe is not in the tree, so a permanent guard is outstanding. The client-constructing tests in `tests/test_talk_client_persistent.py` are that file's subject and stay.

On a tier asserting against an artifact, reading the test tells you almost nothing about whether it can fail. Run the control and write down what it turned red. In the default suite, a double more permissive than the real thing, a double with only its author's failure mode, and a product that catches and returns falsy all make a test green the way a skipped sandbox does. A test asserting that nothing was raised asserts nothing; assert on what the double recorded or the product returned.

## Environment

These are the ones a person sets. The harness sets others itself (the env-file's `ISTOTA_TEST_IMAGE`, `ISTOTA_TEST_CONFIG_DIR`, `ISTOTA_SECRETS_DIR`, `ISTOTA_TESTBED_COMPOSE_DIR` and the published ports, `stack.STACK_ENV_KEYS`); setting one by hand only confuses it.

No variable names the checkout a stack builds from. `Shape` takes `compose_file` as an argument and `tests/conftest.py` supplies it, so an outside consumer can point the pool at its own file.

| Variable | Effect |
|---|---|
| `ISTOTA_TESTBED_KEEP` | persist the Nextcloud fixture's and postgres volumes plus generated credentials between sessions. Containers still come down; keeps `shared_files`, wipes `istota_data` and `redis_data`. `tests/full/` refuses to run under it, since its assertions are about first-install state |
| `ISTOTA_TESTBED_MAIL_IMAGE` | override the pinned Maddy digest |
| `ISTOTA_IMAGE_TAG` | use a prebuilt image instead of building; how the upgrade tier's negative control is fed |
| `ISTOTA_UPDATE_GOLDEN` | rewrite the prompt goldens instead of comparing |
| `ISTOTA_TESTBED_CONTROL_OVERLAYS` | extra compose overlays on every stack; how the run-contract negative controls break one line |

`KEEP` does **not** wipe `shared_files`: that volume holds `/mnt/shared/.istota-provisioned`, which `provision-nc.sh` never rewrites, because it is a `post-installation` hook and `nextcloud:30-apache` runs those only when the installed version is `0.0.0.0`. Wiping it leaves the fixture's healthcheck waiting for a flag nothing writes, so `istota`, which depends on it being healthy, never starts. The host port is also pinned across kept sessions, since the OAuth2 redirect URI is baked at first install.

## Costs, measured

One developer machine, August 2026 (arm64, 10 cores, Docker Desktop 29.6), warm caches, runs serialized through `scripts/qtest`. Treat them as shape, not threshold; the tier prints its own `docker compose exec` fraction at the end of each session.

- Lean tier: nine stacks per session (`base`, `forge`, `no-forge`, `notify`, `feeds`, `mail`, `signaling`, `email`, `email-hold-all`). About 165 seconds for the first six, measured before `signaling` and the two email profiles were added. Per-profile boot 6.5 to 9 seconds, per-test setup after that about 0.7 seconds.
- Full tier: one cold boot of six containers, 50 to 84 seconds to both healthchecks. Nextcloud is healthy before `up` returns, because `istota` declares `depends_on: service_healthy`.
- `docker compose exec` is about 31% of a lean session (123 to 127 ms a call) and 5 to 8% of a full one. No optimization was built; the counters stay.

## Still open

- **The external rigs do not consume this package yet.** istota-demo and istota-redteam still carry their own mail overlay, cert generation, readiness polling and seeder-copy framework. `testbed/` is installable and its wheel ships `compose/`; pointing both rigs at it is hand-driven work in two other repositories.
- A Docker deployment's sandbox posture, above.
- The positive half of ISSUE-245 on a deployed shape: a confirmation prompt that reaches a surface. Reachable on the full profile (Talk plus an auto-provisioned `alerts_channel`); the lean shape has no surface.
- Feeds image dedupe. Its only caller is the authenticated web reader, so a deployed-path scenario cannot reach it until the Svelte components carry `data-testid` hooks.
- `Probe` cannot read a *module* database, so the feeds scenario asserts through the CLI's output rather than on rows.
- Per-session image tags (`istota-test/istota:*`, `istota-test/no-forge:*`, and the full shape's per-project `<project>-istota` and `-web`) are built every session and removed by nothing. Remove them by hand.
