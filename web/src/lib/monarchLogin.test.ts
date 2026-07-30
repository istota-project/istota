import { describe, it, expect, vi, afterEach } from 'vitest';
import { monarchLogin } from './api';

/**
 * The login endpoint's contract lives in its response *body*, and the shared
 * `apiFetch` wrapper discards it — so `monarchLogin` reads the response
 * itself. These tests pin the two things that were wrong before:
 *
 * 1. A one-time-code challenge is a flow state, not an error. Monarch answers
 *    a *correct* password with 412 when it doesn't recognise the device, and
 *    reporting that as a failure told the user their password was wrong.
 * 2. A wrong Monarch password (401) must not be mistaken for an expired istota
 *    session — `apiFetch` throws `AuthError` on 401, which bounces the user to
 *    the istota login page.
 */

function mockResponse(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

afterEach(() => vi.unstubAllGlobals());

describe('monarchLogin', () => {
  it('reports success', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => mockResponse(200, { ok: true })),
    );
    expect(await monarchLogin('a@b.com', 'pw')).toEqual({ status: 'ok' });
  });

  it('surfaces an emailed-code challenge as a flow state, not an error', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        mockResponse(412, {
          detail: { code: 'email_otp_required', message: 'Check your email' },
        }),
      ),
    );

    const result = await monarchLogin('a@b.com', 'pw');

    expect(result).toEqual({
      status: 'challenge',
      kind: 'email_otp',
      message: 'Check your email',
    });
  });

  it('distinguishes an authenticator challenge from an emailed one', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => mockResponse(412, { detail: { code: 'mfa_required', message: 'MFA' } })),
    );

    const result = await monarchLogin('a@b.com', 'pw');

    expect(result.status).toBe('challenge');
    expect(result).toMatchObject({ kind: 'mfa' });
  });

  it('sends each code in its own field', async () => {
    let sent: RequestInit | undefined;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init?: RequestInit) => {
        sent = init;
        return mockResponse(200, { ok: true });
      }),
    );

    await monarchLogin('a@b.com', 'pw', { emailOtp: '820512' });

    const body = JSON.parse(String(sent?.body));
    expect(body.email_otp).toBe('820512');
    expect(body.mfa_totp).toBe('');
  });

  it('returns a bad password as an auth error rather than throwing', async () => {
    // Throwing here is what made a wrong Monarch password log the user out of
    // istota, via apiFetch's blanket 401 -> AuthError rule.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => mockResponse(401, { detail: 'Invalid email and password' })),
    );

    const result = await monarchLogin('a@b.com', 'wrong');

    expect(result).toEqual({
      status: 'error',
      kind: 'auth',
      message: 'Invalid email and password',
    });
  });

  it('separates the environmental 503s the user cannot act on', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => mockResponse(503, { detail: 'blocked by Cloudflare' })),
    );
    expect(await monarchLogin('a@b.com', 'pw')).toMatchObject({ kind: 'cloudflare' });

    vi.stubGlobal(
      'fetch',
      vi.fn(async () => mockResponse(503, { detail: 'CAPTCHA required' })),
    );
    expect(await monarchLogin('a@b.com', 'pw')).toMatchObject({ kind: 'captcha' });
  });

  it('still yields a usable message when the body is not JSON', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 502,
        json: async () => {
          throw new Error('not json');
        },
      })),
    );

    const result = await monarchLogin('a@b.com', 'pw');

    expect(result.status).toBe('error');
    expect(result).toMatchObject({ kind: 'other' });
  });
});
