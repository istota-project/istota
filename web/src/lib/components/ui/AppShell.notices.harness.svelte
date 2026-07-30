<script lang="ts">
  /* Minimal stand-in for a route: a shell with a header and some content, and
     nothing at all to do with notices — which is the point of the test. */
  import AppShell from './AppShell.svelte';
</script>

<AppShell>
  {#snippet header()}
    <div>Section</div>
  {/snippet}
  {#snippet children()}
    <p>Page content</p>
  {/snippet}
</AppShell>
