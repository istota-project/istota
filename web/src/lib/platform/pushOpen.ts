import { base } from '$app/paths';
import { goto } from '$app/navigation';
import { isSafeActionPath, isServerActionPath } from '$lib/api';
import { consumePendingOpen, onPushOpen, pushAvailable, type PushOpen } from './nativePush';

function openPath(open: PushOpen): string {
  if (open.kind === 'chat' && Number.isSafeInteger(open.room_id) && open.room_id! > 0) {
    return `/chat?room=${open.room_id}`;
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
