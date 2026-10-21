import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen, within } from '@testing-library/svelte';
import LogoutButton from './LogoutButton.svelte';

const app = vi.hoisted(() => ({
  signOutOfApp: vi.fn<() => Promise<'signed-out' | 'cancelled' | 'unavailable'>>(),
}));
vi.mock('$lib/platform/nativeConnection', () => app);
const notices = vi.hoisted(() => ({ notifyError: vi.fn() }));
vi.mock('@minot/ui/stores/notices', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@minot/ui/stores/notices')>()),
  notifyError: notices.notifyError,
}));
const lastUser = vi.hoisted(() => ({ forgetLastUserId: vi.fn() }));
vi.mock('$lib/offline/lastUser', () => lastUser);

beforeEach(() => {
  vi.clearAllMocks();
  app.signOutOfApp.mockResolvedValue('unavailable');
});

// The dialog holds a body scroll lock whose reset runs after unmount; ending a
// test with it open lands that reset after jsdom tears `document` down.
afterEach(async () => {
  await fireEvent.keyDown(document.body, { key: 'Escape' });
  cleanup();
  await new Promise((resolve) => setTimeout(resolve, 0));
});

function mount() {
  const navigate = vi.fn();
  render(LogoutButton, { navigate });
  return { navigate, trigger: screen.getByLabelText('Log out') };
}

describe('LogoutButton', () => {
  // The point of the whole component: the logout control sits beside the menu
  // trigger and both are small on mobile, so a single tap must not end the
  // session (ISSUE-209).
  it('does not log out on the first tap', async () => {
    const { navigate, trigger } = mount();
    await fireEvent.click(trigger);
    expect(navigate).not.toHaveBeenCalled();
  });

  it('opens a confirmation asking before it logs out', async () => {
    const { trigger } = mount();
    await fireEvent.click(trigger);
    expect(await screen.findByRole('dialog')).not.toBeNull();
    expect(screen.getByText(/are you sure you want to log out/i)).not.toBeNull();
  });

  it('navigates to the logout endpoint once confirmed', async () => {
    const { navigate, trigger } = mount();
    await fireEvent.click(trigger);
    const dialog = await screen.findByRole('dialog');
    // The trigger carries the same label, so scope the lookup to the dialog.
    const confirm = within(dialog).getByRole('button', { name: 'Log out' });
    await fireEvent.click(confirm);
    expect(navigate).toHaveBeenCalledWith('/logout');
  });

  it('cancelling leaves the session alone', async () => {
    const { navigate, trigger } = mount();
    await fireEvent.click(trigger);
    await fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }));
    expect(navigate).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  // Cancel is the safe default, so the two dismissal gestures a modal offers
  // must both mean "stay logged in" rather than falling through to the action.
  it('escape dismisses without logging out', async () => {
    const { navigate, trigger } = mount();
    await fireEvent.click(trigger);
    await screen.findByRole('dialog');
    await fireEvent.keyDown(document.body, { key: 'Escape' });
    expect(navigate).not.toHaveBeenCalled();
  });
});

describe('LogoutButton in the generic iOS app', () => {
  // The app asks natively and cleans up what /logout would leave bound to the
  // account, so the button hands over instead of asking a second time.
  it('signs out through the app without its own dialog or navigation', async () => {
    app.signOutOfApp.mockResolvedValue('signed-out');
    const { navigate, trigger } = mount();
    await fireEvent.click(trigger);
    await vi.waitFor(() => expect(lastUser.forgetLastUserId).toHaveBeenCalledOnce());
    expect(app.signOutOfApp).toHaveBeenCalledOnce();
    expect(navigate).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('does nothing when the user cancels the app question', async () => {
    app.signOutOfApp.mockResolvedValue('cancelled');
    const { navigate, trigger } = mount();
    await fireEvent.click(trigger);
    await vi.waitFor(() => expect(app.signOutOfApp).toHaveBeenCalledOnce());
    expect(navigate).not.toHaveBeenCalled();
    expect(lastUser.forgetLastUserId).not.toHaveBeenCalled();
    expect(notices.notifyError).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('says so, and stays signed in, when the app refuses', async () => {
    app.signOutOfApp.mockRejectedValue(new Error('The connection changed.'));
    const { navigate, trigger } = mount();
    await fireEvent.click(trigger);
    await vi.waitFor(() => expect(notices.notifyError).toHaveBeenCalledOnce());
    expect(navigate).not.toHaveBeenCalled();
    expect(lastUser.forgetLastUserId).not.toHaveBeenCalled();
  });
});
