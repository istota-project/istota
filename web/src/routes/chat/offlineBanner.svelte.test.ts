/**
 * What the chat page says about being offline: the notice, the room-list badge
 * for what is waiting to send, and what an empty room reads as.
 *
 * Three things here are load-bearing and none of them is the wording.
 *
 * It is tied to the connectivity store rather than to a failed request, so it
 * goes as soon as the connection is back and without a reload.
 *
 * It is a **sticky** notice, not a banner drawn into the page. Both earlier
 * placements — a row in the composer dock, then a `NoticeBanner` in the shell's
 * `extras` band — cost layout: the banner was a bordered card taking about 55px
 * of a phone's pane for one sentence of chrome. The notice band overlays, so
 * the transcript is exactly as long offline as on. `costs the transcript no
 * height` is what pins that, and it is the assertion the whole change exists
 * for.
 *
 * And sticky is what makes a notice honest for a condition rather than an
 * event. The store's own machinery would otherwise retract it while it still
 * held — see `notices.sticky.test.ts`, which owns those semantics. What this
 * file pins is the half that belongs to the page: the raise, the take-down when
 * the connection returns, and the take-down on leaving /chat.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

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
  // Every method the page reaches for answers with a resolved promise —
  // `init()` is awaited on mount, and the rest are click handlers this file
  // never fires.
  const session = new Proxy(stores, {
    get: (target, key: string) => (target[key] ??= vi.fn(async () => undefined)),
  });
  return { getChatSession: () => session };
});

import { getChatSession } from '$lib/stores/chat';
import { online, noteTransport } from '$lib/stores/connectivity';
import { notices, clearNotices } from '$lib/stores/notices';
import Page from './+page.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';
import type { User } from '$lib/api';

const OFFLINE_TEXT = /Offline — messages will send when you’re back/;

// The page reads the identity the root layout resolved rather than fetching one
// (ISSUE-355), so the harness stands in for that layout. Only the author labels
// and the draft key read it, and neither is what this file is about.
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

const renderPage = () => render(Harness, { component: Page, user: person });

/**
 * The visible band's text.
 *
 * Scoped rather than a bare `getByText`: `NoticeDrawer` mirrors the message
 * into two permanently-mounted `aria-live` regions as well as the band, so an
 * unscoped query matches three nodes and throws before asserting anything.
 */
const bandText = () => document.querySelector('.notice-region')?.textContent ?? '';

beforeEach(() => {
  // Any request the page or its components make goes through `apiFetch`, which
  // reports every completion to the connectivity store — so a stub that
  // answered would flip the store back under the test a moment after it set
  // it. One that never settles reports nothing, leaving this file's own
  // `noteTransport` calls the only input.
  vi.stubGlobal(
    'fetch',
    vi.fn(() => new Promise<Response>(() => {})),
  );
  noteTransport(true);
  clearNotices();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  noteTransport(true);
  clearNotices();
});

describe('the offline notice', () => {
  it('is absent while the app can reach the server', async () => {
    renderPage();
    await waitFor(() => expect(bandText()).not.toMatch(OFFLINE_TEXT));
  });

  it('appears when a request finds no server, and goes when one does', async () => {
    renderPage();

    noteTransport(false, 'unreachable');
    expect(get(online)).toBe(false);
    await waitFor(() => expect(bandText()).toMatch(OFFLINE_TEXT));

    // No reload: the same store flipping back takes it away.
    noteTransport(true);
    await waitFor(() => expect(bandText()).not.toMatch(OFFLINE_TEXT));
  });

  it('is rendered by the drawer alone, never into the page\u2019s own layout', async () => {
    // The point of the whole change, and the one claim that has to be checked
    // structurally rather than by measuring. A first attempt asserted that the
    // transcript was the same height offline as online; jsdom performs no
    // layout, so every `getBoundingClientRect().height` is 0 and that assertion
    // passed with a deliberately reintroduced 55px band in the shell. It could
    // not fail. The real measurement was taken in a browser (the transcript is
    // 610.62px both ways) and belongs in the commit message, not here.
    //
    // What jsdom *can* answer is where the sentence is rendered. The drawer's
    // own region is absolutely positioned out of flow, pinned below; anything
    // the page rendered itself would be a flow sibling and would reflow the
    // pane. So: every node carrying the text belongs to the drawer.
    renderPage();
    noteTransport(false, 'unreachable');
    await waitFor(() => expect(bandText()).toMatch(OFFLINE_TEXT));

    const carriers = [...document.querySelectorAll('*')].filter(
      (el) => el.children.length === 0 && OFFLINE_TEXT.test(el.textContent ?? ''),
    );
    expect(carriers.length).toBeGreaterThan(0);
    for (const el of carriers) {
      // `notice-region` is the visible band, `notice-announce` the two live
      // regions it mirrors into. Both are NoticeDrawer's; nothing else may
      // carry this sentence.
      expect(el.closest('.notice-region, .notice-announce')).not.toBeNull();
    }
  });

  it('puts nothing in the composer dock, which is where it used to live', async () => {
    renderPage();
    noteTransport(false, 'unreachable');
    await waitFor(() => expect(bandText()).toMatch(OFFLINE_TEXT));

    const dock = document.querySelector('.composer-dock');
    expect(dock).not.toBeNull();
    expect(OFFLINE_TEXT.test(dock?.textContent ?? '')).toBe(false);
  });

  it('is raised as a sticky notice, so the store cannot retract a live condition', async () => {
    renderPage();
    noteTransport(false, 'unreachable');
    await waitFor(() => expect(bandText()).toMatch(OFFLINE_TEXT));

    const raised = get(notices).filter((n) => n.key === 'chat:offline');
    expect(raised).toHaveLength(1);
    expect(raised[0].sticky).toBe(true);
    // Pinned until something takes it down, never on a clock.
    expect(raised[0].duration).toBe(0);
  });

  it('takes it down on leaving /chat, since sticky survives the navigation clear', async () => {
    // The sentence promises the send queue will drain and no other route has
    // one to promise, so the page that raised it has to be the page that ends
    // it. `clearNotices` deliberately will not.
    renderPage();
    noteTransport(false, 'unreachable');
    await waitFor(() => expect(get(notices).some((n) => n.key === 'chat:offline')).toBe(true));

    cleanup();
    expect(get(notices).some((n) => n.key === 'chat:offline')).toBe(false);
  });

  it('states it once, however many requests fail', async () => {
    renderPage();
    noteTransport(false, 'unreachable');
    await waitFor(() => expect(bandText()).toMatch(OFFLINE_TEXT));

    noteTransport(false, 'timeout');
    noteTransport(false, 'unreachable');
    await waitFor(() =>
      expect(get(notices).filter((n) => n.key === 'chat:offline')).toHaveLength(1),
    );
  });
});

/**
 * The room-list badge for messages waiting to send (ISSUE-202).
 *
 * The drain runs for the room on screen only, so for every other room this
 * badge is the whole of the affordance: it says which room to open for what is
 * waiting in it to go out. Its count comes from the store's `queuedCounts`
 * rather than from the transcript, which holds one room's rows at a time.
 */
describe('the room-list queued badge', () => {
  const room = (id: number, name = `Room ${id}`) => ({
    id,
    token: `t${id}`,
    name,
    archived: false,
    created_at: '',
    updated_at: '',
    origin: 'web',
    unread_count: 0,
  });

  function seedRooms(counts: Record<string, number>, activeId: number | null = null) {
    // The mocked session is module-lived, so every field a test sets has to be
    // set by every test — otherwise the room left open by one decides what the
    // next one renders.
    const session = getChatSession() as unknown as {
      rooms: { set: (v: unknown) => void };
      queuedCounts: { set: (v: unknown) => void };
      activeRoomId: { set: (v: unknown) => void };
    };
    session.rooms.set([room(1), room(2)]);
    session.queuedCounts.set(counts);
    session.activeRoomId.set(activeId);
  }

  /** The sidebar's entry for a room, which is not the only place its name is. */
  const roomRow = (name: string) => screen.getAllByText(name)[0];

  it('counts what is waiting in a room the user is not looking at', async () => {
    seedRooms({ t2: 3 });
    renderPage();
    await waitFor(() => expect(screen.getByTitle('3 not sent yet')).toBeInTheDocument());
    expect(screen.getByTitle('3 not sent yet').textContent).toBe('3');
  });

  it('draws no badge for the room on screen, whose rows are the count', async () => {
    // The same rule the unread pill above it follows: a count of what you are
    // already looking at is noise.
    seedRooms({ t1: 2 }, 1);
    renderPage();
    await waitFor(() => expect(roomRow('Room 1')).toBeInTheDocument());
    expect(screen.queryByTitle(/not sent yet/)).toBeNull();
  });

  it('shows nothing for a room with nothing waiting', async () => {
    seedRooms({});
    renderPage();
    await waitFor(() => expect(roomRow('Room 1')).toBeInTheDocument());
    expect(screen.queryByTitle(/not sent yet/)).toBeNull();
  });
});

/**
 * An empty room with nothing cached for it (ISSUE-202).
 *
 * A room that has never been opened with a connection has no saved tail, so
 * offline it renders empty — honest, and silent about why. The invitation to
 * ask something is the wrong sentence there: it reads as an empty room rather
 * than as a room that cannot be read from here, and it says nothing about the
 * composer underneath still working.
 */
describe('an empty transcript with nothing cached', () => {
  const NOTHING_SAVED = /Nothing from this room is saved on this device/;

  function setEmptyRoom(offline: boolean) {
    const session = getChatSession() as unknown as Record<string, { set: (v: unknown) => void }>;
    session.rooms.set([]);
    session.queuedCounts.set({});
    session.activeRoomId.set(null);
    session.messages.set([]);
    session.view.set('room');
    session.offlineTranscript.set(offline);
  }

  it('says nothing is saved for the room rather than inviting a question', async () => {
    setEmptyRoom(true);
    renderPage();
    await waitFor(() => expect(screen.getByText(NOTHING_SAVED)).toBeInTheDocument());
    expect(screen.queryByText(/Ask Istota anything/)).toBeNull();
  });

  it('keeps the ordinary invitation for a room that is simply empty', async () => {
    setEmptyRoom(false);
    renderPage();
    await waitFor(() => expect(screen.getByText(/Ask Istota anything/)).toBeInTheDocument());
    expect(screen.queryByText(NOTHING_SAVED)).toBeNull();
  });

  /**
   * Read out of the source rather than off a computed style: jsdom applies no
   * stylesheet, so `getComputedStyle` would report an empty padding whether or
   * not the rule is there, and the assertion would pass on a page with the
   * inset removed. Same reasoning as `Composer.sendButton.svelte.test.ts`.
   *
   * A message row takes its inset from `Message`, so an empty state — which
   * has no row — needs one of its own or it runs edge to edge. The offline
   * notice is where that showed: it is the only empty state whose hint wraps
   * to two lines, so on a phone both lines touched both sides.
   */
  it('insets the empty state from both edges', () => {
    const source = readFileSync(
      resolve(dirname(fileURLToPath(import.meta.url)), '+page.svelte'),
      'utf8',
    );
    const rule = source.match(/\n {2}\.chat-empty \{([\s\S]*?)\n {2}\}/);
    expect(rule, '.chat-empty rule not found in +page.svelte').not.toBeNull();
    expect(rule![1]).toMatch(/padding-inline:\s*var\(--space-\d\)/);
  });
});
