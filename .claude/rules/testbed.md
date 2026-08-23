# Testbed

`testbed/` is the staging environment the deployment tiers run against: two compose shapes, the services the daemon believes are real, a session-scoped stack pool, and a probe that reads a running stack's database. It is a package beside `src/` rather than inside `tests/`, with its own `pyproject.toml`, because it is not part of the shipped application and two repositories outside this one (istota-demo, istota-redteam) consume it. It imports no pytest, so a failure surfaces as a raised `StackError` rather than as a call into a test runner that may not be installed.

Nothing under `src/istota/` may import it. `pythonpath = ["src", "."]` in `pyproject.toml` is what makes `testbed.stack` importable — not `["src", "testbed"]`, which would put names as generic as `stack`, `probe` and `services` on the default suite's path. `testpaths = ["tests"]`, so the testbed's own unit tests live under `tests/` like everything else (`tests/test_testbed_services.py`, `tests/test_smoke_tier.py`, `tests/test_full_tier.py`).

The developer-facing version of this file is `docs/development/testing.md`: which tier to run when, and how to add a service. This one is the internals and the traps.

## The two shapes

| | lean | full |
|---|---|---|
| Compose | `docker/docker-compose.test.yml` | `docker/docker-compose.yml` + `testbed/compose/testbed.yml` |
| Containers | one | postgres, redis, nextcloud, istota, web, nginx |
| Entrypoint | bypassed; `init` then the scheduler | the shipped `entrypoint.sh`, in full |
| Config | rendered on the host by `testbed.stack.render_config` and bind-mounted | rendered by `render-config.sh` inside the container, from the compose env-file |
| Boot | seconds | 50 to 84 seconds to both healthchecks, measured over three cold volume sets on warm base images |
| Marker | `smoke` | `full` |

Same fixtures either way. `Profile.shape` picks, and `StackPool` knows how to boot both. The full shape is the only thing in the repository that executes `provision-nc.sh`, or the half of `entrypoint.sh` past the config write, or the room find-and-reuse branch.

The difference in *where* the config is rendered is the one that reaches the code. On the lean shape a service's `config_env()` is merged into the render environment; on the full shape it is written into the compose env-file and the container's own generator reads it. Same map, two destinations.

## Profiles

A profile is a named shape plus the services it runs plus any extra config. `StackPool` keys by profile *name*, so two tests declaring the same profile share one stack for the session, and a profile that differs in anything a boot depends on has to be a new name.

| Profile | Shape | Services | For |
|---|---|---|---|
| `base` | lean | model | anything needing only a scripted task: the sandbox masks |
| `forge` | lean | model, gitlab | the developer skill's forge chain, and secret isolation |
| `no-forge` | lean | model, gitlab | the negative control, on an image with the forge binaries removed |
| `notify` | lean | model, ntfy | a push leaving the container with its headers intact |
| `feeds` | lean | model, feeds | the poller against real HTTP, with `ISTOTA_FEEDS_ENABLED` in `Profile.config` |
| `mail` | lean | model, mail | the deployed email round trip, no Nextcloud |
| `full` | full | model, nextcloud, mail | provisioning, Talk, storage, attachments |

Fine-grained on the lean shape, exactly one on the full shape. The argument for many profiles is that a stack with every subsystem enabled has the daemon polling mail, feeds and Talk during every unrelated test, which makes the quiesce wait the dominant cost. That is an argument about a thirty-second boot and it inverts at a cold six-container one: `full` plus a `full-mail` would be two cold boots to run four scenarios, so `full` carries mail and the extra poller is what the watermark discipline absorbs.

A test declares its profile as a string, `@pytest.mark.profile("forge")`, so a scenario file imports nothing from the package. `fresh=True` on the same marker buys a private stack, torn down at test end, for anything asserting on start-up behaviour. `profiles.ALL` is what the default-suite guard iterates; a profile missing from it is invisible to that check.

There is no `backend` field and no `LOCAL` profile. See "The storage backend" below.

## Services

A **service** is anything the daemon talks to that is not the daemon, real or written by us. A **stub** is one we wrote, and it is a liability to be minimized rather than a design goal. `services.REGISTRY` maps a profile's names to factories:

- `HOST_STUBS` — `model`, `gitlab`, `ntfy`, `feeds`. `ThreadingHTTPServer` in the pytest process, on an ephemeral port bound to all interfaces so a container can reach it, reachable as `host.docker.internal`. `HttpStub` is the shared base.
- `ATTACHED` — `nextcloud`, `mail`. Real servers a compose file already runs. `nextcloud.attach` starts nothing and returns a `Service` over the running container, so the fixture, the profile list and `diagnostics` need no special case for it.

The protocol is `container_url`, `config_env()`, `reset()`, `close()`, and a `name`. Call recording is deliberately not on it: a mail server speaks IMAP and Nextcloud is asserted through its own API, so a `calls` list would mean something different for two of six members.

`ServiceCall.auth` is a shape string — scheme and length, never the value. The other fields are kept whole because assertions need them, so the guarantee is about rendering: `__repr__` redacts, because pytest's assertion rewriting prints the repr of whatever a failing comparison touched.

## The four rules

**A service may only be wired in through a variable `docker/istota/render-config.sh` reads *and* `docker/docker-compose.yml` passes through.** Two files, not one, and they are not automatically in sync. The mail work found `ISTOTA_EMAIL_AUTHSERV_ID` and `ISTOTA_EMAIL_CONFIRM_SENDER_MATCH` read by the generator and passed by neither, which meant an operator who set the confirmation gate to `verify` in `docker/.env` silently got `off`. A missing variable is added to both as a reviewed product change; it is never side-loaded from the fixture. This is the property that makes the tier honest, and it applies to `Profile.config` as well as to `config_env()`. Enforced by `tests/test_testbed_services.py` and `tests/test_render_config.py`, which grep both files.

**A stub bound to anything but loopback must be given a credential to expect.** `HttpStub.start` raises otherwise. Both compose tiers bind all interfaces so a container can reach the stub, which on a laptop on a shared network is an open listener — and in the forge stub's case one running `git http-backend` with `GIT_HTTP_EXPORT_ALL`. The credential also gives the secret-isolation scenario the name of every secret the session published, which is what it sweeps a transcript for.

**A negative assertion takes a watermark and a discriminating column, never an empty table.** `Probe.watermark()` captures `MAX(id)` per table at reset; `Probe.rows_above(table, mark, **filters)` refuses to run without at least one filter. Both halves are needed: a watermark alone still catches one of the eleven background pollers' rows, and a column filter alone still catches the previous test's. `sent_emails`, `processed_emails`, `messages` and `task_events` are framework tables nothing truncates, so "no reply was sent" against a session-scoped stack reads the last scenario's rows unless it is scoped this way.

**Do not stub a service whose client negotiates with it.** `nextcloud/capabilities.py` means the client asks before it acts, and a stub that answers wrongly steers the daemon down paths no test chose. That is why the full shape runs a real Nextcloud 30 provisioned by the shipped script. The rule is not "never stub": `gitlab` is spoken to by a real `glab` and a real `git`, and `ntfy`'s whole assertion is about header bytes, which a recording stub sees better than a real server would.

## Session scope, and what a reset does

`Stack.reset(turns)` runs before each test, not after, so a failed test's state is still there to inspect. The order is forced and the script goes **last**:

1. `reset_framework_state` — release any parked confirmation, cancel retry rows, clear `trusted_senders`.
2. Quiesce: poll until nothing is in a non-terminal status, asking SQLite whether `scheduled_for` has arrived so the comparison happens on the database's clock.
3. `service.reset()` on every service except `model`, then `clear_container_state()`.
4. Quiesce again and install the script behind the endpoint's barrier.
5. Return the watermark; the `stack` fixture stashes it as `stack.mark`.

The script goes last because `script` only holds the barrier across the swap itself. Installing it first and then spending seconds on the slow work leaves this test's turn 0 exposed for exactly as long as the rest takes, which is the defect the barrier exists to close, moved a few lines later.

Four things about this are not obvious and were each found the expensive way:

- **Nothing is truncated.** The daemon is running, and deleting rows underneath its dispatcher is a race. The three exceptions above are forced: a parked `pending_confirmation` blocks its room for two hours, a retry row can fire mid-test and take a scripted turn by a route the barrier cannot see, and a trusted sender changes what every later scenario *means* rather than adding a row to filter past.
- **The retry ladder wedges a naive quiesce.** A failed task is rewritten as `pending` with `scheduled_for` one, then four, then sixteen minutes out. A quiesce filtering on status alone counts that as busy and waits out a backoff it cannot shorten. The barrier's own refusal is a 403 rather than the apter 409 for the same reason: 409 is in neither the transient nor the permanent status set, so the daemon retried it, and the remedy created the row that wedges things.
- **The daemon writes inside the container too.** A host-side stub can rebuild its own state and cannot reach `/data/repos`, where the model cloned on the previous test. Each service declares the container-side directories its use dirties, beside the `config_env()` variable that put the daemon there.
- **`/mnt/shared` is deliberately never cleared.** `.istota-provisioned` lives there and the entrypoint sources it at every boot, so a wholesale clear breaks the stack rather than resetting it. A storage scenario writes under a generated name and asserts on that name.

`nextcloud.reset()` deletes the rooms this object created and nothing else, and it says so by enumeration. The boot leaves far more behind than any test creates and all of it is baseline: `entrypoint.sh` makes a 1:1 room plus `#general`, `#logs` and `#alerts`, seeds `CHANNEL.md` and posts into `#alerts`, and Talk adds a `Talk updates` and a `Note to self` per account of its own accord — six rooms before any scenario runs, two of them receiving the daemon's own log traffic for the whole session. A scenario asserts on a room it made, never on a count.

## What the container shapes concede

`testbed/compose/testbed.yml` is a harness concession file, not a deployment recipe, and its header says so. It is the complete list of ways the stack the `full` tier boots differs from the stack an operator boots, each entry carrying its reason inline:

- `extra_hosts: host.docker.internal:host-gateway` — the name is built in on Docker Desktop and absent on Docker Engine, and nothing in a deployment reaches the host.
- `security_opt: seccomp:unconfined` **and** `systempaths=unconfined`, the pair, on the `istota` service.
- Three credential-shaped brain variables as fixed literals rather than interpolations, on `istota` and on `web`, because the process environment outranks an `--env-file` and a developer's exported `ANTHROPIC_API_KEY` would otherwise reach a test container that POSTs to a listener on their own machine.
- A healthcheck on the `tasks` table. The shipped `istota` service has none, and `restart: unless-stopped` means a container that exits on the 600-second provisioning timeout comes straight back, so "is it running" reads a wedged boot as healthy.

Anything varying per session is in the env-file `StackPool` writes instead: the generated passwords, the `ISTOTA_*_ENABLED` map derived from `Profile.services`, and the ephemeral `NC_PORT` with its matching explicit `ISTOTA_WEB_CALLBACK_URL` (which `provision-nc.sh` bakes irreversibly into the `oauth2_clients` row at first install).

**The two `security_opt` lines are a pair, and neither substitutes for the other.** Seccomp lets bubblewrap *create* the user namespace; it does not let it mount a procfs inside one. Docker's masked `/proc` entries and read-only `/proc/sys` make the container's procfs not "fully visible" to the kernel, which then refuses `mount("proc")` in a nested user namespace, and `build_bwrap_cmd` emits `--proc /proc` on every sandbox. With only the seccomp grant every real sandbox dies at "Can't mount proc on /newroot/proc". `--cap-add=SYS_ADMIN` is not an alternative: measured, it gets past the unshare and fails at `pivot_root`. `docker/docker-compose.test.yml` carries the same pair, plus a fixed fake `ISTOTA_SECRET_KEY`, since bypassing the entrypoint bypasses the thing that generates one.

One consequence runs the other way from expectation: with `/proc/sys` writable, `executor._bwrap_supports_disable_userns` finds `--disable-userns` supported and it reaches the real argv on both container shapes. No scenario asserts anything about nested-userns behaviour, by decision rather than by the flag's absence.

**The shipped `docker/docker-compose.yml` grants neither, so a Docker deployment runs every task unsandboxed.** That is an open product decision, not a settled state: `systempaths=unconfined` unmasks the host kernel's `/proc` to the container, and the supported production shape is bare metal via Ansible, where bwrap unshares the user namespace unasked and neither setting is needed. The daemon at least says so correctly at startup: `_bwrap_available` retries its probe with `--unshare-user` and probes the same mount set `build_bwrap_cmd` emits, so a host that can sandbox is not reported as one that cannot, and an operator who adds both settings gets a working sandbox rather than a probe that lies. Stated in the CHANGELOG and in `docs/deployment/docker.md`.

## The storage backend

`Config.storage_is_nextcloud` is `bool(self.nextcloud.url)`, and both values are shipped install shapes — `local` is what the single-user install runs, not a test convenience. The roadmap is to make Nextcloud optional rather than assumed, so a decoupling change that breaks the Nextcloud-free install has to go red somewhere.

It costs no stack. `storage.py` branches on `use_mount`, not on the backend, and `render-config.sh` writes `nextcloud_mount_path` as the literal `/mnt/shared` on every profile, so briefings, memory and the tasks file take the identical path under both. Exactly three things differ, and all three are pure functions of a `Config`:

| What differs | Witness |
|---|---|
| The prompt's file-tool vocabulary, selected by `storage_backend` | `tests/test_prompt_golden.py`, the `base_nextcloud` / `base_local` pair |
| The skill menu, since `available_capabilities()` drops `nextcloud` on an empty URL | the same pair |
| `runtime.mount_liveness`, `ok` under `nextcloud` and `skip` under `local` | `tests/test_doctor.py::TestMountLiveness` |

Plus one deployment-shaped question, in `tests/test_render_config.py`: does `NC_URL=""` render a config that loads as `storage_backend == "local"`. Set-but-empty, not unset — `render-config.sh`'s preflight is `[ -n "${NC_URL+x}" ]`, which tests whether the variable is set rather than whether it has a value, so an unset `NC_URL` fails the render with exit 2. `APP_PASSWORD` takes the same treatment. Every lean profile renders this way, which is why `runtime.mount_liveness` reports `skip` on the lean shape and why `doctor` assertions there name checks rather than comparing whole payloads.

What this gives up, stated so it can be revisited: nothing asserts that a *booted* local-backend daemon behaves, only that it is configured correctly and assembles the right prompt. Since those three rows are the whole delta, that is a distinction without a consequence today.

## Prompt goldens

`tests/test_prompt_golden.py` runs in the default suite with no container and no model. `execute_task(..., dry_run=True)` returns the fully assembled prompt as the second element of its four-tuple, and twelve cases snapshot it into `tests/golden/prompts/`. A diff is a failure; an intentional change is a reviewed golden update:

```bash
ISTOTA_UPDATE_GOLDEN=1 uv run pytest tests/test_prompt_golden.py -n0
```

`-n0` matters: the orphan check has no ordering relationship with the writers under xdist, so a regeneration that adds a case reports missing goldens from the run that was supposed to create it. The variable is parsed by an `updating()` helper taking the same affirmative and negative sets as `PRECOMMIT_SCANS_REQUIRED` and raising on anything else, because a bare truthiness read would let `ISTOTA_UPDATE_GOLDEN=0` left exported in a shell turn every golden into a rubber stamp.

**`dry_run` returns after assembly rather than instead of it**, so everything assembly calls is live. The first draft of the module reached the network on nine of eleven cases while its own header said it ran against nothing: `read_user_memory_v2` returning None led to `ensure_user_directories_v2` and an OCS share POST, two sockets per Nextcloud-backed case at a ten-second timeout. The rule for whoever adds the next golden case is that a golden path reaching a network socket is a golden that lies about running against nothing. An autouse `_no_sockets` fixture now records, refuses and asserts at teardown — recording rather than only raising, because every caller on that path swallows exceptions for graceful degradation, so a guard that merely raised would be caught and the property would quietly revert to a claim. Turn a live path off through configuration, not through a mock.

Two product gaps are held by named tests here rather than fixed, so a fix arrives as a reviewed golden diff and turns them red rather than passing silently:

- `format_cli_skills` (`skills/_loader.py`) applies neither the capability gate nor the effective disabled set, so a Nextcloud-free install is told `istota-skill nextcloud` exists in the same prompt that omits the skill from the on-demand menu. Filtering it would also drop operator-disabled skills from the model's view while the proxy's own allowlist keeps them executable, which is a behaviour change with its own blast radius. Held by `test_the_cli_tool_list_does_not_apply_the_capability_gate` and by a line in `base_local.txt`.
- `custom_system_prompt` cannot change the assembled prompt, because it is read at brain-request assembly several hundred lines past the `dry_run` return. Not a defect — it is the brain's system prompt, a different thing from the task prompt — and held by an identity assertion that goes red if a future change routes it into the task prompt.

## A probe whose success is indistinguishable from a no-op

The recurring failure in this tier, found four separate times, each time by a control rather than by reading:

- The full shape's readiness probe scanned `/proc/[0-9]*/cmdline` for a string its own `sh -c` command line contained, so it matched itself and returned on the first poll of any container. Verified in a bare `alpine`, which contains no istota at all.
- Three sandbox scenarios asserted that a scripted Bash call's output came back. A task whose sandbox was skipped runs the same command through the same shell and returns the same bytes, so all three stayed green through the entire period both container shapes ran every task unconfined. The distinguishing assertion has to name something only the mechanism can produce: `tests/smoke/test_sandbox_in_stack.py::TestTheDatabaseMasks` requires `stat -f` to report `tmpfs` at `db_path.parent`, the directory to be empty, the framework database to fail to open, and a `touch` to be refused, with an in-session control running the same probe outside the sandbox and requiring the opposite answer.
- Three wire-level email cases meant to carry RFC 2047 encoded words were sending plain text, because `EmailMessage` under `policy.SMTP` decodes an encoded word on assignment for any header the registry does not know. The test source contains the encoded string; the decoding happens between the assignment and the wire.
- The first compose-passthrough assertion could not fail, because the generator's default for `confirm_sender_match` was the value the profile asked for.

So: on a tier asserting against an artifact, reading the test tells you almost nothing about whether it can fail. Run the control, and write down what it turned red.

## Environment

There is no variable naming the checkout a stack builds from. `LeanShape` and `FullShape` take their `compose_file` as an argument and `tests/conftest.py` supplies it, which is what lets a consumer outside this repository point the same pool at its own files.

| Variable | Effect |
|---|---|
| `ISTOTA_TESTBED_KEEP` | keep stacks up after the session and persist the Nextcloud and postgres volumes plus the generated passwords. Keeps `shared_files`; wipes `istota_data` and `redis_data`. `tests/full/` refuses to run under it, because every assertion there is about first-install state |
| `ISTOTA_TESTBED_MAIL_IMAGE` | override the pinned Maddy digest |
| `ISTOTA_IMAGE_TAG` | use a prebuilt image instead of building one; how the upgrade tier's negative control is fed |
| `ISTOTA_UPDATE_GOLDEN` | rewrite the prompt goldens instead of comparing |

`KEEP` does **not** wipe `shared_files`, and the reason is worth knowing before someone "fixes" it: that volume holds `/mnt/shared/.istota-provisioned`, and `provision-nc.sh` will never rewrite it, because it is mounted as a `post-installation` hook and `nextcloud:30-apache` runs those only when the installed version is `0.0.0.0`. Wiping it leaves `entrypoint.sh` waiting 600 seconds for a flag nothing will write, exiting 1, and `restart: unless-stopped` doing that forever. The host port is also pinned across kept sessions, since the OAuth2 redirect URI is baked at first install and not revisited.

`KEEP` is implemented and unit-tested but has never been exercised across two real sessions. The measured cold boot is what makes it unnecessary rather than unproven.

## Costs, measured

- Lean tier: six stacks per session (`base`, `forge`, `no-forge`, `notify`, `feeds`, `mail`), about 165 seconds. Per-profile boot is 6.5 to 9 seconds, and per-test setup after that is about 0.7 seconds. Before session scope it was 180 seconds for fourteen tests at one boot each.
- Full tier: one cold boot of six containers, 50 to 84 seconds to both healthchecks on warm base images. Nextcloud is healthy before `up` returns, because `istota` declares `depends_on: service_healthy` and compose waits.
- `docker compose exec` is about 31% of a lean session (123 to 127 ms a call) and 5 to 8% of a full one — not because it got faster, but because a session with a cold boot in it has a much larger denominator. No optimization was built; the counters stay and the tier prints the fraction at the end of every run.

## Still open

- **The external rigs do not consume this package yet.** istota-demo and istota-redteam still carry their own copies of the mail overlay, cert generation, readiness polling and the seeder-copy framework. `testbed/` is installable and its wheel now ships `compose/`; pointing both rigs at it is hand-driven work in two separate repositories.
- A Docker deployment's sandbox posture, above.
- The positive half of ISSUE-245 on a deployed shape — a confirmation prompt that actually reaches a surface. Reachable on the full profile, which has Talk and an auto-provisioned `alerts_channel`; the lean shape has no surface by construction.
- Feeds image dedupe. Its only caller is the authenticated web reader, so a deployed-path scenario cannot reach it until the Svelte components carry `data-testid` hooks.
- `Probe` cannot read a *module* database, so the feeds scenario asserts through the CLI's own output rather than on rows.
- Per-session image tags (`istota-test/istota:*`, `istota-test/no-forge:*`, and the full shape's per-project `<project>-istota` and `-web`) are built by every session and removed by nothing. They accumulate; remove them by hand.
- A second `entrypoint.sh` wart, found while fixing the first: with location enabled, deleting `.api-provisioned` regenerates `LOCATION_INGEST_TOKEN`, but the config render is gated on `config.toml` not existing, so the config keeps the old token and the flag file records one nothing reads. Unreachable from this tier, since the full profile leaves location off.
