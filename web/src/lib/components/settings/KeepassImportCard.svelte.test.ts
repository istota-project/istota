import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import KeepassImportCard from './KeepassImportCard.svelte';
vi.mock('$app/paths', () => ({ base: '/istota' }));
vi.mock('$lib/api', async (original) => ({
  ...(await original<typeof import('$lib/api')>()),
  previewKeepassImport: vi.fn(),
  applyKeepassImport: vi.fn(),
}));
import { previewKeepassImport, applyKeepassImport } from '$lib/api';
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
async function preview() {
  vi.mocked(previewKeepassImport).mockResolvedValue({
    digest: 'digest',
    scoped: false,
    truncated: '',
    skipped: {},
    items: [
      {
        name: 'new',
        origin: 'entry',
        fields: ['value'],
        hosts: [],
        status: 'new',
        changed_fields: [],
        reason: null,
        default_selected: true,
      },
      {
        name: 'changed',
        origin: 'entry',
        fields: ['value'],
        hosts: [],
        status: 'changed',
        changed_fields: ['value'],
        reason: null,
        default_selected: false,
      },
      {
        name: 'same',
        origin: 'entry',
        fields: ['value'],
        hosts: [],
        status: 'unchanged',
        changed_fields: [],
        reason: null,
        default_selected: false,
      },
    ],
  });
  const file = new File(['fixture'], 'fixture.kdbx');
  const keyfile = new File(['key'], 'fixture.key');
  await fireEvent.change(screen.getByLabelText('KeePass file'), { target: { files: [file] } });
  await fireEvent.change(screen.getByLabelText('Source key file (optional)'), {
    target: { files: [keyfile] },
  });
  await fireEvent.input(screen.getByLabelText('File passphrase'), {
    target: { value: 'fixture-passphrase' },
  });
  await fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
  await screen.findByText('Changed');
  return { file, keyfile };
}
it('renders groups with only new entries selected and sends the source key file', async () => {
  render(KeepassImportCard);
  const { file, keyfile } = await preview();
  expect(vi.mocked(previewKeepassImport)).toHaveBeenCalledWith(
    file,
    'fixture-passphrase',
    keyfile,
    expect.any(AbortSignal),
  );
  expect((screen.getByLabelText('new') as HTMLInputElement).checked).toBe(true);
  expect((screen.getByLabelText('changed') as HTMLInputElement).checked).toBe(false);
  expect((screen.getByLabelText('same') as HTMLInputElement).disabled).toBe(true);
  expect(screen.getByText(/no istota group/)).toBeTruthy();
  vi.mocked(applyKeepassImport).mockResolvedValue({ imported: ['new'], not_imported: {} });
  await fireEvent.click(screen.getByRole('button', { name: 'Import selected' }));
  await waitFor(() =>
    expect(vi.mocked(applyKeepassImport)).toHaveBeenCalledWith(
      file,
      'fixture-passphrase',
      keyfile,
      ['new'],
      'digest',
      expect.any(AbortSignal),
    ),
  );
  await screen.findByText('Imported 1 credential.');
  expect((screen.getByLabelText('File passphrase') as HTMLInputElement).value).toBe('');
  expect(screen.queryByLabelText('new')).toBeNull();
});
it('clears the file, key file and passphrase on cancel', async () => {
  render(KeepassImportCard);
  await preview();
  await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  expect((screen.getByLabelText('File passphrase') as HTMLInputElement).value).toBe('');
  expect((screen.getByRole('button', { name: 'Preview' }) as HTMLButtonElement).disabled).toBe(
    true,
  );
  expect(screen.queryByLabelText('new')).toBeNull();
});

it('aborts a pending preview on navigation and ignores its late result', async () => {
  let resolve!: (value: Awaited<ReturnType<typeof previewKeepassImport>>) => void;
  vi.mocked(previewKeepassImport).mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const component = render(KeepassImportCard);
  await fireEvent.change(screen.getByLabelText('KeePass file'), {
    target: { files: [new File(['x'], 'test.kdbx')] },
  });
  await fireEvent.input(screen.getByLabelText('File passphrase'), {
    target: { value: 'fixture-passphrase' },
  });
  await fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
  const signal = vi.mocked(previewKeepassImport).mock.calls[0][3]!;
  component.unmount();
  expect(signal.aborted).toBe(true);
  resolve({ digest: 'late', scoped: true, truncated: '', items: [], skipped: {} });
  await Promise.resolve();
  expect(screen.queryByText('Import selected')).toBeNull();
});
