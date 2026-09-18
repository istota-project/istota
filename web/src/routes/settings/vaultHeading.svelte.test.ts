/**
 * The credential vault's status line on the "Connected services" heading.
 *
 * **The case that matters is the one that renders nothing.** A vault is an
 * optional per-user feature that almost nobody has, and a heading that always
 * says something is a heading every user has to read past for the life of the
 * deployment. So the empty render is asserted first and twice — once for the
 * ordinary unconfigured answer and once for an endpoint that failed, since a
 * settings page must not fail to load over a feature its actual content does
 * not depend on.
 *
 * Where it does render, it is read-only by construction rather than by
 * omission: the vault has no writing endpoint at all. The two fields selecting
 * the file are TOML-only because they decide which file the daemon decrypts and
 * which credentials it may overwrite, and the passphrase is CLI-only because it
 * has to be generated — a form field there is an invitation to type a memorable
 * one, which is the single thing making the file's presence in the sandbox
 * matter.
 *
 * It sits on the heading rather than in the card list below it because it is
 * not a connected service: it is the *source* those credentials come from, and
 * it is the referent the disabled-field sentence on each managed card needs —
 * which has to be visible from every card that shows one.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';
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
  vault_managed: true,
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

function configured(over: Partial<VaultStatus> = {}): VaultStatus {
  return {
    configured: true,
    path: '/mnt/shared/Users/alice/config/vault.kdbx',
    owned: ['karakeep'],
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
  render(Harness, { component: Page, user: person });
  // The services fetch is part of the page's own load; waiting on it is what
  // says the render below happened after the data arrived rather than before.
  await waitFor(() => expect(api.getSettingsServices).toHaveBeenCalled());
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
    // And the heading it would have hung off is still there, so the assertion
    // above is about the vault line rather than about a page that failed to
    // render its Connected services section at all.
    expect(screen.getByText('Connected services')).toBeTruthy();
  });

  it('is told nothing when the endpoint fails, and the page still loads', async () => {
    // A deployment that has never heard of this endpoint, or one where it
    // errors, must not be a settings page that cannot show its cards.
    api.getVaultStatus.mockRejectedValue(new Error('500'));
    await mount();
    await waitFor(() => expect(api.getVaultStatus).toHaveBeenCalled());
    expect(heading()).toBeNull();
    expect(screen.getByText('Karakeep')).toBeTruthy();
  });
});

describe('a user whose vault owns a service', () => {
  it('names the owned service, the path and the last sync', async () => {
    api.getVaultStatus.mockResolvedValue(configured());
    await mount();

    const line = await findHeading();
    expect(line.textContent).toContain('karakeep');
    expect(line.textContent).toContain('/mnt/shared/Users/alice/config/vault.kdbx');
    // A relative reading, which is what the question "is it keeping up" wants.
    // The exact words are `formatRelative`'s; what this pins is that the value
    // went through it.
    expect(line.textContent).toContain(RENDERED_SYNC);
    // Not the raw wire value: it is UTC to millisecond precision and belongs to
    // nobody reading this page.
    expect(line.textContent).not.toContain(SYNC_AT);
    // It says it is read and never written, which is the property a user has to
    // know before they go looking for a Save button that does not exist.
    expect(line.textContent).toContain('never written');
  });

  it('shows no passphrase and no credential value', async () => {
    // There is nothing in the payload that could carry one — the endpoint is
    // asserted on separately for that — so this is the renderer's half: it must
    // not invent a field, and the whole line is swept rather than named fields.
    api.getVaultStatus.mockResolvedValue(configured());
    await mount();
    const line = await findHeading();
    expect(line.textContent).not.toMatch(/passphrase/i);
  });

  it('reports a failing vault with the class-specific sentence', async () => {
    api.getVaultStatus.mockResolvedValue(
      configured({
        last_outcome: 'VaultLocked',
        last_reason: 'the stored passphrase does not match the file',
        problem: 'the stored passphrase does not match the file',
      }),
    );
    await mount();

    const line = await findHeading();
    expect(line.textContent).toContain('Not working');
    expect(line.textContent).toContain('does not match the file');
    // The failing sentence takes the slot the sync time would have had.
    expect(line.textContent).not.toContain('last applied');
  });

  it('prefers a live path refusal over an older recorded failure', async () => {
    // The two say different things: `outcome` is what this request found and
    // `last_outcome` is what some earlier cycle in another process settled. A
    // refused path is true now and outranks a cycle that ran before the
    // operator introduced it.
    // The precedence itself is the server's — `problem` arrives already
    // resolved — so what this pins is that the renderer uses that field and
    // does not re-derive a verdict from the two it sits beside.
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
    // Substring rather than the whole sentence: the reason is rendered as a
    // text node inside the markup's own line wrapping, so it carries newlines.
    expect(line.textContent).toContain('the configured vault_path');
    expect(line.textContent).not.toContain('does not match the file');
  });

  it('says so when a configured vault has never synced', async () => {
    // Distinct from a failure: nothing is wrong, the pass has simply not run
    // yet. Rendering "Last synced " with nothing after it would be the worse
    // answer of the two.
    api.getVaultStatus.mockResolvedValue(
      configured({ last_success_at: '', last_sync_at: '', last_outcome: '' }),
    );
    await mount();

    const line = await findHeading();
    expect(line.textContent).toContain('Nothing has been applied');
    expect(line.textContent).not.toContain('Not working');
  });

  it('says so when the vault owns nothing yet', async () => {
    // `vault_services = []` is a usable dry-run state: the file is read and
    // nothing is applied. A line claiming it is the authority for an empty list
    // would be the wrong sentence for it.
    api.getVaultStatus.mockResolvedValue(configured({ owned: [] }));
    await mount();

    const line = await findHeading();
    expect(line.textContent).toContain('no services are assigned');
  });
});
