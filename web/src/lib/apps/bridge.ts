/**
 * The shell's half of the app bridge: the relay between an app frame and the
 * server.
 *
 * The shell is not the enforcement point; the server's call route is, and it
 * checks every call against the installed version's grants. What the relay
 * holds is narrower and still load-bearing:
 *
 * - **The port.** The bridge is bound to the installed document, not to the
 *   frame's window. The window is the same object across a navigation, and a
 *   page the frame navigated to controls when its own `load` fires, so a check
 *   on `event.source` and a load count would let that page call with the app's
 *   grants until then. Instead `connect`, run on the frame's first `load`,
 *   posts one end of a `MessageChannel` to the frame (`'*'`, since an opaque
 *   origin cannot be named as a target), and every call, reply and `env`
 *   message after that travels on the channel. The shell takes no call from a
 *   window.
 * - **The key.** A page that navigates the frame before its first `load` is
 *   the one that load belongs to, so it is handed the port. What tells the
 *   installed document from that page is a one-time key the kit makes in its
 *   own closure and posts to this window while it is parsed, before any app
 *   script exists. From the moment it is created the bridge listens on its own
 *   window and takes only the first message whose `source` is the frame's
 *   window: if that is a well-formed `key` message its `k` is recorded,
 *   otherwise no key ever is, and every later window message is ignored. So
 *   create the bridge before the frame is given its `src`. The key is never in
 *   the URL, so app code cannot read it back (from the navigation timing
 *   entry, an error's stack or anything else) and forward it to a page it
 *   navigates to.
 * - **The hello.** Nothing on the port is answered or sent until the kit's
 *   `hello` echoes the recorded key; with no recorded key no hello is taken. A
 *   hello that never comes within `HELLO_TIMEOUT_MS` is reported through
 *   `onUnresponsive` so the shell can remove the frame.
 * - **The shape check.** Anything that is not one of the kit's two request
 *   shapes is dropped without a reply.
 * - **Only the installed document.** After the frame has navigated, nothing
 *   from the port is answered and nothing is sent on it; the page also
 *   removes the frame and disposes the bridge.
 * - **Shell calls** (`env`, `copy`, `error`) are answered here and reach no
 *   data. None of them can be widened into a capability.
 *
 * The network, the clipboard and the error route come in as functions, so this
 * file imports nothing from the app: the escape tier's shell page (stage 11)
 * loads it as it is, beside `frameWatch.ts`.
 */
import type { AppErrorKind, BridgeFailure, CallOutcome } from './api';
import type { AppEnv } from './env';

export const PROTOCOL = 1;

/** How long the installed document has to echo its key before the frame is suspect. */
export const HELLO_TIMEOUT_MS = 4000;

/** The longest key taken from the frame's first message. The kit's is 32 characters. */
export const KEY_MAX = 128;

/** Error reports forwarded per frame load; a looping app stops being heard after this. */
export const ERROR_REPORTS_PER_LOAD = 20;
const REPORT_TEXT_MAX = 4096;
/** The longest capability or op name relayed. The server holds the real rule. */
const NAME_MAX = 200;
const COPY_MAX = 1_000_000;

export interface CallRequest {
  istota: 1;
  id: number;
  kind: 'call';
  capability: string;
  op: string;
  args: Record<string, unknown>;
}

export interface ShellRequest {
  istota: 1;
  id: number;
  kind: 'shell';
  op: 'env' | 'copy' | 'error';
  args: Record<string, unknown>;
}

export type BridgeRequest = CallRequest | ShellRequest;

export type BridgeReply =
  | { istota: 1; id: number; ok: true; result: unknown }
  | { istota: 1; id: number; ok: false; error: BridgeFailure };

export interface BridgeOptions {
  /** Override `HELLO_TIMEOUT_MS` (tests shorten it). */
  helloTimeoutMs?: number;
  /** The frame never said hello: what loaded is not the installed document. */
  onUnresponsive?: () => void;
  /**
   * The frame's window: where `connect` posts the port, and the one source
   * whose first message on this window is read as the key.
   */
  frameWindow: () => Window | null;
  /** Loads after the first (`FrameWatch.navigations`). */
  navigations: () => number;
  online: () => boolean;
  env: () => AppEnv;
  /** Relay a data call to the server (`api.callApp` for this app and version). */
  call: (request: {
    capability: string;
    op: string;
    args: Record<string, unknown>;
  }) => Promise<CallOutcome>;
  copy: (text: string) => Promise<boolean>;
  report: (report: { kind: AppErrorKind; message: string; stack: string }) => Promise<void>;
  /** Told about a call's failure, so the page can act on `not_found` or a lost session. */
  onCallFailed?: (code: BridgeFailure['code']) => void;
  /** The call threw rather than answering: an expired session. */
  onCallThrew?: (error: unknown) => void;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

function isName(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= NAME_MAX;
}

/** One of the kit's two request shapes, or null for anything else. */
export function parseRequest(data: unknown): BridgeRequest | null {
  if (!isPlainObject(data)) return null;
  if (data.istota !== PROTOCOL) return null;
  const id = data.id;
  if (typeof id !== 'number' || !Number.isSafeInteger(id) || id < 0) return null;
  const args = data.args === undefined ? {} : data.args;
  if (!isPlainObject(args)) return null;
  if (data.kind === 'call') {
    if (!isName(data.capability) || !isName(data.op)) return null;
    return { istota: PROTOCOL, id, kind: 'call', capability: data.capability, op: data.op, args };
  }
  if (data.kind === 'shell') {
    if (data.op !== 'env' && data.op !== 'copy' && data.op !== 'error') return null;
    return { istota: PROTOCOL, id, kind: 'shell', op: data.op, args };
  }
  return null;
}

function clip(value: unknown): string {
  return typeof value === 'string' ? value.slice(0, REPORT_TEXT_MAX) : '';
}

export interface Bridge {
  /**
   * Hand the installed document its port. Call once, on the frame's first
   * `load`; a second call, or one after a navigation, does nothing.
   */
  connect(): void;
  /** Tell the frame the environment changed. */
  postEnv(env: AppEnv): void;
  /** Close the port and drop every reply still in flight. */
  dispose(): void;
}

/** The kit's key message, or null for anything else. */
function parseKey(data: unknown): string | null {
  if (!isPlainObject(data)) return null;
  if (data.istota !== PROTOCOL || data.kind !== 'key') return null;
  const k = data.k;
  if (typeof k !== 'string' || k.length === 0 || k.length > KEY_MAX) return null;
  return k;
}

export function createBridge(options: BridgeOptions): Bridge {
  let disposed = false;
  let port: MessagePort | null = null;
  let greeted = false;
  let frameKey: string | null = null;
  let helloDeadline: ReturnType<typeof setTimeout> | null = null;
  let reportsAtLoad = -1;
  let reportsLeft = ERROR_REPORTS_PER_LOAD;

  // Only the frame window's first message counts; whatever it was, the
  // listener goes, so nothing the frame sends later can set the key.
  function takeKey(event: MessageEvent) {
    const frameWindow = options.frameWindow();
    if (!frameWindow || event.source !== frameWindow) return;
    stopListening();
    if (disposed) return;
    frameKey = parseKey(event.data);
  }
  function stopListening() {
    window.removeEventListener('message', takeKey);
  }
  window.addEventListener('message', takeKey);

  function clearDeadline() {
    if (helloDeadline === null) return;
    clearTimeout(helloDeadline);
    helloDeadline = null;
  }

  function post(message: unknown, askedAt: number) {
    if (disposed || !port || !greeted) return;
    // The document that asked is still the one in the frame.
    if (options.navigations() !== askedAt) return;
    port.postMessage(message);
  }

  function reply(id: number, askedAt: number, result: unknown) {
    const message: BridgeReply = { istota: PROTOCOL, id, ok: true, result: result ?? null };
    post(message, askedAt);
  }

  function fail(id: number, askedAt: number, error: BridgeFailure) {
    const message: BridgeReply = { istota: PROTOCOL, id, ok: false, error };
    post(message, askedAt);
  }

  async function relayCall(request: CallRequest, askedAt: number) {
    if (!options.online()) {
      fail(request.id, askedAt, { code: 'offline', message: 'You are offline.' });
      return;
    }
    let outcome: CallOutcome;
    try {
      outcome = await options.call({
        capability: request.capability,
        op: request.op,
        args: request.args,
      });
    } catch (e) {
      options.onCallThrew?.(e);
      fail(request.id, askedAt, { code: 'internal', message: 'The call could not be made.' });
      return;
    }
    if (outcome.ok) {
      reply(request.id, askedAt, outcome.result);
      return;
    }
    options.onCallFailed?.(outcome.error.code);
    fail(request.id, askedAt, outcome.error);
  }

  async function answerShell(request: ShellRequest, askedAt: number) {
    if (request.op === 'env') {
      reply(request.id, askedAt, { ...options.env() });
      return;
    }
    if (request.op === 'copy') {
      const text = request.args.text;
      if (typeof text !== 'string' || text.length > COPY_MAX) {
        fail(request.id, askedAt, { code: 'bad_args', message: 'copy takes one string.' });
        return;
      }
      const copied = await options.copy(text).catch(() => false);
      if (copied) reply(request.id, askedAt, null);
      else fail(request.id, askedAt, { code: 'internal', message: 'Could not copy.' });
      return;
    }
    // An error report from the app's own `error` or `unhandledrejection`.
    const kind = request.args.kind === 'rejection' ? 'rejection' : 'error';
    if (reportsAtLoad !== askedAt) {
      reportsAtLoad = askedAt;
      reportsLeft = ERROR_REPORTS_PER_LOAD;
    }
    if (reportsLeft <= 0) {
      reply(request.id, askedAt, null);
      return;
    }
    reportsLeft -= 1;
    await options
      .report({ kind, message: clip(request.args.message), stack: clip(request.args.stack) })
      .catch(() => {});
    reply(request.id, askedAt, null);
  }

  function handle(data: unknown) {
    if (disposed) return;
    const askedAt = options.navigations();
    if (askedAt !== 0) return;
    if (!greeted) {
      // Only the installed document knows the key, so only its hello is taken.
      if (frameKey === null || !isPlainObject(data)) return;
      if (data.istota !== PROTOCOL || data.kind !== 'hello' || data.k !== frameKey) return;
      greeted = true;
      clearDeadline();
      postEnv(options.env());
      return;
    }
    const request = parseRequest(data);
    if (!request) return;
    if (request.kind === 'call') void relayCall(request, askedAt);
    else void answerShell(request, askedAt);
  }

  function postEnv(env: AppEnv) {
    post({ istota: PROTOCOL, kind: 'env', env: { ...env } }, 0);
  }

  return {
    connect() {
      if (disposed || port || options.navigations() !== 0) return;
      const target = options.frameWindow();
      if (!target) return;
      const channel = new MessageChannel();
      port = channel.port1;
      port.onmessage = (event) => handle(event.data);
      target.postMessage({ istota: PROTOCOL, kind: 'port' }, '*', [channel.port2]);
      // The installed document answers this with its hello; anything else in the
      // frame is a page that does not hold the key.
      helloDeadline = setTimeout(() => {
        helloDeadline = null;
        if (disposed || greeted) return;
        options.onUnresponsive?.();
      }, options.helloTimeoutMs ?? HELLO_TIMEOUT_MS);
    },
    postEnv,
    dispose() {
      disposed = true;
      stopListening();
      clearDeadline();
      port?.close();
    },
  };
}
