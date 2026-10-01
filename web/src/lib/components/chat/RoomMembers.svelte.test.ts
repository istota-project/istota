import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen, waitFor } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';

const mocks = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => mocks);
await fillApiDouble(mocks);

const { getRoomMembers, getChatUsers, addRoomMember, removeRoomMember } = mocks;

import RoomMembers from './RoomMembers.svelte';

const ALICE = { user_id: 'alice', display_name: 'Alice', is_owner: true };
const BOB = { user_id: 'bob', display_name: 'Bob', is_owner: false };

function listing(overrides: Record<string, unknown> = {}) {
  return { members: [ALICE], can_manage: true, message_count: 412, ...overrides };
}

/** bits-ui's select commits on pointerup, not on click. */
async function pick(ariaLabel: string, optionLabel: string) {
  const trigger = screen.getByRole('button', { name: ariaLabel });
  await fireEvent.pointerDown(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.pointerUp(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.click(trigger);
  const item = screen.getByText(optionLabel).closest('[data-select-item]');
  if (!item) throw new Error(`no option ${optionLabel}`);
  await fireEvent.pointerMove(item, { pointerType: 'mouse' });
  await fireEvent.pointerDown(item, { pointerType: 'mouse', button: 0 });
  await fireEvent.pointerUp(item, { pointerType: 'mouse', button: 0 });
}

function button(label: string): HTMLButtonElement {
  const found = [...document.querySelectorAll('button')].find(
    (b) => b.textContent?.trim() === label,
  );
  if (!found) throw new Error(`no button ${label}`);
  return found as HTMLButtonElement;
}

beforeEach(() => {
  getRoomMembers.mockReset();
  getChatUsers.mockReset();
  addRoomMember.mockReset();
  removeRoomMember.mockReset();
  getChatUsers.mockResolvedValue({
    users: [
      { user_id: 'alice', display_name: 'Alice' },
      { user_id: 'bob', display_name: 'Bob' },
    ],
  });
});

afterEach(() => cleanup());

describe('RoomMembers', () => {
  it('lists the members and offers only people not already in the room', async () => {
    getRoomMembers.mockResolvedValue(listing());
    render(RoomMembers, { roomId: 1, userId: 'alice' });
    await screen.findByText('Alice');
    expect(screen.getByText('created the room')).toBeTruthy();
    await pick('Member to add', 'Bob');
    expect(screen.queryAllByText('Alice')).toHaveLength(1);
  });

  it('states how much history an add discloses, and adds only on confirm', async () => {
    getRoomMembers.mockResolvedValue(listing());
    addRoomMember.mockResolvedValue({ member: BOB });
    const onChanged = vi.fn();
    render(RoomMembers, { roomId: 1, userId: 'alice', onChanged });
    await screen.findByText('Alice');
    await pick('Member to add', 'Bob');
    await fireEvent.click(button('Add'));
    // The count comes from the server, and nothing is sent until confirmed.
    expect(await screen.findByText(/Bob will see all 412 messages/)).toBeTruthy();
    expect(addRoomMember).not.toHaveBeenCalled();
    getRoomMembers.mockResolvedValue(listing({ members: [ALICE, BOB] }));
    await fireEvent.click(button('Add and share the history'));
    await waitFor(() => expect(addRoomMember).toHaveBeenCalledWith(1, 'bob'));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(await screen.findByText('Bob')).toBeTruthy();
  });

  it('lets the creator remove a member', async () => {
    getRoomMembers.mockResolvedValue(listing({ members: [ALICE, BOB] }));
    removeRoomMember.mockResolvedValue(undefined);
    render(RoomMembers, { roomId: 1, userId: 'alice' });
    await screen.findByText('Bob');
    await fireEvent.click(button('Remove'));
    await waitFor(() => expect(removeRoomMember).toHaveBeenCalledWith(1, 'bob'));
  });

  it('offers a member only their own Leave, and no add', async () => {
    getRoomMembers.mockResolvedValue(listing({ members: [ALICE, BOB], can_manage: false }));
    render(RoomMembers, { roomId: 1, userId: 'bob' });
    await screen.findByText('Bob');
    expect(button('Leave')).toBeTruthy();
    expect(screen.queryByText('Remove')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Member to add' })).toBeNull();
    expect(getChatUsers).not.toHaveBeenCalled();
  });

  it('says a Talk room is managed in Talk, and offers no change', async () => {
    getRoomMembers.mockResolvedValue(listing({ members: [ALICE, BOB], can_manage: false }));
    render(RoomMembers, { roomId: 1, userId: 'bob', talkBound: true });
    expect(await screen.findByText(/changed in Talk/)).toBeTruthy();
    expect(screen.queryByText('Leave')).toBeNull();
  });

  it('reports a refused change beside the list', async () => {
    getRoomMembers.mockResolvedValue(listing({ members: [ALICE, BOB] }));
    removeRoomMember.mockRejectedValue(new Error('member has a task in progress'));
    render(RoomMembers, { roomId: 1, userId: 'alice' });
    await screen.findByText('Bob');
    await fireEvent.click(button('Remove'));
    expect(await screen.findByText('member has a task in progress')).toBeTruthy();
  });
});
