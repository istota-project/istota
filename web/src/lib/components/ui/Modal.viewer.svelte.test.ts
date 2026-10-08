import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import Fixture from './ModalViewerFixture.svelte';

afterEach(cleanup);

async function open(props = {}) {
  const result = render(Fixture, props);
  const opener = screen.getByRole('button', { name: 'Open viewer' });
  opener.focus();
  await fireEvent.click(opener);
  const dialog = screen.getByRole('dialog', { name: 'Example document' });
  return { ...result, opener, dialog };
}

function navigation() {
  return {
    previousLabel: 'Previous entry',
    nextLabel: 'Next entry',
    canPrevious: true,
    canNext: true,
    onPrevious: vi.fn(),
    onNext: vi.fn(),
  };
}

describe('viewer modal', () => {
  it('renders the compact header with metadata, actions and Close', async () => {
    const { dialog } = await open();
    expect(within(dialog).getByText('12 KB')).toBeInTheDocument();
    expect(within(dialog).getByRole('link', { name: 'Download' })).toHaveAttribute(
      'download',
      'example.txt',
    );
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('preserves the default dialog and does not add viewer controls', async () => {
    const { dialog } = await open({ variant: 'default', navigation: navigation() });
    expect(dialog).toHaveClass('ui-modal-content');
    expect(dialog).not.toHaveClass('ui-viewer-content');
    expect(within(dialog).getByText('Document body')).toBeInTheDocument();
    expect(within(dialog).getByText('Footer')).toBeInTheDocument();
    expect(within(dialog).queryByText('12 KB')).not.toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: 'Close' })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: 'Next entry' })).not.toBeInTheDocument();
  });

  it.each(['default', 'viewer'])(
    'dismisses the %s dialog on Escape and restores the opener',
    async (variant) => {
      const { dialog, opener } = await open({ variant });
      await fireEvent.keyDown(dialog, { key: 'Escape' });
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
      await waitFor(() => expect(opener).toHaveFocus());
    },
  );

  it('only navigates enabled endpoints, and blocks next while busy', async () => {
    const nav = navigation();
    const { dialog, rerender } = await open({ navigation: nav });
    await fireEvent.keyDown(dialog, { key: 'ArrowLeft' });
    await fireEvent.keyDown(dialog, { key: 'ArrowRight' });
    expect(nav.onPrevious).toHaveBeenCalledTimes(1);
    expect(nav.onNext).toHaveBeenCalledTimes(1);
    await rerender({ navigation: { ...nav, canPrevious: false, busy: true } });
    for (const button of within(dialog).getAllByRole('button', { name: 'Next entry' }))
      expect(button).toBeDisabled();
    for (const button of within(dialog).getAllByRole('button', { name: 'Previous entry' }))
      expect(button).toBeDisabled();
    expect(within(dialog).getAllByRole('status').length).toBeGreaterThan(0);
    await fireEvent.keyDown(dialog, { key: 'ArrowLeft' });
    await fireEvent.keyDown(dialog, { key: 'ArrowRight' });
    expect(nav.onPrevious).toHaveBeenCalledTimes(1);
    expect(nav.onNext).toHaveBeenCalledTimes(1);
  });

  it('leaves editing, media, modified, composing and handled keys alone', async () => {
    const nav = navigation();
    const { dialog } = await open({ navigation: nav });
    for (const label of ['Input', 'Source', 'Editable', 'Audio', 'Video']) {
      await fireEvent.keyDown(within(dialog).getByLabelText(label), { key: 'ArrowRight' });
    }
    for (const modifier of ['altKey', 'ctrlKey', 'metaKey', 'shiftKey', 'isComposing']) {
      await fireEvent.keyDown(dialog, { key: 'ArrowRight', [modifier]: true });
    }
    const handled = new KeyboardEvent('keydown', {
      key: 'ArrowRight',
      bubbles: true,
      cancelable: true,
    });
    handled.preventDefault();
    await fireEvent(dialog, handled);
    expect(nav.onNext).not.toHaveBeenCalled();
  });

  it('resets body scroll only on identity changes without remounting content', async () => {
    const { dialog, rerender } = await open();
    const body = dialog.querySelector('.ui-modal-body') as HTMLElement;
    const content = within(dialog).getByText('Document body');
    body.scrollTop = 240;
    await rerender({ metadataText: 'Starred', width: '960px' });
    expect(body.scrollTop).toBe(240);
    await rerender({ bodyKey: 'second' });
    await waitFor(() => expect(body.scrollTop).toBe(0));
    expect(within(dialog).getByText('Document body')).toBe(content);
  });

  it('keeps nested image keys and dismissal inside the Lightbox', async () => {
    const nav = navigation();
    const { dialog } = await open({ navigation: nav });
    const zoom = within(dialog).getByRole('button', { name: 'Zoom image' });
    zoom.focus();
    const body = dialog.querySelector('.ui-modal-body') as HTMLElement;
    body.scrollTop = 240;
    await fireEvent.click(zoom);
    const imageDialog = screen.getByRole('dialog', { name: 'Image viewer' });
    await fireEvent.keyDown(imageDialog, { key: 'ArrowRight' });
    expect(imageDialog.querySelector('img')).toHaveAttribute('src', '/two.jpg');
    expect(nav.onNext).not.toHaveBeenCalled();
    await fireEvent.keyDown(imageDialog, { key: 'Escape' });
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Image viewer' })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole('dialog', { name: 'Example document' })).toBe(dialog);
    expect(body.scrollTop).toBe(240);
    await waitFor(() => expect(zoom).toHaveFocus());
  });
});
