<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { base } from '$app/paths';
  import { page } from '$app/state';
  import { CircleArrowUp, LayoutGrid } from '@lucide/svelte';
  import { createDrawerClose } from '@minot/ui/ui/drawerClose';
  import { latest } from '@minot/ui/ui/latest';
  import {
    AppShell,
    Badge,
    Icon,
    ListRow,
    ShellHeader,
    Sidebar,
    SidebarNav,
    SidebarToggle,
  } from '@minot/ui';
  import { AuthError } from '$lib/api';
  import { appPath, getApps, type AppsList } from '$lib/apps/api';
  import { setAppsList, type AppsHeader } from '$lib/apps/context';
  import { fitsRange, observeWidth } from '$lib/apps/display';
  import { appIcon } from '$lib/apps/icons';
  import { getCurrentUser } from '$lib/userContext';

  let { children } = $props();

  const identity = getCurrentUser();
  let list = $state<AppsList | null>(null);
  let loading = $state(true);
  let error = $state('');
  let sidebarOpen = $state(false);
  let pageHeader = $state<AppsHeader | null>(null);
  let paneWidth = $state<number | null>(null);
  let pane: HTMLDivElement;
  onMount(() => observeWidth(pane, (width) => (paneWidth = width)));
  const drawer = createDrawerClose(
    () => sidebarOpen,
    () => (sidebarOpen = false),
  );
  onDestroy(drawer.cancel);

  const loads = latest();
  onDestroy(() => loads.dispose());

  async function reload() {
    const current = loads.next();
    // A refresh keeps the list on screen; only the first load shows the loader.
    if (!list) loading = true;
    try {
      const next = await getApps();
      if (!current()) return;
      list = next;
      error = '';
    } catch (e) {
      if (!current()) return;
      if (e instanceof AuthError) {
        identity.expireSession();
        return;
      }
      error = e instanceof Error && e.message ? e.message : 'Could not load your apps.';
    } finally {
      if (current()) loading = false;
    }
  }

  setAppsList({
    get list() {
      return list;
    },
    get loading() {
      return loading;
    },
    get error() {
      return error;
    },
    get paneWidth() {
      return paneWidth;
    },
    reload,
    setHeader(next) {
      pageHeader = next;
    },
  });

  onMount(() => void reload());

  const indexPath = `${base}/apps/`;
  const onIndex = $derived(page.url.pathname === indexPath || page.url.pathname === `${base}/apps`);

  function appActive(appId: string): boolean {
    return !onIndex && page.url.searchParams.get('id') === appId;
  }

  function follow(event: MouseEvent, href: string) {
    drawer.link(event, () => void goto(href));
  }
</script>

<AppShell>
  {#snippet header()}
    <ShellHeader
      title={pageHeader?.title ?? 'Apps'}
      onTitleClick={() => (sidebarOpen = !sidebarOpen)}
      titleActionLabel="Toggle apps list"
    >
      {#snippet leading()}
        <SidebarToggle
          open={sidebarOpen}
          label="Toggle apps list"
          onclick={() => (sidebarOpen = !sidebarOpen)}
        />
      {/snippet}
      {#snippet tools()}
        {#if pageHeader?.tools}{@render pageHeader.tools()}{/if}
      {/snippet}
    </ShellHeader>
  {/snippet}

  {#snippet sidebar()}
    <Sidebar
      title="Apps"
      count={list?.apps.length}
      open={sidebarOpen}
      onClose={() => (sidebarOpen = false)}
    >
      <SidebarNav
        items={[{ href: indexPath, label: 'All apps', icon: LayoutGrid, active: onIndex }]}
        label="Apps sections"
        divider={!!list?.apps.length}
        onnavigate={(item, event) => follow(event, item.href)}
      />
      {#each list?.apps ?? [] as app (app.app_id)}
        {@const href = `${base}${appPath(app.app_id)}`}
        {#snippet rowIcon()}<Icon icon={appIcon(app.icon)} size="sm" />{/snippet}
        {#snippet waitingMark()}
          {#if app.pending_id}
            <Icon icon={CircleArrowUp} size="xs" label="An install is waiting" />
          {/if}
        {/snippet}
        {#snippet widerBadge()}
          {#if !fitsRange(app.narrowest, paneWidth)}
            <Badge size="sm" tone="subtle" uppercase={false}>Wider screen</Badge>
          {/if}
        {/snippet}
        <ListRow
          {href}
          active={appActive(app.app_id)}
          icon={rowIcon}
          mark={waitingMark}
          badge={widerBadge}
          onclick={(event) => follow(event, href)}>{app.name}</ListRow
        >
      {/each}
    </Sidebar>
  {/snippet}

  <!-- The pane the section's pages render in, measured for the display ranges,
       and a flex column so the app view's frame can fill it. -->
  <div class="apps-pane" bind:this={pane}>
    {@render children()}
  </div>
</AppShell>

<style>
  .apps-pane {
    flex: 1 0 auto;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
</style>
