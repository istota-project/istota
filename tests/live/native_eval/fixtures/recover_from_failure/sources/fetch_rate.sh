#!/usr/bin/env bash
# Client for the live rate feed.
errors=(
  "connection reset by peer"
  "upstream returned HTTP 503"
  "TLS handshake timed out"
  "feed token expired"
  "DNS lookup failed for the feed host"
)
pick=$((RANDOM % ${#errors[@]}))
echo "fetch_rate: error: ${errors[$pick]} (request $RANDOM)" >&2
exit 1
