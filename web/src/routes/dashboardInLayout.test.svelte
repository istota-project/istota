<script lang="ts">
  /* Test-only harness: the dashboard mounted where it actually lives, inside
     the root layout, so what is exercised is the seam between them — the layout
     resolving the identity and the page reading it — rather than a page handed
     a context by the test.

     Not a route. SvelteKit's manifest ignores any file in `src/routes/` without
     the `+` prefix, and vitest collects `*.test.ts` only, so this is compiled
     solely by the test that imports it. */
  import Layout from './+layout.svelte';
  import Dashboard from './+page.svelte';
</script>

<Layout>
  <Dashboard />
</Layout>
