import { describe, it, expect, vi, afterEach } from 'vitest';
import { sendChatMessage } from './api';

/**
 * `sendChatMessage` is the one call in `api.ts` that classifies its failures
 * instead of throwing (ISSUE-200).
 *
 * That divergence is deliberate and load-bearing, so it is pinned here rather
 * than left to the store tests — which mock this module wholesale and so assert
 * against a result shape they construct themselves. What used to happen: a
 * rejection escaped an un-awaited caller, the composer stayed locked in Stop
 * mode, and nothing appeared on screen at all.
 */

function mockResponse(status: number, body: unknown, headers: Record<string, string> = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (k: string) => headers[k] ?? null },
    json: async () => body,
  } as unknown as Response;
}

afterEach(() => vi.unstubAllGlobals());

describe('sendChatMessage', () => {
  it('returns the task id and the numeric HTTP status on success', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => mockResponse(200, { task_id: 7, status: 'pending' })),
    );
    const res = await sendChatMessage(1, 'hello');
    expect(res.ok).toBe(true);
    expect(res.task_id).toBe(7);
    // The payload carries its own `status` ("pending"); the numeric one wins,
    // because that is what this type promises and what a failure sentence reads.
    expect(res.status).toBe(200);
  });

  it('classifies an unreachable server rather than throwing', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('Failed to fetch');
      }),
    );
    await expect(sendChatMessage(1, 'hello')).resolves.toEqual({
      ok: false,
      status: 0,
      failure: 'unreachable',
    });
  });

  it('distinguishes its own timeout from an unreachable server', async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      'fetch',
      vi.fn(
        (_url: string, init: RequestInit) =>
          new Promise((_res, rej) => {
            init.signal?.addEventListener('abort', () => rej(new DOMException('', 'AbortError')));
          }),
      ),
    );
    const p = sendChatMessage(1, 'hello', [], [], 50);
    await vi.advanceTimersByTimeAsync(60);
    await expect(p).resolves.toEqual({ ok: false, status: 0, failure: 'timeout' });
    vi.useRealTimers();
  });

  it('bounds the body read too, not just the response headers', async () => {
    // A proxy that flushes headers and then stalls is the same hang the bound
    // exists for; clearing the timer on the headers alone would leave it open.
    vi.useFakeTimers();
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init: RequestInit) => ({
        ok: true,
        status: 200,
        headers: { get: () => null },
        json: () =>
          new Promise((_res, rej) => {
            init.signal?.addEventListener('abort', () => rej(new DOMException('', 'AbortError')));
          }),
      })),
    );
    const p = sendChatMessage(1, 'hello', [], [], 50);
    await vi.advanceTimersByTimeAsync(60);
    await expect(p).resolves.toEqual({ ok: false, status: 0, failure: 'timeout' });
    vi.useRealTimers();
  });

  it('returns an expired session instead of throwing AuthError', async () => {
    // Deliberately unlike the other 401 sites in this file: only `getMe` in the
    // root layout catches AuthError, so from here a throw was silence.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => mockResponse(401, {})),
    );
    await expect(sendChatMessage(1, 'hello')).resolves.toEqual({
      ok: false,
      status: 401,
      failure: 'auth',
    });
  });

  it('carries the rate-limit wait through', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => mockResponse(429, {}, { 'Retry-After': '90' })),
    );
    const res = await sendChatMessage(1, 'hello');
    expect(res).toEqual({ ok: false, status: 429, failure: 'rate_limit', retry_after: 90 });
  });

  it('falls back to a usable wait when Retry-After is not seconds', async () => {
    // RFC 9110 permits an HTTP-date, and an intermediary is under no obligation
    // to send the seconds form — "wait NaNs and try again" is now a sentence
    // that would sit permanently on the message row.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => mockResponse(429, {}, { 'Retry-After': 'Wed, 21 Oct 2026 07:28:00 GMT' })),
    );
    expect((await sendChatMessage(1, 'hello')).retry_after).toBe(60);
  });

  it('surfaces the server’s own reason for a rejection', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => mockResponse(409, { error: 'room is archived' })),
    );
    await expect(sendChatMessage(1, 'hello')).resolves.toEqual({
      ok: false,
      status: 409,
      failure: 'rejected',
      error: 'room is archived',
    });
  });

  it('survives an error response with no parseable body', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 502,
        headers: { get: () => null },
        json: async () => {
          throw new SyntaxError('not json');
        },
      })),
    );
    // An nginx error page is HTML, and a 502 is precisely when one arrives.
    const res = await sendChatMessage(1, 'hello');
    expect(res.failure).toBe('rejected');
    expect(res.status).toBe(502);
  });

  // `resp.json()` resolves — it does not throw — for a body that is the
  // literal `null` or a JSON array, so the parse guard never fired and reading
  // `.error` off the result threw a TypeError that escaped to the outer catch.
  // An ordinary 400 was then reported to the user as an unreachable server.
  it.each([
    ['the literal null', null],
    ['a JSON array', [{ error: 'nope' }]],
  ])('classifies a 400 whose body is %s as a rejection', async (_label, body) => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => mockResponse(400, body)),
    );
    const res = await sendChatMessage(1, 'hello');
    expect(res.failure).toBe('rejected');
    expect(res.status).toBe(400);
    expect(res.error).toBe('error 400');
  });

  /** A fetch stub that records the JSON body it was handed. */
  function captureBody() {
    const sent: Record<string, unknown>[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init: RequestInit) => {
        sent.push(JSON.parse(String(init.body)));
        return mockResponse(200, { task_id: 7 });
      }),
    );
    return sent;
  }

  it('carries an idempotency key in the body when one is supplied', async () => {
    const sent = captureBody();
    await sendChatMessage(1, 'hello', [], [], undefined, 'abc-123');
    expect(sent[0]).toMatchObject({ client_msg_id: 'abc-123' });
  });

  it('omits the field entirely when there is no key', async () => {
    const sent = captureBody();
    await sendChatMessage(1, 'hello');
    expect(sent[0]).not.toHaveProperty('client_msg_id');
  });
});
