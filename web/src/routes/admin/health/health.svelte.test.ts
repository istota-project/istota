import { afterEach, expect, it, vi } from 'vitest';
import { render, cleanup, screen, fireEvent } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api, { getAdminDoctor: vi.fn(), requestDoctorRun: vi.fn() });
import Page from './+page.svelte';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
});

function report(answered: { id: string; deep: boolean } | null, requestId: string | null = null) {
  return {
    status: 'ok',
    summary: { ok: 1, warn: 0, fail: 0, skip: 0 },
    deep: false,
    checks: [
      { name: 'runtime.platform', status: 'ok', detail: 'Linux', remedy: '', scope: 'image' },
    ],
    checked_at: '2026-10-12T03:00:00Z',
    request_id: requestId,
    deep_checked_at: null,
    deep_request_id: null,
    pending: null,
    answered,
  };
}

it('waits for the daemon to answer the request it made, not for a report', async () => {
  vi.useFakeTimers();
  api.getAdminDoctor.mockResolvedValue(report(null));
  api.requestDoctorRun.mockResolvedValue({ id: 'r1', deep: true });
  render(Page);
  await vi.advanceTimersByTimeAsync(0);
  // A sweep finished after the request, so the report names no request; only
  // the daemon's answered marker says the run is done.
  api.getAdminDoctor
    .mockResolvedValueOnce(report(null))
    .mockResolvedValue(report({ id: 'r1', deep: true }, null));
  await fireEvent.click(screen.getByRole('button', { name: 'Run deep checks' }));
  await vi.advanceTimersByTimeAsync(1500);
  expect(screen.queryByText(/Deep checks included/)).toBeNull();
  await vi.advanceTimersByTimeAsync(1500);
  expect(screen.getByText(/Deep checks included/)).toBeTruthy();
  expect(api.requestDoctorRun).toHaveBeenCalledWith(true);
});

it('says so when the daemon never answers', async () => {
  vi.useFakeTimers();
  api.getAdminDoctor.mockResolvedValue(report(null));
  api.requestDoctorRun.mockResolvedValue({ id: 'r2', deep: false });
  render(Page);
  await vi.advanceTimersByTimeAsync(0);
  await fireEvent.click(screen.getByRole('button', { name: 'Re-run' }));
  await vi.advanceTimersByTimeAsync(125_000);
  expect(screen.getByText(/has not answered the request yet/)).toBeTruthy();
});
