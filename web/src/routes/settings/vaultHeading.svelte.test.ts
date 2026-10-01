/**
 * The credential vault's status line, on the vault card in Settings → Credentials.
 *
 * **The case that matters is the one that renders nothing.** A vault is an
 * optional per-user feature that almost nobody has, and a heading that always
 * says something is a heading every user has to read past for the life of the
 * deployment. So the empty render is asserted first and twice — once for the
 * ordinary unconfigured answer and once for an endpoint that failed, since a
 * settings page must not fail to load over a feature its actual content does
 * not depend on.
 *
 * Where it does render, it is a *status line* and nothing else. That used to be
 * the whole story — the vault had no writing endpoint at all — and it no longer
 * is: the form that writes it is a sibling of this heading and is covered by
 * `vaultForm.svelte.test.ts`. What is still true, and is what this file asserts,
 * is that the heading itself reports and never edits, and that nothing it
 * renders is a credential.
 *
 * The security property behind the old read-only rule survives the change
 * rather than being dropped. The two fields still decide which file the daemon
 * decrypts and which credentials it may overwrite, so they are not on
 * `user_profiles` with every other per-user setting; they are in a table of
 * their own that nothing downstream of a task writes. The passphrase is still
 * meant to be *generated* rather than chosen — the file sits in a tree bound
 * read-write into that user's own sandbox, so 256 random bits are what stand
 * between a prompt-injected task and the credentials inside it — which is why
 * the form leads with a Generate button and holds a typed value to the same
 * floor the CLI applies.
 *
 * The card sits below the credentials list: the file is one source for the
 * store the list shows, so the line says whether the sync works and carries no
 * count of its own.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { render, cleanup, screen, waitFor, fireEvent } from '@testing-library/svelte';
import type { ServiceCard as ServiceCardData, VaultStatus } from '$lib/api';
import { formatRelative } from '$lib/dateFormat';

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
  // The Identity card mounts a picture control; stubbed so no <img> here points
  // at an address jsdom would try to fetch.
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

import Page from './credentials/+page.svelte';
import SettingsLayout from './+layout.svelte';
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

function configured(over: Partial<VaultStatus> = {}): VaultStatus {
  return {
    configured: true,
    path: '/mnt/shared/Users/alice/config/vault.kdbx',
    entry_count: 2,
    entry_names: ['github_pat', 'home_assistant_token'],
    entry_names_truncated: false,
    passphrase_present: true,
    outcome: '',
    reason: '',
    refusal: '',
    last_success_at: SYNC_AT,
    last_sync_at: SYNC_AT,
    last_outcome: 'ok',
    last_reason: '',
    parsed: false,
    problem: '',
    ...over,
  };
}

// The heading renders through `$lib/dateFormat`, so the assertion is on that
// module's own answer rather than on a string this file formats a second way —
// which is what `dateFormat.test.ts`'s drift guard exists to stop, and it binds
// a test as much as a component.
//
// **Deliberately older than `formatRelative`'s 30-day threshold**, so it renders
// through the absolute fallback and the expected string does not depend on the
// clock. A recent stamp would be read relatively by both sides and agree — until
// a run straddled a rung boundary between the component's call and this one, and
// then it would fail once a month for no reason. Passing a frozen `now` here
// does not fix that either: the component uses the real clock, so the two would
// simply disagree always.
const SYNC_AT = '2025-01-15T10:00:00Z';
const RENDERED_SYNC = formatRelative(SYNC_AT);

async function mount() {
  render(Harness, { component: Page, layout: SettingsLayout, user: person });
  // The vault fetch is the card's own load; waiting on it is what says the
  // render below happened after the data arrived rather than before.
  await waitFor(() => expect(api.getVaultStatus).toHaveBeenCalled());
}

function heading() {
  return screen.queryByTestId('vault-status');
}

/**
 * The heading line once it has rendered.
 *
 * `waitFor` resolves on the first callback that does not throw, and a bare
 * `queryByTestId` returns `null` without throwing — so a callback that merely
 * returns it resolves instantly with `null` and the assertion afterwards fails
 * on a property of `null` rather than on the thing it was testing. The
 * assertion has to be *inside* the callback for the wait to be a wait.
 */
async function findHeading(): Promise<HTMLElement> {
  return waitFor(() => {
    const el = heading();
    expect(el).not.toBeNull();
    return el as HTMLElement;
  });
}

beforeEach(() => {
  api.getVaultStatus.mockReset();
});

afterEach(cleanup);

describe('a user with no credential vault', () => {
  it('is told nothing about one', async () => {
    // The case that matters. Everybody is this user by default.
    api.getVaultStatus.mockResolvedValue({ configured: false });
    await mount();
    await waitFor(() => expect(api.getVaultStatus).toHaveBeenCalled());
    expect(heading()).toBeNull();
    // And the section it sits in still rendered, so the assertion above is
    // about the vault line rather than about a page that failed to render.
    expect(screen.getByRole('heading', { name: 'Credentials' })).toBeTruthy();
  });

  it('is told nothing when the endpoint fails, and the page still loads', async () => {
    // A deployment that has never heard of this endpoint, or one where it
    // errors, must not be a settings page that cannot show its cards.
    api.getVaultStatus.mockRejectedValue(new Error('500'));
    await mount();
    await waitFor(() => expect(api.getVaultStatus).toHaveBeenCalled());
    expect(heading()).toBeNull();
    expect(screen.getByRole('heading', { name: 'Credentials' })).toBeTruthy();
  });

  it('sees one sentence and a Set up button, and no password field', async () => {
    api.getVaultStatus.mockResolvedValue({ configured: false, editable: true, files: [] });
    await mount();
    const card = await screen.findByTestId('vault-card');
    expect(words(card)).toBe(
      'Keep credentials in a KeePassXC file instead? Istota can sync them from a file in your files.',
    );
    expect(screen.getByRole('button', { name: 'Set up' })).toBeTruthy();
    expect(screen.queryByLabelText(/^master password/i)).toBeNull();
  });
});

function words(el: HTMLElement): string {
  return (el.textContent ?? '').replace(/\s+/g, ' ').trim();
}

describe('a user whose vault is working', () => {
  it('shows one line naming the file and when it was updated, and no count', async () => {
    api.getVaultStatus.mockResolvedValue(configured({ entry_count: 2, generated_count: 3 }));
    await mount();

    const line = await findHeading();
    expect(words(line)).toBe(`Syncing vault.kdbx · updated ${RENDERED_SYNC}.`);
    // Not the raw wire value: it is UTC to millisecond precision and belongs to
    // nobody reading this page.
    expect(line.textContent).not.toContain(SYNC_AT);
    // The list carries the count; this card carries none.
    const card = screen.getByTestId('vault-card');
    expect(card.textContent).not.toMatch(/\b[23]\b/);
    expect(card.textContent).not.toMatch(/shared credential/i);
    // Istota writes to the file now, so the old promise is gone.
    expect(document.body.textContent).not.toMatch(/never writes to it/i);
    expect(words(card)).toContain('adds the ones tasks create to its generated group');
    expect(screen.getByTestId('vault-pill').textContent?.trim()).toBe('Working');
  });

  it('keeps the setup panel closed until Manage is pressed', async () => {
    api.getVaultStatus.mockResolvedValue(configured());
    await mount();
    await findHeading();

    expect(screen.queryByLabelText(/^master password/i)).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'Manage' }));
    expect(screen.getByLabelText(/^master password/i)).toBeTruthy();
  });

  it('shows no passphrase and no credential value', async () => {
    api.getVaultStatus.mockResolvedValue(configured());
    await mount();
    const line = await findHeading();
    expect(line.textContent).not.toMatch(/passphrase/i);
  });

  it('says so when a configured vault has never synced', async () => {
    // Distinct from a failure: nothing is wrong, the pass has simply not run yet.
    api.getVaultStatus.mockResolvedValue(
      configured({ last_success_at: '', last_sync_at: '', last_outcome: '' }),
    );
    await mount();

    const line = await findHeading();
    expect(words(line)).toContain('nothing applied yet');
    expect(line.textContent).not.toContain('Not working');
  });
});

describe('a user whose vault has a problem', () => {
  it('says what is wrong and opens the setup panel', async () => {
    api.getVaultStatus.mockResolvedValue(
      configured({
        last_outcome: 'VaultLocked',
        last_reason: 'the stored passphrase does not match the file',
        problem: 'the stored passphrase does not match the file',
      }),
    );
    await mount();

    const line = await findHeading();
    expect(words(line)).toContain('Not working: the stored passphrase does not match the file');
    expect(line.textContent).not.toContain('updated');
    expect(screen.getByTestId('vault-pill').textContent?.trim()).toBe('Needs attention');
    expect(await screen.findByTestId('vault-panel')).toBeTruthy();
    expect(screen.getByLabelText(/^master password/i)).toBeTruthy();
  });

  it('prefers the server verdict over the two fields beside it', async () => {
    // The precedence between a live finding and a recorded one is the server's:
    // `problem` arrives already resolved.
    api.getVaultStatus.mockResolvedValue(
      configured({
        outcome: 'VaultPathRefused',
        reason: 'the configured vault_path is not one the daemon may open',
        last_outcome: 'VaultLocked',
        last_reason: 'the stored passphrase does not match the file',
        problem: 'the configured vault_path is not one the daemon may open',
      }),
    );
    await mount();

    const line = await findHeading();
    expect(line.textContent).toContain('the configured vault_path');
    expect(line.textContent).not.toContain('does not match the file');
  });
});

describe('the scope notice', () => {
  it('says the whole file is shared when the last read was unscoped', async () => {
    api.getVaultStatus.mockResolvedValue(configured({ unscoped: true, entry_count: 412 }));
    await mount();

    await findHeading();
    expect(words(screen.getByTestId('vault-unscoped'))).toBe(
      'This file has no top-level istota group, so every entry in it is shared with your tasks. To share only some, move them into a group named istota.',
    );
  });

  it('says nothing about scope on an ordinary scoped vault', async () => {
    api.getVaultStatus.mockResolvedValue(configured({ unscoped: false, entry_count: 3 }));
    await mount();
    await findHeading();
    expect(screen.queryByTestId('vault-unscoped')).toBeNull();
  });

  it('says nothing about scope before a cycle has read the file', async () => {
    api.getVaultStatus.mockResolvedValue(configured({ unscoped: undefined }));
    await mount();
    const line = await findHeading();
    expect(screen.queryByTestId('vault-unscoped')).toBeNull();
    expect(words(line)).toContain('Syncing vault.kdbx');
  });
});

describe('name conflicts', () => {
  it('says how many entries were skipped, without promising the next sync', async () => {
    api.getVaultStatus.mockResolvedValue(configured({ name_conflicts: 2 }));
    await mount();
    await findHeading();
    const line = words(screen.getByTestId('vault-conflicts'));
    expect(line).toContain(
      '2 entries in the file were skipped because a credential with the same name was added in Istota. Rename them in KeePassXC, or delete the ones added in Istota',
    );
    // An unchanged file is not re-read, so only a change to it brings the entry in.
    expect(line).toContain('skipped entries are synced the next time the file changes');
  });

  it('uses the singular for one', async () => {
    api.getVaultStatus.mockResolvedValue(configured({ name_conflicts: 1 }));
    await mount();
    await findHeading();
    expect(words(screen.getByTestId('vault-conflicts'))).toContain(
      '1 entry in the file was skipped',
    );
  });

  it('says nothing when there are none', async () => {
    api.getVaultStatus.mockResolvedValue(configured({ name_conflicts: 0 }));
    await mount();
    await findHeading();
    expect(screen.queryByTestId('vault-conflicts')).toBeNull();
  });
});
