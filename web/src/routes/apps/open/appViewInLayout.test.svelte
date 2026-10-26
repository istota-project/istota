<script lang="ts">
  /* Test-only harness: the app view mounted inside the Apps layout, which owns
     the app bar, the sidebar and the pane the view measures. Not a route (no
     `+` prefix) and not collected as a test (vitest picks up `*.test.ts`). */
  import AppsLayout from '../+layout.svelte';
  import AppView from './+page.svelte';
</script>

<AppsLayout>
  <AppView />
</AppsLayout>
