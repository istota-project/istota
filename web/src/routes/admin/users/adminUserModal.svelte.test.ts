/**
 * The admin user editor: one user's settings in a modal off the Users page.
 *
 * Driven against the `$lib/api` double, so what is asserted is what the
 * editor sends — only the changed keys, the login email through its own
 * route, the reset through its own — and how it reports a refusal.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import type { AdminUserDetail } from '$lib/api';
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
import AdminUserModal from './AdminUserModal.svelte';

function detail(overrides: Partial<AdminUserDetail> = {}): AdminUserDetail {
  return {
    user_id: 'bob',
    is_admin: false,
    identity: {
      email: 'bob@example.com',
      disabled: false,
      last_login_at: null,
      state: 'passwordless',
    },
    profile: {
      display_name: 'Bob',
      timezone: 'UTC',
      email_addresses: ['bob@example.com'],
      trusted_email_senders: [],
      quiet_email_senders: [],
      outbound_approval: '',
      disabled_skills: [],
      disabled_modules: [],
      default_briefings: true,
      max_foreground_workers: 0,
      max_background_workers: 0,
      sms_phone_number: '+15550100002',
    },
    channels: { log_channel: 'logtoken', alerts_channel: '' },
    whatsapp: {
      number: '+15550100002',
      status: 'enrolled',
      identity: '3f2a…9c1b',
      provider: 'baileys',
      last_seen_at: null,
    },
    managed: [],
    options: {
      modules: ['feeds', 'money'],
      skills: ['browse', 'email'],
      outbound_approval: ['', 'off', 'untrusted', 'all'],
      outbound_approval_floor: 'untrusted',
      email_enabled: true,
      email_login_enabled: true,
      sms_enabled: true,
      whatsapp_enabled: true,
    },
    ...overrides,
  };
}

const props = () => ({
  userId: 'bob',
  onClose: vi.fn(),
  onChanged: vi.fn(),
  onSignedOut: vi.fn(),
});

async function open(d: AdminUserDetail = detail()) {
  api.getAdminUser.mockResolvedValue(d);
  const p = props();
  render(AdminUserModal, p);
  const dialog = await screen.findByRole('dialog', { name: /Settings for/ });
  return { dialog, ...p };
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  api.updateAdminUser.mockImplementation(async () => detail());
});

describe('the admin user editor', () => {
  it('loads the user and sends only the changed keys on save', async () => {
    const { dialog, onChanged, onClose } = await open();
    expect(api.getAdminUser).toHaveBeenCalledWith('bob');
    const name = within(dialog).getByRole('textbox', { name: /Display name/ });
    await fireEvent.input(name, { target: { value: 'Robert' } });
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(api.updateAdminUser).toHaveBeenCalledWith('bob', { display_name: 'Robert' }),
    );
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(onChanged).toHaveBeenCalled();
  });

  it('disables a managed field and badges it, and leaves the rest editable', async () => {
    const { dialog } = await open(detail({ managed: ['email_addresses', 'disabled_modules'] }));
    const addresses = within(dialog).getByRole('textbox', { name: /Email addresses/ });
    expect(addresses).toBeDisabled();
    expect(within(dialog).getAllByText('Set by deployment')).toHaveLength(2);
    expect(within(dialog).getByRole('button', { name: 'feeds' })).toBeDisabled();
    expect(within(dialog).getByRole('button', { name: 'browse' })).not.toBeDisabled();
    expect(within(dialog).getByRole('textbox', { name: /Display name/ })).not.toBeDisabled();
  });

  it('marks the field a 409 names and keeps the draft', async () => {
    api.updateAdminUser.mockRejectedValueOnce(
      new api.AdminUserWriteError('Already assigned to alice.', ['sms_phone_number']),
    );
    const { dialog, onClose } = await open();
    const sms = within(dialog).getByRole('textbox', { name: /SMS number/ });
    await fireEvent.input(sms, { target: { value: '+15550100009' } });
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    expect(await within(dialog).findByText('Already assigned to alice.')).toBeTruthy();
    expect(sms).toHaveAttribute('aria-invalid', 'true');
    expect(sms).toHaveValue('+15550100009');
    expect(onClose).not.toHaveBeenCalled();
  });

  it('toggles a disabled skill as a chip', async () => {
    const { dialog } = await open();
    await fireEvent.click(within(dialog).getByRole('button', { name: 'browse' }));
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(api.updateAdminUser).toHaveBeenCalledWith('bob', { disabled_skills: ['browse'] }),
    );
  });

  it('warns before changing the login email, and saves it through its own route', async () => {
    api.setAdminUserIdentity.mockResolvedValue(
      detail({
        identity: {
          email: 'robert@example.com',
          disabled: false,
          last_login_at: null,
          state: 'passwordless',
        },
      }),
    );
    const { dialog } = await open();
    const warning = /signs this user out everywhere/;
    expect(within(dialog).queryByText(warning)).toBeNull();
    const login = within(dialog).getByRole('textbox', { name: /Login email/ });
    await fireEvent.input(login, { target: { value: 'robert@example.com' } });
    expect(within(dialog).getByText(warning)).toBeTruthy();
    // Not listed yet, so the append defaults on; a change does not invite.
    expect(within(dialog).getByRole('checkbox', { name: /Also add/ })).toBeChecked();
    expect(within(dialog).getByRole('checkbox', { name: /Send invitation/ })).not.toBeChecked();
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Change login email' }));
    await waitFor(() =>
      expect(api.setAdminUserIdentity).toHaveBeenCalledWith('bob', {
        email: 'robert@example.com',
        invite: false,
        add_to_addresses: true,
      }),
    );
    expect(api.updateAdminUser).not.toHaveBeenCalled();
    expect(await within(dialog).findByRole('status')).toHaveTextContent('Login email saved');
  });

  it('copies the SMS number into the WhatsApp field', async () => {
    const { dialog } = await open(
      detail({
        whatsapp: {
          number: '',
          status: 'unbound',
          identity: null,
          provider: null,
          last_seen_at: null,
        },
      }),
    );
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Same as SMS' }));
    expect(within(dialog).getByRole('textbox', { name: 'WhatsApp number' })).toHaveValue(
      '+15550100002',
    );
  });

  it('warns before changing an enrolled number', async () => {
    const { dialog } = await open();
    expect(within(dialog).getByText('Enrolled · Baileys')).toBeTruthy();
    const number = within(dialog).getByRole('textbox', { name: 'WhatsApp number' });
    await fireEvent.input(number, { target: { value: '+15550100003' } });
    expect(within(dialog).getByText(/discards the current enrollment/)).toBeTruthy();
  });

  it('resets the WhatsApp identity only after the confirmation', async () => {
    api.resetAdminUserWhatsApp.mockResolvedValue(
      detail({
        whatsapp: {
          number: '+15550100002',
          status: 'awaiting_first_message',
          identity: null,
          provider: null,
          last_seen_at: null,
        },
      }),
    );
    const { dialog } = await open();
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Reset identity' }));
    const confirm = await screen.findByRole('dialog', { name: 'Reset WhatsApp identity' });
    expect(confirm).toHaveTextContent('Keeps the number');
    expect(api.resetAdminUserWhatsApp).not.toHaveBeenCalled();
    await fireEvent.click(within(confirm).getByRole('button', { name: 'Reset identity' }));
    await waitFor(() => expect(api.resetAdminUserWhatsApp).toHaveBeenCalledWith('bob'));
    expect(await within(dialog).findByText('Waiting for first message')).toBeTruthy();
  });

  it('hides a phone surface the deployment has off, unless a value is stored', async () => {
    const off = {
      ...detail().options,
      sms_enabled: false,
      whatsapp_enabled: false,
    };
    const { dialog } = await open(
      detail({
        options: off,
        profile: { ...detail().profile, sms_phone_number: '' },
      }),
    );
    expect(within(dialog).queryByRole('textbox', { name: /SMS number/ })).toBeNull();
    // The enrolled WhatsApp binding is stored, so it stays where it can be cleared.
    expect(within(dialog).getByRole('textbox', { name: 'WhatsApp number' })).toBeTruthy();
    cleanup();

    const { dialog: bare } = await open(
      detail({
        options: off,
        profile: { ...detail().profile, sms_phone_number: '' },
        whatsapp: {
          number: '',
          status: 'unbound',
          identity: null,
          provider: null,
          last_seen_at: null,
        },
      }),
    );
    expect(within(bare).queryByRole('textbox', { name: /SMS number/ })).toBeNull();
    expect(within(bare).queryByRole('textbox', { name: 'WhatsApp number' })).toBeNull();
  });

  it('asks before closing over unsaved changes', async () => {
    const { dialog, onClose } = await open();
    await fireEvent.input(within(dialog).getByRole('textbox', { name: /Display name/ }), {
      target: { value: 'Robert' },
    });
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    const confirm = await screen.findByRole('dialog', { name: 'Discard changes' });
    expect(onClose).not.toHaveBeenCalled();
    await fireEvent.click(within(confirm).getByRole('button', { name: 'Discard' }));
    expect(onClose).toHaveBeenCalled();
    expect(api.updateAdminUser).not.toHaveBeenCalled();
  });
});
