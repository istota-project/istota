import { base } from '$app/paths';
import { goto } from '$app/navigation';
import { chatRoomLink, isSafeActionPath, isServerActionPath } from '$lib/api';
import { consumePendingOpen, onPushOpen, pushAvailable, type PushOpen } from './nativePush';

function openPath(open: PushOpen): string {
  // The server deep link checks membership and lands on a fresh room list, which a
  // warm page may lack when the push gave the room its first web handle.
  if (open.kind === 'chat') {
    const path = chatRoomLink(open.room_token);
    if (path) return path;
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
