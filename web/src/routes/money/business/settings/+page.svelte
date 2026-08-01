<script lang="ts">
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';

  onMount(() => {
    // Entities and services live under Invoicing since the settings page grew
    // sections; this stub is the old business-settings route.
    goto(`${base}/money/settings/invoicing`, { replaceState: true });
  });
</script>
