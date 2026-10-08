import { webcrypto } from 'node:crypto';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import KeepassExportCard from './KeepassExportCard.svelte';
import { generateKeyfile } from '$lib/keepass';
vi.mock('$app/paths', () => ({ base: '/istota' }));
vi.mock('$lib/api', async (original) => ({
  ...(await original<typeof import('$lib/api')>()),
  startStepUp: vi.fn(),
  exportKeepass: vi.fn(),
}));
import { startStepUp, exportKeepass } from '$lib/api';
const password = 'fixture-export-password';
const downloads: string[] = [];
beforeEach(() => {
  vi.stubGlobal('crypto', webcrypto);
  vi.stubGlobal(
    'URL',
    Object.assign(URL, {
      createObjectURL: vi.fn(() => 'blob:fixture'),
      revokeObjectURL: vi.fn(),
    }),
  );
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    downloads.push(this.download);
  });
  vi.mocked(startStepUp).mockResolvedValue({
    request_id: 'request',
    email_hint: 'a•••@example.com',
    expires_at: 'later',
  });
  vi.mocked(exportKeepass).mockImplementation(async () => ({
    filename: 'istota-export-2026-01-01.kdbx',
    password,
    file: btoa('encrypted'),
    summary: { credentials: 1, generated: 0, otp: 0, recovery: 0 },
  }));
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  downloads.length = 0;
});

it('generates a KeePass 2.0 XML key with the SHA-256 prefix', async () => {
  const xml = new DOMParser().parseFromString(await generateKeyfile(), 'application/xml');
  expect(xml.querySelector('Version')?.textContent).toBe('2.0');
  const data = xml.querySelector('Data')!;
  const text = data.textContent!.replace(/\s/g, '');
  expect(text).toMatch(/^[0-9A-F]{64}$/);
  const raw = Uint8Array.from(text.match(/../g)!, (byte) => parseInt(byte, 16));
  const hash = new Uint8Array(await crypto.subtle.digest('SHA-256', raw));
  expect(data.getAttribute('Hash')).toBe(
    Array.from(hash.slice(0, 4), (byte) => byte.toString(16).padStart(2, '0'))
      .join('')
      .toUpperCase(),
  );
});

async function confirm() {
  await screen.findByText(/a•••@example.com/);
  await fireEvent.input(screen.getByLabelText('Confirmation code'), {
    target: { value: '123456' },
  });
  await fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
}
it('downloads the key first and requires saved acknowledgement before clearing the password', async () => {
  render(KeepassExportCard);
  await fireEvent.click(screen.getByLabelText('Also require a key file'));
  await fireEvent.click(screen.getByRole('button', { name: 'Export credentials' }));
  await screen.findByText(/a•••@example.com/);
  expect(downloads[0]).toMatch(/istota-export-.*\.keyx$/);
  expect(exportKeepass).not.toHaveBeenCalled();
  await confirm();
  await screen.findByText(password);
  expect(downloads[1]).toMatch(/\.kdbx$/);
  expect(exportKeepass).toHaveBeenCalledWith(
    expect.stringContaining('<Version>2.0</Version>'),
    { request_id: 'request', code: '123456' },
    expect.any(AbortSignal),
  );
  expect(URL.revokeObjectURL).toHaveBeenCalledTimes(2);
  await fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
  expect(screen.getByText(password)).toBeTruthy();
  await fireEvent.pointerDown(document.body);
  expect(screen.getByText(password)).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', { name: 'I saved it' }));
  await waitFor(() => expect(screen.queryByText(password)).toBeNull());
  expect(screen.queryByRole('dialog')).toBeNull();
});
it('clears the key file on cancel and ignores a late export after navigation', async () => {
  const component = render(KeepassExportCard);
  await fireEvent.click(screen.getByLabelText('Also require a key file'));
  await fireEvent.click(screen.getByRole('button', { name: 'Export credentials' }));
  await screen.findByText(/a•••@example.com/);
  await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  await fireEvent.click(screen.getByLabelText('Also require a key file'));
  let resolve!: (value: Awaited<ReturnType<typeof exportKeepass>>) => void;
  vi.mocked(exportKeepass).mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  await fireEvent.click(screen.getByRole('button', { name: 'Export credentials' }));
  await confirm();
  expect(vi.mocked(exportKeepass).mock.calls[0][0]).toBeNull();
  const signal = vi.mocked(exportKeepass).mock.calls[0][2]!;
  component.unmount();
  expect(signal.aborted).toBe(true);
  resolve({
    filename: 'late.kdbx',
    file: btoa('encrypted'),
    password,
    summary: { credentials: 1, generated: 0, otp: 0, recovery: 0 },
  });
  await Promise.resolve();
  expect(downloads).toHaveLength(1);
  expect(screen.queryByText(password)).toBeNull();
});
