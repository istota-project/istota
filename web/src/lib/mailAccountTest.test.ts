import { afterEach, expect, it, vi } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
import { POLL_MS, WAIT_MS, waitForMailAccountTest } from './mailAccountTest';

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

const answer = { ok: true, message: 'Signed in.', lines: [] };

it('polls every two seconds until the daemon answers', async () => {
  vi.useFakeTimers();
  api.requestMailAccountTest.mockResolvedValue({ request_id: 'abc' });
  api.getMailAccountTest
    .mockResolvedValueOnce({ status: 'pending' })
    .mockResolvedValue({ status: 'answered', result: answer });
  const pending = waitForMailAccountTest(12);
  await vi.advanceTimersByTimeAsync(POLL_MS);
  expect(api.getMailAccountTest).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(POLL_MS);
  await expect(pending).resolves.toEqual(answer);
  expect(api.requestMailAccountTest).toHaveBeenCalledWith(12);
  expect(api.getMailAccountTest).toHaveBeenCalledWith('abc');
  expect(POLL_MS).toBe(2000);
});

it('gives up with null after two minutes without an answer', async () => {
  vi.useFakeTimers();
  api.requestMailAccountTest.mockResolvedValue({ request_id: 'abc' });
  api.getMailAccountTest.mockResolvedValue({ status: 'pending' });
  const pending = waitForMailAccountTest(12);
  await vi.advanceTimersByTimeAsync(WAIT_MS + POLL_MS);
  await expect(pending).resolves.toBeNull();
  expect(WAIT_MS).toBe(120_000);
});

it('fails on a request the server no longer knows', async () => {
  vi.useFakeTimers();
  api.requestMailAccountTest.mockResolvedValue({ request_id: 'abc' });
  api.getMailAccountTest.mockResolvedValue({ status: 'unknown' });
  const pending = waitForMailAccountTest(12);
  const settled = expect(pending).rejects.toThrow('The test request was lost');
  await vi.advanceTimersByTimeAsync(POLL_MS);
  await settled;
});
