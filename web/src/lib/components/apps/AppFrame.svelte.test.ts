import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, waitFor } from '@testing-library/svelte';

const api = vi.hoisted(() => ({
  callApp: vi.fn(async () => ({ ok: true as const, result: 'v' })),
  reportAppError: vi.fn(async () => {}),
}));
vi.mock('$lib/apps/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/apps/api')>()),
  ...api,
}));
const clipboard = vi.hoisted(() => ({ copyText: vi.fn(async () => true) }));
vi.mock('@minot/ui/clipboard', () => clipboard);

const { default: AppFrame } = await import('./AppFrame.svelte');

beforeEach(() => vi.clearAllMocks());
afterEach(cleanup);

function frameOf(container: HTMLElement): HTMLIFrameElement {
  const frame = container.querySelector('iframe');
  if (!frame) throw new Error('no frame');
  return frame;
}

/** Load the frame's document and take the port the shell hands it, as the kit would. */
function connect(frame: HTMLIFrameElement): MessagePort {
  const handoff = vi.spyOn(frame.contentWindow!, 'postMessage');
  frame.dispatchEvent(new Event('load'));
  const call = handoff.mock.calls.at(-1);
  if (!call) throw new Error('no port was handed to the frame');
  expect(call[0]).toEqual({ istota: 1, kind: 'port' });
  expect(call[1]).toBe('*');
  return ((call as unknown[])[2] as MessagePort[])[0];
}

const KEY = 'key-from-the-kit';

/** The kit's key, posted to the shell's window while the frame's document is parsed. */
function postKey(frame: HTMLIFrameElement, k = KEY) {
  window.dispatchEvent(
    new MessageEvent('message', {
      data: { istota: 1, kind: 'key', k },
      source: frame.contentWindow,
    }),
  );
}

function fromFrame(frame: HTMLIFrameElement, data: unknown) {
  postKey(frame);
  const port = connect(frame);
  port.postMessage({ istota: 1, kind: 'hello', k: KEY });
  port.postMessage(data);
}

describe('AppFrame', () => {
  it('sandboxes the frame with allow-scripts and nothing else', () => {
    const { container } = render(AppFrame, { appId: 'board', version: 2, name: 'Board' });
    const frame = frameOf(container);
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts');
    expect(frame.hasAttribute('data-istota-app-frame')).toBe(true);
    expect(frame.getAttribute('title')).toBe('Board');
  });

  it('loads the current version with the starting environment in the fragment', () => {
    const { container } = render(AppFrame, { appId: 'board', version: 2, name: 'Board' });
    const url = new URL(frameOf(container).src);
    expect(url.pathname).toBe('/api/apps/board/v/2/frame');
    expect(url.search).toBe('');
    const env = new URLSearchParams(url.hash.slice(1));
    expect(env.get('theme')).toMatch(/^(dark|light)$/);
    expect(env.get('scale')).toMatch(/^(small|medium|large)$/);
    expect(env.get('shell')).toBe('web');
    expect(env.get('v')).toBe('1');
    // The bridge's key is the kit's own and never rides in the URL.
    expect([...env.keys()].sort()).toEqual(['scale', 'shell', 'theme', 'v']);
  });

  it("listens for the kit's key before the frame is given its src", () => {
    const order: string[] = [];
    const add = window.addEventListener.bind(window);
    vi.spyOn(window, 'addEventListener').mockImplementation((type, fn, opts) => {
      if (type === 'message') order.push('listen');
      add(type, fn, opts);
    });
    const setSrc = vi.spyOn(HTMLIFrameElement.prototype, 'src', 'set');
    setSrc.mockImplementation(function (this: HTMLIFrameElement, value: string) {
      order.push('src');
      this.setAttribute('src', value);
    });
    render(AppFrame, { appId: 'board', version: 2, name: 'Board' });
    vi.restoreAllMocks();
    expect(order).toEqual(['listen', 'src']);
  });

  it('refuses a hello whose key the frame never posted, and removes the frame', () => {
    vi.useFakeTimers();
    const onNavigated = vi.fn();
    const { container } = render(AppFrame, {
      appId: 'board',
      version: 2,
      name: 'Board',
      onNavigated,
    });
    const frame = frameOf(container);
    // A page that navigated the frame early: the port is its, the key is not.
    const port = connect(frame);
    port.postMessage({ istota: 1, kind: 'hello', k: KEY });
    port.postMessage({ istota: 1, id: 1, kind: 'call', capability: 'kv:own', op: 'get', args: {} });
    vi.advanceTimersByTime(4000);
    expect(api.callApp).not.toHaveBeenCalled();
    expect(onNavigated).toHaveBeenCalledTimes(1);
    expect(container.querySelector('iframe')).toBeNull();
    vi.useRealTimers();
  });

  it('takes only the first key the frame posts', async () => {
    const { container } = render(AppFrame, { appId: 'board', version: 2, name: 'Board' });
    const frame = frameOf(container);
    postKey(frame);
    postKey(frame, 'forwarded');
    const port = connect(frame);
    port.postMessage({ istota: 1, kind: 'hello', k: 'forwarded' });
    port.postMessage({ istota: 1, id: 1, kind: 'call', capability: 'kv:own', op: 'get', args: {} });
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(api.callApp).not.toHaveBeenCalled();
  });

  it('relays a call from its own frame to this app and version', async () => {
    const { container } = render(AppFrame, { appId: 'board', version: 2, name: 'Board' });
    const frame = frameOf(container);
    fromFrame(frame, {
      istota: 1,
      id: 1,
      kind: 'call',
      capability: 'kv:own',
      op: 'get',
      args: { key: 'k' },
    });
    await waitFor(() =>
      expect(api.callApp).toHaveBeenCalledWith('board', 2, {
        capability: 'kv:own',
        op: 'get',
        args: { key: 'k' },
      }),
    );
  });

  it('hands the port only on the first load', () => {
    const { container } = render(AppFrame, { appId: 'board', version: 2, name: 'Board' });
    const frame = frameOf(container);
    const handoff = vi.spyOn(frame.contentWindow!, 'postMessage');
    expect(handoff).not.toHaveBeenCalled();
    frame.dispatchEvent(new Event('load'));
    expect(handoff).toHaveBeenCalledTimes(1);
  });

  it('takes no call posted on the window, even from its own frame, once the port exists', async () => {
    const { container } = render(AppFrame, { appId: 'board', version: 2, name: 'Board' });
    const frame = frameOf(container);
    const port = connect(frame);
    const replies: unknown[] = [];
    port.onmessage = (event) => replies.push(event.data);
    const handoff = vi.spyOn(frame.contentWindow!, 'postMessage');
    window.dispatchEvent(
      new MessageEvent('message', {
        data: { istota: 1, id: 1, kind: 'call', capability: 'kv:own', op: 'get', args: {} },
        source: frame.contentWindow,
      }),
    );
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(api.callApp).not.toHaveBeenCalled();
    expect(handoff).not.toHaveBeenCalled();
    expect(replies.filter((m) => (m as { kind?: string }).kind !== 'env')).toEqual([]);
  });

  it('ignores the same message from another window', async () => {
    render(AppFrame, { appId: 'board', version: 2, name: 'Board' });
    window.dispatchEvent(
      new MessageEvent('message', {
        data: { istota: 1, id: 1, kind: 'call', capability: 'kv:own', op: 'get', args: {} },
        source: window,
      }),
    );
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(api.callApp).not.toHaveBeenCalled();
  });

  it('copies through the shared clipboard helper', async () => {
    const { container } = render(AppFrame, { appId: 'board', version: 2, name: 'Board' });
    fromFrame(frameOf(container), {
      istota: 1,
      id: 2,
      kind: 'shell',
      op: 'copy',
      args: { text: 'hello' },
    });
    await waitFor(() => expect(clipboard.copyText).toHaveBeenCalledWith('hello'));
  });

  it('removes a frame whose document never answers the hello, and records it', () => {
    vi.useFakeTimers();
    const onNavigated = vi.fn();
    const { container } = render(AppFrame, {
      appId: 'board',
      version: 2,
      name: 'Board',
      onNavigated,
    });
    const frame = frameOf(container);
    frame.dispatchEvent(new Event('load'));
    vi.advanceTimersByTime(4000);
    expect(onNavigated).toHaveBeenCalledTimes(1);
    expect(container.querySelector('iframe')).toBeNull();
    expect(api.reportAppError).toHaveBeenCalledWith('board', 2, {
      kind: 'navigated',
      message: expect.any(String),
    });
    vi.useRealTimers();
  });

  it('removes a frame that navigates itself and records it', async () => {
    const onNavigated = vi.fn();
    const { container } = render(AppFrame, {
      appId: 'board',
      version: 2,
      name: 'Board',
      onNavigated,
    });
    const frame = frameOf(container);
    frame.dispatchEvent(new Event('load'));
    expect(onNavigated).not.toHaveBeenCalled();
    frame.dispatchEvent(new Event('load'));
    expect(onNavigated).toHaveBeenCalledTimes(1);
    expect(container.querySelector('iframe')).toBeNull();
    expect(api.reportAppError).toHaveBeenCalledWith('board', 2, {
      kind: 'navigated',
      message: expect.any(String),
    });
  });
});
