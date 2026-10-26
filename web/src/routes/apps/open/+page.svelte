<script lang="ts">
  /* The app view: one installed app filling the section's pane, in its frame.

     Before the frame loads, three things can stand in for it: the offline
     state (the frame needs the server to load and to answer), the display
     range's "made for a wider screen" with Open anyway, and the first-open
     capability screen for a starter app that asks for something not granted
     yet (`FirstOpenPermissions`). Once the frame is running
     it is never torn down by the pane shrinking or the connection dropping,
     since that would lose what the user has not saved; an `Alert` says so
     instead, and a call made offline fails in the frame with `offline`.

     The app bar's title is the app's name and its tools are this page's kebab,
     handed to the layout through the section's context. */
  import { onDestroy, untrack } from 'svelte';
  import { goto } from '$app/navigation';
  import { base } from '$app/paths';
  import { page } from '$app/state';
  import { latest } from '@minot/ui/ui/latest';
  import {
    Alert,
    Badge,
    Button,
    Checkbox,
    ConfirmDialog,
    EmptyState,
    KebabMenu,
    LoadState,
    type KebabItem,
  } from '@minot/ui';
  import { notifySuccess } from '@minot/ui/stores/notices';
  import { AuthError } from '$lib/api';
  import {
    AppsApiError,
    activityPath,
    getApp,
    installPath,
    uninstallApp,
    versionsPath,
    type AppDetail,
    type BridgeErrorCode,
  } from '$lib/apps/api';
  import { getAppsList } from '$lib/apps/context';
  import { fitsRange, rangeLabel } from '$lib/apps/display';
  import AppFrame from '$lib/components/apps/AppFrame.svelte';
  import FirstOpenPermissions from '$lib/components/apps/FirstOpenPermissions.svelte';
  import { online } from '$lib/stores/connectivity';
  import { getCurrentUser } from '$lib/userContext';

  const apps = getAppsList();
  const identity = getCurrentUser();

  const appId = $derived(page.url.searchParams.get('id') ?? '');

  let detail = $state<AppDetail | null>(null);
  let loading = $state(true);
  let error = $state('');
  /** Bumped by Reload: a new frame, a new bridge, a fresh load count. */
  let frameKey = $state(0);
  /** The frame has been shown for this app and load; nothing below takes it away. */
  let started = $state(false);
  let openAnyway = $state(false);
  let navigated = $state(false);
  /** "Not now" on the first-open screen: this open goes ahead with what is granted. */
  let notNow = $state(false);
  let uninstallOpen = $state(false);
  let deleteData = $state(false);

  const loads = latest();
  onDestroy(() => loads.dispose());

  async function load(id: string) {
    const current = loads.next();
    // Another app's detail would start its frame under this app's id.
    if (detail && detail.app_id !== id) detail = null;
    loading = !detail;
    try {
      const next = await getApp(id);
      if (!current()) return;
      detail = next;
      error = '';
    } catch (e) {
      if (!current()) return;
      if (e instanceof AuthError) {
        identity.expireSession();
        return;
      }
      detail = null;
      error =
        e instanceof AppsApiError && e.status === 404
          ? 'This app is not installed.'
          : e instanceof Error && e.message
            ? e.message
            : 'Could not load this app.';
    } finally {
      if (current()) loading = false;
    }
  }

  // A different app in the same view starts from nothing.
  $effect(() => {
    const id = appId;
    started = false;
    openAnyway = false;
    navigated = false;
    notNow = false;
    untrack(() => void load(id));
  });

  const fits = $derived(!detail || fitsRange(detail.narrowest, apps.paneWidth));
  /* A starter app is installed without an approval, so what it asks for
     beyond its own data is asked on open, before the frame loads. An app
     installed through the approval screen was granted everything it asked for
     there, so it never lands here. */
  const asking = $derived(
    !!detail && detail.origin === 'starter' && detail.ungranted.length > 0 && !notNow,
  );
  const ready = $derived(!!detail && !navigated && !asking && (fits || openAnyway) && $online);

  $effect(() => {
    if (ready) started = true;
  });

  async function reloadApp() {
    navigated = false;
    started = false;
    await load(appId);
    frameKey += 1;
  }

  function onNavigated() {
    navigated = true;
  }

  function onCallFailed(code: BridgeErrorCode) {
    // Uninstalled while open: the app has nothing left to talk to.
    if (code === 'not_found') {
      void apps.reload();
      void goto(`${base}/apps/`);
    }
  }

  function onCallThrew(e: unknown) {
    if (e instanceof AuthError) identity.expireSession();
  }

  async function confirmUninstall() {
    if (!detail) return;
    const name = detail.name;
    await uninstallApp(detail.app_id, deleteData);
    await apps.reload();
    await goto(`${base}/apps/`);
    notifySuccess(`${name} was uninstalled.`);
  }

  const kebabItems = $derived.by((): KebabItem[] => {
    if (!detail) return [];
    const id = detail.app_id;
    const items: KebabItem[] = [];
    if (detail.pending_id) {
      items.push({ label: 'Review update', href: `${base}${installPath(id, detail.pending_id)}` });
    }
    items.push(
      { label: 'Permissions', href: `${base}/settings/apps/` },
      { label: 'Versions', href: `${base}${versionsPath(id)}` },
      { label: 'Activity', href: `${base}${activityPath(id)}` },
      { label: 'Reload', onSelect: () => void reloadApp() },
      {
        label: 'Uninstall',
        danger: true,
        onSelect: () => {
          deleteData = false;
          uninstallOpen = true;
        },
      },
    );
    return items;
  });

  const listedName = $derived(apps.list?.apps.find((a) => a.app_id === appId)?.name);

  $effect(() => {
    apps.setHeader({ title: detail?.name ?? listedName ?? 'Apps', tools: headerTools });
  });
  onDestroy(() => apps.setHeader(null));
</script>

{#snippet headerTools()}
  {#if detail}
    {#if detail.pending_id}
      <Badge size="sm" variant="info" uppercase={false}>Update waiting</Badge>
    {/if}
    <KebabMenu items={kebabItems} ariaLabel="{detail.name} actions" />
  {/if}
{/snippet}

<LoadState {loading} {error} onRetry={() => void load(appId)}>
  {#if detail}
    {#if navigated}
      <div class="app-view-notice">
        <Alert variant="danger" title="This app tried to leave its page" role="alert">
          It loaded a page other than its own, so it has been closed. Anything it had on screen may
          have gone with that page. This is recorded in the app's activity.
          {#snippet actions()}
            <Button size="sm" variant="secondary" onclick={() => void reloadApp()}>Reload</Button>
          {/snippet}
        </Alert>
      </div>
    {:else if started}
      <div class="app-view">
        {#if !fits}
          <div class="app-view-notice">
            <Alert variant="warn" title="This app may not fit">
              {detail.name} is made for {rangeLabel(detail.narrowest)}, and this pane is narrower.
            </Alert>
          </div>
        {/if}
        {#key `${frameKey}:${detail.app_id}:${detail.current_version}`}
          <AppFrame
            appId={detail.app_id}
            version={detail.current_version}
            name={detail.name}
            {onNavigated}
            {onCallFailed}
            {onCallThrew}
          />
        {/key}
      </div>
    {:else if !$online}
      <EmptyState title="You are offline">
        {detail.name} opens when the connection is back.
      </EmptyState>
    {:else if asking}
      {#key detail.app_id}
        <FirstOpenPermissions
          {detail}
          onGranted={() => void load(appId)}
          onNotNow={() => (notNow = true)}
          onExpired={() => identity.expireSession()}
        />
      {/key}
    {:else if !fits}
      <EmptyState
        title="Made for a wider screen"
        actionLabel="Open anyway"
        onaction={() => (openAnyway = true)}
      >
        {detail.name} is made for {rangeLabel(detail.narrowest)}.
        {#if detail.reason}{detail.reason}{/if}
      </EmptyState>
    {/if}
  {/if}
</LoadState>

<ConfirmDialog
  bind:open={uninstallOpen}
  title="Uninstall {detail?.name ?? 'app'}"
  confirmLabel="Uninstall"
  busyLabel="Uninstalling…"
  confirmVariant="danger"
  onConfirm={confirmUninstall}
>
  {#snippet body()}
    <div class="vstack">
      <p>
        Are you sure you want to uninstall {detail?.name}? Its versions are removed. The app's
        folder in your files is not touched{#if detail?.origin === 'starter'}, and it stays
          uninstalled until you reinstall it from All apps{/if}.
      </p>
      <Checkbox
        bind:checked={deleteData}
        label="Also delete its data"
        description="The values it stored and its data folder. Without this they are kept."
      />
    </div>
  {/snippet}
</ConfirmDialog>

<style>
  .app-view {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }

  .app-view-notice {
    padding: var(--space-3);
  }
</style>
