<script lang="ts">
  import { getShellScrollRoot } from '@minot/ui';

  /* The shell's pane outlives the page inside it, and SvelteKit's scroll reset only
     moves the window, so a new view would keep the old offset. Render this inside an
     `AppShell`: the shell's context does not reach the script of whatever renders it.
     Keyed on what is shown rather than on navigation, since a shallow `pushState`
     fires no `afterNavigate`. */
  let { key }: { key?: unknown } = $props();

  // Derived so a reactive read inside the caller's expression (all of `page.url` for
  // `page.url.pathname`) resets only when the value changes.
  const view = $derived(key);

  const getScrollRoot = getShellScrollRoot();
  $effect(() => {
    void view;
    const root = getScrollRoot?.();
    if (root) root.scrollTop = 0;
  });
</script>
