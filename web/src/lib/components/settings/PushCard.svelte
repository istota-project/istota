<script lang="ts">
  import { onMount } from 'svelte';
  import { onDocumentVisible } from '$lib/ui/visibility';
  import {
    AuthError,
    listPushDevices,
    deletePushDevice,
    type PushDevice,
    listPushCategories,
    type PushCategory,
  } from '$lib/api';
  import { Button, EmptyState, Select, Card, Field, Checkbox } from '$lib/components/ui';
  import { getUserSettings } from '$lib/settings/userSettingsContext';
  import { isManaged, managedBadge } from '$lib/settings/managed';
  import { getCurrentUser } from '$lib/userContext';
  import {
    pushAvailable,
    pushPermissions,
    registerForPush,
    type PushPermission,
  } from '$lib/platform/nativePush';
  import { connectionContext } from '$lib/platform/nativeConnection';
  import { saveEnrollment } from '$lib/platform/pushEnrollment';
  // The location plugin already opens this app's system Settings page.
  import { openAppSettings } from '$lib/platform/nativeLocation';
  import { invalidateNotificationCounts } from '$lib/stores/notifications';

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
  let native = $state(false);
  let permission: PushPermission | null = $state(null);
  let registering = $state(false);
  let registrationMessage = $state('');
  let permissionRequest = 0;

  async function readPermission() {
    if (registering) return;
    const request = ++permissionRequest;
    try {
      const result = await pushPermissions();
      if (request === permissionRequest) permission = result?.permission ?? null;
    } catch {
      if (request !== permissionRequest) return;
      error = 'Could not read notification permission. Try registering again.';
    }
  }

  async function registerDevice() {
    if (registering) return;
    registering = true;
    permissionRequest += 1;
    error = '';
    registrationMessage = '';
    try {
      // Read before enrolling: the server row names this connection, and the app
      // refuses to record it if the page has since moved to another one.
      const context = await connectionContext();
      const result = await registerForPush();
      if (!result) throw new Error('Push notifications are unavailable in this app build.');
      permission = result.permission;
      if (result.permission !== 'granted') return;
      if (!result.token) throw new Error('No device token was returned. Try again.');
      await saveEnrollment({ ...result, token: result.token }, context);
      registrationMessage = 'This device is registered.';
      invalidateNotificationCounts();
      await loadDevices();
    } catch (e) {
      if (e instanceof AuthError) identity.expireSession();
      else error = (e as Error).message || 'Could not register this device.';
    } finally {
      registering = false;
    }
  }

  async function openSettings() {
    try {
      await openAppSettings();
    } catch {
      error = 'Could not open iOS Settings.';
    }
  }
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
    registrationMessage = '';
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
    native = pushAvailable();
    void loadDevices();
    void loadCategories();
    if (!native) return;
    void readPermission();
    return onDocumentVisible(() => void readPermission());
  });
</script>

<Card
  title="Inbox notifications"
  description="Enabled categories send alerts and count toward the app badge. These choices do not change your inbox, its bell count or delivery to email and Talk."
>
  {#if categoriesLoading}
    <p class="caption">Loading categories…</p>
  {:else if categoriesError}
    <p class="banner error" role="alert">{categoriesError}</p>
    <Button variant="secondary" size="sm" onclick={loadCategories}>Retry categories</Button>
  {:else if profile}
    {#each categories as category (category.id)}
      <Field label={category.label} checkbox badge={managedBadge(profile, 'push_inbox_categories')}>
        <Checkbox
          checked={profile.push_inbox_categories?.[category.id] ?? true}
          disabled={isManaged(profile, 'push_inbox_categories')}
          onchange={(on) => {
            if (profile) {
              profile.push_inbox_categories = {
                ...profile.push_inbox_categories,
                [category.id]: on,
              };
            }
          }}
        />
      </Field>
    {/each}
  {/if}
</Card>

<Card title="Push notifications">
  {#if native}
    {#if permission === 'denied'}
      <p class="description">
        Notifications are denied in iOS Settings. Enable them there, then register this device.
      </p>
      <Button variant="secondary" size="sm" onclick={openSettings}>Open iOS Settings</Button>
    {:else}
      <Button
        variant="secondary"
        size="sm"
        loading={registering}
        loadingLabel="Registering…"
        onclick={registerDevice}
      >
        Register this device
      </Button>
    {/if}
    {#if registrationMessage && permission !== 'denied'}<p class="description" role="status">
        {registrationMessage}
      </p>{/if}
  {:else}
    <p class="description">Register a device from the Istota iOS app.</p>
  {/if}
  <p class="description">iOS notification settings control whether alerts appear on each device.</p>
  {#if error}<p class="banner error" role="alert">{error}</p>{/if}
  {#if loading}
    <p class="caption">Loading devices…</p>
  {:else if devices.length}
    <ul class="devices">
      {#each devices as device (device.id)}
        <li>
          <div>
            <span>iOS {device.app_version} · …{device.token_tail}</span>
            {#if device.last_error}<p class="description">{device.last_error}</p>{/if}
          </div>
          <Button
            variant="secondary"
            size="sm"
            disabled={removing !== null}
            loading={removing === device.id}
            loadingLabel="Removing…"
            onclick={() => removeDevice(device.id)}
            ariaLabel={`Remove device ending ${device.token_tail}`}
          >
            Remove
          </Button>
        </li>
      {/each}
    </ul>
  {:else if !error}
    <EmptyState size="sm">No devices registered.</EmptyState>
  {/if}
  {#if profile}
    <Field
      label="Chat notifications"
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
    </Field>
    <Field label="Show message previews" checkbox badge={managedBadge(profile, 'push_preview')}>
      <Checkbox bind:checked={profile.push_preview} disabled={isManaged(profile, 'push_preview')} />
    </Field>
    <p class="description">
      Previews send message text to Apple for display on your lock screen. Turn them off to send
      only a generic title, badge and link.
    </p>
  {/if}
</Card>

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
