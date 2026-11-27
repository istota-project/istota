<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { base } from '$app/paths';
  import { AuthError, getSettingsPlugins, type PluginsResponse } from '$lib/api';
  import { Badge, Card, EmptyState, SettingsLayout } from '@minot/ui';
  import { notifyError } from '@minot/ui/stores/notices';
  import { latest } from '@minot/ui/ui/latest';
  import { ServiceCard } from '$lib/components/settings';
  import { getCurrentUser } from '$lib/userContext';

  const identity = getCurrentUser();

  let listing: PluginsResponse | null = $state(null);
  let error = $state('');
  let loading = $state(true);
  const loads = latest();
  onDestroy(() => loads.dispose());

  // A reload after a save that fails leaves the cards on screen and says so
  // out of band; a first load that fails has only the pane to say it in.
  async function refresh() {
    const current = loads.next();
    try {
      const result = await getSettingsPlugins();
      if (!current()) return;
      listing = result;
      error = '';
    } catch (e) {
      if (!current()) return;
      if (e instanceof AuthError) {
        identity.expireSession();
        return;
      }
      const message = (e as Error).message || 'The plugins could not be loaded.';
      if (listing) notifyError(message);
      else error = message;
    } finally {
      if (current()) loading = false;
    }
  }

  function retry() {
    loading = true;
    error = '';
    void refresh();
  }

  onMount(() => void refresh());
</script>

<SettingsLayout
  description="Skills added to this installation as plugins, and the credentials they need."
  {loading}
  loadError={listing ? '' : error}
  onRetry={retry}
>
  {#if listing}
    {#if listing.plugins.length === 0}
      <EmptyState>No plugins are available on this installation.</EmptyState>
    {:else}
      <Card title="Installed plugins">
        <ul class="divided-list">
          {#each listing.plugins as plugin (plugin.skill)}
            <li class="plugin-row">
              <div class="plugin-main">
                <span><code>{plugin.skill}</code></span>
                {#if plugin.description}
                  <span class="caption">{plugin.description}</span>
                {/if}
                <span class="caption">{plugin.plugin}</span>
              </div>
              {#if plugin.disabled}
                <Badge size="sm" tone="subtle" uppercase={false} variant="neutral">Disabled</Badge>
              {/if}
            </li>
          {/each}
        </ul>
        {#if listing.plugins.some((plugin) => plugin.disabled)}
          <p class="caption">
            A disabled plugin is one you switched off under Disabled skills in
            <a href="{base}/settings/preferences">Preferences</a>.
          </p>
        {/if}
      </Card>

      {#each listing.services as svc (svc.service)}
        <ServiceCard service={svc} onChanged={refresh} />
      {/each}
    {/if}
  {/if}
</SettingsLayout>

<style>
  .plugin-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .plugin-main {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
    overflow-wrap: anywhere;
  }
</style>
