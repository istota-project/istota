# Declarative settings

`istota snapshot` exports application settings as a plan that `istota apply` can inspect and reconcile. Use an initialized install and name its configuration with `-c` or `ISTOTA_CONFIG_PATH`.

```bash
istota -c /data/config/config.toml snapshot -o settings.toml
istota -c /data/config/config.toml apply -f settings.toml --dry-run
istota -c /data/config/config.toml apply -f settings.toml
```

Apply exits **0** for unchanged settings, **2** for changes made or proposed by a dry run, and **1** for refusal or execution failure. Snapshot exits 0 after writing the complete document and 1 on refusal. Automation must accept apply's exit 2 as success.

## What a snapshot contains

A snapshot includes the raw configuration document, live shared briefing definitions, and every stored user's restorable profile fields, folder resources, briefing schedules and ordered blocks/sources, scheduled jobs from CRON.md, heartbeat definitions from HEARTBEAT.md, and provisionable credential declarations. It includes explicit defaults and empty collections so a destination can clear stale values.

It omits task and delivery history, generated briefing content, heartbeat history, native login identities, WhatsApp enrollment, consent, credential values and deployment inputs. Referenced scripts, prompts and workspace files are not bundled. Use the backup and migration tools for recovery or a whole-install move; a settings plan cannot reconstruct a VM, remote accounts or runtime databases.

Snapshot reads definitions without initializing stores, seeding profiles or updating credential access timestamps. It checks definitions and file revisions again in fresh reads, retries the complete gather once if they changed, and refuses a second change with `state_changed`. This detects concurrent settings edits; it is not a coordinated database backup. A missing optional briefing database is empty. A corrupt store, pending definition migration or unmaterialized configuration user is a refusal, not an empty export.

## Output and scope

```bash
istota -c /data/config/config.toml snapshot --format json -o settings.json
istota -c /data/config/config.toml snapshot --user alice --user bob
istota -c /data/config/config.toml snapshot --no-meta
```

TOML is the default. Without `-o`, or with `-o -`, the document goes to stdout; diagnostics go to stderr. A file is replaced atomically with mode 0600 only after gathering and validation succeed. Non-regular output files and symlinks are refused. Unchanged settings produce the same bytes in the same format.

`--user` is repeatable. It exports only the named users and omits instance configuration and shared briefing definitions. A scoped snapshot warns about shared briefing references that need definitions at the destination. Unknown users are refused. Apply's `--user` likewise selects declared users and excludes instance sections.

The stable metadata identifies the document as a snapshot. Profiles use `profile_mode = "update"`, so existing managed-field locks survive. Snapshot metadata preserves the destination's configuration ownership marker. `--no-meta` removes that metadata; applying such a document uses ordinary plan ownership defaults. To adopt settings as managed configuration, explicitly set `meta.managed = true` and select `profile_mode = "managed"` for the profiles to manage.

## Missing fields and pruning

An absent section is untouched. An absent user is never deleted. Profile updates affect only supplied fields; explicit empty strings, lists, maps and false values reset fields where the domain permits them. Configuration is a whole-document replacement, so omitted configuration fields take loader defaults.

Named collections merge by identity. Briefing blocks and sources use their array positions: supplied entries replace the corresponding prefix, and existing tails survive unless pruning is enabled. Supplied block options and source configuration replace those mappings in full.

```bash
istota -c /data/config/config.toml apply -f settings.toml --dry-run --prune --format json
istota -c /data/config/config.toml apply -f settings.toml --prune
```

`--prune` removes undeclared definitions only within supplied collections. It preserves users, profiles, organizer-owned resources, module jobs, archives and runtime history. It refuses removals that retained references or configuration seeds would undo. A populated destination needs pruning for exact collection restoration. Ordinary pruning preserves every credential.

Heartbeat snapshots use `reset` lists to restore inherited cooldown and interval settings. A file-watch check can also reset `max_age_hours` to remove its age limit. Apply removes this plan metadata before writing HEARTBEAT.md. It writes CRON.md without synchronizing the scheduler database; the scheduler loads changes on its normal pass. Unchanged dispatch keeps a suspended job suspended.

## Credentials

Nonempty recognized credential leaves in raw configuration become `<unchanged>`. An apply on the source preserves the stored value. A destination without that value refuses the plan until a private sidecar supplies a replacement. Empty credential values remain empty. Deployment-file credentials keep their existing writer; a sidecar cannot move them inline.

A snapshot refuses nonempty recognized credential fields in stored briefing block options, per-user source configuration and shared source configuration. The error names the field without its value, and no document or replacement file is written. Remove the embedded credential or configure it through the service's supported credential-store interface before retrying. Folder extras with credential fields are also refused.

The snapshot lists provisionable user service/key slots without decrypting them. Vault, generated and session credentials are excluded and reported on stderr. Missing slots without supplied values produce apply warnings; they do not create placeholder credentials.

A private TOML or JSON sidecar has optional `users` and `config` sections. User values map user → service → key → nonempty string, and must match declared slots. Configuration replacements are a list of objects with a segment-array `path` and a `value`. Integer path segments address array entries, while a dotted header name is one string segment. Set mode 0600 and keep the sidecar outside shared workspace content.

```bash
chmod 600 private-settings.json
istota -c /data/config/config.toml apply -f settings.json --secrets-file private-settings.json --dry-run
istota -c /data/config/config.toml apply -f settings.json --secrets-file private-settings.json
```

`--prune-secrets` requires `--prune` and removes eligible slots only in services explicitly named by a selected user's secret declarations. `keys = []` requests removal of that service's eligible slots. Missing services and `secrets = []` remove no credentials. Vault, OAuth/session and generated credentials remain outside this scope.

Snapshots are private settings files. Addresses, names and paths remain present. Prompts and commands are operator-written text; snapshot does not scan or rewrite them for secrets. Review a snapshot before sharing it or committing it to a repository.

## Moving selected paths

Both commands accept repeated `--patch SOURCE=DEST` arguments. Quote placeholders to prevent shell expansion.

```bash
istota -c /data/config/config.toml snapshot --patch '/data/workspace=${WORKSPACE_ROOT}' -o settings.toml
istota -c /data/config/config.toml apply -f settings.toml --patch '${WORKSPACE_ROOT}=/data/workspace' --dry-run
```

Patches rewrite declared path fields, including resource paths, job prompt files and file-based heartbeat or briefing sources. They do not rewrite commands, prompt bodies, URLs, credentials or arbitrary strings. Matching uses path boundaries and the longest source prefix. Each value is rewritten once; patches do not cascade. An unmatched prefix produces a warning.

A plan containing database or file sections cannot move the running install's storage roots. Complete that deployment migration first, then patch the plan to the destination's actual roots and preview it there.

## Validation and failures

`apply --check --json` validates structure and configuration types without reading live settings. Use `--dry-run` to check identities, references, destination paths and credentials. `--format json` on ordinary apply reports operations, counts, warnings, errors, partial completion and whether successful changes require a restart.

Preflight failure writes no settings. Execution uses separate database transactions and atomic file replacements, so a later failure may leave earlier sections committed. Such a run exits 1 with `partial = true` and names completed, failed and skipped units. Rerun after resolving the error to converge. `--continue-on-error` permits later independent users to proceed after a runtime failure; it does not bypass preflight or stale-state refusals. Direct CLI apply reports restart requirements; it does not restart the daemon itself.
