# Models and providers

Istota keeps one list of the models it can run, the catalogue, and reaches each model through a provider: one endpoint and the key for it. This page covers how both are configured, who may pick which model, and how to move an install that predates providers.

## Providers

A provider has an id, a `kind`, a base URL and a key. Two are built in and need no table:

| Id | Kind | Base URL |
|---|---|---|
| `anthropic` | `anthropic` | `https://api.anthropic.com` |
| `openrouter` | `openrouter` | `https://openrouter.ai/api/v1` |

A provider existing does not make any brain use it. The native brain uses the one `[brain.native] provider` names, and that setting has no default: a native brain with no provider is unavailable, and doctor's `runtime.native_brain` names the field to set. The `claude_code` brain always uses `anthropic`, because the `claude` CLI talks only to Anthropic.

An endpoint of your own, such as a local server or a gateway, is a provider of your own. Its id is lowercase letters, digits and `_`, starting with a letter, and its table sets `kind` and `base_url`:

```toml
[providers.local]
kind = "openai_compat"            # anthropic | openrouter | openai_compat
base_url = "http://192.0.2.10:8080/v1"
api_key = ""
discover = true
models = [ { id = "qwen3-32b", context_window = 32768, reasoning = true } ]

[brain.native]
provider = "local"
model = "qwen3-32b"
```

A table with a built-in id sets fields on the built-in; giving a built-in another `kind` is refused. A table that cannot be used (an unknown kind, a base URL that is not http or https, a model without an id) is left out while the rest of the config loads. `istota apply --check` refuses it and doctor's `models.catalogue` fails on it.

The `kind` decides what the endpoint is sent: cache breakpoints by default for `anthropic`, the reported-cost request and attribution headers for `openrouter`, neither for `openai_compat`. `prompt_caching`, `prompt_cache_key`, `prompt_cache_ttl`, `strict_tool_schemas` and `reasoning_replay` can each be set on the provider's table and win over the kind's default. The [native brain runbook](native-brain.md) describes each one.

### Keys

**A request to a provider carries that provider's key**, whichever brain sends it.

- The deployment's key for provider `<id>` is `[providers.<id>] api_key`, overridden by `ISTOTA_PROVIDER_<ID>_API_KEY` (the id upper-cased). Extra headers work the same way through `ISTOTA_PROVIDER_<ID>_EXTRA_HEADERS`, a JSON object that replaces the table's.
- On the stack, the built-in providers' keys and headers are secret files: `istota_provider_anthropic_api_key`, `istota_provider_anthropic_extra_headers`, `istota_provider_openrouter_api_key` and `istota_provider_openrouter_extra_headers`. `istota setup` writes them, and the web configuration page moves an edited key into its file.
- A provider of your own keeps its key inline in `config.toml` (mode 0600, owned by the daemon's user, shown redacted in the admin configuration page), because the stack's secret files have fixed names.

### Which providers a brain reaches

A provider existing, or holding a key, does not let a brain use it. The `claude_code` brain reaches the `anthropic` provider only. The native brain reaches its default provider, `[brain.native] provider`, and any provider listed in `[brain.native] providers`:

```toml
[brain.native]
provider = "openrouter"
providers = ["local"]              # opt-in; `provider` is always included
```

An `anthropic` key held for the `claude` CLI does not make Anthropic a native provider: on an OpenRouter-native install that does not list it, `anthropic/claude-opus-...` is OpenRouter's id and is billed by OpenRouter, whether or not OpenRouter's model list has been fetched.

### A room's provider

A native room or scheduled job runs on one provider, stored beside its model: the room's own when it has one, else `[brain.native] provider`. Its model is an id of that provider (or an alias), so a task is never sent to a provider because of how its model is spelled. Choosing the provider is admin-only, because it decides whose key is billed, and only the providers `[brain.native] providers` lists are offered:

- `!room provider <id>` in the room, or `!room provider default` to go back to native's default. `!room` shows the room's provider.
- The Provider select in the web room settings, shown to an admin on a native room. The model list below it is that provider's.
- `provider = "<id>"` on a CRON.md job. A non-admin's is dropped when the file is read, as a non-admin's `brain` is.

An admin's `provider/id` in `!room model`, the room settings, `!model` or a CRON.md `model` is split into the two: `!room model local/qwen3-32b` moves the room to `local` and stores `qwen3-32b`. Anyone else picks a model within the room's provider; naming another provider's model is refused. A room moved to another provider keeps its model only if that provider has it, and a move that would leave the room with neither a model nor the provider's default model is refused.

Each provider's default model is `[providers.<id>] default_model`, what a room on it runs with no model of its own. Native's default provider uses `[brain.native] model`. A tier (`fast`, `general`, `smart`) with no target for the room's provider means that provider's default model too.

A room or job pinned to a provider the list no longer holds runs on native's default provider, with a warning in the log, and doctor's `models.catalogue` names each such room and job. The pin comes back into force if the provider is listed again. A room whose brain moves to `claude_code` loses its provider, since that brain runs on `anthropic` alone.

### A user's own key

A user may hold their own key for a deployment provider: `istota secret ensure -u <user> -s provider_keys -k <provider id>`. In that user's tasks, a request to that provider carries their key instead of the deployment's: native's default provider, or the listed provider the task runs on. Requests to other providers still carry the deployment's keys, and so do internal calls made for the operator (the speech gate, conversation triage, the sleep cycle, briefings). Under `auth = "api_key"`, a user's `anthropic` key also reaches the `claude` CLI in their own tasks. A user cannot add an endpoint, only a key for one the deployment has. `istota init` moved the old per-user native keys (`native_brain`/`api_key`) to `provider_keys/<native's provider>` once.

## How the claude CLI authenticates

`[brain.claude_code] auth` decides how the `claude` CLI signs in:

- `"subscription"` gives the CLI the Claude subscription token (`CLAUDE_CODE_OAUTH_TOKEN`, the `claude_code_oauth_token` secret file) and never an API key. The token is only ever sent to Anthropic, so a non-default `[providers.anthropic] base_url` makes the CLI brains unavailable under this mode.
- `"api_key"` gives the CLI the `anthropic` provider's key and base URL, and never the token. A gateway in front of Anthropic is the `anthropic` provider with the gateway's `base_url` and key. The CLI runs outside the sandbox, so its host is not added to the sandbox's network allowlist.

Unset, the mode is `"subscription"` when the token is in the environment. An Anthropic key alone never switches an install to API billing; write `auth = "api_key"` for that.

Choose `"subscription"` when the deployment runs on a Claude plan, and `"api_key"` when it pays per token. Two things to know about `"api_key"`:

- The CLI holds the key in its own environment. Its tools run in the sandboxed tool server, which does not hold it, so on a sandboxed deployment the model cannot read it, as it cannot read the subscription token under `"subscription"`. On an unsandboxed deployment a Bash call can read any process of the daemon user, the CLI included, so use a key from a workspace with a spend limit there.
- A gateway that accepts only a bearer token (`ANTHROPIC_AUTH_TOKEN`) is not supported. The CLI is given `ANTHROPIC_API_KEY` only.

The CLI's environment is built from config alone. Nothing credential-shaped is copied from the daemon's environment, and listing a model credential such as `ANTHROPIC_API_KEY` in `[security] passthrough_env_vars` is refused with a warning. `istota doctor`'s `models.cli_auth` check reports the mode and whether it was written or derived.

## The catalogue

Every surface that names a model reads one catalogue. A model is named by a reference, `provider/id`: `anthropic/<id>`, `openrouter/<vendor>/<model>`, or `local/qwen3-32b`. Where a model's details come from, highest first:

1. `[models.overrides."<ref>"]`, per-field patches you write. An override naming a model nothing lists is ignored, and doctor warns.
2. A provider's declared `models`.
3. The provider's discovered list, fetched in the background.
4. The shipped Anthropic list, a file in the istota package.
5. A conservative default (200k context, no price) for a model nothing knows.

```toml
[models.overrides."openrouter/z-ai/glm-5.3-flash"]
context_window = 202752
reasoning = true
```

### The shipped Anthropic list

Istota ships a list of Anthropic's chat models with their limits, prices, effort levels and families. It is regenerated by `scripts/update-anthropic-catalog.py` at each Anthropic release, and a release is refused while the committed list is out of date. No setting, example or document in istota names a current model id; they name a family (`opus`, `sonnet`, `haiku`) or an alias instead, and `!models` shows what each resolves to today.

### Discovery

The daemon fetches provider model lists in the background, never inside a task. A `model-catalogue` check every 15 minutes refreshes any list older than the provider's `discover_ttl_hours`, and at a first start the providers with no list yet are fetched together within ten seconds before tasks run. Each list is cached in `model-catalogue/<provider>.json` beside the database. The native brain and the web process only read those files.

- The built-in `openrouter` provider is fetched only when native uses it or it has a deployment key, so an install that never configured OpenRouter sends it nothing.
- A provider of your own is fetched whenever `discover` is on. Its `/models` endpoint gives ids only, so fill in context windows and reasoning with `models` or `[models.overrides]`.
- Anthropic's own list is fetched only with a model-listing key (below). It adds new models and marks retired ones as soon as Anthropic lists them; prices still come from the shipped list, and a model the shipped list lacks is costed at zero until it is added.

A failed fetch keeps the previous list, an empty answer is treated as a failure, and a model that drops out of a list stays known for seven days. The admin dashboard's Models card shows each provider's last fetch and last error, and doctor's `models.catalogue` check reports stale lists and refused keys.

### The model-listing key

Anthropic's model list needs a key, and a deployment on a Claude subscription has none. The model-listing key is a key used for that one request, `GET https://api.anthropic.com/v1/models`, and nothing else. It never reaches a model request, a task, a skill or the `claude` CLI.

- **Create it in an Anthropic Console workspace with a spend limit of zero.** Listing models costs nothing, so a key from such a workspace cannot bill anything even if it were read.
- It is read from a file only, never from config text or an environment variable: on the stack the secret file `istota_anthropic_listing_key`, mounted on the daemon alone; on another shape the path in `[models] anthropic_listing_key_file`. Doctor checks that the file is mode 0400 or 0600 and owned by the daemon's user.
- To set it, answer the optional question in `istota setup` (on a terminal, in the web setup's model step, or `--anthropic-model-listing-key-file PATH` with `--yes`), or on a running stack write it with `istota-stack put-files` (`{"secrets": {"istota_anthropic_listing_key": "<key>"}}` on stdin). Admin > Configuration can set it too: the value goes to the host the way the page's other credentials do, and `istota-stack config-apply` writes the file as the daemon's user. The web process never mounts the file, so the page shows whether it holds a key as the daemon's last doctor run found it, and "Unknown here" until the daemon has run one.
- An API-billed deployment that wants discovery puts a key in this file too; it may be the same value as the inference key.
- The request goes straight to Anthropic and ignores `HTTPS_PROXY`, like the subscription usage poll. On a deployment that reaches the internet only through a proxy, the fetch fails and doctor reports it as a fetch error.

Leave the file empty to use the shipped list alone.

## Choosing a model

### Aliases and references

A selection is an alias or a model:

- **Aliases.** The tiers `fast`, `general` and `smart` (shipped as `haiku`, `sonnet` and `opus`), the family shortcuts `opus`, `sonnet` and `haiku`, `default`, and any you add in `[models.aliases]` ([reference](reference.md#modelsaliases)). A family is the newest current model of that family in the catalogue, so a new Anthropic model reaches the tiers when the catalogue lists it. On the native brain an unremapped tier means the room's provider's default model.
- **Per-provider targets.** `[models.aliases.<name>.<provider id>]` gives an alias a target on one provider. `anthropic` is the CLI brains' (and a native room's on `anthropic`); any other id is a native room's on that provider. An alias with no target for a room's provider cannot be picked there and never resolves into another provider. The old `openai_compat` key is read, for one release, as native's default provider.
- **A reference**, `provider/id`. A bare id also works where only one provider lists it; where several do, the brain's own provider is preferred, and otherwise the choice is refused with the candidates listed.
- **A pattern**, a fragment of an id or name, in `!model` and `!room model` only. Never in a config file or CRON.md, where a later catalogue refresh would change what it means.

Any selection may carry `:effort` (`smart:high`, `anthropic/<id>:max`).

### Who may pick what

An admin may pick any model in the catalogue. Anyone else may pick from the enabled set:

```toml
[models]
enabled = ["anthropic/*", "openrouter/z-ai/glm-5*", "openrouter/moonshotai/kimi-k2*"]
```

The entries are case-insensitive globs over references; an entry may carry `:effort`, which is the effort offered with that model and does not narrow the match. Unset, the set is `anthropic/*` plus native's own model, which is what a deployment ran before. A deprecated model is matched only by an entry naming it exactly. Aliases are always selectable, by everyone, because the operator wrote them. Doctor warns about an entry that matches nothing.

The bound is on who may set a selection, not on what runs: a room an admin set to a model outside the enabled set keeps running it for every member, and a member sees it as set by an admin. `!model`, `!room model`, the web room settings and CRON.md all apply it. A refused room change answers with a reason and, for an ambiguous choice, the candidates; a non-admin's refused CRON.md model is dropped at sync and the job runs on the brain's default. `!models` lists the viewer's set by provider, and an admin also gets `!models all <provider>`.

### What is stored

A room or a job stores what was picked, an alias name or a model id, and its provider beside it; both are resolved when each task is created, within that provider only. A room set to `smart` therefore moves to the next Opus on its next task after the catalogue lists one, and a room set to an exact model stays on it. The task copies the provider and records the id it resolved to, except for a portable alias (the tiers, and an alias marked `portable = true`), whose name it keeps so that a fallback to another brain resolves it there; the model that ran is recorded for every task. A retry or a subtask keeps the task's provider.

A fallback between brains always changes provider. An exact model crosses only to a brain that reaches the provider it ran on (a `claude_code` model to native when `[brain.native] providers` lists `anthropic`), and is otherwise dropped: the fallback runs its own default model, and the answer says so. A portable alias is resolved again on the fallback brain, on the room's native provider when the fallback is native and the room has one. Rooms and jobs set before this release have no provider, which means the brain's default provider, so they run where they ran.

### Effort

Effort is clamped to what the chosen model supports, on every brain. A model without reasoning gets none. `xhigh` and `max` are offered only where the model's catalogue entry names them. A level the model lacks moves up to the nearest one it has, and only then down: `xhigh` on a model that tops out at `high` runs at `high`, and `low` on a model whose lowest level is `medium` runs at `medium`. Each brain clamps where it sends the request, after filling its own default, so a task with no effort of its own and a task rerun on the fallback brain are clamped too. The level you asked for is what the room and the task keep; the task's trace notes the clamp.

### The admin configuration page

Every config field that names a model (`[brain.claude_code] model`, `[brain.claude_code] advisor_model`, `[brain.native] model`, the triage, speech-gate, sleep-cycle and review models) is set on the admin configuration page from two lists, a source and a model from that source, with an effort list where the field takes one. `[brain.native] model` offers native's default provider alone, since another provider's default is its own `default_model`; the internal models offer `anthropic` and the providers `[brain.native] providers` lists. `[models] enabled` and `[models.aliases]` have editors of their own. A value the catalogue does not know is kept and shown as such, so saving another field never changes it, and "Other…" accepts an id a provider has not listed yet.

`istota apply` and the page's preview check every model field the document sets. A field naming a provider that is not configured is refused, on the fields that only Anthropic models fill (`[brain.claude_code] model` and `advisor_model`). Everywhere else, including `[brain.native] model`, whose OpenRouter ids contain a slash, a value that does not resolve is a warning: an id a provider lists but istota has not fetched yet is still a valid choice.

## Moving an install that predates providers

This release still reads the old settings, for one release, and reports each one in use:

| Old source | Read as | Move it to |
|---|---|---|
| `[brain.native] base_url`, `api_key`, `extra_headers`, the wire settings | a provider implied by the host: `api.anthropic.com` is `anthropic`, `openrouter.ai` is `openrouter`, anything else a provider called `native` | `[brain.native] provider = "<id>"`, and a `[providers.<id>]` table for your own endpoint |
| `[brain.native] model_catalog_fetch`, `model_catalog_cache_ttl_hours` | the provider's `discover`, `discover_ttl_hours` | `[providers.<id>]` |
| `[brain.native] model_overrides` | `[models.overrides]` | `[models.overrides."<provider>/<id>"]` |
| `ISTOTA_BRAIN_NATIVE_API_KEY`, `ISTOTA_BRAIN_NATIVE_EXTRA_HEADERS` (the `istota_brain_native_*` secret files) | the implied provider's key and headers | `ISTOTA_PROVIDER_<ID>_API_KEY`, `_EXTRA_HEADERS` (the `istota_provider_<id>_*` files) |
| `ANTHROPIC_API_KEY` (the `anthropic_api_key` secret file) | the `anthropic` provider's key, and `auth = "api_key"` when no subscription token is set and `auth` is unset | `istota_provider_anthropic_api_key`, and `[brain.claude_code] auth = "api_key"` |

A current setting always wins over an old one. When `ANTHROPIC_API_KEY` and a native key on `api.anthropic.com` are both set and differ, native gets a provider called `native` with its own key and `anthropic` keeps `ANTHROPIC_API_KEY`, so both keep doing what they did. `istota doctor` (`runtime.native_brain`, `models.cli_auth`) and `istota apply --check` name each old source still in use and what to write instead. Nothing is logged at config load.

`istota setup` does the move for you: a re-run reads each answer from its current setting and, where that is empty, from the old one, writes the current config and secret files, and copies the config it replaces to `config.toml.pre-providers`. The old secret files are left as they are. The standalone and bare-metal importers still write an imported `ANTHROPIC_API_KEY` to the old `anthropic_api_key` file, which this release reads; run `istota setup` again after an import to move it.

A config with neither an old `[brain.native] base_url` nor a `provider` used to mean Anthropic. It now leaves the native brain unconfigured: set `provider = "anthropic"` and the `anthropic` provider's key to keep the old behaviour.

The release after this one stops reading the old settings and drops the three old secret files.

### Rolling back

This release adds a provider column to rooms, scheduled jobs and tasks, and stamps the database schema 2. A release from before it refuses to start on that database (`istota init` exits 3, and `istota-stack rollback` checks first and stays put), because it would ignore every stored provider and send those rooms' models to native's default provider, billed to another key. There is no rolling back across this release without restoring a database backup taken before the upgrade.

For an install moved from before providers, the release before that one reads neither `[providers]` nor a provider id in `[brain.native] provider`: it ignores the first and its native brain fails on the second, and its CLI brains do not read `[brain.claude_code] auth`. To roll back across this release, restore `config.toml.pre-providers` (or the copy `istota apply --backup` kept) before starting the older release. The old secret files still hold their values, so nothing else is needed.
