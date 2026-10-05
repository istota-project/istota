/**
 * An email thread room is read-only in web (hidden-email-threads, stage 1).
 *
 * The thread room is a view of the mail thread, and a web send there was
 * answered in web and mailed to nobody. The server refuses it with 409; the
 * page renders no composer and says, in the server's words, where to ask
 * instead. The private email room and an ordinary web room are the controls.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, waitFor } from '@testing-library/svelte';

// `vi.hoisted`, because `vi.mock`'s factory is lifted above every top-level
// declaration in the file and would otherwise read this before it exists.
const { brainCatalogue } = vi.hoisted(() => ({
  brainCatalogue: vi.fn(async () => [
    { kind: 'native', label: 'Native', model_namespace: 'native' },
    { kind: 'claude_code', label: 'Claude Code', model_namespace: 'claude' },
  ]),
}));

vi.mock('$lib/components/chat/autocomplete/providers', async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return { ...actual, getSelectableBrains: () => brainCatalogue() };
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

const room = (over: Record<string, unknown> = {}) => ({
  id: 1,
  token: 'web-1',
  name: 'Testing',
  origin: 'web',
  talk_token: null,
  model: null,
  effort: null,
  brain: null,
  unread: 0,
  last_activity: new Date().toISOString(),
  ...over,
});

const setRooms = (list: Record<string, unknown>[]) => {
  const session = getChatSession() as unknown as {
    rooms: { set: (v: unknown) => void };
  };
  session.rooms.set(list.map((over, i) => room({ id: i + 1, token: `web-${i + 1}`, ...over })));
};

const renderPage = () => render(Harness, { component: Page, user: person });
const notice = () => document.querySelector<HTMLElement>('.composer-dock .readonly-notice');
const composer = () => document.querySelector('.composer-dock textarea');

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => new Promise<Response>(() => {})),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('the composer in an email thread room', () => {
  it('is replaced by the refusal the server gives', async () => {
    setRooms([{ name: 'Book club', origin: 'email', read_only: true, email_thread: true }]);
    renderPage();
    await waitFor(() => expect(notice()).toBeTruthy());
    expect(notice()!.textContent?.replace(/\s+/g, ' ').trim()).toBe(
      'This is an email thread. Ask from your private chat and the bot will draft the reply.',
    );
    expect(composer()).toBeNull();
  });

  it('leaves the private email room its own wording', async () => {
    setRooms([{ name: 'Email', origin: 'email', phone_surface: 'email', read_only: true }]);
    renderPage();
    await waitFor(() => expect(notice()).toBeTruthy());
    expect(notice()!.textContent).toContain('transcript of an');
    expect(notice()!.textContent).not.toContain('email thread');
  });

  it('renders no draft card under a thread row (stage 3)', async () => {
    // The thread is a mail view: a held mail shows its card and a link to
    // the private chat, and the draft is acted on there, not here.
    const session = getChatSession() as unknown as Record<string, { set: (v: unknown) => void }>;
    const held = {
      cid: 1,
      role: 'assistant',
      text: 'Thursday works.',
      taskId: 9,
      segments: [{ kind: 'text', text: 'Thursday works.' }],
      streaming: false,
      mail: { to: ['alice@example.com'], cc: [], state: 'held' },
    };
    const draft = {
      id: 4,
      status: 'pending',
      task_id: 9,
      to: ['alice@example.com'],
      subject: 'Re: Dinner',
      body: 'Thursday works.',
    };
    session.messages.set([held]);
    session.outboundDrafts.set([draft]);
    try {
      setRooms([{ name: 'Book club', origin: 'email', read_only: true, email_thread: true }]);
      renderPage();
      await waitFor(() => expect(document.querySelector('[data-testid="mail-card"]')).toBeTruthy());
      expect(document.querySelector('.draft-card')).toBeNull();
      cleanup();

      // The control: the same row in an ordinary room carries its draft.
      setRooms([{ name: 'Testing' }]);
      renderPage();
      await waitFor(() => expect(document.querySelector('.draft-card')).toBeTruthy());
    } finally {
      session.messages.set([]);
      session.outboundDrafts.set([]);
    }
  });

  it('puts the draft under the email note in the private room (stage 5)', async () => {
    const session = getChatSession() as unknown as Record<string, unknown> & {
      messages: { set: (v: unknown) => void };
      outboundDrafts: { set: (v: unknown) => void };
      answerDraft: ReturnType<typeof vi.fn>;
    };
    const note = {
      cid: 2,
      role: 'system',
      text: 'Ana wrote on Book club:\n\n> Thursday?\n\nReply waiting for your approval.',
      taskId: 9,
      aboutRoom: { token: 'thread-1', name: 'Book club' },
      segments: [],
      streaming: false,
      mail: { to: ['ana@example.com'], cc: [], state: 'held', body: 'Thursday works.' },
    };
    const draft = {
      id: 4,
      status: 'pending',
      task_id: 9,
      to: ['ana@example.com'],
      subject: 'Re: Book club',
      body: 'Thursday works.',
    };
    session.messages.set([note]);
    session.outboundDrafts.set([draft]);
    try {
      setRooms([{ name: 'general' }]);
      renderPage();
      await waitFor(() => expect(document.querySelector('.draft-card')).toBeTruthy());
      expect(document.querySelector('[data-testid="mail-collapsed"]')).toBeTruthy();
      const send = [...document.querySelectorAll<HTMLButtonElement>('.draft-card button')].find(
        (b) => b.textContent?.trim() === 'Send',
      );
      send!.click();
      await waitFor(() => expect(session.answerDraft).toHaveBeenCalledWith(4, 'approve'));
      cleanup();

      // The control: a plain notice carrying the same task id gets no draft.
      session.messages.set([{ ...note, aboutRoom: undefined, mail: undefined }]);
      setRooms([{ name: 'general' }]);
      renderPage();
      await waitFor(() => expect(document.querySelector('.cmd-row')).toBeTruthy());
      expect(document.querySelector('.draft-card')).toBeNull();
    } finally {
      session.messages.set([]);
      session.outboundDrafts.set([]);
    }
  });

  it('keeps the composer in an ordinary web room', async () => {
    setRooms([{ name: 'Testing' }]);
    renderPage();
    await waitFor(() => expect(composer()).toBeTruthy());
    expect(notice()).toBeNull();
  });
});
