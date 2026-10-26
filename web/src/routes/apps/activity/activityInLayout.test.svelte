<script lang="ts">
  /* Test-only harness: Activity inside the Apps layout, which names the app
     in the app bar from its list. Not a route and not collected as a test. */
  import AppsLayout from '../+layout.svelte';
  import Activity from './+page.svelte';
</script>

<AppsLayout>
  <Activity />
</AppsLayout>
