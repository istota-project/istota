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
 * hand at deployment, which `docs/features/whatsapp.md` documents.
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
  }

  connect() {
    const socket = net.createConnection(this.socketPath);
    this.socket = socket;
    socket.setEncoding('utf8');
    socket.on('connect', () => {
      log('info', 'connected to the daemon');
      this.send(MSG_HELLO, { protocol_version: PROTOCOL_VERSION });
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

function isGroupJid(jid) {
  return typeof jid === 'string' && jid.endsWith('@g.us');
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

class Session {
  constructor(link) {
    this.link = link;
    this.sock = null;
    this.stopping = false;
  }

  async start() {
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

    sock.ev.on('creds.update', saveCreds);
    sock.ev.on('connection.update', (update) => this.onConnection(update, baileys));
    sock.ev.on('messages.upsert', (event) => this.onMessages(event));
    sock.ev.on('messages.update', (updates) => this.onReceipts(updates));
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
      this.link.send(MSG_READY, {});
      return;
    }
    if (connection !== 'close') return;

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
    // A session that cannot be constructed at all is a credential problem
    // rather than a network one: a corrupt or half-written auth state.
    log('error', 'the WhatsApp session could not be started', {
      kind: err && err.name,
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
      if (typeof jid !== 'string' || !jid) continue;
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
      const id = item && item.key && item.key.id;
      const status = item && item.update && item.update.status;
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
    const options = {};
    if (typeof payload.reply_to_message_id === 'string' && payload.reply_to_message_id) {
      // Best effort: a quoted reply needs the original message, which this
      // process may no longer hold. The id alone is what Baileys accepts.
      options.quoted = { key: { id: payload.reply_to_message_id, remoteJid: payload.to } };
    }
    try {
      const sent = await this.sock.sendMessage(payload.to, { text: payload.text }, options);
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

function receiptStatus(status) {
  // Baileys reports a numeric enum and, on some paths, its name. Both are
  // mapped here rather than on the daemon's side, whose table is the ledger's
  // own vocabulary and should not learn a library's enum.
  const byNumber = { 1: 'sent', 2: 'sent', 3: 'delivered', 4: 'read', 5: 'read' };
  if (typeof status === 'number') return byNumber[status] || null;
  if (typeof status !== 'string') return null;
  const name = status.toLowerCase();
  if (name === 'error') return 'failed';
  return ['sent', 'delivered', 'read', 'failed'].includes(name) ? name : null;
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
