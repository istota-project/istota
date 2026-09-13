/*
 * The WhatsApp sidecar: Baileys on one side, istota's line protocol on the
 * other.
 *
 * The daemon listens and this dials, which is what lets the socket's mode be
 * the daemon's to set (0600, owned by it, no network peer) — the whole trust
 * story for a link that carries no HMAC. `ISTOTA_BAILEYS_SOCKET` says where,
 * `ISTOTA_BAILEYS_SESSION_DIR` says where the paired credential lives; both
 * come from the environment rather than argv so neither shows up in `ps`, and
 * the argv stays the operator's to spell.
 *
 * The wire format is `src/istota/transport/whatsapp/baileys_protocol.py`. This
 * is a reimplementation of it rather than a copy — the other end is Python, so
 * there is nothing to byte-compare — and the constants below are pinned
 * against that module by `tests/test_whatsapp_sidecar_vendoring.py`, which is
 * the arrangement `.claude/rules/devbox.md` already documents for
 * `istota_devbox_client.py`.
 *
 * Three rules this file lives under, each of them a property the Python side
 * depends on:
 *
 *   1. **Nothing is written to stdout or stderr.** The daemon spawns this with
 *      both on /dev/null, because Baileys' own logger is chatty about JIDs and
 *      message bodies and the daemon's stdout is the journal and the rotating
 *      log the admin Logs pane reads back. Diagnostics go to a file inside the
 *      0700 session directory, where the credential already is.
 *   2. **A line is a line.** `JSON.stringify` escapes an embedded newline, and
 *      every frame is capped at MAX_LINE_BYTES on this side as well as on the
 *      reader's — a cap only on the reader lets a writer build a frame it can
 *      never deliver.
 *   3. **A failure reason is a key, never prose.** Baileys' errors carry the
 *      destination JID and, on a Boom error, the whole request. The Python
 *      side maps `reason` through a fixed table, so anything sent must be one
 *      of that table's keys.
 *
 * A real connection needs a real WhatsApp account, so none of this is in the
 * default test suite and none of it can be: what is covered here is the wire
 * constants and the module's shape. The connection itself is exercised by
 * hand at deployment. The operator-facing writeup is
 * `docs/features/whatsapp.md`; `README.md` beside this file is the build and
 * run detail under it.
 */

'use strict';

const fs = require('fs');
const net = require('net');
const path = require('path');

// --- the wire format -------------------------------------------------------

const PROTOCOL_VERSION = 1;
const MAX_LINE_BYTES = 256 * 1024;

const MSG_HELLO = 'hello';
const MSG_READY = 'ready';
const MSG_QR = 'qr';
const MSG_INBOUND = 'inbound';
const MSG_RECEIPT = 'receipt';
const MSG_SEND_RESULT = 'send_result';
const MSG_FATAL = 'fatal';
const MSG_SEND = 'send';
const MSG_SHUTDOWN = 'shutdown';

// Every `reason` the daemon's fixed table knows. Anything else there renders
// as the generic sentence, which is a worse diagnostic rather than a leak —
// but sending a key outside this set is still a bug, so it is a set.
const SEND_REASONS = new Set([
  'not_connected',
  'logged_out',
  'not_on_whatsapp',
  'rejected',
  'timeout',
  'internal',
]);

// The permanent ones, which the bridge latches: a session in any of these
// states is gone until somebody re-pairs, so it stops respawning.
const FATAL_LOGGED_OUT = 'logged_out';
const FATAL_BAD_SESSION = 'bad_session';

const SOCKET_PATH = process.env.ISTOTA_BAILEYS_SOCKET || '';
const SESSION_DIR = process.env.ISTOTA_BAILEYS_SESSION_DIR || '';

// --- diagnostics -----------------------------------------------------------

const LOG_LEVELS = { error: 0, warn: 1, info: 2, debug: 3 };
const LOG_LEVEL = LOG_LEVELS[process.env.ISTOTA_BAILEYS_LOG_LEVEL] ?? LOG_LEVELS.info;
const LOG_PATH = SESSION_DIR ? path.join(SESSION_DIR, 'sidecar.log') : '';

/*
 * One line to a file inside the session directory, appended, never to stdio.
 *
 * The directory is 0700 and the daemon spawns this with umask 0o077, so the
 * log is as private as the credential beside it — which it has to be, because
 * a WhatsApp diagnostic is about somebody's conversation. What still must not
 * go in it is a message body or a QR: this takes a fixed message and a small
 * bag of labels, never an arbitrary object, so there is no shape that quietly
 * carries one.
 */
function log(level, message, labels) {
  if ((LOG_LEVELS[level] ?? 99) > LOG_LEVEL || !LOG_PATH) return;
  const parts = [new Date().toISOString(), level.toUpperCase(), message];
  for (const [key, value] of Object.entries(labels || {})) {
    parts.push(`${key}=${String(value).slice(0, 120)}`);
  }
  try {
    fs.appendFileSync(LOG_PATH, parts.join(' ') + '\n', { mode: 0o600 });
  } catch (err) {
    // Nowhere left to report it. Never to stdio.
  }
}

// --- framing ---------------------------------------------------------------

function encode(type, fields) {
  const line = JSON.stringify(Object.assign({ type }, fields || {}));
  const raw = Buffer.from(line + '\n', 'utf8');
  if (raw.length > MAX_LINE_BYTES) {
    throw new Error(`${type} message exceeds ${MAX_LINE_BYTES} bytes`);
  }
  return raw;
}

// --- the link --------------------------------------------------------------

class Link {
  constructor(socketPath) {
    this.socketPath = socketPath;
    this.socket = null;
    this.buffer = '';
    this.onMessage = () => {};
    this.onClose = () => {};
    // Called after every `hello`. The daemon clears `ready` whenever the link
    // drops, and only a `ready` frame sets it back — so a session that never
    // closed has to say so again, or the bridge reports `connected` and not
    // `ready` for ever and `istota whatsapp pair` can never finish.
    this.onReady = () => {};
  }

  connect() {
    const socket = net.createConnection(this.socketPath);
    this.socket = socket;
    socket.setEncoding('utf8');
    socket.on('connect', () => {
      log('info', 'connected to the daemon');
      this.send(MSG_HELLO, { protocol_version: PROTOCOL_VERSION });
      this.onReady();
    });
    socket.on('data', (chunk) => this.feed(chunk));
    socket.on('error', (err) => log('warn', 'socket error', { code: err.code }));
    socket.on('close', () => {
      this.socket = null;
      this.buffer = '';
      this.onClose();
    });
  }

  /*
   * Split on newlines and refuse an over-long line rather than buffering it.
   *
   * Unbounded, a peer that never sends a newline grows this string until the
   * process dies — and the process holds the account credential, so it dying
   * is an outage rather than a tidy failure. The daemon's reader applies the
   * same bound from the other side.
   */
  feed(chunk) {
    this.buffer += chunk;
    if (this.buffer.length > MAX_LINE_BYTES && !this.buffer.includes('\n')) {
      log('error', 'over-long line from the daemon; dropping the link');
      this.buffer = '';
      if (this.socket) this.socket.destroy();
      return;
    }
    let index = this.buffer.indexOf('\n');
    while (index >= 0) {
      const line = this.buffer.slice(0, index);
      this.buffer = this.buffer.slice(index + 1);
      if (line.trim()) this.dispatch(line);
      index = this.buffer.indexOf('\n');
    }
  }

  dispatch(line) {
    let parsed;
    try {
      parsed = JSON.parse(line);
    } catch (err) {
      log('warn', 'malformed line from the daemon');
      return;
    }
    if (!parsed || typeof parsed.type !== 'string' || !parsed.type) {
      log('warn', 'line from the daemon has no message type');
      return;
    }
    this.onMessage(parsed.type, parsed);
  }

  send(type, fields) {
    if (!this.socket || this.socket.destroyed) return false;
    let raw;
    try {
      raw = encode(type, fields);
    } catch (err) {
      // Never the payload, and never the error's own text: an encode failure
      // here is almost always an over-long message body.
      log('error', 'could not encode a frame', { frame: type });
      return false;
    }
    this.socket.write(raw);
    return true;
  }
}

// --- the WhatsApp session --------------------------------------------------

/*
 * `makeWASocket` and its neighbours are required lazily so this file can be
 * loaded — and its constants read — without the dependency installed. The
 * drift guard does exactly that, and a sidecar that cannot be loaded without
 * `node_modules` is a sidecar nothing in the default suite can check.
 */
function loadBaileys() {
  // eslint-disable-next-line global-require
  return require('@whiskeysockets/baileys');
}

const USER_JID_DOMAIN = '@s.whatsapp.net';
const GROUP_JID_DOMAIN = '@g.us';

function isGroupJid(jid) {
  return typeof jid === 'string' && jid.endsWith(GROUP_JID_DOMAIN);
}

// A chat this surface models at all. `status@broadcast` and `@newsletter`
// arrive through `messages.upsert` like any other message and on an active
// account they never stop, so forwarding them costs a queue slot, a thread
// and a write transaction each, every one of which then fails to resolve a
// sender. A group still crosses — the daemon refuses it before any identity
// lookup, and that refusal has to stay a path something drives.
function isForwardableJid(jid) {
  return typeof jid === 'string' &&
    (jid.endsWith(USER_JID_DOMAIN) || jid.endsWith(GROUP_JID_DOMAIN));
}

function messageText(message) {
  const content = message && message.message;
  if (!content) return null;
  if (typeof content.conversation === 'string') return content.conversation;
  if (content.extendedTextMessage && typeof content.extendedTextMessage.text === 'string') {
    return content.extendedTextMessage.text;
  }
  return null;
}

function quotedId(message) {
  const context =
    message &&
    message.message &&
    message.message.extendedTextMessage &&
    message.message.extendedTextMessage.contextInfo;
  return context && typeof context.stanzaId === 'string' ? context.stanzaId : null;
}

// How many consecutive failures to *construct* a session before calling the
// credential unusable. One is a transient fault — a half-written auth file
// mid-rotation, a DNS blip inside the library — and declaring that permanent
// refuses every send and pages the operator to re-pair a session that is fine.
const MAX_START_FAILURES = 5;
const START_RETRY_MS = 5000;

class Session {
  constructor(link) {
    this.link = link;
    this.sock = null;
    this.stopping = false;
    this.startFailures = 0;
    this.starting = false;
    // Whether WhatsApp is connected *now*, as distinct from whether the
    // daemon has been told. `ready` used to be sent on the `open` transition
    // alone, so a daemon restart — or any link blip — left the bridge
    // reporting `connected` and never `ready`, permanently, because no second
    // `open` fires for a session that never closed. `announceReady` is what
    // closes that, and it is why this flag exists rather than being derived
    // from `this.sock`, which is non-null for a socket that is reconnecting.
    this.open = false;
  }

  announceReady() {
    if (this.open) this.link.send(MSG_READY, {});
  }

  async start() {
    // Two `connection: close` events before the reconnect timer fires would
    // otherwise schedule two `start()`s, and two live sockets both write
    // `SESSION_DIR` through `creds.update` — the auth-state corruption the
    // pair command refuses a whole running daemon to avoid, reached from
    // inside one process. The `mine()` guard below handles a *late* event
    // from an orphan; this handles the overlap.
    if (this.starting || this.stopping) return;
    this.starting = true;
    try {
      await this.open_();
    } finally {
      this.starting = false;
    }
  }

  async open_() {
    const baileys = loadBaileys();
    const { state, saveCreds } = await baileys.useMultiFileAuthState(SESSION_DIR);
    const sock = baileys.makeWASocket({
      auth: state,
      // Off, and this is the point of rule 1 rather than a preference:
      // Baileys' default logger writes JIDs and message content to stdout.
      printQRInTerminal: false,
      logger: silentLogger(),
      // The daemon composes and bounds its own bodies; nothing here should
      // mark a conversation read on the account's behalf.
      markOnlineOnConnect: false,
    });
    this.sock = sock;

    // Every handler is bound to the socket that installed it and bails once
    // that is no longer the live one. A closed socket can still deliver a
    // queued event, and an orphan's `connection: close` scheduling a second
    // reconnect is how one process ends up holding two Baileys clients
    // against one auth state — the corruption `istota whatsapp pair` refuses
    // a whole running daemon to avoid.
    const mine = () => this.sock === sock;

    sock.ev.on('creds.update', saveCreds);
    sock.ev.on('connection.update', (update) => {
      if (mine()) this.onConnection(update, baileys);
    });
    sock.ev.on('messages.upsert', (event) => {
      if (mine()) this.onMessages(event);
    });
    sock.ev.on('messages.update', (updates) => {
      if (mine()) this.onReceipts(updates);
    });
  }

  onConnection(update, baileys) {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      // The pairing credential. It crosses the socket and is written nowhere
      // else — not to the log, not to stdout.
      this.link.send(MSG_QR, { qr });
    }
    if (connection === 'open') {
      log('info', 'the WhatsApp session is open');
      // A session that opened is evidence the credential is usable, so the
      // run of construction failures below starts again from zero.
      this.startFailures = 0;
      this.open = true;
      this.link.send(MSG_READY, {});
      return;
    }
    // **Below the guard, not above it.** `connection.update` is a partial:
    // it fires with no `connection` key at all for a QR rotation and for
    // `receivedPendingNotifications` after the session opens. Clearing the
    // flag there marks a live session closed, so the next daemon-link
    // reconnect finds it false, announces nothing, and the bridge reports
    // connected-and-never-ready — the state this flag exists to prevent,
    // reached by another route.
    if (connection !== 'close') return;
    this.open = false;

    const status =
      lastDisconnect &&
      lastDisconnect.error &&
      lastDisconnect.error.output &&
      lastDisconnect.error.output.statusCode;
    const loggedOut = status === baileys.DisconnectReason.loggedOut;
    log('warn', 'the WhatsApp session closed', { status: status ?? 'unknown' });
    if (loggedOut) {
      // Permanent: the credential on disk names a device WhatsApp has
      // unlinked, and reconnecting with it will be refused for ever. The
      // daemon latches this, refuses every send definitely and alerts.
      this.link.send(MSG_FATAL, { reason: FATAL_LOGGED_OUT, permanent: true });
      // **And then it exits.** Staying alive leaves a process holding the
      // session directory with a dead socket, reporting `connected` and not
      // `ready` — and after a re-pair it never re-runs `start()`, so it is
      // useless until somebody restarts it by hand. Exiting is also what the
      // bridge's own supervisor docstring assumes on the external-unit shape:
      // systemd restarts the unit, it reconnects, and its `ready` clears the
      // latch. The delay is for the frame to leave the socket first.
      this.stopping = true;
      setTimeout(() => process.exit(1), 500);
      return;
    }
    if (this.stopping) return;
    // Transient. Reconnecting is this process's job, not the daemon's: the
    // daemon's supervisor respawns a sidecar that *exits*, and exiting here
    // would throw away a live socket and a warm session for a blip.
    setTimeout(() => {
      this.start().catch((err) => this.reportStartFailure(err));
    }, 3000);
  }

  reportStartFailure(err) {
    if (this.stopping) return;
    // **A missing dependency tree is not an unlinked device.** `loadBaileys`
    // is lazy, so a checkout with node and no `npm ci` fails here — and
    // reporting that permanent pages every admin that "the device link ended"
    // on a deployment that has never paired. It exits instead, which the
    // daemon's supervisor reports as a sidecar that will not stay up.
    if (err && err.code === 'MODULE_NOT_FOUND') {
      log('error', 'the Baileys library is not installed', { kind: err.code });
      process.exit(3);
    }
    this.startFailures += 1;
    // One failure is a transient fault — a half-written auth file mid-
    // rotation, a blip inside the library — and calling that permanent
    // refuses every send and asks an operator to re-pair a session that is
    // fine. A *run* of them is the credential problem the state names.
    if (this.startFailures < MAX_START_FAILURES) {
      log('warn', 'the WhatsApp session could not be started; retrying', {
        kind: err && err.name, attempt: this.startFailures,
      });
      setTimeout(() => {
        this.start().catch((again) => this.reportStartFailure(again));
      }, START_RETRY_MS);
      return;
    }
    log('error', 'the WhatsApp session could not be started', {
      kind: err && err.name, attempts: this.startFailures,
    });
    this.link.send(MSG_FATAL, { reason: FATAL_BAD_SESSION, permanent: true });
  }

  onMessages(event) {
    if (!event || !Array.isArray(event.messages)) return;
    for (const message of event.messages) {
      // `fromMe` is our own send echoed back. Ingesting it would put the
      // bot's own answer into the user's task history as their next request.
      if (!message || !message.key || message.key.fromMe) continue;
      const jid = message.key.remoteJid;
      if (!isForwardableJid(jid)) continue;
      const group = isGroupJid(jid);
      const text = group ? null : messageText(message);
      // The `group` flag is read off the chat rather than inferred from the
      // JID's spelling on the daemon's side, which is why it is sent: the
      // daemon refuses a group message before any identity lookup.
      this.link.send(MSG_INBOUND, {
        message_id: message.key.id,
        jid,
        username: group ? null : message.pushName || null,
        message_type: text === null ? 'unsupported' : 'text',
        text,
        callback_data: null,
        reply_to_message_id: group ? null : quotedId(message),
        group,
        timestamp: Number(message.messageTimestamp) || Math.floor(Date.now() / 1000),
      });
    }
  }

  onReceipts(updates) {
    if (!Array.isArray(updates)) return;
    for (const item of updates) {
      // **The inverse of `onMessages`' filter, and it has to be here.**
      // `messages.update` fires in both directions — a message we marked
      // read, a revoked incoming one — and a receipt for somebody else's
      // message matches no ledger row, so the daemon *parks* it against the
      // next send that is mid-flight and prunes it later as foreign traffic.
      // An id collision with a real row would be worse than the noise.
      if (!item || !item.key || !item.key.fromMe) continue;
      const id = item.key.id;
      const status = item.update && item.update.status;
      if (typeof id !== 'string' || !id || status === undefined) continue;
      const mapped = receiptStatus(status);
      if (!mapped) continue;
      this.link.send(MSG_RECEIPT, {
        message_id: id,
        status: mapped,
        timestamp: Math.floor(Date.now() / 1000),
      });
    }
  }

  async send(payload) {
    const requestId = payload && payload.request_id;
    if (typeof requestId !== 'string' || !requestId) {
      log('warn', 'a send arrived with no request id');
      return;
    }
    // A `kind` this side cannot express is refused rather than downgraded: the
    // ledger records which rendering it claimed, and sending a service message
    // for a row that says `template` is a ledger that lies. Definite, because
    // nothing was attempted.
    if (payload.kind !== 'service') {
      this.answer(requestId, { ok: false, reason: 'rejected', definite: true });
      return;
    }
    if (!this.sock) {
      this.answer(requestId, { ok: false, reason: 'not_connected', definite: true });
      return;
    }
    // **`reply_to_message_id` is carried on the wire and not applied here.**
    // A quoted reply needs the whole original `WAMessage`, which this process
    // does not keep, and the obvious synthetic stub — a bare `{key: {id}}` —
    // is a shape the library was not given and may refuse. A refusal would
    // land in the catch below as an *ambiguous* failure, spending `unknown`
    // on a message that never left, for a cosmetic thread marker. Dropping
    // it costs the quote and nothing else.
    try {
      const sent = await this.sock.sendMessage(payload.to, { text: payload.text });
      const id = sent && sent.key && sent.key.id;
      if (typeof id !== 'string' || !id) {
        // Sent, and we cannot name what. Not definite — the message may be on
        // somebody's phone, and the daemon settles that as `unknown`, which is
        // the honest record.
        this.answer(requestId, { ok: false, reason: 'internal', definite: false });
        return;
      }
      this.answer(requestId, { ok: true, message_id: id });
    } catch (err) {
      // **The error's own text never crosses.** It carries the destination JID
      // and, on a Boom error, the whole request.
      log('warn', 'a send failed', { kind: err && err.name });
      this.answer(requestId, {
        ok: false,
        reason: sendFailureReason(err),
        // Ambiguous by default: the socket may have carried it before the
        // failure. Only a refusal this side made before trying is definite,
        // and those are the two branches above.
        definite: false,
      });
    }
  }

  answer(requestId, fields) {
    if (fields.reason && !SEND_REASONS.has(fields.reason)) {
      fields = Object.assign({}, fields, { reason: 'internal' });
    }
    this.link.send(MSG_SEND_RESULT, Object.assign({ request_id: requestId }, fields));
  }

  async stop() {
    this.stopping = true;
    if (!this.sock) return;
    try {
      // `logout()` is deliberately never called: it unlinks the device, which
      // destroys the paired credential and makes an ordinary restart a
      // re-pair. Closing the websocket leaves the session on disk intact.
      this.sock.end(undefined);
    } catch (err) {
      log('warn', 'closing the WhatsApp socket raised', { kind: err && err.name });
    }
  }
}

// `proto.WebMessageInfo.Status`, mapped onto the ledger's own vocabulary here
// rather than on the daemon's side, which should not learn a library's enum.
//
// **0 is ERROR and dropping it is a delivery that failed and was never
// reported.** `byNumber[0]` was absent, `undefined` fell through the caller's
// falsy guard, and the row stayed `accepted` — the exact class the parked
// status table was built for, with no alert behind it. The string branch has
// always mapped `error`; the numeric branch is the live one.
//
// 1 is PENDING, which is *before* the server acknowledged anything, so it maps
// to nothing: calling it `sent` advances the monotonic ladder ahead of the
// fact. 5 is PLAYED, which this surface does not model past `read`.
const RECEIPT_BY_NUMBER = {
  0: 'failed',
  2: 'sent',
  3: 'delivered',
  4: 'read',
  5: 'read',
};
const RECEIPT_BY_NAME = ['sent', 'delivered', 'read', 'failed'];

function receiptStatus(status) {
  // `|| null` would be wrong here even with 0 mapped, since the map's own
  // values are all truthy strings — but the explicit test is what says the
  // zero key is deliberate.
  if (typeof status === 'number') {
    return Object.prototype.hasOwnProperty.call(RECEIPT_BY_NUMBER, status)
      ? RECEIPT_BY_NUMBER[status]
      : null;
  }
  if (typeof status !== 'string') return null;
  const name = status.toLowerCase();
  if (name === 'error') return 'failed';
  return RECEIPT_BY_NAME.includes(name) ? name : null;
}

function sendFailureReason(err) {
  const status = err && err.output && err.output.statusCode;
  if (status === 401 || status === 403) return 'logged_out';
  if (status === 408) return 'timeout';
  if (err && err.message && /not.*on whatsapp/i.test(err.message)) return 'not_on_whatsapp';
  return 'internal';
}

function silentLogger() {
  // Pino's shape, answering nothing. Baileys calls `.child()` on it.
  const noop = () => {};
  const logger = {
    level: 'silent',
    fatal: noop, error: noop, warn: noop, info: noop, debug: noop, trace: noop,
  };
  logger.child = () => logger;
  return logger;
}

// --- entry point -----------------------------------------------------------

function main() {
  if (!SOCKET_PATH || !SESSION_DIR) {
    // No log destination either — the session directory is where the log
    // lives. Exiting non-zero is the only channel left, and the daemon's
    // supervisor reports it as a spawn that did not stay up.
    process.exit(2);
  }
  const link = new Link(SOCKET_PATH);
  const session = new Session(link);

  link.onMessage = (type, payload) => {
    if (type === MSG_SEND) {
      session.send(payload).catch((err) => {
        log('error', 'a send escaped', { kind: err && err.name });
      });
      return;
    }
    if (type === MSG_SHUTDOWN) {
      log('info', 'the daemon asked the sidecar to stop');
      session.stop().finally(() => process.exit(0));
      return;
    }
    log('warn', 'unexpected message from the daemon', { frame: type });
  };
  // The daemon's listener outlives any one sidecar, so a dropped link is a
  // reconnect rather than an exit — and reconnecting keeps the WhatsApp
  // session, which an exit would throw away along with its warm state.
  link.onReady = () => session.announceReady();
  link.onClose = () => {
    if (session.stopping) return;
    log('warn', 'the daemon link closed; reconnecting');
    setTimeout(() => link.connect(), 2000);
  };

  link.connect();
  session.start().catch((err) => session.reportStartFailure(err));

  for (const signal of ['SIGTERM', 'SIGINT']) {
    process.on(signal, () => {
      session.stop().finally(() => process.exit(0));
    });
  }
}

if (require.main === module) {
  main();
}

module.exports = {
  PROTOCOL_VERSION,
  MAX_LINE_BYTES,
  MSG_HELLO,
  MSG_READY,
  MSG_QR,
  MSG_INBOUND,
  MSG_RECEIPT,
  MSG_SEND_RESULT,
  MSG_FATAL,
  MSG_SEND,
  MSG_SHUTDOWN,
  SEND_REASONS,
  encode,
  receiptStatus,
  sendFailureReason,
};
