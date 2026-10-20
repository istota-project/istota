/**
 * `/notes/new`: a text box with focus, saved as a note in the quick notes
 * folder. The first save creates the file under a `client_note_id` minted once
 * per editor session, later saves replace it against the last revision, and a
 * draft in localStorage keeps the text until a confirmed save and Done.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import type { QuickNoteCreated, User, WorkspaceEntry } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);

const nav = vi.hoisted(() => ({ goto: vi.fn(async (_url: string) => {}) }));
vi.mock('$app/navigation', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$app/navigation')>();
  return { ...actual, goto: nav.goto };
});

function dir(name: string): WorkspaceEntry {
  return { name, kind: 'dir', size: null, modified: 0, hidden: false };
}

function created(name: string, over: Partial<QuickNoteCreated> = {}): QuickNoteCreated {
  return { path: `Notes/Inbox/${name}`, name, revision: 'r1', created: true, ...over };
}

await fillApiDouble(api, {
  createQuickNote: vi.fn(async () => created('Buy milk.md')),
  saveQuickNote: vi.fn(async () => ({ revision: 'r2' })),
  updateProfile: vi.fn(async () => ({ ok: true, fields: ['quick_notes_folder'] })),
  getWorkspaceListing: vi.fn(async (_place: string, path = '') => ({
    place: 'home',
    path,
    entries: path === '' ? [dir('Notes')] : [],
    truncated: false,
    skipped_names: 0,
  })),
  createWorkspaceFolder: vi.fn(),
});

const { QuickNoteError, TextFileConflictError, AuthError } = await import('$lib/api');
import Page from './+page.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';

const DRAFT_KEY = 'notes.newDraft';

const alice: User = {
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

const expireSession = vi.fn();

function mount() {
  return render(Harness, { user: alice, component: Page, onExpireSession: expireSession });
}

function editor(): HTMLTextAreaElement {
  return screen.getByRole('textbox', { name: 'Note' }) as HTMLTextAreaElement;
}

async function type(text: string) {
  await fireEvent.input(editor(), { target: { value: text } });
}

function draft(): { text: string; clientNoteId: string; path: string | null } | null {
  const raw = localStorage.getItem(DRAFT_KEY);
  return raw === null ? null : JSON.parse(raw);
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { value: state, configurable: true });
  document.dispatchEvent(new Event('visibilitychange'));
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  vi.mocked(api.createQuickNote).mockImplementation(async () => created('Buy milk.md'));
  vi.mocked(api.saveQuickNote).mockImplementation(async () => ({ revision: 'r2' }));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
});

describe('/notes/new', () => {
  it('puts focus in the text box on mount', async () => {
    mount();
    await waitFor(() => expect(document.activeElement).toBe(editor()));
  });

  it('makes one create when Done, the idle timer and hiding fire together', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const answer = deferred<QuickNoteCreated>();
    vi.mocked(api.createQuickNote).mockImplementation(() => answer.promise);
    mount();
    await type('Buy milk');

    setVisibility('hidden');
    await fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    vi.advanceTimersByTime(1500);
    answer.resolve(created('Buy milk.md'));

    expect(await screen.findByText('Buy milk.md')).toBeInTheDocument();
    expect(api.createQuickNote).toHaveBeenCalledTimes(1);
    expect(api.saveQuickNote).not.toHaveBeenCalled();
  });

  it('replaces the note with the last revision once it exists', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mount();
    await type('Buy milk');
    vi.advanceTimersByTime(1500);
    await waitFor(() => expect(api.createQuickNote).toHaveBeenCalledTimes(1));

    await type('Buy milk\nand eggs');
    await fireEvent.click(screen.getByRole('button', { name: 'Done' }));

    await screen.findByText(/Saved as/);
    expect(api.saveQuickNote).toHaveBeenCalledWith(
      'Notes/Inbox/Buy milk.md',
      'Buy milk\nand eggs',
      'r1',
    );
    expect(api.createQuickNote).toHaveBeenCalledTimes(1);
  });

  it('writes nothing for whitespace only, and Done closes', async () => {
    mount();
    await type('  \n ');
    await fireEvent.click(screen.getByRole('button', { name: 'Done' }));

    await waitFor(() => expect(nav.goto).toHaveBeenCalledWith('/'));
    expect(api.createQuickNote).not.toHaveBeenCalled();
    expect(draft()).toBeNull();
  });

  it('keeps the draft when a save fails, and clears it after a confirmed save and Done', async () => {
    vi.mocked(api.createQuickNote).mockRejectedValueOnce(new Error('Could not reach the server.'));
    mount();
    await type('Buy milk');
    expect(draft()?.text).toBe('Buy milk');

    await fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    expect(await screen.findByText('Could not reach the server.')).toBeInTheDocument();
    expect(draft()?.text).toBe('Buy milk');
    expect(editor().value).toBe('Buy milk');

    await fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    const saved = await screen.findByText(/Saved as/);
    expect(within(saved).getByText('Buy milk.md')).toBeInTheDocument();
    expect(saved).toHaveTextContent('Saved as Buy milk.md in Notes/Inbox');
    expect(draft()).toBeNull();
    expect(screen.getByRole('link', { name: 'Show in Files' })).toHaveAttribute(
      'href',
      '/files?place=home&path=Notes%2FInbox',
    );
  });

  it('restores a draft with the same client_note_id after a reload', async () => {
    const first = mount();
    await type('Buy milk');
    const minted = draft()!.clientNoteId;
    expect(minted).toMatch(/^[A-Za-z0-9-]{8,64}$/);
    first.unmount();

    mount();
    expect(editor().value).toBe('Buy milk');
    await fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    await screen.findByText(/Saved as/);
    expect(api.createQuickNote).toHaveBeenCalledWith('Buy milk', minted);
  });

  it('says the note changed elsewhere on a conflict, and keeps the text', async () => {
    vi.mocked(api.saveQuickNote).mockRejectedValue(new TextFileConflictError());
    mount();
    await type('Buy milk');
    await fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    await screen.findByText(/Saved as/);
    await fireEvent.click(screen.getByRole('button', { name: 'New note' }));
    // A second session on a note the server already holds.
    vi.mocked(api.createQuickNote).mockResolvedValueOnce(created('Eggs.md', { created: false }));
    await type('Eggs');
    await fireEvent.click(screen.getByRole('button', { name: 'Done' }));

    expect(await screen.findByText(/This note was changed elsewhere\./)).toBeInTheDocument();
    expect(editor().value).toBe('Eggs');
    expect(draft()?.text).toBe('Eggs');
    expect(api.saveQuickNote).toHaveBeenCalledTimes(1);
  });

  it('asks for a folder when none is set, keeps the text, and then saves', async () => {
    vi.mocked(api.createQuickNote).mockRejectedValueOnce(
      new QuickNoteError(409, 'notes_folder_unset', 'Choose a quick notes folder first.'),
    );
    mount();
    await type('Buy milk');
    await fireEvent.click(screen.getByRole('button', { name: 'Done' }));

    expect(await screen.findByText(/Choose a quick notes folder/)).toBeInTheDocument();
    expect(editor().value).toBe('Buy milk');
    await fireEvent.click(screen.getByRole('button', { name: 'Choose folder' }));
    const dialog = await screen.findByRole('dialog');
    await fireEvent.click(await within(dialog).findByRole('button', { name: 'Notes' }));
    await waitFor(() => expect(api.getWorkspaceListing).toHaveBeenLastCalledWith('home', 'Notes'));
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Choose this folder' }));

    await waitFor(() =>
      expect(api.updateProfile).toHaveBeenCalledWith({ quick_notes_folder: 'Notes' }),
    );
    await waitFor(() => expect(api.createQuickNote).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.createQuickNote).mock.calls[1][0]).toBe('Buy milk');
    expect(editor().value).toBe('Buy milk');
  });

  it('says the folder changed, keeps the text and offers a new note', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(api.saveQuickNote).mockRejectedValueOnce(
      new QuickNoteError(403, 'outside_folder', 'That note is not in your quick notes folder.'),
    );
    mount();
    await type('Buy milk');
    vi.advanceTimersByTime(1500);
    await waitFor(() => expect(api.createQuickNote).toHaveBeenCalledTimes(1));
    const firstKey = vi.mocked(api.createQuickNote).mock.calls[0][1];

    await type('Buy milk and eggs');
    await fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    expect(await screen.findByText(/quick notes folder changed/)).toBeInTheDocument();
    expect(editor().value).toBe('Buy milk and eggs');

    await fireEvent.click(screen.getByRole('button', { name: 'Save as a new note' }));
    await waitFor(() => expect(api.createQuickNote).toHaveBeenCalledTimes(2));
    const [content, key] = vi.mocked(api.createQuickNote).mock.calls[1];
    expect(content).toBe('Buy milk and eggs');
    expect(key).not.toBe(firstKey);
  });

  it('starts a fresh editor session with a new key on New note', async () => {
    mount();
    await type('Buy milk');
    await fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    await screen.findByText(/Saved as/);

    await fireEvent.click(screen.getByRole('button', { name: 'New note' }));
    expect(editor().value).toBe('');
    await waitFor(() => expect(document.activeElement).toBe(editor()));
    await type('Call the plumber');
    await fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    await screen.findByText(/Saved as/);

    const keys = vi.mocked(api.createQuickNote).mock.calls.map((call) => call[1]);
    expect(keys).toHaveLength(2);
    expect(keys[0]).not.toBe(keys[1]);
    expect(api.saveQuickNote).not.toHaveBeenCalled();
  });

  it('hands an expired session to the layout and keeps the draft', async () => {
    vi.mocked(api.createQuickNote).mockRejectedValueOnce(new AuthError());
    mount();
    await type('Buy milk');
    await fireEvent.click(screen.getByRole('button', { name: 'Done' }));

    await waitFor(() => expect(expireSession).toHaveBeenCalled());
    expect(draft()?.text).toBe('Buy milk');
  });
});
