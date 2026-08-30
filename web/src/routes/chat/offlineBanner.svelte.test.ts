/**
 * What the chat page says about being offline: the banner, the room-list badge
 * for what is waiting to send, and what an empty room reads as.
 *
 * Two things here are load-bearing and neither is the wording. The row is tied
 * to the connectivity store rather than to a failed request, so it goes as soon
 * as the connection is back and without a reload — and it is drawn on the page
 * rather than raised as a notice: a toast announces an event once and then
 * takes the explanation away while the composer still cannot reach anything,
 * which is why the empty notice queue below is an assertion rather than
 * housekeeping.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';
import { get } from 'svelte/store';

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

const OFFLINE_TEXT = /Offline — messages will send when you’re back/;

beforeEach(() => {
  // The page's own mount requests (`getMe`) go through `apiFetch`, which
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

describe('the offline banner', () => {
  it('is absent while the app can reach the server', async () => {
    render(Page);
    await waitFor(() => expect(screen.queryByText(OFFLINE_TEXT)).toBeNull());
  });

  it('appears when a request finds no server, and goes when one does', async () => {
    render(Page);

    noteTransport(false, 'unreachable');
    expect(get(online)).toBe(false);
    await waitFor(() => expect(screen.getByText(OFFLINE_TEXT)).toBeInTheDocument());

    // No reload: the same store flipping back takes the row away.
    noteTransport(true);
    await waitFor(() => expect(screen.queryByText(OFFLINE_TEXT)).toBeNull());
  });

  it('states it on the page rather than through a notice', async () => {
    render(Page);
    noteTransport(false, 'unreachable');
    await waitFor(() => expect(screen.getByText(OFFLINE_TEXT)).toBeInTheDocument());

    expect(get(notices)).toEqual([]);
  });

  it('is non-dismissible: it carries no button of its own', async () => {
    render(Page);
    noteTransport(false, 'unreachable');
    const row = await screen.findByText(OFFLINE_TEXT);
    expect(row.querySelector('button')).toBeNull();
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
    render(Page);
    await waitFor(() => expect(screen.getByTitle('3 not sent yet')).toBeInTheDocument());
    expect(screen.getByTitle('3 not sent yet').textContent).toBe('3');
  });

  it('draws no badge for the room on screen, whose rows are the count', async () => {
    // The same rule the unread pill above it follows: a count of what you are
    // already looking at is noise.
    seedRooms({ t1: 2 }, 1);
    render(Page);
    await waitFor(() => expect(roomRow('Room 1')).toBeInTheDocument());
    expect(screen.queryByTitle(/not sent yet/)).toBeNull();
  });

  it('shows nothing for a room with nothing waiting', async () => {
    seedRooms({});
    render(Page);
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
    render(Page);
    await waitFor(() => expect(screen.getByText(NOTHING_SAVED)).toBeInTheDocument());
    expect(screen.queryByText(/Ask Istota anything/)).toBeNull();
  });

  it('keeps the ordinary invitation for a room that is simply empty', async () => {
    setEmptyRoom(false);
    render(Page);
    await waitFor(() => expect(screen.getByText(/Ask Istota anything/)).toBeInTheDocument());
    expect(screen.queryByText(NOTHING_SAVED)).toBeNull();
  });
});
