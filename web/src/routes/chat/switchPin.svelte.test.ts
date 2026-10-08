/**
 * Returning to a room opens it at the newest message (ISSUE-560).
 *
 * A switch paints twice: the cached tail from IndexedDB, then the server page,
 * which replaces every row. iOS WebKit drops a plain `scrollTop` write made in
 * the frame after a swap that large, so the pin that follows it has to be the
 * repaint pass, and it has to follow the *server* paint. It used to follow the
 * cached one only, and the room opened at the cached tail's bottom, part way up
 * the full page.
 *
 * jsdom lays nothing out and runs no frames, so the tests queue the page's
 * `requestAnimationFrame` callbacks by hand and give the scroller the metrics
 * its arithmetic reads.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
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
    view: writable('room'),
    scrollTarget: writable(null),
    sendSettled: writable({ n: 0, token: null }),
    sendReturned: writable({ n: 0, token: null, text: '', attachments: [] }),
    outboundDrafts: writable([]),
    externalTurnDisplay: writable('full'),
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
const loadOlder = () =>
  (getChatSession() as unknown as { loadOlder: ReturnType<typeof vi.fn> }).loadOlder;

function room(id: number) {
  return {
    id,
    token: `t${id}`,
    name: `Room ${id}`,
    archived: false,
    created_at: '',
    updated_at: '',
    origin: 'web',
    unread_count: 0,
  };
}

let nextCid = 1;
function rows(n: number) {
  return Array.from({ length: n }, (_, i) => ({
    cid: nextCid++,
    role: i % 2 ? 'assistant' : 'user',
    text: `row ${i}`,
    segments: [],
    streaming: false,
  }));
}

// Frames, run by hand.
let frames: FrameRequestCallback[] = [];
function runFrames() {
  while (frames.length) frames.shift()!(0);
}

// Every observer the page makes, by the element it watches. The composer dock
// has one too, so a test reaching for "the" observer has to name the target.
let observers: { cb: () => void; el: Element }[] = [];
class FakeResizeObserver {
  constructor(private cb: () => void) {}
  observe(el: Element) {
    observers.push({ cb: this.cb, el });
  }
  disconnect() {
    observers = observers.filter((o) => o.cb !== this.cb);
  }
}
/** Deliver a size change on the transcript rows, as the browser would. */
function resize() {
  const live = observers.filter((o) => o.el.isConnected && o.el.classList.contains('transcript'));
  expect(live).toHaveLength(1);
  live[0].cb();
}

/** Metrics, and a record of every offset the page writes. */
function measure(list: HTMLElement, scrollHeight: number, clientHeight = 300) {
  Object.defineProperty(list, 'scrollHeight', { value: scrollHeight, configurable: true });
  Object.defineProperty(list, 'clientHeight', { value: clientHeight, configurable: true });
}
function recordWrites(list: HTMLElement) {
  const writes: number[] = [];
  let top = 0;
  Object.defineProperty(list, 'scrollTop', {
    configurable: true,
    get: () => top,
    set: (v: number) => {
      top = v;
      writes.push(v);
    },
  });
  return writes;
}

async function settle() {
  await tick();
  await tick();
}

async function openRoomOne() {
  session().rooms.set([room(1), room(2)]);
  session().view.set('room');
  session().queuedCounts.set({});
  session().hasMore.set(false);
  session().offlineTranscript.set(false);
  session().activeRoomId.set(1);
  session().messages.set(rows(4));
  const { container } = render(Harness, { component: Page, user: person });
  await settle();
  runFrames();
  return container.querySelector<HTMLElement>('[role="log"]')!;
}

/** Switch to room 2 and paint its cached tail, as `loadHistory` does. */
async function switchAndPaintCache(list: HTMLElement) {
  session().activeRoomId.set(2);
  session().messages.set([]);
  await settle();
  measure(list, 1200);
  session().messages.set(rows(20));
  session().offlineTranscript.set(true);
  await settle();
}

/** The server page replacing the cached tail with a taller one. */
async function paintWire(list: HTMLElement) {
  measure(list, 2400);
  session().offlineTranscript.set(false);
  session().hasMore.set(true);
  session().messages.set(rows(50));
  await settle();
}

beforeEach(() => {
  frames = [];
  observers = [];
  session().scrollTarget.set(null);
  vi.stubGlobal(
    'fetch',
    vi.fn(() => new Promise<Response>(() => {})),
  );
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    frames.push(cb);
    return frames.length;
  });
  vi.stubGlobal('ResizeObserver', FakeResizeObserver);
  loadOlder().mockReset();
  loadOlder().mockResolvedValue(false);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('a room switch', () => {
  it('pins the server page with the repaint pass, not only the cached tail', async () => {
    const list = await openRoomOne();
    await switchAndPaintCache(list);
    runFrames();

    const writes = recordWrites(list);
    await paintWire(list);
    runFrames();

    // The pin, one pixel off the bottom of the *server* page, then the pin
    // again: the pass that makes WebKit repaint the swap.
    expect(writes).toEqual([2400, 2400 - 300 - 1, 2400]);
  });

  it('keeps an explicit jump when a room-switch repaint is still queued', async () => {
    const list = await openRoomOne();
    await switchAndPaintCache(list);
    runFrames();
    await paintWire(list);
    const row = list.querySelector<HTMLElement>('[data-cid]')!;
    row.scrollIntoView = vi.fn(() => {
      list.scrollTop = 300;
    });
    session().scrollTarget.set({ cid: Number(row.dataset.cid), nonce: 1 });
    await settle();
    runFrames();
    resize();
    expect(list.scrollTop).toBe(300);
  });

  it('keeps paging off until the settle pin has landed', async () => {
    const list = await openRoomOne();
    await switchAndPaintCache(list);
    runFrames();
    await paintWire(list);

    // WebKit left the offset near the top of the new page and a scroll event
    // reports it before the repaint frames have run.
    list.scrollTop = 0;
    await fireEvent.scroll(list);
    expect(loadOlder()).not.toHaveBeenCalled();

    runFrames();
    list.scrollTop = 0;
    await fireEvent.scroll(list);
    expect(loadOlder()).toHaveBeenCalledTimes(1);
  });

  it('re-pins when the rows grow under a reader at the bottom', async () => {
    const list = await openRoomOne();
    await switchAndPaintCache(list);
    runFrames();
    await paintWire(list);
    runFrames();

    // A dropped write leaves the offset where the cached tail ended; the rows
    // growing is the only thing that happens next.
    list.scrollTop = 900;
    resize();
    expect(list.scrollTop).toBe(2400);
  });

  it('does not undo the repaint nudge when the rows report a size inside its frame', async () => {
    const list = await openRoomOne();
    await switchAndPaintCache(list);
    runFrames();
    await paintWire(list);

    // Frame 1 moves one pixel off the bottom; the browser then delivers the
    // rows' new size before it paints. A plain pin there would put the offset
    // back and leave nothing for the frame to repaint.
    frames.shift()!(0);
    resize();
    expect(list.scrollTop).toBe(2400 - 300 - 1);

    runFrames();
    expect(list.scrollTop).toBe(2400);
  });

  it('leaves the viewport alone when the rows grow under a reader who scrolled up', async () => {
    const list = await openRoomOne();
    measure(list, 2400);
    list.scrollTop = 600;
    await fireEvent.scroll(list);

    resize();
    expect(list.scrollTop).toBe(600);
  });
});

describe('an older page', () => {
  it('restores the anchor only when it prepended into this transcript', async () => {
    const list = await openRoomOne();
    measure(list, 2400);
    session().hasMore.set(true);
    await settle();

    // A page the room left behind: another room's transcript is on screen by
    // the time it settles, and restoring against the old height would throw
    // the viewport somewhere arbitrary in it.
    loadOlder().mockImplementationOnce(async () => {
      measure(list, 1000);
      return false;
    });
    list.scrollTop = 100;
    await fireEvent.scroll(list);
    await settle();
    expect(list.scrollTop).toBe(100);

    // A page prepended: the transcript grew above the viewport.
    measure(list, 2400);
    loadOlder().mockImplementationOnce(async () => {
      measure(list, 3000);
      return true;
    });
    await fireEvent.scroll(list);
    await settle();
    expect(list.scrollTop).toBe(700);
  });
});
