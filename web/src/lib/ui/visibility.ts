/**
 * Run `fn` each time the document becomes visible again: the user coming back
 * to the tab or the app. Returns the cleanup, so it can be the return value of
 * an `onMount` or `$effect`.
 */
export function onDocumentVisible(fn: () => void): () => void {
  if (typeof document === 'undefined') return () => {};
  const handler = () => {
    if (document.visibilityState === 'visible') fn();
  };
  document.addEventListener('visibilitychange', handler);
  return () => document.removeEventListener('visibilitychange', handler);
}
