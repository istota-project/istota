# WhatsApp

Istota can receive requests and send final replies over WhatsApp, through one of two adapters. One WhatsApp number, text only.

**`baileys`** is the default. It holds a WhatsApp Web session paired to a phone, the way a linked desktop client does, so there is no Meta account, no per-message charge, no 24-hour window and no template. It runs a small Node sidecar beside Istota and is paired by scanning a QR code. WhatsApp does not sanction this outside its Business API, so it carries risks the Cloud adapter does not — see "The trade Baileys carries" below.

**`whatsapp_cloud`** is Meta's hosted Cloud API, used directly rather than through Twilio, 360dialog or another Business Solution Provider. It is the sanctioned path. It needs a Meta business account and business verification, it is metered, and a free-form reply is only allowed inside 24 hours of the user's own message.

A WhatsApp exchange is outside the room model on either adapter. It never creates or joins a Talk or web room, never copies a message into a room transcript, and never appears in a web composer. The task and its result stay available in the task history and the admin task views like any other task.

Both adapters handle private text messages and the STOP, START and HELP keywords. Images, documents, audio, video, stickers, contacts, locations, reactions, edits, deletions, calls, Flows and payments are not handled: an unsupported message gets one fixed reply asking for text, and nothing is downloaded. Confirmation questions carry Yes and No buttons on Cloud and arrive as plain text on Baileys; either way a typed `YES` or `NO` answers them, and so does `!confirm <id> yes|no`.

## Choosing an adapter

|  | `baileys` | `whatsapp_cloud` |
| --- | --- | --- |
| Account | a phone with WhatsApp, scanned once | Meta business portfolio, WABA, business verification |
| Cost | none | per Meta's pricing; see below |
| Reply window | none | 24 hours from the user's last message |
| Templates | not applicable | one approved utility template, under paid mode |
| Receives over | a Unix socket from its own Node sidecar | a signed HTTPS callback at `/webhooks/whatsapp` |
| Extra process | the sidecar (its own image or systemd unit) | the shared webhook receiver |
| Sanctioned | no | yes |

Set it in `[whatsapp] provider`. A deployment runs one adapter at a time and there is no failover between them.

**Write `provider` explicitly.** An existing Cloud configuration that names no provider keeps working — Istota reads a `[whatsapp]` block carrying a WABA id, a phone number id or one of the three credentials as a Cloud deployment — but a config written today should say which adapter it means.

## The trade Baileys carries

Baileys speaks WhatsApp's Web protocol as a linked device. That is not a supported automation interface, and three consequences follow.

**Account ban risk.** WhatsApp can and does ban numbers it reads as automated. Use a number dedicated to Istota, do not send in bulk, and treat the account as expendable rather than as one you cannot lose. A ban takes the number, not just the session.

**Protocol breakage.** WhatsApp changes the Web protocol without notice, and the adapter stops working until the library releases a fix. The library version is pinned in the sidecar's `package.json`; recovering from a break means updating it.

**The device unlinks.** WhatsApp drops a linked device after a long stretch with the phone offline, and the session can also end if you unlink it from the phone. Istota notices: sends refuse rather than silently dropping, `istota doctor --only whatsapp.baileys_bridge` fails, and an alert goes to the admins off the WhatsApp surface.

Recovery is `istota whatsapp pair --reset`. The plain `pair` cannot do it, and that is worth understanding rather than remembering: the credential left on disk names a device WhatsApp has unlinked, and the library reads it as a registered account and tries to log in with it rather than offering a code. So every restart is refused and no QR is ever drawn — under systemd that is a restart every thirty seconds, for as long as it takes somebody to notice. `--reset` moves the directory to a timestamped sibling and pairs into a fresh one. It deletes nothing, so a session that turned out to be merely unreachable has lost no keys, and it still refuses to run while a bridge is listening on the socket. Stop the scheduler and any sidecar running as a unit of its own first, as you would for a first pairing; remove the old directory yourself once the new session works.

Until somebody does that, the sidecar keeps starting, is refused, and exits. It spaces those attempts out rather than making one every thirty seconds: no wait on the first, then 30 seconds, 5 minutes, 15, 30, an hour, counted in `logout-backoff.json` inside the session directory. Each attempt is a login against a number WhatsApp has already unlinked, which is exactly the kind of client behaviour the paragraphs above are about, so leaving an unlinked session running for a week is no longer expensive. Re-pairing is not slowed by it — the wait ends as soon as the credential is replaced.

None of that applies to the Cloud adapter, which is why it stays available. If you cannot afford to lose the number or the surface, use Cloud.

## Setting up Baileys

You need a phone with WhatsApp on the number Istota will use, a host with Node, and the sidecar's dependencies installed.

1. Set `provider = "baileys"` and `enabled = true` in `[whatsapp]`, and `business_phone_number` to the number in E.164 form. Nothing else is required; `[whatsapp.baileys]` defaults are fine.
2. Stop the Istota scheduler, **and any sidecar running as a unit or compose service of its own**. Two Baileys clients against one session directory corrupt it, and Istota can only detect one of the two.
3. Install the sidecar's dependencies: `npm ci` in `docker/whatsapp-baileys/`.
4. Run `istota whatsapp pair`. It draws a QR code in the terminal — nothing to install, and the code is redrawn each time WhatsApp rotates it, which is every twenty seconds or so. On the phone, open WhatsApp, go to Linked Devices, and scan.
5. When pairing reports the session is ready, start the scheduler and whatever runs the sidecar. **The daemon spawns no sidecar by default**: with `[whatsapp.baileys] sidecar_command` empty it only listens, which is what a deployment running the sidecar as its own unit or compose service wants. Set `sidecar_command` if you want the daemon to spawn it instead.
6. Check it: `istota doctor --only whatsapp.` should report `whatsapp.baileys_session` ok. The bridge check answers only inside the process holding the bridge, so read `whatsapp.baileys_bridge` from the admin Health pane or `!check` rather than from a shell.
7. Bind a user and message the number from their phone.

The paired session lives in a `whatsapp-baileys-session` directory beside the framework database, unless `[whatsapp.baileys] session_dir` says otherwise. It is 0700, owned by the account the daemon runs as, and Istota refuses to start the bridge against a directory belonging to anyone else.

**That directory is a full-account credential.** Whoever has it can send and read as the paired number, with no second factor. There is no revocation short of unlinking the device from the phone. Back it up the way you would back up a private key, or not at all.

## What the Cloud adapter costs

Meta charges nothing today for a service message sent inside the 24-hour window that opens when a user writes to you, and charges per delivered message for templates. Meta has announced a change for 1 October 2026: each business phone number gets 1,000 free service messages a month and later service messages become billable, and a utility template sent inside an open service window becomes billable too. Prices vary by the recipient's market and Meta can change them. See [Meta's pricing page](https://whatsappbusiness.com/products/platform-pricing/) and the [October 2026 pricing documentation](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing).

Going direct avoids a provider markup and a monthly BSP fee. It does not make production messaging free.

Istota's Cloud default is free-biased, which is not the same as free. In `billing_policy = "free_guard"` it sends no templates, caps its own service-message attempts at `monthly_service_attempt_limit` per calendar month, and blocks every later send the first time a delivery callback says a message was billable. Three things that cap cannot see:

- messages sent from a WhatsApp Business app sharing the number through coexistence;
- messages sent by another application against the same WABA;
- a pricing rule Meta changes after this was written.

So the cap is conservative only when the business number is dedicated to Istota. The default of 900 leaves headroom below Meta's announced 1,000 for that reason, and the headroom is a margin rather than a guarantee.

The cap bounds **service attempts alone**. Under `allow_paid`, template sends are bounded by nothing local. That asymmetry runs the wrong way at the boundary: with the service cap spent, an open window (the cheapest send there is) is refused while a closed window still bills a template. `istota doctor --only whatsapp.billing` reports both counts so the volume is visible.

None of this applies under Baileys. The billing check skips itself there, because there is no circuit, no spend and no cap to report.

## Three Cloud operating modes

**Test.** Meta's test phone number, up to five allowed recipient numbers, and the dashboard's temporary token, which expires after 24 hours. Good for a local round trip and nothing else. The test number is not an operational service and is not a permanent free-tier entitlement.

**Free-biased production.** A registered business number, `billing_policy = "free_guard"`, no templates, the 900-attempt local cap and the billable circuit breaker. This is the Cloud default and the shape most personal deployments want.

**Paid production.** A registered business number, `billing_policy = "allow_paid"`, a payment method in Meta, and optionally one approved utility template so a result can still reach the user after the 24-hour window closes. Set `monthly_service_attempt_limit` to your own budget, or `0` for no local cap.

## Meta setup

Portal labels move; these are the steps as of writing.

1. Sign in with a Meta developer account and create or select a Meta business portfolio.
2. Create a Business app and add the WhatsApp product. Let the setup flow create or attach a WhatsApp Business Account (WABA).
3. For development, use the test phone number the flow provides and add your own number to its allowed recipients. Note the temporary token for a short local test.
4. For production, register a business phone number and complete whatever Meta asks of your account: SMS or voice verification, display-name review, business verification, a payment method, and app publication or review. A number dedicated to Istota is the simplest shape and the only one the local cap is conservative about. Coexistence with an existing WhatsApp Business app may be available; treat it as a paid shape, because messages the app sends never pass through Istota's ledger. A personal WhatsApp account is not silently converted into a business one.
5. Create a system user, assign it the app and the WABA, and generate a long-lived token with `whatsapp_business_messaging`. Add `whatsapp_business_management` only if you want the opt-in live readiness reads. Do not grant `business_management`.
6. In the app's WhatsApp configuration, set the callback URL to `https://assistant.example.com/webhooks/whatsapp` and the verify token to whatever you put in `verify_token`. Save, then subscribe the app to the WABA and to the `messages` webhook field.
7. If you need proactive delivery, create one utility template whose body is fixed text around a single parameter, get it approved in the exact language you will configure, and enable paid mode. Keep a copy of the approved body text: Istota's renderer has to be checked against it, and Meta rejects a parameter that does not fit.

Meta's own references: the [Cloud API collection](https://www.postman.com/meta/whatsapp-business-platform/documentation/wlk6lh4/whatsapp-cloud-api), the [webhook payload reference](https://www.postman.com/meta/whatsapp-business-platform/folder/tduohwq/webhook-payload-reference), and [subscribing an app to a WABA](https://www.postman.com/meta/whatsapp-business-platform/request/c1ai24q/subscribe-to-your-waba).

## Configuration

```toml
[whatsapp]
enabled = true
provider = "baileys"            # or "whatsapp_cloud"
business_phone_number = "+15551234567"

[whatsapp.baileys]
session_dir = ""                # defaults beside the database
library_version = ""            # checked against the sidecar's pinned version
sidecar_command = ""            # empty: the daemon listens and spawns nothing
```

The Cloud adapter's own settings sit in their own block, and are read only under `provider = "whatsapp_cloud"`:

```toml
[site]
hostname = "assistant.example.com"

[whatsapp]
enabled = true
provider = "whatsapp_cloud"
business_phone_number = "+15551234567"

[whatsapp.cloud]
waba_id = "100000000000001"
phone_number_id = "100000000000002"
access_token = "..."
app_secret = "..."
verify_token = "..."
graph_api_version = ""
business_timezone = "America/Los_Angeles"
request_timeout_seconds = 10
billing_policy = "free_guard"
monthly_service_attempt_limit = 900

[whatsapp.proactive_template]
enabled = false
name = ""
language = "en_US"
```

**The Cloud keys used to sit flat on `[whatsapp]`, and that spelling still loads.** A flat block carrying a `waba_id`, a `phone_number_id` or one of the three credentials and no `provider` key is read as a Cloud deployment with those values nested, so an existing configuration keeps working unchanged. Moving them under `[whatsapp.cloud]` is safe on its own — the same rule reads the nested block — and writing `provider = "whatsapp_cloud"` beside them is clearer. The flat spelling will be removed.

`waba_id` and `phone_number_id` are the decimal ids from the app's WhatsApp product page, not phone numbers. An event carrying any other WABA or phone number id is refused. `graph_api_version` empty follows the version the installed PyWa release pins; set a `v25.0`-style value only for a controlled migration. `business_timezone` is the IANA zone the monthly cap's calendar month is computed in.

`business_phone_number` is required under Cloud, where the account is configured by hand, and optional under Baileys, where the paired session carries its own number. Set it anyway if you want to enroll a user who has not written in yet: it is what Istota addresses a first message to.

`enabled = true` with a malformed provider, id, number, timezone, policy or limit fails the config load, and only the active adapter's settings are checked — a Baileys deployment is not asked for a WABA id. The three Cloud credentials never fail the load: they are reported by `istota doctor` and refused at send time, because the Ansible role renders them empty on purpose and delivers them through `secrets.env`.

The full setting table is in the [configuration reference](../configuration/reference.md#whatsapp).

## Identity and enrollment

The durable identity is never the phone number. On Cloud it is the sender's business-scoped user id (BSUID), because a WhatsApp user with a username may have no phone number in the payload at all. On Baileys it is the JID, WhatsApp's own `<number>@s.whatsapp.net` address. Either way, a recycled number must never take over an existing Istota principal.

Enrollment is an operator action, and on both adapters it starts from a number:

```bash
istota user ensure --name alice --whatsapp-number +15551234567
istota user ensure --name alice --whatsapp-bsuid US.1234567890
istota user ensure --name alice --whatsapp-number +15551234567 --whatsapp-bsuid US.1234567890
istota user ensure --name alice --reset-whatsapp-identity
istota user ensure --name alice --clear-whatsapp
```

The bootstrap number is used for the first binding and as an outbound fallback. On the user's first authenticated message Istota latches the adapter's own identity onto the binding — the BSUID under Cloud, the JID under Baileys — and from then on that is what identifies them. `--whatsapp-bsuid` is the Cloud escape for a user with a username and no reachable number; there is no matching flag for a JID, because a Baileys user always has a number.

The binding records which adapter established the identity, and an inbound message from the other adapter never resolves through it. A message arriving from an adapter that is not the configured one creates no task at all.

That binding is a credential. Whoever holds it can create tasks, run commands and answer that user's pending WhatsApp confirmations as them. Meta's signature proves which account sent the webhook, never that the same person still holds the line, and Baileys has no signature at all — what it has is a local socket only the daemon can open. Change or clear the binding when a number is lost, transferred or recycled.

Changing the bootstrap number discards everything learned about the previous holder, including the opt-out and the 24-hour service window, so Istota cannot free-form message a stranger who never wrote in. `--reset-whatsapp-identity` clears the learned identity for both adapters and keeps the number; `--clear-whatsapp` removes the binding. If a bootstrap number matches a user whose stored identity for that adapter is a different one, Istota refuses the message, creates no task and raises one operator alert off WhatsApp.

`istota user ensure` masks what it prints. `istota user show` returns the complete values, and is the private operator surface for that.

## Routing

Use a bare `whatsapp` destination. `whatsapp:+15551234567` is refused, so routing cannot become an arbitrary-contact send API; a bare `whatsapp` always resolves through the addressed user's current binding, read immediately before the send.

`all` and `both` are unchanged. Enabling the transport adds no WhatsApp delivery to a route somebody set up before it existed. `talk,whatsapp` and `all,whatsapp` work.

## The 24-hour service window (Cloud only)

Meta allows a free-form service message only within 24 hours of the user's latest message. Istota keeps a five-minute safety margin, so its local window is open while `now` is before `last_user_message_at + 23h55m`. Meta stays authoritative and can still refuse a send the local state allowed.

Outside the window there are two outcomes. In `free_guard` the send records `window_closed`, makes no API call and raises an operator alert off WhatsApp; the completed task is still readable through the task tools. In `allow_paid` with a template configured, one approved utility template carries the result in its body parameter. Istota never sends a generic wake-up template that leaves the answer stranded until the user replies, never infers a category, never substitutes a language, and never creates or submits a template.

A confirmation question is an interactive message, and Meta caps an interactive body at 1,024 characters rather than the 4,096 a plain text message allows. Istota sizes the question to whichever limit applies. Behind a template, where quick-reply buttons are not available, the question is still answerable: a typed `YES` or `NO` resolves it, and so does `!confirm <id> yes|no`.

Baileys has no window and no interactive message, so a reply goes out whenever the task finishes and a confirmation question gets the full text budget.

## Delivery, and what "sent" means

Istota sends at most one WhatsApp message per logical response, rendered to the adapter's limit with a truncation note rather than split into several messages. A row is written before the network call, so a timeout can never become a second send.

Acceptance is not delivery on either adapter. Task and operator views distinguish accepted, sent, delivered, read, failed, window closed, budget exhausted, billing blocked, opted out, unconfigured and delivery unknown. A definite rejection is `failed`. A failure that may already have been accepted — a timeout, a dropped socket — is `unknown`, and Istota never retries it: a duplicate private answer is worse than a visible unknown state. A failure alert goes out with WhatsApp removed from its route, so a broken surface cannot report itself through itself.

STOP opts the binding out after one acknowledgement, START re-enables it, and HELP sends one fixed explanation. An opt-out survives an adapter switch.

## Operations

```bash
istota whatsapp pair                    # Baileys: link the number by QR scan
istota whatsapp pair --reset            # Baileys: same, after moving an unusable session aside
istota doctor --only whatsapp.          # local readiness, the session, the billing state
istota whatsapp billing-status          # Cloud: read the circuit breaker without changing it
istota whatsapp billing-unblock         # Cloud: clear it, after checking Meta billing
```

`whatsapp.common` reports which fields are missing or invalid for the active adapter, never their values. `whatsapp.billing` reports the Cloud circuit breaker, the service attempts used against the cap, and the template attempts, which nothing local bounds; it skips itself under Baileys. `whatsapp.baileys_session` reports the paired session's permissions and ownership, and does not repair them — if it says a file is readable by other accounts, fix it and run it again. `whatsapp.baileys_bridge` reports the link to the sidecar and only answers inside the process holding it, so read it from the admin Health pane, `!check`, or the boot log rather than from a shell.

The Cloud circuit breaker is persistent and deployment-wide. In `free_guard`, the first authenticated delivery status carrying `billable = true` trips it and every later send is refused. The message that revealed the charge may already have been billed; what the breaker buys is that the next one is not. Check Meta billing first, then `billing-unblock`. Switching to `allow_paid` also clears it.

Before relying on the surface, send one real message in each direction and confirm the delivery states reach what you expect. The automated tests drive a fake at each adapter's boundary; they cannot prove token validity, template approval, number quality, what Meta bills, or that a paired session survives a WhatsApp protocol change.

## Docker

Copy `docker/.env.example`, fill in the `ISTOTA_WHATSAPP_*` values, and add the profile matching your adapter to `COMPOSE_PROFILES`. **The two WhatsApp profiles are alternatives, not a pair**: `whatsapp` starts the shared webhook receiver, which only the Cloud adapter needs, and `whatsapp-baileys` starts the Node sidecar, which only Baileys needs. A profile cannot read the rendered configuration, so pick the one matching `ISTOTA_WHATSAPP_PROVIDER`. Selecting both costs a receiver whose handlers answer 404, which is inert.

```dotenv
COMPOSE_PROFILES=whatsapp-baileys
ISTOTA_WHATSAPP_ENABLED=true
ISTOTA_WHATSAPP_PROVIDER=baileys
ISTOTA_WHATSAPP_BUSINESS_PHONE_NUMBER=+15551234567
```

```dotenv
COMPOSE_PROFILES=whatsapp
ISTOTA_WHATSAPP_ENABLED=true
ISTOTA_WHATSAPP_PROVIDER=whatsapp_cloud
ISTOTA_WHATSAPP_WABA_ID=100000000000001
ISTOTA_WHATSAPP_PHONE_NUMBER_ID=100000000000002
ISTOTA_WHATSAPP_BUSINESS_PHONE_NUMBER=+15551234567
ISTOTA_WHATSAPP_ACCESS_TOKEN=...
ISTOTA_WHATSAPP_APP_SECRET=...
ISTOTA_WHATSAPP_VERIFY_TOKEN=...
```

The `webhooks` service belongs to the `location`, `sms` and `whatsapp` profiles, so enabling several still starts one receiver. Nginx exposes `/webhooks/`; the receiver port stays inside the Compose network. Restart `istota`, `webhooks` and `nginx` after changing these. The main service renders `config.toml`; the webhook service waits for that render before it accepts a request.

**Pairing a Baileys session is not reachable from inside this stack.** `istota whatsapp pair` starts a sidecar of its own, and the istota image ships neither the sidecar program nor its dependencies. Pair on a host with a checkout and Node, then move the session directory into the `istota_data` volume at `/data/db/whatsapp-baileys-session`, 0700 and owned by the uid the containers run as.

## Ansible

The role installs the operator CLI at `/usr/local/bin/<namespace>` — `istota` on a default install — so the commands on this page can be typed by name. It is a wrapper that execs the venv's console script with `-c` pointing at the deployed config, because the config search order starts at the working directory and a bare command run from a home directory would otherwise resolve none. An explicit `-c` of your own still wins.


Set the matching `istota_whatsapp_*` role variables and vault the three Cloud credentials. They are written to the root-owned `secrets.env` and loaded by the scheduler, web app and webhook receiver; they never reach `config.toml`. The role provisions the webhook receiver when location is on, SMS is on, or WhatsApp is on with an adapter that has a callback.

```yaml
istota_whatsapp_enabled: true
istota_whatsapp_provider: "baileys"
istota_whatsapp_business_phone_number: "+15551234567"
istota_whatsapp_baileys_sidecar_unit: true       # run the sidecar as its own unit
istota_whatsapp_baileys_sidecar_command: ""      # leave empty when the unit runs it
istota_whatsapp_baileys_log_level: "info"
istota_whatsapp_baileys_memory_high: "512M"
```

```yaml
istota_whatsapp_enabled: true
istota_whatsapp_provider: "whatsapp_cloud"
istota_whatsapp_waba_id: "100000000000001"
istota_whatsapp_phone_number_id: "100000000000002"
istota_whatsapp_business_phone_number: "+15551234567"
istota_whatsapp_business_timezone: "America/Los_Angeles"
istota_whatsapp_access_token: "{{ vault_whatsapp_access_token }}"
istota_whatsapp_app_secret: "{{ vault_whatsapp_app_secret }}"
istota_whatsapp_verify_token: "{{ vault_whatsapp_verify_token }}"
```

Under Baileys the role installs Node, installs the sidecar's dependencies from its lockfile, creates the session directory 0700 owned by the daemon's account, and runs the sidecar as its own systemd unit. Setting both `istota_whatsapp_baileys_sidecar_unit` and a non-empty `istota_whatsapp_baileys_sidecar_command` is refused: that pairing arranges two Baileys clients against one session directory, and neither process can detect the other. Enabling the sidecar on a host needs one full play first — the update-only mode does not install Node.

Bind a user inside `istota_users`:

```yaml
istota_users:
  alice:
    whatsapp_number: "+15551234567"
```

Read the warning under "Identity and enrollment" before putting one there: inventory is version control, and this value carries authority. Omit the key to leave a CLI-set binding alone; `""` clears the binding.

## Reverse proxy (Cloud only)

Baileys receives over a local socket and needs nothing in front of it. The Cloud adapter's callback needs two things the application cannot enforce for itself. The shipped Docker and Ansible nginx configurations do both; a hand-rolled front end has to.

The verify token arrives as a **query value** on the GET handshake, and nginx's default log format writes the whole request line. Turn the access log off for `/webhooks/whatsapp`, or use a format that logs `$uri` rather than `$request`. Istota logs the handshake itself under a stable event name and without the token.

That is only half of it, and the other half is not in the proxy. Uvicorn's own access log renders the path with its query string, so the receiver would write the token to the container log or the journal whatever the front end does. Both shipped shapes run the receiver with `--no-access-log`, and the combined `istota serve` process has always disabled it. A receiver you start yourself needs the same flag.

The POST body limit has to sit above Istota's own 256 KiB cap so the application answers an oversized body with its own 413 rather than nginx answering with an HTML page Meta will retry against. The shipped configurations use `client_max_body_size 512k` on that one path. The raw body must reach the application unmodified: `X-Hub-Signature-256` is computed over the exact bytes, so anything that rewrites, re-encodes or buffers-and-reserializes the body breaks every request.

## Switching adapters

Change `provider`, provision the new adapter, and restart. What survives is everything that is not one adapter's: user bindings and their bootstrap numbers, opt-outs, conversation tokens, task history, and the ledger rows already written. What does not survive is the learned identity of the adapter you left and the state derived from it. On the first message after the switch, Istota latches the new adapter's identity onto the binding from the bootstrap number and raises one alert per user saying it did, because re-establishing a principal from a phone number alone is worth telling somebody about.

Going **to Baileys**, Meta's 24-hour window state is discarded — a Baileys message opens no conversation Meta knows about, and keeping the timestamp would authorize a free-form Cloud send later on. Switch back and the window fills in again from the first Cloud message.

Going **away from Cloud**, Meta's callback stops being served the moment the new configuration is loaded: both handlers answer 404 and the deployment provisions no receiver. **Meta's outstanding traffic is lost.** Messages still in its retry queue reach nothing, and an outstanding `sent_whatsapp` row keeps whatever state it had, because the delivery status that would have settled it cannot arrive either. Unlike SMS, there is no complete-but-inactive shape that keeps a switched-away adapter's callbacks authenticating. If that matters, let the surface go quiet before you flip it.

## Switching off

Set `enabled = false`. The routes refuse every request and the sidecar is not provisioned. Outstanding rows keep their last state. User bindings, opt-outs, ledger rows and conversation history all survive, so turning it back on resumes where it stopped. A paired Baileys session is left on disk: the Ansible teardown names the directory rather than deleting it, since a switch back re-uses it, and a credential deleted on your behalf during a routine converge is the wrong default. Remove it by hand if you are done with the number, and unlink the device from the phone.
