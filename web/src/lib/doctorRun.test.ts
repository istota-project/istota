import { afterEach, expect, it, vi } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api, { getAdminDoctor: vi.fn(), requestDoctorRun: vi.fn() });
import { POLL_MS, WAIT_MS, waitForDoctorRun } from './doctorRun';

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

function report(answered: { id: string; deep: boolean } | null) {
  return {
    status: 'ok',
    summary: { ok: 1, warn: 0, fail: 0, skip: 0 },
    deep: false,
    checks: [],
    checked_at: '2026-10-12T03:00:00Z',
    request_id: null,
    deep_checked_at: null,
    deep_request_id: null,
    pending: null,
    answered,
  };
}

it('resolves once the daemon answers the request it made, passing each report on', async () => {
  vi.useFakeTimers();
  // A joined request may be deeper than the one asked for; the result says so.
  api.requestDoctorRun.mockResolvedValue({ id: 'r1', deep: true });
  api.getAdminDoctor
    .mockResolvedValueOnce(report({ id: 'r0', deep: false }))
    .mockResolvedValue(report({ id: 'r1', deep: true }));
  const seen: unknown[] = [];
  const pending = waitForDoctorRun(false, (r) => seen.push(r.answered));
  await vi.advanceTimersByTimeAsync(POLL_MS * 2);
  const result = await pending;
  expect(api.requestDoctorRun).toHaveBeenCalledWith(false);
  expect(result.answered).toBe(true);
  expect(result.deep).toBe(true);
  expect(result.report?.answered).toEqual({ id: 'r1', deep: true });
  expect(seen).toEqual([
    { id: 'r0', deep: false },
    { id: 'r1', deep: true },
  ]);
});

it('gives up after the wait with the last report it read', async () => {
  vi.useFakeTimers();
  api.requestDoctorRun.mockResolvedValue({ id: 'r2', deep: false });
  api.getAdminDoctor.mockResolvedValue(report(null));
  const pending = waitForDoctorRun(false);
  await vi.advanceTimersByTimeAsync(WAIT_MS + POLL_MS);
  const result = await pending;
  expect(result.answered).toBe(false);
  expect(result.report?.answered).toBeNull();
});

it('lets a failed request propagate', async () => {
  api.requestDoctorRun.mockRejectedValue(new Error('403'));
  await expect(waitForDoctorRun(true)).rejects.toThrow('403');
  expect(api.getAdminDoctor).not.toHaveBeenCalled();
});
