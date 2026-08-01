<script lang="ts">
  import { goto } from '$app/navigation';
  import NavLink from './NavLink.svelte';
  import Select from './Select.svelte';

  export interface NavItem {
    href: string;
    label: string;
    active?: boolean;
  }

  interface Props {
    items: NavItem[];
    /** Accessible label for the mobile dropdown. */
    ariaLabel?: string;
  }

  let { items, ariaLabel = 'Section' }: Props = $props();

  // The dropdown must always show a selection; reflect the active section,
  // falling back to the first item when none is active (e.g. on a settings
  // sub-page reached via the cog, not the nav).
  const current = $derived(items.find((i) => i.active)?.href ?? items[0]?.href ?? '');
  const options = $derived(items.map((i) => ({ value: i.href, label: i.label })));

  function onChange(href: string) {
    if (href) goto(href);
  }
</script>

<!-- Desktop: inline links. Under 768px: a dropdown, so a link-only header stays
     one line on a phone instead of wrapping.

     Deliberately the app's `Select` and not a native <select>. The touch floor
     in app.css redefines the --text-* tokens on `input`/`select`/`textarea`, so
     a native control here is pushed to 16px and becomes the visually heaviest
     thing in a compact bar (ISSUE-224). A bits-ui trigger is a <button>: WebKit
     never zoomed for it, so the floor has nothing to fix and never reaches it.
     Its inline padding coincides with `NavLink`'s (both 0.5rem, by different
     token names), so the dropdown sits where the chips it replaces would have;
     its type is one step smaller, which preserves what `.nav-select` was
     designed at. -->
<div class="nav-links">
  {#each items as item (item.href)}
    <NavLink href={item.href} active={item.active}>{item.label}</NavLink>
  {/each}
</div>
<div class="nav-select">
  <Select value={current} {options} onValueChange={onChange} {ariaLabel} />
</div>

<style>
  .nav-links {
    display: flex;
    gap: var(--chip-gap);
    align-items: center;
    flex-wrap: wrap;
    min-width: 0;
  }

  /* A wrapper, because the appearance belongs to `Select`; this only decides
     which of the two navs is on screen and where it sits. */
  .nav-select {
    display: none;
  }

  @media (max-width: 768px) {
    .nav-links {
      display: none;
    }
    .nav-select {
      display: inline-flex;
      min-width: 0;
      /* ShellHeader narrows its gap to --space-1 at this breakpoint to land the
         title on the app nav's wordmark inset, which is right for a borderless
         chip — its padding supplies the rest of the clearance. This control is
         a bordered pill, so its *box* edge would sit 0.25rem from the title and
         the two read as one crowded run (ISSUE-223). Correct it here rather
         than by widening the header gap, which would move the title off the
         inset and over-space the chip row on tablets. */
      margin-inline-start: var(--space-2);
    }
  }
</style>
