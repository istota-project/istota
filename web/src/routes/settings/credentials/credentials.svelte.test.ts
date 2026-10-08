import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import Page from './+page.svelte';

vi.mock('$lib/userContext', () => ({ getCurrentUser: () => ({ expireSession: vi.fn() }) }));
vi.mock('$lib/api', async (original) => ({
  ...(await original<typeof import('$lib/api')>()),
  getCredentialGrants: vi.fn(async () => ({
    credentials: [],
    rooms: [],
    sandboxed: true,
    can_add: true,
    add_blocked_reason: '',
    broker_enabled: true,
    grant_existing_available: false,
  })),
  getCredentialBackup: vi.fn(async () => ({
    recipient_suffix: null,
    last_run: null,
    interval: 86400,
    available: true,
  })),
  getCredentialActivity: vi.fn(async () => []),
}));

afterEach(cleanup);
it('shows import and export without loading the retired shared-file card', async () => {
  render(Page);
  await screen.findByText('No credentials yet.');
  expect(screen.getByRole('heading', { name: 'Import from KeePass' })).toBeTruthy();
  expect(screen.getByRole('heading', { name: 'Export credentials' })).toBeTruthy();
  expect(screen.queryByTestId('vault-status')).toBeNull();
});

it('groups card actions and uses the shared checkbox field', async () => {
  render(Page);
  await screen.findByLabelText('Age public key', { exact: false, selector: 'input' });
  for (const name of [
    'Recently deleted credentials',
    'Preview',
    'Cancel',
    'Export credentials',
    'Save backup key',
  ]) {
    const button = screen.getByRole('button', { name });
    expect(button.parentElement?.classList.contains('row'), name).toBe(true);
  }
  expect(
    screen
      .getByLabelText('Also require a key file', { exact: false, selector: 'input' })
      .closest('.field.checkbox'),
  ).not.toBeNull();
});

it('opens optional guidance without toggling the export option', async () => {
  render(Page);
  await screen.findByLabelText('Age public key', { exact: false, selector: 'input' });
  for (const label of [
    'KeePass file',
    'Source key file (optional)',
    'File passphrase',
    'Age public key',
  ]) {
    expect(screen.getByRole('button', { name: `About ${label}` })).toBeTruthy();
  }
  await fireEvent.click(screen.getByRole('button', { name: 'About Also require a key file' }));
  expect(await screen.findByText(/Adds a separate file/)).toBeTruthy();
  expect(
    (
      screen.getByLabelText('Also require a key file', {
        exact: false,
        selector: 'input',
      }) as HTMLInputElement
    ).checked,
  ).toBe(false);
  await fireEvent.click(
    screen.getByLabelText('Also require a key file', { exact: false, selector: 'input' }),
  );
  expect(screen.getByText(/Save the key file when it downloads/)).toBeTruthy();
});
