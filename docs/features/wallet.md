# Wallet and purchase approval

Wallet lets a task request permission to use one of your payment cards at a named merchant. Add cards and set your spending policy in Settings → Wallet. Wallet is available by default.

The limits control when Istota releases a card for filling a checkout form. They do not cap what a merchant charges a static card after receiving it. Use a card with an issuer-side spending limit. Purchases are the task's declared amounts and reported outcomes, not bank-confirmed charges. Issuer-created cards and charge webhooks are not implemented, and Wallet writes nothing to the Money ledger.

## Deployment settings

These optional settings control request and fill limits:

```toml
[security]
wallet_requests_per_task = 3
wallet_fills_per_purchase = 3
wallet_authorization_minutes = 30
```

The three security settings have the defaults shown. The authorization window is clamped to 5–240 minutes. A browser container with `card_fill` support is required; update the browser image with the daemon. Run `istota doctor --only security.wallet` on the deployment host to check readiness.

Multi-user deployments need effective sandboxing or the explicit `allow_unsandboxed_multi_user_vaults` opt-in. Without either, Wallet writes, requests and fills are refused. The opt-in accepts same-uid exposure: an unsandboxed task can access another user's card data. The doctor check warns about that exposure even when the opt-in allows Wallet to run.

## Cards

Add a label, card number, expiry, security code, cardholder name and optional billing address. Spaces and dashes in the number are accepted. The number and security code are encrypted in the daemon's secrets store and are never returned by Wallet's card list. Labels, last four digits, expiry, name and billing address are metadata visible to your tasks.

Edit details, pause or resume a card from its menu. To replace its number or security code, remove the card and add it again. Removing a card deletes its secrets and cancels its open purchases. Pausing blocks fills while the card is paused; resuming does not extend an existing authorization window. Expired cards cannot be used.

When deployment isolation requirements are not met, existing Wallet data remains readable at `/settings/wallet`; writes and task use are refused.

## Spending policy

Save policy changes with the settings header's Save button.

| Setting | Effect |
|---|---|
| Auto limit per purchase | The largest declared amount eligible for automatic approval. |
| 30-day auto budget | The rolling total allowed for automatically approved purchases. |
| Ceiling per purchase | Refuses a larger declared amount in the policy currency. Leave empty for no ceiling. |
| Currency | The currency in which the limits apply. Other currencies always need approval. |
| Scheduled tasks may spend automatically | Allows scheduled tasks and their descendants to qualify for automatic approval. Off by default. |

With no saved policy, every purchase waits for approval. An automatic purchase must satisfy both auto limits and every other gate. Its budget reservation counts while it is authorized, filled, completed or unreported. These are declared amounts, not observed charges. Declined, refused, failed, cancelled and expired purchases do not reserve the auto budget.

A member's request in a shared room always needs approval in their own private room. Guest turns and tasks nobody asked for in a shared room cannot use Wallet. Requests also wait for approval when scheduled spending is off, when an extra payment-frame host is outside the built-in processor list, or when the rolling auto history contains a different currency. Changing the policy currency does not convert old spending or reset the budget; mixed-currency history prevents automatic approval until it leaves the window.

Amounts use integer minor units with server-defined decimal places. The Wallet API publishes `currency_precision` so the page interprets those units exactly as the daemon and CLI do. This is the application's precision contract, not a claim that every accepted three-letter code has a universally correct ISO definition. Browser currency-formatting defaults do not choose the units, and there is no exchange-rate conversion.

## Approving a purchase

The task declares the merchant's exact HTTPS origin, full amount and currency, card, description and any payment-frame hosts. Automatic approval produces a notification; the push text contains no merchant or amount. A held purchase stops the task and shows a preview in your private room or the bell. The preview names the purchase, its card's last four digits, hosts, amount, authorization window and allowed fill count. Approve or decline through the usual confirmation controls.

Approval is bound to the exact stored preview. The window and fill count shown there are saved with the request. Raising the operator's fill setting later cannot expand that approval; lowering it can further restrict use. Each purchase belongs to one user and one task, and its authorization cannot be borrowed by another task. Repeating a request with the same request key and identical fields returns the original purchase; changed fields under that key are refused.

A fill claim is one private-channel release for a browse invocation, which can fill several card fields. The default allows three claims within thirty minutes. A failed browser preflight or an origin refusal can still consume a claim because the daemon has already released the values. Before submitting, the task must compare the checkout total and currency to the declared purchase and stop if either changed. That comparison is an instruction to the model, not an independent measurement of the merchant's eventual charge.

## Checkout and its limits

An authorized purchase fills through `browse interact --purchase ID --fill-card FIELD=SELECTOR`. Hosted fields use `FIELD=FRAME>>>SELECTOR`, where `FRAME` selects the iframe and `SELECTOR` selects the field inside it. Each field's owning frame must have a declared HTTPS origin. A refused card action stops the remaining actions in that interaction, including a later submit click.

Card number and CVC inputs get a display mask; the real values remain in the DOM so the form can submit. Returned browse text is scrubbed before it is clipped. These controls reduce accidental disclosure through normal browse results. They do not make a merchant page trustworthy: page scripts can read submitted card values, change the page, remove the display mask or draw values elsewhere. A card already saved at a merchant, or a checkout reached without a Wallet fill, is outside the Wallet authorization mechanism.

A successful submit should be followed by the task's `wallet complete` report, with an order reference when available. An uncertain result needs inspection, not another submit: a timeout can happen after the merchant accepted the order. `wallet fail` records a reported failure; neither that report nor cancelling a purchase refunds or reverses a merchant charge.

## Purchase history and diagnostics

Wallet shows the latest fifty purchases, their declared amounts, states and approval kinds. Task links appear when the task's room is known. Open purchases can be cancelled. Cancelling stops later fills; stopping a task alone leaves its authorization alive until expiry. Purchase records survive task retention.

An unused authorization becomes `expired` when its window ends. A filled purchase with no outcome report becomes `unreported` one hour after its window ends and still counts toward the auto budget. `completed` and `failed` describe what the task reported. Check your issuer or [Money transactions](money.md) for actual charges.

`security.wallet` checks readiness on every deployment. Its isolation result fails if a multi-user deployment is refused, and warns if isolation is unverified or the operator accepted the unsandboxed exposure. Its card result warns with a count of cards whose secrets are missing or cannot be decrypted, without card labels, user IDs or values. Restore the deployment's original secret key or re-add affected cards after a key change. If doctor cannot initialize decryption in its own process, it reports the cards as unchecked instead; a standalone CLI may not have the daemon's secret-key environment. Its browser result warns when the browser is disabled, unreachable or does not advertise `card_fill`; a non-probing check skips the HTTP request.
