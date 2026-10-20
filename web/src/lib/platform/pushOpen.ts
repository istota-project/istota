import { base } from '$app/paths';
import { goto } from '$app/navigation';
import { isSafeActionPath, isServerActionPath } from '$lib/api';
import { consumePendingOpen, onPushOpen, pushAvailable, type PushOpen } from './nativePush';

function openPath(open: PushOpen): string {
  // The server deep link checks membership and lands on a fresh room list, which a
  // warm page may lack when the push gave the room its first web handle.
  if (
    open.kind === 'chat' &&
    typeof open.room_token === 'string' &&
    open.room_token !== '' &&
    !open.room_token.includes('/')
  ) {
    const path = `/chat/r/${open.room_token}`;
    if (isSafeActionPath(path)) return path;
  }
  if (
    open.kind === 'notification' &&
    isSafeActionPath(open.link) &&
    open.link.trim() === open.link
  ) {
    return open.link;
  }
  console.warn('[istota] Push open has no safe destination.');
  return '/chat';
}

export function startPushOpenHandling(
  navigateServer: (path: string) => void = (path) => window.location.assign(path),
): () => void {
  if (!pushAvailable()) return () => {};
  let stopped = false;
  async function consume() {
    try {
      const open = await consumePendingOpen();
      if (stopped || !open) return;
      const path = openPath(open);
      if (isServerActionPath(path)) navigateServer(`${base}${path}`);
      else await goto(`${base}${path}`);
    } catch {
      console.warn('[istota] Could not open the pending notification.');
    }
  }
  const unsubscribe = onPushOpen(() => void consume());
  void consume();
  return () => {
    stopped = true;
    unsubscribe();
  };
}
