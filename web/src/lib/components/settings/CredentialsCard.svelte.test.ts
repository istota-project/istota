import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import CredentialsCard from './CredentialsCard.svelte';
import type { CredentialGrantsSettings, CredentialSummary } from '$lib/api';
vi.mock('$app/paths', () => ({ base: '/istota' }));
vi.mock('$lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/api')>()),
  getCredentialGrants: vi.fn(),
  saveCredentialGrant: vi.fn(),
  grantExistingCredentials: vi.fn(),
  revokeCredentialGrant: vi.fn(),
  deleteCredential: vi.fn(),
  createCredential: vi.fn(),
  updateLocalCredential: vi.fn(),
  setGeneratedMirror: vi.fn(),
  remirrorGenerated: vi.fn(),
  setGeneratedDefaultMirror: vi.fn(),
  showRecoveryCodes: vi.fn(),
}));
import {
  showRecoveryCodes,
  getCredentialGrants,
  saveCredentialGrant,
  grantExistingCredentials,
  revokeCredentialGrant,
  deleteCredential,
  remirrorGenerated,
  setGeneratedDefaultMirror,
  setGeneratedMirror,
} from '$lib/api';
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function portal(over: Partial<CredentialSummary> = {}): CredentialSummary {
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

function local(over: Partial<CredentialSummary> = {}): CredentialSummary {
  return portal({
    name: 'openrouter_key',
    source: 'local',
    hosts: ['openrouter.ai'],
    url: 'openrouter.ai',
    username_set: false,
    ...over,
  });
}

function settings(over: Partial<CredentialGrantsSettings> = {}): CredentialGrantsSettings {
  return {
    credentials: [portal()],
    rooms: [],
    grant_existing_available: false,
    sandboxed: true,
    can_add: true,
    add_blocked_reason: '',
    broker_enabled: true,
    ...over,
  };
}

// bits-ui opens the menu on pointerdown, which jsdom only partly implements;
// the keyboard path is equivalent (see KebabMenu.svelte.test.ts).
async function openMenu(credential: string) {
  await fireEvent.keyDown(screen.getByLabelText(`Actions for ${credential}`), { key: 'Enter' });
}
async function chooseAction(credential: string, action: string) {
  await openMenu(credential);
  await fireEvent.click(await screen.findByText(action));
}
async function menuLabels(credential: string): Promise<string[]> {
  await openMenu(credential);
  const items = await screen.findAllByRole('menuitem');
  return items.map((item) => (item.textContent ?? '').trim());
}

function row(name: string): HTMLElement {
  return screen.getByTestId(`credential-${name}`);
}

function words(el: HTMLElement): string {
  return (el.textContent ?? '').replace(/\s+/g, ' ').trim();
}

describe('the list', () => {
  it('shows one row per credential with its site and access', async () => {
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
            grant: { scope_mode: 'rooms', rooms: ['r1', 'r2'], allow_scheduled: true },
          }),
        ],
      }),
    );
    render(CredentialsCard);
    await screen.findByText('portal.example');

    expect(within(row('portal')).getByText('No access yet')).toBeTruthy();
    const billing = row('billing');
    expect(within(billing).getByText('billing.example, api.billing.example')).toBeTruthy();
    expect(within(billing).getByText('Readable by tasks')).toBeTruthy();
    expect(within(billing).queryByText('No access yet')).toBeNull();
    expect(billing.textContent).toContain('2 rooms · scheduled');
    expect(screen.getByText(/values are not contained/i)).toBeTruthy();
  });

  it('labels every row with exactly one source badge', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(
      settings({
        credentials: [
          local(),
          portal(),
          portal({ name: 'forge.github', source: 'config', hosts: ['github.com'] }),
        ],
      }),
    );
    render(CredentialsCard);
    await screen.findByText('portal.example');

    const expected: Record<string, string> = {
      openrouter_key: 'Istota',
      portal: 'KeePassXC',
      'forge.github': 'Deployment',
    };
    for (const [name, label] of Object.entries(expected)) {
      const badges = within(row(name))
        .getAllByText(/^(Istota|KeePassXC|Deployment)$/)
        .map((el) => el.textContent);
      expect(badges).toEqual([label]);
    }
    expect(document.body.textContent).not.toContain('Password vault');
  });

  it('tints each source badge by its own kind, at the compact size', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(
      settings({
        credentials: [
          local(),
          portal(),
          portal({ name: 'forge.github', source: 'config', hosts: ['github.com'] }),
        ],
      }),
    );
    render(CredentialsCard);
    await screen.findByText('portal.example');

    const sources: Record<string, string> = {
      openrouter_key: 'local',
      portal: 'vault',
      'forge.github': 'config',
    };
    for (const [name, source] of Object.entries(sources)) {
      const badge = row(name).querySelector(`.cred-source-${source} .badge`);
      expect(badge?.classList.contains('badge-sm')).toBe(true);
    }
    for (const badge of row('openrouter_key').querySelectorAll('.badge')) {
      expect(badge.classList.contains('badge-sm')).toBe(true);
    }
  });

  it('says what to do about a missing site, by source', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(
      settings({ credentials: [local({ hosts: [], url: '' }), portal({ hosts: [] })] }),
    );
    render(CredentialsCard);
    await waitFor(() => expect(row('portal')).toBeTruthy());

    expect(words(row('openrouter_key'))).toContain('No site. Edit it to add one.');
    expect(words(row('portal'))).toContain('No site. Add a URL to this entry in KeePassXC.');
    expect(document.body.textContent).not.toContain('Unbound');
    expect(document.body.textContent).not.toContain('istota_hosts');
  });

  it('shows the count once, in the header', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(
      settings({ credentials: [local(), portal()] }),
    );
    render(CredentialsCard);
    await screen.findByText('portal.example');
    expect(screen.getByRole('heading', { name: 'Credentials' })).toBeTruthy();
    expect(screen.getByText('2')).toBeTruthy();
  });

  it('says so when there are none', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(settings({ credentials: [] }));
    render(CredentialsCard);
    await screen.findByText('No credentials yet.');
    expect(document.body.textContent).not.toContain('KeePassXC');
  });

  it('warns that access is not enforced only while the broker is off', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(settings({ broker_enabled: false }));
    render(CredentialsCard);
    await screen.findByText(/not enforced until your administrator/i);
    cleanup();

    vi.mocked(getCredentialGrants).mockResolvedValue(settings({ broker_enabled: true }));
    render(CredentialsCard);
    await screen.findByText('portal.example');
    expect(screen.queryByText(/not enforced until your administrator/i)).toBeNull();
  });

  it('disables Add and says why when the store is withheld', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(
      settings({ can_add: false, add_blocked_reason: 'Tasks here cannot use stored credentials.' }),
    );
    render(CredentialsCard);
    await screen.findByText('Tasks here cannot use stored credentials.');
    expect(
      (screen.getByRole('button', { name: 'Add credential' }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it('enables Add when the store is available', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(settings());
    render(CredentialsCard);
    await screen.findByText('portal.example');
    expect(
      (screen.getByRole('button', { name: 'Add credential' }) as HTMLButtonElement).disabled,
    ).toBe(false);
    expect(screen.queryByTestId('add-blocked')).toBeNull();
  });
});

describe('the row menu', () => {
  it('offers edit and delete for a credential added in Istota', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(
      settings({
        credentials: [local({ grant: { scope_mode: 'all', rooms: [], allow_scheduled: false } })],
      }),
    );
    render(CredentialsCard);
    await screen.findByText('openrouter.ai');
    expect(await menuLabels('openrouter_key')).toEqual([
      'Edit',
      'Edit access',
      'Revoke access',
      'Delete',
    ]);
  });

  it('offers only removal of the stored copy for a KeePassXC credential', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(settings());
    render(CredentialsCard);
    await screen.findByText('portal.example');
    expect(await menuLabels('portal')).toEqual(['Edit access', 'Remove stored copy']);
  });

  it('offers only access for a deployment credential', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(
      settings({ credentials: [portal({ name: 'forge.github', source: 'config' })] }),
    );
    render(CredentialsCard);
    await screen.findByText('portal.example');
    expect(await menuLabels('forge.github')).toEqual(['Edit access']);
  });

  it('offers no access edit for a credential with no site', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(
      settings({ credentials: [portal({ hosts: [] })] }),
    );
    render(CredentialsCard);
    await screen.findByText(/No site/);
    await openMenu('portal');
    const edit = await screen.findByText('Edit access');
    expect(edit.hasAttribute('data-disabled')).toBe(true);
  });
});

describe('access', () => {
  it('edits access from the row menu and saves narrow defaults', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(settings());
    vi.mocked(saveCredentialGrant).mockResolvedValue({ ok: true });
    render(CredentialsCard);
    await screen.findByText('portal.example');
    await chooseAction('portal', 'Edit access');
    expect(screen.getByRole('dialog', { name: 'Access for portal' })).toBeTruthy();
    expect((screen.getByLabelText('Allow scheduled tasks') as HTMLInputElement).checked).toBe(
      false,
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Save access' }));
    await waitFor(() =>
      expect(saveCredentialGrant).toHaveBeenCalledWith('portal', {
        scope_mode: 'all',
        rooms: [],
        allow_scheduled: false,
        allow_http: false,
      }),
    );
  });

  it('cancels access edits without saving', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(settings());
    render(CredentialsCard);
    await screen.findByText('portal.example');
    await chooseAction('portal', 'Edit access');
    const dialog = screen.getByRole('dialog', { name: 'Access for portal' });
    await fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Allow scheduled tasks' }));
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(saveCredentialGrant).not.toHaveBeenCalled();
    await chooseAction('portal', 'Edit access');
    expect(screen.getByRole('checkbox', { name: 'Allow scheduled tasks' })).not.toBeChecked();
  });

  it('asks before revoking access', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(
      settings({
        credentials: [portal({ grant: { scope_mode: 'all', rooms: [], allow_scheduled: false } })],
      }),
    );
    vi.mocked(revokeCredentialGrant).mockResolvedValue({ ok: true });
    render(CredentialsCard);
    await screen.findByText('portal.example');
    await chooseAction('portal', 'Revoke access');
    expect(revokeCredentialGrant).not.toHaveBeenCalled();
    const dialog = screen.getByRole('dialog');
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Revoke' }));
    await waitFor(() => expect(revokeCredentialGrant).toHaveBeenCalledWith('portal'));
  });

  it('requires confirmation for allow-all-existing and keeps a failed save visible', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(settings({ grant_existing_available: true }));
    vi.mocked(grantExistingCredentials).mockResolvedValue({ ok: true, count: 1 });
    render(CredentialsCard);
    await screen.findByText('portal.example');
    await fireEvent.click(screen.getByRole('button', { name: 'Allow all existing' }));
    expect(grantExistingCredentials).not.toHaveBeenCalled();
    const dialog = screen.getByRole('dialog');
    await fireEvent.click(dialog.querySelector('.btn-primary')!);
    await waitFor(() => expect(grantExistingCredentials).toHaveBeenCalledOnce());
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    await chooseAction('portal', 'Edit access');
    vi.mocked(saveCredentialGrant).mockRejectedValueOnce(new Error('Could not save policy'));
    await fireEvent.click(screen.getByRole('button', { name: 'Save access' }));
    await waitFor(() =>
      expect(screen.getByRole('dialog').textContent).toContain('Could not save policy'),
    );
  });

  it('drops unavailable room selections so access can still be narrowed', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(
      settings({
        credentials: [
          portal({
            grant: {
              scope_mode: 'rooms',
              rooms: ['live-room', 'deleted-room'],
              allow_scheduled: false,
            },
          }),
        ],
        rooms: [{ token: 'live-room', name: 'Personal' }],
      }),
    );
    vi.mocked(saveCredentialGrant).mockResolvedValue({ ok: true });
    render(CredentialsCard);
    await screen.findByText('portal.example');
    await chooseAction('portal', 'Edit access');
    await fireEvent.click(screen.getByRole('button', { name: 'Save access' }));
    await waitFor(() =>
      expect(saveCredentialGrant).toHaveBeenCalledWith('portal', {
        scope_mode: 'rooms',
        rooms: ['live-room'],
        allow_scheduled: false,
        allow_http: false,
      }),
    );
  });

  it('requires an explicit HTTP override', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(
      settings({ credentials: [portal({ hosts: ['http://192.0.2.10:8080'] })] }),
    );
    render(CredentialsCard);
    await screen.findByText('http://192.0.2.10:8080');
    expect(within(row('portal')).getByText('HTTPS required')).toBeTruthy();
    await chooseAction('portal', 'Edit access');
    const toggle = screen.getByLabelText('Allow HTTP (override HTTPS requirement)');
    expect((toggle as HTMLInputElement).checked).toBe(false);
    await fireEvent.click(toggle);
    await fireEvent.click(screen.getByRole('button', { name: 'Save access' }));
    await waitFor(() =>
      expect(saveCredentialGrant).toHaveBeenCalledWith(
        'portal',
        expect.objectContaining({ allow_http: true }),
      ),
    );
  });
});

describe('deletion', () => {
  it('removes the stored copy of a KeePassXC credential after confirming', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(settings());
    vi.mocked(deleteCredential).mockResolvedValue({ ok: true, deleted: true });
    render(CredentialsCard);
    await screen.findByText('portal.example');
    await chooseAction('portal', 'Remove stored copy');
    const dialog = screen.getByRole('dialog', { name: 'Remove stored copy' });
    expect(words(dialog)).toContain('the next sync brings it back without its access settings');
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(deleteCredential).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    await chooseAction('portal', 'Remove stored copy');
    vi.mocked(getCredentialGrants).mockResolvedValue(settings({ credentials: [] }));
    await fireEvent.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'Remove' }),
    );
    await waitFor(() => expect(deleteCredential).toHaveBeenCalledWith('portal'));
    await screen.findByText('No credentials yet.');
  });

  it('says a deleted Istota credential cannot be recovered', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(settings({ credentials: [local()] }));
    vi.mocked(deleteCredential).mockResolvedValue({ ok: true, deleted: true });
    render(CredentialsCard);
    await screen.findByText('openrouter.ai');
    await chooseAction('openrouter_key', 'Delete');
    const dialog = screen.getByRole('dialog', { name: 'Delete credential' });
    expect(words(dialog)).toContain(
      'Delete openrouter_key? Tasks lose it now, and it cannot be recovered.',
    );
    expect(words(dialog)).not.toContain('KeePassXC');
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(deleteCredential).toHaveBeenCalledWith('openrouter_key'));
  });

  it('keeps a credential visible if deletion fails', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(settings());
    vi.mocked(deleteCredential).mockRejectedValueOnce(new Error('Could not delete credential'));
    render(CredentialsCard);
    await screen.findByText('portal.example');
    await chooseAction('portal', 'Remove stored copy');
    await fireEvent.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'Remove' }),
    );
    await screen.findByRole('alert');
    expect(row('portal')).toBeTruthy();
  });
});

describe('the credential form', () => {
  it('opens empty each time, so a cancelled value does not come back', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(settings());
    render(CredentialsCard);
    await screen.findByText('portal.example');
    await fireEvent.click(screen.getByRole('button', { name: 'Add credential' }));
    const value = screen.getByLabelText(/^Secret/) as HTMLInputElement;
    await fireEvent.input(value, { target: { value: 'sk-or-secret' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());

    await fireEvent.click(screen.getByRole('button', { name: 'Add credential' }));
    expect((screen.getByLabelText(/^Secret/) as HTMLInputElement).value).toBe('');
    expect(document.body.textContent).not.toContain('sk-or-secret');
  });

  it('opens Edit for an Istota credential with its site filled in', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(settings({ credentials: [local()] }));
    render(CredentialsCard);
    await screen.findByText('openrouter.ai');
    await chooseAction('openrouter_key', 'Edit');
    const dialog = screen.getByRole('dialog', { name: 'Edit openrouter_key' });
    expect((within(dialog).getByLabelText('Site (optional)') as HTMLInputElement).value).toBe(
      'openrouter.ai',
    );
  });
});

it('labels local and vault credentials with two-factor', async () => {
  vi.mocked(getCredentialGrants).mockResolvedValue(
    settings({
      credentials: [portal({ otp: true }), local({ otp: true, otp_set: true })],
    }),
  );
  render(CredentialsCard);
  await waitFor(() => expect(screen.getAllByText('2FA')).toHaveLength(2));
});

function made(over: Partial<CredentialSummary> = {}): CredentialSummary {
  return portal({
    name: 'generated_acme',
    source: 'generated',
    hosts: ['acme.example'],
    otp: true,
    generated: { mirror: true, state: 'mirrored', divergence: [] },
    ...over,
  });
}

describe('generated credentials', () => {
  it('names a KeePass copy that lost its two-factor seed', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(
      settings({
        vault_enabled: true,
        credentials: [
          made(),
          made({
            name: 'generated_other',
            generated: { mirror: true, state: 'diverged', divergence: ['missing_otp'] },
          }),
        ],
      }),
    );
    render(CredentialsCard);
    await screen.findAllByText('acme.example');
    expect(within(row('generated_acme')).getByText('Generated')).toBeTruthy();
    expect(within(row('generated_acme')).queryByText(/KeePass copy/)).toBeNull();
    expect(within(row('generated_other')).getByText('KeePass copy missing 2FA')).toBeTruthy();
  });

  it('turns the copy off, rewrites it, and offers no copy without a vault', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(
      settings({
        vault_enabled: true,
        credentials: [made({ generated: { mirror: true, state: 'pending', divergence: [] } })],
      }),
    );
    vi.mocked(remirrorGenerated).mockResolvedValue({ ok: true, state: 'mirrored' });
    vi.mocked(setGeneratedMirror).mockResolvedValue({ ok: true, state: 'off' });
    render(CredentialsCard);
    await screen.findByText('acme.example');
    expect(within(row('generated_acme')).getByText('KeePass copy behind')).toBeTruthy();
    expect(await menuLabels('generated_acme')).toEqual(
      expect.arrayContaining(['Stop KeePass copy', 'Write KeePass copy now', 'Retire']),
    );
    await fireEvent.click(await screen.findByText('Write KeePass copy now'));
    await waitFor(() => expect(remirrorGenerated).toHaveBeenCalledWith('generated_acme'));
    await chooseAction('generated_acme', 'Stop KeePass copy');
    await waitFor(() => expect(setGeneratedMirror).toHaveBeenCalledWith('generated_acme', false));

    cleanup();
    vi.mocked(getCredentialGrants).mockResolvedValue(
      settings({ vault_enabled: false, credentials: [made()] }),
    );
    render(CredentialsCard);
    await screen.findByText('acme.example');
    expect(await menuLabels('generated_acme')).not.toContain('Stop KeePass copy');
    expect(screen.queryByText('Copy new generated credentials to KeePass')).toBeNull();
  });

  it('flips the default for new generated credentials', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(
      settings({ vault_enabled: true, generated_default_mirror: true, credentials: [made()] }),
    );
    vi.mocked(setGeneratedDefaultMirror).mockResolvedValue({ ok: true });
    render(CredentialsCard);
    await fireEvent.click(await screen.findByText('Copy new generated credentials to KeePass'));
    await waitFor(() => expect(setGeneratedDefaultMirror).toHaveBeenCalledWith(false));
  });

  it('retires after a confirmation that says the copy goes too', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(settings({ credentials: [made()] }));
    vi.mocked(deleteCredential).mockResolvedValue({ ok: true, deleted: true });
    render(CredentialsCard);
    await screen.findByText('acme.example');
    await chooseAction('generated_acme', 'Retire');
    const dialog = screen.getByRole('dialog', { name: 'Retire credential' });
    expect(words(dialog)).toContain('its KeePass copy is removed, and it cannot be recovered');
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Retire' }));
    await waitFor(() => expect(deleteCredential).toHaveBeenCalledWith('generated_acme'));
  });

  it('shows recovery codes only after an explicit Show', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(
      settings({ credentials: [made({ recovery: true })] }),
    );
    vi.mocked(showRecoveryCodes).mockResolvedValue({ codes: 'fixture-rc-1111\nfixture-rc-2222' });
    render(CredentialsCard);
    await screen.findByText('acme.example');
    expect(within(row('generated_acme')).getByText('Recovery codes')).toBeTruthy();
    await chooseAction('generated_acme', 'Show recovery codes');
    const dialog = screen.getByRole('dialog', { name: 'Recovery codes for generated_acme' });
    expect(showRecoveryCodes).not.toHaveBeenCalled();
    expect(screen.queryByTestId('recovery-codes')).toBeNull();
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Show codes' }));
    await waitFor(() =>
      expect(showRecoveryCodes).toHaveBeenCalledWith('generated_acme', undefined),
    );
    expect((await screen.findByTestId('recovery-codes')).textContent).toBe(
      'fixture-rc-1111\nfixture-rc-2222',
    );
  });

  it('asks for the account password when the server does', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(
      settings({ credentials: [made({ recovery: true })] }),
    );
    vi.mocked(showRecoveryCodes)
      .mockRejectedValueOnce(
        Object.assign(new Error('Enter your account password to show the codes.'), {
          status: 403,
          field: 'password',
        }),
      )
      .mockResolvedValueOnce({ codes: 'fixture-rc-1111' });
    render(CredentialsCard);
    await screen.findByText('acme.example');
    await chooseAction('generated_acme', 'Show recovery codes');
    const dialog = screen.getByRole('dialog', { name: 'Recovery codes for generated_acme' });
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Show codes' }));
    const password = await within(dialog).findByLabelText('Account password');
    expect(within(dialog).getByRole('alert').textContent).toContain('account password');
    await fireEvent.input(password, { target: { value: 'my password' } });
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Show codes' }));
    await waitFor(() =>
      expect(showRecoveryCodes).toHaveBeenLastCalledWith('generated_acme', 'my password'),
    );
    expect((await screen.findByTestId('recovery-codes')).textContent).toBe('fixture-rc-1111');
  });

  it('offers no reveal for a generated credential without codes', async () => {
    vi.mocked(getCredentialGrants).mockResolvedValue(settings({ credentials: [made()] }));
    render(CredentialsCard);
    await screen.findByText('acme.example');
    expect(await menuLabels('generated_acme')).not.toContain('Show recovery codes');
  });
});
