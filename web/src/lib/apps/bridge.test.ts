import { describe, expect, it, vi } from 'vitest';
import type { CallOutcome } from './api';
import { createBridge, ERROR_REPORTS_PER_LOAD, parseRequest, type BridgeOptions } from './bridge';
import type { AppEnv } from './env';

const ENV: AppEnv = {
  theme: 'dark',
  scale: 'medium',
  shell: 'web',
  visibility: 'visible',
  online: true,
  keyboard: false,
};

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
  function send(data: unknown, source: unknown = frameWindow) {
    bridge.handle({ data, source } as unknown as MessageEvent);
  }
  async function settle() {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  return { bridge, options, frameWindow, state, send, settle };
}

const LIST = { istota: 1, id: 7, kind: 'call', capability: 'kv:own', op: 'list', args: {} };

describe('the bridge', () => {
  it('drops a message from any window but the frame', async () => {
    const h = harness();
    h.send(LIST, { postMessage: vi.fn() });
    h.send(LIST, null);
    h.send(LIST, window);
    await h.settle();
    expect(h.options.call).not.toHaveBeenCalled();
    expect(h.frameWindow.postMessage).not.toHaveBeenCalled();
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
    expect(h.frameWindow.postMessage).not.toHaveBeenCalled();
  });

  it('routes a call to the server and posts its reply to the frame', async () => {
    const h = harness();
    h.send({ ...LIST, args: { prefix: 'card:' } });
    await h.settle();
    expect(h.options.call).toHaveBeenCalledWith({
      capability: 'kv:own',
      op: 'list',
      args: { prefix: 'card:' },
    });
    expect(h.frameWindow.postMessage).toHaveBeenCalledWith(
      { istota: 1, id: 7, ok: true, result: { items: [], total: 0 } },
      '*',
    );
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
    expect(h.frameWindow.postMessage).toHaveBeenCalledWith(
      { istota: 1, id: 7, ok: false, error: { code: 'not_granted', message: 'Not granted.' } },
      '*',
    );
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
    expect(h.frameWindow.postMessage).not.toHaveBeenCalled();
  });

  it('answers nothing once the frame has navigated', async () => {
    const h = harness();
    h.state.navigations = 1;
    h.send(LIST);
    h.send({ istota: 1, id: 2, kind: 'shell', op: 'env', args: {} });
    h.bridge.postEnv(ENV);
    await h.settle();
    expect(h.options.call).not.toHaveBeenCalled();
    expect(h.frameWindow.postMessage).not.toHaveBeenCalled();
  });

  it('fails a call made offline without a request', async () => {
    const h = harness();
    h.state.online = false;
    h.send(LIST);
    await h.settle();
    expect(h.options.call).not.toHaveBeenCalled();
    expect(h.frameWindow.postMessage).toHaveBeenCalledWith(
      { istota: 1, id: 7, ok: false, error: { code: 'offline', message: 'You are offline.' } },
      '*',
    );
  });

  it('answers the env shell call itself', async () => {
    const h = harness();
    h.send({ istota: 1, id: 3, kind: 'shell', op: 'env', args: {} });
    await h.settle();
    expect(h.options.call).not.toHaveBeenCalled();
    expect(h.frameWindow.postMessage).toHaveBeenCalledWith(
      { istota: 1, id: 3, ok: true, result: ENV },
      '*',
    );
  });

  it('answers copy through the clipboard helper, and refuses anything but a string', async () => {
    const h = harness();
    h.send({ istota: 1, id: 4, kind: 'shell', op: 'copy', args: { text: 'hello' } });
    h.send({ istota: 1, id: 5, kind: 'shell', op: 'copy', args: { text: { a: 1 } } });
    await h.settle();
    expect(h.options.copy).toHaveBeenCalledTimes(1);
    expect(h.options.copy).toHaveBeenCalledWith('hello');
    expect(h.options.call).not.toHaveBeenCalled();
    expect(h.frameWindow.postMessage).toHaveBeenCalledWith(
      { istota: 1, id: 4, ok: true, result: null },
      '*',
    );
    expect(h.frameWindow.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        id: 5,
        ok: false,
        error: expect.objectContaining({ code: 'bad_args' }),
      }),
      '*',
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

  it('sends env changes to the frame', () => {
    const h = harness();
    h.bridge.postEnv({ ...ENV, theme: 'light' });
    expect(h.frameWindow.postMessage).toHaveBeenCalledWith(
      { istota: 1, kind: 'env', env: { ...ENV, theme: 'light' } },
      '*',
    );
  });

  it('replies to nothing after dispose', async () => {
    const h = harness();
    h.bridge.dispose();
    h.send(LIST);
    await h.settle();
    expect(h.options.call).not.toHaveBeenCalled();
  });

  it('listens on the window it is attached to', async () => {
    const h = harness();
    const target = new EventTarget() as unknown as Window;
    const stop = h.bridge.attach(target);
    const event = new Event('message') as MessageEvent;
    Object.defineProperty(event, 'data', { value: LIST });
    Object.defineProperty(event, 'source', { value: h.frameWindow });
    target.dispatchEvent(event);
    await h.settle();
    expect(h.options.call).toHaveBeenCalledTimes(1);
    stop();
    target.dispatchEvent(event);
    await h.settle();
    expect(h.options.call).toHaveBeenCalledTimes(1);
  });
});

describe('parseRequest', () => {
  it('reads a call with no args as an empty object', () => {
    expect(
      parseRequest({ istota: 1, id: 1, kind: 'call', capability: 'kv:own', op: 'get' }),
    ).toEqual({ istota: 1, id: 1, kind: 'call', capability: 'kv:own', op: 'get', args: {} });
  });
});
