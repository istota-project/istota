<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { base } from '$app/paths';
  import { AuthError, listDevices, type DeviceToken } from '$lib/api';
  import { DeviceTokensCard, SettingsLayout } from '$lib/components/settings';
  import { getCurrentUser } from '$lib/userContext';
  import { latest } from '$lib/ui/latest';
  import { actionsStatus } from '$lib/platform/nativeActions';

  const identity = getCurrentUser();
  const user = $derived(identity.user);

  let devices: DeviceToken[] | null = $state(null);
  let error = $state('');
  let loading = $state(true);
  let thisDeviceId: string | null = $state(null);
  const loads = latest();
  onDestroy(() => loads.dispose());

  async function refresh() {
    const current = loads.next();
    try {
      const result = await listDevices();
      if (!current()) return;
      devices = result.devices;
      error = '';
    } catch (e) {
      if (!current()) return;
      if (e instanceof AuthError) identity.expireSession();
      else error = (e as Error).message || 'Could not load your devices.';
    } finally {
      if (current()) loading = false;
    }
  }

  function retry() {
    loading = true;
    error = '';
    void refresh();
  }

  onMount(() => {
    void refresh();
    void actionsStatus().then((status) => {
      thisDeviceId = status?.deviceId ?? null;
    });
  });
</script>

<SettingsLayout
  description="Phones that can save to your account on their own."
  {loading}
  error={devices ? error : ''}
  loadError={devices ? '' : error}
  onRetry={retry}
>
  {#if devices}
    <DeviceTokensCard {devices} {thisDeviceId} onChanged={refresh}>
      {#if user.features.location}
        <p class="caption">
          Phones that send your location are listed in
          <a href="{base}/location/settings">Location settings</a>.
        </p>
      {/if}
    </DeviceTokensCard>
  {/if}
</SettingsLayout>
