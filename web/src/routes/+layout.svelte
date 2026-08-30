<script lang="ts">
  import { base } from '$app/paths';
  import { afterNavigate } from '$app/navigation';
  import { page, updated } from '$app/state';
  import { onMount } from 'svelte';
  import { Menu, Sun, Moon } from 'lucide-svelte';
  import { DropdownMenu } from 'bits-ui';
  import { getMe, AuthError, type User } from '$lib/api';
  import LogoutButton from '$lib/components/LogoutButton.svelte';
  import { NotificationBell } from '$lib/components/ui';
  import { theme, toggleTheme } from '$lib/stores/theme';
  import { clearNotices } from '$lib/stores/notices';
  import { startNotificationPoll, stopNotificationPoll } from '$lib/stores/notifications';
  import { startConnectivity } from '$lib/stores/connectivity';
  import { installViewportGuard } from '$lib/viewport';
  import { installKeyboardDismiss } from '$lib/platform/input';
  import '../app.css';

  let { children } = $props();

  let user: User | null = $state(null);
  let loading = $state(true);
  let error = $state('');

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

  onMount(async () => {
    console.log(`[istota] web ui ${__APP_VERSION__} (built ${__APP_BUILT_AT__})`);
    try {
      user = await getMe();
      // The bell needs a count on every route, and `AppShell` is not on every
      // route — the error page renders none and neither do the money loading
      // branches — so the poll is anchored here rather than to the shell.
      //
      // Started only once the user has resolved: a logged-out route polling an
      // authenticated endpoint fails, backs off, and is then indistinguishable
      // from a real outage at the next login.
      startNotificationPoll();
    } catch (e) {
      if (e instanceof AuthError) {
        window.location.href = `${base}/login`;
        return;
      }
      error = 'Failed to load user info';
    } finally {
      loading = false;
    }
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
  <div class="error-msg">{error}</div>
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
