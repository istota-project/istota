# WhatsApp

Istota can receive requests and send final replies over WhatsApp through Meta's hosted Cloud API, used directly rather than through Twilio, 360dialog or another Business Solution Provider. One business phone number, text only.

A WhatsApp exchange is outside the room model. It never creates or joins a Talk or web room, never copies a message into a room transcript, and never appears in a web composer. The task and its result stay available in the task history and the admin task views like any other task.

The first version handles private text messages, Istota's own confirmation buttons, and the STOP, START and HELP keywords. Images, documents, audio, video, stickers, contacts, locations, reactions, edits, deletions, calls, Flows and payments are not handled: an unsupported message gets one fixed reply asking for text, and nothing is downloaded.

## What this costs

Meta charges nothing today for a service message sent inside the 24-hour window that opens when a user writes to you, and charges per delivered message for templates. Meta has announced a change for 1 October 2026: each business phone number gets 1,000 free service messages a month and later service messages become billable, and a utility template sent inside an open service window becomes billable too. Prices vary by the recipient's market and Meta can change them. See [Meta's pricing page](https://whatsappbusiness.com/products/platform-pricing/) and the [October 2026 pricing documentation](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing).

Going direct avoids a provider markup and a monthly BSP fee. It does not make production messaging free.

Istota's default is free-biased, which is not the same as free. In `billing_policy = "free_guard"` it sends no templates, caps its own service-message attempts at `monthly_service_attempt_limit` per calendar month, and blocks every later send the first time a delivery callback says a message was billable. Three things that cap cannot see:

- messages sent from a WhatsApp Business app sharing the number through coexistence;
- messages sent by another application against the same WABA;
- a pricing rule Meta changes after this was written.

So the cap is conservative only when the business number is dedicated to Istota. The default of 900 leaves headroom below Meta's announced 1,000 for that reason, and the headroom is a margin rather than a guarantee.

The cap bounds **service attempts alone**. Under `allow_paid`, template sends are bounded by nothing local. That asymmetry runs the wrong way at the boundary: with the service cap spent, an open window (the cheapest send there is) is refused while a closed window still bills a template. `istota doctor --only whatsapp.billing` reports both counts so the volume is visible.

## Three operating modes

**Test.** Meta's test phone number, up to five allowed recipient numbers, and the dashboard's temporary token, which expires after 24 hours. Good for a local round trip and nothing else. The test number is not an operational service and is not a permanent free-tier entitlement.

**Free-biased production.** A registered business number, `billing_policy = "free_guard"`, no templates, the 900-attempt local cap and the billable circuit breaker. This is the default and the shape most personal deployments want.

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
[site]
hostname = "assistant.example.com"

[whatsapp]
enabled = true
waba_id = "100000000000001"
phone_number_id = "100000000000002"
business_phone_number = "+15551234567"
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

`waba_id` and `phone_number_id` are the decimal ids from the app's WhatsApp product page, not phone numbers. An event carrying any other WABA or phone number id is refused. `graph_api_version` empty follows the version the installed PyWa release pins; set a `v25.0`-style value only for a controlled migration. `business_timezone` is the IANA zone the monthly cap's calendar month is computed in.

`enabled = true` with a malformed id, number, timezone, policy or limit fails the config load. The three credentials do not: they are reported by `istota doctor` and refused at send time, because the Ansible role renders them empty on purpose and delivers them through `secrets.env`.

The full setting table is in the [configuration reference](../configuration/reference.md#whatsapp).

## Identity and enrollment

The durable identity is the sender's business-scoped user id (BSUID), not their phone number. A WhatsApp user with a username may have no phone number in the payload at all, and a recycled number must never take over an existing Istota principal.

Enrollment is an operator action. Assign a bootstrap number, or a BSUID, or both:

```bash
istota user ensure --name alice --whatsapp-number +15551234567
istota user ensure --name alice --whatsapp-bsuid US.1234567890
istota user ensure --name alice --whatsapp-number +15551234567 --whatsapp-bsuid US.1234567890
istota user ensure --name alice --reset-whatsapp-identity
istota user ensure --name alice --clear-whatsapp
```

The bootstrap number is used for the first binding and as an outbound fallback. On the user's first authenticated message Istota latches their BSUID onto the binding, and from then on the BSUID is what identifies them. A user with a username and no reachable number cannot bootstrap by phone, so give them a BSUID explicitly; you can read one off the log of a rejected inbound event.

That binding is a credential. Whoever holds it can create tasks, run commands and answer that user's pending WhatsApp confirmations as them. Meta's signature proves which account sent the webhook, never that the same person still holds the line. Change or clear the binding when a number is lost, transferred or recycled.

Changing the bootstrap number discards everything learned about the previous holder, including the 24-hour service window, so Istota cannot free-form message a stranger who never wrote in. `--reset-whatsapp-identity` clears the learned identity and keeps the number; `--clear-whatsapp` removes the binding. If a bootstrap number matches a user whose stored BSUID is a different one, Istota refuses the message, creates no task and raises one operator alert off WhatsApp.

`istota user ensure` masks what it prints. `istota user show` returns the complete values, and is the private operator surface for that.

## Routing

Use a bare `whatsapp` destination. `whatsapp:+15551234567` is refused, so routing cannot become an arbitrary-contact send API; a bare `whatsapp` always resolves through the addressed user's current binding, read immediately before the send.

`all` and `both` are unchanged. Enabling the transport adds no metered delivery to a route somebody set up before it existed. `talk,whatsapp` and `all,whatsapp` work.

## The 24-hour service window

Meta allows a free-form service message only within 24 hours of the user's latest message. Istota keeps a five-minute safety margin, so its local window is open while `now` is before `last_user_message_at + 23h55m`. Meta stays authoritative and can still refuse a send the local state allowed.

Outside the window there are two outcomes. In `free_guard` the send records `window_closed`, makes no API call and raises an operator alert off WhatsApp; the completed task is still readable through the task tools. In `allow_paid` with a template configured, one approved utility template carries the result in its body parameter. Istota never sends a generic wake-up template that leaves the answer stranded until the user replies, never infers a category, never substitutes a language, and never creates or submits a template.

A confirmation question is an interactive message, and Meta caps an interactive body at 1,024 characters rather than the 4,096 a plain text message allows. Istota sizes the question to whichever limit applies. Behind a template, where quick-reply buttons are not available, the question is still answerable: a typed `YES` or `NO` resolves it, and so does `!confirm <id> yes|no`.

## Delivery, and what "sent" means

Istota sends at most one Meta message per logical response, rendered to 4,096 characters with a truncation note rather than split into several separately billed messages. A row is written before the network call, so a timeout can never become a second send.

The API response means Meta accepted the request, not that a handset received it. Task and operator views distinguish accepted, sent, delivered, read, failed, window closed, budget exhausted, billing blocked, opted out, unconfigured and delivery unknown. A definite rejection is `failed`. A timeout or connection loss after Meta may already have accepted the request is `unknown`, and Istota never retries it: a duplicate private answer is worse than a visible unknown state. A failure alert goes out with WhatsApp removed from its route, so a broken surface cannot report itself through itself.

STOP opts the binding out after one acknowledgement, START re-enables it, and HELP sends one fixed explanation.

## Operations

```bash
istota doctor --only whatsapp.          # local readiness and the billing state
istota whatsapp billing-status          # read the circuit breaker without changing it
istota whatsapp billing-unblock         # clear it, after checking Meta billing
```

`whatsapp.common` reports which fields are missing or invalid, never their values. `whatsapp.billing` reports the circuit breaker, the service attempts used against the cap, and the template attempts, which nothing local bounds.

The circuit breaker is persistent and deployment-wide. In `free_guard`, the first authenticated delivery status carrying `billable = true` trips it and every later send is refused. The message that revealed the charge may already have been billed; what the breaker buys is that the next one is not. Check Meta billing first, then `billing-unblock`. Switching to `allow_paid` also clears it.

Before relying on the surface, send one real message in each direction and confirm the delivery callbacks reach the state you expect. The automated tests drive a fake at the Cloud API boundary; they cannot prove token validity, template approval, number quality or what Meta bills.

## Docker

Copy `docker/.env.example`, fill in the `ISTOTA_WHATSAPP_*` values, and add `whatsapp` to `COMPOSE_PROFILES`. The `webhooks` service belongs to the `location`, `sms` and `whatsapp` profiles, so enabling several still starts one receiver. Nginx exposes `/webhooks/`; the receiver port stays inside the Compose network.

```dotenv
COMPOSE_PROFILES=whatsapp
ISTOTA_WHATSAPP_ENABLED=true
ISTOTA_WHATSAPP_WABA_ID=100000000000001
ISTOTA_WHATSAPP_PHONE_NUMBER_ID=100000000000002
ISTOTA_WHATSAPP_BUSINESS_PHONE_NUMBER=+15551234567
ISTOTA_WHATSAPP_ACCESS_TOKEN=...
ISTOTA_WHATSAPP_APP_SECRET=...
ISTOTA_WHATSAPP_VERIFY_TOKEN=...
```

Restart `istota`, `webhooks` and `nginx` after changing these. The main service renders `config.toml`; the webhook service waits for that render before it accepts a request.

## Ansible

Set the matching `istota_whatsapp_*` role variables and vault the three credentials. They are written to the root-owned `secrets.env` and loaded by the scheduler, web app and webhook receiver; they never reach `config.toml`. The role starts the receiver and exposes `/webhooks/` when location is on, SMS is on, or WhatsApp is on.

```yaml
istota_whatsapp_enabled: true
istota_whatsapp_waba_id: "100000000000001"
istota_whatsapp_phone_number_id: "100000000000002"
istota_whatsapp_business_phone_number: "+15551234567"
istota_whatsapp_business_timezone: "America/Los_Angeles"
istota_whatsapp_access_token: "{{ vault_whatsapp_access_token }}"
istota_whatsapp_app_secret: "{{ vault_whatsapp_app_secret }}"
istota_whatsapp_verify_token: "{{ vault_whatsapp_verify_token }}"
```

Bind a user inside `istota_users`:

```yaml
istota_users:
  alice:
    whatsapp_number: "+15551234567"
```

Read the warning under "Identity and enrollment" before putting one there: inventory is version control, and this value carries authority. Omit the key to leave a CLI-set binding alone; `""` clears the binding.

## Reverse proxy

Two things the application cannot enforce for itself, so both belong in front of it. The shipped Docker and Ansible nginx configurations do both; a hand-rolled front end has to.

The verify token arrives as a **query value** on the GET handshake, and nginx's default log format writes the whole request line. Turn the access log off for `/webhooks/whatsapp`, or use a format that logs `$uri` rather than `$request`. Istota logs the handshake itself under a stable event name and without the token.

The POST body limit has to sit above Istota's own 256 KiB cap so the application answers an oversized body with its own 413 rather than nginx answering with an HTML page Meta will retry against. The shipped configurations use `client_max_body_size 512k` on that one path. The raw body must reach the application unmodified: `X-Hub-Signature-256` is computed over the exact bytes, so anything that rewrites, re-encodes or buffers-and-reserializes the body breaks every request.

## Switching away

Set `enabled = false`. Unlike SMS there is no complete-but-inactive shape that keeps delivery callbacks authenticating: WhatsApp has one account rather than two adapters, and the routes refuse every request while the block is off. Outstanding rows keep their last state. User bindings, opt-outs, ledger rows and conversation history all survive, so turning it back on resumes where it stopped.
