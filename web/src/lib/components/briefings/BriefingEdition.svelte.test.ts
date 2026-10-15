import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import type { BriefingDocument } from '$lib/api';
import BriefingEdition from './BriefingEdition.svelte';
import BriefingBlockCard from './BriefingBlockCard.svelte';
import { edition } from './edition.fixture';

afterEach(cleanup);

describe('briefing editions', () => {
  it('renders saved cards in order, with literal titles and an accessible source warning', () => {
    const { container } = render(BriefingEdition, { edition });
    expect(screen.getByRole('heading', { level: 1, name: edition.subject })).toBeInTheDocument();
    expect(screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)).toEqual([
      'Headlines <em>today</em>',
      'Calendar',
      'Notes',
    ]);
    expect(container.querySelector('h2 em')).toBeNull();
    expect(screen.queryByText('Some sources unavailable')).toBeNull();
    expect(screen.getByRole('button', { name: 'Some sources unavailable' })).toBeInTheDocument();
    expect(screen.getByText('An appointment')).toBeInTheDocument();
    expect(screen.queryByText('The linear edition fallback.')).toBeNull();
    expect(
      [...container.querySelectorAll('[data-width]')].map((el) => el.getAttribute('data-width')),
    ).toEqual(['wide', 'auto', 'full']);
    expect(screen.getByRole('link', { name: 'A story' })).toHaveAttribute(
      'href',
      'https://example.com/story',
    );
    expect(screen.getByRole('link', { name: 'Full story' })).toBeInTheDocument();
  });

  it.each(['close', 'escape'])(
    'opens complete content and restores focus after %s',
    async (method) => {
      render(BriefingEdition, { edition });
      const opener = screen.getByRole('button', { name: 'Expand Headlines <em>today</em>' });
      opener.focus();
      await fireEvent.click(opener);
      const dialog = screen.getByRole('dialog', { name: 'Headlines <em>today</em>' });
      expect(dialog).toHaveClass('ui-viewer-content');
      expect(within(dialog).getByRole('link', { name: 'Full story' })).toHaveAttribute(
        'href',
        'https://example.com/full',
      );
      expect(
        within(dialog).queryByRole('button', { name: 'Expand Headlines <em>today</em>' }),
      ).toBeNull();
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
    expect(screen.queryByRole('button', { name: 'Expand Headlines <em>today</em>' })).toBeNull();
  });

  it('keeps hostile body markup inert through preview and full content', async () => {
    const document = structuredClone(edition.document!);
    document.blocks[0].body_md =
      '<script>alert(1)</script>\n\n[Bad](javascript:alert(1))\n\n![Remote](https://example.com/pixel.png)\n\n[Safe](https://example.com/safe)';
    const { container } = render(BriefingEdition, { edition: { ...edition, document } });
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('a[href^="javascript:"]')).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'Expand Headlines <em>today</em>' }));
    const dialog = screen.getByRole('dialog');
    expect(dialog.querySelector('script')).toBeNull();
    expect(dialog.querySelector('img')).toBeNull();
    expect(within(dialog).getByRole('link', { name: 'Safe' })).toHaveAttribute(
      'rel',
      'noopener noreferrer',
    );
  });
});

it('expands short cards and shows settings only for editable live blocks', async () => {
  const onEditBlock = vi.fn();
  render(BriefingEdition, {
    edition,
    editableBlockIds: [edition.document!.blocks[1].block_id],
    onEditBlock,
  });
  expect(screen.getAllByRole('button', { name: /^Expand / })).toHaveLength(3);
  expect(screen.getAllByRole('button', { name: /^Settings for / })).toHaveLength(1);
  expect(
    within(screen.getByRole('region', { name: 'Calendar' }))
      .getAllByRole('button')
      .map((button) => button.getAttribute('aria-label')),
  ).toEqual(['Settings for Calendar', 'Expand Calendar']);
  await fireEvent.click(screen.getByRole('button', { name: 'Settings for Calendar' }));
  expect(onEditBlock).toHaveBeenCalledWith(edition.document!.blocks[1].block_id);
  await fireEvent.click(screen.getByRole('button', { name: 'Expand Calendar' }));
  expect(
    within(screen.getByRole('dialog', { name: 'Calendar' })).getByText('An appointment'),
  ).toBeInTheDocument();
});

it('shows complete long lists and all paragraphs directly in the card', () => {
  const block = {
    ...edition.document!.blocks[1],
    body_md:
      '- One\n- Two\n- Three\n- Four\n- Five\n- Six\n- Seven\n\nFirst paragraph\n\nSecond paragraph\n\nThird paragraph\n\n[Last paragraph](https://example.com/last)',
  };
  render(BriefingBlockCard, { block });
  expect(screen.getAllByRole('listitem')).toHaveLength(7);
  expect(screen.getByRole('link', { name: 'Last paragraph' })).toBeInTheDocument();
  expect(screen.queryByRole('dialog')).toBeNull();
});

it('opens the partial-source explanation on demand without adding text to the card', async () => {
  render(BriefingEdition, { edition });
  const warning = screen.getByRole('button', { name: 'Some sources unavailable' });
  expect(warning.querySelector('svg')).not.toBeNull();
  expect(warning.closest('.block-title')).not.toBeNull();
  expect(
    within(screen.getByRole('region', { name: 'Calendar' })).queryByRole('button', {
      name: 'Some sources unavailable',
    }),
  ).toBeNull();
  await fireEvent.click(warning);
  expect(await screen.findByText('Some sources unavailable')).toBeVisible();
});
