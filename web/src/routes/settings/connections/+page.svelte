<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    AuthError,
    getMailAccounts,
    getSettingsServices,
    type MailAccountsSettings,
    type ServiceCard as ServiceCardData,
    type NextcloudTokenStatus,
  } from '$lib/api';
  import { EmptyState } from '$lib/components/ui';
  import { notifyError } from '$lib/stores/notices';
  import { getCurrentUser } from '$lib/userContext';
  import {
    ServiceCard,
    GarminCard,
    GoogleWorkspaceCard,
    MailAccountsCard,
    NextcloudCard,
    SettingsLayout,
  } from '$lib/components/settings';

  const identity = getCurrentUser();

  let services: ServiceCardData[] = $state([]);
  let loading = $state(true);
  let error = $state('');
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

  // Connected mail accounts have their own switch, `[mail_accounts] enabled`,
  // independent of the services list: off, the listing is a 404 and there is
  // no card. `mailShown` keeps a card that loaded once on screen through a
  // later failed reload.
  let mail: MailAccountsSettings | null = $state(null);
  let mailError = $state('');
  let mailShown = $state(false);

  async function loadMail() {
    try {
      mail = await getMailAccounts();
      mailError = '';
      mailShown = true;
    } catch (e) {
      if ((e as { status?: number }).status === 404) {
        mail = null;
        mailShown = false;
      } else {
        mailFailed(e);
      }
    }
  }

  // A refresh after a change that fails leaves the list on screen and says so
  // out of band; a first load that fails has only the card to say it in.
  function mailFailed(e: unknown) {
    if (e instanceof AuthError) {
      identity.expireSession();
      return;
    }
    const message = (e as Error).message || 'The mail accounts could not be loaded.';
    if (mail) notifyError(message);
    else mailError = message;
    mailShown = true;
  }

  onMount(() => {
    void loadServices();
    void loadMail();
  });

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

<SettingsLayout {loading} {error}>
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

  {#each activeServices as svc (svc.service)}
    {#if svc.custom_ui && svc.service === 'garmin'}
      <GarminCard />
    {:else if svc.custom_ui && svc.service === 'google_workspace'}
      <GoogleWorkspaceCard onChanged={loadServices} />
    {:else}
      <ServiceCard service={svc} onChanged={loadServices} />
    {/if}
  {/each}

  {#if mailShown}
    <MailAccountsCard
      settings={mail}
      loadError={mailError}
      onReload={loadMail}
      onSaved={loadMail}
      onError={mailFailed}
    />
  {/if}

  {#if !ncToken && activeServices.length === 0 && !mailShown}
    <EmptyState>No connected services are available on this deployment.</EmptyState>
  {/if}
</SettingsLayout>

<style>
  .intro {
    margin: 0;
  }
</style>
