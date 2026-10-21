<script lang="ts">
  /**
   * Every phone that holds a credential for this account, one row each, and
   * the location trackers that name no phone.
   *
   * A phone's row covers its device token (quick notes) and its location
   * token, joined by the server on the device id, and Revoke removes both in
   * one request. Overland and QR-code tokens and the shared token from before
   * per-device tokens get their own card with a Remove each. `thisDeviceId`
   * marks the phone the page is running on, when the app can say which one.
   */
  import {
    deleteLegacyIngestToken,
    deleteLocationDevice,
    revokePhone,
    type DevicesResponse,
    type Phone,
    type PhoneLocationToken,
  } from '$lib/api';
  import { Badge, Button, Card, ConfirmDialog, EmptyState, Timestamp } from '@minot/ui';
  import { createConfirm } from '@minot/ui/ui/confirm.svelte';
  import type { Snippet } from 'svelte';

  interface Props {
    devices: DevicesResponse;
    thisDeviceId?: string | null;
    /** Called after a revoke or a remove, to reload the list. */
    onChanged: () => void | Promise<void>;
    /** A line under the phones (the link to set up location tracking). */
    children?: Snippet;
  }

  let { devices, thisDeviceId = null, onChanged, children }: Props = $props();

  const SCOPE_LABELS: Record<string, string> = { notes: 'Quick notes' };

  function capabilities(phone: Phone): string[] {
    const names = (phone.token?.scopes ?? []).map((scope) => SCOPE_LABELS[scope] ?? scope);
    if (phone.location) names.push('Location');
    return names;
  }

  function name(phone: Phone): string {
    return phone.label || 'Device';
  }

  function tails(phone: Phone): string {
    return [phone.token, phone.location]
      .filter((part) => part !== null)
      .map((part) => `…${part.token_tail}`)
      .join(' ');
  }

  function isThisDevice(phone: Phone): boolean {
    return !!thisDeviceId && phone.device_id.toUpperCase() === thisDeviceId.toUpperCase();
  }

  function stops(phone: Phone): string {
    const parts: string[] = [];
    if (phone.token) parts.push('saving quick notes');
    if (phone.location) parts.push('sending locations');
    return parts.join(' and ');
  }

  type TrackerTarget = { kind: 'row'; tracker: PhoneLocationToken } | { kind: 'legacy' };

  const revoke = createConfirm<Phone>();
  const removeTracker = createConfirm<TrackerTarget>();

  async function confirmRevoke() {
    const target = revoke.target;
    if (!target) return;
    await revokePhone(target.device_id);
    await onChanged();
  }

  async function confirmRemoveTracker() {
    const target = removeTracker.target;
    if (!target) return;
    if (target.kind === 'legacy') await deleteLegacyIngestToken();
    else await deleteLocationDevice(target.tracker.id);
    await onChanged();
  }

  const hasTrackers = $derived(
    devices.location_trackers.length > 0 || devices.legacy_location_token,
  );
</script>

<Card
  title="Devices"
  description="Phones that can act for you without a web session: saving a quick note from the app icon, or sending your location."
>
  {#if devices.devices.length === 0}
    <EmptyState size="sm">
      No devices yet. The Istota app adds the phone it runs on once a quick notes folder is set or
      location tracking is turned on.
    </EmptyState>
  {:else}
    <ul class="divided-list">
      {#each devices.devices as phone (phone.device_id)}
        <li class="device-row">
          <div class="device-text">
            <span class="hstack wrap device-name">
              <span>{name(phone)} · {tails(phone)}</span>
              {#if isThisDevice(phone)}
                <Badge variant="info" size="sm">This device</Badge>
              {/if}
            </span>
            <span class="caption">
              {capabilities(phone).join(', ')} · added <Timestamp value={phone.created_at} /> · last used
              <Timestamp value={phone.last_used_at} format="relative" empty="never" />
            </span>
          </div>
          <Button
            variant="secondary"
            size="sm"
            onclick={() => revoke.ask(phone)}
            ariaLabel={`Revoke ${name(phone)} ending ${tails(phone)}`}
          >
            Revoke
          </Button>
        </li>
      {/each}
    </ul>
  {/if}
  {@render children?.()}
</Card>

{#if hasTrackers}
  <Card
    title="Other location trackers"
    description="Tokens set up from a QR code or pasted into a tracker such as Overland. Removing one stops only that tracker, on its next send."
  >
    <ul class="divided-list">
      {#each devices.location_trackers as tracker (tracker.id)}
        <li class="device-row">
          <div class="device-text">
            <span class="device-name">{tracker.label} · …{tracker.token_tail}</span>
            <span class="caption">
              Added <Timestamp value={tracker.created_at} /> · last sent
              <Timestamp value={tracker.last_used_at} format="relative" empty="never" />
            </span>
          </div>
          <Button
            variant="secondary"
            size="sm"
            onclick={() => removeTracker.ask({ kind: 'row', tracker })}
            ariaLabel={`Remove ${tracker.label} ending ${tracker.token_tail}`}
          >
            Remove
          </Button>
        </li>
      {/each}
      {#if devices.legacy_location_token}
        <li class="device-row">
          <div class="device-text">
            <span class="device-name">Shared token (from before per-device tokens)</span>
            <span class="caption">
              Every tracker set up before this change uses it. Remove it once each one has been set
              up again.
            </span>
          </div>
          <Button
            variant="secondary"
            size="sm"
            onclick={() => removeTracker.ask({ kind: 'legacy' })}
            ariaLabel="Remove the shared token"
          >
            Remove
          </Button>
        </li>
      {/if}
    </ul>
  </Card>
{/if}

<ConfirmDialog
  bind:open={revoke.open}
  title="Revoke device"
  message={revoke.target
    ? `Are you sure? ${name(revoke.target)} stops ${stops(revoke.target)} until it is set up again.`
    : ''}
  confirmLabel="Revoke"
  busyLabel="Revoking…"
  confirmVariant="danger"
  onConfirm={confirmRevoke}
/>

<ConfirmDialog
  bind:open={removeTracker.open}
  title={removeTracker.target?.kind === 'legacy' ? 'Remove the shared token' : 'Remove tracker'}
  message={removeTracker.target?.kind === 'legacy'
    ? 'Every tracker still using the shared token stops sending locations.'
    : removeTracker.target
      ? `${removeTracker.target.tracker.label} stops sending locations until it is set up again.`
      : ''}
  confirmLabel="Remove"
  busyLabel="Removing…"
  confirmVariant="danger"
  onConfirm={confirmRemoveTracker}
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
