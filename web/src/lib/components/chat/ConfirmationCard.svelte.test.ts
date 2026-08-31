import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import ConfirmationCard from './ConfirmationCard.svelte';

afterEach(cleanup);

const noop = () => {};

/**
 * The card asks for something, and until now nothing on it said who was
 * asking. It is rendered inside the turn that raised it, whose header names the
 * bot — but a confirmation lands on a continuation row as often as not, and
 * that row has no header at all. The face is what identifies the asker there.
 */
describe('the confirmation card names its actor', () => {
  it('draws the bot icon when the deployment has one', () => {
    const { container } = render(ConfirmationCard, {
      onConfirm: noop,
      onReject: noop,
      botName: 'Istota',
      botAvatar: 'bot99',
    });

    const img = container.querySelector('.confirm-actor img');
    expect(img?.getAttribute('src')).toBe('/api/avatars/bot?v=bot99');
  });

  it('falls back to the chip and asks for nothing when none is set', () => {
    // `null` is `/me` saying there is no icon, which must not become a request
    // per card. Every deployment that has not set one is this case.
    const { container } = render(ConfirmationCard, {
      onConfirm: noop,
      onReject: noop,
      botName: 'Istota',
      botAvatar: null,
    });

    expect(container.querySelector('.confirm-actor img')).toBeNull();
    expect(container.querySelector('.confirm-actor .fallback')?.textContent?.trim()).toBe('I');
  });

  it('renders the identity decoratively, so the sentence is what is read', () => {
    // The card sits under the turn that raised it, and on a first row that
    // turn's header already names the bot. An alt text here would announce the
    // same identity twice before the one sentence that matters.
    const { container } = render(ConfirmationCard, {
      onConfirm: noop,
      onReject: noop,
      botName: 'Istota',
      botAvatar: 'bot99',
    });

    const img = container.querySelector('.confirm-actor img');
    expect(img?.getAttribute('alt')).toBe('');
  });

  it('still offers both actions, and disables them while one is in flight', () => {
    const onConfirm = vi.fn();
    const onReject = vi.fn();
    const { getByText, rerender } = render(ConfirmationCard, {
      onConfirm,
      onReject,
      botName: 'Istota',
      botAvatar: null,
    });

    (getByText('Confirm') as HTMLButtonElement).click();
    expect(onConfirm).toHaveBeenCalledOnce();
    (getByText('Cancel') as HTMLButtonElement).click();
    expect(onReject).toHaveBeenCalledOnce();

    rerender({ onConfirm, onReject, botName: 'Istota', botAvatar: null, busy: true });
    expect((getByText('Confirm').closest('button') as HTMLButtonElement).disabled).toBe(true);
  });
});
