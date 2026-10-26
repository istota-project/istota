<script lang="ts">
  /* The approval screen for one pending install: what the user consents to
     before it becomes the app's next version. It lives in the web UI and not
     in chat because chat text can be written by the model.

     One column at every width, in the spec's order: the capabilities with the
     new ones marked, the display range, the size, the validator's warnings,
     and last the source against the current version in a `Disclosure`. A
     version asking for nothing new is approved with one button at the top. One
     that asks for more shows its button only after the capability list, and
     the approval echoes back the list of new capabilities this screen showed,
     so the server refuses an approval made on a stale screen (409 `stale`),
     after which the screen loads again. */
  import { onDestroy, untrack } from 'svelte';
  import { goto } from '$app/navigation';
  import { base } from '$app/paths';
  import { page } from '$app/state';
  import { latest } from '@minot/ui/ui/latest';
  import { Alert, Badge, Button, Card, Disclosure, LoadState } from '@minot/ui';
  import { notifyError, notifySuccess, notifyWarning } from '@minot/ui/stores/notices';
  import { AuthError } from '$lib/api';
  import {
    AppsApiError,
    appPath,
    approvePending,
    deletePending,
    getPending,
    type PendingInstall,
  } from '$lib/apps/api';
  import { getAppsList } from '$lib/apps/context';
  import { rangeLabel } from '$lib/apps/display';
  import { formatBytes } from '$lib/format';
  import CapabilityList from '$lib/components/apps/CapabilityList.svelte';
  import SourceDiff from '$lib/components/apps/SourceDiff.svelte';
  import { getCurrentUser } from '$lib/userContext';

  const apps = getAppsList();
  const identity = getCurrentUser();

  const appId = $derived(page.url.searchParams.get('id') ?? '');
  const pendingId = $derived(page.url.searchParams.get('pending') ?? '');

  let data = $state<PendingInstall | null>(null);
  let loading = $state(true);
  let error = $state('');
  let busy = $state<'approve' | 'discard' | null>(null);

  const loads = latest();
  onDestroy(() => loads.dispose());

  async function load(id: string, pid: string) {
    const current = loads.next();
    if (!data) loading = true;
    try {
      const next = await getPending(id, pid);
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
      error = e instanceof Error && e.message ? e.message : 'Could not load this install.';
    } finally {
      if (current()) loading = false;
    }
  }

  $effect(() => {
    const id = appId;
    const pid = pendingId;
    untrack(() => {
      data = null;
      void load(id, pid);
    });
  });

  const name = $derived(data?.manifest.name ?? '');
  const firstInstall = $derived(!!data && data.current_version === null);
  const verb = $derived(firstInstall ? 'Install' : 'Update');
  const changedFiles = $derived(data?.diff.filter((f) => f.status !== 'unchanged').length ?? 0);

  $effect(() => {
    apps.setHeader({ title: name || 'Review install' });
  });
  onDestroy(() => apps.setHeader(null));

  function message(e: unknown, fallback: string): string {
    return e instanceof Error && e.message ? e.message : fallback;
  }

  async function approve() {
    if (!data || busy) return;
    const shown = data;
    busy = 'approve';
    try {
      await approvePending(shown.app_id, shown.pending_id, shown.new_capabilities);
      await apps.reload();
      await goto(`${base}${appPath(shown.app_id)}`);
      notifySuccess(firstInstall ? `${name} is installed.` : `${name} is updated.`);
    } catch (e) {
      if (e instanceof AuthError) {
        identity.expireSession();
      } else if (e instanceof AppsApiError && e.status === 409) {
        notifyWarning(
          'What this install asks for changed while the screen was open. Review it again.',
        );
        await load(shown.app_id, shown.pending_id);
      } else if (e instanceof AppsApiError && e.status === 404) {
        data = null;
        error = message(e, 'This install is no longer waiting.');
      } else {
        notifyError(message(e, `Could not approve ${name}.`));
      }
    } finally {
      busy = null;
    }
  }

  async function discard() {
    if (!data || busy) return;
    const shown = data;
    busy = 'discard';
    try {
      await deletePending(shown.app_id, shown.pending_id);
    } catch (e) {
      if (e instanceof AuthError) {
        identity.expireSession();
        busy = null;
        return;
      }
      // Gone already is what Discard asked for.
      if (!(e instanceof AppsApiError && e.status === 404)) {
        notifyError(message(e, `Could not discard ${name}.`));
        busy = null;
        return;
      }
    }
    await apps.reload();
    await goto(`${base}/apps/`);
    busy = null;
  }
</script>

{#snippet approveButton(label: string)}
  <Button
    variant="primary"
    loading={busy === 'approve'}
    loadingLabel="Approving…"
    disabled={busy === 'discard'}
    onclick={approve}>{label}</Button
  >
{/snippet}

<LoadState {loading} {error} onRetry={() => void load(appId, pendingId)}>
  {#if data}
    <div class="install content-frame vstack">
      <Card
        title="{verb} {name}"
        description={firstInstall
          ? 'A new app, built from its folder in your files.'
          : `Version ${(data.current_version ?? 0) + 1}, built from its folder in your files.`}
      >
        {#snippet status()}
          {#if data?.origin === 'starter'}
            <Badge size="sm" tone="subtle" uppercase={false}>Shipped with Istota</Badge>
          {/if}
        {/snippet}
        {#snippet actions()}
          <div class="hstack wrap">
            <Button
              variant="secondary"
              loading={busy === 'discard'}
              loadingLabel="Discarding…"
              disabled={busy === 'approve'}
              onclick={discard}>Discard</Button
            >
            {#if data?.one_button}{@render approveButton(verb)}{/if}
          </div>
        {/snippet}
        {#if data.one_button}
          <p class="muted">
            {firstInstall
              ? 'It asks for nothing beyond keeping its own data and files.'
              : 'It asks for nothing the version you have now was not allowed.'}
          </p>
        {:else}
          <p class="muted">
            It asks for {data.new_capabilities.length === 1
              ? 'one permission'
              : `${data.new_capabilities.length} permissions`}
            {firstInstall
              ? 'beyond its own data and files'
              : 'the version you have now does not have'}. Read them below before you allow them.
          </p>
        {/if}
      </Card>

      <Card title="Permissions">
        <CapabilityList capabilities={data.capabilities} />
        {#snippet footer()}
          {#if !data?.one_button}{@render approveButton(`Allow and ${verb.toLowerCase()}`)}{/if}
        {/snippet}
      </Card>

      <Card title="Details">
        <dl class="kv">
          <dt>Display</dt>
          <dd>
            {#if data.narrowest === 'phone'}
              Works on a phone.
            {:else}
              Made for {rangeLabel(data.narrowest)}.
            {/if}
            {#if data.narrowest !== 'phone' && data.reason}
              <p class="caption display-reason">{data.reason}</p>
            {/if}
          </dd>
          <dt>Size</dt>
          <dd>{formatBytes(data.size)}</dd>
        </dl>
      </Card>

      {#if data.warnings.length}
        <Alert
          variant="warn"
          title={data.warnings.length === 1 ? 'One warning' : `${data.warnings.length} warnings`}
        >
          <ul class="list-reset vstack" style:--vstack-gap="var(--space-1)">
            {#each data.warnings as warning, i (i)}<li>{warning}</li>{/each}
          </ul>
        </Alert>
      {/if}

      <Disclosure
        variant="card"
        summary={firstInstall
          ? `Source (${data.diff.length === 1 ? '1 file' : `${data.diff.length} files`})`
          : `Source changes (${changedFiles === 1 ? '1 file' : `${changedFiles} files`})`}
      >
        <SourceDiff files={data.diff} />
      </Disclosure>
    </div>
  {/if}
</LoadState>

<style>
  .install {
    padding: var(--space-4);
  }

  .install .display-reason {
    margin: var(--space-1) 0 0;
  }
</style>
