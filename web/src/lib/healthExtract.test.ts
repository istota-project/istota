import { afterEach, describe, expect, it, vi } from 'vitest';
import { extractEncounters, extractHealthPanel } from './api';

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: vi.fn(async () => body),
  } as unknown as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('health extraction hand-off', () => {
  it('posts, polls until the daemon answers, and returns the result', async () => {
    vi.useFakeTimers();
    const result = { rows: [{ encounter_date: '2026-01-02' }], warnings: [], document_id: 7 };
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(202, { id: 'abc' }))
      .mockResolvedValueOnce(jsonResponse(200, { state: 'pending' }))
      .mockResolvedValueOnce(jsonResponse(200, { state: 'answered', result }));
    vi.stubGlobal('fetch', fetch);

    const pending = extractEncounters(new File(['%PDF'], 'visit.pdf'));
    await vi.runAllTimersAsync();

    await expect(pending).resolves.toEqual(result);
    expect(fetch.mock.calls.map((c) => c[0])).toEqual([
      expect.stringMatching(/\/api\/health\/encounters\/extract$/),
      expect.stringMatching(/\/api\/health\/extract-requests\/abc$/),
      expect.stringMatching(/\/api\/health\/extract-requests\/abc$/),
    ]);
  });

  it('throws the error an answer carries', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(jsonResponse(202, { id: 'p1' }))
        .mockResolvedValueOnce(
          jsonResponse(200, { state: 'answered', result: { error: 'panel not found' } }),
        ),
    );
    await expect(extractHealthPanel(3)).rejects.toThrow('panel not found');
  });

  it('throws when the request is unknown', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(jsonResponse(202, { id: 'gone' }))
        .mockResolvedValueOnce(jsonResponse(200, { state: 'unknown' })),
    );
    await expect(extractHealthPanel(3)).rejects.toThrow('lost');
  });
});
