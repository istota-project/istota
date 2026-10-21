<script lang="ts">
  /**
   * The phones that hold a device token for native actions, one row each.
   *
   * Revoking a row stops that phone's native actions (today, saving quick
   * notes from the app icon) on its next send; the app asks for a new token
   * the next time it loads Istota with a session. `thisDeviceId` marks the
   * phone the page is running on, when the app can say which one that is.
   */
  import { deleteDevice, type DeviceToken } from '$lib/api';
  import { Badge, Button, Card, ConfirmDialog, EmptyState, Timestamp } from '$lib/components/ui';
  import { createConfirm } from '$lib/ui/confirm.svelte';
  import type { Snippet } from 'svelte';

  interface Props {
    devices: DeviceToken[];
    thisDeviceId?: string | null;
    /** Called after a revoke, to reload the list. */
    onChanged: () => void | Promise<void>;
    /** A line under the list (the link to location devices). */
    children?: Snippet;
  }

  let { devices, thisDeviceId = null, onChanged, children }: Props = $props();

  const SCOPE_LABELS: Record<string, string> = { notes: 'Quick notes' };

  function scopeText(scopes: string[]): string {
    return scopes.map((scope) => SCOPE_LABELS[scope] ?? scope).join(', ');
  }

  function name(device: DeviceToken): string {
    return device.label || 'Device';
  }

  function isThisDevice(device: DeviceToken): boolean {
    return !!thisDeviceId && device.device_id.toUpperCase() === thisDeviceId.toUpperCase();
  }

  const revoke = createConfirm<DeviceToken>();

  async function confirmRevoke() {
    const target = revoke.target;
    if (!target) return;
    await deleteDevice(target.id);
    await onChanged();
  }
</script>

<Card
  title="Devices"
  description="Phones that can act for you without a web session, such as saving a quick note from the app icon."
>
  {#if devices.length === 0}
    <EmptyState size="sm">
      No devices yet. The Istota app adds the phone it runs on once a quick notes folder is set.
    </EmptyState>
  {:else}
    <ul class="divided-list">
      {#each devices as device (device.id)}
        <li class="device-row">
          <div class="device-text">
            <span class="hstack wrap device-name">
              <span>{name(device)} · …{device.token_tail}</span>
              {#if isThisDevice(device)}
                <Badge variant="info" size="sm">This device</Badge>
              {/if}
            </span>
            <span class="caption">
              {scopeText(device.scopes)} · added <Timestamp value={device.created_at} /> · last used
              <Timestamp value={device.last_used_at} format="relative" empty="never" />
            </span>
          </div>
          <Button
            variant="secondary"
            size="sm"
            onclick={() => revoke.ask(device)}
            ariaLabel={`Revoke ${name(device)} ending ${device.token_tail}`}
          >
            Revoke
          </Button>
        </li>
      {/each}
    </ul>
  {/if}
  {@render children?.()}
</Card>

<ConfirmDialog
  bind:open={revoke.open}
  title="Revoke device"
  message={revoke.target
    ? `Are you sure? ${name(revoke.target)} stops saving quick notes until the app sets it up again.`
    : ''}
  confirmLabel="Revoke"
  busyLabel="Revoking…"
  confirmVariant="danger"
  onConfirm={confirmRevoke}
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
