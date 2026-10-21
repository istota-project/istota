import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import Fixture from './LightboxInViewerFixture.svelte';

// The image zoom opens from inside a viewer Modal (the feed reader's hero
// image). minot's own viewer test holds the nested-dialog guard with a generic
// inline dialog; this is the case with istota's Lightbox in it.

afterEach(cleanup);

describe('Lightbox inside a viewer Modal', () => {
  it('keeps the image keys and dismissal inside the Lightbox', async () => {
    const navigation = {
      previousLabel: 'Previous entry',
      nextLabel: 'Next entry',
      canPrevious: true,
      canNext: true,
      onPrevious: vi.fn(),
      onNext: vi.fn(),
    };
    render(Fixture, { navigation });
    await fireEvent.click(screen.getByRole('button', { name: 'Open viewer' }));
    const dialog = screen.getByRole('dialog', { name: 'Example document' });
    const zoom = within(dialog).getByRole('button', { name: 'Zoom image' });
    zoom.focus();
    const body = dialog.querySelector('.ui-modal-body') as HTMLElement;
    body.scrollTop = 240;
    await fireEvent.click(zoom);

    const imageDialog = screen.getByRole('dialog', { name: 'Image viewer' });
    await fireEvent.keyDown(imageDialog, { key: 'ArrowRight' });
    expect(imageDialog.querySelector('img')).toHaveAttribute('src', '/two.jpg');
    expect(navigation.onNext).not.toHaveBeenCalled();

    await fireEvent.keyDown(imageDialog, { key: 'Escape' });
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Image viewer' })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole('dialog', { name: 'Example document' })).toBe(dialog);
    expect(body.scrollTop).toBe(240);
    await waitFor(() => expect(zoom).toHaveFocus());
  });
});
