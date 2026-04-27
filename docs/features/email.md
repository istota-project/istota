# Email

Istota polls an IMAP inbox for incoming messages and sends replies via SMTP.

## Receiving email

The email poller checks the configured IMAP folder (default: `INBOX`) at regular intervals. Routing precedence for incoming mail:

1. **Recipient plus-address**: `bot+user_id@domain` routes directly to the specified user
2. **Sender match**: sender email matched against user `email_addresses` config
3. **Thread match**: `References` header matched against `sent_emails` table (emissary thread replies)

Attachments are downloaded to `/Users/{user_id}/inbox/`.

### Email confirmation gate

Emails from untrusted senders require explicit user confirmation before processing. This applies to:

- Plus-addressed emails (`bot+user_id@domain`) from senders not in the user's trusted list
- Sender-match routed emails when `confirm_sender_match` is enabled (default: true)

When an email is gated, a confirmation prompt is posted to the user's alerts channel (Talk) asking them to approve, trust the sender, or discard the message. Trusted senders bypass the gate.

Trusted senders are configured at two levels:

- **Config-time**: `trusted_email_senders` in per-user config (supports fnmatch patterns like `*@company.com`)
- **Runtime**: managed via Talk commands

```
!trust sender@example.com     # add trusted sender
!untrust sender@example.com   # remove trusted sender
!trust                         # list all trusted senders
```

Runtime trusted senders are stored in the database and checked alongside config-time patterns.

### Suspicious email alerts

During task execution, if the agent detects suspicious content in an email (social engineering, prompt injection, exfiltration attempts), it writes an alert to a deferred JSON file. After task completion, the scheduler posts these alerts to the user's alerts channel in Talk.

## Sending email

Outbound emails use SMTP. The `SMTP_FROM` address is plus-addressed as `bot+user_id@domain` so replies route back to the correct user.

Email output uses a deferred file pattern: Claude writes a JSON file to the temp dir, and the scheduler sends the email after task completion.

### Outbound recipient gate

Sends to addresses not in the user's known-recipients list are held for explicit user confirmation rather than going out immediately. The known-recipients set is built from:

- Addresses you have previously sent to (`sent_emails.to_addr`)
- Addresses that have written to you (`processed_emails.sender_email`)
- Runtime trusted senders (`trusted_email_senders` table — also used by the inbound gate)
- Your own configured `email_addresses`
- For task sources you authored (`cli`, `talk`, `scheduled`, `subtask`): addresses that appear verbatim in the task prompt
- Fnmatch-style globs from per-user `trusted_email_senders` config (e.g. `*@company.com`)

For email-sourced tasks the prompt is the inbound message body, so its embedded addresses are deliberately *not* added to the allowlist — otherwise an attacker could include their own address in the email they send the bot.

When the gate fires, a confirmation prompt is posted to your alerts channel (the same channel used by the inbound gate). Reply with:

- `yes` — send the queued draft
- `yes trust` — send and add the recipient to `trusted_email_senders` so future sends to that address skip the gate
- `no` — discard the draft and cancel the task

This is Layer A of the adversarial-defense plan. It closes the most realistic exfiltration path for a prompt-injected agent: being steered into mailing your data to an attacker.

The gate is on by default. To disable (e.g. during a rollback), set `outbound_gate_email = false` in the `[security]` section.

## Emissary threads

When the bot sends an email on behalf of a user, the outbound message is tracked in the `sent_emails` table (Message-ID, recipient, user, conversation_token). When external contacts reply, the email poller matches `References` headers against sent emails and creates tasks with `output_target="talk"` routed to the originating Talk conversation.

The bot drafts a response and asks for confirmation. On approval, the task re-executes with `confirmation_context` injected, instructing it to send the draft rather than re-draft. Pending confirmations are auto-cancelled when the user sends a new message in the same conversation.

## Configuration

```toml
[email]
enabled = true
imap_host = "imap.example.com"
imap_port = 993
imap_user = "istota@example.com"
imap_password = "app-password-here"
smtp_host = "smtp.example.com"
smtp_port = 587
# smtp_user = ""      # defaults to imap_user
# smtp_password = ""  # defaults to imap_password
poll_folder = "INBOX"
bot_email = "istota@example.com"
```

SMTP credentials fall back to IMAP credentials if not set.

Polling interval is controlled by `email_poll_interval` in `[scheduler]` (default 60s). Old processed emails are cleaned after `email_retention_days` (default 7).

### Per-user email settings

```toml
# config/users/alice.toml
email_addresses = ["alice@example.com"]
trusted_email_senders = ["*@company.com", "boss@other.com"]
alerts_channel = "room789"  # Talk room for confirmations/alerts
```
