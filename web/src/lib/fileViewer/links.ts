import { CHAT_FILES_PREFIX } from '$lib/markdown';
import { viewer } from './store.svelte';

/** The workspace path to preview, or null to keep the browser's link behavior. */
export function fileLinkFromEvent(event: MouseEvent): string | null {
  if (
    event.defaultPrevented ||
    event.button !== 0 ||
    event.ctrlKey ||
    event.metaKey ||
    event.shiftKey ||
    event.altKey
  )
    return null;
  if (!(event.target instanceof Element)) return null;
  const href = event.target.closest('a')?.getAttribute('href');
  if (!href?.startsWith(CHAT_FILES_PREFIX)) return null;
  return new URL(href, location.origin).searchParams.get('path') || null;
}

/** Delegate links in rendered content, including content replaced after mounting. */
export function fileLinks(node: HTMLElement): { destroy(): void } {
  function click(event: MouseEvent) {
    const path = fileLinkFromEvent(event);
    if (path === null) return;
    event.preventDefault();
    viewer.openFile(path);
  }
  node.addEventListener('click', click);
  return { destroy: () => node.removeEventListener('click', click) };
}
