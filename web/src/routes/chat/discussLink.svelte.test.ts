/**
 * "Discuss in private chat" with no note (hidden email threads, section 0c).
 *
 * The thread is read-only and there is no row in the private room to reply
 * to, so the menu item opens the private room with the composer linked to the
 * thread: a `re: <thread>` chip the user can clear. The send carries the
 * thread once and clears the chip; leaving the room clears it too. With a
 * note, the item lands on the note and links nothing.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, waitFor, fireEvent, screen } from '@testing-library/svelte';
import { __history } from '../../../vitest-stubs/app-navigation';

const { brainCatalogue } = vi.hoisted(() => ({
  brainCatalogue: vi.fn(async () => [
    { kind: 'native', label: 'Native', model_namespace: 'native' },
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

type Session = Record<string, unknown> & {
  rooms: { set: (v: unknown) => void };
  activeRoomId: { set: (v: unknown) => void };
  messages: { set: (v: unknown) => void };
  send: ReturnType<typeof vi.fn>;
  selectRoomByToken: unknown;
};
const session = () => getChatSession() as unknown as Session;

const base = {
  origin: 'web',
  talk_token: null,
  model: null,
  effort: null,
  brain: null,
  unread: 0,
  last_activity: new Date().toISOString(),
};
const THREAD = {
  ...base,
  id: 1,
  token: 'thr-1',
  name: 'Book club',
  origin: 'email',
  read_only: true,
  email_thread: true,
  listed: true,
};
const PRIVATE = { ...base, id: 2, token: 'web-2', name: 'general' };
const OTHER = { ...base, id: 3, token: 'web-3', name: 'other' };

function mailRow(over: Record<string, unknown> = {}) {
  return {
    cid: 1,
    role: 'user',
    text: 'Saturday?',
    taskId: 9,
    roomToken: 'thr-1',
    segments: [],
    streaming: false,
    receivedMail: {
      from: { name: 'Ana', address: 'ana@ext.example' },
      to: [],
      cc: [],
      date: '',
      subject: 'Book club',
      attachments: [],
      new_text: 'Saturday?',
      rest: '',
      labels: {},
      note_path: '/chat/r/web-2/t/9',
      discuss: { room: 'web-2', about: 'thr-1' },
      ...over,
    },
  };
}

const chip = () => document.querySelector<HTMLElement>('[data-testid="link-chip"]');
const textarea = () => document.querySelector<HTMLTextAreaElement>('.composer-dock textarea');

async function discuss() {
  await fireEvent.keyDown(await screen.findByLabelText('Mail actions'), { key: 'Enter' });
  return screen.findByText('Discuss in private chat');
}

beforeEach(() => {
  __history.reset('/istota/chat/');
  vi.stubGlobal(
    'fetch',
    vi.fn(() => new Promise<Response>(() => {})),
  );
  const s = session();
  s.rooms.set([THREAD, PRIVATE, OTHER]);
  s.activeRoomId.set(1);
  s.messages.set([mailRow()]);
  s.send = vi.fn(async () => undefined);
  s.selectRoomByToken = vi.fn(async (token: string) => {
    const id = [THREAD, PRIVATE, OTHER].find((r) => r.token === token)?.id;
    if (id === undefined) return false;
    s.activeRoomId.set(id);
    s.messages.set([]);
    return true;
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  session().messages.set([]);
});

const renderPage = () => render(Harness, { component: Page, user: person });

describe('Discuss in private chat with no note', () => {
  it('opens the private room with the composer linked to the thread', async () => {
    renderPage();
    await fireEvent.click(await discuss());
    expect(session().selectRoomByToken).toHaveBeenCalledWith('web-2');
    await waitFor(() => expect(chip()).toBeTruthy());
    expect(chip()!.textContent?.replace(/\s+/g, ' ').trim()).toContain('re: Book club');
    expect(__history.entries[__history.index].url).toBe('/istota/chat/?room=web-2');
    expect(__history.entries).toHaveLength(2);
    __history.back();
    await waitFor(() => expect(session().selectRoomByToken).toHaveBeenLastCalledWith('thr-1'));
    await waitFor(() => expect(chip()).toBeNull());
  });

  it('sends the thread once and clears the chip', async () => {
    renderPage();
    await fireEvent.click(await discuss());
    await waitFor(() => expect(textarea()).toBeTruthy());
    await fireEvent.input(textarea()!, { target: { value: 'tell them I am in' } });
    await fireEvent.click(screen.getByLabelText('Send'));
    await waitFor(() => expect(session().send).toHaveBeenCalledTimes(1));
    expect(session().send.mock.calls[0][0]).toBe('tell them I am in');
    expect(session().send.mock.calls[0][3]).toBe('thr-1');
    await waitFor(() => expect(chip()).toBeNull());

    await fireEvent.input(textarea()!, { target: { value: 'and another' } });
    await fireEvent.click(screen.getByLabelText('Send'));
    await waitFor(() => expect(session().send).toHaveBeenCalledTimes(2));
    expect(session().send.mock.calls[1][3]).toBeUndefined();
  });

  it('is cleared by its own control', async () => {
    renderPage();
    await fireEvent.click(await discuss());
    await waitFor(() => expect(chip()).toBeTruthy());
    await fireEvent.click(screen.getByLabelText('Clear link'));
    expect(chip()).toBeNull();
  });

  it('is cleared by switching rooms', async () => {
    renderPage();
    await fireEvent.click(await discuss());
    await waitFor(() => expect(chip()).toBeTruthy());
    session().activeRoomId.set(3);
    await waitFor(() => expect(chip()).toBeNull());
    session().activeRoomId.set(2);
    await new Promise((r) => setTimeout(r, 0));
    expect(chip()).toBeNull();
  });

  it('with a note, lands on the note and links nothing', async () => {
    session().messages.set([mailRow({ discuss: undefined })]);
    renderPage();
    const item = await discuss();
    expect(item.closest('a')?.getAttribute('href')).toContain('/chat/r/web-2/t/9');
    expect(session().selectRoomByToken).not.toHaveBeenCalled();
    expect(chip()).toBeNull();
  });
});
