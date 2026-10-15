# Migrating group briefings

Upgrading installs the schema and preserves effective legacy defaults. It does not activate imported group briefings or retire publishing scripts automatically. Production cutover is a separate operator action: supply the current prompts, script intent, consumer inventory and per-user dispositions, review the report, and then run the commands below. The development tests do not certify a production cutover.

Run commands with the installation’s configuration, for example `istota -c /data/config/config.toml briefings ...`. Back up the framework database and personal Briefings databases before cutover. Run `istota init` during the normal upgrade before starting the scheduler. Initialization reads configuration without DB overlays, applies migrations, then captures legacy state; ordinary configuration loads refuse an old database that still needs initialization.

## Former default briefings

```bash
istota briefings migrate report --actor alice
istota briefings migrate import --actor alice --name daily --timezone UTC
istota briefings migrate report --actor alice --name daily
```

Import creates a disabled All users definition once. Repeating it preserves later UI edits. The report compares each existing personal schedule and content with the preserved default. Matching a name alone is insufficient: edited, explicit or ambiguous copies need a decision. Per-user default opt-outs become group-wide delivery mutes; known disabled copies retain per-definition mutes. Neither mute removes history access.

Choose the common timezone explicitly. One shared occurrence cannot preserve several personal local times. Edit the imported draft in Briefings, resolve unsupported sources, obtain any contributor approvals, and preview it. A personal source cannot become shared merely because it appeared in a default.

A JSON dispositions file maps reported user ids to `retire`, `keep` or `mute`, for example `{"alice":"retire","bob":"keep"}`. Positively attributable unchanged copies can use the report’s proposed retirement; unresolved copies require an explicit entry. Review and cutover use the current definition revision from the report:

```bash
istota briefings migrate review --actor alice --name daily --timezone UTC --revision 1 --dispositions dispositions.json
istota briefings migrate cutover --actor alice --name daily --revision 1
```

Use `--accept-source-changes` on review only after checking removed or converted sources and delivery changes. It acknowledges that review; it creates no resource-owner approval. An edit or changed candidate after review requires a new review. Running legacy tasks block cutover. Pending or failed old occurrences are cancelled, and completed outcomes remain recorded so the transition occurrence is not generated again. Personal archives and blocks are retained.

## Publishing jobs and shared content

Keep shared blocks that still have consumers. The migration never reads or executes an arbitrary publishing script. Rewrite its intended result as a supported group definition, and list any opaque script or prompt consumers yourself.

The import file is a JSON or TOML group definition plus `group_id`, `outputs` and `dependent_jobs`. For example, after obtaining the actual target id from Groups:

```json
{
  "group_id": "team",
  "name": "news",
  "title": "Daily news",
  "cron": "0 7 * * *",
  "timezone": "UTC",
  "channel": "email",
  "generation_mode": "prompt",
  "generation": {
    "version": 1,
    "prompt": "Summarize the research with article links.",
    "research": {"enabled": true, "prompt": "Read public news and follow relevant article links."},
    "inputs": []
  },
  "outputs": ["briefing_shared_blocks/news"],
  "dependent_jobs": []
}
```

`outputs` names every old shared-block output; the stored job’s declared output must be included. `dependent_jobs` is required even when empty and contains integer job ids for opaque consumers the operator identified. The report also discovers stored personal/group/shared-block source references. The actor must be an installation admin and a current owner of an ordinary target group; All users uses the admin exception.

```bash
istota briefings migrate import --producer-user alice --name news-publisher --actor alice --file publisher.json
istota briefings migrate report --producer-user alice --name news-publisher --actor alice
```

The import is disabled and does not implicitly copy the producer’s private prompt or command. Supply the intended generation settings explicitly. An import without generation settings remains unresolved until the prompt is replaced. Use Preview in Briefings after the final edit; cutover requires a successful preview of that same revision. Publishing jobs use report plus preview, rather than the default migration’s `review` command.

For every remaining reported consumer, a replacements file maps its exact report `id` to an enabled replacement producer’s integer job id. The replacement must publish the same output and cannot be the job being retired. A consumer removed or changed to stop reading that output no longer needs a replacement. An unexplained opaque consumer blocks safe retirement: complete the inventory before confirming.

```bash
istota briefings migrate cutover --producer-user alice --name news-publisher --actor alice --revision 1 --dispositions replacements.json --confirm-cutover
```

Cutover checks the producer, preview and live consumers again. It cancels queued old tasks, refuses in-flight work, enables the group definition and writes a durable retirement record. CRON sync and task admission respect that record, including after a restart or deletion/recreation of the scheduler row. It does not edit CRON.md. Existing KV values retain their timestamps, and old personal archives remain personal.

## Restart, delivery and rollback

Published editions and recipient/part states live in the framework database. A restart resumes the saved edition; retrying delivery makes no further research or synthesis request. Confirmed parts are not repeated. An expired active send or ambiguous provider acceptance becomes uncertain and is held. Retry uncertain delivery only after acknowledging possible duplication; it keeps its original destination identity and refuses a changed destination. Local state cannot guarantee exactly-once email.

Rollback disables the imported definition and restores the recorded old schedule. Use the definition’s current revision, which cutover increments:

```bash
istota briefings migrate rollback --actor alice --name daily --revision 2
istota briefings migrate rollback --producer-user alice --name news-publisher --actor alice --revision 2
```

Sending or uncertain group deliveries block rollback. Resolve their outcome first. Publisher rollback also refuses if the old job changed after cutover. Rollback does not recall sent copies, remove published history, recreate cancelled tasks or replay missed occurrences. Re-enabling a source or removing an exclusion does not backfill skipped delivery. This workflow rolls back schedule ownership, not the database schema or software release; use the deployment backup/rollback procedure for those.

After production cutover, record the chosen timezone, imported definition and revision, candidate dispositions, producer/consumer replacements, cancelled and already-completed outcomes, and any unresolved deliveries in the operator’s private operations notes. No production prompts, scripts, recipients or credentials belong in the repository.
