import { afterEach, expect, it, vi } from 'vitest';
import { uploadWorkspaceFile } from '$lib/api';

afterEach(() => vi.unstubAllGlobals());

it('sends the captured folder and file, reports progress, and preserves the server error', async () => {
  let xhr!: FakeXHR;
  class FakeXHR {
    upload = {
      onprogress: (_event: { loaded: number; total: number; lengthComputable: boolean }) => {},
    };
    status = 413;
    responseText = JSON.stringify({ error: 'file exceeds 100 MB' });
    timeout = 0;
    onload = async () => {};
    open = vi.fn();
    send = vi.fn();
    constructor() {
      xhr = this;
    }
  }
  vi.stubGlobal('XMLHttpRequest', FakeXHR);
  const progress = vi.fn();
  const file = new File(['bytes'], 'report.txt');
  const pending = uploadWorkspaceFile('Notes & drafts', file, progress);
  const refused = expect(pending).rejects.toThrow('file exceeds 100 MB');
  expect(xhr.open).toHaveBeenCalledWith('POST', '/api/workspace/upload');
  const form = xhr.send.mock.calls[0][0] as FormData;
  expect(form.get('place')).toBe('home');
  expect(form.get('path')).toBe('Notes & drafts');
  expect(form.get('file')).toBe(file);
  xhr.upload.onprogress({ loaded: 50, total: 100, lengthComputable: true });
  expect(progress).toHaveBeenCalledWith(50);
  await xhr.onload();
  await refused;
});
