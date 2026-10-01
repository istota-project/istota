import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import CredentialGrantsCard from './CredentialGrantsCard.svelte';
import type { CredentialGrantsSettings } from '$lib/api';
vi.mock('$app/paths', () => ({ base: '/istota' }));
vi.mock('$lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/api')>()),
  getCredentialGrants: vi.fn(),
  saveCredentialGrant: vi.fn(),
  grantExistingCredentials: vi.fn(),
  revokeCredentialGrant: vi.fn(),
  deleteCredential: vi.fn(),
}));
import {
  getCredentialGrants,
  saveCredentialGrant,
  grantExistingCredentials,
  revokeCredentialGrant,
  deleteCredential,
} from '$lib/api';
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

type Credential = CredentialGrantsSettings['credentials'][number];

function portal(over: Partial<Credential> = {}): Credential {
  return {
    name: 'portal',
    source: 'vault',
    hosts: ['portal.example'],
    headers: ['authorization'],
    revealable: false,
    grant: null,
    ...over,
  };
}

function settings(over: Partial<CredentialGrantsSettings> = {}): CredentialGrantsSettings {
  return {
    credentials: [portal()],
    rooms: [],
    grant_existing_available: false,
    sandboxed: true,
    ...over,
  };
}

// bits-ui opens the menu on pointerdown, which jsdom only partly implements;
// the keyboard path is equivalent (see KebabMenu.svelte.test.ts).
async function chooseAction(credential: string, action: string) {
  await fireEvent.keyDown(screen.getByLabelText(`Actions for ${credential}`), { key: 'Enter' });
  await fireEvent.click(await screen.findByText(action));
}

function row(name: string): HTMLElement {
  return screen.getByTestId(`credential-${name}`);
}

it('shows one row per credential with its host and access state', async () => {
  vi.mocked(getCredentialGrants).mockResolvedValue(
    settings({
      sandboxed: false,
      credentials: [
        portal(),
        portal({
          name: 'billing',
          source: 'config',
          hosts: ['billing.example', 'api.billing.example'],
          revealable: true,
          grant: {
            scope_mode: 'rooms',
            rooms: ['r1', 'r2'],
            methods: ['GET', 'POST'],
            allow_scheduled: true,
          },
        }),
        portal({ name: 'loose', hosts: [] }),
      ],
    }),
  );
  render(CredentialGrantsCard);
  await screen.findByText('portal.example');

  expect(within(row('portal')).getByText('Ungranted')).toBeTruthy();
  const billing = row('billing');
  expect(within(billing).getByText('billing.example, api.billing.example')).toBeTruthy();
  expect(within(billing).getByText('Revealable')).toBeTruthy();
  expect(within(billing).queryByText('Ungranted')).toBeNull();
  expect(billing.textContent).toContain('2 rooms · GET, POST · scheduled');
  expect(billing.textContent).toContain('Deployment configuration');
  expect(within(row('loose')).getByText('Unbound')).toBeTruthy();
  expect(screen.getByText(/values are not contained/i)).toBeTruthy();
});

it('edits a grant from the row menu and saves narrow defaults', async () => {
  vi.mocked(getCredentialGrants).mockResolvedValue(settings());
  vi.mocked(saveCredentialGrant).mockResolvedValue({ ok: true });
  render(CredentialGrantsCard);
  await screen.findByText('portal.example');
  await chooseAction('portal', 'Edit grant');
  expect((screen.getByLabelText('Allow scheduled tasks') as HTMLInputElement).checked).toBe(false);
  await fireEvent.click(screen.getByRole('button', { name: 'Save grant' }));
  await waitFor(() =>
    expect(saveCredentialGrant).toHaveBeenCalledWith('portal', {
      scope_mode: 'all',
      rooms: [],
      methods: ['GET', 'HEAD', 'POST', 'PUT', 'PATCH'],
      allow_scheduled: false,
    }),
  );
});

it('offers no grant edit for an unbound credential', async () => {
  vi.mocked(getCredentialGrants).mockResolvedValue(
    settings({ credentials: [portal({ hosts: [] })] }),
  );
  render(CredentialGrantsCard);
  await screen.findByText('Unbound');
  await fireEvent.keyDown(screen.getByLabelText('Actions for portal'), { key: 'Enter' });
  const edit = await screen.findByText('Edit grant');
  expect(edit.hasAttribute('data-disabled')).toBe(true);
});

it('cancels grant edits without saving', async () => {
  vi.mocked(getCredentialGrants).mockResolvedValue(settings());
  render(CredentialGrantsCard);
  await screen.findByText('portal.example');
  await chooseAction('portal', 'Edit grant');
  const dialog = screen.getByRole('dialog', { name: 'Edit grant' });
  expect(within(dialog).getByText('portal')).toBeTruthy();
  await fireEvent.click(within(dialog).getByRole('checkbox', { name: 'DELETE' }));
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(saveCredentialGrant).not.toHaveBeenCalled();
  await chooseAction('portal', 'Edit grant');
  expect(screen.getByRole('checkbox', { name: 'DELETE' })).not.toBeChecked();
});

it('confirms deletion of an unbound credential and refreshes the list', async () => {
  vi.mocked(getCredentialGrants).mockResolvedValue(
    settings({ credentials: [portal({ hosts: [] })] }),
  );
  vi.mocked(deleteCredential).mockResolvedValue({ ok: true, deleted: true });
  render(CredentialGrantsCard);
  await screen.findByText('Unbound');
  await chooseAction('portal', 'Delete credential');
  const dialog = screen.getByRole('dialog', { name: 'Delete credential' });
  expect(dialog.textContent).toContain('KeePassXC');
  expect(deleteCredential).not.toHaveBeenCalled();
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
  expect(deleteCredential).not.toHaveBeenCalled();
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  await chooseAction('portal', 'Delete credential');
  vi.mocked(getCredentialGrants).mockResolvedValue(settings({ credentials: [] }));
  await fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }));
  await waitFor(() => expect(deleteCredential).toHaveBeenCalledWith('portal'));
  await screen.findByText('No credentials have been stored.');
});

it('keeps a credential visible if deletion fails', async () => {
  vi.mocked(getCredentialGrants).mockResolvedValue(settings());
  vi.mocked(deleteCredential).mockRejectedValueOnce(new Error('Could not delete credential'));
  render(CredentialGrantsCard);
  await screen.findByText('portal.example');
  await chooseAction('portal', 'Delete credential');
  await fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }));
  await screen.findByRole('alert');
  expect(row('portal')).toBeTruthy();
});

it('does not offer deletion of a deployment credential', async () => {
  vi.mocked(getCredentialGrants).mockResolvedValue(
    settings({ credentials: [portal({ name: 'forge.github', source: 'config' })] }),
  );
  render(CredentialGrantsCard);
  await screen.findByText('portal.example');
  await fireEvent.keyDown(screen.getByLabelText('Actions for forge.github'), { key: 'Enter' });
  await screen.findByText('Edit grant');
  expect(screen.queryByText('Delete credential')).toBeNull();
});

it('asks before revoking a grant', async () => {
  vi.mocked(getCredentialGrants).mockResolvedValue(
    settings({
      credentials: [
        portal({
          grant: { scope_mode: 'all', rooms: [], methods: ['GET'], allow_scheduled: false },
        }),
      ],
    }),
  );
  vi.mocked(revokeCredentialGrant).mockResolvedValue({ ok: true });
  render(CredentialGrantsCard);
  await screen.findByText('portal.example');
  await chooseAction('portal', 'Revoke grant');
  expect(revokeCredentialGrant).not.toHaveBeenCalled();
  const dialog = screen.getByRole('dialog');
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Revoke' }));
  await waitFor(() => expect(revokeCredentialGrant).toHaveBeenCalledWith('portal'));
});

it('requires confirmation for grant-existing and keeps a failed save visible', async () => {
  vi.mocked(getCredentialGrants).mockResolvedValue(settings({ grant_existing_available: true }));
  vi.mocked(grantExistingCredentials).mockResolvedValue({ ok: true, count: 1 });
  render(CredentialGrantsCard);
  await screen.findByText('portal.example');
  await fireEvent.click(screen.getByRole('button', { name: 'Grant what exists' }));
  expect(grantExistingCredentials).not.toHaveBeenCalled();
  const dialog = screen.getByRole('dialog');
  await fireEvent.click(dialog.querySelector('.btn-primary')!);
  await waitFor(() => expect(grantExistingCredentials).toHaveBeenCalledOnce());
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  await chooseAction('portal', 'Edit grant');
  vi.mocked(saveCredentialGrant).mockRejectedValueOnce(new Error('Could not save policy'));
  await fireEvent.click(screen.getByRole('button', { name: 'Save grant' }));
  await waitFor(() =>
    expect(screen.getByRole('dialog').textContent).toContain('Could not save policy'),
  );
});

it('drops unavailable room selections so a grant can still be narrowed', async () => {
  vi.mocked(getCredentialGrants).mockResolvedValue(
    settings({
      credentials: [
        portal({
          grant: {
            scope_mode: 'rooms',
            rooms: ['live-room', 'deleted-room'],
            methods: ['GET'],
            allow_scheduled: false,
          },
        }),
      ],
      rooms: [{ token: 'live-room', name: 'Personal' }],
    }),
  );
  vi.mocked(saveCredentialGrant).mockResolvedValue({ ok: true });
  render(CredentialGrantsCard);
  await screen.findByText('portal.example');
  await chooseAction('portal', 'Edit grant');
  await fireEvent.click(screen.getByRole('button', { name: 'Save grant' }));
  await waitFor(() =>
    expect(saveCredentialGrant).toHaveBeenCalledWith('portal', {
      scope_mode: 'rooms',
      rooms: ['live-room'],
      methods: ['GET'],
      allow_scheduled: false,
    }),
  );
});
