/**
 * The Identity card's profile-picture control.
 *
 * The control has no Save step — it commits on pick through its own multipart
 * call — so the two things worth pinning are that a pick actually goes out and
 * that a refusal is legible. The server sends `{error}` with a 413 for a file
 * over the cap and a 415 for a format it will not decode, and those are the
 * sentences the user has to read to know what to do differently.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, waitFor, fireEvent } from '@testing-library/svelte';

const api = vi.hoisted(() => ({
  AuthError: class AuthError extends Error {},
  getSettingsServices: vi.fn(async () => ({ services: [] })),
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
      // Spelled out rather than omitted: the page's dropdowns normalize an
      // absent value onto the record as they mount, which adds a key the
      // loaded snapshot did not have and leaves the form dirty before anybody
      // has touched it — and the header-Save assertion below turns on exactly
      // that state.
      external_turn_display: 'collapsed',
      timezone_follow_location: false,
      delivery_surfaces: ['talk'],
    },
  })),
  updateProfile: vi.fn(async () => ({})),
  disconnectNextcloudToken: vi.fn(async () => ({})),
  uploadAvatar: vi.fn(async () => ({ hash: 'new99', mime: 'image/webp', bytes: 1 })),
  deleteAvatar: vi.fn(async () => ({ deleted: true })),
  // The real one, since the `src` this file asserts on is what it builds.
  avatarUrl: vi.fn((kind: string, userId?: string, version?: string | null) => {
    const path = kind === 'bot' ? '/api/avatars/bot' : `/api/avatars/user/${userId}`;
    return version ? `${path}?v=${version}` : path;
  }),
  AVATAR_ACCEPT: 'image/jpeg,image/png,image/webp,image/gif,image/heic,image/heif',
}));
vi.mock('$lib/api', () => api);

vi.mock('$lib/platform/native', () => ({
  isNativeShell: vi.fn(() => false),
  shellVersion: vi.fn(() => ''),
  shellAtLeast: vi.fn(() => false),
  onKeyboardGeometry: vi.fn(() => () => {}),
}));

import Page from './+page.svelte';
import Harness from './reloadableIdentityHarness.test.svelte';
import type { User } from '$lib/api';

const person = (avatars?: { user: string | null; bot: string | null }): User => ({
  username: 'alice',
  display_name: 'Alice',
  bot_name: 'Istota',
  is_admin: false,
  avatars,
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
});

const dropzone = () => document.querySelector('.picture .dropzone') as HTMLElement;
const preview = () => document.querySelector('.picture-preview') as HTMLElement;

/** Drop a file on the picker, which is how a pick reaches the page. */
async function drop(file: File) {
  await fireEvent.drop(dropzone(), { dataTransfer: { files: [file] } });
}

/** Choose a file through the native input, which is the path that can wedge. */
async function choose(file: File) {
  const input = dropzone().querySelector('input[type="file"]') as HTMLInputElement;
  Object.defineProperty(input, 'files', { value: [file], configurable: true });
  await fireEvent.change(input);
}

const picture = new File(['not really a jpeg'], 'me.jpg', { type: 'image/jpeg' });

/* What the next `reload()` publishes. The page reloads the identity on load
   too, so a test that wants a *changed* record has to move this after the page
   has settled rather than seeding it. */
let served: User | null = null;
let reloads = 0;
let expiredSessions = 0;

function renderPage(user: User) {
  served = user;
  return render(Harness, {
    component: Page,
    user,
    onReload: () => {
      reloads += 1;
      return served;
    },
    onExpireSession: () => {
      expiredSessions += 1;
    },
  });
}

beforeEach(() => {
  reloads = 0;
  expiredSessions = 0;
  api.uploadAvatar.mockClear();
  api.deleteAvatar.mockClear();
  api.updateProfile.mockClear();
  api.uploadAvatar.mockResolvedValue({ hash: 'new99', mime: 'image/webp', bytes: 1 });
});

afterEach(cleanup);

describe('the profile-picture control', () => {
  it('offers only the formats the server will decode', async () => {
    // Not `image/*`, which the picker would read as TIFF, BMP, AVIF and SVG
    // too — all refused server-side, and the user would find out only after
    // choosing one.
    renderPage(person());
    await waitFor(() => expect(dropzone()).not.toBeNull());

    const input = dropzone().querySelector('input[type="file"]') as HTMLInputElement;
    expect(input.getAttribute('accept')).toBe(api.AVATAR_ACCEPT);
    expect(input.getAttribute('accept')).not.toContain('image/*');
  });

  it('sends a picked file at once, and moves the preview to the stored hash', async () => {
    // There is no Save step for this control, and the hash comes off the
    // upload's own response rather than off a later read: the browser holds
    // the old `?v` URL as `immutable`, so the preview would keep painting the
    // old face until a new hash reaches the `src`.
    renderPage(person({ user: 'old11', bot: null }));
    await waitFor(() => expect(dropzone()).not.toBeNull());
    expect(preview().querySelector('img')?.getAttribute('src')).toBe(
      '/api/avatars/user/alice?v=old11',
    );

    // What `/me` will say once the upload has landed, which is what the page
    // falls back to after it drops the hash the upload handed it.
    served = person({ user: 'new99', bot: null });
    await drop(picture);

    await waitFor(() => expect(api.uploadAvatar).toHaveBeenCalledWith(picture));
    await waitFor(() =>
      expect(preview().querySelector('img')?.getAttribute('src')).toBe(
        '/api/avatars/user/alice?v=new99',
      ),
    );
  });

  it('gives the picker a fresh input once it has taken a file', async () => {
    /* The recovery path after a transient failure is to pick the same photo
       again, and a browser fires no `change` for a file the input is already
       holding. `FileDropZone` clears it from its own Clear button, which this
       control never renders — the file is taken the moment it is picked — so
       the zone is remounted instead.

       Asserted on the input's *identity* rather than by picking twice, because
       jsdom does not model the behaviour being defended against: it dispatches
       `change` for a re-selected file just the same, so a two-pick test passes
       against the wedged version and proves nothing. The remount is the
       mechanism, and it is the thing that can actually be observed here. */
    api.uploadAvatar.mockRejectedValueOnce(new Error('the server was unreachable'));
    renderPage(person());
    await waitFor(() => expect(dropzone()).not.toBeNull());
    const before = dropzone().querySelector('input[type="file"]');

    await choose(picture);
    await waitFor(() => expect(screen.getByText(/unreachable/)).toBeInTheDocument());

    expect(dropzone().querySelector('input[type="file"]')).not.toBe(before);
  });

  it("shows the server's own refusal, and leaves the picture alone", async () => {
    api.uploadAvatar.mockRejectedValue(new Error('that file is larger than 4096 KB'));
    renderPage(person({ user: 'old11', bot: null }));
    await waitFor(() => expect(dropzone()).not.toBeNull());

    await drop(picture);

    await waitFor(() => expect(screen.getByText(/larger than 4096 KB/)).toBeInTheDocument());
    expect(preview().querySelector('img')?.getAttribute('src')).toBe(
      '/api/avatars/user/alice?v=old11',
    );
  });

  it('hands an expired upload session back to the root identity flow', async () => {
    api.uploadAvatar.mockRejectedValue(new api.AuthError());
    renderPage(person());
    await waitFor(() => expect(dropzone()).not.toBeNull());

    await drop(picture);

    await waitFor(() => expect(expiredSessions).toBe(1));
    expect(screen.queryByText('Not authenticated')).toBeNull();
  });

  it('offers Remove only while a picture is showing', async () => {
    const { unmount } = renderPage(person());
    await waitFor(() => expect(dropzone()).not.toBeNull());
    expect(screen.queryByRole('button', { name: /^Remove$/ })).toBeNull();
    unmount();

    renderPage(person({ user: 'old11', bot: null }));
    await waitFor(() => expect(dropzone()).not.toBeNull());
    await fireEvent.click(await screen.findByRole('button', { name: /^Remove$/ }));

    expect(api.deleteAvatar).toHaveBeenCalledOnce();
  });

  it('leaves the header Save alone, since the picture is not part of the form', async () => {
    // The header Save builds a JSON patch against the profile it loaded, and
    // the picture has already landed by the time it could be pressed. Asserted
    // on the button's own state rather than on `updateProfile` never being
    // called: nothing here presses Save, so that assertion would hold against
    // an implementation where the picture *did* enter the form.
    renderPage(person());
    await waitFor(() => expect(dropzone()).not.toBeNull());
    const save = screen.getByRole('button', { name: /Save changes/ });
    expect(save).toBeDisabled();

    await drop(picture);
    await waitFor(() => expect(api.uploadAvatar).toHaveBeenCalledOnce());

    expect(save).toBeDisabled();
    expect(api.updateProfile).not.toHaveBeenCalled();
  });
});
