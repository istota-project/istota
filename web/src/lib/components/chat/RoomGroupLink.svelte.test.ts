import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen, waitFor } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';

const mocks = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => mocks);
await fillApiDouble(mocks);

const { getRoomGroup, putRoomGroup } = mocks;

import RoomGroupLink from './RoomGroupLink.svelte';

const LABEL = 'Group this room is linked to';

function link(over: Record<string, unknown> = {}) {
  return {
    group_id: null,
    group_name: null,
    can_set: true,
    refusal: null,
    choices: [
      { group_id: 'fam', display_name: 'Family' },
      { group_id: 'club', display_name: 'Club' },
    ],
    ...over,
  };
}

/** bits-ui commits an item on pointerup, so a plain click selects nothing. */
async function pick(optionLabel: string) {
  const trigger = screen.getByRole('button', { name: LABEL });
  await fireEvent.pointerDown(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.pointerUp(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.click(trigger);
  const item = screen.getByText(optionLabel).closest('[data-select-item]');
  if (!item) throw new Error(`no option ${optionLabel}`);
  await fireEvent.pointerMove(item, { pointerType: 'mouse' });
  await fireEvent.pointerDown(item, { pointerType: 'mouse', button: 0 });
  await fireEvent.pointerUp(item, { pointerType: 'mouse', button: 0 });
}

beforeEach(() => {
  getRoomGroup.mockReset();
  putRoomGroup.mockReset();
});

afterEach(() => cleanup());

describe('RoomGroupLink', () => {
  it('links the room to the group the host picks', async () => {
    getRoomGroup.mockResolvedValue(link());
    putRoomGroup.mockResolvedValue(link({ group_id: 'fam', group_name: 'Family' }));
    render(RoomGroupLink, { roomId: 4 });
    await screen.findByText(/Link a group/);
    await pick('Family');
    expect(putRoomGroup).toHaveBeenCalledWith(4, 'fam');
    await screen.findByText(/carry this group's memory/);
  });

  it('unlinks with "No group"', async () => {
    getRoomGroup.mockResolvedValue(link({ group_id: 'fam', group_name: 'Family' }));
    putRoomGroup.mockResolvedValue(link());
    render(RoomGroupLink, { roomId: 4 });
    await screen.findByText(/carry this group's memory/);
    await pick('No group');
    expect(putRoomGroup).toHaveBeenCalledWith(4, null);
  });

  it('is read-only for a member who is not the host, and says why', async () => {
    getRoomGroup.mockResolvedValue(
      link({
        group_id: 'fam',
        group_name: 'Family',
        can_set: false,
        refusal: 'Only this room’s host (alice) can change its settings.',
        choices: [],
      }),
    );
    render(RoomGroupLink, { roomId: 4 });
    await screen.findByText(/host \(alice\)/);
    const trigger = screen.getByRole('button', { name: LABEL });
    expect(trigger.hasAttribute('disabled') || trigger.getAttribute('data-disabled') !== null).toBe(
      true,
    );
    expect(trigger.textContent).toContain('Family');
  });

  it('shows the server’s refusal and keeps the old link', async () => {
    getRoomGroup.mockResolvedValue(link());
    putRoomGroup.mockRejectedValue(new Error("You are not a member of group 'club'."));
    render(RoomGroupLink, { roomId: 4 });
    await screen.findByText(/Link a group/);
    await pick('Club');
    await waitFor(() => expect(screen.getByText(/not a member of group 'club'/)).toBeTruthy());
    expect(screen.getByText(/Link a group/)).toBeTruthy();
  });
});
