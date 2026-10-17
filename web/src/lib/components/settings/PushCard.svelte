<script lang="ts">
  import { onMount } from 'svelte';
  import { AuthError, listPushDevices, deletePushDevice, type PushDevice } from '$lib/api';
  import { Button, EmptyState, Select } from '$lib/components/ui';
  import { SettingsCard, SettingsField } from '$lib/components/settings';
  import { getUserSettings } from '$lib/settings/userSettingsContext';
  import { isManaged, managedBadge } from '$lib/settings/managed';
  import { getCurrentUser } from '$lib/userContext';

  const settings = getUserSettings();
  const identity = getCurrentUser();
  const profile = $derived(settings.profile);
  let devices: PushDevice[] = $state([]);
  let loading = $state(true);
  let error = $state('');
  let removing: number | null = $state(null);
  const chatOptions = [
    { value: 'alerts', label: 'Alerts and badge' },
    { value: 'badge', label: 'Badge only' },
    { value: 'off', label: 'Off' },
  ];

  async function loadDevices() {
    try {
      devices = (await listPushDevices()).devices;
    } catch (e) {
      if (e instanceof AuthError) identity.expireSession();
      else error = (e as Error).message || 'Could not load devices.';
    } finally {
      loading = false;
    }
  }

  async function removeDevice(id: number) {
    removing = id;
    error = '';
    try {
      await deletePushDevice(id);
      devices = devices.filter((device) => device.id !== id);
    } catch (e) {
      if (e instanceof AuthError) identity.expireSession();
      else error = (e as Error).message || 'Could not remove the device.';
    } finally {
      removing = null;
    }
  }

  onMount(() => {
    void loadDevices();
  });
</script>

<SettingsCard title="Push notifications">
  <p class="hint">Register a device from the Istota iOS app.</p>
  <p class="hint">iOS notification settings control whether alerts appear on each device.</p>
  {#if error}<p class="banner error" role="alert">{error}</p>{/if}
  {#if loading}
    <p class="hint">Loading devices…</p>
  {:else if devices.length}
    <ul class="devices">
      {#each devices as device (device.id)}
        <li>
          <div>
            <span>iOS {device.app_version} · …{device.token_tail}</span>
            {#if device.last_error}<p class="hint">{device.last_error}</p>{/if}
          </div>
          <Button
            variant="secondary"
            size="sm"
            disabled={removing !== null}
            onclick={() => removeDevice(device.id)}
            ariaLabel={`Remove device ending ${device.token_tail}`}
          >
            {removing === device.id ? 'Removing…' : 'Remove'}
          </Button>
        </li>
      {/each}
    </ul>
  {:else if !error}
    <EmptyState size="sm">No devices registered.</EmptyState>
  {/if}
  {#if profile}
    <SettingsField label="Chat notifications" badge={managedBadge(profile, 'push_chat_alerts')}>
      <Select
        value={profile.push_chat_alerts ?? 'alerts'}
        options={chatOptions}
        disabled={isManaged(profile, 'push_chat_alerts')}
        ariaLabel="Chat notifications"
        fullWidth
        onValueChange={(value) => {
          if (profile) profile.push_chat_alerts = value as 'alerts' | 'badge' | 'off';
        }}
      />
    </SettingsField>
    <SettingsField
      label="Show message previews"
      checkbox
      badge={managedBadge(profile, 'push_preview')}
    >
      <input
        type="checkbox"
        bind:checked={profile.push_preview}
        disabled={isManaged(profile, 'push_preview')}
      />
    </SettingsField>
    <p class="hint">
      Previews send message text to Apple for display on your lock screen. Turn them off to send
      only a generic title, badge and link.
    </p>
  {/if}
</SettingsCard>

<style>
  .devices {
    list-style: none;
    padding: 0;
  }
  .devices li {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    padding-block: var(--space-2);
  }
</style>
