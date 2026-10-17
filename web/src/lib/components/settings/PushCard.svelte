<script lang="ts">
  import { onMount } from 'svelte';
  import {
    AuthError,
    listPushDevices,
    deletePushDevice,
    type PushDevice,
    listPushCategories,
    type PushCategory,
  } from '$lib/api';
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
  let categories: PushCategory[] = $state([]);
  let categoriesLoading = $state(true);
  let categoriesError = $state('');
  const chatOptions = [
    { value: 'alerts', label: 'Alerts and badge' },
    { value: 'badge', label: 'Badge only' },
    { value: 'off', label: 'Off' },
  ];

  async function loadCategories() {
    categoriesLoading = true;
    categoriesError = '';
    try {
      categories = (await listPushCategories()).categories;
    } catch (e) {
      if (e instanceof AuthError) identity.expireSession();
      else categoriesError = (e as Error).message || 'Could not load notification categories.';
    } finally {
      categoriesLoading = false;
    }
  }

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
    void loadCategories();
  });
</script>

<SettingsCard
  title="Inbox notifications"
  description="Enabled categories send alerts and count toward the app badge. These choices do not change your inbox, its bell count or delivery to email and Talk."
>
  {#if categoriesLoading}
    <p class="hint">Loading categories…</p>
  {:else if categoriesError}
    <p class="banner error" role="alert">{categoriesError}</p>
    <Button variant="secondary" size="sm" onclick={loadCategories}>Retry categories</Button>
  {:else if profile}
    {#each categories as category (category.id)}
      <SettingsField
        label={category.label}
        checkbox
        badge={managedBadge(profile, 'push_inbox_categories')}
      >
        <input
          type="checkbox"
          checked={profile.push_inbox_categories?.[category.id] ?? true}
          disabled={isManaged(profile, 'push_inbox_categories')}
          onchange={(event) => {
            if (profile) {
              profile.push_inbox_categories = {
                ...profile.push_inbox_categories,
                [category.id]: event.currentTarget.checked,
              };
            }
          }}
        />
      </SettingsField>
    {/each}
  {/if}
</SettingsCard>

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
    <SettingsField
      label="Chat notifications"
      labelled={false}
      badge={managedBadge(profile, 'push_chat_alerts')}
      hint="The account default for rooms without their own notification setting. Badge only counts unread messages without alerts; Off excludes them from alerts and the app badge."
    >
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
