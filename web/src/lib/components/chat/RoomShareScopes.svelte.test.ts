import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen, waitFor } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';

const mocks = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => mocks);
await fillApiDouble(mocks);

const { getRoomGrants, putRoomGrants } = mocks;

import RoomShareScopes from './RoomShareScopes.svelte';

function grants(granted: string[], state = 'active') {
  return {
    scopes: ['calendar', 'files', 'memory'].map((name) => ({
      name,
      granted: granted.includes(name),
    })),
    state,
  };
}

function box(name: string): HTMLInputElement {
  const label = screen.getByText(name).closest('label');
  const input = label?.querySelector('input');
  if (!input) throw new Error(`no toggle for ${name}`);
  return input as HTMLInputElement;
}

beforeEach(() => {
  getRoomGrants.mockReset();
  putRoomGrants.mockReset();
});

afterEach(() => cleanup());

describe('RoomShareScopes', () => {
  it('shows each scope with what the caller has granted', async () => {
    getRoomGrants.mockResolvedValue(grants(['files']));
    render(RoomShareScopes, { roomId: 3 });
    await screen.findByText('calendar');
    expect(box('files').checked).toBe(true);
    expect(box('calendar').checked).toBe(false);
    expect(screen.getByText(/everyone in the room reads those answers/)).toBeTruthy();
  });

  it('sends the whole new set, and shows it before the server answers', async () => {
    getRoomGrants.mockResolvedValue(grants(['files']));
    let answer: (v: unknown) => void = () => {};
    putRoomGrants.mockReturnValue(new Promise((resolve) => (answer = resolve)));
    render(RoomShareScopes, { roomId: 3 });
    await screen.findByText('calendar');
    await fireEvent.click(box('calendar'));
    expect(putRoomGrants).toHaveBeenCalledWith(3, ['calendar', 'files']);
    expect(box('calendar').checked).toBe(true);
    answer(grants(['calendar', 'files']));
    await waitFor(() => expect(box('calendar').disabled).toBe(false));
    expect(box('calendar').checked).toBe(true);
  });

  it('puts the toggle back when the server refuses', async () => {
    getRoomGrants.mockResolvedValue(grants([]));
    putRoomGrants.mockRejectedValue(new Error('unknown scope: calendar'));
    render(RoomShareScopes, { roomId: 3 });
    await screen.findByText('calendar');
    await fireEvent.click(box('calendar'));
    expect(await screen.findByText('unknown scope: calendar')).toBeTruthy();
    expect(box('calendar').checked).toBe(false);
  });

  it('says a grant does nothing while a guest reads the room', async () => {
    getRoomGrants.mockResolvedValue(grants(['files'], 'guests_present'));
    render(RoomShareScopes, { roomId: 3 });
    expect(await screen.findByText(/A guest reads this room/)).toBeTruthy();
  });
});
