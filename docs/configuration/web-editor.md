# Changing configuration from the web

An admin can change the instance configuration at Admin → Configuration (`/istota/admin/config`): the keys in `config.toml`, the deployment credentials in `secrets/`, and the admin allowlist in `config/admins`. This page is for the operator: what the editor can change, what stays a host action, and how a change reaches the running stack.

## How a change is applied

The web container cannot write `config/` (it is bound read-only into every service) and cannot restart the stack. A change is therefore a request that the host applies:

1. The admin edits fields (or the whole file as text, below), reviews the change and submits it. The page shows a redacted diff, any problems the configuration check reports, and which credentials change (by name, never by value).
2. The web stores the request in the `config_change_requests` table and writes its id to `requests/config-apply` in the stack directory. Credential values are sealed with the master key until the host takes the request.
3. A systemd path unit on the host runs `istota-stack config-apply`. Under the stack lock it takes the request, checks everything with a dry run before writing anything, writes credentials and admins through `put-files` and the config through `istota apply` (keeping the newest ten `config.toml.bak.<timestamp>` copies), records the outcome and restarts the stack. A timer runs the same unit every five minutes as a backstop. It starts no container while the trigger file is unchanged since a run that found nothing pending, and asks the database again once that run is an hour old, so a request whose trigger the web failed to write is taken within the hour.
4. The page follows the request through the restart and reloads once the web process is back.

Every applied change restarts the stack. The review dialog shows how many tasks are running, so the admin can wait for them.

The applier exists only on the dedicated VM, set up by `host/provision.sh`. Anywhere else (`istota serve` on macOS, a standalone install) the page is read-only and says so. On a VM provisioned before the applier was added, the page is read-only until `istota-stack update` re-runs provisioning and the stack restarts.

## What it guards

- Every submit needs a confirmation: a code sent to the admin's sign-in email address, or, on an install with no mail set up, the admin's account password. An admin with no live email identity cannot submit or preview, and one with no password on an install without mail needs a password link (`istota auth reset <user> --print-link`, which works only where `[web] auth` includes `email`) or mail set up.
- Settings that can lock admins out or change the security posture (`web.auth`, the OAuth client, `security.sandbox_enabled`, `security.skill_proxy_enabled`, `brain.kind`, any change to the admin list, and a few more) need a typed confirmation.
- `[web] auth` must keep `email`, and the admin list must not be empty, must keep the acting admin, and may name only users with a live sign-in identity. Give a user an identity on the Users page first.
- A value the loader would discard blocks the change, including one already in the file. The page shows it against its key so it can be fixed in the same change.
- If `config.toml` changes on the host after the page loaded, the submit is refused, and so is a request the host takes after such a change, so a host edit to `config.toml` is never reverted by an older web request. That check covers `config.toml` only: a credential or the admins file written on the host with `put-files` between the submit and the take is overwritten for the names the request changes.
- A request is refused when a plan has taken over what it changes (`config/managed.json`) since it was made.
- While `config.toml` does not parse, nothing is changed from the page. Fix it on the host with `istota-stack apply`, or copy back a `config.toml.bak.<timestamp>`.
- Only one request is open at a time. A request the host has not taken can be cancelled.
- Every other admin gets an `admin_config_change` notification when a change is applied or fails.

## What stays a host action

These keys are shown read-only, each with the reason and where it is changed instead:

- The container layout `istota setup` writes (`db_path`, `workspace_path` and the rest), and `[users.*]` as form fields.
- `site.hostname`, `nextcloud.url`, `nextcloud.username` and the Nextcloud app password, which the entrypoint preflight and the rclone mount depend on (`istota-stack setup --force`).
- `devbox.network_subnet`, `web.enabled` and `web.port`.
- Keys that select a compose profile: `browser.enabled`, `browser.api_url`, `talk.signaling.*`, `whatsapp.enabled`, `whatsapp.provider`.
- The web session signing key.
- Any key an environment variable sets, which wins over `config.toml`.

`host.env`, `.env`, TLS certificates, the persona and the other prompt files are not edited here at all.

## Managed resources

If the Ansible management role (or a plan with `[meta] managed = true`) owns a resource, the editor shows it read-only, because the next play would revert a web change. Ownership is per resource (config, credentials, admins) and is recorded in `config/managed.json`. To hand a resource to the web, remove it from the role's variables (`istota_config`, `istota_secrets` or `istota_admins`), or run `istota-stack apply` once with `managed = false`. A `managed.json` that cannot be read makes every resource read-only until `istota-stack apply` rewrites it.

## Tables, the whole file, and ignored keys

A table such as `[models.aliases]`, `[[default_briefings]]` or `[[briefing_shared_blocks]]` is edited as TOML that defines that one key, starting from what the file holds. "Reset to default" removes the key; `briefing_shared_blocks = []` is a different edit, and means no shared blocks rather than the shipped ones.

The whole of `config.toml` can also be edited as text. Every credential in it reads `"<unchanged>"`, which keeps the stored value; typing over it sets a new one. The text replaces the whole file, so it cannot be combined with field edits in one change. `[users.*]` can be edited there, but it only seeds users on first start; per-user settings belong on the Users page.

Keys the loader does not recognise, retired keys, and a credential set in `config.toml` while its secrets file is also set (the file wins) are listed under "Warnings about config.toml" with a Remove action. A key inside an array of tables has no Remove action and is removed in the text editor.

## History and restore

Applied, refused and cancelled requests are kept for 180 days with their redacted diff and the host's output. Restoring an earlier request previews its configuration as a new change, which the admin then submits. A restore brings back configuration only: credentials and the admin list stay as they are, and a credential cleared since that request stays cleared.

## Comments in config.toml

Every write goes through the same TOML writer `istota apply` uses, so comments in a hand-written `config.toml` are not kept. The page warns when the file has comments. The previous text is in the newest `config.toml.bak.<timestamp>`.

## When something goes wrong

- A request still waiting after two minutes: check `systemctl status istota-config-apply.path istota-config-apply.timer` on the host, or run `istota-stack config-apply` by hand. `istota doctor --only web.config_applier` warns about a request pending for ten minutes or taken for an hour.
- The web does not come back after a change: on the host, run `istota-stack apply` with an earlier plan, or copy `config/config.toml.bak.<timestamp>` back as uid 10001 and run `istota-stack up`.
