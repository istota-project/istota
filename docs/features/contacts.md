# Contacts

Ask for a person's email address or phone number, or ask the bot to create or edit a contact. The contacts skill searches Nextcloud address books and Google Contacts together. Every result names its source and address book; a person present in both sources appears twice.

## Connect Nextcloud

In the Nextcloud Contacts app, share an address book you own with the bot's Nextcloud account. Give it write access if you want the bot to edit contacts. Run `istota-skill contacts books` to see available books; if none are shared, the response names the bot account to share with.

Istota uses the configured Nextcloud URL and bot account. Your Istota user ID must match the book owner's Nextcloud user ID. Connecting Nextcloud for web login does not grant contact access. Books owned by other users, the bot's own books and the server's system address book are excluded, even if the bot can see them. External CardDAV servers are not supported.

## Connect Google

The operator enables the People API in the Google Cloud project used for [Google Workspace](google-workspace.md). In Settings → Google Workspace, select Contacts and connect or reconnect the account. Existing connections do not gain Contacts automatically.

The default scope ceiling includes these read-only scopes:

```toml
scopes = [
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/contacts.other.readonly",
]
```

If the operator has an explicit `[google_workspace] scopes` list, add both entries to it. To offer read and write, replace the first with `https://www.googleapis.com/auth/contacts`. The user must choose that level and reconnect before edits work. Other contacts, the people Google collects from interactions, is always read-only.

## Commands

```bash
istota-skill contacts books
istota-skill contacts search "Alice" --limit 20
istota-skill contacts search "5550100" --source nextcloud
istota-skill contacts show "nc:BOOK/CARD.vcf"
istota-skill contacts create --book BOOK --name "Alice Example" --email alice@example.com:work
istota-skill contacts update "nc:BOOK/CARD.vcf" --etag '"v1"' --add-phone "+1 202 555 0100:cell"
istota-skill contacts delete "nc:BOOK/CARD.vcf" --etag '"v1"'
```

Copy book keys, contact IDs and etags from the preceding result. Google book keys are `google` and `google-other`; Google contact IDs start with `google:` or `google-other:`. Use `--source nextcloud`, `--source google` or `--source google-other` to narrow a search. The default result limit is 20 and the maximum is 100.

Create accepts given and family names, nickname, repeated email and phone fields, organization, title, birthday and note. Label email or phone values with `:home`, `:work` or `:cell`. Update accepts scalar replacements, `--add-email`, `--remove-email`, `--add-phone`, `--remove-phone`, and `--clear-org`, `--clear-title`, `--clear-birthday`, `--clear-note`. A yearless birthday uses `--birthday=--01-15`. Run a verb with `--help` for its full argument list. Fields an update does not touch are preserved, including extra vCard properties.

The bot asks for confirmation before deleting contacts or modifying contacts you created. Read-only books refuse writes. For edits and deletes, pass the etag from the last read. On `changed_since_read`, read the contact again and check the intervening change before retrying. Nextcloud uses conditional writes and deletes. Google updates enforce the contact source etag; Google deletes compare a supplied etag with a fresh read, but an edit between that check and deletion can still be lost.

## Search limits and errors

Nextcloud matches text substrings and phone digits when the query contains at least four digits. It searches locally within each book; books above 5,000 cards or 20 MiB use a server-side text query and appear in `truncated_books`. Phone digit matching may miss differently formatted numbers in that fallback. Malformed cards are skipped and counted in `skipped_cards`.

Google searches word prefixes, with at most 30 results per Google source. It does not support arbitrary phone digit substring matching. Showing an Other contacts ID scans up to ten pages of 1,000 records without storing them. If that bound prevents a complete lookup, the command returns `lookup_limit`.

A successful response can have `partial: true` and an `errors` list when one source fails. Those results do not cover every source. Reconnect Google for `google_token_invalid` or `google_scope_insufficient`; ask the operator to enable the People API for `google_api_disabled`. `not_found` also covers books outside your access.

Contact data is not cached in Istota's database, but command results can appear in task transcripts. Names, notes and other fields are untrusted data and never establish a sender's trust. Notes and long fields carry untrusted markers. The contacts scope is withheld from guest turns and unasked tasks in shared rooms under the normal room policy.
