/**
 * The quick note and device token clients: what they send, and how a refusal
 * comes back. Stage callers branch on `code` (`notes_folder_unset`,
 * `in_progress`) and on the editor's shared conflict and lock errors.
 */
import { afterEach, expect, it, vi } from 'vitest';
import {
  AuthError,
  createDevice,
  createQuickNote,
  deleteDevice,
  listDevices,
  QuickNoteError,
  saveQuickNote,
  TextFileConflictError,
  TextFileLockedError,
} from './api';

afterEach(() => vi.unstubAllGlobals());

function answer(status: number, body: unknown) {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status }));
  vi.stubGlobal('fetch', fetch);
  return fetch;
}

it('creates a note with its client id and returns the written path', async () => {
  const fetch = answer(201, {
    path: 'Notes/Inbox/Buy milk.md',
    name: 'Buy milk.md',
    revision: 'r1',
    created: true,
  });
  const note = await createQuickNote('Buy milk', 'abcdef12-3456');
  expect(note).toEqual({
    path: 'Notes/Inbox/Buy milk.md',
    name: 'Buy milk.md',
    revision: 'r1',
    created: true,
  });
  const [url, init] = fetch.mock.calls[0];
  expect(url).toMatch(/\/api\/workspace\/notes$/);
  expect(init.method).toBe('POST');
  expect(JSON.parse(init.body)).toEqual({ content: 'Buy milk', client_note_id: 'abcdef12-3456' });
});

it('reports an unset folder by its code', async () => {
  answer(409, { error: 'Choose a quick notes folder first.', code: 'notes_folder_unset' });
  const refused = createQuickNote('Buy milk');
  await expect(refused).rejects.toBeInstanceOf(QuickNoteError);
  await expect(refused).rejects.toMatchObject({
    status: 409,
    code: 'notes_folder_unset',
    message: 'Choose a quick notes folder first.',
  });
});

it('maps a stale revision and a held lock to the editor errors', async () => {
  answer(409, { error: 'This file changed since it was loaded.', code: 'conflict' });
  await expect(saveQuickNote('Notes/Inbox/a.md', 'x', 'r0')).rejects.toBeInstanceOf(
    TextFileConflictError,
  );
  answer(423, { error: 'This file is being written right now. Try again.', code: 'locked' });
  await expect(saveQuickNote('Notes/Inbox/a.md', 'x', 'r0')).rejects.toBeInstanceOf(
    TextFileLockedError,
  );
});

it('keeps other save refusals as quick note errors, and a 401 as AuthError', async () => {
  answer(403, { error: 'This note is not in your quick notes folder.', code: 'outside_folder' });
  await expect(saveQuickNote('Elsewhere/a.md', 'x', 'r0')).rejects.toMatchObject({
    status: 403,
    code: 'outside_folder',
  });
  answer(401, { error: 'unauthorized' });
  await expect(saveQuickNote('Notes/Inbox/a.md', 'x', 'r0')).rejects.toBeInstanceOf(AuthError);
});

it('lists, mints and revokes device tokens', async () => {
  let fetch = answer(200, { devices: [] });
  await expect(listDevices()).resolves.toEqual({ devices: [] });
  expect(fetch.mock.calls[0][0]).toMatch(/\/api\/devices$/);

  fetch = answer(201, { id: 3, token: 'secret', scopes: ['notes'] });
  const device_id = '6F9619FF-8B86-D011-B42D-00C04FC964FF';
  await expect(createDevice({ device_id, label: 'iPhone', scopes: ['notes'] })).resolves.toEqual({
    id: 3,
    token: 'secret',
    scopes: ['notes'],
  });
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
    device_id,
    label: 'iPhone',
    scopes: ['notes'],
  });

  fetch = answer(200, { ok: true });
  await deleteDevice(3);
  expect(fetch.mock.calls[0][0]).toMatch(/\/api\/devices\/3$/);
  expect(fetch.mock.calls[0][1].method).toBe('DELETE');
});
