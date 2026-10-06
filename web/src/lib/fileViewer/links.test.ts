import { afterEach, describe, expect, it, vi } from 'vitest';
vi.mock('$app/paths', () => ({ base: '/istota', assets: '' }));
import { chatFileUrl } from '$lib/api';
import { fileLinkFromEvent, fileLinks } from './links';
import { viewer } from './store.svelte';

const path = '/Users/alice/istota/a # & note.md';
const url = chatFileUrl(path);
afterEach(() => {
  viewer.close();
  vi.restoreAllMocks();
});

function clickPath(href: string, options: MouseEventInit = {}, nested = false, claimed = false) {
  const anchor = document.createElement('a');
  anchor.setAttribute('href', href);
  const span = document.createElement('span');
  anchor.append(span);
  const event = new MouseEvent('click', { bubbles: true, cancelable: true, ...options });
  if (claimed) event.preventDefault();
  let result: string | null = null;
  anchor.addEventListener('click', (e) => {
    result = fileLinkFromEvent(e);
  });
  (nested ? span : anchor).dispatchEvent(event);
  return result;
}

describe('fileLinkFromEvent', () => {
  it('decodes a workspace path, including escaped path punctuation', () => {
    expect(clickPath(url)).toBe(path);
  });
  it('finds the anchor around a nested target and drops image size fragments', () => {
    expect(clickPath(`${url}#w=100&h=200`, {}, true)).toBe(path);
  });
  it.each(['ctrlKey', 'metaKey', 'shiftKey', 'altKey'])('preserves %s', (key) => {
    expect(clickPath(url, { [key]: true })).toBeNull();
  });
  it('preserves middle clicks and claimed events', () => {
    expect(clickPath(url, { button: 1 })).toBeNull();
    expect(clickPath(url, {}, false, true)).toBeNull();
  });
  it.each([
    `https://example.com${url}`,
    `//example.com${url}`,
    `/istota/chat`,
    'note.md',
    '/istota/api/chat/files/preview?path=/note.md',
    '/istota/api/chat/files?',
    '/istota/api/chat/files?path=',
    '/istota/api/chat/files?#path=/note.md',
    'javascript:alert(1)',
  ])('does not intercept %s', (href) => {
    expect(clickPath(href)).toBeNull();
  });
  it('ignores events with no element target', () => {
    expect(fileLinkFromEvent(new MouseEvent('click'))).toBeNull();
  });
});

describe('fileLinks action', () => {
  it('delegates to newly inserted children once, then removes its listener', () => {
    const open = vi.spyOn(viewer, 'openFile');
    const node = document.createElement('div');
    const action = fileLinks(node);
    const anchor = document.createElement('a');
    anchor.href = url;
    node.append(anchor);
    const click = () =>
      anchor.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    expect(click()).toBe(false);
    expect(open).toHaveBeenCalledExactlyOnceWith(path);
    action.destroy();
    expect(click()).toBe(true);
    expect(open).toHaveBeenCalledTimes(1);
  });
});
