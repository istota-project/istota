import { afterEach, describe, expect, it, vi } from 'vitest';
import { disconnectNextcloudToken, saveNextcloudSharing } from './api';

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: vi.fn(async () => body),
  } as unknown as Response;
}

afterEach(() => vi.unstubAllGlobals());

const LISTING = {
  bot: 'istota',
  kinds: {
    calendars: { ceiling: 'full', available: true },
    contacts: { ceiling: 'full', available: true },
  },
  collections: [{ id: 'cal-personal', kind: 'calendars', name: 'Personal', level: 'off' }],
};

describe('Nextcloud sharing API', () => {
  it('resolves a save where every change failed with its re-read rows', async () => {
    const failed = [{ id: 'cal-personal', name: 'Personal', reason: 'no longer exists' }];
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(502, { ...LISTING, failed })),
    );
    const result = await saveNextcloudSharing({ 'cal-personal': 'full' });
    expect(result.failed).toEqual(failed);
    expect(result.collections).toEqual(LISTING.collections);
  });

  it('throws a 502 that carries no listing', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(502, { error: 'Nextcloud is not reachable right now.' })),
    );
    await expect(saveNextcloudSharing({ 'cal-personal': 'full' })).rejects.toThrow(
      'Nextcloud is not reachable right now.',
    );
  });

  it('asks to unshare only when told to, and exposes what is still shared', async () => {
    const fetchMock = vi.fn(async (_url: string) =>
      jsonResponse(409, { error: 'Could not stop sharing.', still_shared: ['Personal'] }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const err = await disconnectNextcloudToken(true).catch((e) => e);
    expect(String(fetchMock.mock.calls[0][0])).toMatch(
      /\/settings\/nextcloud-token\?unshare=true$/,
    );
    expect(err.status).toBe(409);
    expect(err.body.still_shared).toEqual(['Personal']);

    fetchMock.mockImplementation(async (_url: string) =>
      jsonResponse(200, { ok: true, was_connected: true }),
    );
    await disconnectNextcloudToken();
    expect(String(fetchMock.mock.calls[1][0])).toMatch(/\/settings\/nextcloud-token$/);
  });
});
