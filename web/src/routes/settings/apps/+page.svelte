<script lang="ts">
  /* Settings → Apps: for each installed app, what its current version is
     allowed to do, what it asks for and has not been allowed yet, how much it
     stores (its values and its data folder), and Uninstall. The kebab in the
     app view links its Permissions item here. Granting happens where the app
     asks (the approval screen, or the first-open screen); this page shows and
     removes. */
  import { onDestroy, onMount } from 'svelte';
  import { base } from '$app/paths';
  import { createConfirm } from '@minot/ui/ui/confirm.svelte';
  import { latest } from '@minot/ui/ui/latest';
  import { Alert, Badge, Button, Card, Checkbox, ConfirmDialog, EmptyState, Icon } from '@minot/ui';
  import { notifySuccess } from '@minot/ui/stores/notices';
  import { AuthError } from '$lib/api';
  import {
    appPath,
    getApp,
    getApps,
    getAppStorage,
    uninstallApp,
    type AppDetail,
    type AppStorage,
  } from '$lib/apps/api';
  import { appIcon } from '$lib/apps/icons';
  import { formatBytes } from '$lib/format';
  import CapabilityList from '$lib/components/apps/CapabilityList.svelte';
  import { SettingsLayout } from '$lib/components/settings';
  import { getCurrentUser } from '$lib/userContext';

  interface Entry {
    detail: AppDetail;
    storage: AppStorage | null;
  }

  const identity = getCurrentUser();
  const enabled = $derived(!!identity.user.features.apps);

  let entries = $state<Entry[] | null>(null);
  let loading = $state(true);
  let error = $state('');
  let deleteData = $state(false);
  const uninstall = createConfirm<AppDetail>();

  const loads = latest();
  onDestroy(() => loads.dispose());

  async function load() {
    const current = loads.next();
    try {
      const [list, storage] = await Promise.all([getApps(), getAppStorage()]);
      const details = await Promise.all(list.apps.map((app) => getApp(app.app_id)));
      if (!current()) return;
      const byId = new Map(storage.apps.map((row) => [row.app_id, row]));
      entries = details.map((detail) => ({ detail, storage: byId.get(detail.app_id) ?? null }));
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

  function retry() {
    loading = true;
    error = '';
    void load();
  }

  onMount(() => {
    if (enabled) void load();
    else loading = false;
  });

  function ask(detail: AppDetail) {
    deleteData = false;
    uninstall.ask(detail);
  }

  async function confirmUninstall() {
    const detail = uninstall.target;
    if (!detail) return;
    await uninstallApp(detail.app_id, deleteData);
    await load();
    notifySuccess(`${detail.name} was uninstalled.`);
  }
</script>

<SettingsLayout
  description="What each app is allowed to do, and the space it uses."
  {loading}
  error={entries ? error : ''}
  loadError={entries ? '' : error}
  onRetry={retry}
>
  {#if !enabled}
    <Alert variant="info" title="Apps are not turned on">
      Apps are an experimental module, and it is not enabled for your account.
    </Alert>
  {:else if entries && entries.length === 0}
    <EmptyState title="No apps installed" actionLabel="Open Apps" actionHref="{base}/apps/">
      Apps you install show up here with what they are allowed to do.
    </EmptyState>
  {:else if entries}
    {#each entries as { detail, storage } (detail.app_id)}
      <Card title={detail.name} description={detail.description || undefined}>
        {#snippet icon()}<Icon icon={appIcon(detail.icon)} size="md" />{/snippet}
        {#snippet status()}
          {#if detail.origin === 'starter'}
            <Badge size="sm" tone="subtle" uppercase={false}>Shipped with Istota</Badge>
          {/if}
          <Badge size="sm" tone="subtle" uppercase={false}>Version {detail.current_version}</Badge>
        {/snippet}
        {#snippet actions()}
          <div class="hstack wrap">
            <Button
              size="sm"
              variant="secondary"
              href="{base}{appPath(detail.app_id)}versions/"
              ariaLabel="Versions of {detail.name}">Versions</Button
            >
            <Button
              size="sm"
              variant="danger"
              ariaLabel="Uninstall {detail.name}"
              onclick={() => ask(detail)}>Uninstall</Button
            >
          </div>
        {/snippet}
        <h3>Allowed</h3>
        <CapabilityList capabilities={detail.grants} label="Allowed for {detail.name}" />
        {#if detail.ungranted.length}
          <h3>Not allowed yet</h3>
          <CapabilityList
            capabilities={detail.ungranted}
            label="Not allowed yet for {detail.name}"
          />
          <p class="caption">{detail.name} asks for these when you open it.</p>
        {/if}
        <h3>Storage</h3>
        <dl class="kv">
          <dt>Stored values</dt>
          <dd>{storage ? formatBytes(storage.kv_bytes) : '—'}</dd>
          <dt>Data folder</dt>
          <dd>
            {#if storage && storage.data_bytes !== null}
              {formatBytes(storage.data_bytes)}
            {:else}
              Not measured
            {/if}
          </dd>
        </dl>
      </Card>
    {/each}
  {/if}
</SettingsLayout>

<ConfirmDialog
  bind:open={uninstall.open}
  title="Uninstall {uninstall.target?.name ?? 'app'}"
  confirmLabel="Uninstall"
  busyLabel="Uninstalling…"
  confirmVariant="danger"
  onConfirm={confirmUninstall}
>
  {#snippet body()}
    <div class="vstack">
      <p>
        Are you sure you want to uninstall {uninstall.target?.name}? Its versions are removed. The
        app's folder in your files is not touched{#if uninstall.target?.origin === 'starter'}, and
          it stays uninstalled until you reinstall it from All apps{/if}.
      </p>
      <Checkbox
        bind:checked={deleteData}
        label="Also delete its data"
        description="The values it stored and its data folder. Without this they are kept."
      />
    </div>
  {/snippet}
</ConfirmDialog>
