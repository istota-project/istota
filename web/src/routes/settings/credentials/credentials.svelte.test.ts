import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
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
