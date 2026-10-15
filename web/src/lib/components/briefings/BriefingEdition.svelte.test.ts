import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import type { BriefingDocument } from '$lib/api';
import BriefingEdition from './BriefingEdition.svelte';
import BriefingBlockCard from './BriefingBlockCard.svelte';
import { edition } from './edition.fixture';

afterEach(cleanup);

describe('briefing editions', () => {
  it('renders saved cards in order, with literal titles and textual source status', () => {
    const { container } = render(BriefingEdition, { edition });
    expect(screen.getByRole('heading', { level: 1, name: edition.subject })).toBeInTheDocument();
    expect(screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)).toEqual([
      'Headlines <em>today</em>',
      'Calendar',
      'Notes',
    ]);
    expect(container.querySelector('h2 em')).toBeNull();
    expect(screen.getByText('Some sources unavailable')).toBeInTheDocument();
    expect(screen.getByText('An appointment')).toBeInTheDocument();
    expect(screen.queryByText('The linear edition fallback.')).toBeNull();
    expect(
      [...container.querySelectorAll('[data-width]')].map((el) => el.getAttribute('data-width')),
    ).toEqual(['wide', 'auto', 'full']);
    expect(screen.getByRole('link', { name: 'A story' })).toHaveAttribute(
      'href',
      'https://example.com/story',
    );
    expect(screen.queryByRole('link', { name: 'Full story' })).toBeNull();
  });

  it.each(['close', 'escape'])(
    'opens complete content and restores focus after %s',
    async (method) => {
      render(BriefingEdition, { edition });
      const opener = screen.getByRole('button', { name: 'Read full block' });
      opener.focus();
      await fireEvent.click(opener);
      const dialog = screen.getByRole('dialog', { name: 'Headlines <em>today</em>' });
      expect(dialog).toHaveClass('ui-viewer-content');
      expect(within(dialog).getByRole('link', { name: 'Full story' })).toHaveAttribute(
        'href',
        'https://example.com/full',
      );
      expect(within(dialog).queryByRole('button', { name: 'Read full block' })).toBeNull();
      expect(within(dialog).getAllByRole('heading')).toHaveLength(1);
      if (method === 'close')
        await fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
      else await fireEvent.keyDown(dialog, { key: 'Escape' });
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      await waitFor(() => expect(opener).toHaveFocus());
    },
  );

  it('offers complete content without card chrome for a viewer', () => {
    render(BriefingBlockCard, { block: edition.document!.blocks[0], fullContent: true });
    expect(screen.getByRole('link', { name: 'Full story' })).toBeInTheDocument();
    expect(screen.queryByRole('heading')).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it.each([
    null,
    { version: 99 },
    { version: 1, title: 'Invalid', blocks: [{}] },
    { ...edition.document, title: ' ' },
    { ...edition.document, blocks: [edition.document!.blocks[0], edition.document!.blocks[0]] },
    { ...edition.document, blocks: [{ ...edition.document!.blocks[0], kind: undefined }] },
  ])('retains a whole linear reading card when the document is absent or invalid', (document) => {
    const body_md = '# Original section\n\nOne\n\nTwo\n\nThree\n\n[Last](https://example.com/last)';
    const { container } = render(BriefingEdition, {
      edition: { ...edition, body_md, document: document as BriefingDocument | null },
    });
    expect(screen.getAllByRole('heading', { name: edition.subject })).toHaveLength(1);
    expect(screen.getByRole('link', { name: 'Last' })).toBeInTheDocument();
    expect(container.querySelectorAll('.legacy')).toHaveLength(1);
    expect(screen.queryByRole('button', { name: 'Read full block' })).toBeNull();
  });

  it('keeps hostile body markup inert through preview and full content', async () => {
    const document = structuredClone(edition.document!);
    document.blocks[0].body_md =
      '<script>alert(1)</script>\n\n[Bad](javascript:alert(1))\n\n![Remote](https://example.com/pixel.png)\n\n[Safe](https://example.com/safe)';
    const { container } = render(BriefingEdition, { edition: { ...edition, document } });
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('a[href^="javascript:"]')).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'Read full block' }));
    const dialog = screen.getByRole('dialog');
    expect(dialog.querySelector('script')).toBeNull();
    expect(dialog.querySelector('img')).toBeNull();
    expect(within(dialog).getByRole('link', { name: 'Safe' })).toHaveAttribute(
      'rel',
      'noopener noreferrer',
    );
  });
});
