import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

import { getRoomMembers } from '$lib/api';
import { MEMBERS_TTL_MS, dropRoomMembers, loadRoomMembers } from './roomMembers';

const fetchMembers = getRoomMembers as ReturnType<typeof vi.fn>;
const listing = (ids: string[]) => ({
  members: ids.map((user_id) => ({ user_id, display_name: user_id, is_owner: false })),
  can_manage: false,
  message_count: 0,
});

beforeEach(() => {
  dropRoomMembers(1);
  fetchMembers.mockReset();
});

describe('loadRoomMembers', () => {
  it('answers from memory within the lifetime and refetches past it', async () => {
    fetchMembers.mockResolvedValueOnce(listing(['alice'])).mockResolvedValueOnce(listing(['bob']));
    const first = await loadRoomMembers(1, 1000);
    expect(await loadRoomMembers(1, 1000 + MEMBERS_TTL_MS - 1)).toBe(first);
    expect(fetchMembers).toHaveBeenCalledTimes(1);
    // Past it, a member added elsewhere shows up.
    const later = await loadRoomMembers(1, 1000 + MEMBERS_TTL_MS);
    expect(later.map((m) => m.user_id)).toEqual(['bob']);
    expect(fetchMembers).toHaveBeenCalledTimes(2);
  });

  it('answers a failure as empty and retries once the lifetime is up', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    fetchMembers.mockRejectedValueOnce(new Error('blip')).mockResolvedValueOnce(listing(['alice']));
    expect(await loadRoomMembers(1, 0)).toEqual([]);
    expect((await loadRoomMembers(1, MEMBERS_TTL_MS)).map((m) => m.user_id)).toEqual(['alice']);
  });

  it('refetches at once after this viewer changes the membership', async () => {
    fetchMembers.mockResolvedValue(listing(['alice']));
    await loadRoomMembers(1, 0);
    dropRoomMembers(1);
    await loadRoomMembers(1, 1);
    expect(fetchMembers).toHaveBeenCalledTimes(2);
  });
});
