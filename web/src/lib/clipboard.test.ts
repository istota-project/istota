import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { get } from 'svelte/store';
import { copyText } from './clipboard';
import { currentNotice, clearNotices } from './stores/notices';

function stubClipboard(impl: ((text: string) => Promise<void>) | null) {
  if (impl === null) {
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true });
    return;
  }
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText: vi.fn(impl) },
    configurable: true,
  });
}

describe('copyText', () => {
  beforeEach(() => clearNotices());
  afterEach(() => {
    clearNotices();
    vi.restoreAllMocks();
  });

  it('writes the text to the clipboard and reports success', async () => {
    const written: string[] = [];
    stubClipboard(async (t) => void written.push(t));

    await expect(copyText('# hello\n\nworld')).resolves.toBe(true);

    expect(written).toEqual(['# hello\n\nworld']);
    expect(get(currentNotice)?.severity).toBe('success');
  });

  it('takes a caller-supplied label so the notice can name what was copied', async () => {
    stubClipboard(async () => {});

    await copyText('x', { label: 'Code copied' });

    expect(get(currentNotice)?.message).toBe('Code copied');
  });

  it('coalesces repeats under one key rather than stacking', async () => {
    stubClipboard(async () => {});

    await copyText('a');
    await copyText('b');

    expect(get(currentNotice)?.count).toBe(2);
  });

  it('reports an error when the clipboard API is absent', async () => {
    // The realistic case: an insecure context, where navigator.clipboard is
    // simply not there. Silence would read as a successful copy.
    stubClipboard(null);

    await expect(copyText('x')).resolves.toBe(false);

    expect(get(currentNotice)?.severity).toBe('error');
  });

  it('reports an error when the write is refused', async () => {
    stubClipboard(async () => {
      throw new Error('denied');
    });

    await expect(copyText('x')).resolves.toBe(false);

    expect(get(currentNotice)?.severity).toBe('error');
  });

  it('refuses to copy an empty string without touching the clipboard', async () => {
    // Nothing to put on the clipboard, and claiming "Copied" would be a lie.
    // Guards the streaming case where a block exists but has no text yet.
    const write = vi.fn(async () => {});
    stubClipboard(write);

    await expect(copyText('   ')).resolves.toBe(false);

    expect(write).not.toHaveBeenCalled();
  });
});
