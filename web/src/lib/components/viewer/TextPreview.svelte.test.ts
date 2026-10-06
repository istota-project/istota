import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
vi.mock('$app/paths', () => ({ base: '/istota', assets: '' }));
import TextPreview from './TextPreview.svelte';
import { HIGHLIGHT_MAX_CHARS } from '$lib/fileViewer/presentation';
import { viewer } from '$lib/fileViewer/store.svelte';

afterEach(() => {
  cleanup();
  viewer.close();
});

describe('shared text preview', () => {
  it('renders Markdown and frontmatter with a reversible readonly source view', async () => {
    const text = '---\ntitle: Note\n---\n# Hello';
    render(TextPreview, { name: 'note.md', text });
    expect(screen.getByRole('heading', { name: 'Hello' })).toBeTruthy();
    expect(screen.getByText('Frontmatter').closest('details')).toHaveTextContent('title: Note');
    await fireEvent.click(screen.getByRole('button', { name: 'Source' }));
    expect(screen.getByRole('textbox')).toHaveValue(text);
    expect(screen.getByRole('textbox')).toHaveAttribute('readonly');
    expect(screen.queryByRole('heading')).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'Rendered' }));
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

  it.each(['html', 'svg'])('shows %s as highlighted source, never live markup', (extension) => {
    const text = '<svg onload="alert(1)"><script>alert(1)</script></svg>';
    const { container } = render(TextPreview, { name: `file.${extension}`, text });
    expect(container.querySelector('pre code')).toHaveTextContent(text);
    expect(container.querySelector('svg, script, iframe, object, embed')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Rendered' })).toBeNull();
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
