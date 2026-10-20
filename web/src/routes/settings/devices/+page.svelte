<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { base } from '$app/paths';
  import { AuthError, listDevices, type DeviceToken } from '$lib/api';
  import { DeviceTokensCard, SettingsLayout } from '$lib/components/settings';
  import { getCurrentUser } from '$lib/userContext';
  import { latest } from '$lib/ui/latest';

  const identity = getCurrentUser();
  const user = $derived(identity.user);

  let devices: DeviceToken[] | null = $state(null);
  let error = $state('');
  let loading = $state(true);
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

  onMount(refresh);
</script>

<SettingsLayout
  description="Phones that can save to your account on their own."
  {loading}
  error={devices ? error : ''}
  loadError={devices ? '' : error}
  onRetry={retry}
>
  {#if devices}
    <DeviceTokensCard {devices} onChanged={refresh}>
      {#if user.features.location}
        <p class="caption">
          Phones that send your location are listed in
          <a href="{base}/location/settings">Location settings</a>.
        </p>
      {/if}
    </DeviceTokensCard>
  {/if}
</SettingsLayout>
