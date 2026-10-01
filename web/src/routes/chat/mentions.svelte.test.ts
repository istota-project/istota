/**
 * The chat page hands the open room's members to the transcript (ISSUE-578).
 *
 * Only a shared room's turns style mentions, the list comes from the room's
 * own members plus the bot, and the viewer's own name is the `self` entry.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, waitFor, fireEvent } from '@testing-library/svelte';

const { members } = vi.hoisted(() => ({
  members: vi.fn(async (_id: number) => [
    { user_id: 'alice', display_name: 'Alice', is_owner: true },
    { user_id: 'bob', display_name: 'Bob', is_owner: false },
  ]),
}));

vi.mock('$lib/roomMembers', () => ({
  loadRoomMembers: (id: number) => members(id),
  dropRoomMembers: vi.fn(),
}));

vi.mock('$lib/components/chat/autocomplete/providers', async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return { ...actual, getSelectableBrains: async () => [] };
});

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
  is_admin: true,
  features: {
    chat: true,
    feeds: false,
    location: false,
    money: false,
    health: false,
    briefings: false,
    google_workspace: false,
    google_workspace_enabled: false,
    admin: true,
  },
};

type Writable = { set: (v: unknown) => void };
const session = () => getChatSession() as unknown as Record<string, Writable>;

const setRoom = (shared: boolean) =>
  session().rooms.set([
    {
      id: 1,
      token: 'web-1',
      name: 'Team',
      origin: 'web',
      talk_token: null,
      model: null,
      effort: null,
      brain: null,
      unread: 0,
      last_activity: new Date().toISOString(),
      shared,
    },
  ]);

const setMessages = () =>
  session().messages.set([
    {
      cid: 1,
      role: 'user',
      text: 'hey @alice, ask @Istota, not @mallory',
      segments: [],
      streaming: false,
      msgId: 1,
      author: 'Bob',
      authorId: 'bob',
    },
  ]);

const mentions = () =>
  [...document.querySelectorAll<HTMLElement>('.user-text .mention')].map((el) => ({
    text: el.textContent,
    self: el.classList.contains('mention-self'),
  }));

beforeEach(() => {
  members.mockClear();
  vi.stubGlobal(
    'fetch',
    vi.fn(() => new Promise<Response>(() => {})),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('mentions in the transcript', () => {
  it('styles members and the bot in a shared room, the viewer as self', async () => {
    setRoom(true);
    setMessages();
    render(Harness, { component: Page, user: person });
    await waitFor(() => expect(mentions().length).toBe(2));
    expect(mentions()).toEqual([
      { text: '@alice', self: true },
      { text: '@Istota', self: false },
    ]);
    expect(members).toHaveBeenCalledWith(1);
  });

  it('hands the composer the same people to suggest after @, never the viewer', async () => {
    setRoom(true);
    setMessages();
    render(Harness, { component: Page, user: person });
    await waitFor(() => expect(mentions().length).toBe(2));
    const textarea = document.querySelector('textarea') as HTMLTextAreaElement;
    textarea.value = 'hi @';
    textarea.selectionStart = textarea.selectionEnd = textarea.value.length;
    await fireEvent.input(textarea);
    await waitFor(() =>
      expect(
        [...document.querySelectorAll('[role="option"] .ac-label')].map((e) => e.textContent),
      ).toEqual(['@bob', '@Istota']),
    );
  });

  it('styles nothing in a private room, and does not fetch its members', async () => {
    setRoom(false);
    setMessages();
    render(Harness, { component: Page, user: person });
    await waitFor(() => expect(document.querySelector('.user-text')).toBeTruthy());
    expect(mentions()).toEqual([]);
    expect(members).not.toHaveBeenCalled();
  });
});
