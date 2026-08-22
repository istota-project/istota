import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';

// Hoisted with the `vi.mock` factory, so the component and the test share one
// class object — `instanceof` in the component only matches if they do, and a
// separately declared stub would send every failure down the generic branch.
const mocks = vi.hoisted(() => {
  class ChatMemoryConflictError extends Error {
    constructor() {
      super('channel memory changed since it was loaded');
      this.name = 'ChatMemoryConflictError';
    }
  }
  class ChatMemoryBusyError extends Error {
    constructor(message: string) {
      super(message);
      this.name = 'ChatMemoryBusyError';
    }
  }
  return {
    getRoomMemory: vi.fn(),
    saveRoomMemory: vi.fn(),
    ChatMemoryConflictError,
    ChatMemoryBusyError,
  };
});

const { getRoomMemory, saveRoomMemory, ChatMemoryConflictError, ChatMemoryBusyError } = mocks;

vi.mock('$lib/api', () => mocks);

import RoomMemory from './RoomMemory.svelte';

const TEMPLATE = '# Channel Memory\n\n## Notes\n\n';

function loaded(overrides: Record<string, unknown> = {}) {
  return {
    room_id: 1,
    token: 'web-alice-abc',
    content: '# Channel Memory\n\nAnswer briefly.\n',
    exists: true,
    shared: false,
    template: TEMPLATE,
    revision: 'rev-1',
    ...overrides,
  };
}

function mount(props: Record<string, unknown> = {}) {
  return render(RoomMemory, {
    open: true,
    roomId: 1,
    roomName: 'general',
    onClose: vi.fn(),
    ...props,
  });
}

function textarea(): HTMLTextAreaElement {
  const el = document.querySelector('textarea');
  if (!el) throw new Error('no textarea rendered');
  return el as HTMLTextAreaElement;
}

function buttonNamed(label: string): HTMLButtonElement {
  const match = [...document.querySelectorAll('button')].find(
    (b) => b.textContent?.trim() === label,
  );
  if (!match) throw new Error(`no button labelled ${label}`);
  return match as HTMLButtonElement;
}

beforeEach(() => {
  getRoomMemory.mockReset();
  saveRoomMemory.mockReset();
});

afterEach(cleanup);

describe('RoomMemory', () => {
  it('loads the file into the editor', async () => {
    getRoomMemory.mockResolvedValue(loaded());
    mount();
    await waitFor(() => expect(textarea().value).toContain('Answer briefly.'));
    expect(getRoomMemory).toHaveBeenCalledWith(1);
  });

  it('offers the template only while the file is empty and untouched', async () => {
    getRoomMemory.mockResolvedValue(loaded({ content: '', exists: false }));
    mount();
    await waitFor(() => expect(textarea().value).toBe(''));

    await fireEvent.click(buttonNamed('Start from template'));
    expect(textarea().value).toBe(TEMPLATE);
    // Once there is text to lose, the offer withdraws itself.
    await waitFor(() =>
      expect(
        [...document.querySelectorAll('button')].some(
          (b) => b.textContent?.trim() === 'Start from template',
        ),
      ).toBe(false),
    );
  });

  it('save is disabled until the text changes, and sends the loaded revision', async () => {
    getRoomMemory.mockResolvedValue(loaded());
    saveRoomMemory.mockResolvedValue({ status: 'ok', revision: 'rev-2' });
    mount();
    await waitFor(() => expect(textarea().value).toContain('Answer briefly.'));
    expect(buttonNamed('Save').disabled).toBe(true);

    await fireEvent.input(textarea(), { target: { value: 'new text' } });
    expect(buttonNamed('Save').disabled).toBe(false);

    await fireEvent.click(buttonNamed('Save'));
    await waitFor(() => expect(saveRoomMemory).toHaveBeenCalledWith(1, 'new text', 'rev-1'));
  });

  it('adopts the returned revision so a second save is not a false conflict', async () => {
    getRoomMemory.mockResolvedValue(loaded());
    saveRoomMemory.mockResolvedValue({ status: 'ok', revision: 'rev-2' });
    mount();
    await waitFor(() => expect(textarea().value).toContain('Answer briefly.'));

    await fireEvent.input(textarea(), { target: { value: 'first' } });
    await fireEvent.click(buttonNamed('Save'));
    await waitFor(() => expect(saveRoomMemory).toHaveBeenCalledTimes(1));

    await fireEvent.input(textarea(), { target: { value: 'second' } });
    await fireEvent.click(buttonNamed('Save'));
    await waitFor(() => expect(saveRoomMemory).toHaveBeenLastCalledWith(1, 'second', 'rev-2'));
  });

  it('a conflict keeps the typed text and offers a reload', async () => {
    getRoomMemory.mockResolvedValue(loaded());
    saveRoomMemory.mockRejectedValue(new ChatMemoryConflictError());
    mount();
    await waitFor(() => expect(textarea().value).toContain('Answer briefly.'));

    await fireEvent.input(textarea(), { target: { value: 'mine' } });
    await fireEvent.click(buttonNamed('Save'));

    await waitFor(() =>
      expect(document.body.textContent).toContain('changed this file while you were editing'),
    );
    // The whole point of the conflict branch: the user's work survives it.
    expect(textarea().value).toBe('mine');

    getRoomMemory.mockResolvedValue(loaded({ content: 'theirs', revision: 'rev-9' }));
    await fireEvent.click(buttonNamed('Discard mine and reload'));
    await waitFor(() => expect(textarea().value).toBe('theirs'));
  });

  it('reports a busy room without losing the text', async () => {
    getRoomMemory.mockResolvedValue(loaded());
    saveRoomMemory.mockRejectedValue(new ChatMemoryBusyError('room has a task in progress'));
    mount();
    await waitFor(() => expect(textarea().value).toContain('Answer briefly.'));

    await fireEvent.input(textarea(), { target: { value: 'mine' } });
    await fireEvent.click(buttonNamed('Save'));

    await waitFor(() =>
      expect(document.body.textContent).toContain("can't be saved while the room is working"),
    );
    expect(textarea().value).toBe('mine');
  });

  it('a failed load never shows the previous room under the new title', async () => {
    getRoomMemory.mockResolvedValueOnce(loaded({ content: 'room A secrets' }));
    const { rerender } = mount();
    await waitFor(() => expect(textarea().value).toBe('room A secrets'));

    getRoomMemory.mockRejectedValueOnce(new Error('network'));
    await rerender({ open: true, roomId: 2, roomName: 'other', onClose: vi.fn() });

    await waitFor(() => expect(document.body.textContent).toContain("Couldn't load"));
    // Room A's text must be gone: rendered under room B it is one Save away
    // from being written into room B, and when both files are empty the
    // revisions match so the server would accept it.
    expect(document.querySelector('textarea')).toBeNull();
    expect(document.body.textContent).not.toContain('room A secrets');
  });

  it('refetches on every open, not only on a room change', async () => {
    getRoomMemory.mockResolvedValue(loaded());
    const { rerender } = mount();
    await waitFor(() => expect(getRoomMemory).toHaveBeenCalledTimes(1));

    await rerender({ open: false, roomId: 1, roomName: 'general', onClose: vi.fn() });
    await rerender({ open: true, roomId: 1, roomName: 'general', onClose: vi.fn() });

    // The agent writes this file between visits, so a cached buffer carries a
    // stale revision and the next save blames the assistant for a write that
    // predates the session.
    await waitFor(() => expect(getRoomMemory).toHaveBeenCalledTimes(2));
  });

  it('says so when the file is shared across a room', async () => {
    getRoomMemory.mockResolvedValue(loaded({ shared: true }));
    mount();
    await waitFor(() => expect(document.body.textContent).toContain('This room is shared'));
  });

  it('a private room says nothing about sharing', async () => {
    getRoomMemory.mockResolvedValue(loaded());
    mount();
    await waitFor(() => expect(textarea().value).toContain('Answer briefly.'));
    expect(document.body.textContent).not.toContain('This room is shared');
  });
});
