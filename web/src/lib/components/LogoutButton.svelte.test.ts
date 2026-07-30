import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen, within } from '@testing-library/svelte';
import LogoutButton from './LogoutButton.svelte';

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
