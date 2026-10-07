---
name: usage
description: Read token usage, costs and subscription list-price equivalents by model, origin, source or brain
cli: true
shared_room: private
---
# Usage

Read recorded usage directly when asked about token counts, spending, or what subscription usage would cost at API list prices. Every response is JSON; `--json` is accepted explicitly too.

```bash
istota-skill usage --days 30 --by model --json
istota-skill usage --since 2026-08-01 --until 2026-08-31 --by origin
istota-skill usage --days 7 --by source
istota-skill usage --days 30 --by brain
```

The default window is the last 30 days. Dates are UTC; `--until` includes the whole date. Choose `--days` or `--since`. Without `--by`, the response has one totals group.

Members see only their own usage. Admins see all usage by default and may use `--user alice` to narrow it or `--by user` to compare users. Both options are refused for non-admins. Usage is personal data and is unavailable to a task whose room withholds scopes.

Each group includes token totals, cache hit rate, context measurements and `cost_by_basis`. Interpret each cost basis separately:

- `api`: recorded API cost.
- `subscription`: list-price equivalent of the recorded usage, not an amount billed or the subscription fee. Use this for questions about what the same usage would cost on the API, and label it as a list-price equivalent.
- `estimated`: catalog estimate, not a recorded bill; an unknown model can have a zero estimate.

Always report `unmeasured_tasks` when it is nonzero. Those tasks recorded no usage, so the totals are incomplete. Rows can include retries and calls outside tasks, so a row count is not a task count. The response covers retained records only. Do not present subscription or estimated costs as actual spending or mix them into an API bill.
