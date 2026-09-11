# SMS

SMS is one provider-neutral, user-routable push surface with Twilio and Telnyx adapters. It is never a room member or view, writes no `messages` row, and uses the stable `sms-<user hash>` conversation token plus task history for context. A bare `sms` destination resolves the user's current binding immediately before send. A descriptor carrying a phone number is never sent to — the binding wins and the number is ignored with a warning — so the route grammar cannot become a way to send to an arbitrary number. A destination whose user has no binding is kept rather than dropped, and records `unconfigured` with a task alert: dropping it empties the plan, which discards the answer and makes an SMS-origin confirmation complete instead of parking.

The phone binding is an identity credential. Exact E.164 lookup decides which user an authenticated inbound event may act as. It can create tasks, dispatch commands, and answer that user's pending SMS confirmations. Unknown senders create no task and are not retained. Changing or clearing the binding revokes delivery to the old number.

Provider adapters own signature verification, provider payloads, account or profile checks, acknowledgements, send calls, exception classification, and status mapping. Common code sees only `SmsProviderEvent`, `SmsSendRequest`, `SmsSendResult`, and public error codes. A complete inactive adapter may authenticate late delivery callbacks, but its inbound messages create no task.

Outbound work is ledger-backed and one-send. The caller supplies a stable logical id, the row claims one provider before the network call, and a timeout or other ambiguous outcome becomes `unknown` with no retry or provider failover. The renderer sends one provider message within the configured GSM-7 or UCS-2 segment budget. Provider acceptance is not handset delivery.

STOP and START state is stored by phone number in `sms_opt_outs`, outside the provider namespace. Switching providers preserves user bindings, routes, conversation history, opt-outs, and common ledger rows. Keep the old complete provider block until its outstanding callbacks settle if later delivery state matters.

The webhook receiver mounts `/webhooks/sms/twilio` and `/webhooks/sms/telnyx`. Docker and Ansible run one receiver for location, active SMS, or complete callback-only provider configuration. The combined `istota serve` process includes only the configured location and SMS routers on its web port. Provider setup and operating steps are in `docs/features/sms.md`.
