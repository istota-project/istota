<script lang="ts">
  import { base } from '$app/paths';
  import { afterNavigate } from '$app/navigation';
  import { page, updated } from '$app/state';
  import { onMount } from 'svelte';
  import { get } from 'svelte/store';
  import { Menu, Sun, Moon } from 'lucide-svelte';
  import { DropdownMenu } from 'bits-ui';
  import { getMe, AuthError, type User } from '$lib/api';
  import LogoutButton from '$lib/components/LogoutButton.svelte';
  import { NotificationBell } from '$lib/components/ui';
  import { theme, toggleTheme } from '$lib/stores/theme';
  import { clearNotices } from '$lib/stores/notices';
  import { startNotificationPoll, stopNotificationPoll } from '$lib/stores/notifications';
  import { online, startConnectivity } from '$lib/stores/connectivity';
  import { installViewportGuard } from '$lib/viewport';
  import { installKeyboardDismiss } from '$lib/platform/input';
  import { shellAtLeast } from '$lib/platform/native';
  import {
    canReadCachedUser,
    forgetLastUserId,
    rememberLastUserId,
    seedUserId,
  } from '$lib/offline/lastUser';
  import { readUser, writeUser } from '$lib/offline/db';
  import { setCurrentUser } from '$lib/userContext';
  import '../app.css';

  let { children } = $props();

  let user: User | null = $state(null);
  let loading = $state(true);
  let error = $state('');
  // Whether the identity on screen came from the server *this session*, rather
  // than from the cache or from nowhere at all. What the retry below is trying
  // to reach, and nothing that is painted: a cached user renders exactly as a
  // live one does, because it is the same record.
  let live = $state(false);

  // A notice comments on the surface that raised it, so leaving that surface
  // retires it: an error is pinned until acknowledged, and without this one
  // raised on /chat would sit under the Feeds header describing nothing on
  // screen. The corollary for callers is that a notice meant to survive a
  // navigation has to be raised after it, not before.
  afterNavigate(() => clearNotices());

  // App-wide, because a soft keyboard is raised from every section that has a
  // filter box or a form — not just the chat composer, which is where the first
  // version of this lived.
  onMount(() => installViewportGuard());

  // Same reasoning, same scope: every section has something to type into, and
  // on a touch device the keyboard outstays its welcome in all of them.
  onMount(() => installKeyboardDismiss());

  // One connectivity fact for the whole app (ISSUE-202), anchored here for the
  // same reason the notification poll is: it is read on routes that render no
  // `AppShell`, and the interface listeners must outlive any one page. The
  // requests each page makes are what keep it current; this only adds the
  // window events and the probe schedule.
  onMount(() => startConnectivity());

  // Ask for the origin's storage to be exempt from eviction, once.
  //
  // The offline cache and the outbox being built on top of this live in
  // storage iOS may reclaim: Intelligent Tracking Prevention removes a site's
  // script-writable storage after a period without interaction, it is on in
  // every WKWebView, and there is no way to opt an origin out — `persist()`
  // grants only to origins already on WebKit's exemption list, which this one
  // does not join. So a `false` here is the expected answer on the phone and
  // not a fault. The point of asking is to have observed it rather than
  // assumed it: the answer is what decides whether the outbox eventually has
  // to move into native storage. Logged rather than surfaced, because there is
  // nothing the user could do about it and nothing here fails if it is no.
  onMount(() => {
    if (typeof navigator === 'undefined' || typeof navigator.storage?.persist !== 'function') {
      return;
    }
    navigator.storage
      .persist()
      .then((granted) => console.log(`[istota] storage.persist() → ${granted}`))
      .catch(() => console.log('[istota] storage.persist() → refused'));
  });

  // The offline app shell, in the native app only (ISSUE-202).
  //
  // Without a worker, a cold launch with no connection never gets a document —
  // the WebView is pointed at the deployment, so WebKit paints its own
  // unreachable-server page and none of the offline cache below ever runs.
  // With one, the navigation resolves from cache and the app boots into the
  // banner, the cached transcript and a composer that queues.
  //
  // Gated on the shell for blast radius rather than for taste: this app
  // deploys continuously, and a service worker is the one client artifact that
  // can pin a client to a build the server has deleted. The phone is where the
  // cold launch happens and is also the surface with a native escape hatch, so
  // it is the only place this runs. Kit's own automatic registration is off
  // (`svelte.config.js`) so that this gate is the only way in.
  //
  // On the *version* rather than on the shell alone, per the facade's rule
  // that a shell-dependent capability is gated on the version that introduced
  // it: 0.10.0 is the build that declares the app-bound domains WebKit needs
  // before it will run a worker here at all, and it is also the build whose
  // settings row can unregister one. An older app is expected to have no
  // `navigator.serviceWorker` to register with — but if that expectation is
  // ever wrong, it would install a worker it has no way to remove.
  //
  // Fire-and-forget: a registration that fails leaves the app exactly as it is
  // without one, which is what every other surface already runs.
  onMount(() => {
    if (!shellAtLeast('0.10.0')) return;
    if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return;
    navigator.serviceWorker
      // Kit bundles the worker as a classic script for the build and serves it
      // as an ES module in dev, and registering with the wrong type fails
      // outright.
      .register(`${base}/service-worker.js`, { type: import.meta.env.DEV ? 'module' : 'classic' })
      .catch((e) => console.log(`[istota] service worker not registered: ${e}`));
  });

  // Stale-build prompt. `kit.version.pollInterval` flips `updated.current` when
  // a new build ships, but SvelteKit only acts on it at the *next navigation* —
  // and a chat tab left open for days never navigates. Worse on the iOS
  // home-screen PWA, which caches the app shell hard enough to keep running a
  // bundle the server has deleted; that is how a device ended up polling
  // endpoints that no longer exist while never opening the room stream.
  //
  // The prompt is deliberately a tap rather than an automatic reload: a reload
  // discards a half-typed message, and the client cannot tell whether the
  // composer is mid-thought.
  onMount(() => {
    if (typeof document === 'undefined') return;
    // The poll timer is throttled in a background tab and stopped outright in a
    // suspended PWA, so returning to the app is the moment a check is actually
    // worth something — it is also the moment a stale bundle does its damage.
    const onVisible = () => {
      if (document.visibilityState === 'visible') void updated.check();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => document.removeEventListener('visibilitychange', onVisible);
  });

  /**
   * The last thing the cache can answer with, or null.
   *
   * Asked only about a failure the connectivity store says was a *gap*.
   * `apiFetch` has already told the store which of the two this exception was,
   * so the store is read here rather than the same failure being classified a
   * second time — the read `chat.ts` already makes when its room list fetch
   * fails. A 500 leaves the store online and gets nothing from here.
   *
   * Its own function so its own failure cannot escape into `loadUser`'s catch,
   * where a throw would leave `user`, `error` and `loading` all falsy and the
   * layout rendering none of its three branches.
   */
  async function cachedIdentity(): Promise<User | null> {
    try {
      const seed = get(online) ? null : seedUserId();
      return seed ? await readUser(seed) : null;
    } catch {
      return null;
    }
  }

  /**
   * The run counter `chat.ts` and `notifications.ts` both keep, for the reason
   * they keep it: two loads can be in flight at once — a reconnect while the
   * first is still awaiting — and without this the slower one wins whatever it
   * has, which here means a month-old cached identity painted over the live one
   * the app had just resolved.
   */
  let loadGeneration = 0;

  /**
   * Resolve who is signed in, and paint something either way.
   *
   * This gate decides whether the app renders at all — everything the offline
   * work built sits under `{@render children()}` — so a cold launch with no
   * connection used to end here, with a cached room list, cached transcripts,
   * an offline banner and a send queue all present, correct and unreachable
   * behind "Failed to load user info" (ISSUE-354).
   *
   * Three answers, and keeping them apart is the whole of it. A session that is
   * over ends the session. A server that could not be *reached* falls back to
   * the identity it last confirmed. Anything else — a server that answered and
   * refused, or nothing cached to fall back on — is still the error page, which
   * on a first launch is the honest outcome.
   *
   * Returns whether the *server* answered this run. A page that reaches this
   * through the context's `reload()` needs that answer (ISSUE-355): it is the
   * difference between a record it can trust to be current and one the cache
   * supplied, and only the caller knows which of the two its own screen needs.
   */
  async function loadUser(): Promise<boolean> {
    const mine = ++loadGeneration;
    const current = () => mine === loadGeneration;
    try {
      const fresh = await getMe();
      // Reported as an answer even when a newer load owns the paint: the
      // caller asked whether the server could confirm the account, and it did.
      if (!current()) return true;
      user = fresh;
      live = true;
      // A retry that succeeds clears the message its own first attempt left.
      error = '';
      // The pointer the next cold launch reads its cache by (ISSUE-202).
      // `/chat/config` writes the same value — its `user_id` *is* the username
      // — but this runs on every route, so a session that never opens chat
      // still leaves the shell able to find what it stored.
      rememberLastUserId(fresh.username);
      // Gated where the *read* is gated, unlike the pointer beside it: the
      // pointer is one string with nothing personal in it and is written
      // everywhere so the shell finds one already there, while this record
      // carries the user's inbound address and no browser can either read it
      // back or reach the settings row that clears it.
      if (canReadCachedUser()) void writeUser(fresh.username, fresh);
      // The bell needs a count on every route, and `AppShell` is not on every
      // route — the error page renders none and neither do the money loading
      // branches — so the poll is anchored here rather than to the shell.
      //
      // Started only once the user has resolved: a logged-out route polling an
      // authenticated endpoint fails, backs off, and is then indistinguishable
      // from a real outage at the next login.
      startNotificationPoll();
      return true;
    } catch (e) {
      if (e instanceof AuthError) {
        // The session is over, so the pointer that says whose cache to read
        // before the next one resolves goes with it (ISSUE-202). Whoever signs
        // in next writes their own on the first config read; until then a cold
        // launch with no connection reads nothing, which is the right answer
        // when nobody is signed in.
        //
        // Ahead of every offline branch below, and ahead of the generation
        // check too: a dead session and a dead network are different answers
        // and must stay different, and a 401 is authoritative whoever asked.
        forgetLastUserId();
        window.location.href = `${base}/login`;
        return false;
      }
      if (!current()) return false;
      const cached = await cachedIdentity();
      if (!current()) return false;
      if (cached) {
        // **The cache never paints over an identity already on screen**, and
        // that guard belongs here rather than at the callers. It stops two
        // different things. A `reload()` from a page (ISSUE-355) can now reach
        // this branch while a live record is up: without the guard a single
        // failed refresh would swap it for a stale one and leave `live` true,
        // which is what both retries below are gated on — the app would hold
        // the stale record for the life of the page with nothing left to
        // correct it. And a retry that fails while the cached record is
        // *already* what is shown would otherwise re-assign an equal record
        // from a fresh read, re-running everything derived from it: the
        // dashboard's greeting re-rolls on every backgrounding of an offline
        // app, with nothing having changed.
        if (!user) user = cached;
        // Deliberately no `startNotificationPoll()` and no write back. The poll
        // would fail against the connection that just failed and back off into
        // something indistinguishable from a real outage; re-storing what the
        // cache just answered with would push its own expiry out forever and
        // make the pointer self-confirming. Both happen on the retry below.
        return false;
      }
      // A retry already has an app on screen, and replacing one with an error
      // message is the failure this branch exists to stop.
      if (!user) error = 'Failed to load user info';
      return false;
    } finally {
      if (current()) loading = false;
    }
  }

  /**
   * Published to every route under this one, so a page reads the identity this
   * gate resolved rather than asking the same question again (ISSUE-355). Set
   * at init, which is before any child exists.
   *
   * A getter, not a snapshot: `loadUser` reassigns `user` when a cached
   * identity is upgraded to a live one, and every reader has to see that.
   */
  setCurrentUser({
    // Non-null by construction. `{@render children()}` sits inside the
    // `{:else if user}` branch below, so nothing able to call this exists until
    // the gate has an identity — which is what lets a page read it with no null
    // handling and no wait.
    //
    // A throw rather than a cast, though it is unreachable from every reader
    // today: the context is set at init, when `user` is still null, so a future
    // reader placed above the gate would be handed an `undefined` typed as a
    // `User` and would fail somewhere inside its own template instead of here.
    get user(): User {
      if (!user) throw new Error('current user read before the layout resolved one');
      return user;
    },
    get live(): boolean {
      return live;
    },
    reload: loadUser,
  });

  onMount(() => {
    console.log(`[istota] web ui ${__APP_VERSION__} (built ${__APP_BUILT_AT__})`);
    // Seeded before the load rather than after it. The load awaits `getMe()`
    // and then a first open of IndexedDB, which can spend three seconds, and a
    // connection that returned inside that window would otherwise be read as
    // the state the detector started from and never reported at all.
    let was = get(online);
    void loadUser();
    // Subscribed synchronously and unconditionally, not from inside the load's
    // own `.then()`: a cleanup returned before that promise settles cannot
    // unsubscribe something not yet subscribed, which is the leak the comment
    // below this hook was written about.
    //
    // `live` rather than "booted from the cache" is what it waits on, so the
    // error page retries too. That page is otherwise the one screen with no way
    // out — there is no reload affordance in the shell — and it is where a
    // misread failure lands.
    const unwatch = online.subscribe((reachable) => {
      const returned = reachable && !was;
      was = reachable;
      if (returned && !live) void loadUser();
    });
    // The other signal the connectivity probe and the notification poll both
    // already use: the user coming back to look. A retry that fails against a
    // server that *answered* leaves the store online, so no further edge is
    // coming and without this the app would hold a stale identity, silently,
    // for the life of the page.
    const onReturn = () => {
      if (document.visibilityState === 'visible' && !live) void loadUser();
    };
    document.addEventListener('visibilitychange', onReturn);
    return () => {
      unwatch();
      document.removeEventListener('visibilitychange', onReturn);
    };
  });

  // Its own hook, and synchronous. Svelte only honours a cleanup returned from
  // a *synchronous* `onMount` callback — returning one from the async hook above
  // is silently dropped, which would leave the poll running against a torn-down
  // layout.
  onMount(() => () => stopNotificationPoll());

  function isActive(path: string): boolean {
    const current = page.url.pathname;
    if (path === '/') return current === `${base}` || current === `${base}/`;
    return current.startsWith(`${base}${path}`);
  }

  const pageTitle = $derived.by(() => {
    const path = page.url.pathname.replace(base, '').replace(/^\/+/, '');
    if (!path) return 'Istota';
    const segment = path.split('/')[0];
    return `Istota - ${segment.charAt(0).toUpperCase()}${segment.slice(1)}`;
  });
</script>

<svelte:head>
  <title>{pageTitle}</title>
</svelte:head>

{#if loading}
  <div class="center-msg">Loading…</div>
{:else if error}
  <div class="center-msg error">{error}</div>
{:else if user}
  <nav class="app-nav">
    <a href="{base}/" class="app-name">
      <img class="sigil" src="{base}/octopus-sigil.webp" alt="" width="19" height="20" />
      Istota
    </a>
    <div class="nav-links">
      <a href="{base}/chat" class:active={isActive('/chat')}>Chat</a>
      {#if user.features.briefings}
        <a href="{base}/briefings" class:active={isActive('/briefings')}>Briefings</a>
      {/if}
      {#if user.features.feeds}
        <a href="{base}/feeds" class:active={isActive('/feeds')}>Feeds</a>
      {/if}
      {#if user.features.location}
        <a href="{base}/location" class:active={isActive('/location')}>Location</a>
      {/if}
      {#if user.features.money}
        <a href="{base}/money" class:active={isActive('/money')}>Money</a>
      {/if}
      {#if user.features.health}
        <a href="{base}/health" class:active={isActive('/health')}>Health</a>
      {/if}
      {#if user.features.admin}
        <a href="{base}/admin" class:active={isActive('/admin')}>Admin</a>
      {/if}
    </div>
    <div class="nav-right">
      <a
        href="{base}/settings"
        class="nav-user"
        class:active={isActive('/settings')}
        title="Settings"
      >
        {user.display_name}
      </a>
      <NotificationBell />
      <button
        type="button"
        class="nav-icon-btn theme-btn"
        onclick={toggleTheme}
        title={$theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
        aria-label="Toggle color theme"
      >
        {#if $theme === 'dark'}
          <Sun size={15} />
        {:else}
          <Moon size={15} />
        {/if}
      </button>
      <LogoutButton />
      <DropdownMenu.Root>
        <DropdownMenu.Trigger>
          {#snippet child({ props })}
            <button class="nav-icon-btn hamburger-btn" aria-label="Open menu" {...props}>
              <Menu size={18} />
            </button>
          {/snippet}
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content class="app-nav-menu" align="end" sideOffset={6}>
            <DropdownMenu.Item>
              {#snippet child({ props })}
                <a
                  href="{base}/chat"
                  class="app-nav-menu-link"
                  class:active={isActive('/chat')}
                  {...props}>Chat</a
                >
              {/snippet}
            </DropdownMenu.Item>
            {#if user.features.briefings}
              <DropdownMenu.Item>
                {#snippet child({ props })}
                  <a
                    href="{base}/briefings"
                    class="app-nav-menu-link"
                    class:active={isActive('/briefings')}
                    {...props}>Briefings</a
                  >
                {/snippet}
              </DropdownMenu.Item>
            {/if}
            {#if user.features.feeds}
              <DropdownMenu.Item>
                {#snippet child({ props })}
                  <a
                    href="{base}/feeds"
                    class="app-nav-menu-link"
                    class:active={isActive('/feeds')}
                    {...props}>Feeds</a
                  >
                {/snippet}
              </DropdownMenu.Item>
            {/if}
            {#if user.features.location}
              <DropdownMenu.Item>
                {#snippet child({ props })}
                  <a
                    href="{base}/location"
                    class="app-nav-menu-link"
                    class:active={isActive('/location')}
                    {...props}>Location</a
                  >
                {/snippet}
              </DropdownMenu.Item>
            {/if}
            {#if user.features.money}
              <DropdownMenu.Item>
                {#snippet child({ props })}
                  <a
                    href="{base}/money"
                    class="app-nav-menu-link"
                    class:active={isActive('/money')}
                    {...props}>Money</a
                  >
                {/snippet}
              </DropdownMenu.Item>
            {/if}
            {#if user.features.health}
              <DropdownMenu.Item>
                {#snippet child({ props })}
                  <a
                    href="{base}/health"
                    class="app-nav-menu-link"
                    class:active={isActive('/health')}
                    {...props}>Health</a
                  >
                {/snippet}
              </DropdownMenu.Item>
            {/if}
            {#if user.features.admin}
              <DropdownMenu.Item>
                {#snippet child({ props })}
                  <a
                    href="{base}/admin"
                    class="app-nav-menu-link"
                    class:active={isActive('/admin')}
                    {...props}>Admin</a
                  >
                {/snippet}
              </DropdownMenu.Item>
            {/if}
            <DropdownMenu.Item>
              {#snippet child({ props })}
                <a
                  href="{base}/settings"
                  class="app-nav-menu-link"
                  class:active={isActive('/settings')}
                  {...props}>Settings</a
                >
              {/snippet}
            </DropdownMenu.Item>
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>
    </div>
  </nav>
  <main
    class="app-content"
    class:app-content-fill={isActive('/') ||
      isActive('/chat') ||
      isActive('/location') ||
      isActive('/feeds') ||
      isActive('/money') ||
      isActive('/health') ||
      isActive('/briefings') ||
      isActive('/admin') ||
      isActive('/settings')}
  >
    {@render children()}
  </main>
{/if}

{#if updated.current}
  <!-- Sits outside the {#if user} block: a stale bundle can fail /api/me in
       ways a fresh one would not, and that is exactly when the prompt matters. -->
  <div class="update-toast" role="status">
    <span>A new version is available.</span>
    <button type="button" onclick={() => location.reload()}>Reload</button>
  </div>
{/if}

<style>
  /* Pinned above the safe-area inset so it clears the iOS home indicator —
     this prompt exists chiefly for the home-screen PWA.
     Centred by insetting both edges + `margin-inline: auto` rather than
     `left: 50%` + translate: with only `left` set, a fixed element's
     shrink-to-fit width is capped at the space from the midpoint to the right
     edge (50vw), so the label wrapped mid-sentence on a phone. */
  .update-toast {
    position: fixed;
    left: 1rem;
    right: 1rem;
    bottom: calc(1rem + var(--safe-bottom));
    width: fit-content;
    margin-inline: auto;
    z-index: var(--z-toast);
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-3);
    padding: var(--space-2) var(--space-2) var(--space-2) var(--space-4);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-pill);
    background: var(--surface-raised);
    color: var(--text-primary);
    font-size: var(--text-sm);
    box-shadow: var(--shadow-md);
  }
  /* The row fits on a 320px viewport at the default text scale; at a larger
     one the label still has to wrap, so balance it into even lines rather than
     leaving one orphaned word above the button. */
  .update-toast span {
    text-wrap: balance;
  }
  .update-toast button {
    font: inherit;
    border: none;
    border-radius: var(--radius-pill);
    padding: var(--space-1) var(--space-3);
    background: var(--accent-blue);
    color: var(--on-accent-fg);
    cursor: pointer;
    white-space: nowrap;
  }

  /* Layout, hit area, reset and hover all come from the shared `.nav-icon-btn`
     rule in app.css — these three siblings only differ in resting color and,
     for the hamburger, in when they are shown at all. */
  .theme-btn {
    color: var(--text-dim);
  }

  .hamburger-btn {
    display: none;
    color: var(--text-muted);
  }

  .nav-user.active {
    color: var(--text-primary);
  }

  @media (max-width: 640px) {
    .hamburger-btn {
      display: inline-flex;
    }
    .nav-user {
      display: none;
    }
  }

  :global(.app-nav-menu) {
    min-width: 9rem;
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: var(--space-1);
    z-index: var(--z-popover);
    box-shadow: var(--shadow-md);
    outline: none;
  }

  :global(.app-nav-menu-link) {
    display: block;
    padding: var(--space-2) var(--space-3);
    font-size: var(--text-base);
    color: var(--text-muted);
    text-decoration: none;
    border-radius: var(--radius-sm);
    outline: none;
  }

  :global(.app-nav-menu-link:hover),
  :global(.app-nav-menu-link[data-highlighted]) {
    background: var(--surface-raised);
    color: var(--text-primary);
  }

  :global(.app-nav-menu-link.active) {
    color: var(--text-primary);
  }
</style>
