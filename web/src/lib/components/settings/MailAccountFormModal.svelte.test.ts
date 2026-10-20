import { afterEach, expect, it, vi } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
vi.mock('$app/paths', () => ({ base: '/istota' }));
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
import MailAccountFormModal from './MailAccountFormModal.svelte';
import { AuthError, createMailAccount, updateMailAccount, type MailAccount } from '$lib/api';

const stored: MailAccount = {
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
};

function mount(account: MailAccount | null = null, allowSend = true) {
  const props = { account, allowSend, onClose: vi.fn(), onSaved: vi.fn(), onError: vi.fn() };
  render(MailAccountFormModal, props);
  return props;
}

function imap() {
  return within(screen.getByRole('group', { name: 'Incoming mail (IMAP)' }));
}

async function type(input: HTMLElement, value: string) {
  await fireEvent.input(input, { target: { value } });
}

async function pick(trigger: HTMLElement, option: string) {
  await fireEvent.pointerDown(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.pointerUp(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.click(trigger);
  const choice = await screen.findByRole('option', { name: option });
  await fireEvent.pointerUp(choice, { pointerType: 'mouse', button: 0 });
  await fireEvent.click(choice);
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it('creates a read account with its password and no SMTP', async () => {
  vi.mocked(createMailAccount).mockResolvedValue({ account: stored });
  const props = mount();
  await type(screen.getByRole('textbox', { name: 'Short name' }), 'work');
  await type(imap().getByRole('textbox', { name: 'Server' }), 'imap.example.com');
  await type(imap().getByRole('textbox', { name: 'Username' }), 'me@example.com');
  await type(imap().getByLabelText('Password'), 'hunter2');
  await fireEvent.click(screen.getByRole('button', { name: 'Add account' }));
  await waitFor(() => expect(props.onSaved).toHaveBeenCalledOnce());
  expect(createMailAccount).toHaveBeenCalledWith({
    label: '',
    slug: 'work',
    permission: 'read',
    imap_host: 'imap.example.com',
    imap_port: 993,
    imap_security: 'tls',
    imap_username: 'me@example.com',
    default_folder: 'INBOX',
    imap_password: 'hunter2',
  });
  expect(props.onClose).toHaveBeenCalledOnce();
});

it('names the Microsoft 365 limit beside the IMAP server', () => {
  mount();
  expect(
    imap().getByText(/Microsoft 365 and Outlook.com mailboxes cannot be connected/),
  ).toBeVisible();
});

it('turns sending on with SMTP and keeps a blank password as stored', async () => {
  vi.mocked(updateMailAccount).mockResolvedValue({ account: stored });
  const props = mount(stored);
  expect(imap().getByLabelText('Password')).toHaveAttribute(
    'placeholder',
    '•••••••• stored, type to replace',
  );
  await fireEvent.click(screen.getByRole('checkbox', { name: 'Allow sending' }));
  const smtp = within(screen.getByRole('group', { name: 'Outgoing mail (SMTP)' }));
  await type(smtp.getByRole('textbox', { name: 'Server' }), 'smtp.example.com');
  await type(smtp.getByRole('textbox', { name: 'From address' }), 'me@example.com');
  await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(props.onSaved).toHaveBeenCalledOnce());
  const [id, body] = vi.mocked(updateMailAccount).mock.calls[0];
  expect(id).toBe(12);
  expect(body).toMatchObject({
    permission: 'read_send',
    enabled: true,
    smtp: {
      host: 'smtp.example.com',
      port: 587,
      security: 'starttls',
      same_login: true,
      username: '',
    },
    from_address: 'me@example.com',
  });
  expect(body).not.toHaveProperty('imap_password');
  expect(body).not.toHaveProperty('smtp_password');
});

it('moves a default port with the security mode and leaves a typed one', async () => {
  vi.mocked(createMailAccount).mockResolvedValue({ account: stored });
  mount();
  const port = imap().getByRole('textbox', { name: 'Port' });
  expect(port).toHaveValue('993');
  await pick(imap().getByLabelText('Security'), 'STARTTLS');
  await waitFor(() => expect(port).toHaveValue('143'));
  await type(port, '1993');
  await pick(imap().getByLabelText('Security'), 'TLS');
  await waitFor(() => expect(imap().getByLabelText('Security')).toHaveTextContent('TLS'));
  expect(port).toHaveValue('1993');
});

it('refuses a port that is not one before asking the server', async () => {
  mount();
  await type(imap().getByRole('textbox', { name: 'Port' }), '99999');
  await fireEvent.click(screen.getByRole('button', { name: 'Add account' }));
  expect(await screen.findByText('Use a port between 1 and 65535')).toBeInTheDocument();
  expect(createMailAccount).not.toHaveBeenCalled();
});

it('shows a refusal beside its field and keeps the dialog open', async () => {
  vi.mocked(createMailAccount).mockRejectedValue(
    Object.assign(new Error('You already have an account with that slug'), { field: 'slug' }),
  );
  const props = mount();
  await type(screen.getByRole('textbox', { name: 'Short name' }), 'work');
  await fireEvent.click(screen.getByRole('button', { name: 'Add account' }));
  expect(await screen.findByText('You already have an account with that slug')).toBeInTheDocument();
  expect(screen.getByRole('dialog')).toBeInTheDocument();
  expect(props.onClose).not.toHaveBeenCalled();
});

it('hands an expired session to onError and closes', async () => {
  vi.mocked(createMailAccount).mockRejectedValue(new AuthError());
  const props = mount();
  await fireEvent.click(screen.getByRole('button', { name: 'Add account' }));
  await waitFor(() => expect(props.onError).toHaveBeenCalledOnce());
  expect(props.onClose).toHaveBeenCalledOnce();
});
