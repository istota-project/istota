<script lang="ts">
  /* Test-only harness: the settings page mounted where it actually lives,
     inside the root layout, so what is exercised is the seam between them.

     Settings is the one page that calls `reload()` on the identity context
     (ISSUE-355), which means it is the one page that can drive the layout's own
     state — so a stub context would test everything about it except the part
     that is new.

     Not a route, and not collected as a test: SvelteKit's manifest ignores a
     file in `src/routes/` without the `+` prefix, and vitest picks up
     `*.test.ts` only. */
  import Layout from '../+layout.svelte';
  import Settings from './+page.svelte';
</script>

<Layout>
  <Settings />
</Layout>
