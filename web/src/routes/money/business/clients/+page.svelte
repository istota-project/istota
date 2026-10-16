<script lang="ts">
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';

  onMount(() => {
    // Clients live under Invoicing beside entities and services; this stub is
    // the old Business → Clients route.
    goto(`${base}/money/settings/invoicing`, { replaceState: true });
  });
</script>
