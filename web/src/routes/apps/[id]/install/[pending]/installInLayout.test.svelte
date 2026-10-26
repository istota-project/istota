<script lang="ts">
  /* Test-only harness: the approval screen mounted inside the Apps layout,
     which owns the app bar and the list the screen reloads after approving.
     Not a route (no `+` prefix) and not collected as a test (vitest picks up
     `*.test.ts` only). */
  import AppsLayout from '../../../+layout.svelte';
  import Install from './+page.svelte';
</script>

<AppsLayout>
  <Install />
</AppsLayout>
