<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    getModuleServices,
    getLocationSettingsInfo,
    generateIngestToken,
    type ServiceCard as ServiceCardData,
    type LocationSettingsInfo,
  } from '$lib/api';
  import { ServiceCard, SettingsLayout, SettingsCard } from '$lib/components/settings';
  import { Button, ConfirmDialog } from '$lib/components/ui';
  import QrCode from '$lib/components/location/QrCode.svelte';
  import DeviceTrackerCard from '$lib/components/location/DeviceTrackerCard.svelte';
  import { encodeProvisioning, endpointFromWebhookUrl } from '$lib/location/provisioning';

  let loading = $state(true);
  let error = $state('');

  let moduleServices: ServiceCardData[] = $state([]);
  let moduleEnabled = $state(true);
  let info: LocationSettingsInfo | null = $state(null);

  /**
   * The freshly minted token, held for this page view only.
   *
   * There is no read path for it — the generate call is the one and only time
   * the server hands it back — so leaving the page loses it and the next
   * device needs a new one. That is the whole reason the card says so out
   * loud rather than assuming anyone would guess.
   */
  let minted = $state<{ token: string; endpoint: string; webhookUrl: string } | null>(null);
  let minting = $state(false);
  let mintError = $state('');
  let confirmRotate = $state(false);

  const tokenConfigured = $derived(
    moduleServices.some(
      (svc) => svc.service === 'overland' && svc.configured_keys.includes('ingest_token'),
    ),
  );

  const qrPayload = $derived(
    minted ? encodeProvisioning({ endpoint: minted.endpoint, token: minted.token }) : '',
  );

  async function mint() {
    // ConfirmDialog does not close itself on confirm, so its button stays
    // live for the whole round trip. Without this guard a double-tap mints
    // twice and the QR can end up showing the token that lost the race —
    // which the phone would accept and then 401 on forever.
    if (minting) return;
    minting = true;
    mintError = '';
    try {
      const result = await generateIngestToken();
      minted = {
        token: result.token,
        endpoint: endpointFromWebhookUrl(result.webhook_url),
        webhookUrl: result.webhook_url,
      };
      await reloadServices();
    } catch (e) {
      mintError = e instanceof Error ? e.message : 'Could not generate a token';
    } finally {
      minting = false;
      confirmRotate = false;
    }
  }

  /** Rotating cuts off every device on this account, so it asks first. */
  function requestMint() {
    if (tokenConfigured) confirmRotate = true;
    else void mint();
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
  }

  onMount(refresh);
</script>

<SettingsLayout
  title="Location settings"
  description="Overland GPS connection and place-detection tuning. The ingest token is encrypted at rest and never sent back to the browser."
  {loading}
  {error}
>
  {#if !moduleEnabled}
    <div class="banner info">
      Location module is disabled. Enable it in
      <a href="{base}/settings">Settings → Preferences</a> to manage GPS ingest.
    </div>
  {:else}
    {#each moduleServices as svc (svc.service)}
      <ServiceCard service={svc} onChanged={reloadServices} />
    {/each}

    <!-- Outside the `info` guard below: this card reads the device, not the
         server, and it is the readout that says whether tracking silently
         stopped. A transient server failure must not be what hides it. -->
    <DeviceTrackerCard />

    {#if info}
      <SettingsCard
        title="Provision a device"
        description="Generate a token and scan it from the Istota app on the phone you want to track."
      >
        {#snippet actions()}
          <Button variant="secondary" size="sm" onclick={requestMint} disabled={minting}>
            {tokenConfigured ? 'Generate new token' : 'Generate token'}
          </Button>
        {/snippet}

        {#if mintError}<p class="mint-error">{mintError}</p>{/if}

        {#if minted}
          <div class="provision">
            <QrCode value={qrPayload} label="Location provisioning code" />
            <div class="provision-copy">
              <p class="hint">
                Scan this from <strong>This device</strong> below, in the app on the phone. It is shown
                once — leaving this page loses it, and the next device needs a new token.
              </p>
              <p class="hint">
                For the third-party Overland app, which cannot scan, paste this URL instead. It
                carries the same token and is shown only now, for the same reason.
              </p>
              <code class="webhook-url">{minted.webhookUrl}</code>
            </div>
          </div>
        {:else}
          <p class="hint">
            The token is stored encrypted and never sent back to the browser, so it can only be
            shown at the moment it is generated. Generating a new one immediately cuts off every
            device currently using the old one, which is also how you revoke a lost phone.
          </p>
          <code class="webhook-url">{info.webhook_url}</code>
        {/if}
      </SettingsCard>

      <SettingsCard
        title="Place detection"
        description="Read-only — these knobs are tuned instance-wide."
      >
        <dl class="kv">
          <dt>Accuracy threshold (m)</dt>
          <dd>{info.place_detection.accuracy_threshold_m}</dd>
          <dt>Visit exit (min)</dt>
          <dd>{info.place_detection.visit_exit_minutes}</dd>
        </dl>
      </SettingsCard>
    {/if}
  {/if}
</SettingsLayout>

<ConfirmDialog
  bind:open={confirmRotate}
  title="Generate a new token"
  message="Are you sure? The current token stops working immediately, on every device using it. Any phone still tracking with the old one will stop sending until you rescan its code."
  confirmLabel="Generate"
  confirmVariant="danger"
  confirmDisabled={minting}
  onConfirm={mint}
/>

<style>
  /* Shared .settings/.card/.field/.banner primitives live in
	   web/src/lib/styles/settings.css (imported by app.css). Only location-
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

  .webhook-url {
    display: block;
    padding: var(--space-2) var(--space-2);
    font-family: ui-monospace, SFMono-Regular, monospace;
    word-break: break-all;
  }

  .kv dt {
    color: var(--text-dim);
  }

  .kv dd {
    margin: 0;
    color: var(--text-secondary);
  }
</style>
