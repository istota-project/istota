#!/bin/sh
# Create the testbed's two mailboxes, then serve.
#
# One file rather than two copies of the same shell, because the container is
# started two ways: `docker run` from `run_standalone`, for the wire suite, and
# `docker compose` from `mail.yml`, for a profile. A compose `entrypoint:` has
# to write `$$` for every `$`, so an inline copy there would differ from the
# `docker run` one character by character — which is exactly the shape that
# drifts. `tests/test_testbed_services.py` asserts the addresses and the
# password here still agree with `testbed/services/mail.py`.
#
# `set -e` and no `|| true`, against istota-redteam's copy. `/data` is a tmpfs,
# so the accounts never already exist and a failure here is a failure — let it
# pass and it surfaces one layer later as a login rejection, which reads as a
# wrong password rather than as a mailbox that was never made.
#
# Account creation runs against `accounts.conf`, not `maddy.conf`: maddy's
# `creds` and `imap-acct` subcommands initialize every endpoint in the config
# they are handed, and the submission block breaks them. Both files share
# `state_dir` and the same DB dsns, so the accounts created are the ones served.
set -e

ACCOUNTS='bot@bot.test catchall@ext.test'
PASSWORD='maddy-testbed'

for acct in $ACCOUNTS; do
  /bin/maddy -config /data/accounts.conf creds create -p "$PASSWORD" "$acct"
  /bin/maddy -config /data/accounts.conf imap-acct create "$acct"
done

exec /bin/maddy -config /data/maddy.conf run
