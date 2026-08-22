# Testing

Istota uses TDD with pytest and pytest-asyncio. The Python suite has roughly 13,900 tests across ~414 files; the frontend has its own vitest suite under `web/`.

Almost all of those tests assert against Python objects on a developer's host, which for most people is macOS. That is the right default and it has one blind spot: it cannot observe what actually runs in production — a built image, a rendered `config.toml`, a `PATH`, a bubblewrap namespace. The five discretionary tiers under "Deployment tiers" below cover that, and none of them runs unless you ask for it.

## Running tests

```bash
scripts/qtest uv run pytest                                  # the default suite
uv run pytest tests/test_doctor.py                           # one file, no semaphore needed
uv run pytest tests/ --cov=istota --cov-report=term-missing  # coverage
```

`addopts` in `pyproject.toml` pins `-n auto`, so the suite runs under pytest-xdist by default. New tests must be order-independent. For a local edit loop, `--testmon -n0` reruns only what your change touched; `-v` is only readable with `-n0`, since xdist interleaves worker output.

Wrap a full suite run in `scripts/qtest`. Both this suite and vitest size their worker pool from `cpu_count()`, so each run claims the whole machine — correct for one run and pathological for several, which is what happens with work spread across parallel git worktrees. `qtest` is a `flock` semaphore holding one machine-wide slot; it queues the run rather than letting three jobs ask for 36 workers on 12 cores. Exit code 75 means no slot came free and the command did not run, which is not a test failure. A single test file needs no slot, and neither do `ruff`, `svelte-check` or `format:check`.

Six marker sets are deselected by default (also via `addopts`), each with a different prerequisite, so they are selectable independently:

| Marker | Needs | Runner |
|---|---|---|
| `integration` | a live Nextcloud instance, or Garmin credentials | `uv run pytest -m integration` |
| `live` | a real LLM API key; costs money | `uv run pytest -m live` |
| `linux` | a real Linux kernel and a usable bubblewrap | `scripts/test-linux.sh` |
| `image` | a Docker daemon | `uv run pytest -m image -n0` |
| `smoke` | a Docker daemon | `uv run pytest -m smoke -n0` |
| `full` | a Docker daemon, and the network — see below | `uv run pytest -m full -n0` |

A seventh marker, `requires_dac`, is not deselected: it skips itself when the process can bypass permission bits, which is what happens as root inside the Linux runner.

`image`, `smoke` and `full` must run with `-n0`. Their fixtures are session-scoped and build one tagged image; N xdist workers would each race to build it, and on the two compose tiers would also bring up their own stacks under one project prefix and sweep each other's projects. The conftest fails the session with that reason rather than letting it happen.

**Two shapes, one seam.** `smoke` and `full` are the same fixtures over different compose files. The *lean* shape (`docker/docker-compose.test.yml`) is one container with the entrypoint bypassed and the config rendered on the host: seconds to boot, right for a subsystem whose external is an HTTP endpoint. The *full* shape (`docker/docker-compose.yml` plus `testbed/compose/testbed.yml`) is the deployment as shipped — postgres, redis, nextcloud, istota, web, nginx — booted through `entrypoint.sh` with the generator running inside the container. It is the only thing that executes `provision-nc.sh` or reaches the half of `entrypoint.sh` past the config write. Read `testbed/compose/testbed.yml` before adding to it: it is a list of harness concessions, and each one is there with its reason.

**The full tier needs the network, and it is worth knowing which way that fails.** `provision-nc.sh` runs `app:enable spreed`, `calendar` and `files_external`; only the last is bundled in `nextcloud:30-apache`, and the other two are fetched from the app store at first install. Every `occ` call in that script is `|| true`, so an install with no network writes its provisioning flag and reports success having enabled nothing. `tests/full/test_provisioning.py` asserts outcomes by name for exactly that reason.

## Deployment tiers

Five discretionary tiers, none of them automatic, each answering "does the artifact match what the code assumes?" rather than "does the code do the right thing?".

```bash
scripts/test-linux.sh                        # the suite + the linux tests, on a real kernel
uv run pytest -m image -n0                   # the built image's contract
uv run pytest -m smoke -n0                   # end-to-end against the lean compose stack
uv run pytest -m full -n0                    # end-to-end against the full stack, incl. a real Nextcloud
scripts/test-upgrade.sh                      # the current image over an older release's state
```

When to run each:

| Tier | Run it when | Cost |
|---|---|---|
| `scripts/test-linux.sh` | you touched the sandbox, the network proxy, the skill proxy, or anything else whose behaviour differs on Linux | minutes; the whole suite, in a container |
| `-m image` | you touched `docker/istota/Dockerfile`, `render-config.sh`, or anything about where a binary lives | under a minute against a warm layer cache |
| `-m image --platform amd64` | before a release | about ten minutes under emulation, and it is the only thing that ever executes the amd64-only devbox image |
| `-m smoke` | you touched the developer skill's forge chain, the entrypoint, or the compose stack | about a minute against a warm layer cache: one stack per profile rather than per test, so most of it is the three boots |
| `-m full` | you touched `entrypoint.sh`, `provision-nc.sh`, `docker-compose.yml`, or anything about first-boot provisioning; and before a release | about a minute and a half against warm image and layer caches, most of it Nextcloud installing itself on a cold volume set |
| `scripts/test-upgrade.sh` | you touched a migration, a config key, or `config.toml` generation | seconds against a cached capture |
| `scripts/test-upgrade.sh --from-floor --shape volume` | before a release | seconds, plus one container the first time |

Two of these carry a negative control, and the controls are not a formality — on a tier that asserts against an artifact, reading the test tells you almost nothing about whether it can fail. `scripts/test-image-negative-control.sh` builds the image with the forge binaries removed and requires the image tier to go red against it. The upgrade tier's control is the same image passed through `ISTOTA_IMAGE_TAG`:

```bash
ISTOTA_IMAGE_TAG=istota-test/no-forge:control uv run pytest -m image -n0 tests/image/test_upgrade.py
```

A clean run there is the failure.

### Shared machinery, and how to add a tier

The pieces under `testbed/` are general, not forge-specific. The forge chain is the first thing to use them, and it should not be the last:

- `stack.py` — bringing a compose stack up and down, waiting for a service to report healthy, sweeping leftovers from an interrupted run, and `Stack`, which is what a scenario is handed: `submit`, `script`, `exec`, `doctor`, `restart`, `logs`, `diagnostics`.
- `probe.py` — reading the framework database of a stack that is currently running, or of a local file.
- `httpstub.py` and `services/` — the `Service` protocol every external the daemon talks to conforms to, and the shared `ThreadingHTTPServer` base under the ones we wrote. `services/model_endpoint.py` is a deterministic model endpoint serving canned turns over HTTP, so a task's path through the daemon is reproducible without an LLM; `services/gitlab.py` answers enough REST v4 for `glab` plus a real git over HTTP.
- `profiles.py` — what a scenario declares it needs: a shape, a set of services, and any extra config.

`tests/support/upgrade.py` — capturing an older release's `config.toml` and schema — deliberately stayed where it is. It belongs to the upgrade tier, and `scripts/test-upgrade.sh` reaches it by string path.

`testbed/` sits beside `src/` rather than inside `tests/` because it is not part of the shipped application and two repos outside this one consume it. It has its own `pyproject.toml` and imports no pytest, so a failure surfaces as a raised `StackError` rather than as a call into a test runner that is not installed. Two rules bind anything added to it: a service may only point the daemon at itself through a variable `docker/istota/render-config.sh` reads **and** `docker/docker-compose.yml` passes through (add one to both as a reviewed product change if it is missing — never side-load config from the fixture), and a stub bound to anything but loopback must be given a credential to expect. Both are enforced in `tests/test_testbed_services.py`. So when a new subsystem needs an end-to-end tier, extend `docker/docker-compose.test.yml`, write a service, and reuse these — don't build a second stack alongside them.

`testbed/services/model_endpoint.py`'s wire format has its own tests in the default suite (`tests/test_model_endpoint.py`), pinned against the real provider over a real socket. That matters more than it looks: nothing in a smoke test can tell a correctly framed stream from a subtly wrong one — a stream missing its completion signal arrives as a task that failed for a reason unrelated to what the test was asserting.

### The upgrade tier's two anchors

`scripts/test-upgrade.sh` boots the current image over an older release's `config.toml` and database. It exists because two of istota's three upgrade shapes never regenerate `config.toml`: the auto-update cron resets to main every two minutes without running Ansible, and a Docker rebuild over a retained volume keeps the config the entrypoint wrote on that volume's first boot. Every other tier renders a fresh config, and a fresh config is current by definition.

- **Near anchor**, the default: the merge-base with the default branch. That is about three days at the current release cadence — close to a no-op as a regression detector on its own, but it is the span the auto-update cron actually crosses, and it is cheap.
- **Far anchor**: the tag in `scripts/upgrade-floor`, roughly a month back. That file is the statement of how far back an upgrade is supported. Bump it deliberately, and never to make a red run green without reading why it went red.

```bash
scripts/test-upgrade.sh                                 # near anchor, code shape
scripts/test-upgrade.sh --from-floor --shape volume     # far anchor, before a release
scripts/test-upgrade.sh --shape both
scripts/test-upgrade.sh --from v0.38.0 --shape volume   # reproduce a specific report
```

The assertion is not "reproduces ISSUE-263". `resolve_real_bin`'s fallback to the image's own binary directory is what makes such an upgrade clean, so that criterion is unreachable against current code — the fix working, not a gap in the test. What is asserted is that no check fails in either shape, and that `developer.forge_config_drift` reports `WARN` naming both the stale configured path and the resolved one on the retained-volume shape. That warning is the signal ISSUE-263 never had.

## The frontend suite

`web/` is checked independently of Python — a change touching only one half need
only run that half:

```bash
npm --prefix web run lint:design   # design-language lint (raw colours, tokens)
npm --prefix web run check         # svelte-check
npm --prefix web run test          # vitest run
npm --prefix web run format:check  # prettier
```

Needs `npm ci` in `web/` first. There is no wrapper script over the two halves —
`.claude/verify.sh` was tried and removed, because scoping and runner fallback
inside a wrapper hid which runner produced a failure. Run the commands directly;
the full set is listed in `AGENTS.md`.

## Test patterns

**Real SQLite via `tmp_path`**: No database mocking. Tests create real SQLite databases initialized from `schema.sql`. This catches actual SQL issues that mocks would hide.

**`unittest.mock` for external dependencies**: HTTP calls, subprocess invocations, and file system operations outside the test directory are mocked.

**Class-based tests**: Tests are organized in classes grouping related scenarios.

## Shared fixtures (`conftest.py`)

| Fixture | Purpose |
|---|---|
| `db_path` | Initialized SQLite database from schema.sql |
| `db_conn` | Database connection |
| `make_task` | Factory for creating test tasks |
| `make_config` | Factory for creating Config objects |
| `make_user_config` | Factory for creating UserConfig objects |

Three autouse fixtures apply to every test whether you ask for them or not: `_no_network_symbol_lookups` (fails a test that tries to resolve a ticker symbol over the network), `_reset_async_runtime_singletons` (drops the persistent asyncio loop and pooled HTTP client between tests), and `_reset_expunge_warning_latch` (clears the once-per-process IMAP expunge warning).

## Testing skills

Skill loader tests require isolation from bundled skills:

```python
# Pass bundled_dir to isolate from bundled skills
index = load_skill_index(skills_dir, bundled_dir=_empty_bundled(tmp_path))
```

Executor tests set `bundled_skills_dir` on the Config object to an empty directory to isolate from bundled skills.

## TDD workflow

For new features:

1. Read existing codebase structure and test patterns
2. Write failing tests covering happy path, edge cases, and error handling
3. Run tests to confirm they fail
4. Implement the feature
5. Run tests and iterate until all pass
6. Run `ruff check --output-format concise src tests testbed`, plus the `web/` checks above if the change touched the frontend
7. Commit
