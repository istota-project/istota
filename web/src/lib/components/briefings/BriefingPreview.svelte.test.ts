import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import type { BriefingArchiveItem, BriefingBlock } from '$lib/api';
import { acceptanceEdition, acceptancePreview, edition } from './edition.fixture';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
import BriefingPreview from './BriefingPreview.svelte';

const blocks: BriefingBlock[] = edition.document!.blocks.map((block, position) => ({
  id: block.block_id,
  briefing_name: 'morning',
  position,
  title: block.title,
  directive: '',
  render_mode: 'synthesis',
  options: { presentation: block.presentation },
  sources: [],
}));
const props = { briefingName: 'morning', blocks, onClose: vi.fn() };

beforeEach(() => {
  vi.clearAllMocks();
  api.getBriefingArchive.mockResolvedValue({
    items: [edition],
    total: 1,
    briefing_names: ['morning'],
  });
  api.getBriefingArchiveItem.mockResolvedValue(edition);
  api.previewBriefing.mockResolvedValue({
    body_md: 'Full body',
    plain: 'A story (https://example.com/story)\n\nFull last paragraph',
    html: '<html><head></head><body><h2>Headlines</h2><p><a href="https://example.com/story">A story</a></p><img src="https://example.com/pixel"><script>unsafe()</script><form action="https://example.com/send"></form></body></html>',
  });
});
afterEach(cleanup);

async function loaded() {
  await waitFor(() => expect(api.previewBriefing).toHaveBeenCalledOnce());
  await waitFor(() => expect(screen.queryByText('Loading preview…')).toBeNull());
}

describe('briefing delivery previews', () => {
  it('uses the production edition grid, order and full viewer, then isolates HTML and plain alternatives', async () => {
    render(BriefingPreview, props);
    await loaded();
    const article = screen.getByRole('article');
    expect(
      within(article)
        .getAllByRole('heading', { level: 2 })
        .map((h) => h.textContent),
    ).toEqual(blocks.map((block) => block.title));
    const opener = within(article).getByRole('button', { name: 'Expand Headlines <em>today</em>' });
    opener.focus();
    await fireEvent.click(opener);
    const viewer = screen.getByRole('dialog', { name: 'Headlines <em>today</em>' });
    expect(within(viewer).getByRole('link', { name: 'Full story' })).toBeInTheDocument();
    await fireEvent.click(within(viewer).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(opener).toHaveFocus());
    await fireEvent.click(screen.getByRole('radio', { name: 'HTML email' }));
    const frame = screen.getByTitle('HTML email preview');
    expect(frame).toHaveAttribute('sandbox', '');
    expect(frame).toHaveAttribute('referrerpolicy', 'no-referrer');
    const html = new DOMParser().parseFromString(frame.getAttribute('srcdoc')!, 'text/html');
    expect(
      html.querySelector('meta[http-equiv="Content-Security-Policy"]')?.getAttribute('content'),
    ).toBe("default-src 'none'; base-uri 'none'; form-action 'none'");
    expect(html.querySelector('a, script, img, form')).toBeNull();
    expect(html.querySelector('span')?.textContent).toBe('A story');
    expect(html.body.textContent).toContain('Headlines');
    await fireEvent.click(screen.getByRole('radio', { name: 'Plain text' }));
    expect(screen.getByLabelText('Plain text preview')).toHaveTextContent('Full last paragraph');
    expect(screen.getByLabelText('Plain text preview').tagName).toBe('PRE');
    expect(api.previewBriefing).toHaveBeenCalledOnce();
  });

  it('uses placeholders for new IDs and legacy content, without title or other-briefing matching', async () => {
    const { rerender } = render(BriefingPreview, {
      ...props,
      blocks: [blocks[1], { ...blocks[0], id: 900 }],
      draft: {
        briefing_name: 'morning',
        title: 'New block',
        options: { presentation: { width: 'full' } },
      },
    });
    await loaded();
    const document = api.previewBriefing.mock.calls[0][0];
    expect(document.blocks.map((block: { block_id: number }) => block.block_id)).toEqual([
      43, 900, -1,
    ]);
    expect(document.blocks[0].body_md).toBe(edition.document!.blocks[1].body_md);
    expect(document.blocks[1].body_md).toContain('Placeholder:');
    expect(document.blocks[2]).toMatchObject({
      title: 'New block',
      presentation: { width: 'full' },
    });
    expect(document.blocks[2].body_md).toContain('Placeholder:');
    expect(screen.queryByRole('link', { name: 'A story' })).toBeNull();
    api.getBriefingArchiveItem.mockResolvedValue({ ...edition, document: null });
    await rerender({ ...props, blocks: [...blocks] });
    await waitFor(() => expect(api.previewBriefing).toHaveBeenCalledTimes(2));
    expect(
      api.previewBriefing.mock.calls[1][0].blocks.every((block: { body_md: string }) =>
        block.body_md.startsWith('Placeholder:'),
      ),
    ).toBe(true);
    api.getBriefingArchiveItem.mockResolvedValue({ ...edition, briefing_name: 'different' });
    await rerender({ ...props, blocks: [...blocks] });
    await waitFor(() => expect(api.previewBriefing).toHaveBeenCalledTimes(3));
    expect(
      api.previewBriefing.mock.calls[2][0].blocks.every((block: { body_md: string }) =>
        block.body_md.startsWith('Placeholder:'),
      ),
    ).toBe(true);
  });

  it('renders a new briefing with labelled placeholders and no fabricated edition date', async () => {
    api.getBriefingArchive.mockResolvedValue({ items: [], total: 0, briefing_names: [] });
    render(BriefingPreview, props);
    await loaded();
    expect(api.getBriefingArchiveItem).not.toHaveBeenCalled();
    expect(
      screen.getAllByText('Placeholder: no saved edition content for this block.'),
    ).toHaveLength(3);
    expect(screen.queryByText(/Invalid Date/)).toBeNull();
  });

  it('ignores late archive and render responses after selection or unmount', async () => {
    let finishArchive!: (edition: BriefingArchiveItem) => void;
    api.getBriefingArchiveItem.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishArchive = resolve;
        }),
    );
    const view = render(BriefingPreview, props);
    await waitFor(() => expect(api.getBriefingArchiveItem).toHaveBeenCalledOnce());
    await view.rerender({ ...props, briefingName: 'evening', blocks: [] });
    await loaded();
    finishArchive(edition);
    await waitFor(() =>
      expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Preview of evening'),
    );
    expect(api.previewBriefing).toHaveBeenCalledOnce();
    view.unmount();
    let finishRender!: (result: { body_md: string; plain: string; html: string }) => void;
    api.previewBriefing.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishRender = resolve;
        }),
    );
    const next = render(BriefingPreview, props);
    await waitFor(() => expect(api.previewBriefing).toHaveBeenCalledTimes(2));
    next.unmount();
    finishRender({ body_md: 'Late', plain: 'Late', html: '<p>Late</p>' });
    await Promise.resolve();
    expect(screen.queryByText('Late')).toBeNull();
  });

  it('shows archive failures and retries without saving or generating content', async () => {
    api.getBriefingArchive.mockRejectedValueOnce(new Error('Archive unavailable'));
    render(BriefingPreview, props);
    await screen.findByRole('alert');
    expect(api.previewBriefing).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByRole('button', { name: 'Retry preview' }));
    await loaded();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(api.putBriefingBlock).not.toHaveBeenCalled();
    expect(api.runSharedBlock).not.toHaveBeenCalled();
  });
});

it('uses the same actual archive and render response as the completion-to-MIME acceptance fixture', async () => {
  api.getBriefingArchive.mockResolvedValue({
    items: [acceptanceEdition],
    total: 1,
    briefing_names: ['M'],
  });
  api.getBriefingArchiveItem.mockResolvedValue(acceptanceEdition);
  api.previewBriefing.mockResolvedValue(acceptancePreview);
  const definitions = acceptanceEdition.document!.blocks.map((block, position) => ({
    id: block.block_id,
    briefing_name: 'M',
    position,
    title: block.title,
    directive: '',
    render_mode: 'synthesis',
    options: { presentation: block.presentation },
    sources: [],
  }));
  render(BriefingPreview, { ...props, briefingName: 'M', blocks: definitions });
  await loaded();
  expect(api.previewBriefing).toHaveBeenCalledWith(acceptanceEdition.document);
  expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(acceptanceEdition.subject);
  const article = screen.getByRole('article');
  expect(
    within(article)
      .getAllByRole('heading', { level: 2 })
      .map((heading) => heading.textContent),
  ).toEqual(acceptanceEdition.document!.blocks.map((block) => block.title));
  expect(within(article).getByRole('link', { name: 'A story' })).toHaveAttribute(
    'href',
    'https://example.com/a_b?one=two_three&four=five_six',
  );
  await fireEvent.click(screen.getByRole('radio', { name: 'HTML email' }));
  const frame = new DOMParser().parseFromString(
    screen.getByTitle('HTML email preview').getAttribute('srcdoc')!,
    'text/html',
  );
  expect([...frame.querySelectorAll('h2')].map((heading) => heading.textContent)).toEqual(
    acceptanceEdition.document!.blocks.map((block) => block.title),
  );
  expect(frame.body.textContent).toContain('Full story');
  expect(frame.body.querySelector('a')).toBeNull();
  await fireEvent.click(screen.getByRole('radio', { name: 'Plain text' }));
  expect(screen.getByLabelText('Plain text preview').textContent).toBe(acceptancePreview.plain);
});

it('previews a legacy archive with no subject using labelled placeholders', async () => {
  api.getBriefingArchiveItem.mockResolvedValue({ ...edition, subject: '', document: null });
  render(BriefingPreview, props);
  await loaded();
  expect(api.previewBriefing.mock.calls[0][0].title).toBe('Preview of morning');
  expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Preview of morning');
  expect(screen.getAllByText('Placeholder: no saved edition content for this block.')).toHaveLength(
    3,
  );
});
