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

### What trusting a sender means

One list, two meanings. `trusted_email_senders` decides both that this person's mail is processed without asking you, and that mail *to* this person is sent without waiting for your approval. Trusting someone so their newsletter stops interrupting you also authorizes the bot to write to them unprompted.

That is a deliberate trade — the alternative is two lists that drift apart — but it has one consequence worth knowing: every entry written before the outbound gate shipped was made under the narrower inbound-only meaning, and those entries now carry the wider one. Read your list once (`!trust` with no argument prints it) if that matters to you.

A catch-all pattern (`*`, `*@*`) therefore turns the `untrusted` outbound policy off entirely. It is logged when it happens, but narrow the pattern rather than relying on the log.

Trusted senders are configured at two levels:

- **Config-time**: `trusted_email_senders` in per-user config (supports fnmatch patterns like `*@company.com`)
- **Runtime**: managed via `!trust` from any surface with a composer — Talk, web chat, the CLI

```
!trust sender@example.com     # add trusted sender
!untrust sender@example.com   # remove trusted sender
!trust                         # list all trusted senders
```

Runtime trusted senders are stored in the database and checked alongside config-time patterns. `yes trust` at an inbound confirmation prompt is the same grant, given inline.

### Suspicious email alerts

During task execution, if the agent detects suspicious content in an email (social engineering, prompt injection, exfiltration attempts), it writes an alert to a deferred JSON file. After task completion, the scheduler posts these alerts to the user's alerts channel in Talk.

## Sending email

Outbound emails use SMTP. The `SMTP_FROM` address is plus-addressed as `bot+user_id@domain` so replies route back to the correct user.

Email output uses a deferred file pattern: Claude writes a JSON file to the temp dir, and the scheduler sends the email after task completion.

### The outbound approval gate

Mail to someone you have not authorized is not sent on the bot's judgement. It is composed, held as an editable draft, and shown to you; you approve, edit, or discard it, and approving sends exactly the bytes you read.

The decision is made on the **recipients** and nothing else. It does not read the message, does not try to judge whether the text commits you to anything, and cannot be argued past — the check runs in the send path outside the sandbox, so the model has no way to assert around it. A single unauthorized address in To, Cc or Bcc holds the whole message; there are no partial sends.

Three policies, ordered `off < untrusted < all`:

| Policy | A message is sent immediately when |
|---|---|
| `off` | always — no holds |
| `untrusted` | every recipient is trusted: one of your own addresses, a `trusted_email_senders` pattern, or an address you trusted at runtime |
| `all` | every recipient is one of your own addresses |

The operator sets a floor in `[email] outbound_approval_floor` (default `untrusted`). A user may tighten past it and never loosen below it, and a user who has never set their own policy follows the floor — so raising the floor reaches everyone.

A user's own policy is set with `istota user ensure --outbound-approval <policy>`, which is what Ansible runs, or cleared back to following the floor with `--outbound-approval ""`. The `[users.X] outbound_approval` key in `config.toml` seeds the value **only for a user with no profile row yet**; on any instance that has already started once the DB row wins, so editing the TOML for an existing user does nothing. That is the general rule for per-user fields, and it is the one that bites here — use the CLI.

An invalid floor fails the config load rather than falling back. There is no safe value to guess: `off` would disable a gate you asked for, and `untrusted` would override an operator who deliberately wrote `off`.

**What is authorized is only what you said so.** The allowlist is your own addresses, your configured patterns, and addresses you trusted by hand. It is never derived from who you have corresponded with. An earlier attempt at this gate built its allowlist from observed mail and inverted itself — one message from a stranger permanently authorized mailing them back — which is exactly the wrong direction, since the addresses the gate most needs to hold are the ones that reach you.

### Answering a held draft

From web chat, the draft appears as a card under the turn that wrote it, showing the recipients, the subject, the whole drafted body, and anything else that task did — a calendar event it created, say, so declining does not quietly leave one behind. Send, edit the wording, or discard. A draft from a job with no conversation of its own appears in a list above the transcript, so nothing is reachable only from a room you never open.

From Talk or any other surface with a composer:

```
!drafts                  # list what is waiting, with ids
!drafts send <id>        # release one
!drafts discard <id>     # bin one
```

With exactly one draft pending the id may be omitted. With several it is required, and the command lists them rather than guessing.

One state needs a human rather than a button. If the process dies between claiming a draft and recording the send, the draft is left marked as sending, and nobody can know from the outside whether the mail went out — so the card shows it and offers no action, because one of the actions would send it twice. Check your Sent folder. There is currently no way to dismiss such a row.

**A held draft does not expire.** It is your own unfinished reply, and binning it silently after a couple of hours would lose work with no trace — so unlike the inbound confirmation gate, nothing cancels it. A draft still waiting after 24 hours raises one notification (not a hundred, and never as a briefing item) naming the recipient and subject. Turning the policy off later does not auto-send anything already held.

Recipients and threading are not editable, only the body. An editable recipient list is a gate you can be talked through.

The check runs twice, in two different places, and both are deliberate. The `send` and `reply` verbs check before they do anything, so the refusal reaches the model in-turn, worded so it can tell you the message is waiting instead of retrying with different arguments. The delivery leg checks again immediately before the message leaves — and that second one is what makes the guarantee true rather than conventional, because it is the only point every path passes through. A reply the task defers through `email output`, a hand-written deferred file, a scheduled job's mail: all of them arrive there.

That second check is what an earlier version of this gate was missing. It covered the two verbs and not `email output`, which is the one the model actually reaches for when replying to the message that created the task — so the first adversarial exchange after the gate shipped held nothing, and two messages reached an address that had been explicitly declined.

A reply held at the delivery leg raises a notification the moment it is held. That is not decoration: the assistant finished its turn believing the reply went out, and has usually already told you so, and a first-contact thread has no room for the draft card to appear in — so without the notice the hold would be invisible until the 24-hour reminder. `!drafts` releases or discards it. Approving sends the reply threaded onto the original message, from the recipients and headers snapshotted at hold time.

One fidelity note. A held message stores a single body, so a briefing held on its way into an email thread is released as plain text, losing the HTML alternative with its article links. What you approve is what is sent, which is the property worth keeping; the links are the cost.

One thing worth knowing about what an inbound approval means. Answering `yes` to an inbound confirmation prompt approves *reading* that one message. It writes no trust row, so it does not authorize mailing that sender back — the reply is held separately under whatever policy applies. Answering `yes trust` does authorize both, because it adds the address to your trusted list.

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
outbound_approval_floor = "untrusted"  # off | untrusted | all
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
outbound_approval = "all"   # tighten past the operator floor; "" follows it
```

As above, `outbound_approval` here is read only when the user has no profile row yet. For an existing user set it with `istota user ensure --outbound-approval`, which is the path Ansible uses.
