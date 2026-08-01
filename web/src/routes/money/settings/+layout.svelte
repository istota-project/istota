<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { getModuleServices, type ServiceCard as ServiceCardData } from '$lib/api';
  import { setMoneyServices } from '$lib/money/settingsContext';

  let { children } = $props();

  // The module gate belongs here rather than on each section: the answer is the
  // same for all three, and this layout persists across a section switch, so
  // the check costs one request per visit rather than one per navigation. The
  // services it returns are what the Connections section renders — handed over
  // through context so it does not fetch the same endpoint a second time.
  let services: ServiceCardData[] = $state([]);
  let moduleEnabled = $state(true);
  let loading = $state(true);
  let error = $state('');

  async function load() {
    try {
      const mod = await getModuleServices('money');
      services = mod.services;
      moduleEnabled = mod.module_enabled;
      error = '';
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load money settings';
    } finally {
      loading = false;
    }
  }

  onMount(load);

  setMoneyServices({
    get services() {
      return services;
    },
    reload: load,
  });
</script>

{#if loading}
  <div class="center-msg">Loading…</div>
{:else if error}
  <div class="settings">
    <div class="banner error">{error}</div>
  </div>
{:else if !moduleEnabled}
  <div class="settings">
    <div class="banner info">
      Money module is disabled. Enable it in
      <a href="{base}/settings">Settings → Preferences</a> to manage Monarch credentials and invoicing.
    </div>
  </div>
{:else}
  {@render children()}
{/if}
