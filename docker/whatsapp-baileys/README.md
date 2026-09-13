# The WhatsApp Baileys sidecar

A small Node program holding one WhatsApp Web session, reached by the daemon
over a Unix socket. It exists because Baileys is TypeScript with no Python
port, which is the only reason this is a separate process at all.

`index.js` is the program, `package.json` pins the library. The wire format is
`src/istota/transport/whatsapp/baileys_protocol.py`; the daemon side of the
link is `baileys_bridge.py`.

## Running it

Two environment variables, and nothing else:

- `ISTOTA_BAILEYS_SOCKET` — the socket the daemon is listening on.
- `ISTOTA_BAILEYS_SESSION_DIR` — the 0700 directory holding the paired
  credential. The sidecar's own log is `sidecar.log` inside it.

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

## Why this is its own image rather than a stage in istota's

Decided on size. `@whiskeysockets/baileys` pulls in libsignal, protobufjs and
a Node runtime — a few hundred megabytes of `node_modules` plus the runtime
itself — and `[whatsapp] enabled` is **false** by default, so folding it into
the main image charges that to every deployment for a surface most of them do
not run. It is also the only component here that is optional *and* large;
the devbox and browser images are already separate for the same reason.

The separation costs nothing at run time: the two processes share a Unix
socket and a directory, and the daemon's `sidecar_argv=()` shape is built for
exactly this — the daemon listens, something else runs the sidecar. The image
itself, the compose service and the systemd unit are the deployment stage's.

## What is not tested here

A real Baileys connection needs a real WhatsApp account and a phone to scan
with, so it is in no automated tier and cannot be. What the default suite
holds is the wire constants, pinned against the Python module in
`tests/test_whatsapp_sidecar_vendoring.py` — the two ends disagreeing about
the protocol is the failure with no error message. The connection is exercised
by hand at deployment; `docs/features/whatsapp.md` is where that is written
down.
