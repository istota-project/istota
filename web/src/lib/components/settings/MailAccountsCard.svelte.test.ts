import { afterEach, expect, it, vi } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
vi.mock('$app/paths', () => ({ base: '/istota' }));
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
const wait = vi.hoisted(() => ({ waitForMailAccountTest: vi.fn() }));
vi.mock('$lib/mailAccountTest', () => wait);
import MailAccountsCard from './MailAccountsCard.svelte';
import {
  AuthError,
  deleteMailAccount,
  type MailAccount,
  type MailAccountsSettings,
} from '$lib/api';

function account(overrides: Partial<MailAccount> = {}): MailAccount {
  return {
    id: 12,
    slug: 'work',
    label: 'Work',
    permission: 'read',
    imap_host: 'imap.example.com',
    imap_port: 993,
    imap_security: 'tls',
    imap_username: 'me@example.com',
    default_folder: 'INBOX',
    smtp: null,
    from_address: '',
    from_name: '',
    sent_folder: '',
    enabled: true,
    imap_password_set: true,
    smtp_password_set: false,
    pending_drafts: 0,
    updated_at: null,
    ...overrides,
  };
}

function settings(overrides: Partial<MailAccountsSettings> = {}): MailAccountsSettings {
  return {
    enabled: true,
    allow_send: true,
    max_per_user: 10,
    refusal: null,
    accounts: [account()],
    ...overrides,
  };
}

function mount(data: MailAccountsSettings | null, extra: Record<string, unknown> = {}) {
  const props = {
    settings: data,
    onReload: vi.fn(),
    onSaved: vi.fn(),
    onError: vi.fn(),
    ...extra,
  };
  render(MailAccountsCard, props);
  return props;
}

async function choose(item: string, name = 'Work') {
  await fireEvent.click(screen.getByRole('button', { name: `Actions for ${name}` }));
  await fireEvent.click(await screen.findByRole('menuitem', { name: item }));
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it('lists each account with its state and never a password', () => {
  mount(
    settings({
      accounts: [
        account(),
        account({
          id: 13,
          slug: 'lists',
          label: 'Lists',
          permission: 'read_send',
          smtp: {
            host: 'smtp.example.com',
            port: 587,
            security: 'starttls',
            same_login: true,
            username: '',
          },
          from_address: 'me@example.com',
        }),
        account({ id: 14, slug: 'old', label: 'Old', enabled: false }),
        account({ id: 15, slug: 'new', label: 'New', imap_password_set: false }),
      ],
    }),
  );
  const rows = screen.getAllByRole('listitem');
  expect(rows[0]).toHaveTextContent('Read only');
  expect(rows[0]).toHaveTextContent('me@example.com at imap.example.com');
  expect(rows[1]).toHaveTextContent('Read and send');
  expect(rows[2]).toHaveTextContent('Disabled');
  expect(rows[3]).toHaveTextContent('Password not set');
});

it('shows a read_send account as read only when the server turns sending off', () => {
  mount(
    settings({
      allow_send: false,
      accounts: [account({ permission: 'read_send', from_address: 'me@example.com' })],
    }),
  );
  expect(screen.getByRole('listitem')).toHaveTextContent('Read only');
  expect(screen.getByText(/Sending from connected accounts is turned off/)).toBeInTheDocument();
});

it('says why writes are refused and disables every write', async () => {
  mount(settings({ refusal: 'Credentials need a sandboxed deployment.' }));
  expect(screen.getByText('Credentials need a sandboxed deployment.')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Add account' })).toBeDisabled();
  await fireEvent.click(screen.getByRole('button', { name: 'Actions for Work' }));
  for (const item of ['Edit', 'Test connection', 'Delete']) {
    expect(await screen.findByRole('menuitem', { name: item })).toHaveAttribute('data-disabled');
  }
});

it('stops adding at the per-person limit', () => {
  mount(settings({ max_per_user: 1 }));
  expect(screen.getByRole('button', { name: 'Add account' })).toBeDisabled();
  expect(screen.getByText('This server allows 1 mail accounts per person.')).toBeInTheDocument();
});

it('states the held drafts a delete discards, then deletes', async () => {
  vi.mocked(deleteMailAccount).mockResolvedValue({ deleted: true, drafts_discarded: 2 });
  const props = mount(settings({ accounts: [account({ pending_drafts: 2 })] }));
  await choose('Delete');
  const dialog = await screen.findByRole('dialog');
  expect(dialog).toHaveTextContent('2 messages waiting for your approval');
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
  await waitFor(() => expect(props.onSaved).toHaveBeenCalledOnce());
  expect(deleteMailAccount).toHaveBeenCalledWith(12);
});

it('says nothing about drafts when none are held', async () => {
  mount(settings());
  await choose('Delete');
  expect(await screen.findByRole('dialog')).not.toHaveTextContent('approval');
});

it('keeps a refused delete in the dialog and hands an expired session to onError', async () => {
  vi.mocked(deleteMailAccount).mockRejectedValueOnce(new Error('Mail account not found'));
  const props = mount(settings());
  await choose('Delete');
  let dialog = await screen.findByRole('dialog');
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Mail account not found');
  expect(props.onError).not.toHaveBeenCalled();

  vi.mocked(deleteMailAccount).mockRejectedValueOnce(new AuthError());
  dialog = screen.getByRole('dialog');
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
  await waitFor(() => expect(props.onError).toHaveBeenCalledOnce());
});

it('shows a test result under its account, failures with each step', async () => {
  let answer!: (value: unknown) => void;
  wait.waitForMailAccountTest.mockReturnValue(new Promise((resolve) => (answer = resolve)));
  mount(settings());
  await choose('Test connection');
  expect(await screen.findByText('Testing the connection…')).toBeInTheDocument();
  expect(wait.waitForMailAccountTest).toHaveBeenCalledWith(12);
  answer({
    ok: false,
    message: 'The server refused the username or password.',
    lines: [{ step: 'imap', ok: false, message: 'The server refused the username or password.' }],
  });
  const row = screen.getByRole('listitem');
  await waitFor(() => expect(row).toHaveTextContent('Failed: The server refused'));
  expect(screen.queryByText('Testing the connection…')).toBeNull();
});

it('says when the daemon has not answered after two minutes', async () => {
  wait.waitForMailAccountTest.mockResolvedValue(null);
  mount(settings());
  await choose('Test connection');
  expect(await screen.findByText(/has not been answered after two minutes/)).toBeInTheDocument();
});

it('shows a first load that failed with a retry', async () => {
  const props = mount(null, { loadError: 'The mail accounts could not be loaded' });
  expect(screen.getByRole('alert')).toHaveTextContent('The mail accounts could not be loaded');
  expect(screen.queryByRole('button', { name: 'Add account' })).toBeNull();
  await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
  expect(props.onReload).toHaveBeenCalledOnce();
});
