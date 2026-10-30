import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
vi.mock('$app/paths', () => ({ base: '/istota', assets: '' }));
import TextPreview from './TextPreview.svelte';
import { HIGHLIGHT_MAX_CHARS } from '$lib/fileViewer/presentation';
import { HTML_FRAME_SANDBOX, sandboxedDocument } from '$lib/fileViewer/html';
import { viewer } from '$lib/fileViewer/store.svelte';
import { VIEWPORT_DEFAULT, VIEWPORT_NO_ZOOM } from '$lib/zoomPin';

afterEach(() => {
  cleanup();
  viewer.close();
});

describe('shared text preview', () => {
  it('renders Markdown and frontmatter, or the readonly source when asked', async () => {
    const text = '---\ntitle: Note\n---\n# Hello';
    const { rerender } = render(TextPreview, { name: 'note.md', text });
    expect(screen.getByRole('heading', { name: 'Hello' })).toBeTruthy();
    const frontmatter = screen.getByText('Frontmatter').closest('details');
    expect(frontmatter).toHaveTextContent('title: Note');
    expect(frontmatter?.nextElementSibling).toHaveClass('markdown');
    expect(screen.queryByRole('button')).toBeNull();
    await rerender({ source: true });
    expect(screen.getByRole('textbox')).toHaveValue(text);
    expect(screen.getByRole('textbox')).toHaveAttribute('readonly');
    expect(screen.queryByRole('heading')).toBeNull();
    await rerender({ source: false });
    expect(screen.getByRole('heading', { name: 'Hello' })).toBeTruthy();
  });

  it('keeps plain text literal, selectable and readonly', () => {
    render(TextPreview, { name: 'note.txt', text: '<b>plain</b>' });
    const input = screen.getByRole('textbox', { name: 'Source of note.txt' });
    expect(input).toHaveValue('<b>plain</b>');
    expect(input).toHaveAttribute('readonly');
    expect(input).not.toBeDisabled();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('pins the viewport zoom while the source view has focus', async () => {
    // The textarea renders under the 16px touch floor on purpose, so it has to
    // stop iOS zooming in on focus itself.
    const meta = document.createElement('meta');
    meta.name = 'viewport';
    meta.content = VIEWPORT_DEFAULT;
    document.head.append(meta);
    try {
      render(TextPreview, { name: 'note.txt', text: 'plain' });
      const input = screen.getByRole('textbox');
      await fireEvent.focus(input);
      expect(meta.content).toBe(VIEWPORT_NO_ZOOM);
      await fireEvent.blur(input);
      expect(meta.content).toBe(VIEWPORT_DEFAULT);
    } finally {
      meta.remove();
    }
  });

  it('shows svg as highlighted source, never live markup', () => {
    const text = '<svg onload="alert(1)"><script>alert(1)</script></svg>';
    const { container } = render(TextPreview, { name: 'file.svg', text });
    expect(container.querySelector('pre code')).toHaveTextContent(text);
    expect(container.querySelector('svg, script, iframe, object, embed')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Rendered' })).toBeNull();
  });

  it('renders html only inside a scriptless sandboxed frame, or as source when asked', async () => {
    const text = '<p onclick="alert(1)">Hi</p><script>alert(1)</script>';
    const { container, rerender } = render(TextPreview, { name: 'page.html', text });
    const frame = await vi.waitFor(() => {
      const frame = container.querySelector('iframe');
      if (!frame) throw new Error('no frame yet');
      return frame;
    });
    expect(frame.getAttribute('sandbox')).toBe(HTML_FRAME_SANDBOX);
    expect(frame.getAttribute('sandbox')).not.toContain('allow-scripts');
    expect(frame.getAttribute('srcdoc')).toBe(sandboxedDocument(text));
    expect(frame).toHaveAttribute('title', 'page.html');
    expect(frame).toHaveAttribute('referrerpolicy', 'no-referrer');
    expect(container.querySelector('script, p')).toBeNull();
    await rerender({ source: true });
    expect(container.querySelector('iframe')).toBeNull();
    expect(container.querySelector('pre code.hljs')).toHaveTextContent(text);
  });

  it("inlines a page's relative image through the caller's file URL", async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(new Uint8Array([0x89, 0x50, 0x4e, 0x47]), {
          headers: { 'Content-Type': 'image/png' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);
    try {
      const { container } = render(TextPreview, {
        name: 'r.html',
        text: '<img src="img/a.png">',
        path: '/docs/r.html',
        assetUrl: (path: string) => `/files?path=${encodeURIComponent(path)}`,
      });
      const frame = await vi.waitFor(() => {
        const frame = container.querySelector('iframe');
        if (!frame) throw new Error('no frame yet');
        return frame;
      });
      expect(fetchMock).toHaveBeenCalledWith(
        '/files?path=%2Fdocs%2Fimg%2Fa.png',
        expect.anything(),
      );
      expect(frame.getAttribute('srcdoc')).toContain('src="data:image/png;base64,iVBORw=="');
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('highlights code up to the limit and uses readonly text above it', async () => {
    const text = 'const value = 1;';
    const { container, rerender } = render(TextPreview, {
      name: 'code.js',
      text: text.padEnd(HIGHLIGHT_MAX_CHARS),
    });
    expect(container.querySelector('code.hljs span')).not.toBeNull();
    expect(screen.queryByRole('textbox')).toBeNull();
    await rerender({ text: text.padEnd(HIGHLIGHT_MAX_CHARS + 1) });
    expect(container.querySelector('code')).toBeNull();
    expect(screen.getByRole('textbox')).toHaveAttribute('readonly');
  });

  it('delegates plain document links while preserving modified clicks', async () => {
    render(TextPreview, {
      name: 'note.md',
      text: '[Next](/istota/api/chat/files?path=%2Fnext.md)',
    });
    const link = screen.getByRole('link', { name: 'Next' });
    expect(await fireEvent.click(link, { metaKey: true })).toBe(true);
    expect(viewer.state.mode).toBe('closed');
    expect(await fireEvent.click(link)).toBe(false);
    expect(viewer.state).toMatchObject({ mode: 'file', path: '/next.md' });
  });
});
