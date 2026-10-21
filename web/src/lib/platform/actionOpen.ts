import { base } from '$app/paths';
import { goto } from '$app/navigation';
import { chatRoomLink, isServerActionPath } from '$lib/api';
import {
  actionsAvailable,
  consumeActionOpen,
  onActionOpen,
  type ActionOpen,
} from './nativeActions';

/**
 * The Files folder for a path relative to My files. The native sheet's Show in
 * Files leaves the saved note's path, so a `.md` last part opens the folder it
 * is in. The path is a query value, never a route, so it cannot point the
 * navigation anywhere but the Files page.
 */
function filesFolder(path: unknown): string {
  if (typeof path !== 'string') return '';
  const parts = path.split('/').filter((part) => part !== '');
  if (parts.some((part) => part === '.' || part === '..' || /[\u0000-\u001f]/.test(part))) {
    return '';
  }
  if (parts.length > 0 && parts[parts.length - 1].toLowerCase().endsWith('.md')) parts.pop();
  return parts.join('/');
}

function openPath(open: ActionOpen): string | null {
  if (open.kind === 'notes-settings') return '/settings/preferences/#quick-notes';
  if (open.kind === 'files') {
    const folder = filesFolder(open.path);
    const query = new URLSearchParams({ place: 'home', ...(folder ? { path: folder } : {}) });
    return `/files?${query}`;
  }
  // The server resolves the token to the user's room, or lands on the chat
  // page's default for a room that is gone or not theirs.
  if (open.kind === 'room') return chatRoomLink(open.token) ?? '/chat';
  console.warn('[istota] Native action open has no known destination.');
  return null;
}

export function startActionOpenHandling(
  navigateServer: (path: string) => void = (path) => window.location.assign(path),
): () => void {
  if (!actionsAvailable()) return () => {};
  let stopped = false;
  async function consume() {
    try {
      const open = await consumeActionOpen();
      if (stopped || !open) return;
      const path = openPath(open);
      if (!path) return;
      if (isServerActionPath(path)) navigateServer(`${base}${path}`);
      else await goto(`${base}${path}`);
    } catch {
      console.warn('[istota] Could not open the pending native action.');
    }
  }
  const unsubscribe = onActionOpen(() => void consume());
  void consume();
  return () => {
    stopped = true;
    unsubscribe();
  };
}
