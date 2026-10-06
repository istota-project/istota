import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync } from 'svelte';
import { page } from '../../../vitest-stubs/app-state.svelte';
import * as navigation from '../../../vitest-stubs/app-navigation';
import { createUrlSelection, type UrlSelectionSpec } from './urlSelection.svelte';

type Selection = { room: string; task?: number };
const disposers: (() => void)[] = [];

function setup(
  options: Partial<UrlSelectionSpec<Selection>> = {},
  initial: Selection | null = { room: 'A' },
) {
  let selected = $state<Selection | null>(initial);
  const apply = vi.fn((sel: Selection) => {
    selected = { room: sel.room };
  });
  const selection = createUrlSelection<Selection>({
    key: 'chat',
    params: ['room', 'task'],
    encode: (sel) => ({ room: sel.room, ...(sel.task ? { task: String(sel.task) } : {}) }),
    decode: (params) =>
      params.room
        ? { room: params.room, ...(params.task ? { task: Number(params.task) } : {}) }
        : null,
    read: () => selected,
    apply,
    ...options,
  });
  return {
    selection,
    apply,
    get selected() {
      return selected;
    },
    set selected(value: Selection | null) {
      selected = value;
    },
    start() {
      disposers.push($effect.root(() => selection.start()));
      flushSync();
    },
  };
}

beforeEach(() => {
  navigation.__history.reset('/istota/chat/?room=A');
  vi.spyOn(navigation, 'pushState');
  vi.spyOn(navigation, 'replaceState');
  vi.spyOn(navigation, 'goto');
});

afterEach(() => {
  for (const dispose of disposers.splice(0)) dispose();
  vi.restoreAllMocks();
});

describe('URL selection', () => {
  it('prefers shallow page.state over the unchanged load URL and falls back on reload', () => {
    const { selection } = setup();
    page.state = { chat: { room: 'B' } };
    expect(page.url.search).toBe('?room=A');
    expect(selection.current()).toEqual({ room: 'B' });
    page.state = {};
    expect(selection.current()).toEqual({ room: 'A' });
  });

  it('applies before pushing, preserves other params, hash and state, and does not navigate', () => {
    navigation.__history.reset('/istota/chat/?room=A&google=x#message', { modal: { open: true } });
    const h = setup();
    const afterNavigation = vi.fn();
    navigation.afterNavigate(afterNavigation);
    h.start();
    h.selection.push({ room: 'B' });
    flushSync();
    expect(h.selected).toEqual({ room: 'B' });
    expect(h.apply.mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(navigation.pushState).mock.invocationCallOrder[0],
    );
    expect(navigation.pushState).toHaveBeenCalledExactlyOnceWith(
      '/istota/chat/?google=x&room=B#message',
      { modal: { open: true }, chat: { room: 'B' } },
    );
    expect(navigation.replaceState).not.toHaveBeenCalled();
    expect(page.url.search).toBe('?room=A&google=x');
    expect(afterNavigation).not.toHaveBeenCalled();
    expect(navigation.goto).not.toHaveBeenCalled();
  });

  it('does nothing on a second click of the current selection', () => {
    const h = setup();
    h.start();
    h.selection.push({ room: 'A' });
    flushSync();
    expect(h.apply).not.toHaveBeenCalled();
    expect(navigation.pushState).not.toHaveBeenCalled();
    expect(navigation.replaceState).not.toHaveBeenCalled();
  });

  it('replaces a store-driven change without applying or pushing', () => {
    const h = setup();
    h.start();
    h.selected = { room: 'B' };
    flushSync();
    expect(navigation.replaceState).toHaveBeenCalledExactlyOnceWith('/istota/chat/?room=B', {
      chat: { room: 'B' },
    });
    expect(navigation.pushState).not.toHaveBeenCalled();
    expect(h.apply).not.toHaveBeenCalled();
  });

  it('replaces the bare URL once the default is ready', () => {
    navigation.__history.reset('/istota/chat/');
    const h = setup({}, null);
    h.start();
    expect(navigation.replaceState).not.toHaveBeenCalled();
    h.selected = { room: 'A' };
    flushSync();
    expect(navigation.replaceState).toHaveBeenCalledTimes(1);
    expect(navigation.__history.entries).toHaveLength(1);
  });

  it('restores Back and Forward once, without another history write', () => {
    const h = setup();
    h.start();
    h.selection.push({ room: 'B' });
    flushSync();
    vi.clearAllMocks();
    navigation.__history.back();
    flushSync();
    expect(h.apply).toHaveBeenCalledExactlyOnceWith({ room: 'A' });
    expect(navigation.pushState).not.toHaveBeenCalled();
    expect(navigation.replaceState).not.toHaveBeenCalled();
    expect(page.url.search).toBe('?room=A');
    vi.clearAllMocks();
    navigation.__history.forward();
    flushSync();
    expect(h.apply).toHaveBeenCalledExactlyOnceWith({ room: 'B' });
    expect(navigation.pushState).not.toHaveBeenCalled();
    expect(navigation.replaceState).not.toHaveBeenCalled();
  });

  it('repairs an invalid Back entry even when the store did not change', () => {
    const valid = new Set(['A', 'B']);
    const h = setup({
      decode: (params) => (valid.has(params.room) ? { room: params.room } : null),
    });
    h.start();
    h.selection.push({ room: 'B' });
    flushSync();
    valid.delete('A');
    vi.clearAllMocks();
    navigation.__history.back();
    flushSync();
    expect(h.apply).not.toHaveBeenCalled();
    expect(navigation.replaceState).toHaveBeenCalledExactlyOnceWith('/istota/chat/?room=B', {
      chat: { room: 'B' },
    });
    expect(navigation.pushState).not.toHaveBeenCalled();
  });

  it('preserves jump params during reconcile and replays same-room jumps on Forward', () => {
    const h = setup({ compareKeys: ['room'] });
    h.start();
    h.selection.push({ room: 'A', task: 42 });
    flushSync();
    expect(navigation.pushState).toHaveBeenCalledTimes(1);
    expect(navigation.__history.entries[1].url).toContain('task=42');
    expect(navigation.replaceState).not.toHaveBeenCalled();
    navigation.__history.back();
    flushSync();
    vi.clearAllMocks();
    navigation.__history.forward();
    flushSync();
    expect(h.apply).toHaveBeenCalledExactlyOnceWith({ room: 'A', task: 42 });
    expect(navigation.replaceState).not.toHaveBeenCalled();
    expect(navigation.pushState).not.toHaveBeenCalled();
  });

  it('applies without writing before start', () => {
    const h = setup();
    h.selection.push({ room: 'B' });
    expect(h.apply).toHaveBeenCalledExactlyOnceWith({ room: 'B' });
    expect(navigation.pushState).not.toHaveBeenCalled();
  });

  it('logs rejected applies and allows the next push', async () => {
    const error = new Error('load failed');
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const apply = vi.fn().mockRejectedValueOnce(error).mockResolvedValue(undefined);
    const h = setup({ apply });
    h.start();
    h.selection.push({ room: 'B' });
    await Promise.resolve();
    h.selection.push({ room: 'C' });
    expect(warn).toHaveBeenCalledWith('urlSelection', 'chat', error);
    expect(apply).toHaveBeenCalledTimes(2);
    expect(navigation.pushState).toHaveBeenCalledTimes(2);
  });

  it('keeps the load URL available while the page waits for its list', () => {
    const h = setup({}, null);
    h.start();
    expect(h.selection.current()).toEqual({ room: 'A' });
    expect(navigation.replaceState).not.toHaveBeenCalled();
    h.selected = h.selection.current();
    flushSync();
    expect(navigation.replaceState).not.toHaveBeenCalled();
  });

  it('logs a synchronous apply failure without throwing', () => {
    const error = new Error('apply failed');
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const h = setup({
      apply: () => {
        throw error;
      },
    });
    h.start();
    expect(() => h.selection.push({ room: 'B' })).not.toThrow();
    expect(warn).toHaveBeenCalledWith('urlSelection', 'chat', error);
  });

  it('logs a failed reconcile write without throwing', () => {
    const error = new Error('history unavailable');
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.mocked(navigation.replaceState).mockImplementationOnce(() => {
      throw error;
    });
    const h = setup();
    h.start();
    h.selected = { room: 'B' };
    expect(() => flushSync()).not.toThrow();
    expect(warn).toHaveBeenCalledWith('urlSelection', 'chat', error);
  });

  it('keeps the store change if a history write fails', () => {
    const error = new Error('router not initialized');
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.mocked(navigation.pushState).mockImplementationOnce(() => {
      throw error;
    });
    const h = setup();
    h.start();
    expect(() => h.selection.push({ room: 'B' })).not.toThrow();
    expect(h.selected).toEqual({ room: 'B' });
    expect(warn).toHaveBeenCalledWith('urlSelection', 'chat', error);
  });
});
