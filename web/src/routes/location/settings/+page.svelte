<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    getModuleServices,
    getLocationSettingsInfo,
    createLocationDevice,
    type ServiceCard as ServiceCardData,
    type LocationSettingsInfo,
  } from '$lib/api';
  import { ServiceCard, SettingsLayout } from '$lib/components/settings';
  import { Alert, Button, Card, CopyField } from '@minot/ui';
  import QrCode from '$lib/components/location/QrCode.svelte';
  import DeviceTrackerCard from '$lib/components/location/DeviceTrackerCard.svelte';
  import LocationDevicesCard from '$lib/components/location/LocationDevicesCard.svelte';
  import { encodeProvisioning } from '$lib/location/provisioning';

  let loading = $state(true);
  let error = $state('');

  let moduleServices: ServiceCardData[] = $state([]);
  let moduleEnabled = $state(true);
  let info: LocationSettingsInfo | null = $state(null);

  /**
   * The freshly created code, held for this page view only.
   *
   * There is no read path for it — the create call is the one and only time
   * the server hands the token back — so leaving the page loses it and the
   * next device needs a new code. That is the whole reason the card says so
   * out loud rather than assuming anyone would guess.
   */
  let minted = $state<{ token: string; endpoint: string; webhookUrl: string } | null>(null);
  let minting = $state(false);
  let mintError = $state('');
  let devicesCard: ReturnType<typeof LocationDevicesCard> | undefined = $state();

  const qrPayload = $derived(
    minted ? encodeProvisioning({ endpoint: minted.endpoint, token: minted.token }) : '',
  );

  async function mint() {
    if (minting) return;
    minting = true;
    mintError = '';
    try {
      const result = await createLocationDevice({ kind: 'code' });
      minted = {
        token: result.token,
        endpoint: result.endpoint,
        webhookUrl: result.webhook_url,
      };
      await devicesCard?.reload();
    } catch (e) {
      mintError = e instanceof Error && e.message ? e.message : 'Could not create a code.';
    } finally {
      minting = false;
    }
  }

  async function refresh() {
    loading = true;
    error = '';
    try {
      const [mod, settings] = await Promise.all([
        getModuleServices('location'),
        getLocationSettingsInfo(),
      ]);
      moduleEnabled = mod.module_enabled;
      moduleServices = mod.services;
      info = settings;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load settings';
    } finally {
      loading = false;
    }
  }

  async function reloadServices() {
    try {
      const mod = await getModuleServices('location');
      moduleServices = mod.services;
      moduleEnabled = mod.module_enabled;
    } catch {
      // non-fatal
    }
    // The overland card writes the shared token, which the device list shows.
    await devicesCard?.reload();
  }

  onMount(refresh);
</script>

<SettingsLayout
  title="Location settings"
  description="The devices that send locations to this account, and place-detection tuning. A device's token is shown once, when it is created, and never sent back to the browser."
  {loading}
  {error}
>
  {#if !moduleEnabled}
    <Alert appearance="inline" variant="info">
      Location module is disabled. Enable it in
      <a href="{base}/settings">Settings → Preferences</a> to manage GPS ingest.
    </Alert>
  {:else}
    {#each moduleServices as svc (svc.service)}
      <ServiceCard service={svc} onChanged={reloadServices} />
    {/each}

    <!-- Outside the `info` guard below: this card reads the device, not the
         server, and it is the readout that says whether tracking silently
         stopped. A transient server failure must not be what hides it. -->
    <DeviceTrackerCard onDeviceAdded={() => devicesCard?.reload()} />

    <LocationDevicesCard bind:this={devicesCard} />

    {#if info}
      <Card
        title="Provision a device"
        description="Create a code to scan from the Istota app on a phone, or to paste into Overland. Each code is a device of its own in the list above."
      >
        {#snippet actions()}
          <Button
            variant="secondary"
            size="sm"
            onclick={mint}
            loading={minting}
            loadingLabel="Creating…"
          >
            Create code
          </Button>
        {/snippet}

        {#if mintError}<p class="mint-error">{mintError}</p>{/if}

        {#if minted}
          <div class="provision">
            <QrCode value={qrPayload} label="Location provisioning code" />
            <div class="provision-copy">
              <p class="description">
                Scan this from <strong>This device</strong>, in the app on the phone. It is shown
                once — leaving this page loses it, and the next device needs a new code.
              </p>
              <p class="description">
                For the third-party Overland app, which cannot scan, paste this URL instead. It
                carries the same token and is shown only now, for the same reason.
              </p>
              <CopyField label="Webhook URL" value={minted.webhookUrl} monospace />
            </div>
          </div>
        {:else}
          <p class="description">
            A code is shown once, when it is created, and the server keeps no copy it could show
            again. Creating one does not affect any other device. On a phone with the Istota app,
            open this page in the app and turn tracking on there instead.
          </p>
          <CopyField label="Webhook URL" value={info.webhook_url} monospace />
        {/if}
      </Card>

      <Card title="Place detection" description="Read-only — these knobs are tuned instance-wide.">
        <dl class="kv">
          <dt>Accuracy threshold (m)</dt>
          <dd>{info.place_detection.accuracy_threshold_m}</dd>
          <dt>Visit exit (min)</dt>
          <dd>{info.place_detection.visit_exit_minutes}</dd>
        </dl>
      </Card>
    {/if}
  {/if}
</SettingsLayout>

<style>
  /* Shared .settings/.card/.field/.banner primitives live in
	   minot's settings.css (in @minot/ui/styles.css). Only location-
	   specific styling (webhook URL display, kv list) stays. */

  .provision {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    gap: var(--space-4);
  }

  .provision-copy {
    flex: 1 1 16rem;
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .mint-error {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--status-danger-fg);
  }

  dd {
    color: var(--text-secondary);
  }
</style>
