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
 * **No path at all.** The card offers the `.kdbx` files the server found in the
 * user's vault folder, and what a save carries is one of those names. A
 * filename is validated by membership in a listing rather than by a parse, so
 * there is no traversal to refuse, no absolute form to reject and nothing for
 * this side to restate — which is why the tests below assert on what is offered
 * rather than on a rule the form applies.
 *
 * **`editable: false` is precedence, not permission.** A vault whose file is
 * set in the deployment's configuration is not selectable here because a stored
 * choice would be a control that does nothing. The form says that in words
 * instead of showing disabled controls with no explanation.
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

/** The bits-ui trigger renders as a button carrying the control's aria-label. */
function control(label: string) {
  return screen.queryByRole('button', { name: label });
}

/**
 * Pick an option out of a `Select`, the way bits-ui actually listens for it.
 *
 * A plain `click` on the item opens nothing and selects nothing — the item
 * commits on pointerup — so a test written with `click` passes while asserting
 * against a value that never changed. Lifted from
 * `RoomSettings.svelte.test.ts`, which found that the expensive way.
 */
async function pick(ariaLabel: string, optionLabel: string) {
  const trigger = screen.getByRole('button', { name: ariaLabel });
  await fireEvent.pointerDown(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.pointerUp(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.click(trigger);
  const item = screen.getByText(optionLabel).closest('[data-select-item]');
  if (!item) throw new Error(`no option ${optionLabel} under ${ariaLabel}`);
  await fireEvent.pointerMove(item, { pointerType: 'mouse' });
  await fireEvent.pointerDown(item, { pointerType: 'mouse', button: 0 });
  await fireEvent.pointerUp(item, { pointerType: 'mouse', button: 0 });
}

/**
 * `textContent` keeps the source's own line wrapping, so a phrase that wraps
 * in the markup carries a newline and a run of indentation. Collapse it: the
 * assertions are about what is said, not about where prettier broke the line.
 */
/**
 * The passphrase input, found the way every other credential field is.
 *
 * Anchored rather than a loose substring: the field carries a `hint`, and
 * `HintPopover` labels its own trigger "About Master password", which an
 * unanchored match also finds — so `/master password/i` raised "found
 * multiple elements" rather than reaching either one. Not `exact` either:
 * the popover's "?" trigger sits inside the same `field-label` span, so
 * the label's text is not the label prop on its own.
 */
function passwordField(): HTMLElement {
  return screen.getByLabelText(/^master password/i);
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

const VAULT_DIR = '/mnt/shared/Users/alice/istota/vault';

/** The unconfigured answer: no file and no passphrase, which is everybody. */
function unconfigured(over: Partial<VaultStatus> = {}): VaultStatus {
  return {
    configured: false,
    editable: true,
    vault_dir: VAULT_DIR,
    files: [],
    vault_file: '',
    passphrase_present: false,
    ...over,
  };
}

function configured(over: Partial<VaultStatus> = {}): VaultStatus {
  return {
    ...unconfigured(),
    configured: true,
    files: ['personal.kdbx'],
    vault_file: 'personal.kdbx',
    path: `${VAULT_DIR}/personal.kdbx`,
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
  api.selectVaultFile.mockReset();
  api.setVaultPassphrase.mockReset();
  api.selectVaultFile.mockResolvedValue(undefined);
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

  it('names the folder to put the file in, which is the whole instruction', async () => {
    api.getVaultStatus.mockResolvedValue(unconfigured());
    await mount();

    const folder = screen.getByTestId('vault-folder');
    expect(words(folder)).toContain(VAULT_DIR);
    expect(words(folder)).toMatch(/put your keepassxc file in/i);
  });

  it('offers nothing to type, because a path is not what is asked for', async () => {
    api.getVaultStatus.mockResolvedValue(unconfigured());
    await mount();

    // The free-text path input, the resolved-path preview and the service
    // checkboxes are all gone with the value that needed them.
    expect(screen.queryByTestId('vault-path-input')).toBeNull();
    expect(screen.queryByTestId('vault-resolved')).toBeNull();
    expect(screen.queryByLabelText(/Karakeep/)).toBeNull();
    expect(button(/save vault settings/i)).toBeNull();
  });

  it('says so plainly when this deployment cannot reach the files', async () => {
    // An rclone-backed deployment with no local mount: the folder cannot be
    // listed, so the file is an operator setting and the card says that rather
    // than naming a folder that is not there.
    api.getVaultStatus.mockResolvedValue(unconfigured({ vault_dir: '' }));
    await mount();

    expect(words(screen.getByTestId('vault-folder'))).toMatch(
      /cannot reach your files on this deployment/i,
    );
  });
});

describe('the file in the folder', () => {
  it('is read with no dropdown and nothing stored when there is one', async () => {
    // The ordinary case, and the reason auto-selection is a rule in the
    // resolver rather than a write this page makes on first render.
    api.getVaultStatus.mockResolvedValue(configured());
    await mount();

    expect(words(screen.getByTestId('vault-folder'))).toContain('personal.kdbx');
    expect(control('Vault file')).toBeNull();
    expect(api.selectVaultFile).not.toHaveBeenCalled();
  });

  it('is a question when there are several and none is chosen', async () => {
    api.getVaultStatus.mockResolvedValue(
      configured({
        files: ['personal.kdbx', 'work.kdbx'],
        vault_file: '',
        problem: 'there are several vault files in your vault folder',
      }),
    );
    await mount();

    expect(control('Vault file')).toBeTruthy();
    expect(words(screen.getByTestId('vault-form'))).toMatch(/more than one file/i);
  });

  it('stores the name when one is chosen', async () => {
    api.getVaultStatus.mockResolvedValue(
      configured({ files: ['personal.kdbx', 'work.kdbx'], vault_file: '' }),
    );
    await mount();

    await pick('Vault file', 'work.kdbx');

    await waitFor(() => expect(api.selectVaultFile).toHaveBeenCalledWith('work.kdbx'));
  });

  it('reports a refusal from the server rather than swallowing it', async () => {
    // A file deleted between the page load and the save: the listing is taken
    // again at request time, so the server refuses a name it no longer holds
    // and the card has to say so rather than looking as though it saved.
    api.getVaultStatus.mockResolvedValue(
      configured({ files: ['personal.kdbx', 'work.kdbx'], vault_file: '' }),
    );
    api.selectVaultFile.mockRejectedValue(
      new Error('that file is not in your vault folder any more'),
    );
    await mount();

    await pick('Vault file', 'work.kdbx');

    const err = await screen.findByTestId('vault-error');
    expect(words(err)).toMatch(/not in your vault folder/i);
  });
});

describe('a vault whose file is set in configuration', () => {
  // One case rather than two. `source` carried three values because a stored
  // path from the retired web form was one of them; that table is gone, so
  // what is left is an operator's line or nothing, and `editable` says which.

  it('is explained rather than shown as a dead form', async () => {
    api.getVaultStatus.mockResolvedValue(
      configured({ editable: false, files: ['a.kdbx', 'b.kdbx'] }),
    );
    await mount();

    const form = screen.getByTestId('vault-form');
    expect(words(form)).toMatch(/set outside this page/i);
    // Not disabled controls with no explanation: the file controls are absent,
    // so there is nothing to wonder about.
    expect(control('Vault file')).toBeNull();
    expect(screen.queryByTestId('vault-folder')).toBeNull();
  });

  it('still offers the passphrase, which belongs to the user either way', async () => {
    // Only the *file* half is somebody else's decision. The passphrase is a
    // credential Istota holds to open the file, not a setting an operator
    // made — withholding it left a user whose file an operator had named with
    // no way to store one at all.
    api.getVaultStatus.mockResolvedValue(
      configured({ editable: false, passphrase_present: false }),
    );
    await mount();

    expect(passwordField()).toBeTruthy();
    expect(button(/generate/i)).toBeTruthy();
  });

  it('offers the folder controls again once nothing outranks them', async () => {
    // The control for the two above: they would each pass against a card that
    // had stopped rendering the file half at all.
    api.getVaultStatus.mockResolvedValue(
      configured({ editable: true, files: ['a.kdbx', 'b.kdbx'] }),
    );
    await mount();

    expect(screen.queryByTestId('vault-not-selectable')).toBeNull();
    expect(control('Vault file')).toBeTruthy();
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

    // `SecretField` says a value is set through its placeholder rather than
    // by rendering anything — which is the whole point of it, and why this
    // assertion is on the attribute and not on the card's text.
    expect((passwordField() as HTMLInputElement).placeholder).toMatch(/stored — enter to replace/i);

    // The "cannot be shown again" sentence is the field's `hint`, so it is
    // behind the "?" and renders into a portal only once that is opened —
    // asserting on the card's text would pass only while the sentence was
    // inline, which is what it stopped being.
    await fireEvent.click(screen.getByLabelText('About Master password'));
    expect(words(document.body)).toMatch(/cannot be shown to you again/i);
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
