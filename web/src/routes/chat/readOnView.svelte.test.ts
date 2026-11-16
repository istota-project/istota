/**
 * Read on view, the page half: which rows of an aggregate pane count as seen,
 * and when.
 *
 * A row counts once half of it, or half the scroller, has been on screen for
 * 500 ms with the tab visible; the store decides what that does. jsdom has no
 * IntersectionObserver, so the tests stub one and hand it entries, and run the
 * dwell on fake timers.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';

vi.mock('$lib/stores/chat', async () => {
  const { writable } = await import('svelte/store');
  const stores: Record<string, unknown> = {
    rooms: writable([]),
    activeRoomId: writable(null),
    messages: writable([]),
    status: writable('idle'),
    loaded: writable(true),
    hasMore: writable(false),
    loadingOlder: writable(false),
    view: writable('unread'),
    scrollTarget: writable(null),
    sendSettled: writable({ n: 0, token: null }),
    sendReturned: writable({ n: 0, token: null, text: '', attachments: [] }),
    outboundDrafts: writable([]),
    externalTurnDisplay: writable('full'),
    readOnView: writable(true),
    offlineTranscript: writable(false),
    queuedCounts: writable({}),
  };
  const session = new Proxy(stores, {
    get: (target, key: string) => (target[key] ??= vi.fn(async () => undefined)),
  });
  return { getChatSession: () => session };
});

import { getChatSession } from '$lib/stores/chat';
import Page from './+page.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';
import type { User } from '$lib/api';

const person: User = {
  username: 'alice',
  display_name: 'Alice',
  bot_name: 'Istota',
  is_admin: false,
  features: {
    chat: true,
    feeds: false,
    location: false,
    money: false,
    health: false,
    briefings: false,
    google_workspace: false,
    google_workspace_enabled: false,
    admin: false,
  },
};

type Stores = Record<string, { set: (v: unknown) => void }>;
const session = () => getChatSession() as unknown as Stores;
const spy = (name: string) =>
  (getChatSession() as unknown as Record<string, ReturnType<typeof vi.fn>>)[name];

function room(id: number) {
  return {
    id,
    token: `t${id}`,
    name: `Room ${id}`,
    archived: false,
    created_at: '',
    updated_at: '',
    origin: 'web',
    unread_count: 2,
  };
}

function row(cid: number, unread = true) {
  return {
    cid,
    msgId: 100 + cid,
    role: 'assistant',
    text: `row ${cid}`,
    segments: [],
    streaming: false,
    roomToken: 't1',
    roomName: 'Room 1',
    taskId: cid,
    unread,
  };
}

class FakeIntersectionObserver {
  observed = new Set<Element>();
  disconnected = false;
  constructor(
    private cb: IntersectionObserverCallback,
    public options: IntersectionObserverInit,
  ) {
    observers.push(this);
  }
  observe(el: Element) {
    this.observed.add(el);
  }
  unobserve(el: Element) {
    this.observed.delete(el);
  }
  disconnect() {
    this.observed.clear();
    this.disconnected = true;
  }
  takeRecords() {
    return [];
  }
  /** Deliver one entry: `visible` px of a `height` px row in a `scroller` px pane. */
  show(el: Element, visible: number, height = 100, scroller = 300) {
    const entry = {
      target: el,
      isIntersecting: visible > 0,
      intersectionRatio: height ? visible / height : 0,
      intersectionRect: { height: visible },
      boundingClientRect: { height },
      rootBounds: { height: scroller },
      time: 0,
    } as unknown as IntersectionObserverEntry;
    this.cb([entry], this as unknown as IntersectionObserver);
  }
}
let observers: FakeIntersectionObserver[] = [];
const live = () => observers.filter((o) => !o.disconnected);

let visibility: DocumentVisibilityState = 'visible';

async function settle() {
  await tick();
  await tick();
}

async function openPane(opts: { view?: string; readOnView?: boolean; rows?: unknown[] } = {}) {
  session().rooms.set([room(1)]);
  session().view.set(opts.view ?? 'unread');
  session().readOnView.set(opts.readOnView ?? true);
  session().activeRoomId.set(null);
  session().messages.set(opts.rows ?? [row(1), row(2), row(3, false)]);
  const { container } = render(Harness, { component: Page, user: person });
  await settle();
  const list = container.querySelector<HTMLElement>('[role="log"]')!;
  const rowEl = (cid: number) => list.querySelector<HTMLElement>(`[data-cid="${cid}"]`)!;
  return { list, rowEl };
}

function setVisibility(state: DocumentVisibilityState) {
  visibility = state;
  document.dispatchEvent(new Event('visibilitychange'));
}

beforeEach(() => {
  observers = [];
  visibility = 'visible';
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => visibility,
  });
  session().scrollTarget.set(null);
  spy('noteSeen').mockReset();
  spy('flushSeen').mockReset();
  vi.stubGlobal('IntersectionObserver', FakeIntersectionObserver);
  vi.stubGlobal(
    'fetch',
    vi.fn(() => new Promise<Response>(() => {})),
  );
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('the observer', () => {
  it('watches the unread rows of an aggregate pane, rooted on the scroller', async () => {
    const { list, rowEl } = await openPane();
    expect(live()).toHaveLength(1);
    const io = live()[0];
    expect(io.options.root).toBe(list);
    expect(io.options.threshold).toEqual([0, 0.5, 1]);
    expect(io.observed.has(rowEl(1))).toBe(true);
    expect(io.observed.has(rowEl(2))).toBe(true);
    // A row already read is not watched.
    expect(io.observed.has(rowEl(3))).toBe(false);
  });

  it('is not mounted in the room view', async () => {
    await openPane({ view: 'room' });
    expect(live()).toHaveLength(0);
  });

  it('is not mounted with the preference off', async () => {
    await openPane({ readOnView: false });
    expect(live()).toHaveLength(0);
  });

  it('watches rows that arrive later and lets go of rows that were read', async () => {
    const { rowEl } = await openPane();
    session().messages.set([row(1, false), row(2), row(3, false), row(4)]);
    await settle();
    const io = live()[0];
    expect(io.observed.has(rowEl(4))).toBe(true);
    expect(io.observed.has(rowEl(1))).toBe(false);
  });
});

describe('the dwell', () => {
  it('counts a row as seen after 500 ms on screen', async () => {
    const { rowEl } = await openPane();
    live()[0].show(rowEl(1), 100);
    vi.advanceTimersByTime(499);
    expect(spy('noteSeen')).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(spy('noteSeen')).toHaveBeenCalledWith(1);
  });

  it('starts over when the row leaves before the dwell is up', async () => {
    const { rowEl } = await openPane();
    const io = live()[0];
    io.show(rowEl(1), 100);
    vi.advanceTimersByTime(400);
    io.show(rowEl(1), 0);
    vi.advanceTimersByTime(1000);
    expect(spy('noteSeen')).not.toHaveBeenCalled();
  });

  it('needs half the row, or half the scroller for a tall row', async () => {
    const { rowEl } = await openPane();
    const io = live()[0];
    io.show(rowEl(1), 40, 100);
    vi.advanceTimersByTime(1000);
    expect(spy('noteSeen')).not.toHaveBeenCalled();

    // A row three screens tall never shows half of itself; half the scroller
    // is enough.
    io.show(rowEl(2), 150, 900, 300);
    vi.advanceTimersByTime(500);
    expect(spy('noteSeen')).toHaveBeenCalledWith(2);
  });

  it('is cancelled by leaving the pane', async () => {
    const { rowEl } = await openPane();
    const io = live()[0];
    io.show(rowEl(1), 100);
    session().view.set('room');
    await settle();
    expect(io.disconnected).toBe(true);
    vi.advanceTimersByTime(1000);
    expect(spy('noteSeen')).not.toHaveBeenCalled();
  });

  it('is cancelled by the tab going hidden, and restarts when it comes back', async () => {
    const { rowEl } = await openPane();
    live()[0].show(rowEl(1), 100);
    vi.advanceTimersByTime(300);
    setVisibility('hidden');
    vi.advanceTimersByTime(1000);
    expect(spy('noteSeen')).not.toHaveBeenCalled();

    setVisibility('visible');
    vi.advanceTimersByTime(499);
    expect(spy('noteSeen')).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(spy('noteSeen')).toHaveBeenCalledWith(1);
  });
});

describe('flushing', () => {
  it('flushes with keepalive when the tab goes hidden and on pagehide', async () => {
    await openPane();
    setVisibility('hidden');
    expect(spy('flushSeen')).toHaveBeenCalledWith({ keepalive: true });
    spy('flushSeen').mockClear();
    window.dispatchEvent(new Event('pagehide'));
    expect(spy('flushSeen')).toHaveBeenCalledWith({ keepalive: true });
  });
});

describe('the Unread pane opening', () => {
  it('puts the first row at the top, unhighlighted, and keeps it there', async () => {
    const { list, rowEl } = await openPane();
    Object.defineProperty(list, 'scrollHeight', { value: 2400, configurable: true });
    Object.defineProperty(list, 'clientHeight', { value: 300, configurable: true });
    const first = rowEl(1);
    first.scrollIntoView = vi.fn(() => {
      list.scrollTop = 0;
    });
    // The store paints the page and asks for the top in one go, as
    // loadViewPage does on entry.
    session().messages.set([row(1), row(2), row(5)]);
    session().scrollTarget.set({ cid: 1, nonce: 1, block: 'start' });
    await settle();

    expect(first.scrollIntoView).toHaveBeenCalledWith({ behavior: 'instant', block: 'start' });
    expect(first.classList.contains('jump-highlight')).toBe(false);
    expect(list.scrollTop).toBe(0);
  });
});

describe('the unread mark', () => {
  it('draws on unread rows in the pane', async () => {
    const { rowEl } = await openPane();
    expect(rowEl(1).querySelector('[aria-label="Unread"]')).not.toBeNull();
    expect(rowEl(3).querySelector('[aria-label="Unread"]')).toBeNull();
  });

  it('is not drawn with the preference off', async () => {
    const { rowEl } = await openPane({ readOnView: false });
    expect(rowEl(1).querySelector('[aria-label="Unread"]')).toBeNull();
  });
});
