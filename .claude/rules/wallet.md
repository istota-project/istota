# Wallet: cards and purchase authorization

`wallet/` owns manual cards, policy decisions and purchase records in the framework DB. It is an experimental settings feature, not a module, and has no module database. `wallet/cards.py`, `policy.py`, `purchases.py`, `hosts.py` and `money.py` separate storage, admission, lifecycle, host normalization and amount units. User-facing documentation is `docs/features/wallet.md`.

## Storage and reach

Card number and CVC use the existing encrypted secrets store under `service='wallet'`, keys `card:<id>:number` and `card:<id>:cvc`. They are absent from `CONNECTED_SERVICE_SCHEMA`, `vault_entries`, credential snapshots and public reveal requests. Keep card POST bodies hand-parsed and bounded: automatic validation responses can echo secret input. Errors never include submitted values. Metadata includes cardholder name and billing address and is visible to the owner's tasks.

`vault_isolation_refusal` gates writes, requests and fills. Withheld room scopes refuse Wallet; a member's own shared-room turn always holds for private approval. The GET settings route remains readable with the feature off or isolation refused; mutations return 404 or 403 respectively. Card removal deletes secrets and cancels open purchases through the same lifecycle helpers that close held approvals. Purchases outlive task retention and never write to the Money ledger.

## Policy and approval

`purchases.request` takes `BEGIN IMMEDIATE` before policy and budget reads. The order is unavailable, card validity, per-task request cap, same-currency ceiling, automatic eligibility, then held approval. Refusals count toward the request cap. The auto budget counts the last thirty days of `authorized`, `filled`, `completed` and `unreported` auto purchases. Mixed-currency history holds a request; never sum those units as a basis for automatic authorization or invent an exchange rate. The scheduled ancestry rule is the broker's `_task_context`, and shared history uses the canonical room's `room_was_ever_shared`.

A held purchase uses the existing `whatsapp_skill_requests` purchase kind, `_store_request`, digest confirmation, private preview routing and task park. There is no clean-turn bypass. Preview scalars are collapsed; the description is fenced as untrusted. The stored destination snapshots `authorization_minutes` and `fills_per_purchase`. Approval uses that window, and fill claims use the lower of the snapshot and the current cap. Describe the configured number of fills; do not call a multi-fill authorization single-use. Decline, expiry, cancellation and card removal close the corresponding held request. The automatic notice is a `task_alert`: private details in the bell, fixed `WALLET_AUTO_PUSH` text through `pushing_only`, delivery after commit.

`money.py` owns decimal places for integer minor units despite the historical `_cents` column names. `GET /settings/wallet` supplies `currency_precision = {default: 2, exceptions: CURRENCY_EXPONENTS}`. The web page passes explicit fraction digits to formatting and uses that map for parsing; local `Intl` defaults must never reinterpret stored amounts. The exception map was seeded from runtime currency data, including legacy codes; it is an application contract, not universal ISO validation. No conversion is performed.

## Private fill and browser boundary

The only release is `wallet_card` over the inherited private credential channel. The public skill-proxy socket rejects it before any DB lookup. `credential_shim.fetch_card` and `_credref`'s `CardSecret` carry boxed values resolved at parse time; there is no public fallback. A claim checks ownership, task, state, expiry, cap, card availability, feature, isolation and room reach inside the write transaction. It increments `fill_count` once per release, not once per field. Browser failures after release still consume it.

Browse reuses its credential action path. `--fill-card` splits at the first `=`; `FIELD=FRAME>>>SELECTOR` explicitly enters a hosted field's iframe. Preflight requires `per_user_profiles`, `credential_origin_check` and `card_fill`. The container checks the target frame's HTTPS origin, supports expiry selects, masks number/CVC inputs and stops the remaining interaction after any card refusal. Text is scrubbed before clipping, including render and browse responses. The mask changes display only; the DOM value must remain to submit. Page scripts can read it or remove the mask, so no claim of protection from a hostile merchant or arbitrary page rendering belongs in docs or UI.

Static-card policy bounds releases, not charges. A task's total comparison and complete/fail report are not issuer evidence. Saved merchant cards bypass Wallet altogether. Issuer minting, recurring merchant cards and verified charge webhooks remain unimplemented.

## Doctor and verification

`doctor.check_wallet` is registered as deployment-scoped `security.wallet`, outside config-load checks. It returns separate isolation, cards and browser results. It uses `_deployment_sandboxing`'s three-state answer so `probe=False` never spawns, and skips the health request in that mode. Unlike the vault check, isolation runs whenever Wallet is enabled even if no vault file exists. Its read-only card scan uses the store's cipher directly: `get_secret` would update access timestamps and log row identities. Report counts only, never card labels, user IDs, values or exception contents. `/health` is management metadata and needs no user browser or admission lock.

Tests cover the wallet stores, approvals, private proxy, skills, web routes and page; `tests/test_doctor.py::TestWallet` covers readiness. Browser validation used the built image and a local test checkout, including cross-origin fields and a negative mask-removal artifact. It did not exercise an external Stripe checkout. Migration upgrade and skill-proxy smoke runs are separate deployment checks; unit tests do not replace them.
