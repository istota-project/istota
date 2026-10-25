<script lang="ts">
  /* "All apps": everything the Apps section knows about, in four groups. What
     is installed, what is waiting for the user's approval, the drafts in the
     workspace that are new or changed, and the apps that ship with Istota the
     user has uninstalled. The list itself is the layout's (`getAppsList`), so
     the sidebar and this page never disagree. */
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { base } from '$app/paths';
  import {
    Alert,
    Badge,
    Button,
    Card,
    CardGrid,
    EmptyState,
    LoadState,
    Timestamp,
  } from '@minot/ui';
  import { notifyError } from '@minot/ui/stores/notices';
  import { AuthError } from '$lib/api';
  import {
    AppsApiError,
    createPending,
    installPath,
    reinstallStarter,
    type DraftSummary,
  } from '$lib/apps/api';
  import { getAppsList } from '$lib/apps/context';
  import { formatBytes } from '$lib/format';
  import AppCard from '$lib/components/apps/AppCard.svelte';
  import { getCurrentUser } from '$lib/userContext';

  const apps = getAppsList();
  const identity = getCurrentUser();
  const user = $derived(identity.user);
  const list = $derived(apps.list);

  // The layout persists across the section, so coming back here from an app
  // (after an install or an uninstall) asks for the list again.
  onMount(() => {
    if (apps.list) void apps.reload();
  });

  /** Validation errors per draft, shown on that draft's card until the next try. */
  let draftErrors = $state<Record<string, string[]>>({});
  let busy = $state<string | null>(null);

  const starterShown = $derived(
    (list?.starter ?? []).filter(
      (entry) =>
        entry.state === 'dismissed' ||
        entry.state === 'conflict' ||
        (entry.state === 'unavailable' && list?.kit_built),
    ),
  );
  const nothing = $derived(
    !!list &&
      list.apps.length === 0 &&
      list.pending.length === 0 &&
      list.drafts.length === 0 &&
      starterShown.length === 0,
  );

  /** A starter app's name before it is installed: its id, as words. */
  function starterName(appId: string): string {
    const words = appId.replace(/-/g, ' ');
    return words.charAt(0).toUpperCase() + words.slice(1);
  }

  function draftName(draft: DraftSummary): string {
    return draft.name || draft.app_id;
  }

  function failed(e: unknown, fallback: string): string {
    if (e instanceof AuthError) identity.expireSession();
    return e instanceof Error && e.message ? e.message : fallback;
  }

  async function reviewInstall(draft: DraftSummary) {
    busy = `draft:${draft.app_id}`;
    const kept = { ...draftErrors };
    delete kept[draft.app_id];
    draftErrors = kept;
    try {
      const pending = await createPending(draft.app_id);
      await goto(`${base}${installPath(pending.app_id, pending.pending_id)}`);
    } catch (e) {
      if (e instanceof AppsApiError && e.status === 422 && e.errors.length) {
        draftErrors = { ...draftErrors, [draft.app_id]: e.errors };
      } else {
        notifyError(failed(e, `Could not build ${draftName(draft)}.`));
        // A 503 means the kit went missing; the list says so once reloaded.
        if (e instanceof AppsApiError && e.status === 503) void apps.reload();
      }
    } finally {
      busy = null;
    }
  }

  async function reinstall(appId: string) {
    busy = `starter:${appId}`;
    try {
      await reinstallStarter(appId);
      await apps.reload();
    } catch (e) {
      notifyError(failed(e, `Could not reinstall ${starterName(appId)}.`));
    } finally {
      busy = null;
    }
  }
</script>

<LoadState loading={apps.loading} error={apps.error} onRetry={() => apps.reload()}>
  {#if list}
    <div class="card-pane vstack" style:--vstack-gap="var(--space-6)">
      {#if !list.kit_built}
        <Alert variant="warn" title="The apps kit is not built">
          Apps you have installed keep working. Installing a new app, and the apps that ship with
          Istota, wait until the web build has been run on this server.
        </Alert>
      {/if}

      {#if nothing}
        <EmptyState title="No apps yet">
          An app is a folder in the <code>apps</code> folder of your {user.bot_name} folder in Files.
          Once one is there it shows up here, ready to review and install.
        </EmptyState>
      {/if}

      {#if list.apps.length}
        <section class="vstack" aria-labelledby="apps-installed">
          <h2 id="apps-installed" class="title-section">Installed</h2>
          <CardGrid>
            {#each list.apps as app (app.app_id)}
              <AppCard {app} paneWidth={apps.paneWidth} />
            {/each}
          </CardGrid>
        </section>
      {/if}

      {#if list.pending.length}
        <section class="vstack" aria-labelledby="apps-pending">
          <h2 id="apps-pending" class="title-section">Waiting for review</h2>
          <CardGrid>
            {#each list.pending as pending (pending.pending_id)}
              <Card title={pending.name} level={3}>
                {#snippet actions()}
                  <Button
                    size="sm"
                    variant="primary"
                    href="{base}{installPath(pending.app_id, pending.pending_id)}"
                    ariaLabel="Review {pending.name}">Review</Button
                  >
                {/snippet}
                <p class="caption">
                  {formatBytes(pending.size)} · built <Timestamp
                    value={pending.created_at}
                    format="relative"
                  />
                </p>
              </Card>
            {/each}
          </CardGrid>
        </section>
      {/if}

      {#if list.drafts.length}
        <section class="vstack" aria-labelledby="apps-drafts">
          <h2 id="apps-drafts" class="title-section">Drafts</h2>
          <CardGrid>
            {#each list.drafts as draft (draft.app_id)}
              {@const errors = draftErrors[draft.app_id]}
              <Card title={draftName(draft)} level={3}>
                {#snippet status()}
                  <Badge size="sm" tone="subtle" uppercase={false}
                    >{draft.state === 'new' ? 'New' : 'Changed'}</Badge
                  >
                {/snippet}
                {#snippet actions()}
                  <Button
                    size="sm"
                    variant="secondary"
                    loading={busy === `draft:${draft.app_id}`}
                    loadingLabel="Building…"
                    disabled={!!busy && busy !== `draft:${draft.app_id}`}
                    ariaLabel="Review install of {draftName(draft)}"
                    onclick={() => reviewInstall(draft)}>Review install</Button
                  >
                {/snippet}
                <p class="caption">
                  <span class="mono">apps/{draft.app_id}/</span>
                  {#if !draft.has_manifest}· no manifest.toml yet{/if}
                </p>
                {#if errors}
                  <Alert appearance="inline" variant="danger" role="alert">
                    <ul class="list-reset">
                      {#each errors as message (message)}<li>{message}</li>{/each}
                    </ul>
                  </Alert>
                {/if}
              </Card>
            {/each}
          </CardGrid>
        </section>
      {/if}

      {#if starterShown.length}
        <section class="vstack" aria-labelledby="apps-starter">
          <h2 id="apps-starter" class="title-section">Shipped with Istota</h2>
          <CardGrid>
            {#each starterShown as entry (entry.app_id)}
              <Card title={starterName(entry.app_id)} level={3}>
                {#snippet actions()}
                  {#if entry.state !== 'conflict'}
                    <Button
                      size="sm"
                      variant="secondary"
                      loading={busy === `starter:${entry.app_id}`}
                      loadingLabel="Installing…"
                      disabled={!!busy && busy !== `starter:${entry.app_id}`}
                      ariaLabel="{entry.state === 'dismissed'
                        ? 'Reinstall'
                        : 'Install'} {starterName(entry.app_id)}"
                      onclick={() => reinstall(entry.app_id)}
                      >{entry.state === 'dismissed' ? 'Reinstall' : 'Install'}</Button
                    >
                  {/if}
                {/snippet}
                <p class="caption">
                  {#if entry.state === 'dismissed'}
                    You uninstalled this app.
                  {:else if entry.state === 'conflict'}
                    An app you installed uses the id {entry.app_id}, so this one is not installed.
                  {:else}
                    Not installed yet.
                  {/if}
                </p>
              </Card>
            {/each}
          </CardGrid>
        </section>
      {/if}
    </div>
  {/if}
</LoadState>
