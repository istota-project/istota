/**
 * The credential vault's form, under "Connected services".
 *
 * Its sibling `vaultHeading.svelte.test.ts` covers the *status line*, which
 * renders only for a vault that exists. This covers the form, and the split
 * between the two is the point: the user this page most needs to serve is the
 * one with **no** vault yet, and the heading is by design silent for them.
 *
 * Three properties carry the file, and each is a boundary rather than a
 * preference.
 *
 * **No absolute path.** The form takes a path relative to the user's own
 * workspace and says so. An absolute one is checked against the trees a sandbox
 * binds read-write rather than against one user's directory — the right
 * question for a path an operator wrote into `config.toml`, and not a line a
 * user may put themselves on the far side of, since it would read any
 * daemon-readable file as the daemon user and decrypt the result into that
 * user's own credential rows. The server refuses one; what is asserted here is
 * that the form reports the refusal rather than swallowing it.
 *
 * **`editable: false` is precedence, not permission.** A vault set in
 * `config.toml` is not writable here because a stored row would outrank that
 * line and make the operator's file silently inert. The form says that in
 * words instead of showing disabled controls with no explanation.
 *
 * **The minted passphrase is rendered exactly once.** Nothing reads it back —
 * there is no route that could — so the response that mints it is the only
 * place it exists outside the user's own clipboard. The assertions are that it
 * appears there and that a *typed* one never does.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { render, cleanup, screen, waitFor, fireEvent } from '@testing-library/svelte';

/** A `Button` renders its label as its accessible name; there is no test id. */
function button(name: RegExp) {
  return screen.queryByRole('button', { name });
}

/**
 * `textContent` keeps the source's own line wrapping, so a phrase that wraps
 * in the markup carries a newline and a run of indentation. Collapse it: the
 * assertions are about what is said, not about where prettier broke the line.
 */
/** The passphrase input, found the way every other credential field is. */
function passwordField(): HTMLElement {
  return screen.getByLabelText(/master password/i);
}

function words(el: HTMLElement): string {
  return (el.textContent ?? '').replace(/\s+/g, ' ').trim();
}
import type { ServiceCard as ServiceCardData, VaultStatus } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);

const KARAKEEP: ServiceCardData = {
  service: 'karakeep',
  label: 'Karakeep',
  status: 'configured',
  fields: [{ key: 'api_key', label: 'API key', type: 'password' }],
  configured_keys: ['api_key'],
  last_updated: null,
  vault_managed: false,
};

await fillApiDouble(api, {
  getSettingsServices: vi.fn(async () => ({ services: [KARAKEEP] })),
  getModules: vi.fn(async () => ({ modules: [] })),
  getProfile: vi.fn(async () => ({
    profile: {
      user_id: 'alice',
      display_name: 'Alice',
      timezone: 'UTC',
      email_addresses: [],
      trusted_email_senders: [],
      quiet_email_senders: [],
      disabled_skills: [],
      disabled_modules: [],
      routing: {},
      default_destination: 'talk',
      default_room: '',
      delivery_surfaces: ['talk'],
    },
  })),
  updateProfile: vi.fn(async () => ({})),
  disconnectNextcloudToken: vi.fn(async () => ({})),
  uploadAvatar: vi.fn(async () => ({ hash: 'h1', mime: 'image/webp', bytes: 1 })),
  deleteAvatar: vi.fn(async () => ({ deleted: true })),
  avatarUrl: vi.fn(() => '/api/avatars/user/alice'),
});

const native = vi.hoisted(() => ({
  isNativeShell: vi.fn(() => false),
  shellVersion: vi.fn(() => ''),
  shellAtLeast: vi.fn(() => false),
  onKeyboardGeometry: vi.fn(() => () => {}),
}));
vi.mock('$lib/platform/native', () => native);

import Page from './+page.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';
import type { User } from '$lib/api';

const person: User = {
  username: 'alice',
  display_name: 'Alice',
  bot_name: 'Istota',
  is_admin: false,
  features: {
    chat: true,
    feeds: false,
    location: false,
    money: false,
    health: false,
    briefings: false,
    google_workspace: false,
    google_workspace_enabled: false,
    admin: false,
  },
};

// The real shapes, which is the point of the fixture: `ntfy` is five fields and
// `karakeep` two, so a form that names only the service is naming the wrong
// thing.
const ELIGIBLE = [
  { service: 'karakeep', label: 'Karakeep', keys: ['Base URL', 'API key'] },
  {
    service: 'ntfy',
    label: 'ntfy push',
    keys: [
      'Server URL',
      'Default topic',
      'Access token (optional)',
      'Username (optional)',
      'Password (optional)',
    ],
  },
];

/** The unconfigured answer: no vault, and the form's own half beside it. */
function unconfigured(over: Partial<VaultStatus> = {}): VaultStatus {
  return {
    configured: false,
    editable: true,
    source: '',
    vault_path: '',
    vault_root: '/mnt/shared/Users/alice',
    eligible_services: ELIGIBLE,
    passphrase_present: false,
    ...over,
  };
}

function configured(over: Partial<VaultStatus> = {}): VaultStatus {
  return {
    ...unconfigured(),
    configured: true,
    source: 'db',
    vault_path: 'config/vault.kdbx',
    path: '/mnt/shared/Users/alice/config/vault.kdbx',
    owned: ['karakeep'],
    passphrase_present: true,
    outcome: '',
    reason: '',
    refusal: '',
    last_success_at: '2025-01-15T10:00:00Z',
    last_sync_at: '2025-01-15T10:00:00Z',
    last_outcome: 'ok',
    last_reason: '',
    parsed: false,
    problem: '',
    ...over,
  };
}

async function mount() {
  render(Harness, { component: Page, user: person });
  await waitFor(() => expect(api.getSettingsServices).toHaveBeenCalled());
  await waitFor(() => expect(api.getVaultStatus).toHaveBeenCalled());
}

beforeEach(() => {
  api.getVaultStatus.mockReset();
  api.updateVaultConfig.mockReset();
  api.clearVaultConfig.mockReset();
  api.setVaultPassphrase.mockReset();
  api.updateVaultConfig.mockResolvedValue(undefined);
  api.clearVaultConfig.mockResolvedValue(undefined);
});

afterEach(cleanup);

describe('a user with no vault yet', () => {
  it('is offered the form, which is the case the heading is silent for', async () => {
    api.getVaultStatus.mockResolvedValue(unconfigured());
    await mount();

    expect(screen.getByTestId('vault-form')).toBeTruthy();
    // The control that says this test is about the split rather than about the
    // form merely existing.
    expect(screen.queryByTestId('vault-status')).toBeNull();
  });

  it('offers only the services a vault may own, with their labels', async () => {
    // Server-rendered, so the form cannot offer a name the write would refuse.
    api.getVaultStatus.mockResolvedValue(unconfigured());
    await mount();

    expect(screen.getByLabelText(/Karakeep/)).toBeTruthy();
    expect(screen.getByLabelText(/ntfy push/)).toBeTruthy();
    // `vault` itself and the daemon-written services are not eligible and the
    // server does not send them, so nothing here should invent one.
    expect(screen.queryByLabelText(/garmin/i)).toBeNull();
  });

  it('names the fields each service hands over, not just the service', async () => {
    // The checkbox label alone reads as "the API key". It is not: ntfy is five
    // fields and karakeep two, and ownership includes *deletion* — a field the
    // file does not hold is removed from the secrets table. A user picking from
    // service names can lose a value they never had in mind.
    api.getVaultStatus.mockResolvedValue(unconfigured());
    await mount();

    const form = screen.getByTestId('vault-form');
    expect(words(form)).toContain('Base URL · API key');
    expect(words(form)).toContain('Password (optional)');
    // And the warning says deletion out loud, since that is the half a user
    // cannot undo by unticking the box afterwards.
    expect(words(form)).toMatch(/removes any the file does not/i);
    expect(words(form)).toMatch(/put every value you already have into the file first/i);
  });

  it('says the path is relative and offers no way to write an absolute one', async () => {
    api.getVaultStatus.mockResolvedValue(unconfigured());
    await mount();

    const form = screen.getByTestId('vault-form');
    expect(words(form)).toMatch(/inside your own files/i);
    expect(words(form)).toMatch(/absolute path is an administrator setting/i);
  });

  it('says where a relative path lands, as it is typed', async () => {
    // The example alone is ambiguous: the directory a relative path resolves
    // under is the same one holding the inbox, memories and shared folders, so
    // `config/vault.kdbx` reads as though it might be relative to any of them.
    api.getVaultStatus.mockResolvedValue(unconfigured());
    await mount();

    // Before anything is typed, the root is still named.
    expect(words(screen.getByTestId('vault-form'))).toContain('/mnt/shared/Users/alice');

    await fireEvent.input(screen.getByTestId('vault-path-input'), {
      target: { value: 'shared/vault.kdbx' },
    });
    const resolved = await screen.findByTestId('vault-resolved');
    expect(words(resolved)).toContain('/mnt/shared/Users/alice/shared/vault.kdbx');
  });

  it('does not build a half path when the server could not name a root', async () => {
    // A deployment whose root cannot be resolved falls back to naming the
    // directory in words rather than showing a path with a missing front half,
    // which would be worse than saying nothing.
    api.getVaultStatus.mockResolvedValue(unconfigured({ vault_root: '' }));
    await mount();

    await fireEvent.input(screen.getByTestId('vault-path-input'), {
      target: { value: 'vault.kdbx' },
    });
    expect(screen.queryByTestId('vault-resolved')).toBeNull();
    // And the rule is still stated, in the field's own warning.
    expect(words(screen.getByTestId('vault-form'))).toMatch(/inside your own files/i);
  });

  it('will not save an empty path', async () => {
    api.getVaultStatus.mockResolvedValue(unconfigured());
    await mount();

    expect((button(/save vault settings/i) as HTMLButtonElement).disabled).toBe(true);
    expect(api.updateVaultConfig).not.toHaveBeenCalled();
  });

  it('sends the path and the ticked services', async () => {
    api.getVaultStatus.mockResolvedValue(unconfigured());
    await mount();

    await fireEvent.input(screen.getByTestId('vault-path-input'), {
      target: { value: 'config/v.kdbx' },
    });
    await fireEvent.click(screen.getByLabelText(/Karakeep/));
    await fireEvent.click(button(/save vault settings/i)!);

    await waitFor(() =>
      expect(api.updateVaultConfig).toHaveBeenCalledWith('config/v.kdbx', ['karakeep']),
    );
  });

  it('reports a refusal from the server rather than swallowing it', async () => {
    // The absolute-path case reaches the user this way: the form has no rule of
    // its own, because the containment rule belongs to the resolver and a copy
    // here would be a second opinion about a boundary.
    api.getVaultStatus.mockResolvedValue(unconfigured());
    api.updateVaultConfig.mockRejectedValue(
      new Error('a vault path set here must be relative to your own workspace'),
    );
    await mount();

    await fireEvent.input(screen.getByTestId('vault-path-input'), {
      target: { value: '/etc/shadow' },
    });
    await fireEvent.click(button(/save vault settings/i)!);

    const err = await screen.findByTestId('vault-error');
    expect(words(err)).toMatch(/must be relative/i);
  });
});

describe('a vault the operator set in config.toml', () => {
  it('is explained rather than shown as a dead form', async () => {
    api.getVaultStatus.mockResolvedValue(configured({ source: 'toml', editable: false }));
    await mount();

    const form = screen.getByTestId('vault-form');
    expect(words(form)).toMatch(/set in this deployment's configuration file/i);
    // Not disabled controls with no explanation: the fields are absent, so
    // there is nothing to wonder about.
    expect(screen.queryByTestId('vault-path-input')).toBeNull();
    expect(button(/save vault settings/i)).toBeNull();
    expect(button(/generate/i)).toBeNull();
  });
});

describe('a vault the user set themselves', () => {
  it('offers a switch-off, which the unconfigured case does not', async () => {
    api.getVaultStatus.mockResolvedValue(configured());
    await mount();
    expect(button(/switch off/i)!).toBeTruthy();

    await fireEvent.click(button(/switch off/i)!);
    await waitFor(() => expect(api.clearVaultConfig).toHaveBeenCalled());
  });

  it('shows no switch-off when there is nothing stored to switch off', async () => {
    api.getVaultStatus.mockResolvedValue(unconfigured());
    await mount();
    expect(button(/switch off/i)).toBeNull();
  });
});

describe('the passphrase', () => {
  it('shows a generated one once, and says it will not be shown again', async () => {
    api.getVaultStatus.mockResolvedValue(unconfigured());
    api.setVaultPassphrase.mockResolvedValue({ ok: true, generated: 'MINTED-VALUE-123' });
    await mount();

    await fireEvent.click(button(/generate/i)!);

    const shown = await screen.findByTestId('vault-minted');
    expect(words(shown)).toContain('MINTED-VALUE-123');
    expect(words(shown)).toMatch(/will not be shown again/i);
    expect(api.setVaultPassphrase).toHaveBeenCalledWith({ generate: true, replace: false });
  });

  it('generates straight away when there is nothing to destroy', async () => {
    api.getVaultStatus.mockResolvedValue(unconfigured());
    api.setVaultPassphrase.mockResolvedValue({ ok: true, generated: 'FIRST-VALUE' });
    await mount();

    await fireEvent.click(button(/generate/i)!);

    await waitFor(() => expect(api.setVaultPassphrase).toHaveBeenCalled());
    expect(api.setVaultPassphrase).toHaveBeenCalledWith({ generate: true, replace: false });
  });

  it('asks before replacing one, and does not send until confirmed', async () => {
    // The page's one irreversible action. The KDBX is encrypted under the
    // stored value and generating does not re-encrypt it, so a second mint
    // destroys the only copy of the passphrase that opens the file. The server
    // refuses it without `replace`; this is the half that means a user is told
    // rather than shown a 409.
    api.getVaultStatus.mockResolvedValue(configured({ passphrase_present: true }));
    api.setVaultPassphrase.mockResolvedValue({ ok: true, generated: 'SECOND-VALUE' });
    await mount();

    await fireEvent.click(button(/generate/i)!);
    // Nothing has gone out yet, which is the assertion that distinguishes a
    // dialog from a dialog that fires and then asks.
    expect(api.setVaultPassphrase).not.toHaveBeenCalled();

    const dialog = await screen.findByText(/will stop opening until you set the new password/i);
    expect(dialog).toBeTruthy();

    // The dialog's own confirm, which is deliberately not the card button's
    // wording: two buttons reading "Generate a new one" is one ambiguous
    // accessible name, and the destructive one should say what it destroys.
    await fireEvent.click(button(/^replace it$/i)!);
    await waitFor(() => expect(api.setVaultPassphrase).toHaveBeenCalled());
    expect(api.setVaultPassphrase).toHaveBeenCalledWith({ generate: true, replace: true });
  });

  it('never sends replace on the typed path', async () => {
    // Generate-only, matching the CLI's `--force`: re-storing a value the user
    // is holding destroys nothing they cannot type again.
    api.getVaultStatus.mockResolvedValue(configured({ passphrase_present: true }));
    api.setVaultPassphrase.mockResolvedValue({ ok: true, generated: '' });
    await mount();

    await fireEvent.input(passwordField(), {
      target: { value: 'a-passphrase-long-enough-to-pass-the-floor' },
    });
    await fireEvent.click(button(/save password/i)!);

    await waitFor(() => expect(api.setVaultPassphrase).toHaveBeenCalled());
    expect(api.setVaultPassphrase).toHaveBeenCalledWith({
      passphrase: 'a-passphrase-long-enough-to-pass-the-floor',
    });
  });

  it('never renders a typed one back', async () => {
    // The server returns an empty `generated` for a typed value, and the form
    // must not fill that gap from the input it still holds — a passphrase on
    // screen after a save is one a shoulder or a screenshot gets for free.
    api.getVaultStatus.mockResolvedValue(unconfigured());
    api.setVaultPassphrase.mockResolvedValue({ ok: true, generated: '' });
    await mount();

    const input = passwordField() as HTMLInputElement;
    await fireEvent.input(input, { target: { value: 'correct-horse-battery-staple-and-more' } });
    await fireEvent.click(button(/save password/i)!);

    await waitFor(() => expect(api.setVaultPassphrase).toHaveBeenCalled());
    expect(screen.queryByTestId('vault-minted')).toBeNull();
    // And the field is cleared, so it is not sitting in the DOM either.
    expect(input.value).toBe('');
    expect(screen.getByTestId('vault-form').textContent).not.toContain(
      'correct-horse-battery-staple',
    );
  });

  it('is a password field, so it is not typed in the clear', async () => {
    api.getVaultStatus.mockResolvedValue(unconfigured());
    await mount();
    expect((passwordField() as HTMLInputElement).type).toBe('password');
  });

  it('will not send an empty typed value', async () => {
    api.getVaultStatus.mockResolvedValue(unconfigured());
    await mount();
    expect((button(/save password/i) as HTMLButtonElement).disabled).toBe(true);
  });

  it('says a passphrase is stored without offering to show it', async () => {
    api.getVaultStatus.mockResolvedValue(configured({ passphrase_present: true }));
    await mount();

    const form = screen.getByTestId('vault-form');
    // `SecretField` says a value is set through its placeholder rather than
    // by rendering anything — which is the whole point of it, and why this
    // assertion is on the attribute and not on the card's text.
    expect((passwordField() as HTMLInputElement).placeholder).toMatch(/stored — enter to replace/i);
    expect(words(form)).toMatch(/cannot be shown to you again/i);
  });

  it('reports a refusal from the floor the CLI applies', async () => {
    api.getVaultStatus.mockResolvedValue(unconfigured());
    api.setVaultPassphrase.mockRejectedValue(
      new Error('a vault passphrase must be at least 32 characters'),
    );
    await mount();

    await fireEvent.input(passwordField(), {
      target: { value: 'short' },
    });
    await fireEvent.click(button(/save password/i)!);

    const err = await screen.findByTestId('vault-error');
    expect(words(err)).toMatch(/at least 32 characters/i);
  });
});
