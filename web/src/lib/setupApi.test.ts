/**
 * The setup API's error shape, against the setup server's own bodies
 * (`webui/setup_app.py`): `{error: <code>, message, fields?, step?}`.
 *
 * The point of not going through `apiFetch` is here: the message is
 * `message` and not the `error` code, a 401 is "locked" and never a redirect
 * to the login page, and a refused Finish keeps its per-field `step`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  AuthError,
  checkSetupConnection,
  finishSetup,
  getSetupState,
  putSetupDraft,
  SetupApiError,
  unlockSetup,
} from '$lib/api';

const fetchMock = vi.fn();

function answer(status: number, body: unknown) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

async function refusal(promise: Promise<unknown>): Promise<SetupApiError> {
  const error = await promise.then(
    () => null,
    (e: unknown) => e,
  );
  expect(error).toBeInstanceOf(SetupApiError);
  return error as SetupApiError;
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('the setup API', () => {
  it('asks the setup routes under the API base and returns the body', async () => {
    fetchMock.mockResolvedValue(answer(200, { phase: 'locked', local: true }));

    expect(await getSetupState()).toEqual({ phase: 'locked', local: true });
    expect(fetchMock.mock.calls[0][0]).toBe('/api/setup/state');
  });

  it('sends a JSON body on a write', async () => {
    fetchMock.mockResolvedValue(answer(200, { draft: {} }));

    await putSetupDraft({ bot_name: 'Marlowe', native_api_key: null });

    const [, init] = fetchMock.mock.calls[0];
    expect(init.method).toBe('PUT');
    expect(init.headers).toEqual({ 'Content-Type': 'application/json' });
    expect(JSON.parse(init.body)).toEqual({ bot_name: 'Marlowe', native_api_key: null });
  });

  it('posts a check with no body', async () => {
    fetchMock.mockResolvedValue(answer(200, { ok: true, warning: false, message: 'Connected.' }));

    await checkSetupConnection('model');

    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe('/api/setup/check/model');
    expect(init.method).toBe('POST');
    expect(init.body).toBeUndefined();
  });

  it('carries the server’s message and code, not the code as the message', async () => {
    fetchMock.mockResolvedValue(
      answer(403, { error: 'forbidden', message: 'That setup code is not valid.' }),
    );

    const error = await refusal(unlockSetup('WRONG'));
    expect(error.status).toBe(403);
    expect(error.code).toBe('forbidden');
    expect(error.message).toBe('That setup code is not valid.');
  });

  it('reads a 401 as locked, never as a signed-out session', async () => {
    fetchMock.mockResolvedValue(answer(401, { error: 'no_session', message: 'Setup is locked.' }));

    const error = await getSetupState().catch((e: unknown) => e);
    expect(error).not.toBeInstanceOf(AuthError);
    expect(error).toBeInstanceOf(SetupApiError);
    expect((error as SetupApiError).status).toBe(401);
  });

  it('keeps a refused Finish’s fields and the step to go back to', async () => {
    fetchMock.mockResolvedValue(
      answer(400, {
        error: 'invalid',
        message: 'Some answers need fixing before setup can finish.',
        step: 'account',
        fields: [
          { field: 'password', message: 'Use at least 12 characters.', step: 'account' },
          { field: 7, message: 'not a field' },
        ],
      }),
    );

    const error = await refusal(finishSetup());
    expect(error.step).toBe('account');
    expect(error.fields).toEqual([
      { field: 'password', message: 'Use at least 12 characters.', step: 'account' },
    ]);
  });

  it('reports no answer at all as status 0', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'));

    const error = await refusal(getSetupState());
    expect(error.status).toBe(0);
    expect(error.code).toBe('unreachable');
    expect(error.message).toBe("Can't reach the setup service.");
  });

  it('refuses an answer that is not JSON, whatever its status', async () => {
    fetchMock.mockResolvedValue(new Response('<html></html>', { status: 200 }));

    const error = await refusal(getSetupState());
    expect(error.status).toBe(200);
    expect(error.message).toBe('The setup service answered 200.');
  });
});
