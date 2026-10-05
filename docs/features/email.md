# Email

Istota polls an IMAP inbox for incoming messages and sends replies via SMTP.

## Receiving email

The email poller checks the configured IMAP folder (default: `INBOX`) at regular intervals. Routing precedence for incoming mail:

1. **Recipient plus-address**: `bot+user_id@domain` routes directly to the specified user
2. **Signup address**: `bot+user_id+slug@domain` files mail only while that credential's tag is open. The address is looked up as a whole tag, so user IDs containing `+` work.
3. **Sender match**: sender email matched against user `email_addresses` config
4. **Thread match**: `References` or `In-Reply-To` matched against the `sent_emails` table, for a reply on a thread the bot sent before rooms were created at the send. The reply creates the thread's [room](#email-thread-rooms).

When Istota creates a credential, its username defaults to a signup address if the bot mailbox is configured. The MTA must deliver addresses with two plus tags to that mailbox. A message to an open signup address is stored separately and marked `routing_method="signup"`; it is never used as a task prompt or sent through the sender confirmation gate. Read it with `istota-skill email signup-inbox --slug SLUG`. The returned content is untrusted, and ordinary mailbox reads exclude signup addresses. A message addressed to both an exact user and an unknown, pending, or closed signup tail is discarded. The first message within `[email] signup_task_window_minutes` (30 by default) creates one follow-up task with an Istota-authored prompt; setting it to 0 only files mail. The task's full result stays in the task log; Istota sends a fixed completion or failure notice that contains neither the email body nor the model's reply. Any later message, including one within the window after a task was minted, is filed and raises a notification for the user to inspect. Deleting the credential from the vault closes the tag. Filed bodies are cleared after `[email] signup_body_retention_days` (14 by default); the message rows remain.

Attachments are downloaded to `/Users/{user_id}/inbox/`.

### Email confirmation gate

Emails from untrusted senders require explicit user confirmation before processing. This applies to:

- Plus-addressed emails (`bot+user_id@domain`) from senders not in the user's trusted list
- Emails whose `From:` names one of the user's own addresses, when `confirm_sender_match` is `"verify"` (and the message did not authenticate) or `"gate"` (default: `"off"`, never)
- Replies on an email thread whose `From:` is not one of the thread's people, or, on a thread sent before rooms were created at the send, not one of the addresses the bot wrote to

When an email is gated, a confirmation prompt is posted to the user's alerts channel (Talk) asking them to approve, discard, or — for an external sender — trust them so later mail passes. Trusted senders bypass the gate.

A reply from the contact the bot wrote to is not gated: that address is the one the bot chose to correspond with, so the reply carries the same evidence the send did. Someone *else* replying on that thread is, and it takes one approval per thread: a plain `yes` processes the mail and adds its sender to the thread's people, so their later mail on that thread is not gated again, while mail from them on any other thread still is. `yes trust` lets their mail through everywhere. The `Message-ID` alone is no longer enough, because it is not a secret: it travels to everyone Cc'd, everyone the thread is forwarded to, and into any public archive the thread reaches, and it never expires.

### `confirm_sender_match`

**This flag is a declaration about your inbound mail path, not a security feature you switch on for extra safety.** The bot treats a `From:` matching one of a user's `email_addresses` as proof that user sent the mail. SMTP `From:` is unauthenticated, so on its own that is a claim anyone who knows the address can make. The question the flag answers is *who checked it*:

- **`"off"` (default)** — "something upstream already authenticated the `From:`." Normally that means the receiving mail infrastructure enforces DMARC, so a forged message claiming your address is rejected at SMTP time and never reaches the folder the poller reads. Nothing asks you anything; mail you send the bot is processed immediately.
- **`"verify"`** — "ask the mail server." A message whose own stamp carries a verified, aligned `dmarc=pass` is processed immediately; anything else is held. Requires `authserv_id`, and the bot refuses to start without it.
- **`"gate"`** — "nothing upstream authenticates it, so ask me." Every message arriving with a user's own address on it is held until they approve it from Talk, a channel the sender cannot reach.

The legacy booleans still load: `false` is `"off"` and `true` is `"gate"`, so an existing config keeps its exact behaviour with no edit.

Solving this at the MTA is strictly better than solving it here. It is silent, it costs nothing per message, and it cannot be talked past by a tired human approving a prompt. The gate exists for deployments that cannot do it upstream.

**`"verify"` is the setting worth reaching for, and it is why the other two are the way they are.** `"gate"` is noisy by construction: nothing in a plain SMTP message distinguishes you from someone claiming to be you, so it has to ask about every message you send the bot, which is why almost nobody leaves it on. Your mail server's own verdict is the signal that finally tells the two apart. With `"verify"`, mail that authenticates cleanly goes straight through and you are asked only when it does not — which on a working mail path is close to never.

It requires `authserv_id` because an unscoped verdict is read from whichever `Authentication-Results` header arrived on top, and in the case the gate matters — your server no longer stamping — that header is the sender's own. Gating on a value the sender writes is worse than not gating, because it reads as protection. The bot refuses to start rather than run that way.

Two things `"verify"` does not change. An unevaluable verdict is held, not passed: if the check cannot reach an answer you get the question, because holding costs one confirmation while the other direction runs an unauthenticated message on a check that never happened. And it narrows only what the *own-address claim* buys — an address you trusted deliberately, via `trusted_email_senders` or `!trust`, still goes through.

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

`dmarc_canary` (on by default) is the automated version of the checklist. When mail routes on the strength of a user's own address, it reads the DMARC verdict the receiving MTA stamped in `Authentication-Results` and logs a warning — plus an alert — if that verdict is anything other than `pass`. Mail carrying no DMARC verdict at all is a separate case, silent by default; see `dmarc_canary_warn_on_missing` below. Silent when healthy.

Which header it reads depends on `authserv_id`. Unset, it reads the **topmost** one: each hop prepends its own, so while your MTA stamps, the top one is its stamp and everything below is whatever the sender chose to include. Set, it reads only the headers carrying your MTA's own authserv-id and discards the rest. Read the next section before leaving it unset.

Note that `dmarc=none` counts as a failure here, not as an absence. It means the sending domain publishes no policy — the "someone deleted the DMARC record" case — so it warns. A header the check cannot read cleanly also warns, rather than being treated as "no verdict": a sender can plant punctuation that hides the real verdict from a parser, and treating that as silence is exactly what would let them turn the check off.

What it catches, and when, is worth being precise about. Removing a DMARC record, or a mail path that stops evaluating DMARC, shows up on the next ordinary message. *Weakening* a policy from `p=reject` to `p=none` does not: legitimate mail still passes, so nothing looks wrong until someone actually forges your address — at which point the forgery itself trips the canary, rather than the config change that allowed it. Checking that your published policy is still `p=reject` stays a manual item on the checklist above.

One thing it is not: a **verifier**. It does not check DKIM itself, because if your MTA already rejects forgeries then re-implementing that check buys nothing, and getting it wrong is worse than not having it.

It used to be documented as "not a gate" as well, and under the default settings that is still true — nothing is blocked, held or rerouted, and `dmarc_canary` cannot cost you a message. But `confirm_sender_match = "verify"` makes the same verdict decide whether a self-addressed message runs. The detector and the control read one shared answer; which of the two you get is `confirm_sender_match`'s decision, not the canary's. See the next section.

It follows that an attacker who forges an `Authentication-Results: … dmarc=pass` header the check accepts suppresses the warning. That is fine, and worth being explicit about: the canary is not the boundary, the MTA is. Its job is catching misconfiguration and drift, not attack. A canary that can be silenced by the thing it is not defending against is still worth having — a canary mistaken for a control is not.

#### `authserv_id`, and why the default has a blind spot

"Topmost" is a proxy for "ours", and it holds only while your MTA stamps. The one drift case it cannot see is the one where the stamping itself stops: with no header of your own on the message, the topmost header is whatever the sender wrote, so a forged `Authentication-Results: mx.example.com; dmarc=pass header.from=you.example` reads as a healthy path. The canary reports normal, `dmarc_canary_warn_on_missing` never fires because a verdict is present, and the setting that would have made the drift visible is defeated by the drift.

`authserv_id` closes that. RFC 8601 puts the receiving host's own identity in the first field of the header, before the semicolon, and that field is what separates your stamp from one the sender wrote. Set it to your MTA's value — read it off the `Authentication-Results` header of a message you have actually received — and any header from another authserv-id is discarded rather than parsed.

Setting it says two things, and the second is what makes it worth setting. Your MTA stamps with this id, so a message arriving without your stamp contradicts your own configuration and warns on its own, without `dmarc_canary_warn_on_missing`. That flag keeps its narrower meaning: your stamp is there and carries no DMARC verdict.

It does not make the canary a boundary. A sender who knows your authserv-id — it is visible in every message your MTA has ever stamped, including replies to your own mail — can still forge a header naming it. What changes is that the forgery now has to be aimed at you, and the accident cannot happen at all.

**Finding the value.** You do not have to open a raw header. While `authserv_id` is blank, the next message that authenticates cleanly writes the observed id and the line to paste into the log, once. It has to be a message that passed: on a failing verdict the topmost header is the one under suspicion, and naming *its* authserv-id would be an invitation to scope the check to a spoofer's own stamp — so an alert about a failing check tells you the setting exists and deliberately names no value. Confirm the id is really your mail server before setting it either way.

#### Two checks that run either way

These apply to whichever header the canary reads, whether or not `authserv_id` is set.

It reports the `dkim=` and `spf=` verdicts alongside the DMARC one, because a `dkim=pass` next to a `dmarc=fail` is a partial misconfiguration and reads differently from a wholly broken path. Neither changes the verdict; DMARC is the verdict.

And it checks the `header.from` the MTA recorded against the `From:` domain the mail actually routed on. A `dmarc=pass` says the MTA authenticated some address, and taking that as a statement about *this* sender is the assumption worth dropping. A mismatch warns, and so does a `header.from` that is present but unreadable. A subdomain of the `From:` domain (or the other way round) counts as aligned, because DMARC's own relaxed mode aligns on the organizational domain and some MTAs record the domain they evaluated rather than the literal one. Many MTAs do not emit the property at all, and that absence is not a mismatch — it means the check could not run, so it stays silent.

**This is the one thing that changes for an existing deployment on upgrade.** Everything else here is either unchanged or waits for you to set `authserv_id`, but the alignment check runs on the default config, so a `dmarc=pass` about a different address that was previously silent now raises a warning. That is a finding worth seeing rather than noise, but it is new.

`dmarc_canary_warn_on_missing` (off by default) extends the check to mail whose stamp carries no DMARC verdict at all. It is off because a mail path that evaluates nothing would otherwise warn on every message, which trains you to ignore it. Turn it on once you know your MTA does evaluate DMARC — with `authserv_id` unset it is also the only way "the mailbox moved somewhere that does not evaluate DMARC" ever becomes visible.

Alerts are deduplicated per sender and verdict for 24 hours, so a persistently broken path does not flood the channel. The log warning is not deduplicated, so there is still a per-message record.

#### With `confirm_sender_match` set to verify or gate

It applies to whichever route the mail takes, not only to sender-match routing. Routing is decided by the recipient first, and the plus-address is public — it is the `From:` on every message the bot sends on the user's behalf — so a sender who knows the address the gate is about also knows how to arrive as a plus-addressed message instead. The same claim gets the same answer either way. Mail from a genuinely external sender is unaffected by the flag; it is gated or not on the existing plus-address rule.

Two escape hatches keep it usable. An address listed in the user's `trusted_email_senders` is exempt outright, and `!trust <address>` adds one at runtime. Both are deliberate grants rather than the header trusting itself — but an address trusted either way is then trusted for anyone who can spoof it, so a deployment that turns the flag on for the spoofing protection should not immediately trust its way back out of it. For that reason the confirmation prompt for a self-claim offers only `yes` and `no`; the `yes trust` shortcut is offered only for genuinely external senders, where trusting them costs nothing this gate protects.

Two limits to know. An unanswered confirmation is auto-cancelled after `scheduler.confirmation_timeout_minutes`, so leaving the flag on with no watched Talk channel drops inbound mail rather than queuing it (an undeliverable prompt is logged as a warning). And attachments are downloaded to the user's `inbox/` before the gate runs, so declining a message holds its *processing* — the attached files have already landed and are not removed.

### What trusting a sender means

One list, two meanings. `trusted_email_senders` decides both that this person's mail is processed without asking you, and that mail *to* this person is sent without waiting for your approval. Trusting someone so their newsletter stops interrupting you also authorizes the bot to write to them unprompted.

That is a deliberate trade — the alternative is two lists that drift apart — but it has one consequence worth knowing: every entry written before the outbound gate shipped was made under the narrower inbound-only meaning, and those entries now carry the wider one. Read your list once (`!trust` with no argument prints it) if that matters to you.

A catch-all pattern (`*`, `*@*`) therefore turns the `untrusted` outbound policy off entirely. It is logged when it happens, but narrow the pattern rather than relying on the log.

Trust also decides how much a correspondent can do. On an [email thread room](#email-thread-rooms) a trusted or admitted sender's mail runs as you, at your full reach, with your memory loaded: the sender can ask the bot about anything you can reach, and its reply goes to everyone on the thread. What it reveals there is bounded by the model's judgement of what is private, and, for anyone you have not trusted, by the outbound gate holding the reply as a draft. There is no per-scope limit. Trust senders sparingly, and prefer narrow patterns over a domain or a catch-all.

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

A reply held at the delivery leg raises a notification the moment it is held. That is not decoration: the assistant finished its turn believing the reply went out, and has usually already told you so, and the draft card is in a chat you may not have open, so without the notice the hold could go unseen until the 24-hour reminder. `!drafts` releases or discards it. Approving sends the reply threaded onto the original message, from the recipients and headers snapshotted at hold time.

One fidelity note. A held message stores a single body, so a briefing held on its way into an email thread is released as plain text, losing the HTML alternative with its article links. What you approve is what is sent, which is the property worth keeping; the links are the cost.

One thing worth knowing about what an inbound approval means. Answering `yes` to an inbound confirmation prompt approves *reading* that message and admits its sender to that one thread, so their later mail on it passes the inbound gate. It writes no trust row, so it does not authorize mailing that sender back — the reply is held separately under whatever policy applies. Answering `yes trust` does authorize both, because it adds the address to your trusted list.

## Mail the bot sends

When the bot sends an email on behalf of a user, the outbound message is tracked in the `sent_emails` table (Message-ID, recipients, user, and the room the send came from). A send to anyone besides the user creates the thread's [room](#email-thread-rooms), and every reply to it is a turn in that room, answered there by mail. The room you asked from is your private conversation and gets no copy of the replies.

### Finding a reply to something the bot sent

Every read verb takes `--scope {mine,shared,all}`, defaulting to `all`. `mine` means mail addressed to your `bot+<you>@…` plus-address, mail from your own address, **or** a reply to a thread you started.

That third arm matters when a correspondent answers the bot's plain address rather than your personal one. Such a reply is delivered and answered normally, but `list --scope mine` did not show it — so the one query you run when you suspect something went missing was the query that hid it.

`list` narrows the fetch server-side, so its thread arm reaches back over your last twenty-five or so sent messages. `search` filters the whole window client-side and has always found these; use it when hunting a reply to something older than that.

## Email thread rooms

A thread with two or more people besides the bot becomes a [shared room](shared-rooms.md) for its host: the Istota user whose thread it is. So does any mail the bot sends for a user to someone other than that user, and a stranger's mail to a user's plus address once it is admitted. The room is the mail thread itself: every message in it is a mail that came in or one the bot sent, and what the bot has to say to the host goes to the host's private chat as a [note](#the-note-in-your-private-chat). Mail between you and the bot alone is your [private email room](#your-private-email-room) instead.

- **When.** A mail the bot sends for you (with `email send` or `email reply`, a draft you release, or the answer to a mail) creates the thread's room as it goes out, whenever anyone besides you is on To or Cc, with the sent mail as the room's first message. Bcc is never recorded. Mail only to your own addresses creates no thread room. A mail received creates a room when the thread is the host's (their address is on it, it replies to a mail the bot sent for them before rooms were created at the send, or it came to their plus address) and the sender got past the [confirmation gate](#email-confirmation-gate). A reply to a mail the bot sent, or a stranger's first mail to `bot+<you>@`, is a room with one correspondent on it. A held mail is not added to any room until you approve it; approving it adds it, creating the room if this is the stranger's first mail. Declining it creates nothing.
- **Who.** Everyone on From, To and Cc over the thread is a participant, and nobody leaves by being dropped from Cc. The thread is the host's correspondence, so the host is its only member. Another Istota user on the thread takes part as a correspondent like anyone else, and the web refuses to add them as a member. Rooms that had extra members before this was the rule lose them on upgrade; those people keep everything that reached their own inbox.
- **When the bot replies.** Istota decides from the message's own headers, before any task exists. On the host's own mail: with the bot in To, the bot replies; in Cc it listens, unless the new part of the message names it. On anyone else's mail, being in To says nothing (every reply-all on a thread the bot started has it there), so the bot replies when the new part names it, or when the host is on neither To nor Cc, since then nobody else will tell them. A correspondent's reply-all that the host is on and that does not name the bot is recorded in the room and nothing else happens. Naming the bot means `@<name>` anywhere, or the name as the first word of any line ("Hi all,\n\nZorg, when did we last meet them?"). "Ask Zorg about it" is about the bot, not to it, and does not count. Quoted history is not read, so a reply quoting an earlier question does not ask it again: text below an `On … wrote:` line (and its German and French forms), `>`-quoted lines, a forwarded or original-message block, and Outlook's quoted `From:`/`Sent:` header. A client that quotes in some other form is read as new text. The thread stays on this rule even when the deployment uses the classifier. A host who is only Bcc'd, or who gets the mail through a list or a forwarding alias not among their addresses, counts as not on the message.
- **A note for the host.** When the host was not on a message, they get one note about it in their private chat, whatever the bot did. When they were on it, they get one only when something needs them. See [the note in your private chat](#the-note-in-your-private-chat).
- **How it replies.** A reply goes to everyone on the latest message (its sender in To, its other recipients in Cc, without the bot's own addresses), threaded to the message that asked, through the [outbound approval gate](#the-outbound-approval-gate), which checks every recipient. A held reply is a draft, approved from the note in the host's private chat. `room post` and guest proposals do not reach an email thread: a post there is refused, and the text goes out with `email reply-all` instead.
- **The host's own question.** When the host asks the bot something on the thread, in front of everyone on it, the answer is sent without an outbound hold. Asking there is the host's consent for the answer to reach those people. This applies only when the host's mail passed DMARC as stamped by your own MTA, which needs [`authserv_id`](#confirm_sender_match) set (a `From:` naming the host is not enough, and without `authserv_id` the verdict could be one the sender wrote); only when the mail asks rather than mentions (the bot in To, `@<name>`, or the name followed by a comma or colon, not "Zorg booked the table"); only to the task that mail created; and only while the reply goes to exactly the people that mail went to. A forged or unauthenticated mail, a scheduled job, a subtask, or a thread that gained a recipient since the question are all gated as before. The bot runs at the host's full reach on that turn, and whatever it puts in the answer reaches everyone on the thread.
- **Correspondents run as the host.** There is no guest mode on an email thread. Every admitted message, whoever wrote it, runs as the host at the host's full reach, with their memory (`USER.md`, recalled memories) loaded, as an email to the bot always has. The mail reaches the model marked as untrusted input. What bounds such a turn is the two gates: the [confirmation gate](#email-confirmation-gate) decides whose mail is processed at all, and the [outbound approval gate](#the-outbound-approval-gate) holds the reply as a draft unless every recipient is trusted. The room's guest reply setting does not apply to an email thread and is not shown in its settings. See [what trusting a sender means](#what-trusting-a-sender-means) for what this makes of a trusted sender.
- **Private replies.** An email thread has no private chat of its own. What the bot has for you alone goes to your private chat on web, Talk or WhatsApp (or the notification bell when you have none). It is never mailed, either as a reply on the thread or to your own address.
- **No reply limit.** On other shared rooms the bot stops answering guests after three of its own replies with no member speaking. An email thread has no such limit, since a correspondent's mail runs as the host, as email always has. A mail loop is bounded by the [volume limits](#volume-limits), which count every message from a sender.
- **Newcomers.** Anyone newly copied on the thread narrows what the bot draws on to the conversation since they joined.
- **Switching it off.** Anyone on the thread can reply with `!<name> off` as the first line. See [switching the bot off](room-veto.md).
- **No announcement.** The bot does not introduce itself on an email thread: mail from the bot's address is from the bot. If you want every mail it sends into a thread to say so, set `thread_disclosure_footer = true` under `[email]`. Each plain-text mail then ends with a line like "Written by Zorg, an AI assistant, for Carol. To stop it replying on this thread, reply with `!zorg off` as the first line." Off by default.

### What the thread room shows

The thread room is a view of the mail, read-only in web chat.

- **Every row is a mail card.** A mail that came in shows who sent it, To and Cc (your own addresses read "you", the bot's address its name, and a list longer than three folds behind "+N more"), the subject, the date and its attachments, with the new text first and the quoted history and signature behind "Show quoted text". A badge says whether the sender is on your trusted list, passed the sender check, failed it, or was not checked. A mail the bot sent shows as the card alone, with its body inside and its state: sent, held for your approval, not sent or discarded. A held mail links to its note in your private chat and has no approve or edit buttons in the thread. Mail recorded before this card existed shows only its sender, subject and date.
- **The card's menu** copies the sender's address (on a sent mail, the first recipient's), shows the stored headers (Message-ID, In-Reply-To, date, the sender check and whether the sender is trusted), and opens "Discuss in private chat". Nothing on the card is read from the raw headers when you open it: everything is stored when the mail arrives. Bcc is never stored.
- **What the bot sent, not what it told you.** The bot's message in the thread is the body it mailed with `email output`. A turn that mailed nothing adds no message to the thread. The bot's own remark to you goes in the note.
- **No composer.** A message typed in the thread used to become an ordinary web chat turn that mailed nothing, so web chat refuses a send, a `!command` or a reply there with "This is an email thread. Ask from your private chat and the bot will draft the reply." Confirming a question parked on the thread, acting on its draft and retrying a failed turn still work, since none of those is a new message.
- **Out of the room list.** Thread rooms sit in a collapsed "Email threads" group below your other rooms, and their messages count toward neither the Unread total nor the All, Unread and Starred views. The note's `re:` chip, a link and the group still open one. "Show in room list", in the room's settings or its menu, puts one thread back in your main list; the choice is yours alone.

### The note in your private chat

For every mail on a thread that you were not on, the bot writes one note in your private chat (your default web room, or your Talk or WhatsApp chat; see [private replies](shared-rooms.md#private-replies)). When you were on the mail, it writes one only when a reply is held for your approval, a question waits for you, a send failed, or the bot answered you alone without mailing anyone. A reply-all that went out is already in your inbox, so it gets no note.

The note reads, in order: who wrote on which thread (and "without you on the message" when you were not on it), the new part of their mail quoted (up to 500 characters, never the quoted history), one line saying what happened ("Replied.", "Reply waiting for your approval.", "Question for you.", "The reply could not be sent." or "No reply sent."), and then the bot's own remark, if it made one. Everything above the remark is built by Istota from the stored mail, never written by the model.

In web chat the note carries the mail the bot sent as a one-line card that expands, and a held reply's draft card under it with Send, Edit and Discard. Both follow the mail's current state, so a draft you send from the note shows as sent there and in the thread. A question the bot parks on a thread is itself the note, with "Question for you." and the confirmation card under it. On Talk and WhatsApp the note is posted as text. A web-only private chat gets one push per note, to ntfy or email and never to a Talk room. With no private chat at all, the note is a notification in the bell. A retried task writes no second note and sends no second push.

To talk about a thread, reply to its note. When there is no note (you were on the mail and the reply went out), "Discuss in private chat" on a mail card opens your private chat with a `re:` chip naming the thread: your next message there is about that thread, and the bot sees its recent mail. Clear the chip, send, or leave the room to drop it.

## Your private email room

Mail between you and the bot alone, with nobody else on From, To or Cc, is a turn in your private email room. It is the email counterpart of your SMS thread or private WhatsApp chat: one room per user, created by your first such mail, with you as its only member.

- **What lands there.** Each mail you send the bot from one of your own addresses, at its plain address or your plus address, and the bot's answer, which goes back as a reply to that mail and to you alone. Mail the bot sends only to your own addresses (a briefing, a reminder, an `email send` to yourself) is added too once the room exists. Nothing creates the room except your own mail.
- **Read-only in web.** The room shows in your web sidebar with an envelope, and each message in it is a mail card, as in a [thread room](#what-the-thread-room-shows). You cannot write into it from web, since a message sent there would not go by mail: reply by email to continue it. Members cannot be added, and it is never used as your default web room.
- **Scheduling into it.** A task in this room is told to use `target = "email"` to deliver into it from a scheduled job. That mails you and records the mail here.
- **Not a thread.** No one else's mail lands in it, even one that quotes or replies to a message in it: a Message-ID is not a secret. Mail with anyone else on it is a [thread room](#email-thread-rooms).
- **Held mail.** With [`confirm_sender_match`](#confirm_sender_match) set to `verify` or `gate`, a mail claiming your address that does not pass is held. Approving it adds it to the room and runs it.

Mail you sent yourself before this existed is not copied into the room.

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

Polling interval is controlled by `email_poll_interval` in `[scheduler]` (default 60s), and `email_poll_batch_size` (default 50) caps how many messages one poll walks. The cap is a batch boundary, not a window: each poll takes the oldest unprocessed mail and leaves the rest for the next tick, so a burst larger than one batch drains in arrival order instead of burying the messages underneath it. A poll that fills its batch logs that a backlog remains. Mail is deleted from the IMAP folder after `email_retention_days` (default 7) via a server-side date search, so the sweep keeps working on a busy mailbox. It deletes everything in the folder past the cutoff, not only mail Istota processed, and the deletion is permanent — set the window deliberately, and note that the first run after upgrading from a version whose sweep silently did nothing will clear the accumulated backlog (the candidate count is logged before anything is removed). A backlog is drained a couple of thousand messages per cleanup tick rather than in one pass, so a large one clears over several minutes; each tick logs how many are left. The record of which messages have already been processed is pruned separately after `processed_email_retention_days` (default 90) — always at least as long as the mail itself, so a message still in the folder can't lose its record and be ingested a second time.

That record is keyed on `(uidvalidity, email_id)`, not on the id alone. IMAP UIDs restart when a mailbox is recreated, so a move to a new mail server, or a rebuilt mailbox, used to make every new message look like one already handled — silently skipped, with no error and no log line.

### Volume limits

`bot+{user_id}@domain` is public by construction — it is the `From:` on every mail the bot sends on a user's behalf, which is the whole point, since replies have to route back. So everyone the user has ever corresponded with through the bot holds a working address that turns one message into a task on that user's account. Four limits bound what that can cost, all in `[scheduler]`:

- **A per-user budget** — `email_rate_limit_messages` (default 60) inbound email tasks per `email_rate_limit_window_seconds` (default one hour), counted over a sliding window of recent tasks.
- **A per-sender budget under it** — `email_sender_rate_limit_messages` (default 20), so one loud correspondent throttles alone instead of consuming the whole allowance. A mailing list the user subscribed the address to and forgot about hits this without anyone meaning harm, which is the ordinary case it exists for. It bounds an *unintentional* flood: the sender is the unauthenticated `From:`, so somebody deliberately rotating addresses falls through to the per-user cap, where their correspondents' mail is filed too and reachable only with `email from-senders`.
- **A body cap** — `email_max_body_chars` (default 32000). The body goes into the prompt whole, so one very long message is its own amplification with no flood required. Past the cap it is truncated with a marker saying so; the full message stays in the mailbox.
- **Attachment caps** — `email_max_attachment_bytes` per message (25 MiB) and `email_max_attachment_bytes_per_poll` across a whole poll (100 MiB). These bound what is written to disk and pushed to Nextcloud, not the IMAP transfer: the mail client fetches and decodes a whole message before any attachment can be inspected. Whole attachments only, since a half-file nothing knows is half is worse than an absent one — and the prompt names anything skipped, so the bot never answers "see the attached invoice" as though there were no invoice.

**Over-budget mail is filed, not dropped.** It gets a `throttled` record, stays in the mailbox, and creates no task — the same treatment a user's configured quiet senders get, applied automatically. Ask the bot to read it later with `email from-senders` and it is all still there, until `email_retention_days` (default 7) sweeps the folder like any other mail. A limit that discarded would be silent mail loss with a config knob on it, which is the failure this whole area exists to avoid.

The user is told **once per window**, not once per message: one alert naming how many were filed and which senders sent them. The untrusted-sender confirmation prompts collapse the same way, since they are the other route from a mail flood to a notification flood — past `email_confirmation_prompts_per_window` held messages from one sender (default 3), the individual prompts stop and a single notice covers the rest. Every held message is still held and still individually answerable with `!confirm <task-id>`; only the interruption is collapsed. The two notices are deduplicated separately, so being told about throttled mail never costs you the notice about held mail — held mail is on a two-hour clock and is cancelled if nobody answers, while filed mail just sits in the mailbox.

Separately, inbound mail runs on the **background** worker queue (`email_task_queue`, default `background`). Email is the only surface an unauthenticated stranger can create work on, and the one whose turnaround nobody is watching — a poll interval of 60s already sets that expectation — so it should not compete for the worker slots a live Talk or web-chat turn needs. Scheduled work is unaffected: briefings and cron jobs run at a higher priority than inbound mail, so a burst queues behind them rather than in front. Two consequences to know before changing it. Mail is slower under load, which is the trade. And because the per-room "one turn at a time" rule applies to the interactive queue only, a mail turn and a live chat turn in the same room can now run at the same time; the same rule is what used to let one unanswered confirmation freeze a thread for two hours, which no longer happens. Set it to `foreground` to restore both behaviours.

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
