<script lang="ts">
  import { SettingsLayout, PushCard } from '$lib/components/settings';
  import { getUserSettings } from '$lib/settings/userSettingsContext';

  const settings = getUserSettings();
</script>

<SettingsLayout
  description="Choose which notifications send alerts and count toward the app badge."
  loading={settings.loading}
  error={settings.error}
  info={settings.info}
>
  {#if settings.profile}
    <PushCard />
  {/if}
</SettingsLayout>
