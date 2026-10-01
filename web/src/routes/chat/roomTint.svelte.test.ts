/**
 * A room colour tints the room's sidebar row and adds nothing else to it.
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

function room(id: number, color: string | null) {
  return {
    id,
    token: `t${id}`,
    name: `Room ${id}`,
    archived: false,
    created_at: '',
    updated_at: '',
    origin: 'web',
    unread_count: 0,
    color,
  };
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => new Promise<Response>(() => {})),
  );
  vi.stubGlobal('requestAnimationFrame', () => 0);
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      disconnect() {}
    },
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('a coloured room row', () => {
  it('is tinted and carries no dot', async () => {
    session().rooms.set([room(1, 'rose'), room(2, null)]);
    session().activeRoomId.set(2);
    const { container } = render(Harness, { component: Page, user: person });
    await tick();
    await tick();

    const rows = container.querySelectorAll<HTMLElement>('.room-row');
    expect(rows).toHaveLength(2);
    expect(rows[0].classList.contains('tinted')).toBe(true);
    expect(rows[0].style.getPropertyValue('--room-tint')).not.toBe('');
    expect(rows[1].classList.contains('tinted')).toBe(false);
    expect(container.querySelector('.room-dot')).toBeNull();
  });
});
