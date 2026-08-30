/**
 * Who this device belongs to, for the one moment nothing else can say.
 *
 * Every offline cache key is namespaced by `user_id`, which arrives on
 * `GET /chat/config`. A cold launch with no connection never gets that fetch,
 * so it has no key to read by and boots to an empty cache — the exact outcome
 * the service worker exists to prevent (ISSUE-202). This is the pointer that
 * closes that gap: `chat.lastUserId`, written on every successful config read
 * and read back as the seed for cache reads.
 *
 * **Read only inside the native shell**, and that gate is what makes it safe
 * rather than a hole in the namespacing it undoes. The namespace guards a
 * shared browser profile — two people, one Chrome, one `localStorage`. The
 * shell is one device, one session cookie, one person, and it is also the only
 * surface with a service worker to cold-launch offline at all. So the pointer
 * is read exactly where the hazard it would otherwise create cannot arise, and
 * nowhere else.
 *
 * The read is also *only* a read. Until the server confirms the id, the store
 * writes nothing under it and drains nothing from it (`cacheUserId` and
 * `canDrain` in `stores/chat.ts`) — a guess may decide what is painted, and
 * may not decide what is stored or what is sent.
 *
 * The net underneath it is in `stores/chat.ts`: when the real config arrives
 * and disagrees with the pointer, everything painted from the guessed
 * namespace is dropped and repainted from the right one. A wrong guess is a
 * flicker, not a cross-user read.
 *
 * Written through `persisted.ts` like the queue and the drafts, so a refusal
 * (private mode, quota, storage disabled) is swallowed the same way and costs
 * the cold-launch paint rather than the load.
 */
import { loadSetting, saveSetting } from '$lib/stores/persisted';
import { shellAtLeast } from '$lib/platform/native';

export const LAST_USER_KEY = 'chat.lastUserId';

/**
 * Record who the server says this session belongs to.
 *
 * Called on every successful config read rather than once, so a device that
 * changes hands re-points before the next cold launch reads it. `null` clears
 * the pointer, which is what a config carrying no `user_id` means.
 */
export function rememberLastUserId(userId: string | null): void {
  saveSetting(LAST_USER_KEY, userId ?? null);
}

/** Forget it — the 401 path that ends a session. */
export function forgetLastUserId(): void {
  saveSetting(LAST_USER_KEY, null);
}

/**
 * The id to read the cache by before the server has said, or null.
 *
 * Null in a browser, always: off the shell there is no cold launch to rescue
 * and the namespace is guarding something real. Null in a shell older than the
 * one that runs a service worker, too — without a worker there is no boot with
 * no connection for the guess to be needed on, so the same gate the
 * registration uses keeps the guess out of every session that cannot benefit
 * from it.
 */
export function seedUserId(): string | null {
  if (!shellAtLeast('0.10.0')) return null;
  const raw = loadSetting<unknown>(LAST_USER_KEY, null);
  return typeof raw === 'string' && raw ? raw : null;
}
