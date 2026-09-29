/**
 * The chat page's half of not jumping while an image loads.
 *
 * `lib/markdown/index.test.ts` pins that a `#w=&h=` hint becomes `width` /
 * `height` on the tag; this pins that the attributes survive the page's own
 * render. A renderer that emits the attributes into a page that strips them
 * reserves nothing. An image that arrived without a hint grows the rows when it
 * decodes, and the page's content ResizeObserver re-pins for that, along with
 * every other late growth — `switchPin.svelte.test.ts` covers it.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, waitFor } from '@testing-library/svelte';

// Same mock, same reason, as `imageLightbox.svelte.test.ts`: the renderer
// admits an image only for a src starting `${base}/api/chat/files?`.
vi.mock('$app/paths', () => ({ base: '/istota', assets: '' }));

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

const SRC = '/istota/api/chat/files?path=%2FUsers%2Falice%2Fistota%2Fradar.png';

/** The mocked session is module-lived, so each test seeds every field it reads. */
function seedTranscript(body: string) {
  const session = getChatSession() as unknown as Record<string, { set: (v: unknown) => void }>;
  session.rooms.set([
    {
      id: 1,
      token: 't1',
      name: 'Room 1',
      archived: false,
      created_at: '',
      updated_at: '',
      origin: 'web',
      unread_count: 0,
    },
  ]);
  session.activeRoomId.set(1);
  session.view.set('room');
  session.queuedCounts.set({});
  session.offlineTranscript.set(false);
  session.messages.set([
    {
      cid: 1,
      role: 'assistant',
      text: '',
      segments: [{ kind: 'text', id: 't0', text: body, settled: true }],
      streaming: false,
    },
  ]);
}

const renderPage = () => render(Harness, { component: Page, user: person });

beforeEach(() => {
  // A fetch that never settles moves no state under the test.
  vi.stubGlobal(
    'fetch',
    vi.fn(() => new Promise<Response>(() => {})),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('an image the model sized', () => {
  it('reaches the DOM with both attributes, so the box is reserved', async () => {
    seedTranscript(`![Radar](${SRC}#w=1439&h=812)`);
    const { container } = renderPage();
    const img = await waitFor(() => {
      const el = container.querySelector<HTMLImageElement>('img.md-image');
      expect(el).not.toBeNull();
      return el!;
    });
    expect(img.getAttribute('width')).toBe('1439');
    expect(img.getAttribute('height')).toBe('812');
  });
});
