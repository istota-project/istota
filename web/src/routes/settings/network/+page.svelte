<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { base } from '$app/paths';

  // Stored bell rows and bookmarks still name this page.
  onMount(() => {
    void goto(`${base}/settings/jobs`, { replaceState: true });
  });
</script>
