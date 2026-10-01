<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    getSettingsServices,
    disconnectNextcloudToken,
    type ServiceCard as ServiceCardData,
    type NextcloudTokenStatus,
  } from '$lib/api';
  import { Button } from '$lib/components/ui';
  import { getCurrentUser } from '$lib/userContext';
  import {
    ServiceCard,
    GarminCard,
    GoogleWorkspaceCard,
    SettingsLayout,
    SettingsCard,
  } from '$lib/components/settings';

  const identity = getCurrentUser();

  let services: ServiceCardData[] = $state([]);
  let loading = $state(true);
  let error = $state('');
  let info = $state('');
  let ncTokenBusy = $state(false);
  // Set by a disconnect made here, which the identity record does not learn
  // about until the next `/me`. Otherwise the record is the answer — the
  // settings layout re-resolves it on entry (ISSUE-355), so a connect made
  // elsewhere is already reflected. null = the operator has not enabled
  // encrypted token storage, so there is no card.
  let ncTokenOverride: NextcloudTokenStatus | undefined = $state(undefined);
  const ncToken = $derived(ncTokenOverride ?? identity.user.nextcloud_token ?? null);

  async function loadServices() {
    try {
      services = (await getSettingsServices()).services;
      error = '';
    } catch (e) {
      error = (e as Error).message || 'Failed to load services';
    } finally {
      loading = false;
    }
  }

  onMount(loadServices);

  // A full page navigation, not `goto`: `/reconnect` is a server auth route that
  // answers with a redirect to Nextcloud, so the client router has nothing to
  // resolve. Same shape as GoogleWorkspaceCard's connect.
  //
  // This replaces the card's old instruction to log out and back in, which was
  // the only documented remedy for a credential that had died silently
  // (ISSUE-333).
  function reconnectNextcloud() {
    window.location.href = `${base}/reconnect`;
  }

  async function disconnectNextcloud() {
    ncTokenBusy = true;
    try {
      await disconnectNextcloudToken();
      ncTokenOverride = { connected: false, expires_at: null };
      info = 'Nextcloud connection removed.';
    } catch (e) {
      error = (e as Error).message || 'Disconnect failed';
    } finally {
      ncTokenBusy = false;
    }
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

<SettingsLayout {loading} {error} {info}>
  <p class="hint intro">
    Accounts and per-service credentials for skills that need them. Values are encrypted at rest and
    never sent back to the browser — secret fields are write-only. Module-specific credentials live
    on their own settings pages (<a href="{base}/feeds/settings">feeds</a>,
    <a href="{base}/money/settings">money</a>,
    <a href="{base}/location/settings">location</a>).
  </p>

  {#if ncToken}
    {@const nc = ncToken}
    <SettingsCard
      title="Nextcloud"
      description="When connected, messages you send from web chat appear in Nextcloud Talk under your own name, and read state syncs between web and Talk."
    >
      {#snippet status()}
        <span class="status-pill status-{nc.connected ? 'configured' : 'missing'}">
          {nc.connected ? 'Connected' : 'Not connected'}
        </span>
      {/snippet}
      {#if nc.connected}
        <div class="card-actions">
          <Button variant="secondary" size="sm" onclick={reconnectNextcloud}>Reconnect</Button>
          <Button
            variant="secondary"
            size="sm"
            onclick={disconnectNextcloud}
            disabled={ncTokenBusy}
          >
            {ncTokenBusy ? 'Disconnecting…' : 'Disconnect'}
          </Button>
        </div>
      {:else}
        <div class="card-actions">
          <Button variant="primary" size="sm" onclick={reconnectNextcloud}>Connect</Button>
        </div>
        <p class="empty">
          Connecting signs you in to Nextcloud again and brings you back here. Your session stays as
          it is.
        </p>
      {/if}
    </SettingsCard>
  {/if}

  {#each activeServices as svc (svc.service)}
    {#if svc.custom_ui && svc.service === 'garmin'}
      <GarminCard />
    {:else if svc.custom_ui && svc.service === 'google_workspace'}
      <GoogleWorkspaceCard onChanged={loadServices} />
    {:else}
      <ServiceCard service={svc} onChanged={loadServices} />
    {/if}
  {/each}

  {#if !ncToken && activeServices.length === 0}
    <p class="empty">No connected services are available on this deployment.</p>
  {/if}
</SettingsLayout>

<style>
  .intro {
    margin: 0;
  }

  .card-actions {
    display: flex;
    gap: var(--space-2);
  }
</style>
