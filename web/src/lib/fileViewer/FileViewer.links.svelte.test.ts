import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
vi.mock('$app/paths', () => ({ base: '/istota', assets: '' }));
vi.mock('$lib/api', async (original) => ({
  ...(await original<typeof import('$lib/api')>()),
  previewChatFile: vi.fn(),
  getBriefingArchiveItem: vi.fn(),
}));
import { chatFileUrl, previewChatFile, getBriefingArchiveItem, type FilePreview } from '$lib/api';
import { selectedBriefingId } from '$lib/stores/briefings';
import Message from '$lib/components/chat/Message.svelte';
import Briefing from '../../routes/briefings/+page.svelte';
import FileViewerHost from './FileViewerHost.svelte';
import { viewer } from './store.svelte';

const first = '/Users/alice/istota/first.md';
const second = '/Users/alice/istota/second.md';
const image = '/Users/alice/istota/image.png';
const preview = vi.mocked(previewChatFile);
function text(name: string, body: string): FilePreview {
  return {
    name,
    text: body,
    kind: 'text',
    size: 20,
    modified: '2026-01-01T00:00:00Z',
    truncated: false,
  };
}
beforeEach(() => {
  viewer.close();
  vi.clearAllMocks();
  preview.mockImplementation(async (path) =>
    path === first
      ? text(
          'first.md',
          `# First\n[**Next file**](${chatFileUrl(second)})\n![Inline](${chatFileUrl(image)})`,
        )
      : path === second
        ? text('second.md', '# Second')
        : {
            name: 'image.png',
            kind: 'image',
            media_type: 'image/png',
            size: 20,
            modified: '2026-01-01T00:00:00Z',
            truncated: false,
          },
  );
});
afterEach(() => {
  cleanup();
  viewer.close();
  selectedBriefingId.set(null);
  vi.restoreAllMocks();
});

describe('delegated links through the real viewer host', () => {
  it('opens from a message, replaces from nested markdown, closes and reopens fresh', async () => {
    render(FileViewerHost);
    render(Message, {
      onConfirm: () => {},
      onReject: () => {},
      message: {
        cid: 1,
        role: 'assistant',
        text: '',
        streaming: false,
        segments: [
          { kind: 'text', id: 't0', text: `[Read file](${chatFileUrl(first)})`, settled: true },
        ],
      },
    });
    const opener = screen.getByRole('link', { name: 'Read file' });
    opener.focus();
    expect(await fireEvent.click(opener)).toBe(false);
    expect(await screen.findByRole('heading', { name: 'First', exact: true })).toBeTruthy();
    expect(await fireEvent.click(screen.getByAltText('Inline'))).toBe(true);
    expect(document.querySelector('.lightbox')).toBeNull();
    const nested = screen.getByText('Next file');
    expect(await fireEvent.click(nested, { metaKey: true })).toBe(true);
    expect(preview).toHaveBeenCalledTimes(1);
    expect(await fireEvent.click(nested)).toBe(false);
    expect(await screen.findByRole('heading', { name: 'Second', exact: true })).toBeTruthy();
    expect(preview.mock.calls.map(([path]) => path)).toEqual([first, second]);
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    expect(screen.queryByRole('heading', { name: 'First', exact: true })).toBeNull();
    const download = screen.getByRole('link', { name: 'Download' });
    expect(download.getAttribute('href')).toBe(chatFileUrl(second));
    expect(await fireEvent.click(download)).toBe(true);
    expect(preview).toHaveBeenCalledTimes(2);
    await fireEvent.click(screen.getByRole('button', { name: 'Source' }));
    await fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(opener));
    await fireEvent.click(opener);
    expect(await screen.findByRole('heading', { name: 'First', exact: true })).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Close', exact: true }));
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('opens a briefing file link into the host image handoff', async () => {
    vi.mocked(getBriefingArchiveItem).mockResolvedValue({
      id: 1,
      task_id: null,
      briefing_name: 'Morning',
      generated_at: '2026-01-01T00:00:00Z',
      subject: 'Morning briefing',
      body_md: `[**View image**](${chatFileUrl(image)})`,
      delivered_to: [],
    });
    selectedBriefingId.set(1);
    render(FileViewerHost);
    render(Briefing);
    expect(await fireEvent.click(await screen.findByText('View image'))).toBe(false);
    await waitFor(() =>
      expect(document.querySelector('.lightbox img')?.getAttribute('src')).toBe(chatFileUrl(image)),
    );
    expect(screen.queryByRole('dialog')).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'Close image' }));
    expect(document.querySelector('.lightbox')).toBeNull();
  });
});
