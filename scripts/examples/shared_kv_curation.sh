#!/usr/bin/env bash
#
# Example: hand-rolled shared briefing content via the shared KV store.
#
# The briefings module ships batteries-included shared blocks (world-headlines,
# markets-summary) that it generates once globally — see
# docs/features/briefings.md "Module-owned shared blocks". This script is the
# ESCAPE HATCH for content the module doesn't ship a generator for: an admin
# fetches/builds content on a schedule and publishes it into the shared-block
# namespace, where any user's briefing reads it via a `shared_block` source — so
# the fetch and the synthesis happen ONCE, not once per user.
#
# Run this from an admin CRON `command:` job (the task's identity must be an
# admin — shared writes are admin-only, fail-closed on a blank admins file):
#
#   # CRON.md
#   [[jobs]]
#   name = "curate-tech-digest"
#   type = "command"
#   command = "bash /srv/app/istota/scripts/examples/shared_kv_curation.sh"
#   cron = "0 6 * * *"          # before the briefing window that reads it
#   silent_unless_action = true
#
# Then a briefing reads it with a `shared_block` source (the key is the block
# name; it also appears in the web editor's shared-block picker as "custom"):
#
#   [[users.alice.briefings.blocks]]
#   title = "📰 Tech digest"
#   render_mode = "structured"
#     [[users.alice.briefings.blocks.sources]]
#     kind = "shared_block"
#     config = { name = "tech-digest", max_age_hours = 24 }
#
set -euo pipefail

NAMESPACE="briefing_shared_blocks"
KEY="tech-digest"

# 1. Build your content however you like (curl an API, run a scraper, call a
#    summarizer, …). Produce ONE of the two shapes below.

# ---- Shape A: pre-rendered section text (share the synthesis too) ----
#   A `structured` consuming block splices this near-verbatim.
SECTION_TEXT="📰 Tech digest
- Example headline one — one-sentence summary. [source](https://example.com/a)
- Example headline two — one-sentence summary. [source](https://example.com/b)"

# jq -n keeps the JSON well-formed regardless of newlines/quotes in the content.
VALUE="$(jq -n --arg text "$SECTION_TEXT" '{text: $text}')"

# ---- Shape B: raw items (share only the fetch; each reader synthesizes) ----
#   Uncomment to publish items instead; a `synthesis` consuming block will
#   group and rewrite them per user.
#
# VALUE="$(jq -n '{items: [
#   {title: "Example headline one", summary: "…", url: "https://example.com/a"},
#   {title: "Example headline two", summary: "…", url: "https://example.com/b"}
# ]}')"

# 2. Publish to the shared store. --shared writes are admin-only; the deferred
#    op is authorized at apply time against this task's (admin) identity.
istota-skill kv set --shared "$NAMESPACE" "$KEY" "$VALUE"

echo "published shared KV ${NAMESPACE}/${KEY}"
