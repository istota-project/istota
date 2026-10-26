<script lang="ts">
  /* Versions: every installed version of one app, newest first, with its
     dates and what it asks for. "Make current" is the rollback: grants are
     stored per version, so the version made current runs with exactly what it
     was approved for. A version that is not current can be deleted; the
     current one cannot (the server refuses it too). */
  import { onDestroy, untrack } from 'svelte';
  import { base } from '$app/paths';
  import { page } from '$app/state';
  import { createConfirm } from '@minot/ui/ui/confirm.svelte';
  import { latest } from '@minot/ui/ui/latest';
  import { Badge, Button, Card, ConfirmDialog, LoadState, PageHeader, Timestamp } from '@minot/ui';
  import { notifyError, notifySuccess } from '@minot/ui/stores/notices';
  import { AuthError } from '$lib/api';
  import {
    AppsApiError,
    appPath,
    deleteVersion,
    getVersions,
    makeCurrent,
    type AppVersion,
    type AppVersions,
  } from '$lib/apps/api';
  import { getAppsList } from '$lib/apps/context';
  import { getCurrentUser } from '$lib/userContext';

  const apps = getAppsList();
  const identity = getCurrentUser();

  const appId = $derived(page.params.id ?? '');
  const name = $derived(apps.list?.apps.find((a) => a.app_id === appId)?.name ?? appId);

  let data = $state<AppVersions | null>(null);
  let loading = $state(true);
  let error = $state('');
  let busy = $state<number | null>(null);
  const remove = createConfirm<AppVersion>();

  const loads = latest();
  onDestroy(() => loads.dispose());

  async function load(id: string) {
    const current = loads.next();
    if (!data) loading = true;
    try {
      const next = await getVersions(id);
      if (!current()) return;
      data = next;
      error = '';
    } catch (e) {
      if (!current()) return;
      if (e instanceof AuthError) {
        identity.expireSession();
        return;
      }
      data = null;
      error =
        e instanceof AppsApiError && e.status === 404
          ? 'This app is not installed.'
          : e instanceof Error && e.message
            ? e.message
            : 'Could not load the versions.';
    } finally {
      if (current()) loading = false;
    }
  }

  $effect(() => {
    const id = appId;
    untrack(() => {
      data = null;
      void load(id);
    });
  });

  $effect(() => {
    apps.setHeader({ title: name });
  });
  onDestroy(() => apps.setHeader(null));

  const versions = $derived([...(data?.versions ?? [])].sort((a, b) => b.version - a.version));

  function failed(e: unknown, fallback: string): string {
    if (e instanceof AuthError) identity.expireSession();
    return e instanceof Error && e.message ? e.message : fallback;
  }

  async function rollback(version: AppVersion) {
    busy = version.version;
    try {
      await makeCurrent(appId, version.version);
      await Promise.all([load(appId), apps.reload()]);
      notifySuccess(`Version ${version.version} of ${name} is current.`);
    } catch (e) {
      notifyError(failed(e, `Could not make version ${version.version} current.`));
    } finally {
      busy = null;
    }
  }

  async function confirmDelete() {
    const version = remove.target;
    if (!version) return;
    await deleteVersion(appId, version.version);
    await load(appId);
    notifySuccess(`Version ${version.version} was deleted.`);
  }

  /** What a version asks for beyond its own data, since every version asks for that. */
  function asked(version: AppVersion): string[] {
    return version.capabilities.filter((cap) => cap !== 'kv:own' && cap !== 'files:own');
  }
</script>

<div class="versions content-frame vstack">
  <PageHeader
    title="Versions"
    back={{ href: `${base}${appPath(appId)}`, label: `Back to ${name}` }}
    align="start"
  />
  <LoadState {loading} {error} onRetry={() => void load(appId)}>
    {#if data}
      <ul class="list-reset vstack">
        {#each versions as version (version.version)}
          {@const extra = asked(version)}
          <li>
            <Card title="Version {version.version}" level={2}>
              {#snippet status()}
                {#if version.current}
                  <Badge size="sm" variant="success" uppercase={false}>Current</Badge>
                {/if}
              {/snippet}
              {#snippet actions()}
                {#if !version.current}
                  <div class="hstack wrap">
                    <Button
                      size="sm"
                      variant="danger"
                      disabled={busy !== null}
                      ariaLabel="Delete version {version.version}"
                      onclick={() => remove.ask(version)}>Delete</Button
                    >
                    <Button
                      size="sm"
                      variant="secondary"
                      loading={busy === version.version}
                      loadingLabel="Switching…"
                      disabled={busy !== null && busy !== version.version}
                      ariaLabel="Make version {version.version} current"
                      onclick={() => rollback(version)}>Make current</Button
                    >
                  </div>
                {/if}
              {/snippet}
              <dl class="kv">
                <dt>Installed</dt>
                <dd><Timestamp value={version.installed_at} format="datetime" /></dd>
                <dt>Approved</dt>
                <dd>
                  <Timestamp
                    value={version.approved_at}
                    format="datetime"
                    empty="Installed with Istota, without an approval"
                  />
                </dd>
                <dt>Asks for</dt>
                <dd>
                  {#if extra.length}
                    <ul class="list-reset hstack wrap">
                      {#each extra as cap (cap)}
                        <li>
                          <code class="code-chip">{cap}</code>
                          {#if !version.grants.includes(cap)}
                            <span class="caption">not allowed</span>
                          {/if}
                        </li>
                      {/each}
                    </ul>
                  {:else}
                    Only its own data and files.
                  {/if}
                </dd>
              </dl>
            </Card>
          </li>
        {/each}
      </ul>
    {/if}
  </LoadState>
</div>

<ConfirmDialog
  bind:open={remove.open}
  title="Delete version {remove.target?.version ?? ''}"
  message="Are you sure you want to delete version {remove.target?.version ??
    ''} of {name}? It can no longer be made current. The app's data is not touched."
  confirmLabel="Delete"
  busyLabel="Deleting…"
  confirmVariant="danger"
  onConfirm={confirmDelete}
/>

<style>
  .versions {
    padding: var(--space-4);
  }
</style>
