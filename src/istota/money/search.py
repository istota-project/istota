"""On-demand ledger search with the transactions page's filter semantics."""

import hashlib
import json
import time
from collections import Counter

from istota.lib.text_match import make_snippet, terms_to_plain
from istota.money._loader import UserNotFoundError, resolve_for_user
from istota.money.core.ledger import search_transactions
from istota.search.core import Provider, ProviderResult, SearchHit
from istota.search.links import route


def search(ctx, terms, mode, limit, offset):
    if time.monotonic() >= ctx.deadline:
        raise TimeoutError
    try:
        user = resolve_for_user(ctx.user_id, ctx.config)
    except (UserNotFoundError, FileNotFoundError):
        return ProviderResult([], False)
    remaining = ctx.deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    if not user.ledgers or not user.ledgers[0]["path"].is_file():
        return ProviderResult([], False)
    rows = search_transactions(
        user.ledgers[0]["path"], terms_to_plain(terms),
        limit=offset + limit + 1, timeout=remaining,
    )
    if time.monotonic() >= ctx.deadline:
        raise TimeoutError
    hits = []
    occurrences = Counter()
    for index, row in enumerate(rows[:offset + limit]):
        # Postings can share a transaction ID, including identical split rows.
        digest = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
        occurrences[digest] += 1
        if index < offset:
            continue
        payee, narration = row.get("payee") or "", row.get("narration") or ""
        snippet, highlights = make_snippet(f"{payee} {narration}", terms)
        date = row.get("date") or ""
        account = row.get("account") or ""
        hits.append(SearchHit(
            id=f"money:{digest}:{occurrences[digest]}", kind="transaction",
            title=" ".join((payee or narration).split()), subtitle=account,
            snippet=snippet, highlights=highlights, date=date or None,
            link=route("/money/transactions/", account=account, year=date[:4]),
            badges=[],
        ))
    return ProviderResult(hits, len(rows) > offset + limit)


PROVIDER = Provider("money", "Transactions", 80, "money", 8.0, True, search)
