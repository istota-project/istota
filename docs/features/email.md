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

#### The DMARC canary

Every item on that checklist can stop being true later, and none of them announce it. A DMARC record gets edited. The mailbox moves to a provider that does not enforce. Someone adds an "always trust mail from …" rule for the address the check is about. A forwarding path appears that lands mail behind the filter. The protection is gone and every surface still reports normal; the first sign would otherwise be a task that ran because someone forged a header.

`dmarc_canary` (on by default) is the automated version of the checklist. When mail routes on the strength of a user's own address, it reads the DMARC verdict the receiving MTA stamped in `Authentication-Results` and logs a warning — plus an alert — if that verdict is anything other than `pass`. Mail carrying no DMARC verdict at all is a separate case, silent by default; see `dmarc_canary_warn_on_missing` below. Silent when healthy. It reads the **topmost** header only: each hop prepends its own, so the top one is the final receiving MTA's, and everything below it is whatever the sender chose to include.

Note that `dmarc=none` counts as a failure here, not as an absence. It means the sending domain publishes no policy — the "someone deleted the DMARC record" case — so it warns. A header the check cannot read cleanly also warns, rather than being treated as "no verdict": a sender can plant punctuation that hides the real verdict from a parser, and treating that as silence is exactly what would let them turn the check off.

What it catches, and when, is worth being precise about. Removing a DMARC record, or a mail path that stops evaluating DMARC, shows up on the next ordinary message. *Weakening* a policy from `p=reject` to `p=none` does not: legitimate mail still passes, so nothing looks wrong until someone actually forges your address — at which point the forgery itself trips the canary, rather than the config change that allowed it. Checking that your published policy is still `p=reject` stays a manual item on the checklist above.

Two things it is not. It is **not a gate** — nothing is blocked, held, or rerouted, and turning it on cannot cost you a message. Whether to hold unauthenticated mail is `confirm_sender_match`'s decision, and the two are independent. And it is **not a verifier**: it does not check DKIM itself, because if your MTA already rejects forgeries then re-implementing that check buys nothing, and getting it wrong is worse than not having it.

It follows that an attacker who forges an `Authentication-Results: … dmarc=pass` header suppresses the warning. That is fine, and worth being explicit about: the canary is not the boundary, the MTA is. Its job is catching misconfiguration and drift, not attack. A canary that can be silenced by the thing it is not defending against is still worth having — a canary mistaken for a control is not.

`dmarc_canary_warn_on_missing` (off by default) extends it to mail carrying no DMARC verdict at all. It is off because a mail path that stamps nothing would otherwise warn on every message, which trains you to ignore it. Turn it on once you know your MTA does stamp — that is the only way "the mailbox moved somewhere that does not evaluate DMARC" ever becomes visible, and that is one of the drift cases worth catching.

Alerts are deduplicated per sender and verdict for 24 hours, so a persistently broken path does not flood the channel. The log warning is not deduplicated, so there is still a per-message record.

#### With `confirm_sender_match` on

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

Polling interval is controlled by `email_poll_interval` in `[scheduler]` (default 60s). Mail is deleted from the IMAP folder after `email_retention_days` (default 7) via a server-side date search, so the sweep keeps working on a busy mailbox. It deletes everything in the folder past the cutoff, not only mail Istota processed, and the deletion is permanent — set the window deliberately, and note that the first run after upgrading from a version whose sweep silently did nothing will clear the accumulated backlog (the candidate count is logged before anything is removed). A backlog is drained a couple of thousand messages per cleanup tick rather than in one pass, so a large one clears over several minutes; each tick logs how many are left. The record of which messages have already been processed is pruned separately after `processed_email_retention_days` (default 90) — always at least as long as the mail itself, so a message still in the folder can't lose its record and be ingested a second time.

Where the server advertises the `UIDPLUS` capability (most do, including Dovecot and Gmail), both the retention sweep and the `delete` verb remove exactly the messages they picked. Without it, IMAP offers no way to remove one message without a folder-wide expunge, which also permanently removes anything *another* mail client has flagged for deletion and not yet expunged. Istota logs one warning naming the server when it has to fall back that way — worth reading if the same mailbox is open in another client. If the server refuses the scoped removal, Istota unmarks the messages rather than leaving them flagged-but-present, so a refusal changes nothing.

### Per-user email settings

```toml
# [users.alice] block in config.toml — DB row populated by `istota user ensure` wins
email_addresses = ["alice@example.com"]
trusted_email_senders = ["*@company.com", "boss@other.com"]
alerts_channel = "room789"  # Talk room for confirmations/alerts
```
