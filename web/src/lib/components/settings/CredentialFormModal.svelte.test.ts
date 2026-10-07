import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import CredentialFormModal from './CredentialFormModal.svelte';
import type { CredentialSummary } from '$lib/api';
vi.mock('$app/paths', () => ({ base: '/istota' }));
vi.mock('$lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/api')>()),
  createCredential: vi.fn(),
  updateLocalCredential: vi.fn(),
}));
import { createCredential, updateLocalCredential, CredentialWriteError } from '$lib/api';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const SECRET = 'sk-or-v1-not-a-real-secret';

const LOCAL: CredentialSummary = {
  name: 'openrouter_key',
  source: 'local',
  hosts: ['openrouter.ai', 'api2.openrouter.ai'],
  headers: ['authorization', 'x-api-key'],
  revealable: false,
  grant: null,
  url: 'openrouter.ai',
  extra_hosts: 'api2.openrouter.ai',
  username_set: true,
};

function mount(over: Record<string, unknown> = {}) {
  const onClose = vi.fn();
  const onSaved = vi.fn();
  render(CredentialFormModal, {
    props: {
      mode: 'add',
      rooms: [{ token: 'room-1', name: 'Personal' }],
      onClose,
      onSaved,
      ...over,
    },
  });
  return { onClose, onSaved };
}

function input(label: RegExp): HTMLInputElement {
  return screen.getByLabelText(label) as HTMLInputElement;
}

async function type(label: RegExp, value: string) {
  await fireEvent.input(input(label), { target: { value } });
}

describe('adding', () => {
  it('shows the access fields only once a site is typed', async () => {
    mount();
    const access = screen.getByTestId('credential-access');
    expect(within(access).queryByText('Room scope')).toBeNull();
    expect(access.textContent).toContain('Add a site to choose who may use it.');

    await type(/^Site/, 'openrouter.ai');

    expect(within(access).getByText('Room scope')).toBeTruthy();
    expect(
      within(access).getByRole('checkbox', { name: 'Allow scheduled tasks' }),
    ).not.toBeChecked();
  });

  it('shows a refusal under the field it names and keeps the draft', async () => {
    vi.mocked(createCredential).mockRejectedValue(
      new CredentialWriteError('a credential named openrouter_key already exists', 'name'),
    );
    const { onClose } = mount();
    await type(/^Name/, 'openrouter_key');
    await type(/^Secret/, SECRET);
    await fireEvent.click(screen.getByRole('button', { name: 'Add credential' }));

    const name = await screen.findByText('a credential named openrouter_key already exists');
    expect(name.closest('label')?.textContent).toContain('Name');
    expect(input(/^Name/).getAttribute('aria-invalid')).toBe('true');
    expect(input(/^Secret/).value).toBe(SECRET);
    expect(screen.getByRole('dialog')).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('shows a refusal of the whole request as a banner', async () => {
    vi.mocked(createCredential).mockRejectedValue(
      new CredentialWriteError('the request body is not a JSON object', null),
    );
    mount();
    await type(/^Name/, 'openrouter_key');
    await type(/^Secret/, SECRET);
    await fireEvent.click(screen.getByRole('button', { name: 'Add credential' }));

    const banner = await screen.findByRole('alert');
    expect(banner.textContent).toContain('not a JSON object');
    expect(input(/^Secret/).value).toBe(SECRET);
  });

  it('sends the access chosen with the site, then clears the value and closes', async () => {
    vi.mocked(createCredential).mockResolvedValue({
      ok: true,
      name: 'openrouter_key',
      username_name: null,
      url_name: 'openrouter_key_url',
      grant: null,
    });
    const { onClose, onSaved } = mount();
    await type(/^Name/, 'openrouter_key');
    await type(/^Secret/, SECRET);
    await type(/^Site/, 'openrouter.ai');
    const value = input(/^Secret/);
    await fireEvent.click(screen.getByRole('button', { name: 'Add credential' }));

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('openrouter_key'));
    expect(createCredential).toHaveBeenCalledWith({
      name: 'openrouter_key',
      value: SECRET,
      username: '',
      url: 'openrouter.ai',
      extra_hosts: '',
      headers: '',
      revealable: false,
      access: { scope_mode: 'all', rooms: [], allow_scheduled: false, allow_http: false },
    });
    expect(onClose).toHaveBeenCalled();
    expect(value.value).toBe('');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('sends no access without a site', async () => {
    vi.mocked(createCredential).mockResolvedValue({
      ok: true,
      name: 'device_pin',
      username_name: null,
      url_name: null,
      grant: null,
    });
    const { onSaved } = mount();
    await type(/^Name/, 'device_pin');
    await type(/^Secret/, '4321');
    await fireEvent.click(screen.getByRole('button', { name: 'Add credential' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(vi.mocked(createCredential).mock.calls[0][0]).not.toHaveProperty('access');
  });

  it('clears the value on Cancel', async () => {
    const { onClose } = mount();
    await type(/^Secret/, SECRET);
    const value = input(/^Secret/);
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onClose).toHaveBeenCalled();
    expect(value.value).toBe('');
    expect(createCredential).not.toHaveBeenCalled();
  });
});

describe('editing', () => {
  it('has a read-only name and an empty value that keeps the stored one', async () => {
    mount({ mode: 'edit', credential: LOCAL });
    expect(screen.getByRole('dialog', { name: 'Edit openrouter_key' })).toBeTruthy();
    expect(input(/^Name/).readOnly).toBe(true);
    expect(input(/^Name/).value).toBe('openrouter_key');
    expect(input(/^Secret/).value).toBe('');
    expect(screen.getByText('Leave empty to keep the current secret.')).toBeTruthy();
    expect(screen.getByText('Leave empty to keep it.')).toBeTruthy();
    expect(screen.queryByTestId('credential-access')).toBeNull();
  });

  it('keeps the value and username when both are left empty', async () => {
    vi.mocked(updateLocalCredential).mockResolvedValue({
      ok: true,
      name: 'openrouter_key',
      username_name: 'openrouter_key_username',
      url_name: 'openrouter_key_url',
      grant: null,
    });
    const { onSaved } = mount({ mode: 'edit', credential: LOCAL });
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('openrouter_key'));
    expect(updateLocalCredential).toHaveBeenCalledWith('openrouter_key', {
      value: null,
      username: null,
      url: 'openrouter.ai',
      extra_hosts: 'api2.openrouter.ai',
      headers: 'authorization, x-api-key',
      revealable: false,
    });
  });

  it('takes the extra hosts from the server, not from the bound hosts', async () => {
    // The site's bound host is `openrouter.ai` while the typed site is
    // `openrouter.ai:443`; reading extra hosts off `hosts` would keep the old
    // site bound after the site changed.
    vi.mocked(updateLocalCredential).mockResolvedValue({
      ok: true,
      name: 'openrouter_key',
      username_name: null,
      url_name: 'openrouter_key_url',
      grant: null,
    });
    const { onSaved } = mount({
      mode: 'edit',
      credential: { ...LOCAL, hosts: ['openrouter.ai'], url: 'openrouter.ai:443', extra_hosts: '' },
    });
    await type(/^Site/, 'api.example.com');
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(updateLocalCredential).toHaveBeenCalledWith(
      'openrouter_key',
      expect.objectContaining({ url: 'api.example.com', extra_hosts: '' }),
    );
  });

  it('stays open when dismissed while a save is running', async () => {
    let finish: () => void = () => {};
    vi.mocked(updateLocalCredential).mockReturnValue(
      new Promise((resolve) => {
        finish = () =>
          resolve({
            ok: true,
            name: 'openrouter_key',
            username_name: null,
            url_name: null,
            grant: null,
          });
      }),
    );
    const { onClose, onSaved } = mount({ mode: 'edit', credential: LOCAL });
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog')).toBeTruthy();
    finish();
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('removes the username when asked', async () => {
    vi.mocked(updateLocalCredential).mockResolvedValue({
      ok: true,
      name: 'openrouter_key',
      username_name: null,
      url_name: 'openrouter_key_url',
      grant: null,
    });
    const { onSaved } = mount({ mode: 'edit', credential: LOCAL });
    await fireEvent.click(screen.getByRole('checkbox', { name: 'Remove username' }));
    await type(/^Secret/, SECRET);
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(updateLocalCredential).toHaveBeenCalledWith(
      'openrouter_key',
      expect.objectContaining({ value: SECRET, username: '' }),
    );
  });
});

describe('required and optional fields', () => {
  it('requires the name and secret on add and marks the rest optional', () => {
    mount();
    expect(input(/^Name/).required).toBe(true);
    expect(input(/^Secret/).required).toBe(true);
    expect(input(/^Username \(optional\)/).required).toBe(false);
    expect(input(/^Site \(optional\)/).required).toBe(false);
    expect(screen.getByRole('checkbox', { name: 'Tasks may read the secret' })).toBeTruthy();
  });

  it('does not require the secret on edit, where empty keeps the stored one', () => {
    mount({ mode: 'edit', credential: LOCAL });
    expect(input(/^Secret/).required).toBe(false);
  });
});
