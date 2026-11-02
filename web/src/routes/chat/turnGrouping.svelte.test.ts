import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, waitFor } from '@testing-library/svelte';
import type { Writable } from 'svelte/store';

vi.mock('$lib/stores/chat', async () => {
  const { writable } = await import('svelte/store');
  const stores: Record<string, unknown> = {
    rooms: writable([]),
    activeRoomId: writable(1),
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
    set: (target, key: string, value) => {
      target[key] = value;
      return true;
    },
  });
  return { getChatSession: () => session };
});

import { getChatSession } from '$lib/stores/chat';
import Page from './+page.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';
import type { ChatRoom, User } from '$lib/api';
import type { ChatMessage } from '$lib/stores/segments';

const person = {
  username: 'alice',
  display_name: 'Alice',
  bot_name: 'Istota',
  is_admin: false,
  features: { chat: true },
} as User;

const session = getChatSession() as unknown as {
  rooms: Writable<ChatRoom[]>;
  messages: Writable<ChatMessage[]>;
};

function assistant(cid: string, over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    cid,
    role: 'assistant',
    text: `text of ${cid}`,
    segments: [],
    streaming: false,
    createdAt: '2026-11-01T21:41:37Z',
    ...over,
  } as ChatMessage;
}

async function continuations(rows: ChatMessage[]): Promise<boolean[]> {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => new Promise<Response>(() => {})),
  );
  session.rooms.set([{ id: 1, token: 'room-a', name: 'Room A', origin: 'web' } as ChatRoom]);
  session.messages.set(rows);
  const { container } = render(Harness, { component: Page, user: person });
  await waitFor(() => expect(container.querySelectorAll('.msg').length).toBe(rows.length));
  return [...container.querySelectorAll('.msg')].map((el) => el.classList.contains('continuation'));
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('chat transcript grouping across tasks (#911)', () => {
  it('gives a live placeholder its own header under another task’s row', async () => {
    expect(
      await continuations([
        assistant('result', { taskId: 90 }),
        assistant('live', { taskId: 96, text: '', streaming: true }),
      ]),
    ).toEqual([false, false]);
  });

  it('gives each of two finished results from different tasks its own header', async () => {
    expect(
      await continuations([assistant('a', { taskId: 89 }), assistant('b', { taskId: 90 })]),
    ).toEqual([false, false]);
  });

  it('still groups bot rows that name no task', async () => {
    expect(await continuations([assistant('a'), assistant('b')])).toEqual([false, true]);
  });
});
