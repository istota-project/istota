import { afterEach, expect, it, vi } from 'vitest';
import {
  getGroupMemory,
  saveGroupMemory,
  putGroupMember,
  getGroupMemoryActivity,
  GroupApiError,
  TextFileConflictError,
} from './api';
afterEach(() => vi.unstubAllGlobals());
it('keeps read refusals as errors without content or revision', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          error: 'memory_unreadable',
          message: 'Group memory cannot be read',
          reason: 'storage_unavailable',
        }),
        { status: 503 },
      ),
    ),
  );
  await expect(getGroupMemory('team')).rejects.toMatchObject({
    status: 503,
    code: 'memory_unreadable',
    reason: 'storage_unavailable',
  });
});
it('maps notebook conflicts to the shared editor error', async () => {
  vi.stubGlobal(
    'fetch',
    vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ error: 'memory_conflict' }), { status: 409 }),
      ),
  );
  await expect(saveGroupMemory('team', 'draft', 'r1')).rejects.toBeInstanceOf(
    TextFileConflictError,
  );
});
it('preserves membership disclosure and encodes identities and revisions', async () => {
  const fetch = vi.fn().mockResolvedValue(
    new Response(
      JSON.stringify({
        error: 'history_acknowledgement_required',
        message: 'Acknowledge history',
        disclosure: { memory: 'GROUP.md', rooms: [], briefings: [] },
      }),
      { status: 409 },
    ),
  );
  vi.stubGlobal('fetch', fetch);
  await expect(
    putGroupMember('a b', 'a/b', { revision: 7, role: 'member' }),
  ).rejects.toBeInstanceOf(GroupApiError);
  expect(fetch).toHaveBeenCalledWith(
    '/api/groups/a%20b/members/a%2Fb',
    expect.objectContaining({
      method: 'PUT',
      credentials: 'same-origin',
      body: JSON.stringify({ revision: 7, role: 'member' }),
    }),
  );
});
it('encodes the opaque activity cursor', async () => {
  const fetch = vi
    .fn()
    .mockResolvedValue(new Response(JSON.stringify({ entries: [], next_cursor: null })));
  vi.stubGlobal('fetch', fetch);
  await getGroupMemoryActivity('team', 'a&b');
  expect(fetch).toHaveBeenCalledWith(
    '/api/groups/team/memory/activity?cursor=a%26b',
    expect.anything(),
  );
});
