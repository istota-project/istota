import { afterEach, describe, expect, it, vi } from 'vitest';
import type { CallOutcome } from './api';
import {
  createBridge,
  ERROR_REPORTS_PER_LOAD,
  KEY_MAX,
  parseRequest,
  type Bridge,
  type BridgeOptions,
} from './bridge';
import type { AppEnv } from './env';

const ENV: AppEnv = {
  theme: 'dark',
  scale: 'medium',
  shell: 'web',
  visibility: 'visible',
  online: true,
  keyboard: false,
};

const KEY = 'key-from-the-kit';

const bridges: Bridge[] = [];
afterEach(() => {
  for (const bridge of bridges.splice(0)) bridge.dispose();
});

/** A message posted to the shell's window, as the frame's `window.parent.postMessage` lands. */
function onWindow(data: unknown, source: unknown) {
  window.dispatchEvent(new MessageEvent('message', { data, source: source as Window }));
}

function harness(overrides: Partial<BridgeOptions> = {}) {
  const frameWindow = { postMessage: vi.fn() };
  const state = { navigations: 0, online: true };
  const options: BridgeOptions = {
    frameWindow: () => frameWindow as unknown as Window,
    navigations: () => state.navigations,
    online: () => state.online,
    env: () => ENV,
    call: vi.fn(async (): Promise<CallOutcome> => ({ ok: true, result: { items: [], total: 0 } })),
    copy: vi.fn(async () => true),
    report: vi.fn(async () => {}),
    ...overrides,
  };
  const bridge = createBridge(options);
  bridges.push(bridge);
  let keyed = false;
  /** The kit's key, the first message the frame's window posts to the shell. */
  function key(data: unknown = { istota: 1, kind: 'key', k: KEY }, source: unknown = frameWindow) {
    if (source === frameWindow) keyed = true;
    onWindow(data, source);
  }
  // The frame's end of the channel, as the kit would hold it.
  let kitPort: MessagePort | null = null;
  const received: unknown[] = [];
  /** The shell's handoff; `greet` posts the hello the kit sends back, as `connect` would. */
  function connect({ greet = true } = {}) {
    bridge.connect();
    const handoff = frameWindow.postMessage.mock.calls.at(-1);
    if (!handoff) throw new Error('no port was posted');
    kitPort = (handoff[2] as MessagePort[])[0];
    kitPort.onmessage = (event) => received.push(event.data);
    if (greet) hello();
    return handoff;
  }
  /** The kit's first port message, echoing the key it posted; posts that key first if nothing has. */
  function hello(k = KEY) {
    if (!keyed) key();
    if (!kitPort) connect({ greet: false });
    kitPort!.postMessage({ istota: 1, kind: 'hello', k });
  }
  function send(data: unknown) {
    if (!kitPort) connect();
    kitPort!.postMessage(data);
  }
  async function settle() {
    for (let i = 0; i < 3; i++) await new Promise((resolve) => setTimeout(resolve, 0));
  }
  /** What reached the kit over the port, less the env sent on connect. */
  function replies() {
    return received.filter((m) => (m as { kind?: string }).kind !== 'env');
  }
  return {
    bridge,
    options,
    frameWindow,
    state,
    key,
    connect,
    hello,
    send,
    settle,
    received,
    replies,
  };
}

const LIST = { istota: 1, id: 7, kind: 'call', capability: 'kv:own', op: 'list', args: {} };

describe('the bridge', () => {
  it('hands the frame one port, with the wildcard target an opaque origin needs', () => {
    const h = harness();
    const [message, target, transfer] = h.connect();
    expect(message).toEqual({ istota: 1, kind: 'port' });
    expect(target).toBe('*');
    expect(transfer).toHaveLength(1);
    h.bridge.connect();
    expect(h.frameWindow.postMessage).toHaveBeenCalledTimes(1);
  });

  it('hands no port to a frame that has navigated', () => {
    const h = harness();
    h.state.navigations = 1;
    h.bridge.connect();
    expect(h.frameWindow.postMessage).not.toHaveBeenCalled();
  });

  it('sends the current environment over the port once the frame says hello', async () => {
    const h = harness();
    h.connect();
    await h.settle();
    expect(h.received).toEqual([{ istota: 1, kind: 'env', env: ENV }]);
  });

  it('sends nothing on the port until the hello arrives', async () => {
    const h = harness();
    h.connect({ greet: false });
    await h.settle();
    expect(h.received).toEqual([]);
    h.hello();
    await h.settle();
    expect(h.received).toEqual([{ istota: 1, kind: 'env', env: ENV }]);
  });

  it('drops a call that arrives before the hello, and answers one after', async () => {
    const h = harness();
    h.connect({ greet: false });
    h.send(LIST);
    await h.settle();
    expect(h.options.call).not.toHaveBeenCalled();
    expect(h.replies()).toEqual([]);
    h.hello();
    h.send({ ...LIST, id: 8 });
    await h.settle();
    expect(h.options.call).toHaveBeenCalledTimes(1);
    expect(h.replies()).toEqual([{ istota: 1, id: 8, ok: true, result: { items: [], total: 0 } }]);
  });

  it('refuses a hello with the wrong key, and takes the right one after it', async () => {
    const h = harness();
    h.connect({ greet: false });
    h.hello('guessed');
    h.send(LIST);
    await h.settle();
    expect(h.options.call).not.toHaveBeenCalled();
    expect(h.replies()).toEqual([]);
    h.hello();
    h.send({ ...LIST, id: 9 });
    await h.settle();
    expect(h.options.call).toHaveBeenCalledTimes(1);
  });

  it('ignores a later hello once it is greeted', async () => {
    const h = harness();
    h.connect();
    h.hello('whatever');
    await h.settle();
    // One env, at the greeting; a second hello neither re-greets nor re-sends it.
    expect(h.received).toEqual([{ istota: 1, kind: 'env', env: ENV }]);
  });

  it('calls onUnresponsive when a document never says hello', async () => {
    const onUnresponsive = vi.fn();
    const h = harness({ onUnresponsive, helloTimeoutMs: 20 });
    h.connect({ greet: false });
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(onUnresponsive).toHaveBeenCalledTimes(1);
  });

  it('stays quiet when the hello arrives in time', async () => {
    const onUnresponsive = vi.fn();
    const h = harness({ onUnresponsive, helloTimeoutMs: 20 });
    h.connect();
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(onUnresponsive).not.toHaveBeenCalled();
  });

  it('stops the hello deadline once disposed', async () => {
    const onUnresponsive = vi.fn();
    const h = harness({ onUnresponsive, helloTimeoutMs: 20 });
    h.connect({ greet: false });
    h.bridge.dispose();
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(onUnresponsive).not.toHaveBeenCalled();
  });

  it('takes nothing posted on the window, even from the frame, once the port exists', async () => {
    const h = harness();
    h.connect();
    window.dispatchEvent(
      new MessageEvent('message', { data: LIST, source: h.frameWindow as unknown as Window }),
    );
    await h.settle();
    expect(h.options.call).not.toHaveBeenCalled();
    expect(h.replies()).toEqual([]);
    expect(h.frameWindow.postMessage).toHaveBeenCalledTimes(1);
  });

  it('drops malformed messages without a reply', async () => {
    const h = harness();
    const bad: unknown[] = [
      null,
      'kv:own',
      [LIST],
      { ...LIST, istota: 2 },
      { ...LIST, id: '7' },
      { ...LIST, id: 1.5 },
      { ...LIST, kind: 'exec' },
      { ...LIST, capability: '' },
      { ...LIST, capability: 7 },
      { ...LIST, op: 'x'.repeat(201) },
      { ...LIST, args: [1, 2] },
      { ...LIST, args: 'all' },
      { istota: 1, id: 1, kind: 'shell', op: 'run', args: {} },
    ];
    for (const data of bad) h.send(data);
    await h.settle();
    expect(h.options.call).not.toHaveBeenCalled();
    expect(h.replies()).toEqual([]);
  });

  it('routes a call to the server and replies on the port only', async () => {
    const h = harness();
    h.send({ ...LIST, args: { prefix: 'card:' } });
    await h.settle();
    expect(h.options.call).toHaveBeenCalledWith({
      capability: 'kv:own',
      op: 'list',
      args: { prefix: 'card:' },
    });
    expect(h.replies()).toEqual([{ istota: 1, id: 7, ok: true, result: { items: [], total: 0 } }]);
    // The port handoff is the only thing ever posted to the frame's window.
    expect(h.frameWindow.postMessage).toHaveBeenCalledTimes(1);
  });

  it('passes a refusal through with its code and tells the page', async () => {
    const onCallFailed = vi.fn();
    const h = harness({
      call: vi.fn(async (): Promise<CallOutcome> => ({
        ok: false,
        error: { code: 'not_granted', message: 'Not granted.' },
      })),
      onCallFailed,
    });
    h.send(LIST);
    await h.settle();
    expect(h.replies()).toEqual([
      { istota: 1, id: 7, ok: false, error: { code: 'not_granted', message: 'Not granted.' } },
    ]);
    expect(onCallFailed).toHaveBeenCalledWith('not_granted');
  });

  it('drops the reply when the frame navigated while the call was out', async () => {
    let answer!: (outcome: CallOutcome) => void;
    const h = harness({
      call: vi.fn(() => new Promise<CallOutcome>((resolve) => (answer = resolve))),
    });
    h.send(LIST);
    await h.settle();
    h.state.navigations = 1;
    answer({ ok: true, result: 'secret' });
    await h.settle();
    expect(h.replies()).toEqual([]);
  });

  it('answers nothing once the frame has navigated', async () => {
    const h = harness();
    h.connect();
    await h.settle();
    h.state.navigations = 1;
    h.send(LIST);
    h.send({ istota: 1, id: 2, kind: 'shell', op: 'env', args: {} });
    h.bridge.postEnv(ENV);
    await h.settle();
    expect(h.options.call).not.toHaveBeenCalled();
    expect(h.received).toHaveLength(1);
  });

  it('fails a call made offline without a request', async () => {
    const h = harness();
    h.state.online = false;
    h.send(LIST);
    await h.settle();
    expect(h.options.call).not.toHaveBeenCalled();
    expect(h.replies()).toEqual([
      { istota: 1, id: 7, ok: false, error: { code: 'offline', message: 'You are offline.' } },
    ]);
  });

  it('answers the env shell call itself', async () => {
    const h = harness();
    h.send({ istota: 1, id: 3, kind: 'shell', op: 'env', args: {} });
    await h.settle();
    expect(h.options.call).not.toHaveBeenCalled();
    expect(h.replies()).toEqual([{ istota: 1, id: 3, ok: true, result: ENV }]);
  });

  it('answers copy through the clipboard helper, and refuses anything but a string', async () => {
    const h = harness();
    h.send({ istota: 1, id: 4, kind: 'shell', op: 'copy', args: { text: 'hello' } });
    h.send({ istota: 1, id: 5, kind: 'shell', op: 'copy', args: { text: { a: 1 } } });
    await h.settle();
    expect(h.options.copy).toHaveBeenCalledTimes(1);
    expect(h.options.copy).toHaveBeenCalledWith('hello');
    expect(h.options.call).not.toHaveBeenCalled();
    expect(h.replies()).toContainEqual({ istota: 1, id: 4, ok: true, result: null });
    expect(h.replies()).toContainEqual(
      expect.objectContaining({
        id: 5,
        ok: false,
        error: expect.objectContaining({ code: 'bad_args' }),
      }),
    );
  });

  it('forwards error reports, clipped, and stops after the per-load budget', async () => {
    const h = harness();
    const long = 'x'.repeat(10_000);
    h.send({
      istota: 1,
      id: 1,
      kind: 'shell',
      op: 'error',
      args: { kind: 'rejection', message: long, stack: 'at app.js:1' },
    });
    await h.settle();
    expect(h.options.report).toHaveBeenCalledWith({
      kind: 'rejection',
      message: 'x'.repeat(4096),
      stack: 'at app.js:1',
    });
    for (let id = 2; id <= ERROR_REPORTS_PER_LOAD + 5; id++) {
      h.send({
        istota: 1,
        id,
        kind: 'shell',
        op: 'error',
        args: { kind: 'navigated', message: 'm' },
      });
    }
    await h.settle();
    expect(h.options.report).toHaveBeenCalledTimes(ERROR_REPORTS_PER_LOAD);
    // The app cannot report on the shell's behalf: anything but a rejection is an error.
    expect(h.options.report).toHaveBeenLastCalledWith(expect.objectContaining({ kind: 'error' }));
  });

  it('sends env changes over the port', async () => {
    const h = harness();
    h.connect();
    await h.settle();
    h.bridge.postEnv({ ...ENV, theme: 'light' });
    await h.settle();
    expect(h.received).toEqual([
      { istota: 1, kind: 'env', env: ENV },
      { istota: 1, kind: 'env', env: { ...ENV, theme: 'light' } },
    ]);
    expect(h.frameWindow.postMessage).toHaveBeenCalledTimes(1);
  });

  it('sends nothing before the port exists', () => {
    const h = harness();
    h.bridge.postEnv(ENV);
    expect(h.frameWindow.postMessage).not.toHaveBeenCalled();
  });

  it('replies to nothing after dispose', async () => {
    const h = harness();
    h.connect();
    h.bridge.dispose();
    h.send(LIST);
    await h.settle();
    expect(h.options.call).not.toHaveBeenCalled();
  });
});

describe('the key', () => {
  /** True when a call sent after `hello(k)` was relayed. */
  async function accepted(h: ReturnType<typeof harness>, k = KEY) {
    h.connect({ greet: false });
    h.hello(k);
    h.send(LIST);
    await h.settle();
    return (h.options.call as ReturnType<typeof vi.fn>).mock.calls.length > 0;
  }

  it('takes a hello carrying the key from the first frame-window message', async () => {
    const h = harness();
    h.key();
    expect(await accepted(h)).toBe(true);
  });

  it('refuses every hello when no key message came at all', async () => {
    const h = harness();
    h.connect({ greet: false });
    for (const k of [KEY, '', 'undefined']) {
      h.send({ istota: 1, kind: 'hello', k });
    }
    h.send(LIST);
    await h.settle();
    expect(h.options.call).not.toHaveBeenCalled();
    expect(h.received).toEqual([]);
  });

  it('refuses the hello when the first frame-window message was not a key', async () => {
    const h = harness();
    h.key({ istota: 1, kind: 'hello', k: KEY });
    h.key();
    expect(await accepted(h)).toBe(false);
  });

  it.each([
    ['an empty key', { istota: 1, kind: 'key', k: '' }],
    ['a key that is not a string', { istota: 1, kind: 'key', k: 7 }],
    ['an overlong key', { istota: 1, kind: 'key', k: 'k'.repeat(KEY_MAX + 1) }],
    ['another protocol', { istota: 2, kind: 'key', k: KEY }],
  ])('takes no key from %s, and none after it', async (_label, data) => {
    const h = harness();
    h.key(data);
    h.key();
    const k = typeof (data as { k: unknown }).k === 'string' ? (data as { k: string }).k : KEY;
    expect(await accepted(h, k)).toBe(false);
  });

  it('ignores a second key message from the frame', async () => {
    const h = harness();
    h.key();
    h.key({ istota: 1, kind: 'key', k: 'forwarded-by-app-code' });
    expect(await accepted(h, 'forwarded-by-app-code')).toBe(false);
    h.send({ istota: 1, kind: 'hello', k: KEY });
    h.send({ ...LIST, id: 8 });
    await h.settle();
    expect(h.options.call).toHaveBeenCalledTimes(1);
  });

  it('takes no key from another window, and still takes the frame’s after it', async () => {
    const h = harness();
    const stranger = { postMessage: vi.fn() };
    h.key({ istota: 1, kind: 'key', k: 'stranger' }, stranger);
    h.key({ istota: 1, kind: 'key', k: 'parent' }, window);
    h.connect({ greet: false });
    h.send({ istota: 1, kind: 'hello', k: 'stranger' });
    h.send({ istota: 1, kind: 'hello', k: 'parent' });
    h.send(LIST);
    await h.settle();
    expect(h.options.call).not.toHaveBeenCalled();
    h.key();
    h.hello();
    h.send({ ...LIST, id: 8 });
    await h.settle();
    expect(h.options.call).toHaveBeenCalledTimes(1);
  });

  it('removes a frame that never posted a key, on the hello deadline', async () => {
    const onUnresponsive = vi.fn();
    const h = harness({ onUnresponsive, helloTimeoutMs: 20 });
    h.connect({ greet: false });
    h.send({ istota: 1, kind: 'hello', k: KEY });
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(onUnresponsive).toHaveBeenCalledTimes(1);
  });
});

describe('parseRequest', () => {
  it('reads a call with no args as an empty object', () => {
    expect(
      parseRequest({ istota: 1, id: 1, kind: 'call', capability: 'kv:own', op: 'get' }),
    ).toEqual({ istota: 1, id: 1, kind: 'call', capability: 'kv:own', op: 'get', args: {} });
  });
});
