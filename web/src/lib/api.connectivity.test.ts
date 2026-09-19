/**
 * What the API layer tells the connectivity store.
 *
 * Through the real seam rather than a mocked one: the value of this wiring is
 * that `apiFetch` sees every request in the app and `sendChatMessage` sees the
 * one that matters most, so what is worth testing is the classification each
 * makes from a real `fetch` outcome — a rejection, an abort, a 500, a 401.
 * Mocking `noteTransport` and asserting it was called would only restate the
 * call sites.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { get } from 'svelte/store';

type Api = typeof import('./api');
type Connectivity = typeof import('./stores/connectivity');

let api: Api;
let connectivity: Connectivity;
const fetchMock = vi.fn();

function jsonResponse(status: number, body: unknown = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(),
    json: async () => body,
  } as unknown as Response;
}

beforeEach(async () => {
  vi.resetModules();
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
  // Imported together so both modules come from the same fresh graph — the
  // store is module state, and a stale copy would be reporting on nothing.
  api = await import('./api');
  connectivity = await import('./stores/connectivity');
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('apiFetch', () => {
  it('reports a reachable server on a plain success', async () => {
    connectivity.noteTransport(false, 'unreachable');
    fetchMock.mockResolvedValue(jsonResponse(200, { username: 'ada' }));

    await api.getMe();
    expect(get(connectivity.online)).toBe(true);
  });

  it('reports a reachable server on a 500, which the caller still sees as an error', async () => {
    connectivity.noteTransport(false, 'unreachable');
    fetchMock.mockResolvedValue(jsonResponse(500));

    await expect(api.getMe()).rejects.toThrow();
    expect(get(connectivity.online)).toBe(true);
  });

  it('reports a reachable server on a 401', async () => {
    // The session is gone, which is the opposite of a network gap: an expired
    // session offered as "you are offline" would send the user looking at their
    // signal instead of signing in.
    connectivity.noteTransport(false, 'unreachable');
    fetchMock.mockResolvedValue(jsonResponse(401));

    await expect(api.getMe()).rejects.toBeInstanceOf(api.AuthError);
    expect(get(connectivity.online)).toBe(true);
  });

  it('reports a gap when fetch rejects', async () => {
    fetchMock.mockRejectedValue(new TypeError('Load failed'));

    await expect(api.getMe()).rejects.toThrow();
    expect(get(connectivity.online)).toBe(false);
  });

  it('reports a gap when nothing answers inside its own timeout', async () => {
    fetchMock.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted')));
        }),
    );

    vi.useFakeTimers();
    try {
      const pending = api.getChatRooms(5_000);
      const settled = expect(pending).rejects.toThrow();
      await vi.advanceTimersByTimeAsync(5_000);
      await settled;
    } finally {
      vi.useRealTimers();
    }
    expect(get(connectivity.online)).toBe(false);
  });

  it('reports a gap when the headers arrive and the body then stalls', async () => {
    // The half-delivered answer, and the one that used to pass for a reachable
    // server: a proxy flushes the headers and holds the socket. The store must
    // not clear the banner on that, because the probe rides on this call — a
    // stall answering as "reachable" would take the banner down and cancel the
    // schedule that was going to correct it.
    fetchMock.mockImplementation((_url: string, init: RequestInit) =>
      Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers(),
        json: () =>
          new Promise((_resolve, reject) => {
            init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted')));
          }),
      } as unknown as Response),
    );

    vi.useFakeTimers();
    try {
      const pending = api.getChatRooms(5_000);
      const settled = expect(pending).rejects.toThrow();
      await vi.advanceTimersByTimeAsync(5_000);
      await settled;
    } finally {
      vi.useRealTimers();
    }
    expect(get(connectivity.online)).toBe(false);
  });
});

describe('sendChatMessage', () => {
  it('reports a gap on a network rejection', async () => {
    fetchMock.mockRejectedValue(new TypeError('Load failed'));

    const result = await api.sendChatMessage(1, 'hello');
    expect(result).toMatchObject({ ok: false, status: 0, failure: 'unreachable' });
    expect(get(connectivity.online)).toBe(false);
  });

  it('reports a gap on its own timeout', async () => {
    fetchMock.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted')));
        }),
    );

    vi.useFakeTimers();
    let result: Awaited<ReturnType<Api['sendChatMessage']>>;
    try {
      const pending = api.sendChatMessage(1, 'hello', [], [], 1_000);
      await vi.advanceTimersByTimeAsync(1_000);
      result = await pending;
    } finally {
      vi.useRealTimers();
    }
    expect(result).toMatchObject({ failure: 'timeout' });
    expect(get(connectivity.online)).toBe(false);
  });

  it('reports a reachable server when the server refuses the message', async () => {
    // The distinction the outbox turns on: a refusal is a verdict, so it fails
    // the row rather than parking it and raising the banner.
    connectivity.noteTransport(false, 'unreachable');
    fetchMock.mockResolvedValue(jsonResponse(400, { error: 'too long' }));

    const result = await api.sendChatMessage(1, 'hello');
    expect(result).toMatchObject({ ok: false, failure: 'rejected' });
    expect(get(connectivity.online)).toBe(true);
  });

  it('reports a reachable server when the send is accepted', async () => {
    connectivity.noteTransport(false, 'unreachable');
    fetchMock.mockResolvedValue(jsonResponse(200, { task_id: 7 }));

    const result = await api.sendChatMessage(1, 'hello');
    expect(result.ok).toBe(true);
    expect(get(connectivity.online)).toBe(true);
  });
});

describe('the probe', () => {
  it('asks the server and resolves the store', async () => {
    connectivity.noteTransport(false, 'unreachable');
    fetchMock.mockResolvedValue(jsonResponse(200, { max_attachment_mb: 25 }));

    await expect(connectivity.probe()).resolves.toBe(true);
    expect(fetchMock).toHaveBeenCalledWith('/api/chat/config', expect.anything());
    expect(get(connectivity.online)).toBe(true);
  });

  it('stays offline when the probe cannot reach the server', async () => {
    connectivity.noteTransport(false, 'unreachable');
    fetchMock.mockRejectedValue(new TypeError('Load failed'));

    await expect(connectivity.probe()).resolves.toBe(false);
    expect(get(connectivity.online)).toBe(false);
  });

  it('comes back online for a server that answers with an error', async () => {
    // A 500, or a config endpoint that has since changed shape: the request
    // fails, and the store still learns the one thing it asked about, which is
    // that the server is there. Its own error is not a connectivity fault.
    connectivity.noteTransport(false, 'unreachable');
    fetchMock.mockResolvedValue(jsonResponse(500));

    await expect(connectivity.probe()).resolves.toBe(true);
  });
});

/**
 * What a failed request tells the *user*, which is a different question from
 * what it tells the connectivity store above.
 *
 * `apiFetch` threw `API error: <status>` unconditionally and never read the
 * body, so every FastAPI `detail` in the app was discarded. Three call sites
 * had already dropped out of `apiFetch` to recover it. The case that made it
 * worth fixing at the seam: the vault's master password is refused with a 400
 * whose message is the whole content of the refusal — the passphrase floor —
 * and a user who typed one got `API error: 400` and no way to know why.
 *
 * Driven through a real exported caller rather than by reaching for the
 * private helper, so what is asserted is what a page would actually show.
 */
describe('what a failed request says', () => {
  it('surfaces a FastAPI detail rather than the bare status', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(400, {
        detail: 'a vault passphrase must be at least 32 characters, and should be generated',
      }),
    );

    await expect(api.setVaultPassphrase({ passphrase: 'short' })).rejects.toThrow(
      /at least 32 characters/,
    );
  });

  it('unwraps a structured detail to its message', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(409, {
        detail: { code: 'already_set', message: 'a passphrase is already stored' },
      }),
    );

    await expect(api.setVaultPassphrase({ generate: true })).rejects.toThrow(/already stored/);
  });

  it("reads this app's own `error` spelling too", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(400, { error: 'that file is not in your vault folder' }),
    );

    await expect(api.selectVaultFile('gone.kdbx')).rejects.toThrow(/not in your vault folder/);
  });

  it('falls back to the status when the body carries no reason', async () => {
    // An HTML error page, an empty body, a proxy's own 502: the status is the
    // only thing that was said, so it stays the message. `json()` rejecting
    // must not escape as a different error than the request failing.
    fetchMock.mockResolvedValue({
      ok: false,
      status: 502,
      headers: new Headers(),
      json: async () => {
        throw new SyntaxError('Unexpected token <');
      },
    } as unknown as Response);

    await expect(api.getVaultStatus()).rejects.toThrow('API error: 502');
  });

  it('leaves a 401 as an AuthError, which is not a message at all', async () => {
    // The status branch above it, and the one case where the body must not be
    // consulted: the session is gone and the app redirects rather than
    // rendering whatever the server said about it.
    fetchMock.mockResolvedValue(jsonResponse(401, { detail: 'Not authenticated' }));

    await expect(api.getVaultStatus()).rejects.toBeInstanceOf(api.AuthError);
  });
});
