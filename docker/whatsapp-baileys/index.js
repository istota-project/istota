/*
 * The WhatsApp sidecar: Baileys on one side, istota's line protocol on the
 * other.
 *
 * The daemon listens and this dials, which is what lets the socket's mode be
 * the daemon's to set (0600, owned by it, no network peer) — the whole trust
 * story for a link that carries no HMAC. `ISTOTA_BAILEYS_SOCKET` says where,
 * `ISTOTA_BAILEYS_SESSION_DIR` says where the paired credential lives; both
 * come from the environment rather than argv so neither shows up in `ps`, and
 * the argv stays the operator's to spell. Those two are required and the
 * program exits 2 without either. `ISTOTA_BAILEYS_MEDIA_DIR` is a third and
 * is *not* required: it is a fixed name beside the session directory, so
 * `deriveMediaDir` computes it when nobody said, and the variable is an
 * override. See that function for why the exception is only for this one.
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
 *   4. **Nothing this process creates is wider than 0600.** The session files
 *      are a full-account WhatsApp credential and Baileys writes them, so the
 *      only thing that decides their mode at birth is this process's umask.
 *      `applyPrivateUmask` is what sets it; see that function for why it is
 *      set here rather than by whatever started the program.
 *
 * A real connection needs a real WhatsApp account, so none of this is in the
 * default test suite and none of it can be: what is covered here is the wire
 * constants and the module's shape. The connection itself is exercised by
 * hand at deployment. The operator-facing writeup is
 * `docs/features/whatsapp.md`; `README.md` beside this file is the build and
 * run detail under it.
 */

'use strict';

const crypto = require('crypto');
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
// A `creds.json` that exists and cannot be read, with no usable backup
// (ISSUE-552). The session was paired and is lost; starting the library on it
// would begin a fresh pairing instead and say nothing.
const FATAL_CREDENTIAL_UNREADABLE = 'credential_unreadable';

// --- inbound media ---------------------------------------------------------

/*
 * What one inbound file may weigh, and **the fetcher owns this bound** — the
 * daemon only ever sees a file that already exists, so on this path the cap
 * has to be enforced here or nowhere. `collectMediaChunk` is where it lives,
 * so the download aborts partway rather than measuring a buffer that is
 * already in memory.
 *
 * It is `media.MAX_MEDIA_BYTES` and the vendoring guard executes both sides
 * to say so. `stage_to_attachment` re-checks the staged file against the same
 * number, which is the funnel both adapters reach; a drift there makes the
 * daemon refuse what this accepted, which is an image fetched, written and
 * then discarded with nobody told.
 */
const MAX_MEDIA_BYTES = 16 * 1024 * 1024;

/*
 * Every `media_error` the daemon's fixed table knows. Rule 3 applies here as
 * it does to `SEND_REASONS`, and for a sharper reason: a download failure
 * carries the media URL, which carries the recipient's identifiers, and a
 * Boom error carries the whole request. So the key crosses and the sentence
 * is the daemon's.
 */
const MEDIA_ERRORS = new Set([
  'download_failed',
  'over_the_cap',
  'write_failed',
]);

/*
 * The suffix a staged file wears, from the *declared* mimetype.
 *
 * **Advisory, and trusted by nothing.** `stage_to_attachment` sniffs the
 * bytes and names the inbox copy from its own answer, because the sender
 * chose what they uploaded and the declared type is theirs to spell. What
 * this is for is that the staged stem in `sidecar.log` and the inbox copy
 * agree in the ordinary case, so the two logs read side by side — and that a
 * declared type cannot become a path component, which is why anything
 * unrecognised is `bin` rather than something derived from the string.
 */
const MEDIA_EXTENSIONS = {
  'image/png': 'png',
  'image/jpeg': 'jpg',
  'image/gif': 'gif',
  'image/webp': 'webp',
  'image/heic': 'heic',
  'image/heif': 'heif',
};
const MEDIA_EXTENSION_FALLBACK = 'bin';

// How long one media download may take before it is abandoned. The inbound
// path is serialized — order within a conversation is meaning — so an
// unbounded fetch holds every message behind it, which is a surface that has
// gone quiet rather than one message that failed.
const MEDIA_DOWNLOAD_TIMEOUT_MS = 60_000;

// How many `messages.upsert` batches may wait on the serialized inbound
// chain. Bounded and dropped loudly rather than allowed to grow, which is the
// rule the daemon's own inbound worker follows: with a per-download bound of
// a minute, an unbounded chain turns a burst of photos into head-of-line
// delay measured in hours, and the messages at the back are stale by the time
// they are read. Dropping the newest is the honest half of that trade and is
// the one thing a counter can report.
const MAX_INBOUND_QUEUE = 64;

const SOCKET_PATH = process.env.ISTOTA_BAILEYS_SOCKET || '';
const SESSION_DIR = process.env.ISTOTA_BAILEYS_SESSION_DIR || '';

// The fixed name `media.MEDIA_DIR_NAME` carries on the daemon's side.
const MEDIA_DIR_NAME = 'whatsapp-media';

/*
 * Where a staged image goes when nobody said, derived from where the session
 * lives.
 *
 * **The one variable of the three this side derives, and the exception is
 * argued rather than assumed** (ISSUE-508). The socket and the session
 * directory are genuinely the daemon's to choose — each has a config override
 * and no sibling to compute from — so for those the rule stands: the daemon
 * resolves it and hands it over. The media directory has neither. It is
 * `media.default_media_dir`, which is `{db_path.parent}/whatsapp-media`, and
 * the session directory's own default is `{db_path.parent}/whatsapp-baileys-session`,
 * so the second names the first outright.
 *
 * Requiring it anyway cost a whole-surface outage. The two-minute update cron
 * ships `docker/whatsapp-baileys/` and restarts this unit, but it is a shell
 * script and cannot re-render `istota-whatsapp-baileys.service.j2` — so the
 * new program met the old unit, exited 2 on every start, `Restart=always`
 * brought it straight back, and the journal was empty because `log` writes
 * inside the session directory and swallows its own failure. Messages sat on
 * one checkmark. Text as well as images, for a directory none of them needed.
 *
 * Sibling, **never** child, even though the session directory is the one path
 * this program holds outright: it carries a full-account credential,
 * `harden_session_files` narrows everything in it to 0600, and
 * `dir_holds_a_session` counts any file it does not recognise — so a staged
 * photo in there would make a media-only directory read as a paired session
 * and break `restore-session`'s newest-good rule.
 *
 * **The sibling rule is enforced here rather than asserted.** `path.dirname`
 * alone does not give it, which two reviewers established by running this:
 * `/a/b/.` has dirname `/a/b` and *is* `/a/b`, so the join lands inside the
 * session directory; `/a/b/..` does the same one level up; and a session
 * directory already named `whatsapp-media` derives to itself. So the path is
 * resolved first and the result is refused when it is the session directory
 * or underneath it.
 *
 * A relative value is refused outright. `path.dirname('sess')` is `'.'`, so
 * the join would stage into the process cwd — which under the Ansible unit is
 * `WorkingDirectory={{ istota_repo_dir }}/docker/whatsapp-baileys`, the
 * checkout the update cron `git reset --hard`s. Staging somebody's
 * photographs there is worse than not staging at all. The filesystem root is
 * refused for the same reason it has no sibling: everything is inside it.
 *
 * Empty is the answer to all of those, and `main()` turns it into a refusal
 * with a reason. See the note there for why that is not the refusal ISSUE-508
 * removed.
 */
function deriveMediaDir(sessionDir) {
  if (typeof sessionDir !== 'string' || !sessionDir) return '';
  if (!path.isAbsolute(sessionDir)) return '';
  const session = path.resolve(sessionDir);
  const parent = path.dirname(session);
  if (parent === session) return '';
  const derived = path.join(parent, MEDIA_DIR_NAME);
  if (derived === session || derived.startsWith(session + path.sep)) return '';
  return derived;
}

// Where a staged image is written. `media.default_media_dir` is the rule that
// picks it, and the daemon still hands it over on the spawned shape and in
// both deployment literals — this stays an override, so an operator who wants
// the directory elsewhere keeps that, and the daemon's spawn env is unchanged.
const MEDIA_DIR = process.env.ISTOTA_BAILEYS_MEDIA_DIR || deriveMediaDir(SESSION_DIR);

// --- diagnostics -----------------------------------------------------------

const LOG_LEVELS = { error: 0, warn: 1, info: 2, debug: 3 };
const LOG_LEVEL = LOG_LEVELS[process.env.ISTOTA_BAILEYS_LOG_LEVEL] ?? LOG_LEVELS.info;
const LOG_PATH = SESSION_DIR ? path.join(SESSION_DIR, 'sidecar.log') : '';

/*
 * One line to a file inside the session directory, appended, never to stdio.
 *
 * The directory is 0700 and `applyPrivateUmask` has run, so the log is as
 * private as the credential beside it — which it has to be, because
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
    // Whether `hello` has gone on the current connection. The daemon refuses
    // a connection whose first line is anything else, so a frame written to a
    // socket that is still connecting gets the link dropped rather than
    // delivered.
    this.greeted = false;
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
      this.greeted = true;
      this.onReady();
    });
    socket.on('data', (chunk) => this.feed(chunk));
    socket.on('error', (err) => log('warn', 'socket error', { code: err.code }));
    socket.on('close', () => {
      this.socket = null;
      this.buffer = '';
      this.greeted = false;
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
async function loadBaileys() {
  // **`import()` rather than `require`, and that is the v7 upgrade's one
  // structural cost.** Baileys 7 is ESM-only, which a CommonJS program cannot
  // `require` at all. Converting this file to ESM was the alternative and is
  // the worse trade: `module.exports` is what lets the default suite load the
  // program with no `node_modules` present and execute its pure functions,
  // and a dynamic import keeps both that and the laziness the drift guard
  // depends on — the specifier is resolved when a session opens, not when the
  // module loads.
  return import('@whiskeysockets/baileys');
}

const USER_JID_DOMAIN = '@s.whatsapp.net';
const GROUP_JID_DOMAIN = '@g.us';
// Not a forwardable domain of its own — `chatAddress` translates a chat in it
// to the phone JID beside it, and nothing in this namespace ever crosses the
// socket.
const LID_JID_DOMAIN = '@lid';

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

// The domain half of a JID, and nothing else. A dropped message has to be
// explicable — silence is what made a whole surface look dead — but the local
// part is the correspondent, so only the part after the last `@` is ever
// logged. An unparseable value reports its shape rather than its content.
function jidDomain(jid) {
  if (typeof jid !== 'string') return 'not-a-string';
  const at = jid.lastIndexOf('@');
  return at === -1 ? 'no-domain' : jid.slice(at + 1, at + 33);
}

/*
 * The address an inbound message is attributed to, or `''` for one this
 * surface cannot place.
 *
 * **WhatsApp addresses an ordinary one-to-one chat by LID.** A LID is a
 * durable per-contact id in a namespace of its own and it carries no phone
 * number, so `remoteJid` on a live account is routinely `<lid>@lid` — which
 * is not a thing the daemon can resolve: `identity.normalize_jid` accepts
 * `@s.whatsapp.net` alone, `jid_number` takes the E.164 out of it to compare
 * against the operator's configured bootstrap number, and
 * `address_for_binding` renders that same spelling back as a destination.
 * Forwarding the LID would move the silent drop one layer down rather than
 * fix it.
 *
 * The number is not missing, it is on a different field: Baileys stamps the
 * sender's phone JID onto the key as `senderPn`, from the server's own
 * `sender_pn` stanza attribute. That is the same channel and the same
 * authenticator `remoteJid` always came from, so trusting it changes which
 * field the resolution reads and not how far the resolution trusts the
 * stanza.
 *
 * **Substituted only for `@lid`**, deliberately: a chat WhatsApp still
 * addresses by number keeps `remoteJid`, so every path that worked before is
 * byte for byte the path it was. And **selection only** — the spelling stays
 * the daemon's to own, device suffix included, because one spelling rule in
 * one place is what `normalize_jid` exists to be.
 */
function chatAddress(key) {
  const jid = key && key.remoteJid;
  if (typeof jid !== 'string') return '';
  if (!jid.endsWith(LID_JID_DOMAIN)) return isForwardableJid(jid) ? jid : '';
  // `remoteJidAlt` is Baileys 7's name for it and is the *other* namespace's
  // address for the same correspondent — the phone JID when the chat is
  // LID-addressed, which is this branch. `senderPn` is 6.7.x's spelling and is
  // kept as a fallback so the function does not depend on which version is
  // installed; a downgrade is then a version change rather than a silent
  // return to the bug this was written for.
  const pn = key.remoteJidAlt || key.senderPn;
  return typeof pn === 'string' && pn.endsWith(USER_JID_DOMAIN) ? pn : '';
}

/*
 * Keys that ride *alongside* a message's content and are never content
 * themselves. A stanza carrying nothing but these was received but not read.
 */
const NON_CONTENT_KEYS = new Set([
  'messageContextInfo',
  'senderKeyDistributionMessage',
]);

/*
 * Whether this is a message at all, as opposed to a placeholder for one.
 *
 * **A linked device that cannot decrypt a stanza is handed a stub, not
 * nothing.** Baileys emits it through `messages.upsert` with
 * `messageStubType = CIPHERTEXT` and no content, then asks WhatsApp to resend
 * — and WhatsApp re-sends *the same message id*, re-encrypted. The stub is
 * therefore a promise of a delivery still to come.
 *
 * Forwarding one is wrong twice, and the second is what makes it expensive.
 * The daemon has no content to read, so it answers "that WhatsApp message
 * type is not supported yet" about an ordinary text message. And it claims
 * the id in `processed_whatsapp` on the way past, so every retry behind it —
 * the ones carrying the decrypted text — is refused as a duplicate and the
 * message is lost for good. Measured on a live deployment: one claim at
 * `unsupported_type`, three duplicates behind it, nothing delivered.
 *
 * So a stub is dropped and the retry is what gets forwarded. That direction
 * fails safe: the cost is silence on a message WhatsApp never redelivered,
 * against a guaranteed loss the other way.
 *
 * **An unsupported *type* is a different thing and still crosses.** An image
 * or a voice note was received and read; the daemon answers for it and is
 * right to claim the id. What is withheld here is only a message that has not
 * arrived yet.
 */
function hasReadableContent(message) {
  const content = message && message.message;
  if (!content || typeof content !== 'object') return false;
  return Object.keys(content).some((key) => !NON_CONTENT_KEYS.has(key));
}

/*
 * The media node this surface will fetch, or null.
 *
 * **`imageMessage` and nothing else.** Audio, video, documents and stickers
 * each keep the unsupported reply they have now, and a sticker is the case
 * that says why the gate is here rather than at the sniff: a sticker is WebP,
 * so it would pass a signature test cleanly. What excludes it is its message
 * type, which is a thing WhatsApp said rather than a thing the bytes say.
 *
 * Top level only, matching `messageText` — a wrapper (a disappearing or an
 * edited message) carries its real content one level down and is left for
 * whoever has a reason to walk it. `messageShape` already reports the
 * wrapper in the log line, so such a message is explicable rather than
 * silent.
 *
 * A content object holding a second media key is not a shape WhatsApp sends;
 * if one ever appears this returns the image and the rest is ignored, which
 * is the singular answer the daemon's record is shaped for.
 */
function mediaPart(message) {
  const content = message && message.message;
  if (!content || typeof content !== 'object') return null;
  const part = content.imageMessage;
  return part && typeof part === 'object' ? part : null;
}

/*
 * The words a user typed, including the caption on a photo.
 *
 * The caption rides the same field as any other message, deliberately: every
 * text gate on the daemon's side then applies to it with no new code, so
 * `STOP` means the same thing whether it was typed alone or under a picture.
 * A separate `caption` field would have needed each of those gates written
 * twice.
 *
 * `videoMessage` and `documentMessage` carry captions too and are **not**
 * read: those types keep the unsupported reply, and a caption without the
 * bytes is a message the model answers about an image nobody can see.
 */
function messageText(message) {
  const content = message && message.message;
  if (!content) return null;
  if (typeof content.conversation === 'string') return content.conversation;
  if (content.extendedTextMessage && typeof content.extendedTextMessage.text === 'string') {
    return content.extendedTextMessage.text;
  }
  const media = mediaPart(message);
  if (media && typeof media.caption === 'string') return media.caption;
  return null;
}

/*
 * A bounded, advisory suffix for a declared mimetype.
 *
 * The parameters after `;` are dropped and the lookup is exact, so nothing a
 * sender writes reaches the filename: an unrecognised or hostile value is
 * `bin`, never a slice of the string. See `MEDIA_EXTENSIONS` for why this is
 * advisory at all.
 */
function mediaExtension(mimetype) {
  if (typeof mimetype !== 'string') return MEDIA_EXTENSION_FALLBACK;
  const bare = mimetype.split(';')[0].trim().toLowerCase();
  return MEDIA_EXTENSIONS[bare] || MEDIA_EXTENSION_FALLBACK;
}

/*
 * What a staged file is called.
 *
 * **Nothing off the wire is a path component**, so this is random and the
 * suffix above is the only part anything declared. The daemon's own
 * `media.staged_name` puts a message fingerprint in front of the random half
 * and this cannot: the fingerprint's salt is the daemon's, which is exactly
 * why `media.is_staged_name` validates a *component* rather than that format.
 * Correlating a staged file with a log line is done on the stem, which both
 * sides print.
 *
 * 32 hex characters plus a dot plus at most four is well inside the 64 the
 * daemon's validator allows, and the leading character is a hex digit, which
 * its charset requires.
 */
function stagedMediaName(ext) {
  return `${crypto.randomBytes(16).toString('hex')}.${ext}`;
}

/*
 * A collector for a media download, bounded by `MAX_MEDIA_BYTES`.
 *
 * **This is where the per-file cap lives, and the cap is read from the module
 * constant rather than taken as a parameter.** A parameter with a default is
 * a second place for the number to sit, and it lets a test pass while the
 * download loop passes something else entirely.
 *
 * Past the cap it drops what it was holding as well as refusing the rest: a
 * collector that flags and keeps is still a 16 MiB buffer the caller may
 * concatenate, so the refusal would cost the memory it exists to bound.
 */
function newMediaCollector() {
  return { chunks: [], received: 0, overCap: false };
}

function collectMediaChunk(collector, chunk) {
  if (!collector || collector.overCap) return false;
  const length = chunk && chunk.length ? chunk.length : 0;
  if (collector.received + length > MAX_MEDIA_BYTES) {
    collector.chunks = [];
    collector.received = 0;
    collector.overCap = true;
    return false;
  }
  collector.chunks.push(chunk);
  collector.received += length;
  return true;
}

/*
 * Write one staged file, 0600, under `MEDIA_DIR`. Throws on anything else.
 *
 * `O_EXCL` so the name is claimed rather than written through — which also
 * refuses a symlink planted at it, since `O_CREAT | O_EXCL` fails on an
 * existing symlink rather than following it — and `O_NOFOLLOW` beside it
 * because both are free. The mode argument applies only to a file the call
 * creates, which with `O_EXCL` is every file this writes.
 *
 * The name is held to one ordinary component here as well as on the daemon's
 * side. Validating only at the reader would mean a bug in this file could put
 * bytes outside the staging directory before any frame was built, and the
 * daemon's later refusal would be about a file that already escaped.
 */
function writeStaged(name, buffer) {
  if (!MEDIA_DIR) throw new Error('no media directory is configured');
  if (typeof name !== 'string' || !name || name === '.' || name === '..' ||
      name !== path.basename(name) || name.includes('\0')) {
    throw new Error('staged media name is not a single ordinary component');
  }
  const fd = fs.openSync(
    path.join(MEDIA_DIR, name),
    fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_WRONLY |
      fs.constants.O_NOFOLLOW,
    0o600,
  );
  try {
    // `writeFileSync` on a descriptor loops until the buffer is gone.
    // `writeSync` issues one `write(2)` and *returns* the count, so a partial
    // write is not an error — and a truncated image still sniffs correctly off
    // its header, so it would be copied into somebody's inbox and fail the
    // decode with the model told the fetch succeeded.
    fs.writeFileSync(fd, buffer);
  } finally {
    fs.closeSync(fd);
  }
}

// What an `inbound` frame carries when the message had no media at all. One
// object rather than four literals at the send site, so the shape of "no
// media" cannot drift between the two branches that produce it.
const NO_MEDIA = Object.freeze({
  media_name: null, media_mime: null, media_bytes: 0, media_error: null,
});

/*
 * The **shape** of a message this side could find no text in — the protobuf
 * field names WhatsApp used, and nothing else.
 *
 * A message typed `unsupported` reaches the user as "that WhatsApp message
 * type is not supported yet", which names no type and leaves nobody able to
 * say which one it was: the body is the one thing that must never be logged,
 * so the answer cannot be read out of the transcript afterwards either. The
 * field names are the message's schema rather than its content, so they can
 * be. One level of nesting is walked because WhatsApp wraps rather than
 * replaces — a disappearing or edited message carries the real content under
 * its own key — and a wrapper reported as a wrapper says nothing useful.
 *
 * Every name is bounded and the count is capped: these come off the wire.
 */
function messageShape(message) {
  const content = message && message.message;
  if (!content || typeof content !== 'object') return 'none';
  const describe = (node, depth) => {
    const keys = Object.keys(node).slice(0, 8);
    return keys.map((key) => {
      const label = key.slice(0, 40);
      const child = node[key];
      if (depth > 0 && child && typeof child === 'object' && child.message &&
          typeof child.message === 'object') {
        return `${label}(${describe(child.message, depth - 1)})`;
      }
      return label;
    }).join(',');
  };
  return describe(content, 1) || 'empty';
}

function quotedId(message) {
  const context =
    message &&
    message.message &&
    message.message.extendedTextMessage &&
    message.message.extendedTextMessage.contextInfo;
  return context && typeof context.stanzaId === 'string' ? context.stanzaId : null;
}

/*
 * The last few message bodies this process sent, so a recipient that could not
 * decrypt one can be answered.
 *
 * **Signal encryption is per device and per session, and the first message
 * after a session goes stale routinely fails on the far side.** WhatsApp's
 * remedy is a retry receipt: the recipient asks for the message again, and the
 * sender re-encrypts it against a fresh session. Baileys implements the
 * receiving half of that and delegates the one thing only the caller has — the
 * body — to `getMessage`, whose default is `async () => undefined` and whose
 * source carries the matching TODO. With no hook, `sendMessagesAgain` logs
 * "message not available" and relays nothing, so the recipient's client sits
 * on "Waiting for this message. This may take a while." for ever.
 *
 * Measured on a live deployment across three sidecar restarts in ten minutes:
 * the two replies sent on sessions fresh from pairing reached `read`, and
 * every reply after them stuck at `accepted` with nothing delivered. The
 * self-heal WhatsApp designed for that state was simply switched off.
 *
 * **In memory and nowhere else.** The value is a person's message body, and
 * the session directory holds a full-account credential rather than a
 * transcript — nothing in this program writes content to disk and a resend
 * cache is not the place to start. The cost is that a retry arriving after a
 * restart finds an empty cache and that message stays unreadable; the
 * alternative is a plaintext message store beside the credential.
 *
 * 256 is Baileys' own number, from the TODO this implements. A `Map` keeps
 * insertion order, so eviction is the oldest key.
 */
const SENT_CACHE_LIMIT = 256;

const sentMessages = new Map();

function rememberSent(id, content) {
  if (typeof id !== 'string' || !id || !content) return;
  // Delete before set so a resend of a known id refreshes its position rather
  // than keeping the original one — otherwise a retry loop against a single
  // message ages out everything sent after it.
  sentMessages.delete(id);
  sentMessages.set(id, content);
  while (sentMessages.size > SENT_CACHE_LIMIT) {
    sentMessages.delete(sentMessages.keys().next().value);
  }
}

function recallSent(id) {
  if (typeof id !== 'string' || !id) return undefined;
  return sentMessages.get(id);
}

// How many consecutive failures to *construct* a session before calling the
// credential unusable. One is a transient fault — a half-written auth file
// mid-rotation, a DNS blip inside the library — and declaring that permanent
// refuses every send and pages the operator to re-pair a session that is fine.
const MAX_START_FAILURES = 5;
const START_RETRY_MS = 5000;

// --- the logged-out backoff ------------------------------------------------

/*
 * Spacing out the doomed logins a de-paired session would otherwise make for
 * ever.
 *
 * The `loggedOut` branch below exits, and on both shipped deployment shapes
 * something starts the program again at a fixed interval — `Restart=always`
 * with `RestartSec=30` on the systemd unit, Docker's own backoff capped at
 * 60s on compose. Neither can tell an unlinked device from a crash, and both
 * must keep restarting promptly for the second, so the bound cannot live
 * there. Nothing bounded it here either: `MAX_START_FAILURES` gates
 * `reportStartFailure`, which covers a session that could not be
 * *constructed*, and the `loggedOut` branch is upstream of that counter.
 *
 * What that cost is an account, not a log file. Every cycle is a real
 * websocket and a real authentication attempt against a number WhatsApp has
 * already unlinked once — roughly 2,400 a day on the unit — and this is a
 * client WhatsApp does not sanction, whose maintainers removed delivery ACKs
 * because accounts were being banned for them.
 *
 * So the wait goes *before* the exit, which is also what keeps a re-pair
 * prompt: the supervisor's own interval is untouched, so the start that
 * finally works is not the one being delayed. The count has to outlive a
 * process that exits, and the session directory is the only place state
 * survives one, so it goes in a 0600 file beside `sidecar.log` holding a
 * count and two timestamps and nothing else.
 */
const LOGOUT_STATE_PATH =
  SESSION_DIR ? path.join(SESSION_DIR, 'logout-backoff.json') : '';
const CREDS_PATH = SESSION_DIR ? path.join(SESSION_DIR, 'creds.json') : '';

// Long enough for the `fatal` frame to leave the socket before the process
// goes. It is a floor rather than a rung, so the first logout of a run still
// behaves exactly as it did before there was a backoff at all.
const FATAL_FLUSH_MS = 500;

// Indexed by the run length, clamped at the last entry. These are *sleeps*
// and not effective intervals: the supervisor's own interval is added on top
// and is deliberately not written down here, because a copy of `RestartSec`
// in this file would be a second place for one deployment shape's number to
// live and would be wrong for the other one.
const LOGOUT_BACKOFF_MS = [0, 30_000, 300_000, 900_000, 1_800_000, 3_600_000];

// How often the wait looks up to see whether the credential changed under it.
const CREDENTIAL_POLL_MS = 30_000;

/*
 * The rung to wait when the run could not be recorded at all (ISSUE-501).
 *
 * A run *length* rather than a duration, so a change to the ladder moves this
 * with it instead of leaving a literal here that used to be mid-ladder. It is
 * not an array index — `logoutExitDelayMs` reads `LOGOUT_BACKOFF_MS[run - 1]`
 * — so 3 is the ladder's third rung, five minutes, and "correcting" it to 2
 * would halve the fallback. Five minutes takes the unbounded case from 2,880
 * logins a day to about 290 — a bound rather than a cure, which is the right
 * size for a guess: the count is genuinely unknown on this path, so a rung
 * near the top would be asserting a long outage on no evidence.
 *
 * It cannot hold a working session down, and that is what licenses guessing
 * at all. A session that opens deletes the file, and a re-pair ends the wait
 * through the credential watch within one poll — including the documented
 * `--reset` remedy, which moves the whole directory and so changes the stamp
 * even where the directory itself is what cannot be written.
 */
const LOGOUT_UNKNOWN_RUN = 3;

function logoutExitDelayMs(count) {
  const n = Number(count);
  const run =
    Number.isFinite(n) && n >= 1
      ? Math.min(Math.floor(n), LOGOUT_BACKOFF_MS.length)
      : 1;
  // `Math.max` rather than a special case for run 1: it is also what stops a
  // shortened ladder rung from cutting the frame flush, and an index past the
  // end — `setTimeout(fn, undefined)` fires immediately, switching the
  // backoff off at exactly the run length where it matters most — is refused
  // by the clamp above rather than here.
  return Math.max(FATAL_FLUSH_MS, LOGOUT_BACKOFF_MS[run - 1]);
}

/*
 * Read a recorded run out of the file's text. Never raises.
 *
 * Anything it cannot read is nought runs, and the direction is the decision:
 * a truncated write, a hand-edited file or a half-written one on a full disk
 * degrades to today's prompt retry rather than to an hour of silence. A wait
 * this file lengthens by mistake is a working session held down.
 */
function parseLogoutState(text) {
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (err) {
    return { count: 0, first_at: '' };
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { count: 0, first_at: '' };
  }
  const n = Number(parsed.count);
  return {
    count: Number.isFinite(n) && n > 0 ? Math.floor(n) : 0,
    first_at: typeof parsed.first_at === 'string' ? parsed.first_at : '',
  };
}

// `first_at` is carried rather than restamped, so the file says how long the
// session has been unlinked and not merely when it last tried. Nothing reads
// it yet; `doctor` reporting a duration is the obvious second reader and is
// why the field is here rather than added later as a schema change.
function nextLogoutState(previous, nowIso) {
  const prev = previous || { count: 0, first_at: '' };
  return {
    count: prev.count + 1,
    first_at: prev.first_at || nowIso,
    at: nowIso,
  };
}

/*
 * Read the recorded run, and say whether the answer is a fact or a guess.
 *
 * `ENOENT` is the healthy first logout of a run: nought is the true count and
 * 500ms is the right wait for it. Every other errno means the rung is
 * *unknowable*, which is a different answer from zero — and reporting it as
 * zero is half of what pinned the ladder at its floor for ever (ISSUE-501).
 * A read that succeeds and then fails to *parse* is not unknown: the bytes
 * were reachable, so the file is one this program can replace, and the write
 * below will.
 */
function readLogoutState() {
  // Not `unknown`: no session directory is a configuration fact rather than a
  // filesystem failure, and nothing was read badly. The wait is floored all
  // the same, by the write half — with no path there is nothing to store — so
  // labelling it here would only attribute it to the wrong cause.
  if (!LOGOUT_STATE_PATH) return { count: 0, first_at: '', unknown: false };
  try {
    return parseLogoutState(fs.readFileSync(LOGOUT_STATE_PATH, 'utf8'));
  } catch (err) {
    return {
      count: 0, first_at: '', unknown: !err || err.code !== 'ENOENT',
    };
  }
}

/*
 * Advance the run and return it. Never raises.
 *
 * The caller waits on what this returns, so a failed write still has to hand
 * back a usable run — a read-only directory or a full disk must not make the
 * answer `undefined`, which `setTimeout` fires immediately on. That would
 * switch the backoff off through the one failure mode most likely to be
 * permanent. The in-memory increment stands either way; what a failed write
 * costs is that the *next* process starts the run again.
 */
function recordLogout(nowIso) {
  const previous = readLogoutState();
  const state = nextLogoutState(
    previous, nowIso || new Date().toISOString(),
  );
  let stored = false;
  if (LOGOUT_STATE_PATH) {
    try {
      fs.writeFileSync(
        LOGOUT_STATE_PATH, JSON.stringify(state) + '\n', { mode: 0o600 },
      );
      stored = true;
    } catch (err) {
      log('warn', 'the logged-out run could not be recorded', {
        kind: err && err.code,
      });
    }
  }
  /*
   * Whether the count this returns can be believed by the *next* process.
   *
   * The two halves are a union rather than a conjunction, and each covers a
   * shape the other leaves unbounded (ISSUE-501). A write that did not land
   * means the next process reads what this one read, so a run that started
   * from nought never leaves the floor — which is the read-only directory
   * with no file, where the read is a perfectly ordinary `ENOENT`. An
   * unknowable *read* means the count is a guess even when the write lands,
   * which is the unreadable file in a writable directory, rewritten to `1` on
   * every cycle for ever.
   *
   * Set after the write and never before: this is a fact about the process's
   * filesystem rather than about the run, so serializing it would let a
   * deployment that has since recovered read a stale one back.
   */
  if (!stored || previous.unknown) state.unrecorded = true;
  return state;
}

/*
 * How long to wait before exiting, given what `recordLogout` could establish.
 *
 * `Math.max` rather than a branch, so the fallback is a floor under the
 * ladder and never a replacement for it: five recorded logouts and an
 * unwritable file is still a run five long, and taking the fallback rung
 * there would cut an hour to five minutes at exactly the run length where the
 * wait matters most.
 */
function logoutWaitMs(state) {
  const recorded = logoutExitDelayMs(state && state.count);
  if (!state || !state.unrecorded) return recorded;
  return Math.max(recorded, logoutExitDelayMs(LOGOUT_UNKNOWN_RUN));
}

function clearLogoutState() {
  if (!LOGOUT_STATE_PATH) return;
  try {
    fs.unlinkSync(LOGOUT_STATE_PATH);
  } catch (err) {
    // The file is absent on every healthy start, so that is the common case
    // rather than the exception and is not worth a line.
    if (err && err.code !== 'ENOENT') {
      log('warn', 'the logged-out run could not be cleared', {
        kind: err && err.code,
      });
    }
  }
}

/*
 * A fingerprint of the paired credential, not its contents.
 *
 * What it has to separate is "the file a re-pair just replaced" from "the
 * file that was refused a minute ago", and mtime with size does that without
 * reading a full-account credential into this process for no other reason.
 *
 * The two failure strings are distinct on purpose. A credential that is
 * *removed* — the documented remedy for this state, and ISSUE-496's `--reset`
 * — is as much evidence that the next start is not the doomed one as a
 * replacement is, so it has to compare unequal to a present file rather than
 * collapsing into one constant with the no-session-directory case.
 */
function credentialStamp() {
  if (!CREDS_PATH) return 'unconfigured';
  try {
    const info = fs.statSync(CREDS_PATH);
    return `${info.mtimeMs}:${info.size}`;
  } catch (err) {
    return 'absent';
  }
}

/*
 * Wait out the backoff, then exit — unless the credential changes first.
 *
 * A plain `setTimeout` is the version that does not keep the property the
 * backoff is supposed to keep. Nothing in the re-pair flow restarts the
 * systemd unit: `istota whatsapp pair` spawns a sidecar of its own against
 * the same directory, so a session re-paired partway through an hour-long
 * wait would sit out the remainder before its first working login — a
 * working session held down by the machinery meant to protect a dead one.
 * A changed `creds.json` is exactly the evidence that the next login is no
 * longer the doomed one, so it ends the wait. Removal counts too, since
 * moving the directory aside is the documented remedy.
 *
 * **The baseline is taken at the first poll rather than here**, and that is
 * the one subtle thing in this function. `saveCreds` runs off a
 * `creds.update` event, so one raised by the login that has just failed can
 * land *after* this is called — and a baseline taken before it lands reads our own write as a
 * re-pair, ends the wait at the first poll on every rung, and leaves a
 * `logout-backoff.json` whose climbing count says the backoff is working.
 * That is the whole defect wearing a label. What the later baseline costs is
 * a blind window of one `CREDENTIAL_POLL_MS`: a credential replaced before
 * the first tick is adopted *as* the baseline, so it is never noticed at all
 * and the rung runs to its full length. Rungs 1 and 2 have no watch whatever,
 * since `min(delayMs, poll)` makes their single tick also their exit. Against
 * the rungs where the wait is long enough to matter that window is minutes
 * out of tens of minutes, and the alternative is the silent defect above.
 *
 * A top-level function taking its exit and its interval rather than a method
 * on `Session`, because `Session` is not exported and a wait loop nothing can
 * execute is pinned by substring presence alone — under which a deadline
 * computed wrongly, an inverted comparison and a tick that never reschedules
 * all stay green.
 */
function scheduleLogoutExit(delayMs, onExit, pollMs) {
  const poll = pollMs || CREDENTIAL_POLL_MS;
  const deadline = Date.now() + delayMs;
  let baseline = null;
  const tick = () => {
    const stamp = credentialStamp();
    if (baseline === null) {
      baseline = stamp;
    } else if (stamp !== baseline) {
      log('info', 'the credential changed during the wait; exiting now');
      onExit();
      return;
    }
    const left = deadline - Date.now();
    if (left <= 0) {
      onExit();
      return;
    }
    setTimeout(tick, Math.min(left, poll));
  };
  // Not `unref()`ed: the wait is the only thing holding this process open,
  // and an unreferenced timer would let Node exit immediately instead.
  setTimeout(tick, Math.min(delayMs, poll));
}

// --- the auth state --------------------------------------------------------

/*
 * The paired credential and its Signal keys, written so that a stop at any
 * instant leaves either the old file or the new one (ISSUE-554).
 *
 * Baileys' `useMultiFileAuthState` writes each file with `writeFile` in
 * place, which truncates and then writes. `creds.json` is rewritten on every
 * connection open, so a process stopped between the two left a 0-byte file,
 * and the library reads an unparseable file as no credential at all: a fresh,
 * unregistered one, a QR, and a session that is simply gone. Observed once,
 * 7ms after an open, during a replaced-connection loop that was opening every
 * few seconds.
 *
 * This is that function's logic with the one change it needs: every write
 * goes to a temp file in the same directory, is fsynced, and is renamed over
 * the target, and the directory is fsynced after. The file names are
 * Baileys' own (`authFileName`), so a directory written by the library reads
 * back unchanged. Writes are synchronous, which is what makes the library's
 * per-file mutex unnecessary: nothing can interleave inside one.
 *
 * The three library helpers it needs are passed in rather than imported, so
 * the default suite can drive it with no `node_modules` present.
 */
const AUTH_TEMP_PREFIX = '.istota-tmp-';
const CREDS_FILE = 'creds.json';
const CREDS_BACKUP_FILE = 'creds.json.bak';

function authFileName(file) {
  return String(file).replace(/\//g, '__').replace(/:/g, '-');
}

/*
 * Write `text` to `target` so that no reader ever sees a partial file.
 *
 * `O_EXCL` with `O_NOFOLLOW` on the temp name, so the create cannot land on
 * something planted there, and `rename(2)` replaces a symlink at `target`
 * rather than writing through it. 0600 at creation, whatever the umask. A
 * failure before the rename removes the temp file and raises; the target is
 * untouched. The directory fsync is what makes the rename itself survive a
 * power loss, and its failure is logged rather than raised because by then
 * the new contents are already in place.
 *
 * `durable: false` keeps the temp-and-rename and skips both fsyncs. The
 * Signal keys take it: Baileys writes 812 pre-keys in one batch at pairing,
 * and two synchronous fsyncs each would hold the event loop, and with it the
 * WhatsApp socket, for seconds. The rename alone is what protects a key file
 * from a process stopped mid-write, which is the failure that was observed;
 * `creds.json` and its backup keep the full sync, since losing either is
 * losing the session.
 */
function writeFileAtomic(target, text, options) {
  const durable = !options || options.durable !== false;
  const dir = path.dirname(target);
  const temp = path.join(
    dir,
    `${AUTH_TEMP_PREFIX}${path.basename(target)}.${crypto.randomBytes(6).toString('hex')}`,
  );
  const flags =
    fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL |
    (fs.constants.O_NOFOLLOW || 0);
  const fd = fs.openSync(temp, flags, 0o600);
  try {
    fs.writeFileSync(fd, text);
    if (durable) fs.fsyncSync(fd);
  } catch (err) {
    try { fs.closeSync(fd); } catch (ignored) {}
    try { fs.unlinkSync(temp); } catch (ignored) {}
    throw err;
  }
  fs.closeSync(fd);
  try {
    fs.renameSync(temp, target);
  } catch (err) {
    try { fs.unlinkSync(temp); } catch (ignored) {}
    throw err;
  }
  if (!durable) return;
  try {
    const dirFd = fs.openSync(dir, 'r');
    try { fs.fsyncSync(dirFd); } finally { fs.closeSync(dirFd); }
  } catch (err) {
    log('warn', 'the session directory could not be synced', { kind: err && err.code });
  }
}

/*
 * Read one stored credential. `absent`, `unreadable` or `ok`, never a raise.
 *
 * `unreadable` covers empty, whitespace, truncated JSON and a value that is
 * not an object, which are the shapes an interrupted write or a full disk
 * leaves. It is kept apart from `absent` because the two mean opposite
 * things: no file is a session that has never paired, and a broken one is a
 * session that did and was lost.
 */
function readStoredJson(file, reviver) {
  let text;
  try {
    text = fs.readFileSync(file, 'utf8');
  } catch (err) {
    if (err && err.code === 'ENOENT') return { status: 'absent' };
    return { status: 'unreadable', kind: (err && err.code) || 'read_failed' };
  }
  try {
    return { status: 'ok', value: JSON.parse(text, reviver) };
  } catch (err) {
    return { status: 'unreadable', kind: text.trim() ? 'unparseable' : 'empty' };
  }
}

function readStoredCreds(file, reviver) {
  const stored = readStoredJson(file, reviver);
  if (stored.status !== 'ok') return stored;
  const value = stored.value;
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return { status: 'unreadable', kind: 'not_an_object' };
  }
  return stored;
}

// Temp files a killed writer left behind. They hold a half-written copy of a
// full-account credential and are never read, so they go at load.
function sweepAuthTempFiles(folder) {
  let names = [];
  try {
    names = fs.readdirSync(folder);
  } catch (err) {
    return;
  }
  for (const name of names) {
    if (!name.startsWith(AUTH_TEMP_PREFIX)) continue;
    try {
      fs.unlinkSync(path.join(folder, name));
    } catch (err) {
      log('warn', 'a stray auth temp file could not be removed', { kind: err && err.code });
    }
  }
}

/*
 * What the stored credential is, read before anything is opened: `absent`,
 * `ok`, `backup` (the main file is broken and the backup is usable) or
 * `unreadable` (broken, and no usable backup), with `kind` saying how the
 * main file failed — an errno for a read that failed, or `empty`,
 * `unparseable`, `not_an_object`. Plain `JSON.parse`, so it needs no
 * library: the reviver only turns encoded Buffers back into Buffers, and a
 * file that parses without it parses with it.
 */
function storedCredentialVerdict(folder) {
  if (!folder) return { verdict: 'absent' };
  const main = readStoredCreds(path.join(folder, CREDS_FILE));
  if (main.status === 'ok') return { verdict: 'ok' };
  if (main.status === 'absent') return { verdict: 'absent' };
  const backup = readStoredCreds(path.join(folder, CREDS_BACKUP_FILE));
  return {
    verdict: backup.status === 'ok' ? 'backup' : 'unreadable',
    kind: main.kind,
  };
}

// How long a process holding an unreadable credential waits before exiting.
// It opens nothing while it waits, so the wait costs no login; it bounds how
// long a repaired directory goes unnoticed if the credential watch misses it.
const CREDENTIAL_UNREADABLE_WAIT_MS = 3_600_000;

/*
 * Baileys' `useMultiFileAuthState`, with atomic writes and a backup.
 *
 * `source` says where the credential came from: `main`, `backup`, `fresh`
 * (nothing on disk, a first pairing) or `unreadable` (a credential that
 * existed and cannot be read, with no usable backup). The backup is written
 * after every save, so it is the last credential that saved successfully;
 * it is read when `creds.json` is broken, and restored over it at
 * once so the daemon's `session_is_registered` reads what the sidecar is
 * acting on.
 */
async function useAtomicAuthState(folder, lib) {
  const { initAuthCreds, BufferJSON, proto } = lib || {};
  // Refused rather than degraded: without the library's replacer and reviver
  // the Buffers in a credential are written in a form nothing revives, which
  // corrupts the session quietly instead of failing.
  if (
    typeof initAuthCreds !== 'function' ||
    !BufferJSON || typeof BufferJSON.replacer !== 'function' ||
    typeof BufferJSON.reviver !== 'function' ||
    !proto || !proto.Message || !proto.Message.AppStateSyncKeyData
  ) {
    throw new Error('the Baileys auth helpers are missing');
  }
  const replacer = BufferJSON.replacer;
  const reviver = BufferJSON.reviver;

  let info = null;
  try {
    info = fs.statSync(folder);
  } catch (err) {
    info = null;
  }
  if (info && !info.isDirectory()) {
    throw new Error('the session path is not a directory');
  }
  if (!info) fs.mkdirSync(folder, { recursive: true, mode: 0o700 });
  sweepAuthTempFiles(folder);

  const fileFor = (name) => path.join(folder, authFileName(name));
  const writeData = (value, name, options) => {
    writeFileAtomic(fileFor(name), JSON.stringify(value, replacer), options);
  };
  const readData = (name) => {
    // Whatever the file parses to, as the library did: a key's value is not
    // always an object.
    const stored = readStoredJson(fileFor(name), reviver);
    return stored.status === 'ok' ? stored.value : null;
  };
  const removeData = (name) => {
    try {
      fs.unlinkSync(fileFor(name));
    } catch (err) {
      // Absent is the goal.
    }
  };

  const main = readStoredCreds(fileFor(CREDS_FILE), reviver);
  let creds;
  let source;
  if (main.status === 'ok') {
    creds = main.value;
    source = 'main';
  } else if (main.status === 'absent') {
    // Not the backup. The writer never leaves `creds.json` missing once it
    // has written one, so an absent file beside a backup is somebody's
    // deliberate removal, and restoring the credential they removed would
    // undo it.
    creds = initAuthCreds();
    source = 'fresh';
  } else {
    const backup = readStoredCreds(fileFor(CREDS_BACKUP_FILE), reviver);
    if (backup.status === 'ok') {
      creds = backup.value;
      source = 'backup';
      log('warn', 'creds.json is unreadable; starting from creds.json.bak', {
        main: main.status,
      });
      try {
        writeData(creds, CREDS_FILE);
      } catch (err) {
        log('warn', 'creds.json could not be restored from the backup', {
          kind: err && err.code,
        });
      }
    } else {
      log('error', 'creds.json is unreadable and there is no usable backup', {
        backup: backup.status,
      });
      creds = initAuthCreds();
      source = 'unreadable';
    }
  }

  return {
    source,
    state: {
      creds,
      keys: {
        get: async (type, ids) => {
          const data = {};
          for (const id of ids) {
            let value = readData(`${type}-${id}.json`);
            if (type === 'app-state-sync-key' && value) {
              value = proto.Message.AppStateSyncKeyData.fromObject(value);
            }
            data[id] = value;
          }
          return data;
        },
        set: async (data) => {
          for (const category of Object.keys(data)) {
            for (const id of Object.keys(data[category])) {
              const value = data[category][id];
              const name = `${category}-${id}.json`;
              if (value) writeData(value, name, { durable: false });
              else removeData(name);
            }
          }
        },
      },
    },
    saveCreds: async () => {
      writeData(creds, CREDS_FILE);
      // Second, so a stop between the two leaves a new main and an old
      // backup, both readable.
      writeData(creds, CREDS_BACKUP_FILE);
    },
  };
}

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
    // Whether WhatsApp has unlinked the device. Distinct from `stopping`,
    // which is also what a deliberate shutdown sets: the daemon link has to
    // keep reconnecting through a logout wait and must not through a
    // shutdown, and only one of the two states has a verdict to re-announce.
    this.loggedOut = false;
    // Whether the run behind the wait below reached disk. Carried on the
    // re-announced verdict as well as on the frame that first reported it,
    // because the daemon's latch is in memory: a scheduler that restarted
    // during the wait would otherwise re-learn the logout and not that the
    // backoff behind it is running on a guess.
    this.runUnrecorded = false;
    // Whether the stored credential is lost (ISSUE-552). Like `loggedOut`, a
    // verdict to re-announce on every daemon-link reconnect, since the
    // daemon's latch is in memory.
    this.credentialUnreadable = false;
    // The tail of the serialized inbound chain. See `onMessages`: a batch is
    // appended to it rather than handled where it arrives, so a photo's
    // download cannot be overtaken by the text message behind it.
    this.inbound = Promise.resolve();
    this.inboundDepth = 0;
    this.inboundDropped = 0;
  }

  announceReady() {
    if (this.credentialUnreadable) {
      this.link.send(MSG_FATAL, {
        reason: FATAL_CREDENTIAL_UNREADABLE, permanent: true,
      });
      return;
    }
    if (this.loggedOut) {
      // **The verdict, not the readiness.** The daemon's permanent-fatal
      // latch is in memory and is set only by this frame, so a scheduler that
      // restarted during the wait has none — and with the wait now lasting up
      // to an hour rather than 500ms, `doctor` and the admin alert would
      // report "no sidecar connected" instead of "logged out, re-pair" for
      // all of it. Re-sending is what keeps the reason on the daemon's side
      // for as long as this process is the thing holding the session.
      this.link.send(MSG_FATAL, {
        reason: FATAL_LOGGED_OUT,
        permanent: true,
        run_unrecorded: this.runUnrecorded,
      });
      return;
    }
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
    // **Before the library is loaded or anything is opened.** The library
    // would read the broken file as no credential and offer a QR, and its
    // first save would rename a fresh credential over the evidence.
    const stored = storedCredentialVerdict(SESSION_DIR);
    if (stored.verdict === 'unreadable') {
      this.refuseUnreadableCredential(stored.kind);
      return;
    }
    const baileys = await loadBaileys();
    const auth = await useAtomicAuthState(SESSION_DIR, baileys);
    if (auth.source === 'unreadable') {
      // The file broke between the check above and the read. Same answer.
      this.refuseUnreadableCredential('changed_during_load');
      return;
    }
    const { state, saveCreds } = auth;
    const logger = silentLogger();
    const sock = baileys.makeWASocket({
      // **Not `auth: state` directly.** The auth state reads each
      // Signal key back off the disk on demand, so a key written and then
      // immediately read again can miss — and a session that reads as absent
      // is a session Baileys re-establishes with a fresh PreKey handshake it
      // did not need, which is one of the ways the far side ends up unable to
      // decrypt. The wrapper is Baileys' own answer to that race and is what
      // its README passes.
      auth: {
        creds: state.creds,
        keys: baileys.makeCacheableSignalKeyStore(state.keys, logger),
      },
      // Off, and this is the point of rule 1 rather than a preference:
      // Baileys' default logger writes JIDs and message content to stdout.
      printQRInTerminal: false,
      logger,
      // Answering a retry receipt. Without this Baileys has the protocol and
      // not the body, so it relays nothing and the recipient waits for ever.
      getMessage: async (key) => {
        const found = recallSent(key && key.id);
        log('info', 'a recipient asked for a message again', {
          served: Boolean(found),
        });
        return found;
      },
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

    // Guarded like its three siblings, which it was not. Two reasons, and the
    // second is new: an orphan socket writing the auth state is the same
    // two-writers hazard `mine()` exists for, and after a logout `this.sock`
    // is nulled — so this guard is what stops a late save from moving
    // `creds.json` under a wait that fingerprints it to notice a re-pair.
    sock.ev.on('creds.update', () => {
      if (mine()) saveCreds().catch((err) => this.onSaveFailed(err));
    });
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

  /*
   * Report a lost credential and hold, opening nothing (ISSUE-552).
   *
   * Permanent, so the daemon latches it, refuses every send definitely and
   * alerts once, through the path an unlinked device already takes; the
   * remedy is the same `istota whatsapp pair --reset`, which moves the
   * damaged directory aside rather than deleting it. Sent here only on a
   * link that has already said `hello`: on a cold start this runs before the
   * link connects, and a frame written ahead of `hello` gets the connection
   * refused. `announceReady` sends it on every connect instead.
   *
   * `kind` is logged because "cannot be read" covers a permission or type
   * error as well as a lost file, and the fix for those is a `chown`.
   *
   * Then it waits rather than exiting, so a supervisor does not restart it
   * every thirty seconds into the same answer. The wait watches `creds.json`
   * as the logged-out one does, so a re-pair or a restore ends it early.
   */
  refuseUnreadableCredential(kind) {
    if (this.credentialUnreadable) return;
    this.credentialUnreadable = true;
    this.stopping = true;
    this.sock = null;
    log('error', 'creds.json cannot be read and there is no usable backup; '
      + 'nothing will be opened until it is repaired or re-paired', {
      kind: kind || 'unknown',
    });
    if (this.link.greeted) {
      this.link.send(MSG_FATAL, {
        reason: FATAL_CREDENTIAL_UNREADABLE, permanent: true,
      });
    }
    scheduleLogoutExit(CREDENTIAL_UNREADABLE_WAIT_MS, () => process.exit(1));
  }

  // A save that failed leaves the previous `creds.json` in place, which is
  // the point of writing it atomically; the next `creds.update` tries again.
  // Caught because an unhandled rejection ends a Node process.
  onSaveFailed(err) {
    log('warn', 'the credential could not be saved', { kind: err && err.code });
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
      // run of construction failures below starts again from zero — and so
      // does the recorded run of logouts, for the same reason one level out.
      this.startFailures = 0;
      clearLogoutState();
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
      // **One-shot.** `if (this.stopping) return;` sits below this branch, so
      // it does not guard it, and `mine()` still passes for the socket that
      // has just closed — so a repeated close carrying the same status would
      // re-enter. That was harmless when the branch was one `setTimeout`, and
      // is not now that it writes a persisted count: one duplicate event
      // would cost a rung, permanently advancing the ladder on the evidence
      // of a single unlink, which is the direction this whole mechanism must
      // not fail in.
      if (this.loggedOut) return;
      this.loggedOut = true;
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
      // latch.
      this.stopping = true;
      // **Dropped, not merely stopped.** `send`'s `if (!this.sock)` guard is
      // the only thing that answers a send with a definite `not_connected`;
      // with the socket left in place a send reaches `sendMessage` on a dead
      // one and settles the ledger `unknown`, the one state an operator
      // cannot resolve. The daemon's own latch should stop a send ever
      // arriving, but that window was 500ms and is now up to an hour, so it
      // should not be the only thing standing there. Nulling also makes
      // `mine()` false for every handler this socket installed, which is what
      // stops a late `creds.update` writing the credential the wait below
      // fingerprints.
      this.sock = null;
      // Recorded *before* the wait, not after it: a `systemctl restart` or a
      // SIGTERM partway through a half-hour one must not lose the increment,
      // or an operator's own intervention resets the ladder to its first rung
      // and the loop is unbounded again. The frame above has already gone, so
      // the daemon latches the fatal and alerts at the moment the session
      // died rather than at the end of the wait.
      const run = recordLogout();
      const delay = logoutWaitMs(run);
      // **The marker is reported, not just acted on** (ISSUE-501). The only
      // other signal this condition has is the `warn` line `recordLogout`
      // writes, and `log()` appends to `sidecar.log` *inside* the directory
      // that cannot be written — so on the most likely trigger it goes
      // nowhere at all. A second `fatal` rather than a field on the first,
      // because the first one's position is load-bearing: it has to leave
      // before any filesystem work, which on a hung mount could block for
      // ever. The healthy path sends exactly one frame as before, and the
      // daemon's once-per-outage alert has already fired on it, so this
      // updates `doctor`'s answer without paging anybody twice.
      this.runUnrecorded = run.unrecorded === true;
      if (this.runUnrecorded) {
        this.link.send(MSG_FATAL, {
          reason: FATAL_LOGGED_OUT, permanent: true, run_unrecorded: true,
        });
      }
      log('warn', 'the device link ended; waiting before exiting', {
        run: run.count, wait_ms: delay, unrecorded: this.runUnrecorded,
      });
      scheduleLogoutExit(delay, () => process.exit(1));
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
    // Both spellings: `require` says `MODULE_NOT_FOUND` and the dynamic
    // `import()` in `loadBaileys` says `ERR_MODULE_NOT_FOUND`, so matching the
    // first alone left this branch unreachable once the library became ESM.
    if (err && (err.code === 'MODULE_NOT_FOUND' || err.code === 'ERR_MODULE_NOT_FOUND')) {
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

  /*
   * Hand a batch to the serialized inbound chain.
   *
   * **Serialized because fetching an image is an `await` and reading a text
   * message is not.** Handling batches concurrently would let a short text
   * message overtake the photo sent before it, which reorders a conversation
   * at the source — and the daemon's own inbound worker is serial precisely
   * because order within a conversation is meaning. Doing it here rather than
   * there is the only place it can be done: the daemon sees whatever order
   * the socket delivers.
   *
   * The `catch` is not tidiness. Without it one rejected batch becomes the
   * chain's tail and every later message is dropped for the life of the
   * process, silently, which is the whole-surface failure this file's filters
   * are written to avoid one message at a time.
   */
  onMessages(event) {
    if (!event || !Array.isArray(event.messages)) return;
    if (this.inboundDepth >= MAX_INBOUND_QUEUE) {
      this.inboundDropped += 1;
      log('warn', 'inbound batch dropped', {
        why: 'queue_full', dropped: this.inboundDropped,
      });
      return;
    }
    this.inboundDepth += 1;
    this.inbound = this.inbound
      .then(() => this.handleMessages(event))
      .catch((err) => {
        log('error', 'an inbound batch escaped', { kind: err && err.name });
      })
      // After the catch, so it runs on both paths: a depth that only
      // decremented on success would ratchet to the bound and stay there.
      .then(() => { this.inboundDepth -= 1; });
  }

  async handleMessages(event) {
    // Both filters below drop a message and return nothing, so a surface that
    // is receiving and discarding everything is indistinguishable from one
    // receiving nothing at all — on either side of the socket. The arrival
    // itself is therefore recorded before any of them run.
    log('info', 'messages.upsert arrived', {
      count: event.messages.length, kind: event.type || 'none',
    });
    for (const message of event.messages) {
      // `fromMe` is our own send echoed back. Ingesting it would put the
      // bot's own answer into the user's task history as their next request.
      // Debug rather than info: this fires once per outbound message, for
      // ever, and it explains nothing a reader of this log wants explained.
      if (!message || !message.key) continue;
      if (message.key.fromMe) {
        log('debug', 'inbound dropped', { why: 'from_me' });
        continue;
      }
      const jid = chatAddress(message.key);
      if (!jid) {
        log('info', 'inbound dropped', {
          why: 'no_usable_address', domain: jidDomain(message.key.remoteJid),
        });
        continue;
      }
      if (!hasReadableContent(message)) {
        // Not a message yet. Baileys has already asked WhatsApp to resend it,
        // and the retry carries the same id — which is exactly why this must
        // not cross: the daemon would claim that id and refuse the retry.
        log('info', 'inbound withheld until it decrypts', {
          shape: messageShape(message),
          stub: message.messageStubType === undefined
            ? 'none' : String(message.messageStubType).slice(0, 40),
        });
        continue;
      }
      const group = isGroupJid(jid);
      const text = group ? null : messageText(message);
      // A group message is refused above every identity lookup on the
      // daemon's side, so fetching its media would be bytes on disk for a
      // message nothing will ever consume.
      const part = group ? null : mediaPart(message);
      if (!group && !part && text === null) {
        log('info', 'inbound has no text this side can read', {
          shape: messageShape(message),
        });
      }
      const media = part ? await this.downloadMedia(message) : NO_MEDIA;
      // The `group` flag is read off the chat rather than inferred from the
      // JID's spelling on the daemon's side, which is why it is sent: the
      // daemon refuses a group message before any identity lookup.
      //
      // An image is typed `image` whether or not the fetch worked: a failed
      // one carries `media_error` and the daemon answers "that image could
      // not be fetched", which is a different and better answer from "that
      // message type is not supported yet".
      const delivered = this.link.send(MSG_INBOUND, {
        message_id: message.key.id,
        jid,
        username: group ? null : message.pushName || null,
        message_type: part ? 'image' : (text === null ? 'unsupported' : 'text'),
        text,
        callback_data: null,
        reply_to_message_id: group ? null : quotedId(message),
        group,
        timestamp: Number(message.messageTimestamp) || Math.floor(Date.now() / 1000),
        media_name: media.media_name,
        media_mime: media.media_mime,
        media_bytes: media.media_bytes,
        media_error: media.media_error,
      });
      if (!delivered) {
        // `Link.send` answers false for a destroyed socket and says nothing.
        // Before the chain existed the send happened inside the event
        // handler, so this could only lose a message to an encode failure;
        // now a download can outlive the daemon link and the loss is silent.
        log('warn', 'an inbound message reached nobody', {
          why: 'link_unavailable', staged: Boolean(media.media_name),
        });
      }
    }
  }

  /*
   * Fetch one image onto disk and describe it, or say why not.
   *
   * **No media bytes cross the socket.** The frame names a file the daemon
   * can open; the daemon sniffs it, copies it into the sender's workspace and
   * unlinks the staged copy. This side downloads because it already holds the
   * decryption keys and nothing else does.
   *
   * Streamed rather than buffered, which is what makes the cap an abort:
   * `collectMediaChunk` refuses partway through and the stream is destroyed,
   * where `'buffer'` would hand back a file of any size already in memory.
   *
   * `reuploadRequest` is what lets WhatsApp re-serve media whose URL has
   * expired instead of the download simply failing — the spec's "expired
   * Baileys media" case landing on the happier branch where it can.
   *
   * Every failure is a key out of `MEDIA_ERRORS` and nothing more: a download
   * error carries the media URL, which carries the recipient's identifiers,
   * and a Boom error carries the whole request.
   */
  async downloadMedia(message) {
    const failure = (raw) => {
      // The same guard `answer` puts in front of `SEND_REASONS`, and for the
      // same reason: a key outside the daemon's table renders as the generic
      // sentence for ever, so a typo at a call site below would cost the
      // diagnostic silently. This is the only producer of a `media_error`.
      const key = MEDIA_ERRORS.has(raw) ? raw : 'download_failed';
      log('warn', 'inbound media was not staged', { why: key });
      return {
        media_name: null, media_mime: null, media_bytes: 0, media_error: key,
      };
    };
    const part = mediaPart(message);
    const mime = typeof part.mimetype === 'string' ? part.mimetype : null;
    const collector = newMediaCollector();
    let stream = null;
    // The connect, the `reuploadRequest` round trip and the body all sit
    // inside one deadline. An earlier shape armed the timer *after*
    // `downloadMediaMessage` resolved, which left the CDN connect and the
    // reupload — the two slowest things here, and the ones that hang —
    // bounded by nothing at all.
    const fetchAll = async () => {
      const baileys = await loadBaileys();
      stream = await baileys.downloadMediaMessage(
        message,
        'stream',
        {},
        {
          logger: silentLogger(),
          // Wrapped rather than passed as a bare property reference: a method
          // read off the socket is called by Baileys with no receiver. The
          // wrapper is correct whether or not the library happens to close
          // over its own state, and `undefined` is what its own guard expects
          // when there is no socket — after a logout `this.sock` is null.
          reuploadRequest: this.sock
            ? (media) => this.sock.updateMediaMessage(media)
            : undefined,
        },
      );
      await new Promise((resolve, reject) => {
        const settle = (err) => { if (err) reject(err); else resolve(); };
        stream.on('data', (chunk) => {
          if (!collectMediaChunk(collector, chunk)) settle(null);
        });
        stream.on('end', () => settle(null));
        stream.on('error', (err) => settle(err));
      });
    };
    let deadline = null;
    try {
      // Bounded, because the inbound chain is serial: a hung fetch holds
      // every message behind it, which is a surface gone quiet rather than
      // one message lost. Racing releases the chain; it cannot cancel a fetch
      // already inside the library, so `destroy` below is what stops the
      // bytes still arriving.
      await Promise.race([
        fetchAll(),
        new Promise((resolve, reject) => {
          deadline = setTimeout(
            () => reject(new Error('media download timed out')),
            MEDIA_DOWNLOAD_TIMEOUT_MS,
          );
        }),
      ]);
    } catch (err) {
      return failure('download_failed');
    } finally {
      clearTimeout(deadline);
      // Whether the cap fired, the deadline did, or the stream ended: the
      // reader is detached either way and a stream abandoned mid-body would
      // otherwise keep its socket.
      try { if (stream && stream.destroy) stream.destroy(); } catch (err) {}
    }
    if (collector.overCap) return failure('over_the_cap');
    // A fetch that yielded nothing is a failure rather than a zero-byte
    // image: staging one costs a file the daemon sniffs, refuses and sweeps,
    // and reports success on the frame while doing it.
    if (collector.received === 0) return failure('download_failed');
    const name = stagedMediaName(mediaExtension(mime));
    try {
      writeStaged(name, Buffer.concat(collector.chunks));
    } catch (err) {
      return failure('write_failed');
    }
    log('info', 'inbound media staged', {
      file: name, bytes: collector.received,
    });
    return {
      media_name: name,
      media_mime: mime,
      media_bytes: collector.received,
      media_error: null,
    };
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
      // `sent.message` is the generated content, which is what `relayMessage`
      // re-encrypts on a retry — the `{ text }` handed in above is not.
      rememberSent(id, sent && sent.message);
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

/*
 * Make every file this process creates private to the account running it.
 *
 * The session directory holds a **full-account WhatsApp credential**: anything
 * that can read it can send and read as the paired number, with no second
 * factor and nothing the account holder would see. Baileys writes those files,
 * so the daemon cannot create them at the right mode — the umask of the
 * process Baileys runs in is the only thing that decides it at birth, and
 * `harden_session_files` on the Python side runs once when the bridge starts
 * and so can only narrow what is already there. A live session writes a new
 * pre-key every few messages.
 *
 * Set **here** because the program is the one place all three launch shapes
 * pass through. The daemon's own spawn already passes `umask=0o077` and covers
 * exactly one of them: on Ansible this is a systemd unit, whose default
 * `UMask` is 0022, and on compose it is a service of its own — and a compose
 * service cannot express a umask at all. Both of those were writing 0644, and
 * `doctor`'s `whatsapp.baileys_session` check is what said so.
 *
 * The unit sets `UMask=0077` as well. That is defence in depth and the half an
 * operator reading the unit can see; this is the mechanism.
 */
function applyPrivateUmask() {
  process.umask(0o077);
}

function main() {
  applyPrivateUmask();
  if (!SOCKET_PATH || !SESSION_DIR) {
    // No log destination either — the session directory is where the log
    // lives. Exiting non-zero is the only channel left, and the daemon's
    // supervisor reports it as a spawn that did not stay up.
    //
    process.exit(2);
  }
  if (!MEDIA_DIR) {
    // **Not the refusal ISSUE-508 removed, and the difference is what it is
    // keyed on.** That one fired whenever `ISTOTA_BAILEYS_MEDIA_DIR` was
    // absent, which is every unit rendered before the variable existed — so
    // an update cron that ships this program and cannot re-render the unit
    // took the whole surface down. This fires only when the variable is
    // absent *and* `SESSION_DIR` is one `deriveMediaDir` refuses to work
    // from: relative, the filesystem root, or the media directory itself.
    // Both shipped shapes pass an absolute canonical literal and every stale
    // unit passes the same one it always did, so no deployment reaches this;
    // it takes a hand-edited value, and naming the variable is the fix.
    //
    // Refusing rather than staging somewhere wrong is the right direction
    // here: the alternatives `deriveMediaDir` rejected are the process cwd
    // (the checkout the cron resets) and a directory inside the session
    // directory, where `dir_holds_a_session` would read staged photographs as
    // a paired session and break `restore-session`'s newest-good rule.
    log('error', 'the session directory yields no media directory beside it', {
      variable: 'ISTOTA_BAILEYS_MEDIA_DIR',
    });
    process.exit(2);
  }
  try {
    // The daemon's `media.ensure_media_dir` is authoritative for the mode and
    // runs when the bridge starts; this only has to make the directory exist.
    // On Ansible and compose the sidecar is a unit of its own with no ordering
    // guarantee against the daemon, so without this a boot in the other order
    // answers `write_failed` for every image until the daemon catches up —
    // and `recursive: true` makes an existing directory a no-op, so the
    // daemon stays the only thing that ever narrows one.
    fs.mkdirSync(MEDIA_DIR, { recursive: true, mode: 0o700 });
  } catch (err) {
    log('error', 'the media staging directory could not be created', {
      kind: err && err.code,
    });
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
    // `stopping` alone would stop reconnecting through a logout wait, which
    // now lasts up to an hour — long enough for a scheduler restart to land
    // inside one and leave a sidecar that neither reconnects nor exits, with
    // the unlinked-device verdict reaching nobody. A logout wait reconnects
    // and re-announces the fatal; a deliberate shutdown still does not.
    if (session.stopping && !session.loggedOut && !session.credentialUnreadable) {
      return;
    }
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
  applyPrivateUmask,
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
  MAX_MEDIA_BYTES,
  MEDIA_ERRORS,
  MEDIA_EXTENSIONS,
  chatAddress,
  collectMediaChunk,
  deriveMediaDir,
  encode,
  hasReadableContent,
  mediaExtension,
  mediaPart,
  messageText,
  newMediaCollector,
  stagedMediaName,
  writeStaged,
  rememberSent,
  recallSent,
  SENT_CACHE_LIMIT,
  AUTH_TEMP_PREFIX,
  authFileName,
  writeFileAtomic,
  readStoredCreds,
  useAtomicAuthState,
  storedCredentialVerdict,
  logoutExitDelayMs,
  LOGOUT_UNKNOWN_RUN,
  logoutWaitMs,
  parseLogoutState,
  nextLogoutState,
  readLogoutState,
  recordLogout,
  clearLogoutState,
  credentialStamp,
  scheduleLogoutExit,
  messageShape,
  receiptStatus,
  sendFailureReason,
};
