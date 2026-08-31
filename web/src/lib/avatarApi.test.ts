import { afterEach, describe, expect, it, vi } from 'vitest';
import { AuthError, deleteAvatar, deleteBotAvatar, uploadAvatar, uploadBotAvatar } from './api';

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: vi.fn(async () => body),
  } as unknown as Response;
}

afterEach(() => vi.unstubAllGlobals());

describe('avatar API authentication', () => {
  it.each([
    ['user upload', () => uploadAvatar(new File(['avatar'], 'avatar.png'))],
    ['user delete', () => deleteAvatar()],
    ['bot upload', () => uploadBotAvatar(new File(['avatar'], 'avatar.png'))],
    ['bot delete', () => deleteBotAvatar()],
  ])('routes a 401 from %s through the sign-in flow', async (_name, request) => {
    const response = jsonResponse(401, { error: 'session expired' });
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => response),
    );

    await expect(request()).rejects.toBeInstanceOf(AuthError);
    expect(response.json).not.toHaveBeenCalled();
  });

  it.each([
    [400, uploadAvatar, 'the multipart body was malformed'],
    [413, uploadBotAvatar, 'that image is too large'],
    [415, uploadAvatar, 'that format cannot be decoded'],
  ])('preserves the endpoint message for a %i upload refusal', async (status, upload, message) => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(status, { error: message })),
    );

    await expect(upload(new File(['avatar'], 'avatar.png'))).rejects.toThrow(message);
  });
});
