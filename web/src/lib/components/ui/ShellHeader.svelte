<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    title: string;
    /* Leading slot, before the title — the conventional home for a drawer
       toggle (Material's navigation-icon slot). */
    leading?: Snippet;
    /* Inside the heading, directly after the title: a fact about what the
       title names (the chat's shared-room glyph), not a control, so it sits
       before `nav` and nothing there can come between the two. */
    afterTitle?: Snippet;
    nav?: Snippet;
    tools?: Snippet;
    /* When set, the title itself is a second hit target for the same action as
       the leading control. Deliberately styled plain: a chevron here would
       promise a dropdown, and what opens is a drawer. */
    onTitleClick?: () => void;
    titleActionLabel?: string;
  }

  let { title, leading, afterTitle, nav, tools, onTitleClick, titleActionLabel }: Props = $props();
</script>

<div class="header">
  {#if leading}{@render leading()}{/if}
  <h1 class:with-after={!!afterTitle}>
    {#if onTitleClick}
      <button
        class="title-btn"
        onclick={onTitleClick}
        type="button"
        aria-label={titleActionLabel ? `${title} — ${titleActionLabel}` : undefined}
      >
        {title}
      </button>
    {:else}
      {title}
    {/if}
    {#if afterTitle}{@render afterTitle()}{/if}
  </h1>
  {#if nav}<div class="header-nav">{@render nav()}</div>{/if}
  {#if tools}<div class="header-tools">{@render tools()}</div>{/if}
</div>

<style>
  .header {
    display: flex;
    align-items: center;
    gap: var(--space-4);
    padding: var(--space-3);
    border-bottom: 1px solid var(--border-subtle);
  }

  .header h1 {
    font-size: 1rem;
    font-weight: 600;
    margin: 0;
    min-width: 0;
  }

  .header h1.with-after {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .header h1.with-after .title-btn {
    min-width: 0;
  }

  /* Inherits everything so the heading looks untouched; it is a button purely
	   to widen the drawer's hit area. On desktop the sidebar is permanent, so the
	   toggle it drives is a no-op — harmless, and keeping the markup stable beats
	   branching it on a breakpoint the component can't see. */
  .title-btn {
    font: inherit;
    color: inherit;
    background: none;
    border: none;
    padding: 0;
    text-align: left;
    cursor: pointer;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .header-nav {
    display: flex;
    gap: var(--chip-gap);
    align-items: center;
    flex-wrap: wrap;
    min-width: 0;
  }

  .header-tools {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  @media (max-width: 768px) {
    .header {
      padding: var(--space-2) var(--space-3);
      /* Tight enough that the title lands on the app nav's wordmark: the toggle
			   occupies 0.4→2.0rem after its negative margins, so this gap is what puts
			   the title at the shared 2.25rem inset (see app.css). The toggle itself is
			   left alone — it is also used outside this bar, and its own margins are
			   what centre the glyph on the row's left edge. Nav chips carry
			   --chip-padding-x, so their text still clears the title by 0.75rem. */
      gap: var(--space-1);
    }
  }
</style>
