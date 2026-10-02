<script lang="ts">
  // The section was /health/bloodwork until it became Labs (#598). Bookmarks
  // and old notification links still land there, so each old page forwards to
  // its new path with the query string intact.
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { base } from '$app/paths';
  import { page } from '$app/state';

  let { to }: { to: string } = $props();

  onMount(() => {
    goto(`${base}/health/labs${to}${page.url.search}`, { replaceState: true });
  });
</script>
