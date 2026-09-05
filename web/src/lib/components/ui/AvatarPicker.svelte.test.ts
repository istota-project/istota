/**
 * The shared avatar picker.
 *
 * Both call sites used to carry this control verbatim, and the two properties
 * that are easy to lose in a refactor were stated only in their comments: the
 * picker is *unmounted* while busy rather than disabled, because remounting is
 * what resets the native input, and every route in is gated on the busy state,
 * because the wrapper keeps taking drops and pastes while the picker is gone.
 * Those are asserted here rather than in either page.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup, screen, fireEvent } from '@testing-library/svelte';

import Harness from './avatarPickerHarness.test.svelte';

afterEach(cleanup);

const picture = new File(['not really a png'], 'me.png', { type: 'image/png' });

const picker = () => document.querySelector('.avatar-picker') as HTMLElement;
const fileInput = () => picker().querySelector('input[type="file"]') as HTMLInputElement | null;

function show(props: Record<string, unknown> = {}) {
  const onPick = vi.fn();
  const onRemove = vi.fn();
  const rendered = render(Harness, { onPick, onRemove, ...props });
  return { ...rendered, onPick, onRemove };
}

describe('taking a file', () => {
  it('takes a drop', async () => {
    const { onPick } = show();
    await fireEvent.drop(picker(), { dataTransfer: { files: [picture] } });
    expect(onPick).toHaveBeenCalledWith(picture);
  });

  it('takes a paste, and only when the clipboard carries a file', async () => {
    // This sits on pages with text fields of their own, so an ordinary paste
    // must fall through rather than being swallowed.
    const { onPick } = show();
    await fireEvent.paste(picker(), { clipboardData: { files: [] } });
    expect(onPick).not.toHaveBeenCalled();

    await fireEvent.paste(picker(), { clipboardData: { files: [picture] } });
    expect(onPick).toHaveBeenCalledWith(picture);
  });

  it('takes the native picker, which the picture opens', async () => {
    const { onPick } = show();
    const input = fileInput()!;
    const opened = vi.spyOn(input, 'click');

    // The picture is the only affordance: there is no Choose file button.
    await fireEvent.click(screen.getByRole('button', { name: 'Choose a picture' }));
    expect(opened).toHaveBeenCalledOnce();

    Object.defineProperty(input, 'files', { value: [picture], configurable: true });
    await fireEvent.change(input);
    expect(onPick).toHaveBeenCalledWith(picture);
  });

  it('offers the caller-supplied accept rather than image/*', async () => {
    // `image/*` would let the picker offer TIFF, BMP, AVIF and SVG, all of
    // which the server refuses — and the user would find out after choosing.
    show({ accept: 'image/png,image/webp' });
    expect(fileInput()!.getAttribute('accept')).toBe('image/png,image/webp');
  });
});

describe('while an upload or a removal is running', () => {
  it('unmounts the picker rather than disabling it', async () => {
    // The remount is what resets the native input, and a browser fires no
    // `change` for a file the input is already holding — so with the picker
    // merely disabled, re-picking the photo whose upload just failed would do
    // nothing and say nothing.
    const { rerender } = show();
    const before = fileInput();
    expect(before).not.toBeNull();

    await rerender({ busyLabel: 'Saving your picture…' });
    expect(fileInput()).toBeNull();
    expect(screen.getByText('Saving your picture…')).toBeInTheDocument();

    await rerender({ busyLabel: '' });
    expect(fileInput()).not.toBeNull();
    expect(fileInput()).not.toBe(before);
  });

  it('keeps showing the picture, so the row does not move', async () => {
    show({ busyLabel: 'Saving your picture…' });
    expect(picker().querySelector('.target')).not.toBeNull();
  });

  it('ignores a drop and a paste, which the wrapper still receives', async () => {
    // The wrapper stays mounted while the picker does not, so this is the one
    // route a second file could take mid-upload. Two concurrent PUTs would
    // resolve in either order, leaving the preview on one hash and the stored
    // row on the other.
    const { onPick } = show({ busyLabel: 'Saving your picture…' });
    await fireEvent.drop(picker(), { dataTransfer: { files: [picture] } });
    await fireEvent.paste(picker(), { clipboardData: { files: [picture] } });
    expect(onPick).not.toHaveBeenCalled();
  });

  it('withholds Remove', () => {
    show({ removable: true, busyLabel: 'Saving your picture…' });
    expect(screen.queryByRole('button', { name: 'Remove' })).toBeNull();
  });
});

describe('Remove', () => {
  it('is offered only when there is something to remove', async () => {
    const { rerender, onRemove } = show({ removable: false });
    expect(screen.queryByRole('button', { name: 'Remove' })).toBeNull();

    await rerender({ removable: true });
    await fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
    expect(onRemove).toHaveBeenCalledOnce();
  });
});

describe('the accessible names', () => {
  it('names the picture-as-button for the action, not for what it shows', () => {
    // The picture inside it is `alt`-labelled for the current state, so this
    // is the only place the control says what pressing it does.
    show({ pickLabel: 'Choose the bot icon' });
    expect(screen.getByRole('button', { name: 'Choose the bot icon' })).toBeInTheDocument();
  });

  it('announces the busy line, which replaces the prompt', () => {
    show({ busyLabel: 'Removing your picture…' });
    const line = screen.getByText('Removing your picture…');
    expect(line.getAttribute('aria-live')).toBe('polite');
  });
});
