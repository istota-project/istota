# The WhatsApp Baileys sidecar

A small Node program holding one WhatsApp Web session, reached by the daemon
over a Unix socket. It exists because Baileys is TypeScript with no Python
port, which is the only reason this is a separate process at all.

`index.js` is the program, `package.json` pins the library and
`package-lock.json` is what `npm ci` installs. `Dockerfile` builds the image the
`whatsapp-baileys` compose service runs. The wire format is
`src/istota/transport/whatsapp/baileys_protocol.py`; the daemon side of the link
is `baileys_bridge.py`.

## Running it

Two environment variables, and nothing else:

- `ISTOTA_BAILEYS_SOCKET` — the socket the daemon is listening on.
- `ISTOTA_BAILEYS_SESSION_DIR` — the 0700 directory holding the paired
  credential. The sidecar's own log is `sidecar.log` inside it, and
  `logout-backoff.json` beside it is how long a run of logged-out starts has
  been going — see below. `connection-replaced.json` records a run of
  connections WhatsApp closed because another client logged in with the same
  session (status 440): after five in ten minutes the sidecar stops
  reconnecting, retries after 15 minutes and then after an hour twice more,
  and then gives up until the session is re-paired.

Both are set by the daemon when it spawns the sidecar itself, and have to be
set by the unit or the compose service otherwise. `ISTOTA_BAILEYS_LOG_LEVEL`
(`error`, `warn`, `info`, `debug`) raises the log; it defaults to `info`.

```
npm ci
ISTOTA_BAILEYS_SOCKET=/srv/app/istota/data/whatsapp-baileys.sock \
ISTOTA_BAILEYS_SESSION_DIR=/srv/app/istota/data/whatsapp-baileys-session \
  node index.js
```

Pairing is `istota whatsapp pair`, which starts a sidecar of its own and
renders the QR. Do not run two sidecars against one session directory: two
Baileys clients on one auth state corrupt it.

## When WhatsApp unlinks the device

The credential on disk names a device that no longer exists, so every start
reconnects, is refused with a 401 and exits. Whatever is supervising the
program starts it again, and each of those cycles is a real authentication
attempt against an account WhatsApp has already unlinked once.

So the program waits before exiting, and the wait grows with the run: no wait
on the first, then 30 seconds, 5 minutes, 15, 30, an hour. The run is counted
in `logout-backoff.json` inside the session directory, because every cycle is
a different process and nothing in memory survives one. A session that opens
deletes the file.

When the run cannot be written down, or the previous one cannot be read back —
a read-only directory, a full disk, something standing at the file's path, a
file the sidecar cannot read — the count cannot advance. The wait is floored at
the five-minute rung instead of collapsing to the first one. It is a floor and
not a rung of its own, so a run already further up the ladder keeps its own
longer wait; what such a deployment does not do is climb, so it holds where it
is rather than reaching the hourly rung. The sidecar says so on the `fatal`
frame and `istota doctor` reports it beside the unlink, because the warning
about it is written into the directory that cannot be written.

The wait is here rather than in the unit's `RestartSec` or compose's restart
policy because neither can tell an unlinked device from a crash, and both have
to keep restarting promptly for the second. It also watches `creds.json` while
it waits: a re-pair that replaces the credential, or a session directory moved
aside, ends the wait immediately rather than holding a working session down
for the rest of an hour.

## Why this is its own image rather than a stage in istota's

Decided on size. `@whiskeysockets/baileys` pulls in libsignal, protobufjs and
a Node runtime — a few hundred megabytes of `node_modules` plus the runtime
itself — and `[whatsapp] enabled` is **false** by default, so folding it into
the main image charges that to every deployment for a surface most of them do
not run. It is also the only component here that is optional *and* large;
the devbox and browser images are already separate for the same reason.

The separation costs nothing at run time: the two processes share a Unix
socket and a directory, and the daemon's `sidecar_argv=()` shape is built for
exactly this — the daemon listens, something else runs the sidecar.

It costs one thing at setup time, on the compose shape only. `istota whatsapp
pair` spawns a sidecar of its own, and the istota image ships neither this
program nor its dependencies — so **pairing is not reachable from inside that
stack**. Pair from a checkout with node (the Ansible shape, or a developer
machine) and move the session directory into the `istota_data` volume at
`/data/db/whatsapp-baileys-session`, 0700 and owned by the uid the containers
run as. A listen-only mode for `pair`, which would let the compose sidecar
supply the QR to a daemon-side listener, is the real fix and is not built.

## What is not tested here

A real Baileys connection needs a real WhatsApp account and a phone to scan
with, so it is in no automated tier and cannot be. What the default suite
holds is the wire constants, pinned against the Python module in
`tests/test_whatsapp_sidecar_vendoring.py` — the two ends disagreeing about
the protocol is the failure with no error message.

The connection is exercised by hand at deployment. `docs/features/whatsapp.md`
is the operator-facing writeup, including the setup steps and the trade this
adapter carries. The short version, for somebody already in this directory:

1. Stop the istota scheduler, and any sidecar running as a unit of its own.
2. `npm ci` in this directory.
3. `istota whatsapp pair`, and scan the code from WhatsApp's Linked Devices
   screen. Pairing finds the program in a checkout by itself; it needs no
   configuration.
4. Arrange for something to run the sidecar. Either set `[whatsapp.baileys]
   sidecar_command` so the daemon spawns it, or start the unit or compose
   service that does. **The daemon spawns nothing by default** — with
   `sidecar_command` empty it only listens, which is what a deployment
   running its own sidecar wants and is why step 5 would otherwise report no
   sidecar connected.
5. Start the scheduler. `istota doctor --only whatsapp.` should report
   `whatsapp.baileys_session` ok; the bridge check answers only inside the
   daemon, so read that one from the admin Health pane or `!check`.
6. Message the number from a bound user's phone and confirm a reply arrives.
