# WordPress

The `wordpress` skill reads, writes and administers WordPress sites over the core REST API. It works against an ordinary install and an ordinary account: nothing has to be installed on the site. It covers posts, pages and custom post types (discovered at runtime, including types outside `wp/v2`), ACF or Secure Custom Fields values, terms, media, users, site settings, plugins, any other REST route, and the Abilities API on WordPress 6.9 and later. An optional companion plugin adds ACF options pages and a multisite network's site list. One user can have several installs, and several sites of one multisite network.

The skill is a menu skill with a CLI (`istota-skill wordpress`). The model reads its instructions when a request needs it.

## What the operator sets

Nothing is required. Two optional settings in `config.toml`:

```toml
[wordpress]
private_hosts = []   # exact host names allowed to resolve to a private or loopback address
max_upload_mb = 25   # largest file one media upload may send
```

The skill runs host-side, in the daemon's network namespace, so every site address is checked before anything is sent. A host that resolves to a private, loopback or reserved address is refused unless it is listed in `private_hosts`. That covers a local development site and a backend on the internal network, including a public name that internal DNS answers with a private address from the istota host. It is never a general switch. The list is operator-only, since it is the rule that stops a user-chosen URL from reaching the daemon's own network.

Each entry is a bare host name or IP address, compared exactly (case and a trailing dot aside) with the host in the site's URL: no scheme, port, path or wildcard. The config loader logs a warning for an entry that is not, since it would lift the refusal for no site. An internationalised domain is written in its ASCII (punycode) form, in the entry and in the site's URL alike.

On a deployment built by the Ansible role, set these through the role rather than editing `config.toml`, which the next run rewrites:

```yaml
istota_wordpress_private_hosts: ["wp.internal.example.com"]
istota_wordpress_max_upload_mb: 25
```

The role refuses to deploy an entry that is not a bare host name. On the Docker stack, set `ISTOTA_WORDPRESS_PRIVATE_HOSTS` (comma-separated) and `ISTOTA_WORDPRESS_MAX_UPLOAD_MB` in `docker/.env`.

`istota_security_network_extra_hosts` is not the setting for this. It is the sandbox's egress allowlist, which the skill never passes through, so listing the WordPress host there reaches nothing the skill uses and opens a route from the model's own shell.

TLS uses the system trust store. For a site with a locally signed certificate, point `SSL_CERT_FILE` in the daemon's environment at a bundle that carries the local CA.

## What each user sets

One vault entry per install, in the user's credential vault (see [Credentials](../configuration/credentials.md)), under the `istota` group. Nothing else: there is no site file.

The entry's name is `wordpress_` followed by the site's name, which is what `--site` takes: an entry `wordpress_blog` is `--site blog`. A user with one such entry never needs `--site`; with several, every call names one. Site names are lowercase letters, digits and underscores, starting with a letter.

| Vault field | Holds |
|---|---|
| URL | the site's HTTPS address, such as `https://blog.example.com`, with a path if WordPress lives in a subdirectory |
| Username | the WordPress login |
| Password | an application password, made in wp-admin under Users, Profile, Application Passwords |

The URL field also binds the entry to its host. The skill sends the password only to a host the entry is bound to, so a site name or `--blog` the model chooses cannot send it anywhere else. On a subdomain multisite network, list each subsite host in the entry's `istota_hosts` attribute.

Each call reads the entry once from the task's vault budget (`[security] vault_fetch_limit_per_task`). The vault's own rules apply: it is withheld on a guest's turn and on a task nobody asked in a shared room, and with the credential broker on, a scheduled job needs its grant's scheduled use switched on.

`istota-skill wordpress sites` lists the `wordpress_*` entries by site name, without reading a password or making a request.

## Asking before anything public

The model reads text it did not write: other authors' posts, plugin output, the site's error messages. Any of it can carry instructions. So every action that is public or hard to undo refuses without `--confirmed`, and the answer carries a `would` line saying exactly what would happen, for example `would publish "Weekly update" (post #42) on blog`. The model shows that line to the user and passes `--confirmed` only after the user agrees in the conversation.

Gated:

- publishing, scheduling, or making a post private, and any edit to a post that is already live;
- deleting a post for good (moving it to the trash is not gated);
- creating terms;
- creating or updating users, changing site settings, and activating, deactivating or installing plugins;
- any `rest` call that is not a `GET`, and running an ability the site does not mark read-only;
- writing an ACF options page.

Drafts, pending posts and media uploads are not gated.

## How writes behave

- A create, an upload, a user create, a plugin install, a `rest` call and an ability run are sent once. If the connection drops or the site fails partway, the answer is `outcome_unknown`, with a lookup command where there is one. Nothing is resent blind.
- Every post write is read back, and the answer names any field WordPress dropped or changed, such as unregistered meta, markup filtered for an account without `unfiltered_html`, or a slug that gained `-2`. Settings writes report a setting the site ignored the same way.
- ACF fields are written whole, in the shape `get` returns. Anywhere in an ACF value, `{"$upload": PATH}` uploads a file from the user's own workspace and puts its attachment id there. All uploads happen before the post is written.
- `users create` sets no password anybody knows, and WordPress sends no email. The new user signs in after using "Lost your password?" on the login page.
- Every string the site wrote comes back inside untrusted-content markers.

## Multisite

A network is one vault entry and one credential; a super admin's application password works on every site. `--blog SLUG` (subdirectory network) or `--blog HOST` (subdomain network) addresses one site. The skill checks that the site exists before acting on it, so a typo answers `unknown_blog` rather than acting on the main site.

`plugins activate --network` asks core REST to network-activate a plugin. If the site refuses, including because it is not a network, the answer is `unsupported_on_multisite` and the plugin is not activated per site instead; network-activate it in the network admin.

## The istota-connector plugin

Core REST has no route for an ACF options page and none for listing a multisite network's sites. A small companion plugin fills both gaps. It is optional: everything else works without it, and the three verbs that need it answer `connector_missing` with a pointer here.

| Verb | Ability | Needs |
|---|---|---|
| `options get --page SLUG` | `istota/options-get` | `manage_options`, and the options page's own capability |
| `options update --page SLUG --acf-file F --confirmed` | `istota/options-update` | the same |
| `network sites` | `istota/network-sites` | `manage_sites` on the main site (a super admin) |

The plugin registers these as abilities with the WordPress Abilities API, so it needs WordPress 6.9 or later. It adds no REST routes of its own, has no settings and stores nothing. Only fields in a field group with "Show in REST API" switched on can be read or written, the same rule ACF applies to posts. `options update` replaces each named field's value whole and leaves the others alone. Write image and file fields as attachment ids.

The source is `integrations/wordpress/istota-connector/` in the istota repository. To install it:

1. Build the zip from a checkout:

   ```bash
   scripts/build-wordpress-connector.sh
   ```

   This writes `dist/istota-connector-<version>.zip` from the committed files at `HEAD`. Pass another git ref as the first argument to build that one instead. Uncommitted edits are not included.

2. In wp-admin, go to Plugins, Add New, Upload Plugin, and upload the zip. On a single site, activate it. On a multisite network, upload it in the network admin and network-activate it, so every site has the options abilities.

3. Check it with `istota-skill wordpress describe --refresh`, which should report `connector: true`.

To update the plugin, build a new zip and upload it again; WordPress offers to replace the installed copy.

## Not offered

Core updates, creating network sites, network users and super admins, search-replace, database import and export, and editing field group or post type definitions all need WP-CLI or wp-admin. Secure Custom Fields registers abilities that create, update, delete and import those definitions; `abilities run` refuses every `scf/*` and `acf/*` ability that is not read-only, with `definition_edit_refused`, whatever `--confirmed` says. Its read abilities still run. Deleting users and plugins, and anything to do with application passwords, are left to wp-admin too.

## Testing against a live site

`tests/test_wordpress_integration.py` is a smoke test against a real site, marked `integration` and skipped unless `ISTOTA_WP_TEST_URL`, `ISTOTA_WP_TEST_USER` and `ISTOTA_WP_TEST_APP_PASSWORD` are set. It writes, so point it at a local development copy, never at production, and revoke the application password afterwards. The module docstring lists the optional variables.

```bash
uv run pytest -m integration tests/test_wordpress_integration.py -n0 -v
```
