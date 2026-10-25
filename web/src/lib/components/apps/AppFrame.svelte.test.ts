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

function fromFrame(frame: HTMLIFrameElement, data: unknown) {
  window.dispatchEvent(new MessageEvent('message', { data, source: frame.contentWindow }));
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
