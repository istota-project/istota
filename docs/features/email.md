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
- Emails whose `From:` names one of the user's own addresses, when `confirm_sender_match` is enabled (default: false)

When an email is gated, a confirmation prompt is posted to the user's alerts channel (Talk) asking them to approve, discard, or — for an external sender — trust them so later mail passes. Trusted senders bypass the gate.

Thread-matched emails (emissary replies) are never gated — the external contact holds a `Message-ID` from mail the bot sent on the user's behalf, and that is the routing evidence.

### `confirm_sender_match`

**This flag is a declaration about your inbound mail path, not a security feature you switch on for extra safety.** The bot treats a `From:` matching one of a user's `email_addresses` as proof that user sent the mail. SMTP `From:` is unauthenticated, so on its own that is a claim anyone who knows the address can make. The question the flag answers is *who checked it*:

- **`false` (default)** — "something upstream already authenticated the `From:`." Normally that means the receiving mail infrastructure enforces DMARC, so a forged message claiming your address is rejected at SMTP time and never reaches the folder the poller reads. Nothing asks you anything; mail you send the bot is processed immediately.
- **`true`** — "nothing upstream authenticates it, so ask me." Mail arriving with a user's own address on it is held until they approve it from Talk, a channel the sender cannot reach.

Solving this at the MTA is strictly better than solving it here. It is silent, it costs nothing per message, and it cannot be talked past by a tired human approving a prompt. The confirmation gate exists for deployments that cannot do it upstream — it is a fallback, and it is noisy by construction, because nothing in a plain SMTP message distinguishes you from someone claiming to be you.

#### What the default assumes

Leaving the flag off makes the mail path load-bearing. Worth confirming, and re-confirming when the mail setup changes:

- Every domain in `email_addresses` publishes DMARC `p=reject` (or `quarantine`) with DKIM or SPF alignment. This is the *sending* domain's policy — check each domain you list, not only the main one.
- The bot's mailbox provider actually evaluates and enforces that policy. Publishing is the sender side; enforcement is the receiver side. A self-hosted MTA with no DMARC milter enforces nothing.
- No provider-level allowlist exempts your own address. "Always trust mail from …" rules are common, and defeat the check for precisely the address that matters.
- `poll_folder` is the folder the surviving mail lands in. Under `p=quarantine` a forgery goes to Junk, which is as good as a rejection here — but only because that folder is not polled.
- Nothing injects into the mailbox behind the check: internal relays, IMAP `APPEND`, webmail send-to-self.

Forwarding is the case that hurts legitimate mail rather than security. A forwarder breaks SPF and some rewrite `From:`, so mail forwarded into the bot may be rejected upstream or may route differently once it arrives.

#### With the flag on

It applies to whichever route the mail takes, not only to sender-match routing. Routing is decided by the recipient first, and the plus-address is public — it is the `From:` on every message the bot sends on the user's behalf — so a sender who knows the address the gate is about also knows how to arrive as a plus-addressed message instead. The same claim gets the same answer either way. Mail from a genuinely external sender is unaffected by the flag; it is gated or not on the existing plus-address rule.

Two escape hatches keep it usable. An address listed in the user's `trusted_email_senders` is exempt outright, and `!trust <address>` adds one at runtime. Both are deliberate grants rather than the header trusting itself — but an address trusted either way is then trusted for anyone who can spoof it, so a deployment that turns the flag on for the spoofing protection should not immediately trust its way back out of it. For that reason the confirmation prompt for a self-claim offers only `yes` and `no`; the `yes trust` shortcut is offered only for genuinely external senders, where trusting them costs nothing this gate protects.

Two limits to know. An unanswered confirmation is auto-cancelled after `scheduler.confirmation_timeout_minutes`, so leaving the flag on with no watched Talk channel drops inbound mail rather than queuing it (an undeliverable prompt is logged as a warning). And attachments are downloaded to the user's `inbox/` before the gate runs, so declining a message holds its *processing* — the attached files have already landed and are not removed.

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

## Emissary threads

When the bot sends an email on behalf of a user, the outbound message is tracked in the `sent_emails` table (Message-ID, recipient, user, conversation_token, plus an origin descriptor recording the surface+channel the thread started on). When external contacts reply, the email poller matches `References` headers against sent emails and creates a task routed by the user's `email_reply_routing` policy:

- `origin+thread` (default) — deliver the reply to the conversation's origin surface (`web` / `talk` / etc.) **and** mirror it back over email to the thread.
- `origin` — origin surface only.
- `thread` — email thread only.

The origin descriptor self-addresses the surface and channel, so a reply to a web-started thread lands back in the right web room rather than defaulting to Talk. Replies whose origin can't be recovered (plus-address / sender-match paths with no descriptor) fall back to the user's resolved Talk room.

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
# [users.alice] block in config.toml — DB row populated by `istota user ensure` wins
email_addresses = ["alice@example.com"]
trusted_email_senders = ["*@company.com", "boss@other.com"]
alerts_channel = "room789"  # Talk room for confirmations/alerts
```
