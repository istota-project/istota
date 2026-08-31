import { getContext, setContext } from 'svelte';
import type { User } from '$lib/api';

const CURRENT_USER = Symbol('current-user');

/**
 * The identity the root layout resolved, published to every route under it.
 *
 * Four routes used to fetch `/me` for themselves and none of them agreed on
 * what a failure meant (ISSUE-355). The dashboard's copy was the visible one:
 * offline its `getMe()` rejected, the mount returned early, and the whole page
 * body — every tile is a static link gated on `user.features` — stayed behind a
 * null check while the shell and the header rendered around nothing.
 *
 * The layout has already answered that question by the time any page mounts.
 * Children render only inside its `{:else if user}` branch, so `user` is
 * non-null by construction here: a reader needs no null handling and no wait.
 *
 * `user` is a getter rather than a value so the layout can back it with
 * `$state`. That is load-bearing, not a style: a cached identity is swapped for
 * the live one when the connection returns (ISSUE-354), and a page that read
 * the value once at mount would pin the stale copy for the life of the page —
 * the same defect as the one this replaces, one layer quieter.
 *
 * **It is display data.** It may be the cached record rather than a live one,
 * and every authorization check is server-side regardless. Branch on it to
 * decide what is *shown*, never what is *permitted*.
 */
export interface CurrentUser {
  readonly user: User;
  /**
   * Whether the *server* confirmed this record during this session, as against
   * the cache having supplied it (ISSUE-354).
   *
   * Not provenance for its own sake: it is the answer to "may I write anything
   * under this identity". A cached record can be the last-user pointer's guess,
   * and the chat store already refuses to key its cache or drain its queue
   * while a guess stands — anything else that spells a per-user storage key has
   * to refuse on the same terms, or it writes one person's text into another
   * person's drawer with nothing to collect it. Reactive like `user`: it turns
   * true when a retry reaches the server.
   *
   * Never a permission. It says whether the record is current, not what the
   * person may do.
   */
  readonly live: boolean;
  /** End the current session and route to the server-rendered login page. */
  expireSession: () => void;
  /**
   * Re-resolve the identity from the server, for a page that needs it fresh
   * rather than merely current — `nextcloud_token` changes while the settings
   * page is open, and that page has to make the round trip anyway.
   *
   * Routed through the layout rather than fetched privately, so the answer
   * reaches the nav and the offline cache too. Resolves to whether the server
   * answered: it never rejects (the layout owns the 401 redirect and the
   * offline fallback), so a caller that needs to know must read the result. A
   * `false` means `user` is whatever it already was — the layout will not paint
   * a cached record over a live one — so a page showing a value that only a
   * fresh record can vouch for should say it could not confirm it rather than
   * show a stale one.
   */
  reload: () => Promise<boolean>;
}

export function setCurrentUser(ctx: CurrentUser): void {
  setContext(CURRENT_USER, ctx);
}

export function getCurrentUser(): CurrentUser {
  const ctx = getContext<CurrentUser | undefined>(CURRENT_USER);
  if (!ctx) throw new Error('getCurrentUser() outside the root layout');
  return ctx;
}
