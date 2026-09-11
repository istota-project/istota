# SMS

Istota can receive requests and send final replies through Twilio or Telnyx. SMS is one provider-neutral surface: user phone assignments, task history, routing, opt-out state, and delivery records do not change when the provider changes.

An SMS exchange is separate from the room model. It does not create or join a Talk or web room, and it does not copy messages into a room transcript. The task and its result remain available in the task history.

## Identity and consent

Assigning a phone number gives that number authority to act as the user. It can create tasks, run commands, and answer that user's pending SMS confirmation questions. A bare `yes` or `no` answers only a question parked by an SMS task — it will not resolve one waiting in Talk, web chat, or the inbound email gate, because the number is the weakest credential any surface authenticates with. Use `!confirm <id> yes|no` to answer another surface's question deliberately. Provider signatures prove which provider sent a webhook, but they do not prove that the same person still controls the SIM. Remove or change the assignment when a number is lost, transferred, or recycled.

Phone numbers must use exact E.164 form and must be unique across users. Assign or clear one with the operator CLI:

```bash
istota user ensure --name alice --sms-number +15551234567
istota user ensure --name alice --clear-sms-number
```

Istota records STOP and START state by phone number, outside either provider's account. A provider switch therefore does not restore delivery to a number that opted out. HELP, STOP, and START are handled by the provider and recorded locally; Istota sends no second compliance response.

## Common configuration

Set the public hostname and common SMS block, then configure at least the active provider:

```toml
[site]
hostname = "assistant.example.com"

[sms]
enabled = true
provider = "twilio"                  # twilio | telnyx
service_numbers = ["+15551230000"]
default_sender_number = "+15551230000"
max_segments = 6
request_timeout_seconds = 10
```

`service_numbers` contains the numbers that may receive inbound messages. `default_sender_number` must be one of them. A direct reply prefers the number that received the request; other sends use the default. `max_segments` accepts 1 through 10. `request_timeout_seconds` accepts 1 through 30.

The sender's number must be assigned to a user before Istota accepts work from it. Unknown senders receive the provider's normal success acknowledgement, but create no task and leave no phone number or raw payload in the SMS tables.

Use a bare `sms` routing destination. `sms:+15551234567` is rejected because routing must not become an arbitrary-contact send API. Existing `all` and `both` destinations do not include SMS, so enabling the transport does not add paid delivery to old routes.

## Twilio setup

Create a Messaging Service, add every configured service number to its sender pool, and enable Advanced Opt-Out. Configure neutral provider-managed responses for START, STOP, and HELP because Istota deliberately sends no second compliance response. Configure both incoming messages and status callbacks to:

```text
https://assistant.example.com/webhooks/sms/twilio
```

Configure the provider block:

```toml
[sms.twilio]
account_sid = "AC..."
auth_token = "..."
api_key_sid = "SK..."
api_key_secret = "..."
messaging_service_sid = "MG..."
```

The Account SID and Auth Token authenticate inbound webhooks. Outbound sends use the restricted API key SID and secret. Istota does not fall back to the account Auth Token for sends. The Messaging Service controls the permitted sender pool.

Twilio signs the exact public URL and every form field. Do not point the provider at an internal container address or rewrite the path before it reaches Istota. If connection overrides are used, restrict retries to connection failures, timeouts, and server errors. An automatic retry after a normal client error is not useful.

See Twilio's [incoming message webhook guide](https://www.twilio.com/docs/messaging/guides/webhook-request), [webhook security guide](https://www.twilio.com/docs/usage/webhooks/webhooks-security), and [connection override guide](https://www.twilio.com/docs/usage/webhooks/webhooks-connection-overrides).

## Telnyx setup

Create a Messaging Profile, assign every configured service number, configure its opt-in and opt-out responses, and set its webhook URL to:

```text
https://assistant.example.com/webhooks/sms/telnyx
```

Configure the provider block:

```toml
[sms.telnyx]
api_key = "..."
public_key = "..."
messaging_profile_id = "..."
```

The public signing key authenticates the exact raw webhook body and timestamp. The API key authenticates outbound sends. The Messaging Profile identifies accepted inbound events and outbound policy. Keep separate profiles for separate programs because provider-managed opt-out blocking is profile-scoped.

Telnyx expects the webhook acknowledgement within two seconds. Istota verifies, records, and queues the event before acknowledging it; no model or provider API call runs on that request path. A failover URL may point to the same fixed endpoint on another instance only when both instances share the same database and task-claim guarantees.

See Telnyx's [inbound messaging guide](https://developers.telnyx.com/docs/messaging/messages/receive-message), [webhook event guide](https://developers.telnyx.com/docs/messaging/messages/receiving-webhooks), and [send guide](https://developers.telnyx.com/docs/messaging/messages/send-message).

## Docker

Copy `docker/.env.example`, set the common and provider-qualified `ISTOTA_SMS_*` values, and add `sms` to `COMPOSE_PROFILES`. The `webhooks` service belongs to both the `location` and `sms` profiles, so enabling both still starts one process. Nginx exposes `/webhooks/`; the receiver port is internal to the Compose network.

```dotenv
COMPOSE_PROFILES=sms
ISTOTA_SMS_ENABLED=true
ISTOTA_LOCATION_ENABLED=false
ISTOTA_SMS_PROVIDER=twilio
ISTOTA_SMS_SERVICE_NUMBERS=+15551230000
ISTOTA_SMS_DEFAULT_SENDER_NUMBER=+15551230000
```

Restart `istota`, `webhooks`, and `nginx` after changing these values. The main service renders `config.toml`; the webhook service waits for that render before accepting requests.

## Ansible

Set the matching `istota_sms_*` role variables. Provider fields are written to the root-readable `secrets.env` file by default and are loaded by the scheduler, web app, and webhook receiver. The role starts the receiver and exposes `/webhooks/` when location is on, SMS is on, or a complete inactive provider block remains for callbacks.

```yaml
istota_sms_enabled: true
istota_sms_provider: telnyx
istota_sms_service_numbers:
  - "+15551230000"
istota_sms_default_sender_number: "+15551230000"
istota_sms_telnyx_api_key: "{{ vault_sms_telnyx_api_key }}"
istota_sms_telnyx_public_key: "{{ vault_sms_telnyx_public_key }}"
istota_sms_telnyx_messaging_profile_id: "{{ vault_sms_telnyx_profile_id }}"
```

Vault provider values in inventory. Do not put them in a public playbook or commit them to the repository.

## Switching providers

Configure the new provider account and its full provider block first. Point its inbound and callback settings at the fixed endpoint, move the service numbers, then change `sms.provider`. User phone assignments, `sms` routes, opt-out rows, and the SMS conversation token remain the same.

Keep the old provider block until its outstanding messages reach a terminal state, or accept that later callbacks will be refused and those rows will retain their last state. A complete inactive adapter accepts signed delivery callbacks but does not accept new inbound tasks. Istota never resends an old logical message through the new provider.

## Delivery and cost states

Istota sends at most one provider message per logical response. It counts GSM-7 septets or UTF-16 code units, truncates within `max_segments`, and keeps links and ordinary line breaks. Carrier registration, country rules, and provider account approval remain operator responsibilities; `istota doctor` checks local configuration, not carrier reachability.

The provider's API response means accepted, not delivered. Task and operator views distinguish accepted, queued, sent, delivered, delivery unconfirmed, failed, blocked by opt-out, unconfigured, and delivery unknown. A definite rejection is failed. A timeout or connection loss after the provider may have accepted the request is unknown, and Istota does not retry or fail over that send because either action could create a duplicate.

Run `istota doctor --only sms.` after setup. Before relying on the transport, send and receive one real message through each configured provider and confirm that delivery callbacks reach the expected final state. Automated tests use provider SDK fixtures and cannot prove carrier delivery or portal configuration.
