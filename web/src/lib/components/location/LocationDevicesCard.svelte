<script lang="ts">
  /**
   * Every device that can send locations to this account, one token each.
   *
   * Removing a row stops only that device. The shared token from before
   * per-device tokens is listed as one extra row while it exists, because
   * the server cannot tell which devices still hold it.
   */
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    listLocationDevices,
    deleteLocationDevice,
    deleteLegacyIngestToken,
    type LocationDevice,
  } from '$lib/api';
  import { Badge, Button, Card, ConfirmDialog, EmptyState, Timestamp } from '@minot/ui';
  import { trackerAvailable, trackerStatus } from '$lib/platform/nativeLocation';
  import { notifyError } from '@minot/ui/stores/notices';

  type Target = { kind: 'row'; id: number; label: string } | { kind: 'legacy' };

  let devices: LocationDevice[] = $state([]);
  let legacyToken = $state(false);
  let loading = $state(true);
  let loadError = $state('');
  let thisDeviceId: string | null = $state(null);

  let target = $state<Target | null>(null);
  let confirmOpen = $state(false);
  let removing = $state(false);

  /** Reload the list; the page calls this after it mints a code. */
  export async function reload() {
    loadError = '';
    try {
      const result = await listLocationDevices();
      devices = result.devices;
      legacyToken = result.legacy_token;
    } catch (e) {
      loadError = e instanceof Error && e.message ? e.message : 'Could not load devices.';
    } finally {
      loading = false;
    }
  }

  async function readThisDevice() {
    if (!trackerAvailable()) return;
    try {
      thisDeviceId = (await trackerStatus())?.deviceId ?? null;
    } catch {
      // The marker is a convenience; the list stands without it.
    }
  }

  function askRemove(next: Target) {
    target = next;
    confirmOpen = true;
  }

  async function remove() {
    if (!target || removing) return;
    removing = true;
    try {
      if (target.kind === 'legacy') await deleteLegacyIngestToken();
      else await deleteLocationDevice(target.id);
    } catch (e) {
      notifyError(e instanceof Error && e.message ? e.message : 'Could not remove the device.');
    } finally {
      removing = false;
      confirmOpen = false;
      target = null;
    }
    await reload();
  }

  const confirmTitle = $derived(
    target?.kind === 'legacy' ? 'Remove the shared token' : 'Remove device',
  );
  const confirmMessage = $derived(
    target?.kind === 'legacy'
      ? 'Every device still using the shared token stops sending locations.'
      : target
        ? `${target.label} stops sending locations until it is set up again.`
        : '',
  );

  onMount(() => {
    void reload();
    void readThisDevice();
  });
</script>

<Card
  title="Devices"
  description="Each device has its own token. Removing one stops only that device, on its next send."
>
  {#if loading}
    <p class="description">Loading devices…</p>
  {:else if loadError}
    <p class="description">{loadError}</p>
    <div>
      <Button variant="secondary" size="sm" onclick={reload}>Retry</Button>
    </div>
  {:else if devices.length === 0 && !legacyToken}
    <EmptyState size="sm">No devices can send locations yet.</EmptyState>
  {:else}
    <ul class="divided-list">
      {#each devices as device (device.id)}
        <li class="device-row">
          <div class="device-text">
            <span class="hstack wrap device-name">
              <span>{device.label} · …{device.token_tail}</span>
              {#if thisDeviceId && device.device_id === thisDeviceId}
                <Badge variant="info" size="sm">This phone</Badge>
              {/if}
            </span>
            <span class="caption">
              Added <Timestamp value={device.created_at} /> · last sent
              <Timestamp value={device.last_used_at} format="relative" empty="never" />
            </span>
          </div>
          <Button
            variant="secondary"
            size="sm"
            disabled={removing}
            onclick={() => askRemove({ kind: 'row', id: device.id, label: device.label })}
            ariaLabel={`Remove ${device.label} ending ${device.token_tail}`}
          >
            Remove
          </Button>
        </li>
      {/each}
      {#if legacyToken}
        <li class="device-row">
          <div class="device-text">
            <span class="device-name">Shared token (from before per-device tokens)</span>
            <span class="caption">
              Every device set up before this change uses it. Remove it once each one has been set
              up again.
            </span>
          </div>
          <Button
            variant="secondary"
            size="sm"
            disabled={removing}
            onclick={() => askRemove({ kind: 'legacy' })}
            ariaLabel="Remove the shared token"
          >
            Remove
          </Button>
        </li>
      {/if}
    </ul>
  {/if}
  <p class="caption">
    To revoke everything a phone holds, quick notes included, use
    <a href="{base}/settings/devices">Settings → Devices</a>.
  </p>
</Card>

<ConfirmDialog
  bind:open={confirmOpen}
  title={confirmTitle}
  message={confirmMessage}
  confirmLabel="Remove"
  busyLabel="Removing…"
  confirmVariant="danger"
  onConfirm={remove}
/>

<style>
  .device-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
  }

  .device-text {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }

  .device-name {
    overflow-wrap: anywhere;
  }
</style>
