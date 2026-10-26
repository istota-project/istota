<script lang="ts">
  /* Test-only harness: "All apps" mounted inside the Apps layout, since the
     page reads the list the layout fetched. Not a route (no `+` prefix) and
     not collected as a test (vitest picks up `*.test.ts` only). */
  import AppsLayout from './+layout.svelte';
  import AllApps from './+page.svelte';
</script>

<AppsLayout>
  <AllApps />
</AppsLayout>
