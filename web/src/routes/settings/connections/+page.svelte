<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    getSettingsServices,
    type ServiceCard as ServiceCardData,
    type NextcloudTokenStatus,
  } from '$lib/api';
  import { EmptyState, LoadState } from '$lib/components/ui';
  import { getCurrentUser } from '$lib/userContext';
  import {
    ServiceCard,
    GarminCard,
    GoogleWorkspaceCard,
    NextcloudCard,
    SettingsLayout,
  } from '$lib/components/settings';

  const identity = getCurrentUser();

  let services: ServiceCardData[] = $state([]);
  let loading = $state(true);
  let loaded = $state(false);
  let refreshing = $state(false);
  let error = $state('');
  // Set by a disconnect made here, which the identity record does not learn
  // about until the next `/me`. Otherwise the record is the answer — the
  // settings layout re-resolves it on entry (ISSUE-355), so a connect made
  // elsewhere is already reflected. null = the operator has not enabled
  // encrypted token storage, so there is no card.
  let ncTokenOverride: NextcloudTokenStatus | undefined = $state(undefined);
  const ncToken = $derived(ncTokenOverride ?? identity.user.nextcloud_token ?? null);

  async function loadServices() {
    // Cleared first so a repeated failure is a change LoadState reports again.
    error = '';
    refreshing = true;
    try {
      services = (await getSettingsServices()).services;
      loaded = true;
    } catch (e) {
      error = (e as Error).message || 'Failed to load services';
    } finally {
      loading = false;
      refreshing = false;
    }
  }

  onMount(loadServices);

  async function retryPane() {
    loading = true;
    await loadServices();
  }

  // /settings/services already filters to connected services (no module-owned
  // monarch/feeds/overland leak through). Skip cards whose status is
  // "unavailable" — historically used to mean "no resource declaration" but
  // now only OAuth services with the global flag off can land there.
  //
  // OAuth cards sort to the top so they sit with the Nextcloud card rendered
  // above this list, which is a connect flow too — the account connections
  // group together instead of being split by the credential-field cards. The
  // sort is stable, so everything else keeps the API's order, and it runs on
  // filter()'s fresh array rather than mutating `services`.
  let activeServices = $derived(
    services
      .filter((s) => s.status !== 'unavailable')
      .sort((a, b) => Number(b.oauth ?? false) - Number(a.oauth ?? false)),
  );
</script>

<!-- A failed list takes the pane only when there is nothing else to show. The
     Nextcloud card draws from the identity, not from this list, so with it the
     failure sits where the list goes and leaves that card usable (ISSUE-801). -->
<SettingsLayout {loading} loadError={!ncToken && !loaded ? error : ''} onRetry={retryPane}>
  <p class="description intro">
    Accounts and per-service credentials for skills that need them. Values are encrypted at rest and
    never sent back to the browser — secret fields are write-only. Module-specific credentials live
    on their own settings pages (<a href="{base}/feeds/settings">feeds</a>,
    <a href="{base}/money/settings">money</a>,
    <a href="{base}/location/settings">location</a>).
  </p>

  {#if ncToken}
    <NextcloudCard
      token={ncToken}
      username={identity.user.username}
      onDisconnected={() => (ncTokenOverride = { connected: false, expires_at: null })}
    />
  {/if}

  <LoadState
    scope="card"
    loading={refreshing}
    {error}
    hasData={loaded}
    onRetry={() => void loadServices()}
  >
    {#each activeServices as svc (svc.service)}
      {#if svc.custom_ui && svc.service === 'garmin'}
        <GarminCard />
      {:else if svc.custom_ui && svc.service === 'google_workspace'}
        <GoogleWorkspaceCard onChanged={loadServices} />
      {:else}
        <ServiceCard service={svc} onChanged={loadServices} />
      {/if}
    {/each}

    {#if loaded && !ncToken && activeServices.length === 0}
      <EmptyState>No connected services are available on this deployment.</EmptyState>
    {/if}
  </LoadState>
</SettingsLayout>

<style>
  .intro {
    margin: 0;
  }
</style>
